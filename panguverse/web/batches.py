"""批量运行：同一 batch 内套件**串行**执行，每个套件一个 job、一个 run。

契约见 ``tasks/web-refactor-spec.md`` v4.0 §3：

- 套件严格串行（前一个 job 进入终态后才启动下一个），避免争抢目标服务；
- 单套件失败**不中断**其余套件；
- ``aggregate`` 全部来自各 run 的真实报告（``summary`` 数字），不做估算；
- batch / job 都在内存，服务重启即丢；``runs/`` 落盘不受影响。

本模块不 import ``service``：任务构造与报告读取由调用方注入，避免循环依赖。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .jobs import FAILED, Job, JobManager

BATCH_QUEUED = "queued"
BATCH_RUNNING = "running"
BATCH_DONE = "done"
BATCH_FAILED = "failed"

SUITE_QUEUED = "queued"
SUITE_RUNNING = "running"
SUITE_DONE = "done"
SUITE_FAILED = "failed"

MAX_BATCHES = 100


@dataclass
class BatchItem:
    """一个待入批的套件：job 已创建（queued），任务闭包延迟构造。"""

    suite_id: str
    name: str
    job: Job
    task: Callable[[Job], Any]


class _Entry:
    """batch 内单个套件的运行状态（对外字段与契约 §2 一致）。"""

    __slots__ = ("duration_ms", "error", "job_id", "name", "passed", "run_id", "status", "suite_id", "total")

    def __init__(self, suite_id: str, name: str, job_id: str) -> None:
        self.suite_id = suite_id
        self.name = name
        self.job_id = job_id
        self.status = SUITE_QUEUED
        self.run_id = ""
        self.passed = 0
        self.total = 0
        self.duration_ms = 0.0
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "name": self.name,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "status": self.status,
            "passed": self.passed,
            "total": self.total,
            "error": self.error,
        }


class Batch:
    """内存态批次；所有字段读写都在锁内，``snapshot`` 是唯一出口。"""

    def __init__(self, batch_id: str, project_id: str, entries: list[_Entry]) -> None:
        self.batch_id = batch_id
        self.project_id = project_id
        self.created_at = time.time()
        self.status = BATCH_QUEUED
        self._entries = entries
        self._lock = threading.RLock()

    # -- 状态推进（只由 BatchManager 的调度线程调用） ------------------- #
    def _set_running(self) -> None:
        with self._lock:
            if self.status == BATCH_QUEUED:
                self.status = BATCH_RUNNING

    def _entry(self, suite_id: str) -> _Entry:
        for entry in self._entries:
            if entry.suite_id == suite_id:
                return entry
        raise KeyError(suite_id)

    def _mark_running(self, suite_id: str) -> None:
        with self._lock:
            self._entry(suite_id).status = SUITE_RUNNING

    def _record(self, suite_id: str, job: Job, summary: dict[str, Any] | None) -> None:
        """把 job 终态写进 entry：数字优先取磁盘报告 summary（真值）。"""
        with self._lock:
            entry = self._entry(suite_id)
            if job.run_id:
                entry.run_id = job.run_id
            if isinstance(summary, dict):
                entry.passed = _as_int(summary.get("passed"))
                entry.total = _as_int(summary.get("total"))
                entry.duration_ms = _as_float(summary.get("duration_ms"))
            if job.status == FAILED:
                entry.status = SUITE_FAILED
                entry.error = job.error or "运行失败"
            else:
                entry.status = SUITE_DONE
                entry.error = None

    def _mark_failed(self, suite_id: str, message: str) -> None:
        with self._lock:
            entry = self._entry(suite_id)
            entry.status = SUITE_FAILED
            entry.error = message or "运行失败"

    def _finish(self) -> None:
        with self._lock:
            if any(entry.status == SUITE_FAILED for entry in self._entries):
                self.status = BATCH_FAILED
            else:
                self.status = BATCH_DONE

    # -- 只读 ------------------------------------------------------------ #
    def _aggregate_locked(self) -> dict[str, Any]:
        entries = self._entries
        suites_passed = sum(
            1
            for entry in entries
            if entry.status == SUITE_DONE and entry.total > 0 and entry.passed == entry.total
        )
        return {
            "passed": sum(entry.passed for entry in entries),
            "total": sum(entry.total for entry in entries),
            "suites_passed": suites_passed,
            "suites_failed": len(entries) - suites_passed,
            # 串行执行：总耗时 = 各 run 真实耗时之和（可逐条对账）
            "duration_ms": round(sum(entry.duration_ms for entry in entries)),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "batch_id": self.batch_id,
                "project_id": self.project_id,
                "status": self.status,
                "suites": [entry.to_dict() for entry in self._entries],
                "aggregate": self._aggregate_locked(),
            }


class BatchManager:
    """批次表 + 串行调度线程。线程安全（``threading.Lock`` 保护表本身）。"""

    def __init__(
        self,
        jobs: JobManager,
        *,
        load_summary: Callable[[str], dict[str, Any] | None] | None = None,
        max_batches: int = MAX_BATCHES,
    ) -> None:
        self.jobs = jobs
        self.load_summary = load_summary
        self.max_batches = max(1, int(max_batches))
        self._batches: dict[str, Batch] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def submit(self, project_id: str, items: list[BatchItem]) -> Batch:
        """创建批次并立即返回；实际执行在独立 daemon 线程里串行推进。"""
        if not items:
            raise ValueError("batch 至少需要一个套件")
        batch = Batch(
            uuid.uuid4().hex[:12],
            project_id,
            [_Entry(item.suite_id, item.name, item.job.job_id) for item in items],
        )
        with self._lock:
            self._batches[batch.batch_id] = batch
            self._prune_locked()
        thread = threading.Thread(
            target=self._run,
            args=(batch, list(items)),
            name=f"pv-batch-{batch.batch_id}",
            daemon=True,
        )
        thread.start()
        return batch

    def get(self, batch_id: str) -> Batch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def recent(self, limit: int = 20) -> list[Batch]:
        with self._lock:
            batches = sorted(
                self._batches.values(), key=lambda item: item.created_at, reverse=True
            )
        return batches[: max(1, limit)]

    # ------------------------------------------------------------------ #
    def _run(self, batch: Batch, items: list[BatchItem]) -> None:
        batch._set_running()
        for item in items:
            batch._mark_running(item.suite_id)
            finished = threading.Event()

            def on_finish(job: Job, *, suite_id: str = item.suite_id, event: threading.Event = finished) -> None:
                batch._record(suite_id, job, self._summary_for(job))
                event.set()

            item.job.on_finish = on_finish
            try:
                self.jobs.start(item.job, item.task)
            except Exception as exc:  # noqa: BLE001 - 调度失败也要继续后面的套件
                batch._mark_failed(item.suite_id, str(exc))
                continue
            finished.wait()
        batch._finish()

    def _summary_for(self, job: Job) -> dict[str, Any] | None:
        """优先读磁盘报告（真值）；读不到再退回 job 内存 summary。"""
        if job.run_id and self.load_summary is not None:
            try:
                summary = self.load_summary(job.run_id)
            except Exception:  # noqa: BLE001 - 读报告失败不能让批次卡死
                summary = None
            if isinstance(summary, dict):
                return summary
        return job.summary

    def _prune_locked(self) -> None:
        if len(self._batches) <= self.max_batches:
            return
        ordered = sorted(self._batches.values(), key=lambda item: item.created_at)
        for batch in ordered[: len(self._batches) - self.max_batches]:
            if batch.status in (BATCH_DONE, BATCH_FAILED):
                self._batches.pop(batch.batch_id, None)


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


__all__ = [
    "BATCH_DONE",
    "BATCH_FAILED",
    "BATCH_QUEUED",
    "BATCH_RUNNING",
    "SUITE_DONE",
    "SUITE_FAILED",
    "SUITE_QUEUED",
    "SUITE_RUNNING",
    "Batch",
    "BatchItem",
    "BatchManager",
]
