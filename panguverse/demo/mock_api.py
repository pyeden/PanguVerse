"""内置被测量（SUT）：一个零依赖的内存版业务 API。

存在的意义：让「API 自动化测试闭环」可以**离线、可复现**地跑通——
不需要任何外部服务、账号或网络，一条命令即可演示完整闭环。

覆盖的真实形状：
- 认证链路（登录拿 token → 携带 token 访问业务接口 → 401 失效）
- CRUD（用户）与业务状态机（订单 CREATED → PAID / CANCELLED，409 冲突）
- 故障注入：``/api/flaky`` 前 N 次返回 500（验证重试自愈）、``/api/slow`` 慢响应（验证超时与耗时断言）
- 调试开关：``/api/debug/invalidate-tokens`` 让已发 token 立即失效（验证「重新认证」自愈）

也可以单独启动当靶子用::

    python -m panguverse.demo.mock_api --port 8899
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class MockState:
    """服务端内存状态（每个实例独立，测试之间互不干扰）。"""

    def __init__(self, *, flaky_failures: int = 2, token_ttl: float = 3600.0) -> None:
        self.lock = threading.Lock()
        self.flaky_failures = flaky_failures
        self.token_ttl = token_ttl
        self.flaky_hits = 0
        self.tokens: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.request_count = 0
        self._seed()

    def _seed(self) -> None:
        user_id = "u_1001"
        self.users[user_id] = {
            "id": user_id,
            "name": "demo",
            "email": "demo@panguverse.dev",
            "role": "admin",
            "created_at": time.time(),
        }
        self.credentials = {"demo": "demo123", "tester": "test123"}
        for name in self.credentials:
            if name == "tester":
                self.users["u_1002"] = {
                    "id": "u_1002",
                    "name": "tester",
                    "email": "tester@panguverse.dev",
                    "role": "user",
                    "created_at": time.time(),
                }

    # ------------------------------------------------------------------ #
    def issue_token(self, username: str) -> str:
        token = f"tok_{uuid.uuid4().hex[:24]}"
        self.tokens[token] = {"username": username, "issued_at": time.time()}
        return token

    def auth(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        info = self.tokens.get(token)
        if not info:
            return None
        if self.token_ttl > 0 and time.time() - info["issued_at"] > self.token_ttl:
            self.tokens.pop(token, None)
            return None
        return info

    def next_flaky(self) -> tuple[bool, int]:
        with self.lock:
            self.flaky_hits += 1
            hit = self.flaky_hits
        return hit > self.flaky_failures, hit


class _Router:
    """极简路由：``(method, regex) -> handler(ctx) -> (status, body, headers)``。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self.routes.append((method.upper(), re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str) -> tuple[Callable[..., Any] | None, dict[str, str]]:
        for route_method, regex, handler in self.routes:
            if route_method != method.upper():
                continue
            found = regex.match(path)
            if found:
                return handler, found.groupdict()
        return None, {}


def build_router(state: MockState) -> _Router:
    router = _Router()

    # ---------------- 健康 / 调试 ---------------- #
    def health(ctx: dict[str, Any]) -> Any:
        return 200, {"status": "ok", "service": "panguverse-mock", "time": time.time()}

    def flaky(ctx: dict[str, Any]) -> Any:
        passed, hit = state.next_flaky()
        if not passed:
            return 500, {"error": "injected_failure", "attempt": hit, "message": "故障注入：第 %d 次调用" % hit}
        return 200, {"status": "ok", "attempt": hit, "message": "第 %d 次调用恢复正常" % hit}

    def slow(ctx: dict[str, Any]) -> Any:
        ms = int(ctx["query"].get("ms", ["300"])[0])
        time.sleep(max(0, ms) / 1000.0)
        return 200, {"status": "ok", "slept_ms": ms}

    def echo(ctx: dict[str, Any]) -> Any:
        return 200, {
            "method": ctx["method"],
            "path": ctx["path"],
            "query": {k: v[0] for k, v in ctx["query"].items()},
            "headers": {k.lower(): v for k, v in ctx["headers"].items()},
            "body": ctx["json"],
        }

    def invalidate(ctx: dict[str, Any]) -> Any:
        count = len(state.tokens)
        state.tokens.clear()
        return 200, {"invalidated": count, "message": "所有 token 已失效"}

    def reset_flaky(ctx: dict[str, Any]) -> Any:
        with state.lock:
            state.flaky_hits = 0
        return 200, {"flaky_hits": 0}

    # ---------------- 认证 ---------------- #
    def login(ctx: dict[str, Any]) -> Any:
        body = ctx["json"] or {}
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if state.credentials.get(username) != password:
            return 401, {"error": "invalid_credentials", "message": "用户名或密码错误"}
        token = state.issue_token(username)
        user = next((u for u in state.users.values() if u["name"] == username), None)
        return 200, {"token": token, "token_type": "Bearer", "expires_in": int(state.token_ttl), "user": user}

    def me(ctx: dict[str, Any]) -> Any:
        info = state.auth(ctx["bearer"])
        if not info:
            return 401, {"error": "unauthorized", "message": "token 无效或已过期"}
        user = next((u for u in state.users.values() if u["name"] == info["username"]), None)
        return 200, {"data": user}

    # ---------------- 用户 ---------------- #
    def list_users(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        limit = int(ctx["query"].get("limit", ["50"])[0])
        items = list(state.users.values())[:limit]
        return 200, {"total": len(state.users), "items": items}

    def create_user(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        body = ctx["json"] or {}
        if not body.get("name"):
            return 400, {"error": "invalid_request", "message": "name 必填"}
        user_id = f"u_{uuid.uuid4().hex[:6]}"
        user = {
            "id": user_id,
            "name": body["name"],
            "email": body.get("email", f"{body['name']}@panguverse.dev"),
            "role": body.get("role", "user"),
            "created_at": time.time(),
        }
        state.users[user_id] = user
        return 201, {"data": user}

    def get_user(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        user = state.users.get(ctx["params"]["user_id"])
        if not user:
            return 404, {"error": "not_found", "message": f"用户不存在: {ctx['params']['user_id']}"}
        return 200, {"data": user}

    def delete_user(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        removed = state.users.pop(ctx["params"]["user_id"], None)
        if not removed:
            return 404, {"error": "not_found"}
        return 204, None

    # ---------------- 订单（业务状态机） ---------------- #
    def create_order(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        body = ctx["json"] or {}
        sku = str(body.get("sku", ""))
        quantity = body.get("quantity", 0)
        if not sku:
            return 400, {"error": "invalid_request", "message": "sku 必填"}
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return 400, {"error": "invalid_request", "message": "quantity 必须是整数"}
        if quantity <= 0:
            return 400, {"error": "invalid_request", "message": "quantity 必须大于 0"}
        order_id = f"ord_{uuid.uuid4().hex[:8]}"
        order = {
            "id": order_id,
            "sku": sku,
            "quantity": quantity,
            "amount": round(float(body.get("unit_price", 99.0)) * quantity, 2),
            "status": "CREATED",
            "created_at": time.time(),
        }
        state.orders[order_id] = order
        return 201, {"data": order}

    def list_orders(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        status = ctx["query"].get("status", [None])[0]
        items = [o for o in state.orders.values() if not status or o["status"] == status]
        return 200, {"total": len(items), "items": items}

    def get_order(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        order = state.orders.get(ctx["params"]["order_id"])
        if not order:
            return 404, {"error": "not_found", "message": f"订单不存在: {ctx['params']['order_id']}"}
        return 200, {"data": order}

    def pay_order(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        order = state.orders.get(ctx["params"]["order_id"])
        if not order:
            return 404, {"error": "not_found"}
        if order["status"] == "PAID":
            return 409, {"error": "conflict", "message": "订单已支付", "data": order}
        if order["status"] == "CANCELLED":
            return 409, {"error": "conflict", "message": "订单已取消", "data": order}
        order["status"] = "PAID"
        order["paid_at"] = time.time()
        return 200, {"data": order}

    def cancel_order(ctx: dict[str, Any]) -> Any:
        if not state.auth(ctx["bearer"]):
            return 401, {"error": "unauthorized"}
        order = state.orders.get(ctx["params"]["order_id"])
        if not order:
            return 404, {"error": "not_found"}
        if order["status"] == "PAID":
            return 409, {"error": "conflict", "message": "已支付订单不可取消"}
        order["status"] = "CANCELLED"
        return 200, {"data": order}

    # ---------------- 注册 ---------------- #
    router.add("GET", r"/api/health", health)
    router.add("GET", r"/api/flaky", flaky)
    router.add("GET", r"/api/slow", slow)
    router.add("GET", r"/api/echo", echo)
    router.add("POST", r"/api/debug/invalidate-tokens", invalidate)
    router.add("POST", r"/api/debug/reset-flaky", reset_flaky)
    router.add("POST", r"/api/login", login)
    router.add("GET", r"/api/me", me)
    router.add("GET", r"/api/users", list_users)
    router.add("POST", r"/api/users", create_user)
    router.add("GET", r"/api/users/(?P<user_id>[^/]+)", get_user)
    router.add("DELETE", r"/api/users/(?P<user_id>[^/]+)", delete_user)
    router.add("GET", r"/api/orders", list_orders)
    router.add("POST", r"/api/orders", create_order)
    router.add("GET", r"/api/orders/(?P<order_id>[^/]+)", get_order)
    router.add("POST", r"/api/orders/(?P<order_id>[^/]+)/pay", pay_order)
    router.add("POST", r"/api/orders/(?P<order_id>[^/]+)/cancel", cancel_order)
    return router


def _make_handler(state: MockState) -> type[BaseHTTPRequestHandler]:
    router = build_router(state)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PanguVerseMock/0.1"
        quiet = True

        def log_message(self, fmt: str, *args: Any) -> None:
            if not Handler.quiet:
                super().log_message(fmt, *args)

        # ---------------------------------------------------------- #
        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload: Any = None
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "invalid_json", "message": "请求体不是合法 JSON"})
                    return
            auth_header = self.headers.get("Authorization") or ""
            bearer = auth_header.split(" ", 1)[1].strip() if auth_header.lower().startswith("bearer ") else None
            ctx = {
                "method": method,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers),
                "json": payload,
                "raw": raw,
                "bearer": bearer,
                "params": {},
            }
            handler, params = router.match(method, parsed.path)
            if handler is None:
                self._send(404, {"error": "no_route", "message": f"{method} {parsed.path} 未定义"})
                return
            ctx["params"] = params
            with state.lock:
                state.request_count += 1
            try:
                status, body = handler(ctx)
            except Exception as exc:  # noqa: BLE001 - mock 服务不应崩
                self._send(500, {"error": "mock_internal_error", "message": str(exc)})
                return
            self._send(status, body)

        def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
            payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            if payload:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-PanguVerse-Mock", "1")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def do_PUT(self) -> None:
            self._handle("PUT")

    return Handler


class MockAPI:
    """可嵌入测试的 mock 服务：``with MockAPI(port=0) as api: ... api.base_url``"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        flaky_failures: int = 2,
        token_ttl: float = 3600.0,
        quiet: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.state = MockState(flaky_failures=flaky_failures, token_ttl=token_ttl)
        handler = _make_handler(self.state)
        handler.quiet = quiet
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> str:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="panguverse-mock", daemon=True)
        self.thread.start()
        return self.base_url

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)

    def __enter__(self) -> MockAPI:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PanguVerse 内置 mock API（被测量）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--flaky-failures", type=int, default=2, help="前 N 次调用 /api/flaky 返回 500")
    parser.add_argument("--token-ttl", type=float, default=3600.0, help="token 有效期（秒）")
    parser.add_argument("--verbose", action="store_true", help="打印访问日志")
    args = parser.parse_args(argv)

    api = MockAPI(
        args.host,
        args.port,
        flaky_failures=args.flaky_failures,
        token_ttl=args.token_ttl,
        quiet=not args.verbose,
    )
    api.start()
    print(f"[mock-api] 已启动: {api.base_url}  (Ctrl+C 退出)")
    print(f"[mock-api] 登录账户: demo/demo123, tester/test123；/api/flaky 前 {args.flaky_failures} 次返回 500")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[mock-api] 正在停止…")
    finally:
        api.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
