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
from urllib.parse import parse_qs, quote, unquote

from ..report import html_report, junit
from . import artifacts, executions, explore, reports, schedules, service, studio, triggers
from .artifacts import ArtifactError
from .auth import AuthManager, build_cookie
from .batches import BatchManager
from .jobs import JobManager
from .projects import ProjectStoreError
from .schedules import ScheduleError
from .service import ApiError
from .studio import StudioError
from .tasks_store import TaskStoreError

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
MAX_BODY_BYTES = 8 * 1024 * 1024

#: env 里的敏感键名（保留键名，只把值打码，方便排障）
_SENSITIVE_ENV_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential)")
MASK = "***"


def is_sensitive_env_key(key: Any) -> bool:
    """env 键名是否敏感（供 AI 总结打码复用同一套规则）。"""
    return isinstance(key, str) and bool(_SENSITIVE_ENV_KEY_RE.search(key))


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


def error_response(
    code: str,
    message: str,
    status: int | None = None,
    *,
    details: dict[str, Any] | None = None,
) -> Response:
    http_status = status or service.STATUS_BY_CODE.get(code, 500)
    error: dict[str, Any] = {"code": code, "message": service.safe_message(message)}
    if details:
        # 仅工作室冲突等需要结构化上下文时追加（A17–A62 的错误体仍是 code+message）
        error.update(details)
    return json_response({"error": error}, status=http_status)


