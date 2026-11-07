"""执行中心：列表/详情/取消 + 可协作取消的执行器。

P0 语义（契约 v5.1 §5.1）：

- **execution 列表**：磁盘 ``runs/`` 与内存 job 合并，同一次执行一条；
- **取消 job**：设置协作取消标记，执行器在**当前步骤结束后**停止，不再开始下一步；
- **取消 batch**：已完成的 run 保留；未开始的套件标 ``cancelled``；当前 job 按上面规则停；
- **已结束** 的取消一律 409。

引擎本身不修改：这里用 ``Executor`` 子类在执行边界检查取消标记。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..core.errors import PlanError
from ..engine.executor import Executor
from ..engine.result import ERROR, SKIPPED, RunResult, StepResult
from . import audit, phase_store, service
from .batches import (
    BATCH_DONE,
    BATCH_FAILED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    BatchManager,
)
from .jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, Job

BATCH_CANCELLED = "cancelled"
SUITE_CANCELLED = "cancelled"
EID_RE = re.compile(r"^[0-9a-f]{6,32}$")
_CANCEL_STATUSES = {QUEUED, RUNNING, BATCH_QUEUED, BATCH_RUNNING}


class JobCancelled(Exception):
    """执行器协作取消信号（不写进最终 job 错误）。"""


# --------------------------------------------------------------------------- #
# 可取消执行器
# --------------------------------------------------------------------------- #
class CancellableExecutor(Executor):
    """在执行步骤边界检查 job 取消标记，尽可能保留已完成步骤的结果。"""

    def __init__(self, *args: Any, job: Job, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.job = job

    def _execute_step(self, case: Any, step: Any, ctx: Any, heal_budget: int) -> Any:
        if self.job.cancel_requested:
            # 当前步骤照常跑完；从这里开始不再调用任何原子，剩余步骤一律 skipped。
            return (
                StepResult(
                    step_id=step.id,
                    atom=step.use,
                    name=step.name,
                    status=SKIPPED,
                    skipped_reason="用户取消：当前步骤结束后停止",
                ),
                0,
            )
        return super()._execute_step(case, step, ctx, heal_budget)

    def run(
        self,
        plan: Any,
        *,
        env: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        run_id: str | None = None,
        meta: dict[str, Any] | None = None,
        output_dir: Any = None,
    ) -> RunResult:
        cases = list(getattr(plan, "cases", []) or [])
        if not cases:
            raise PlanError("计划中没有任何用例")
        from ..http.client import backend_name

        plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
        plan_env = dict(getattr(plan, "env", {}) or {})
        merged_env = {**plan_env, **(env or {})}
        run = RunResult(
            run_id=run_id or uuid.uuid4().hex[:12],
            plan=plan_dict,
            env=merged_env,
            meta={**(meta or {}), "backend": backend_name(), "atom_count": len(self.registry)},
        )
        run.add_phase("plan", "ok", f"{len(cases)} 个用例来自 {getattr(plan, 'source', 'unknown')}")
        for case in cases:
            if self.job.cancel_requested:
                run.notes.append("用户取消：在当前步骤结束后停止，未开始的用例未执行")
                break
            case_result = self.run_case(
                case,
                env=merged_env,
                variables=variables,
                run_id=run.run_id,
                output_dir=output_dir,
            )
            run.cases.append(case_result)
            if self.settings.stop_on_first_failure and case_result.status in (FAILED, ERROR):
                run.notes.append(f"fail-fast：用例 {case.case_id} 失败后停止")
                break
            if self.job.cancel_requested:
                run.notes.append("用户取消：在当前步骤结束后停止，未开始的用例未执行")
                break
        run.finished_at = time.time()
        summary = run.summary
        run.add_phase(
            "execute",
            "ok" if summary["success"] else "failed",
            f"{summary['passed']}/{summary['total']} 通过，"
            f"{summary['failed']} 失败，{summary['errored']} 异常，"
            f"{summary['http_requests']} 次 HTTP 调用",
        )
        return run


def install_cancellation(axe: Any, job: Job) -> None:
    """给本次运行的 axe 换上可取消执行器（幂等）。"""
    current = getattr(axe, "executor", None)
    if isinstance(current, CancellableExecutor) and current.job is job:
        return
    axe.executor = CancellableExecutor(
        axe.registry,
        settings=axe.settings,
        healer=axe.healer,
        workspace=axe.workspace,
        job=job,
    )


# --------------------------------------------------------------------------- #
# 可取消批次
# --------------------------------------------------------------------------- #
class CancellableBatchManager(BatchManager):
    """在 BatchManager 之上补取消语义，不改原调度器的串行约束。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._job_batches: dict[str, str] = {}
        self._batch_jobs: dict[str, list[Job]] = {}

    def submit(self, project_id: str, items: list[Any]) -> Any:
        batch = super().submit(project_id, items)
        with self._lock:
            self._batch_jobs[batch.batch_id] = [item.job for item in items]
            for item in items:
                self._job_batches[item.job.job_id] = batch.batch_id
        return batch

    def batch_for_job(self, job_id: str) -> str:
        with self._lock:
            return self._job_batches.get(job_id, "")

    def cancel(self, batch_id: str, *, note: str = "") -> dict[str, Any] | None:
        batch = self.get(batch_id)
        if batch is None:
            return None
        if batch.status in (BATCH_DONE, BATCH_FAILED, BATCH_CANCELLED):
            return {"cancelled": False, "status": batch.status}
        batch.cancel_requested = True
        cancelled_suites: list[str] = []
        jobs = []
        with self._lock:
            jobs = list(self._batch_jobs.get(batch_id, []))
        with batch._lock:
            current_job_id = getattr(batch, "_current_job_id", "")
            for entry in batch._entries:
                if entry.status == "queued":
                    entry.status = SUITE_CANCELLED
                    entry.error = "批量已取消"
                    cancelled_suites.append(entry.suite_id)
        if current_job_id:
            current = self.jobs.get(current_job_id)
            if current is not None:
                current.request_cancel()
        for job in jobs:
            if job.job_id == current_job_id:
                continue
            if job.status == QUEUED:
                job.cancel("批量已取消")
        return {
            "cancelled": True,
            "status": "cancelling" if current_job_id else BATCH_CANCELLED,
            "batch_id": batch_id,
            "current_job_id": current_job_id,
            "cancelled_suites": cancelled_suites,
        }

    # -- 调度循环（与基类同序，额外处理取消） -------------------------- #
    def _run(self, batch: Any, items: list[Any]) -> None:
        batch._set_running()
        for item in items:
            with batch._lock:
                entry = batch._entry(item.suite_id)
                cancelled = bool(getattr(batch, "cancel_requested", False)) or entry.status == SUITE_CANCELLED
                if entry.status == "queued" and cancelled:
                    entry.status = SUITE_CANCELLED
                    entry.error = "批量已取消"
            if cancelled:
                item.job.cancel("批量已取消")
                continue
            batch._mark_running(item.suite_id)
            batch._current_job_id = item.job.job_id
            finished = threading_event()

            def on_finish(job: Job, *, suite_id: str = item.suite_id, event: Any = finished) -> None:
                self._record_entry(batch, suite_id, job)
                event.set()

            item.job.on_finish = on_finish
            try:
                self.jobs.start(item.job, item.task)
            except Exception as exc:  # noqa: BLE001
                batch._mark_failed(item.suite_id, str(exc))
                finished.set()
            finished.wait()
            batch._current_job_id = ""
        self._finish_batch(batch)

    def _record_entry(self, batch: Any, suite_id: str, job: Job) -> None:
        if job.status == CANCELLED:
            with batch._lock:
                entry = batch._entry(suite_id)
                if job.run_id:
                    entry.run_id = job.run_id
                entry.status = SUITE_CANCELLED
                entry.error = job.error or "已取消"
            return
        batch._record(suite_id, job, self._summary_for(job))

    def _finish_batch(self, batch: Any) -> None:
        with batch._lock:
            if getattr(batch, "cancel_requested", False):
                batch.status = BATCH_CANCELLED
            elif any(entry.status == "failed" for entry in batch._entries):
                batch.status = BATCH_FAILED
            else:
                batch.status = BATCH_DONE


