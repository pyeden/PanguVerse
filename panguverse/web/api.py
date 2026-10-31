"""HTTP API 契约层：路由 + 参数校验 + 统一错误体。

契约见 ``docs/web-api.md`` / ``tasks/web-app-mvp-spec.md`` §3：
所有响应 ``application/json; charset=utf-8``，错误体固定为
``{"error": {"code": "...", "message": "..."}}``。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from ..report import html_report, junit
from . import service
from .auth import AuthManager, build_cookie
from .jobs import JobManager
from .service import ApiError
from .tasks_store import TaskStoreError

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
MAX_BODY_BYTES = 8 * 1024 * 1024

#: env 里的敏感键名（保留键名，只把值打码，方便排障）
_SENSITIVE_ENV_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential)")
MASK = "***"


def _mask_env_value(value: Any) -> Any:
    """把 env 子树里的敏感键值替换成 ``***``（递归处理嵌套 dict/list）。"""
    if isinstance(value, dict):
        return {
            key: (
                MASK
                if isinstance(key, str) and _SENSITIVE_ENV_KEY_RE.search(key)
                else _mask_env_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_env_value(item) for item in value]
    return value


def mask_env_payload(data: Any) -> Any:
    """HTTP 层打码：任何 ``env`` 子树中的敏感值一律替换，磁盘文件保持原样。

    递归处理整个响应体；只对键名命中 ``env`` 的子树做敏感键替换，
    不会影响 ``/api/atoms`` 这类契约字段。
    """
    if isinstance(data, dict):
        return {
            key: (_mask_env_value(value) if key == "env" else mask_env_payload(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [mask_env_payload(item) for item in data]
    return data


@dataclass
class Response:
    """一个已经渲染好的 HTTP 响应（body 为 bytes，避免二次编码）。"""

    status: int
    body: bytes
    content_type: str = JSON_CONTENT_TYPE
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200, *, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(mask_env_payload(data), ensure_ascii=False, default=str).encode("utf-8")
    merged = {"Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return Response(status=status, body=body, headers=merged)


def error_response(code: str, message: str, status: int | None = None) -> Response:
    http_status = status or service.STATUS_BY_CODE.get(code, 500)
    return json_response(
        {"error": {"code": code, "message": service.safe_message(message)}},
        status=http_status,
    )


class WebAPI:
    """把请求映射到服务层；不碰 socket，便于单测直接调用。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        jobs: JobManager | None = None,
        auth: AuthManager | None = None,
    ) -> None:
        self.root = service.resolve_workspace(workspace)
        self.jobs = jobs or JobManager()
        #: ``None`` = 关闭认证（--no-auth / 单元测试直接嵌入）；``serve`` 默认构造 AuthManager
        self.auth = auth

    # ------------------------------------------------------------------ #
    def handle(
        self,
        method: str,
        path: str,
        query: str = "",
        raw_body: bytes = b"",
        *,
        headers: dict[str, str] | None = None,
        client_ip: str = "",
    ) -> Response:
        try:
            params = parse_qs(query, keep_blank_values=True)
            body = self._parse_body(raw_body) if method in {"POST", "PUT", "PATCH"} else None
            request_headers = headers or {}
            # 认证必须在路由分派之前统一拦截（契约 §3.1），只有 /api/auth/* 白名单放行
            self._check_auth(path, request_headers)
            return self._route(
                method.upper(), path, params, body, headers=request_headers, client_ip=client_ip
            )
        except (ApiError, TaskStoreError) as exc:
            return error_response(exc.code, exc.message, exc.status)
        except Exception as exc:  # noqa: BLE001 - 兜底：异常不得穿透到 handler 线程
            return error_response("internal_error", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_body(raw: bytes) -> Any:
        if not raw:
            return None
        if len(raw) > MAX_BODY_BYTES:
            raise ApiError("bad_request", "请求体过大")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError("bad_request", f"请求体不是合法 JSON: {exc}") from exc

    def _check_auth(self, path: str, headers: dict[str, str]) -> None:
        if self.auth is None:
            return
        segments = [part for part in path.strip("/").split("/") if part]
        if not segments or segments[0] != "api":
            return
        if len(segments) >= 2 and segments[1] == "auth":
            return
        if self.auth.user_from_headers(headers) is None:
            raise ApiError("unauthorized", "需要登录", status=401)

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: Any,
        *,
        headers: dict[str, str] | None = None,
        client_ip: str = "",
    ) -> Response:
        segments = [unquote(part) for part in path.strip("/").split("/") if part]
        if len(segments) < 2 or segments[0] != "api":
            raise ApiError("not_found", "接口不存在")
        tail = segments[1:]
        head = tail[0]
        rest = tail[1:]
        request_headers = headers or {}

        if head == "auth":
            return self._handle_auth(method, rest, body, request_headers, client_ip)

        if method == "GET":
            if head == "status" and not rest:
                return json_response(service.status_payload(self.root))
            if head == "atoms" and not rest:
                return json_response(
                    service.list_atoms(
                        domain=_first(query, "domain"),
                        tag=_first(query, "tag"),
                        category=_first(query, "category"),
                    )
                )
            if head == "packs":
                if not rest:
                    return json_response(service.list_packs(domain=_first(query, "domain")))
                if len(rest) == 1:
                    return json_response(service.get_pack(rest[0]))
            if head == "scenarios" and not rest:
                return json_response(service.list_scenarios(self.root))
            if head == "tasks":
                if not rest:
                    return json_response({"tasks": service.list_tasks(self.root)})
                if len(rest) == 1:
                    return json_response(service.get_task(self.root, rest[0]))
            if head == "jobs":
                if not rest:
                    return json_response([job.to_dict() for job in self.jobs.recent(20)])
                if len(rest) == 1:
                    job = self.jobs.get(rest[0])
                    if job is None:
                        raise ApiError("not_found", "job 不存在")
                    return json_response(job.to_dict())
            if head == "runs":
                if not rest:
                    limit = _int_query(query, "limit", 50, maximum=500)
                    return json_response(service.list_runs(self.root, limit=limit))
                if len(rest) == 1:
                    return json_response(service.get_run(self.root, rest[0]))
                if len(rest) == 2:
                    run_id, filename = rest
                    if filename == "config":
                        return json_response(service.run_config(self.root, run_id))
                    if filename in ("report.html", "junit.xml"):
                        # 磁盘文件保持原样（回归回放依赖）；出口按同一套 mask_env_payload 重新渲染，
                        # 避免 HTML/XML 旁路泄漏 env 明文（A16，v1.4）
                        path_obj = service.run_artifact(self.root, run_id, filename)
                        payload = service.get_run(self.root, run_id)
                        report = mask_env_payload(payload["report"])
                        plan_source = str((payload.get("plan") or {}).get("source") or "unknown")
                        if filename == "report.html":
                            title = _html_title(_read_text(path_obj))
                            return Response(
                                status=200,
                                body=html_report.render(report, title=title).encode("utf-8"),
                                content_type="text/html; charset=utf-8",
                                headers={"Cache-Control": "no-store"},
                            )
                        return Response(
                            status=200,
                            body=junit.render(report, suite_name=f"PanguVerse.{plan_source}").encode(
                                "utf-8"
                            ),
                            content_type="application/xml",
                            headers={
                                "Cache-Control": "no-store",
                                "Content-Disposition": f'attachment; filename="panguverse-{run_id}.xml"',
                            },
                        )
        elif method == "POST":
            if head == "runs" and not rest:
                job = service.submit_run(self.jobs, self.root, body)
                return json_response({"job_id": job.job_id, "status": "queued"}, status=202)
            if head == "runs" and len(rest) == 2 and rest[1] == "rerun":
                job = service.rerun(self.jobs, self.root, rest[0], body)
                return json_response({"job_id": job.job_id, "status": "queued"}, status=202)
            if head == "runs" and len(rest) == 2 and rest[1] == "save_as_task":
                task = service.save_run_as_task(self.root, rest[0], body)
                return json_response(task, status=201)
            if head == "tasks":
                if not rest:
                    task = service.create_task(self.root, body)
                    return json_response(task, status=201)
                if len(rest) == 2 and rest[1] == "run":
                    job = service.run_task(self.jobs, self.root, rest[0], body)
                    return json_response({"job_id": job.job_id, "status": "queued"}, status=202)
        elif method == "PATCH":
            if head == "tasks" and len(rest) == 1:
                return json_response(service.update_task(self.root, rest[0], body))
        elif method == "DELETE":
            if head == "tasks" and len(rest) == 1:
                service.delete_task(self.root, rest[0])
                # 204 无实体：content_type 留空，server 层也不会写 Content-Type/Content-Length
                return Response(
                    status=204,
                    body=b"",
                    content_type="",
                    headers={"Cache-Control": "no-store"},
                )

        raise ApiError("not_found", "接口不存在")

    # ------------------------------------------------------------------ #
    def _handle_auth(
        self,
        method: str,
        rest: list[str],
        body: Any,
        headers: dict[str, str],
        client_ip: str,
    ) -> Response:
        if len(rest) != 1:
            raise ApiError("not_found", "接口不存在")
        action = rest[0]
        if action == "status" and method == "GET":
            if self.auth is None:
                return json_response(
                    {"authenticated": True, "user": "", "auth_disabled": True}
                )
            user = self.auth.user_from_headers(headers)
            if user:
                return json_response({"authenticated": True, "user": user})
            return json_response({"authenticated": False})
        if action == "login" and method == "POST":
            return self._login(body, headers, client_ip)
        if action == "logout" and method == "POST":
            if self.auth is not None:
                self.auth.revoke_headers(headers)
            return json_response(
                {"ok": True},
                headers={
                    "Set-Cookie": build_cookie(
                        "", secure=_forwarded_https(headers), max_age=0
                    )
                },
            )
        raise ApiError("not_found", "接口不存在")

    def _login(
        self, body: Any, headers: dict[str, str], client_ip: str
    ) -> Response:
        if self.auth is None:
            return json_response({"ok": True, "user": "", "auth_disabled": True})
        retry_after = self.auth.rate_limit_retry(client_ip)
        if retry_after:
            return json_response(
                {"error": {"code": "too_many_attempts", "message": "尝试过多，请 5 分钟后再试"}},
                status=429,
                headers={"Retry-After": str(retry_after)},
            )
        if not isinstance(body, dict):
            raise ApiError("bad_request", "请求体必须是 JSON 对象")
        username = body.get("username")
        password = body.get("password")
        if not self.auth.verify_credentials(username, password):
            self.auth.record_failure(client_ip)
            self.auth.log_login_failure(
                username if isinstance(username, str) else "", client_ip
            )
            raise ApiError("bad_credentials", "账号或密码不正确", status=401)
        self.auth.record_success(client_ip)
        token = self.auth.issue_session()
        cookie = build_cookie(token, secure=_forwarded_https(headers))
        return json_response({"ok": True, "user": self.auth.user}, headers={"Set-Cookie": cookie})


# --------------------------------------------------------------------------- #
def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = (values[0] or "").strip() if values else ""
    return value or None


def _int_query(
    query: dict[str, list[str]], key: str, default: int, *, maximum: int
) -> int:
    raw = _first(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError("bad_request", f"{key} 必须是整数") from exc
    return max(1, min(maximum, value))


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ApiError("internal_error", f"读取文件失败: {exc}") from exc


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiError("internal_error", f"读取文件失败: {exc}") from exc


def _forwarded_https(headers: dict[str, str]) -> bool:
    for key, value in headers.items():
        if key.lower() == "x-forwarded-proto":
            return (value or "").split(",")[0].strip().lower() == "https"
    return False


def _html_title(disk_html: str) -> str:
    """沿用磁盘 HTML 的标题，保证打码副本与原文只差敏感值。"""
    match = re.search(r"<title>(.*?)</title>", disk_html, re.S)
    if not match:
        return "PanguVerse API 自动化测试报告"
    return html_lib.unescape(match.group(1).strip())


__all__ = [
    "JSON_CONTENT_TYPE",
    "MAX_BODY_BYTES",
    "Response",
    "WebAPI",
    "error_response",
    "json_response",
]
