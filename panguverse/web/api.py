"""HTTP API 契约层：路由 + 参数校验 + 统一错误体。

契约见 ``docs/web-api.md`` / ``tasks/web-app-mvp-spec.md`` §3：
所有响应 ``application/json; charset=utf-8``，错误体固定为
``{"error": {"code": "...", "message": "..."}}``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from . import service
from .jobs import JobManager
from .service import ApiError

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
MAX_BODY_BYTES = 8 * 1024 * 1024


@dataclass
class Response:
    """一个已经渲染好的 HTTP 响应（body 为 bytes，避免二次编码）。"""

    status: int
    body: bytes
    content_type: str = JSON_CONTENT_TYPE
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200, *, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
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

    def __init__(self, workspace: str | Path, *, jobs: JobManager | None = None) -> None:
        self.root = service.resolve_workspace(workspace)
        self.jobs = jobs or JobManager()

    # ------------------------------------------------------------------ #
    def handle(
        self, method: str, path: str, query: str = "", raw_body: bytes = b""
    ) -> Response:
        try:
            params = parse_qs(query, keep_blank_values=True)
            body = self._parse_body(raw_body) if method in {"POST", "PUT"} else None
            return self._route(method.upper(), path, params, body)
        except ApiError as exc:
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

    def _route(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> Response:
        segments = [unquote(part) for part in path.strip("/").split("/") if part]
        if len(segments) < 2 or segments[0] != "api":
            raise ApiError("not_found", "接口不存在")
        tail = segments[1:]
        head = tail[0]
        rest = tail[1:]

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
                    if filename == "report.html":
                        path_obj = service.run_artifact(self.root, run_id, "report.html")
                        return Response(
                            status=200,
                            body=_read_bytes(path_obj),
                            content_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"},
                        )
                    if filename == "junit.xml":
                        path_obj = service.run_artifact(self.root, run_id, "junit.xml")
                        return Response(
                            status=200,
                            body=_read_bytes(path_obj),
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

        raise ApiError("not_found", "接口不存在")


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


__all__ = [
    "JSON_CONTENT_TYPE",
    "MAX_BODY_BYTES",
    "Response",
    "WebAPI",
    "error_response",
    "json_response",
]