def threading_event() -> Any:
    """延迟导入 threading，保持模块顶部的执行相关导入更聚焦。"""
    import threading

    return threading.Event()


# --------------------------------------------------------------------------- #
# 执行列表 / 详情
# --------------------------------------------------------------------------- #
def _execution_meta(root: Path, run_id: str) -> dict[str, Any]:
    path = Path(root) / ".panguverse" / "executions" / run_id / "execution.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    # phases 单独落盘：优先磁盘快照，缺失时保留 execution.json 自带字段（若有）。
    stored = phase_store.read_phases(root, run_id)
    if stored:
        data["phases"] = stored
    elif not isinstance(data.get("phases"), list):
        data["phases"] = []
    return data


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _trigger_of(meta: dict[str, Any], config: dict[str, Any], execution: dict[str, Any]) -> str:
    for value in (
        execution.get("trigger"),
        config.get("trigger"),
        meta.get("trigger"),
    ):
        if isinstance(value, str) and value:
            return value
    mode = str(config.get("mode") or execution.get("mode") or "")
    if mode in ("workflow", "scenario", "intent", "demo", "regression"):
        return "task"
    return "manual"


def _disk_row(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    run_id = str(run.get("run_id") or "")
    meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
    config = meta.get("run_config") if isinstance(meta.get("run_config"), dict) else {}
    execution = _execution_meta(root, run_id)
    phases = execution.get("phases")
    if not isinstance(phases, list) or not phases:
        # 旧记录兼容：没有 sidecar 或 sidecar 为空时回退 report.phases。
        report_phases = run.get("phases")
        if isinstance(report_phases, list) and report_phases:
            phases = report_phases
    if not isinstance(phases, list):
        phases = []
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    run_status = str(run.get("status") or "done")
    status = str(execution.get("status") or run_status)
    if status == "ok":
        status = DONE
    return {
        "eid": run_id,
        "execution_id": run_id,
        "run_id": run_id,
        "job_id": execution.get("job_id") or "",
        "batch_id": "",
        "mode": str(execution.get("mode") or config.get("mode") or ""),
        "trigger": _trigger_of(meta, config, execution),
        "project_id": run.get("project_id") or execution.get("project_id") or "",
        "suite_id": run.get("suite_id") or execution.get("suite_id") or "",
        "workflow_ref": meta.get("workflow_ref") or execution.get("workflow_ref") or "",
        "status": status,
        "run_status": run_status,
        "passed": _as_int(summary.get("passed")),
        "total": _as_int(summary.get("total")),
        "duration_ms": _as_float(run.get("duration_ms") or summary.get("duration_ms")),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "created_at": run.get("started_at"),
        "error": execution.get("error") or "",
        "conclusion": run.get("conclusion"),
        "summary": summary,
        "phases": phases,
        "cancellable": False,
    }


def _job_row(job: Job, batch_id: str = "") -> dict[str, Any]:
    data = job.to_dict()
    run_id = str(data.get("run_id") or "")
    status = str(data.get("status") or QUEUED)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "eid": run_id or job.job_id,
        "execution_id": run_id or job.job_id,
        "run_id": run_id,
        "job_id": job.job_id,
        "batch_id": batch_id,
        "mode": data.get("mode") or "",
        "trigger": "manual",
        "project_id": "",
        "suite_id": "",
        "workflow_ref": "",
        "status": status,
        "run_status": "",
        "passed": _as_int(summary.get("passed")),
        "total": _as_int(summary.get("total")),
        "duration_ms": _as_float(data.get("elapsed_ms")),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "created_at": data.get("created_at"),
        "error": data.get("error") or "",
        "conclusion": data.get("conclusion"),
        "summary": summary,
        "phases": data.get("phases") or [],
        "cancellable": status in (QUEUED, RUNNING),
    }


