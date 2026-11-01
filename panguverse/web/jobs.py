"""后台任务：内存态 job 表 + daemon 线程 + 超时看门狗。

MVP 只存内存（服务重启即清空），不排队、允许并发；前端负责一次只发一个。
任务失败/超时一律落到 ``failed`` 并把打码后的一行消息放进 ``error``，
绝不「返回 200 但隐藏失败」。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..core.redact import mask_text

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

DEFAULT_TIMEOUT = 300.0
MAX_JOBS = 200
_SECRET_ENV_KEYS = ("PANGUVERSE_API_KEY", "OPENAI_API_KEY", "PANGUVERSE_TOKEN")


def sanitize_message(value: Any, *, limit: int = 500) -> str:
    """把异常消息压成一行并打码：错误体里不能出现密钥原文。"""
    line = " ".join(str(value if value is not None else "").split())
    secrets = [os.environ.get(key) or "" for key in _SECRET_ENV_KEYS]
    line = mask_text(line, [secret for secret in secrets if len(secret) >= 4])
    return line[:limit] or "未知错误"


def resolve_timeout(value: float | None = None) -> float:
    """单 job 超时：显式参数 > ``PANGUVERSE_WEB_JOB_TIMEOUT`` > 300s。"""
    if value is not None:
        return max(0.0, float(value))
    raw = os.environ.get("PANGUVERSE_WEB_JOB_TIMEOUT")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT


class Job:
    """一次后台运行的内存记录（字段与 ``GET /api/jobs/{id}`` 契约一致）。"""

    def __init__(self, job_id: str, mode: str) -> None:
        self.job_id = job_id
        self.mode = mode
        self.status = QUEUED
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.run_id = ""
        self.phases: list[dict[str, Any]] = []
        self.summary: dict[str, Any] | None = None
        self.conclusion: dict[str, Any] | None = None
        self.error: str | None = None
        #: 终态回调（如回写任务的 last_run_id）：在状态切到终态时同步执行，
        #: 保证「客户端看到 done/failed 时」旁路数据已经落盘
        self.on_finish: Callable[[Job], None] | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        with self._lock:
            if self.status != QUEUED:
                return
            self.status = RUNNING
            self.started_at = time.time()

    def add_phase(self, name: str, status: str = "ok", detail: str = "") -> None:
        with self._lock:
            self.phases.append({"name": name, "status": status, "detail": detail})

    def set_run_id(self, run_id: str) -> None:
        with self._lock:
            self.run_id = run_id

    def finish(
        self,
        *,
        run_id: str | None = None,
        summary: dict[str, Any] | None = None,
        conclusion: dict[str, Any] | None = None,
    ) -> bool:
        """标记完成；已进入终止态（超时/失败）时不再改写。"""
        with self._lock:
            if self.status in (DONE, FAILED):
                return False
            if run_id:
                self.run_id = run_id
            if summary is not None:
                self.summary = summary
            if conclusion is not None:
                self.conclusion = conclusion
            self.status = DONE
            self.finished_at = time.time()
            self._notify_finished()
            return True

    def fail(self, message: Any) -> bool:
        """标记失败；已进入终止态时不再改写。"""
        with self._lock:
            if self.status in (DONE, FAILED):
                return False
            self.status = FAILED
            self.error = sanitize_message(message)
            self.finished_at = time.time()
            self._notify_finished()
            return True

    def _notify_finished(self) -> None:
        """在锁内执行终态回调；回调失败不得影响 job 本身。"""
        callback = self.on_finish
        if callback is None:
            return
        with contextlib.suppress(Exception):  # 回调是尽力而为的旁路
            callback(self)

    # ------------------------------------------------------------------ #
    @property
    def elapsed_ms(self) -> int:
        if self.started_at is None:
            return 0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0, round((end - self.started_at) * 1000))

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "mode": self.mode,
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_ms": self.elapsed_ms,
                "run_id": self.run_id,
                "phases": list(self.phases),
                "summary": self.summary,
                "conclusion": self.conclusion,
                "error": self.error,
            }


class JobManager:
    """job 表 + 线程调度 + 超时。线程安全（``threading.Lock``）。"""

    def __init__(self, *, timeout: float | None = None) -> None:
        self.timeout = resolve_timeout(timeout)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def submit(
        self,
        mode: str,
        task: Callable[[Job], dict[str, Any] | None],
        *,
        on_finish: Callable[[Job], None] | None = None,
    ) -> Job:
        return self.start(self.create(mode, on_finish=on_finish), task)

    def create(
        self, mode: str, *, on_finish: Callable[[Job], None] | None = None
    ) -> Job:
        """只登记一个 queued job、不启动线程。

        批量运行需要**先返回全部 job_id**（202 响应里带 jobs 数组），再按套件串行
        启动，因此把「登记」和「启动」拆开；``submit`` 的行为保持不变。
        """
        job = Job(uuid.uuid4().hex[:12], mode)
        job.on_finish = on_finish
        with self._lock:
            self._jobs[job.job_id] = job
            self._prune_locked()
        return job

    def start(self, job: Job, task: Callable[[Job], dict[str, Any] | None]) -> Job:
        """在独立 daemon 线程里启动一个已登记的 queued job。"""
        thread = threading.Thread(
            target=self._worker,
            args=(job, task),
            name=f"pv-job-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        return jobs[: max(1, limit)]

    # ------------------------------------------------------------------ #
    def _worker(self, job: Job, task: Callable[[Job], dict[str, Any] | None]) -> None:
        job.start()
        watchdog: threading.Timer | None = None
        if self.timeout > 0:
            watchdog = threading.Timer(self.timeout, job.fail, args=("timeout",))
            watchdog.daemon = True
            watchdog.start()
        try:
            result = task(job) or {}
            job.finish(
                run_id=result.get("run_id"),
                summary=result.get("summary"),
                conclusion=result.get("conclusion"),
            )
        except Exception as exc:  # noqa: BLE001 - 任务异常必须落到 failed，而不是吞掉
            job.fail(f"{type(exc).__name__}: {exc}")
        finally:
            if watchdog is not None:
                watchdog.cancel()

    def _prune_locked(self) -> None:
        if len(self._jobs) <= MAX_JOBS:
            return
        ordered = sorted(self._jobs.values(), key=lambda item: item.created_at)
        for job in ordered[: len(self._jobs) - MAX_JOBS]:
            if job.status in (DONE, FAILED):
                self._jobs.pop(job.job_id, None)


__all__ = [
    "DONE",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "Job",
    "JobManager",
    "resolve_timeout",
    "sanitize_message",
]
