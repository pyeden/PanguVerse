"""执行阶段持久化（A7 告警在重启后仍可读）。

覆盖：

- 产物目录只读 → artifacts warning 落盘；换一个**全新 WebAPI（空 JobManager）**
  仍能从 ``GET /api/executions/{run_id}`` 读回 warning；
- 旧记录没有 phases → 200 + 空数组，或回退 ``report.json`` 里的 phases；
- phases 写盘失败 → run 仍然完成，不拖垮执行。
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from panguverse.web.api import WebAPI
from panguverse.web.server import PanguverseHTTPServer

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, base: str, workspace: Path) -> None:
        self.base = base
        self.workspace = workspace

    def request(self, method: str, path: str, payload: Any = None) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _DIRECT.open(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get_json(self, path: str) -> tuple[int, Any]:
        status, body = self.request("GET", path)
        return status, json.loads(body.decode("utf-8"))

    def post_json(self, path: str, payload: Any) -> tuple[int, Any]:
        status, body = self.request("POST", path, payload)
        return status, json.loads(body.decode("utf-8"))

    def wait_job(self, job_id: str, *, timeout: float = 120.0) -> dict[str, Any]:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.get_json(f"/api/jobs/{job_id}")
            assert status == 200, data
            if data["status"] in ("done", "failed", "cancelled"):
                return data
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


@contextlib.contextmanager
def serve(workspace: Path) -> Iterator[Client]:
    """每个实例都有自己的空 JobManager：模拟进程重启后的只读服务。"""
    api = WebAPI(workspace)
    server = PanguverseHTTPServer(("127.0.0.1", 0), api=api, quiet=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}", api.root)
    finally:
        server.shutdown()
        server.server_close()


def _artifacts_phase(row: dict[str, Any]) -> dict[str, Any] | None:
    phases = row.get("phases")
    if not isinstance(phases, list):
        return None
    matches = [phase for phase in phases if phase.get("name") == "artifacts"]
    return matches[-1] if matches else None


def test_execution_phases_survive_restart_after_warning(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root 无视目录权限，无法注入产物收集失败")
    workspace = tmp_path / "ws"

    with serve(workspace) as first:
        first.workspace.mkdir(parents=True, exist_ok=True)
        (first.workspace / "art.yaml").write_text(
            "name: artifact-links\n"
            "cases:\n"
            "  - id: c1\n"
            "    name: links\n"
            "    steps:\n"
            "      - id: links\n"
            "        use: web_extract_links\n"
            "        with:\n"
            "          html: \"<html><body><a href='/a'>A</a><a href='https://example.com/b'>B</a></body></html>\"\n",
            encoding="utf-8",
        )
        directory = first.workspace / ".panguverse" / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        original = stat.S_IMODE(directory.stat().st_mode)
        os.chmod(directory, 0o500)
        try:
            status, queued = first.post_json(
                "/api/runs", {"mode": "scenario", "scenario": "art.yaml"}
            )
            assert status == 202, queued
            job = first.wait_job(queued["job_id"])
        finally:
            os.chmod(directory, original)

        assert job["status"] == "done", job
        run_id = job["run_id"]
        assert run_id
        live = _artifacts_phase(job)
        assert live is not None and live["status"] == "warning", job
        status, before = first.get_json(f"/api/executions/{run_id}")
        assert status == 200, before
        assert _artifacts_phase(before) is not None

    # 第一个实例（及其内存 job 表）已经关闭；新实例只读磁盘。
    with serve(workspace) as second:
        status, after = second.get_json(f"/api/executions/{run_id}")
        assert status == 200, after
        phase = _artifacts_phase(after)
        assert phase is not None, after
        assert phase["status"] == "warning", phase
        assert phase["detail"].strip(), phase


def test_legacy_execution_without_phases_returns_empty_or_report_fallback(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    runs = workspace / "runs"
    executions = workspace / ".panguverse" / "executions"

    fallback_id = "abcdef123456"
    (runs / fallback_id).mkdir(parents=True)
    (runs / fallback_id / "report.json").write_text(
        json.dumps(
            {
                "run_id": fallback_id,
                "started_at": 1,
                "finished_at": 2,
                "summary": {"passed": 1, "total": 1},
                "conclusion": {"success": True},
                "phases": [{"name": "plan", "status": "ok", "detail": "legacy-report"}],
            }
        ),
        encoding="utf-8",
    )
    (executions / fallback_id).mkdir(parents=True)
    (executions / fallback_id / "execution.json").write_text(
        json.dumps({"run_id": fallback_id, "job_id": "legacyjob", "status": "done"}),
        encoding="utf-8",
    )

    empty_id = "abcdef654321"
    (runs / empty_id).mkdir(parents=True)
    (runs / empty_id / "report.json").write_text(
        json.dumps(
            {
                "run_id": empty_id,
                "started_at": 3,
                "finished_at": 4,
                "summary": {"passed": 1, "total": 1},
                "conclusion": {"success": True},
            }
        ),
        encoding="utf-8",
    )

    with serve(workspace) as client:
        status, with_fallback = client.get_json(f"/api/executions/{fallback_id}")
        assert status == 200, with_fallback
        assert with_fallback["phases"] == [
            {"name": "plan", "status": "ok", "detail": "legacy-report"}
        ]

        status, empty = client.get_json(f"/api/executions/{empty_id}")
        assert status == 200, empty
        assert empty["phases"] == []


def test_phase_write_failure_does_not_fail_run(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    state = workspace / ".panguverse"
    state.mkdir(parents=True)
    # 让 executions 目录无法创建：execution.json 与 phases.json 的写入都会失败。
    (state / "executions").write_text("not-a-directory", encoding="utf-8")

    with serve(workspace) as client:
        status, queued = client.post_json("/api/runs", {"mode": "demo"})
        assert status == 202, queued
        job = client.wait_job(queued["job_id"])
        assert job["status"] == "done", job
        assert (state / "executions").is_file()