def _merge_row(base: dict[str, Any], job: Job, batch_id: str) -> dict[str, Any]:
    data = job.to_dict()
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else base.get("summary") or {}
    merged = dict(base)
    merged.update(
        {
            "eid": base.get("run_id") or job.job_id,
            "execution_id": base.get("run_id") or job.job_id,
            "job_id": job.job_id,
            "batch_id": batch_id or base.get("batch_id") or "",
            "status": data.get("status") or base.get("status"),
            "passed": _as_int(summary.get("passed")) or base.get("passed", 0),
            "total": _as_int(summary.get("total")) or base.get("total", 0),
            "duration_ms": _as_float(data.get("elapsed_ms")) or base.get("duration_ms", 0),
            "started_at": data.get("started_at") or base.get("started_at"),
            "finished_at": data.get("finished_at") or base.get("finished_at"),
            "error": data.get("error") or base.get("error") or "",
            "conclusion": data.get("conclusion") or base.get("conclusion"),
            "summary": summary,
            "phases": data.get("phases") or base.get("phases") or [],
            "cancellable": str(data.get("status")) in (QUEUED, RUNNING),
        }
    )
    return merged


def collect_executions(
    root: Path,
    jobs: Any,
    batches: Any | None = None,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """磁盘 run + 内存 job 合并；同一次执行（run_id 相同）只保留一条。"""
    rows: dict[str, dict[str, Any]] = {}
    for run in service.list_runs(root, limit=limit):
        row = _disk_row(root, run)
        rows[row["eid"]] = row

    for job in jobs.recent(limit):
        eid = job.run_id or job.job_id
        batch_id = ""
        if batches is not None and hasattr(batches, "batch_for_job"):
            batch_id = batches.batch_for_job(job.job_id)
        if eid in rows:
            rows[eid] = _merge_row(rows[eid], job, batch_id)
        else:
            row = _job_row(job, batch_id)
            rows[eid] = row
    result = list(rows.values())
    result.sort(
        key=lambda item: item.get("started_at") or item.get("created_at") or 0,
        reverse=True,
    )
    return result


def _filter_rows(rows: list[dict[str, Any]], *, project: str, status: str) -> list[dict[str, Any]]:
    wanted = (project or "").strip()
    state = (status or "all").strip()
    out = []
    for row in rows:
        if wanted and wanted not in ("all",):
            owner = row.get("project_id") or ""
            if wanted in ("unclassified", "none", "_"):
                if owner:
                    continue
            elif owner != wanted:
                continue
        if state not in ("", "all"):
            candidates = {row.get("status"), row.get("run_status")}
            if state == "passed":
                if (row.get("conclusion") or {}).get("success"):
                    candidates.update({"passed", "done"})
            if state not in candidates and not (
                state == "failed" and (row.get("conclusion") or {}).get("success") is False
            ):
                continue
        out.append(row)
    return out


def list_executions(
    root: Path,
    jobs: Any,
    batches: Any | None,
    *,
    project: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    rows = _filter_rows(
        collect_executions(root, jobs, batches),
        project=project or "",
        status=status or "all",
    )
    total = len(rows)
    page = rows[offset : offset + limit]
    return {"executions": page, "total": total, "offset": offset, "limit": limit}


def get_execution(
    root: Path, jobs: Any, batches: Any | None, eid: str
) -> dict[str, Any]:
    if not isinstance(eid, str) or not EID_RE.match(eid):
        raise service.ApiError("bad_request", "execution id 非法（应为 6-32 位小写十六进制）")
    rows = collect_executions(root, jobs, batches)
    for row in rows:
        if eid in {row.get("eid"), row.get("execution_id"), row.get("job_id"), row.get("run_id")}:
            detail = dict(row)
            if detail.get("run_id"):
                try:
                    detail["report_available"] = True
                except Exception:  # noqa: BLE001
                    detail["report_available"] = False
            return detail
    raise service.ApiError("not_found", "执行不存在")


# --------------------------------------------------------------------------- #
# 取消
# --------------------------------------------------------------------------- #
def cancel_execution(
    root: Path,
    jobs: Any,
    batches: Any | None,
    eid: str,
    *,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    if not isinstance(eid, str) or not EID_RE.match(eid):
        raise service.ApiError("bad_request", "execution id 非法（应为 6-32 位小写十六进制）")

    job = next((item for item in jobs.recent(200) if eid in (item.job_id, item.run_id)), None)
    batch_id = ""
    if job is None and batches is not None and hasattr(batches, "get"):
        batch = batches.get(eid)
        if batch is not None:
            result = batches.cancel(eid, note=note)
            if result is None or not result.get("cancelled"):
                raise service.ApiError("conflict", "该批次已结束，不能取消")
            audit.append(
                root,
                actor=actor,
                action="execution.cancel",
                target=f"batch:{eid}",
                from_=batch.status,
                to=result.get("status"),
                note=note or "取消批次",
            )
            return {
                "eid": eid,
                "batch_id": eid,
                "status": result.get("status"),
                "cancelled_suites": result.get("cancelled_suites") or [],
            }
        raise service.ApiError("conflict", "执行已结束或不存在，不能取消")

    if job is None:
        raise service.ApiError("conflict", "执行已结束或不存在，不能取消")
    if job.status not in (QUEUED, RUNNING):
        raise service.ApiError("conflict", f"执行已结束（{job.status}），不能取消")

    if batches is not None and hasattr(batches, "batch_for_job"):
        batch_id = batches.batch_for_job(job.job_id)
    if batch_id and hasattr(batches, "cancel"):
        result = batches.cancel(batch_id, note=note)
        if result and result.get("cancelled"):
            audit.append(
                root,
                actor=actor,
                action="execution.cancel",
                target=f"execution:{eid}",
                from_=job.status,
                to=result.get("status"),
                note=note or f"取消批次 {batch_id}",
            )
            return {
                "eid": eid,
                "job_id": job.job_id,
                "batch_id": batch_id,
                "status": result.get("status"),
                "cancelled_suites": result.get("cancelled_suites") or [],
            }

    if job.status == QUEUED:
        job.cancel("用户取消")
        new_status = CANCELLED
    else:
        job.request_cancel()
        new_status = "cancelling"
    audit.append(
        root,
        actor=actor,
        action="execution.cancel",
        target=f"execution:{eid}",
        from_=job.status,
        to=new_status,
        note=note,
    )
    return {"eid": eid, "job_id": job.job_id, "status": new_status}


__all__ = [
    "BATCH_CANCELLED",
    "SUITE_CANCELLED",
    "CancellableBatchManager",
    "CancellableExecutor",
    "JobCancelled",
    "cancel_execution",
    "collect_executions",
    "get_execution",
    "install_cancellation",
    "list_executions",
]