class WebAPI:
    """把请求映射到服务层；不碰 socket，便于单测直接调用。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        jobs: JobManager | None = None,
        auth: AuthManager | None = None,
        insight_llm: Any = None,
        batches: BatchManager | None = None,
        explore_llm: Any = None,
        explore_registry: Any = None,
        crystallize_llm: Any = None,
    ) -> None:
        self.root = service.resolve_workspace(workspace)
        self.jobs = jobs or JobManager()
        #: 批量运行表（内存态）；聚合数字优先读磁盘报告，读不到再退回 job.summary
        if batches is not None:
            self.batches = batches
        else:
            # 批次默认支持取消（P0）：未开始套件标 cancelled，已完成的 run 保留
            self.batches = executions.CancellableBatchManager(
                self.jobs,
                load_summary=lambda run_id: service.run_summary(self.root, run_id),
            )
        #: ``None`` = 关闭认证（--no-auth / 单元测试直接嵌入）；``serve`` 默认构造 AuthManager
        self.auth = auth
        #: 注入用的 LLM client（A44 假 client）；``None`` = 按环境变量自动判定
        self.insight_llm = insight_llm
        #: explore-first：注入假 LLM/原子库用于测试；None = 按环境/默认原子库
        self.explore_llm = explore_llm
        self.explore_registry = explore_registry
        #: 固化语义归纳专用 LLM；None = 探索 LLM（测试注入）或环境变量客户端
        self.crystallize_llm = crystallize_llm
        #: 调度线程（单进程）：只在 ``serve`` 里 start/stop；测试可手动调用
        self.scheduler = schedules.Scheduler(self.root, self.jobs, self.batches)

    # ------------------------------------------------------------------ #
    def start_scheduler(self) -> None:
        self.scheduler.start()

    def stop_scheduler(self) -> None:
        self.scheduler.stop()

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
        except StudioError as exc:
            return error_response(
                exc.code, exc.message, exc.status, details=exc.details or None
            )
        except (ApiError, ProjectStoreError, TaskStoreError, ScheduleError, ArtifactError) as exc:
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
        if len(segments) >= 2 and segments[1] in ("auth", "share", "hooks"):
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

        if head == "studio":
            return self._handle_studio(method, rest, query, body, request_headers)

        if method == "POST" and head == "atoms" and len(rest) == 1 and rest[0] == "register":
            from .custom_atoms import register_custom_atom
            return json_response(
                register_custom_atom(self.root, body, actor=self._actor(request_headers)),
                status=201,
            )

        if method == "GET":
            if head == "status" and not rest:
                payload = service.status_payload(self.root)
                # 调度线程自检：last_tick_at / last_tick_error 由 Scheduler 维护
                payload.update(self.scheduler.status_snapshot())
                return json_response(payload)
            if head == "atoms" and not rest:
                return json_response(
                    service.list_atoms(
                        domain=_first(query, "domain"),
                        tag=_first(query, "tag"),
                        category=_first(query, "category"),
                        workspace=self.root,
                    )
                )
            if head == "packs":
                if not rest:
                    return json_response(
                        service.list_packs(
                            domain=_first(query, "domain"), workspace=self.root
                        )
                    )
                if len(rest) == 1:
                    return json_response(service.get_pack(rest[0], workspace=self.root))
            if head == "scenarios" and not rest:
                return json_response(service.list_scenarios(self.root))
            if head == "tasks":
                if not rest:
                    return json_response(
                        {
                            "tasks": service.list_tasks(
                                self.root,
                                include_archived=_flag_query(query, "include_archived"),
                            )
                        }
                    )
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
            if head == "executions":
                if not rest:
                    return json_response(
                        executions.list_executions(
                            self.root,
                            self.jobs,
                            self.batches,
                            project=_first(query, "project"),
                            status=_first(query, "status"),
                            offset=_offset_query(query, "offset", 0),
                            limit=_int_query(query, "limit", 50, maximum=500),
                        )
                    )
                if len(rest) == 1:
                    return json_response(
                        executions.get_execution(self.root, self.jobs, self.batches, rest[0])
                    )
            if head == "reports" and not rest:
                return json_response(
                    reports.list_reports(
                        self.root,
                        project=_first(query, "project"),
                        status=_first(query, "status"),
                        offset=_offset_query(query, "offset", 0),
                        limit=_int_query(query, "limit", 50, maximum=500),
                    )
                )
            if head == "runs":
                if rest == ["query"]:
                    # 必须在 run_id 分支之前：'query' 不是合法 run_id，但语义上是子资源
                    return json_response(
                        service.query_runs(
                            self.root,
                            project=_first(query, "project"),
                            suite=_first(query, "suite"),
                            status=_first(query, "status") or "all",
                            offset=_offset_query(query, "offset", 0),
                            limit=_int_query(query, "limit", 20, maximum=200),
                        )
                    )
                if not rest:
                    limit = _int_query(query, "limit", 50, maximum=500)
                    return json_response(service.list_runs(self.root, limit=limit))
                if len(rest) == 1:
                    return json_response(service.get_run(self.root, rest[0]))
                if len(rest) == 2:
                    run_id, filename = rest
                    if filename == "insight":
                        return json_response(service.get_insight(self.root, run_id))
                    if filename == "diff":
                        from . import rundiff

                        return json_response(
                            rundiff.diff_runs(
                                self.root,
                                run_id,
                                base_id=_first(query, "base") or None,
                            )
                        )
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
            if head == "projects":
                if not rest:
                    return json_response(
                        {
                            "projects": service.list_project_summaries(
                                self.root,
                                include_archived=_flag_query(query, "include_archived"),
                            )
                        }
                    )
                if len(rest) == 1:
                    return json_response(service.get_project_detail(self.root, rest[0]))
                if len(rest) == 2 and rest[1] == "summary":
                    return json_response(
                        service.project_summary(
                            self.root,
                            rest[0],
                            limit=_int_query(query, "limit", 5, maximum=50),
                        )
                    )
            if head == "batches" and len(rest) == 1:
                batch = self.batches.get(rest[0])
                if batch is None:
                    raise ApiError("not_found", "batch 不存在")
                return json_response(batch.snapshot())
            if head == "schedules":
                if not rest:
                    return json_response(
                        {
                            "schedules": schedules.list_schedules(
                                self.root,
                                include_disabled=_flag_query(query, "include_disabled"),
                            )
                        }
                    )
                if len(rest) == 1:
                    return json_response(
                        schedules.public_schedule(schedules.read_schedule(self.root, rest[0]))
                    )
                if len(rest) == 2 and rest[1] == "events":
                    return json_response(
                        {
                            "events": schedules.read_events(
                                self.root,
                                rest[0],
                                limit=_int_query(query, "limit", 100, maximum=500),
                            )
                        }
                    )
            if head == "artifacts":
                if not rest:
                    return json_response(
                        artifacts.list_artifacts(
                            self.root,
                            project=_first(query, "project"),
                            run=_first(query, "run"),
                            type=_first(query, "type"),
                            category=_first(query, "category"),
                            offset=_offset_query(query, "offset", 0),
                            limit=_int_query(query, "limit", 50, maximum=200),
                        )
                    )
                if len(rest) == 1:
                    return json_response(
                        artifacts.get_artifact(
                            self.root, rest[0], project=_first(query, "project")
                        )
                    )
                if len(rest) == 2 and rest[1] == "download":
                    data, filename, mime = artifacts.download_artifact(
                        self.root, rest[0], project=_first(query, "project")
                    )
                    return Response(
                        status=200,
                        body=data,
                        content_type=mime,
                        headers={
                            "Cache-Control": "no-store",
                            "Content-Disposition": _attachment_header(filename),
                        },
                    )
            if head == "share" and rest:
                if len(rest) == 1:
                    body, content_type = artifacts.share_preview(self.root, rest[0])
                    return Response(
                        status=200,
                        body=body,
                        content_type=content_type,
                        headers={
                            "Cache-Control": "no-store",
                            "X-Robots-Tag": "noindex",
                            "Content-Security-Policy": (
                                "default-src 'none'; img-src 'self' data:; "
                                "style-src 'unsafe-inline'"
                            ),
                        },
                    )
                if len(rest) == 2 and rest[1] == "raw":
                    data, filename, mime = artifacts.share_raw(self.root, rest[0])
                    return Response(
                        status=200,
                        body=data,
                        content_type=mime,
                        headers={
                            "Cache-Control": "no-store",
                            "X-Robots-Tag": "noindex",
                            "Content-Disposition": _attachment_header(filename, inline=True),
                        },
                    )
        elif method == "POST":
            if head == "executions" and len(rest) == 2 and rest[1] == "cancel":
                return json_response(
                    executions.cancel_execution(
                        self.root,
                        self.jobs,
                        self.batches,
                        rest[0],
                        actor=self._actor(request_headers),
                    )
                )
            if head == "reports" and len(rest) == 2:
                if rest[1] == "sign":
                    return json_response(
                        reports.sign_report(
                            self.root, rest[0], body, actor=self._actor(request_headers)
                        )
                    )
                if rest[1] == "void":
                    return json_response(
                        reports.void_report(
                            self.root, rest[0], body, actor=self._actor(request_headers)
                        )
                    )
            if head == "tasks" and len(rest) == 2 and rest[1] in ("archive", "unarchive"):
                return json_response(
                    service.archive_task(
                        self.root,
                        rest[0],
                        archived=rest[1] == "archive",
                        actor=self._actor(request_headers),
                    )
                )
            if head == "projects" and len(rest) == 2 and rest[1] in ("archive", "unarchive"):
                return json_response(
                    service.archive_project(
                        self.root,
                        rest[0],
                        archived=rest[1] == "archive",
                        actor=self._actor(request_headers),
                    )
                )
            if head == "projects":
                if not rest:
                    return json_response(service.create_project(self.root, body), status=201)
                if len(rest) == 2 and rest[1] == "suites":
                    return json_response(
                        service.add_suite(self.root, rest[0], body), status=201
                    )
                if len(rest) == 2 and rest[1] == "run":
                    payload = service.start_project_batch(
                        self.jobs, self.batches, self.root, rest[0], body
                    )
                    return json_response(payload, status=202)
            if head == "runs" and not rest:
                job = service.submit_run(self.jobs, self.root, body)
                return json_response({"job_id": job.job_id, "status": "queued"}, status=202)
            if head == "runs" and len(rest) == 2 and rest[1] == "rerun":
                job = service.rerun(self.jobs, self.root, rest[0], body)
                return json_response({"job_id": job.job_id, "status": "queued"}, status=202)
            if head == "runs" and len(rest) == 2 and rest[1] == "save_as_task":
                task = service.save_run_as_task(self.root, rest[0], body)
                return json_response(task, status=201)
            if head == "runs" and len(rest) == 2 and rest[1] == "insight":
                payload = service.generate_insight(
                    self.root, rest[0], body, llm_client=self.insight_llm
                )
                return json_response(payload)
            if head == "tasks":
                if not rest:
                    task = service.create_task(self.root, body)
                    return json_response(task, status=201)
                if len(rest) == 2 and rest[1] == "run":
                    job = service.run_task(self.jobs, self.root, rest[0], body)
                    return json_response({"job_id": job.job_id, "status": "queued"}, status=202)
            if head == "schedules":
                if not rest:
                    return json_response(
                        schedules.create_schedule(self.root, body), status=201
                    )
                if len(rest) == 2 and rest[1] == "run-now":
                    result = schedules.trigger_schedule(
                        self.root,
                        rest[0],
                        self.jobs,
                        self.batches,
                        trigger="manual",
                        env=_schedule_env(body),
                    )
                    return json_response(result, status=202)
            if head == "triggers" and len(rest) == 2 and rest[1] == "run":
                result = triggers.run_trigger(
                    self.root,
                    rest[0],
                    self.jobs,
                    trigger="manual",
                    env=_schedule_env(body),
                )
                return json_response(result, status=202)
            if head == "hooks" and len(rest) == 2 and rest[0] == "workflow":
                trigger = triggers.find_by_token(self.root, rest[1])
                if not trigger.get("enabled"):
                    raise ScheduleError("conflict", "触发器已禁用", status=409)
                payload = body.get("payload") if isinstance(body, dict) else None
                result = triggers.run_trigger(
                    self.root,
                    trigger["trigger_id"],
                    self.jobs,
                    trigger="webhook",
                    env=_schedule_env(body),
                    payload=payload,
                )
                return json_response(result, status=202)
            if head == "hooks" and len(rest) == 1:
                schedule = schedules.find_by_webhook_token(self.root, rest[0])
                if not schedule.get("enabled"):
                    raise ScheduleError("conflict", "调度已禁用", status=409)
                result = schedules.trigger_schedule(
                    self.root,
                    schedule["sid"],
                    self.jobs,
                    self.batches,
                    trigger="webhook",
                    env=_schedule_env(body),
                )
                return json_response(result, status=202)
            if head == "artifacts" and len(rest) == 2 and rest[1] == "share":
                return json_response(
                    artifacts.create_share(self.root, rest[0], body), status=201
                )
        elif method == "PATCH":
            if head == "projects":
                if len(rest) == 1:
                    return json_response(service.update_project(self.root, rest[0], body))
                if len(rest) == 3 and rest[1] == "suites":
                    return json_response(
                        service.update_suite(self.root, rest[0], rest[2], body)
                    )
            if head == "tasks" and len(rest) == 1:
                return json_response(service.update_task(self.root, rest[0], body))
            if head == "schedules" and len(rest) == 1:
                return json_response(schedules.update_schedule(self.root, rest[0], body))
            if head == "triggers" and len(rest) == 1:
                return json_response(triggers.update_trigger(self.root, rest[0], body))
        elif method == "DELETE":
            if head == "projects":
                if len(rest) == 1:
                    service.delete_project(self.root, rest[0])
                    return _no_content()
                if len(rest) == 3 and rest[1] == "suites":
                    service.delete_suite(self.root, rest[0], rest[2])
                    return _no_content()
            if head == "tasks" and len(rest) == 1:
                service.delete_task(self.root, rest[0])
                # 204 无实体：content_type 留空，server 层也不会写 Content-Type/Content-Length
                return Response(
                    status=204,
                    body=b"",
                    content_type="",
                    headers={"Cache-Control": "no-store"},
                )
            if head == "schedules" and len(rest) == 1:
                schedules.delete_schedule(self.root, rest[0])
                return _no_content()
            if head == "triggers" and len(rest) == 1:
                triggers.delete_trigger(self.root, rest[0])
                return _no_content()
            if head == "artifacts" and len(rest) == 2 and rest[1] == "share":
                artifacts.revoke_all_shares(self.root, rest[0])
                return _no_content()
            if head == "artifacts" and len(rest) == 3 and rest[1] == "share":
                artifacts.revoke_share(self.root, rest[0], rest[2])
                return _no_content()

        raise ApiError("not_found", "接口不存在")

    # ------------------------------------------------------------------ #
    def _actor(self, headers: dict[str, str]) -> str:
        """审计里的「谁」：认证开启时用登录名，本地/测试模式记 local。"""
        if self.auth is None:
            return "local"
        return self.auth.user_from_headers(headers) or "local"

    def _handle_studio(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: Any,
        headers: dict[str, str],
    ) -> Response:
        """工作台 MVP（契约 v5.0 §2.2）：草案 → 图 → 调试 → 审阅 → 发布。"""
        if not rest:
            raise ApiError("not_found", "接口不存在")
        actor = self._actor(headers)

        # explore-first：POST /api/studio/explore + 会话/产物/固化路由
        if rest[0] == "explore":
            if len(rest) == 1:
                if method == "POST":
                    started = explore.start_explore(
                        self.root,
                        body,
                        actor=actor,
                        llm=self.explore_llm,
                        registry=self.explore_registry,
                    )
                    return json_response(started, status=202)
                if method == "GET":
                    return json_response(
                        explore.list_sessions(
                            self.root,
                            limit=_int_query(query, "limit", 50, maximum=200),
                        )
                    )
            if len(rest) == 2 and method == "GET":
                return json_response(
                    explore.get_session(
                        self.root, rest[1], cursor=_cursor_query(query, "cursor", 0)
                    )
                )
            if len(rest) == 3 and rest[2] == "artifact" and method == "GET":
                data, content_type, filename = explore.read_artifact(
                    self.root, rest[1], _first(query, "path") or ""
                )
                return Response(
                    status=200,
                    body=data,
                    content_type=content_type,
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": f'inline; filename="{filename}"',
                    },
                )
            if len(rest) == 3 and rest[2] == "crystallize" and method == "POST":
                return json_response(
                    explore.crystallize(
                        self.root,
                        rest[1],
                        body,
                        actor=actor,
                        registry=self.explore_registry,
                        llm=self.crystallize_llm or self.explore_llm,
                    )
                )
            raise ApiError("not_found", "接口不存在")

        # GET /api/studio/debug/{session_id}?cursor= —— 增量读取异步调试会话
        if rest[0] == "debug":
            if len(rest) == 2 and method == "GET":
                return json_response(
                    studio.get_debug_session(
                        self.root,
                        rest[1],
                        cursor=_cursor_query(query, "cursor", 0),
                    )
                )
            raise ApiError("not_found", "接口不存在")

        if rest[0] == "drafts":
            if len(rest) != 1:
                raise ApiError("not_found", "接口不存在")
            if method == "POST":
                return json_response(
                    studio.create_draft(self.root, body, actor=actor), status=201
                )
            if method == "GET":
                return json_response(
                    studio.list_drafts(
                        self.root,
                        status=_first(query, "status"),
                        domain_pack=_first(query, "domain_pack"),
                        include_archived=_flag_query(query, "include_archived"),
                    )
                )
            raise ApiError("not_found", "接口不存在")

        if len(rest) == 2 and rest[1] == "triggers":
            wid = rest[0]
            if method == "GET":
                return json_response(
                    {"triggers": triggers.list_triggers(self.root, wid)}
                )
            if method == "POST":
                return json_response(
                    triggers.create_trigger(self.root, wid, body), status=201
                )
            raise ApiError("not_found", "接口不存在")

        wid = rest[0]
        tail = rest[1:]
        if not tail:
            if method == "PATCH":
                return json_response(
                    studio.rename_workflow(self.root, wid, body, actor=actor)
                )
            if method != "GET":
                raise ApiError("not_found", "接口不存在")
            default_rev = int(studio.read_workflow(self.root, wid).get("current_rev") or 0)
            rev = _rev_query(_first(query, "rev"), default_rev)
            return json_response(studio.get_workflow(self.root, wid, rev=rev))

        if len(tail) == 1:
            action = tail[0]
            if action == "nodes" and method == "POST":
                return json_response(
                    studio.add_node(self.root, wid, body, actor=actor), status=201
                )
            if action == "edges" and method == "POST":
                return json_response(
                    studio.add_edge(self.root, wid, body, actor=actor), status=201
                )
            if action == "replan" and method == "POST":
                return json_response(studio.replan(self.root, wid, body, actor=actor))
            if action == "apply-diff" and method == "POST":
                return json_response(studio.apply_diff(self.root, wid, body, actor=actor))
            if action == "archive" and method == "POST":
                return json_response(
                    studio.archive_workflow(self.root, wid, body, actor=actor)
                )
            if action == "freeze" and method == "POST":
                return json_response(studio.freeze(self.root, wid, body, actor=actor))
            if action == "unfreeze" and method == "POST":
                return json_response(studio.unfreeze(self.root, wid, body, actor=actor))
            if action == "validate" and method == "POST":
                return json_response(self._studio_validate(wid, body))
            if action == "debug" and method == "POST":
                return json_response(
                    studio.debug_workflow(self.root, wid, body, actor=actor), status=202
                )
            if action == "review" and method == "POST":
                return json_response(studio.review(self.root, wid, body))
            if action == "publish" and method == "POST":
                result = studio.publish(self.root, wid, body, actor=actor)
                # 发布成功即按 wid 幂等建/复用 workflow 任务，便于任务列表直接运行。
                result["task"] = service.ensure_published_workflow_task(
                    self.root, wid, int(result["rev"])
                )
                return json_response(result)
            if action == "state" and method == "POST":
                return json_response(
                    studio.transition_state(self.root, wid, body, actor=actor)
                )

        if len(tail) == 3 and tail[0] == "nodes" and tail[2] == "implementation":
            if method != "GET":
                raise ApiError("not_found", "接口不存在")
            default_rev = int(studio.read_workflow(self.root, wid).get("current_rev") or 0)
            rev = _rev_query(_first(query, "rev"), default_rev)
            return json_response(
                studio.node_implementation(self.root, wid, tail[1], rev=rev)
            )

        if len(tail) == 2 and tail[0] == "nodes":
            node_id = tail[1]
            if method == "PATCH":
                return json_response(
                    studio.patch_node(self.root, wid, node_id, body, actor=actor)
                )
            if method == "DELETE":
                return json_response(
                    studio.delete_node(self.root, wid, node_id, actor=actor)
                )

        raise ApiError("not_found", "接口不存在")

    def _studio_validate(self, wid: str, body: Any) -> dict[str, Any]:
        data = body if isinstance(body, dict) else {}
        workflow = studio.read_workflow(self.root, wid)
        current = int(workflow.get("current_rev") or 0)
        rev = _rev_query(data.get("rev"), current)
        revision = studio.read_revision(self.root, wid, rev)
        request_env = data.get("env") if isinstance(data.get("env"), dict) else {}
        baseline = service.load_last_env(self.root)
        available_env = set(baseline) | {str(key) for key in request_env}
        return studio.validate_nodes(
            self.root,
            wid=wid,
            rev=rev,
            nodes=revision.get("nodes") or [],
            edges=revision.get("edges") or [],
            intent=str(workflow.get("intent") or ""),
            env_schema=revision.get("env_schema") or [],
            assumptions=revision.get("assumptions") or [],
            available_env=available_env,
            checks=data.get("checks"),
            domain_pack=str(workflow.get("domain_pack") or ""),
            origin=str(workflow.get("origin") or ""),
            explore_id=str(workflow.get("explore_id") or ""),
        )

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
                {
                    "error": {
                        "code": "too_many_attempts",
                        "message": "尝试过多，请 5 分钟后再试",
                    },
                    # additive：前端可直接展示倒计时；Retry-After 头保持兼容。
                    "retry_after_seconds": retry_after,
                },
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


def _cursor_query(query: dict[str, list[str]], key: str, default: int = 0) -> int:
    raw = _first(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError("bad_request", f"{key} 必须是整数") from exc
    return max(0, value)


def _offset_query(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _first(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError("bad_request", f"{key} 必须是整数") from exc
    if value < 0:
        raise ApiError("bad_request", f"{key} 不能为负数")
    return value


def _flag_query(query: dict[str, list[str]], key: str) -> bool:
    raw = _first(query, key)
    return bool(raw and raw.strip().lower() in ("1", "true", "yes", "on"))


def _rev_query(raw: Any, default: int) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ApiError("bad_request", "rev 必须是非负整数")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError("bad_request", "rev 必须是非负整数") from exc
    if value < 0:
        raise ApiError("bad_request", "rev 必须是非负整数")
    return value


def _attachment_header(filename: str, *, inline: bool = False) -> str:
    """安全的 Content-Disposition：ASCII 回退 + RFC 5987 文件名。"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "artifact") or "artifact"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{safe}\"; filename*=UTF-8''{quote(filename or safe, safe='')}"


def _schedule_env(body: Any) -> dict[str, Any]:
    """webhook / run-now 可带 ``env`` 覆盖；非对象一律 400。"""
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApiError("bad_request", "请求体必须是 JSON 对象")
    env = body.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ApiError("bad_request", "env 必须是对象")
    return env


def _no_content() -> Response:
    """204 无实体：content_type 留空，server 层不会写 Content-Type/Content-Length。"""
    return Response(status=204, body=b"", content_type="", headers={"Cache-Control": "no-store"})


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
    "is_sensitive_env_key",
    "json_response",
]
