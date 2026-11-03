"""HTTP 服务：静态资源 + API 路由。

- ``ThreadingHTTPServer`` + 自定义 ``BaseHTTPRequestHandler``，``allow_reuse_address``；
- 默认只服务本机单人使用：**没有认证**，不可暴露到公网；
- 静态资源只认白名单（``index.html``/``app.js``/``style.css``/``favicon.svg``），
  并用 ``realpath`` 前缀校验兜底，杜绝目录穿越；
- 前端资源缺失时 ``GET /`` 返回 503 + 明确文案，而不是让服务崩溃。
"""

from __future__ import annotations

import ipaddress
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .api import Response as ApiResponse
from .api import WebAPI, error_response
from .auth import AuthManager

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_MIME = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
# 前端用的是 hash 路由（#/overview），只有深链才会带这些前缀；其余未知路径一律 JSON 404。
SPA_PREFIXES = {"overview", "run", "history", "runs", "atoms", "packs"}
MISSING_STATIC_MESSAGE = (
    "前端资源未就绪：未找到 panguverse/web/static/index.html。"
    "请等待前端资源部署后再访问（API 已可用）。"
)
MAX_BODY_BYTES = 8 * 1024 * 1024

#: 只有这些对端才被视为受信代理（nginx 在本机回环上反代）
TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_trusted_proxy(peer: str) -> bool:
    address = _parse_ip(peer)
    if address is None:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address in network for network in TRUSTED_PROXY_NETWORKS)


def resolve_client_ip(peer: str, headers: dict[str, str]) -> str:
    """限流用的真实客户端 IP（契约 §3 #7）。

    - 对端不是受信代理 → 用 TCP 对端 IP，**忽略** ``X-Forwarded-For``（防伪造）；
    - 对端是回环/受信代理 → 取 ``X-Forwarded-For`` 的**最右一跳**（nginx 追加的那一跳），
      非法/缺失时回退对端 IP。
    """
    peer = (peer or "").strip() or "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    forwarded = ""
    for key, value in headers.items():
        if key.lower() == "x-forwarded-for":
            forwarded = value or ""
            break
    if not forwarded:
        return peer
    rightmost = forwarded.split(",")[-1].strip()
    if _parse_ip(rightmost) is None:
        return peer
    return rightmost


class PanguverseRequestHandler(BaseHTTPRequestHandler):
    server_version = "PanguVerseWeb/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if _is_api_path(path):
            self._emit(self._api("GET", parsed))
            return
        if path == "/":
            self._serve_index()
            return
        if path.startswith("/static/"):
            self._serve_static(unquote(path[len("/static/"):]))
            return
        if _is_spa_route(path):
            self._serve_index()
            return
        self._emit(error_response("not_found", "路径不存在"))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        self._handle_write("POST")

    def do_PATCH(self) -> None:
        # 任务定义的「部分更新」走 PATCH（契约 §3.2）
        self._handle_write("PATCH")

    def do_DELETE(self) -> None:
        # 任务定义的删除走 DELETE（204，无响应体）
        self._handle_write("DELETE")

    def _handle_write(self, method: str) -> None:
        parsed = urlsplit(self.path)
        if not _is_api_path(parsed.path):
            self._emit(error_response("not_found", "路径不存在"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._emit(error_response("bad_request", "请求体过大"), close=True)
            return
        raw = self.rfile.read(length) if length else b""
        self._emit(self._api(method, parsed, raw))

    def _api(self, method: str, parsed: Any, raw: bytes = b"") -> ApiResponse:
        """把 Cookie / X-Forwarded-Proto / 真实客户端 IP 交给 API 层（认证需要）。"""
        headers = dict(self.headers.items())
        peer = self.client_address[0] if self.client_address else ""
        client_ip = resolve_client_ip(peer, headers)
        return self.server.api.handle(
            method, parsed.path, parsed.query, raw, headers=headers, client_ip=client_ip
        )

    # ------------------------------------------------------------------ #
    def _serve_static(self, name: str) -> None:
        if name not in STATIC_MIME or "/" in name or "\\" in name or name.startswith("."):
            self._emit(error_response("not_found", "静态资源不存在"))
            return
        path = (STATIC_DIR / name).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._emit(error_response("not_found", "静态资源不存在"))
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._emit(error_response("not_found", "静态资源不存在"))
            return
        self._send(200, data, STATIC_MIME[name], {"Cache-Control": "no-cache"})

    def _serve_index(self) -> None:
        index = STATIC_DIR / "index.html"
        try:
            data = index.read_bytes()
        except OSError:
            self._send(
                503,
                MISSING_STATIC_MESSAGE.encode("utf-8"),
                "text/plain; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        self._send(200, data, STATIC_MIME["index.html"], {"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ #
    def _emit(self, response: ApiResponse, *, close: bool = False) -> None:
        headers = dict(response.headers)
        if close:
            self.close_connection = True
        self._send(response.status, response.body, response.content_type, headers)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        # 204/304/1xx 按 HTTP 语义没有实体：不写 Content-Type，也不写 Content-Length 与 body。
        # 只回「空体 + JSON Content-Type」会让前端 res.json() 对空体 reject（F1）。
        bodyless = status in (204, 304) or 100 <= status < 200
        if not bodyless:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not bodyless and self.command != "HEAD" and body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)


class PanguverseHTTPServer(ThreadingHTTPServer):
    """带 API 实例的线程化 HTTP 服务。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler] = PanguverseRequestHandler,
        *,
        api: WebAPI,
        quiet: bool = False,
    ) -> None:
        self.api = api
        self.quiet = quiet
        super().__init__(server_address, handler_cls)


def create_server(
    host: str,
    port: int,
    workspace: str | Path,
    *,
    quiet: bool = False,
    auth: AuthManager | None = None,
) -> PanguverseHTTPServer:
    api = WebAPI(workspace, auth=auth)
    return PanguverseHTTPServer((host, port), api=api, quiet=quiet)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 7788,
    workspace: str | Path = ".",
    open_browser: bool = False,
    quiet: bool = False,
    no_auth: bool = False,
    auth_user: str | None = None,
) -> int:
    root = Path(workspace).expanduser().resolve()
    auth: AuthManager | None = None
    if not no_auth:
        # 默认开启：凭据 env > .panguverse/web/auth.json > 首启生成（密码只打印一次）
        auth = AuthManager(root, username=auth_user, logger=lambda line: print(line, flush=True))
    try:
        httpd = create_server(host, port, root, quiet=quiet, auth=auth)
    except OSError as exc:
        print(f"❌ 无法监听 {host}:{port} -> {exc}（端口被占用或地址不可用）")
        return 2

    bound_port = int(httpd.server_address[1])
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    url = f"http://{display_host}:{bound_port}/"

    def announce(line: str) -> None:
        # 启动日志必须即时可见：重定向到文件/systemd 时 stdout 是块缓冲，
        # 首启密码不 flush 就等于没打印（契约要求「仅显示这一次」）
        print(line, flush=True)

    announce(f"🪓 PanguVerse Web 已启动: {url}")
    announce(f"   工作区: {root}")
    if auth is None:
        announce("⚠️  已关闭登录认证（--no-auth）：任何人只要能访问端口即可操作，仅限本机调试")
    else:
        if auth.generated_password:
            announce(
                f"[auth] 初始账号 {auth.user}，初始密码 {auth.generated_password}"
                "（首次启动生成，仅显示这一次）"
            )
        announce(f"[auth] 已启用登录认证，账号 {auth.user}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        if auth is None:
            announce("🚨 危险：正在监听非本机地址且未开启认证，切勿暴露公网！")
        else:
            announce("⚠️  已监听非本机地址：登录认证已开启，仍建议放在 HTTPS 反向代理之后")
    if not (STATIC_DIR / "index.html").is_file():
        announce("⚠️  前端资源未就绪（panguverse/web/static/index.html 缺失）：GET / 将返回 503")

    if open_browser and host in {"127.0.0.1", "localhost", "::1"}:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    httpd.api.start_scheduler()
    announce("⏰ 调度线程已启动（单进程；错过的任务不补跑）")
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.api.stop_scheduler()
        httpd.server_close()
    return 0


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _is_spa_route(path: str) -> bool:
    head = path.strip("/").split("/", 1)[0]
    return bool(head) and head in SPA_PREFIXES


__all__ = [
    "MISSING_STATIC_MESSAGE",
    "STATIC_DIR",
    "PanguverseHTTPServer",
    "PanguverseRequestHandler",
    "create_server",
    "serve",
]
