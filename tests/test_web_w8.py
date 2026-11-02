"""W8 收口（契约 v5.1 §5.1）：执行、取消、归档、报告签署/作废。

覆盖五项 P0：执行列表/详情、取消语义（job / batch）、任务与项目软归档、
报告签署锁定与 force、作废不可撤销，以及统一审计。
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from panguverse.web.api import WebAPI

SLEEP_SCENARIO = """\
name: {name}
cases:
  - id: {name}
    name: {name}
    steps:
{steps}
"""


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    route, _, query = path.partition("?")
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    response = api.handle(method, route, query, raw)
    body = json.loads(response.body.decode("utf-8")) if response.body else None
    return response.status, body


def write_scenario(root: Path, name: str, sleeps: list[float]) -> str:
    directory = root / "scenarios"
    directory.mkdir(parents=True, exist_ok=True)
    steps = "\n".join(
        f"      - id: step_{index}\n        use: sleep\n        with: {{seconds: {seconds}}}"
        for index, seconds in enumerate(sleeps)
    )
    (directory / f"{name}.yaml").write_text(
        SLEEP_SCENARIO.format(name=name, steps=steps), encoding="utf-8"
    )
    return f"scenarios/{name}.yaml"


def wait_job(api: WebAPI, job_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, job = call(api, "GET", f"/api/jobs/{job_id}")
        assert status == 200, job
        if job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def wait_running(api: WebAPI, job_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, job = call(api, "GET", f"/api/jobs/{job_id}")
        assert status == 200, job
        if job.get("status") == "running" and job.get("run_id"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} 未进入 running")


def wait_batch(api: WebAPI, batch_id: str, *, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, batch = call(api, "GET", f"/api/batches/{batch_id}")
        assert status == 200, batch
        if batch.get("status") in ("done", "failed", "cancelled"):
            return batch
        time.sleep(0.02)
    raise AssertionError(f"batch {batch_id} 未在 {timeout}s 内结束")


@pytest.fixture()
def api(tmp_path: Path) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    return WebAPI(root)


def audit_entries(api: WebAPI) -> list[dict[str, Any]]:
    path = api.root / ".panguverse" / "web" / "audit.jsonl"
    if not path.is_file():
        return []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# 1) 执行列表 / 详情
# --------------------------------------------------------------------------- #
def test_executions_list_and_detail_merge_disk_and_job(api: WebAPI) -> None:
    scenario = write_scenario(api.root, "exec_one", [0.01])
    status, queued = call(api, "POST", "/api/runs", {"mode": "scenario", "scenario": scenario})
    assert status == 202, queued
    job = wait_job(api, queued["job_id"])
    assert job["status"] == "done"
    run_id = job["run_id"]

    status, listing = call(api, "GET", "/api/executions")
    assert status == 200, listing
    assert set(listing) >= {"executions", "total", "offset", "limit"}
    row = next(item for item in listing["executions"] if item["run_id"] == run_id)
    assert row["job_id"] == queued["job_id"]
    assert row["eid"] == run_id
    assert row["status"] == "done"
    assert row["trigger"] == "task"
    assert row["passed"] == row["total"] == 1
    assert row["duration_ms"] >= 0
    assert "workflow_ref" in row and "project_id" in row

    status, detail = call(api, "GET", f"/api/executions/{run_id}")
    assert status == 200, detail
    assert detail["run_id"] == run_id
    assert detail["job_id"] == queued["job_id"]
    assert detail["summary"]["passed"] == 1

    status, page = call(api, "GET", "/api/executions?status=done&offset=0&limit=1")
    assert status == 200
    assert page["limit"] == 1 and len(page["executions"]) <= 1
    status, bad = call(api, "GET", "/api/executions/not-a-run")
    assert status == 400 and bad["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# 2) job 取消：当前步骤结束后停
# --------------------------------------------------------------------------- #
def test_cancel_job_stops_after_current_step(api: WebAPI) -> None:
    scenario = write_scenario(api.root, "cancel_job", [0.6, 0.05, 0.05])
    status, queued = call(api, "POST", "/api/runs", {"mode": "scenario", "scenario": scenario})
    assert status == 202
    running = wait_running(api, queued["job_id"])
    assert running["status"] == "running"

    status, body = call(api, "POST", f"/api/executions/{queued['job_id']}/cancel", {})
    assert status == 200, body
    assert body["status"] == "cancelling"

    job = wait_job(api, queued["job_id"])
    assert job["status"] == "cancelled"
    assert job["error"] and "取消" in job["error"]
    run = call(api, "GET", f"/api/runs/{job['run_id']}")[1]
    steps = run["report"]["cases"][0]["steps"]
    assert steps[0]["status"] == "passed"
    assert steps[1]["status"] == "skipped"
    assert steps[2]["status"] == "skipped"

    status, body = call(api, "POST", f"/api/executions/{queued['job_id']}/cancel", {})
    assert status == 409
    assert body["error"]["code"] == "conflict"

    entries = audit_entries(api)
    assert any(
        entry["action"] == "execution.cancel"
        and entry["target"] == f"execution:{queued['job_id']}"
        for entry in entries
    )


# --------------------------------------------------------------------------- #
# 3) batch 取消：已完成 run 保留，未开始套件 cancelled
# --------------------------------------------------------------------------- #
def test_cancel_batch_keeps_completed_runs_and_cancels_pending(api: WebAPI) -> None:
    project = call(api, "POST", "/api/projects", {"name": "批次取消"})[1]
    pid = project["project_id"]
    for name, sleeps in (
        ("batch_fast", [0.05]),
        ("batch_slow", [0.8]),
        ("batch_late", [0.05]),
    ):
        scenario = write_scenario(api.root, name, sleeps)
        status, suite = call(
            api, "POST", f"/api/projects/{pid}/suites", {"name": name, "scenario": scenario}
        )
        assert status == 201, suite

    status, started = call(api, "POST", f"/api/projects/{pid}/run", {})
    assert status == 202, started
    batch_id = started["batch_id"]
    second_job = started["jobs"][1]["job_id"]

    deadline = time.time() + 8
    while time.time() < deadline:
        snapshot = call(api, "GET", f"/api/batches/{batch_id}")[1]
        statuses = [entry["status"] for entry in snapshot["suites"]]
        if statuses[:2] == ["done", "running"]:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"批次未进入预期状态: {statuses}")

    status, body = call(api, "POST", f"/api/executions/{second_job}/cancel", {})
    assert status == 200, body
    assert body["batch_id"] == batch_id

    final = wait_batch(api, batch_id)
    assert final["status"] == "cancelled", final
    by_name = {entry["name"]: entry for entry in final["suites"]}
    assert by_name["batch_fast"]["status"] == "done"
    assert by_name["batch_fast"]["run_id"]
    assert by_name["batch_slow"]["status"] == "cancelled"
    assert by_name["batch_late"]["status"] == "cancelled"
    assert by_name["batch_late"]["run_id"] == ""

    # 已完成的 run 仍在磁盘历史中，不受 batch 取消影响
    status, runs = call(api, "GET", "/api/runs")
    assert status == 200
    assert by_name["batch_fast"]["run_id"] in {item["run_id"] for item in runs}

    entries = audit_entries(api)
    assert any(entry["action"] == "execution.cancel" and entry.get("to") == "cancelling" for entry in entries)


# --------------------------------------------------------------------------- #
# 4) 任务 / 项目软归档
# --------------------------------------------------------------------------- #
def test_task_and_project_archive_are_soft_and_default_hidden(api: WebAPI) -> None:
    task = call(api, "POST", "/api/tasks", {"name": "归档任务", "mode": "demo"})[1]
    project = call(api, "POST", "/api/projects", {"name": "归档项目"})[1]
    tid, pid = task["task_id"], project["project_id"]

    assert call(api, "POST", f"/api/tasks/{tid}/archive", {})[0] == 200
    assert call(api, "POST", f"/api/projects/{pid}/archive", {})[0] == 200

    assert call(api, "GET", "/api/tasks")[1]["tasks"] == []
    assert call(api, "GET", "/api/projects")[1]["projects"] == []
    tasks_all = call(api, "GET", "/api/tasks?include_archived=1")[1]["tasks"]
    projects_all = call(api, "GET", "/api/projects?include_archived=1")[1]["projects"]
    assert tasks_all[0]["task_id"] == tid and tasks_all[0]["archived_at"]
    assert projects_all[0]["project_id"] == pid and projects_all[0]["archived_at"]

    # 详情仍可读，历史/报告不受影响
    assert call(api, "GET", f"/api/tasks/{tid}")[0] == 200
    assert call(api, "GET", f"/api/projects/{pid}")[0] == 200

    assert call(api, "POST", f"/api/tasks/{tid}/unarchive", {})[0] == 200
    assert call(api, "POST", f"/api/projects/{pid}/unarchive", {})[0] == 200
    assert [item["task_id"] for item in call(api, "GET", "/api/tasks")[1]["tasks"]] == [tid]
    assert [item["project_id"] for item in call(api, "GET", "/api/projects")[1]["projects"]] == [pid]

    actions = [entry["action"] for entry in audit_entries(api)]
    assert "task.archive" in actions and "task.unarchive" in actions
    assert "project.archive" in actions and "project.unarchive" in actions


# --------------------------------------------------------------------------- #
# 5) 报告列表 / 签署锁定 / force / 作废不可撤销
# --------------------------------------------------------------------------- #
def test_reports_sign_lock_force_and_void(api: WebAPI) -> None:
    scenario = write_scenario(api.root, "report_one", [0.01])
    queued = call(api, "POST", "/api/runs", {"mode": "scenario", "scenario": scenario})[1]
    job = wait_job(api, queued["job_id"])
    run_id = job["run_id"]

    status, listing = call(api, "GET", "/api/reports")
    assert status == 200, listing
    row = next(item for item in listing["reports"] if item["run_id"] == run_id)
    assert row["conclusion"]["success"] is True
    assert row["has_insight"] is False
    assert row["signed"] is False and row["voided"] is False
    assert row["sign_state"] == "unsigned"
    assert "workflow_ref" in row

    status, signed = call(
        api, "POST", f"/api/reports/{run_id}/sign", {"verdict": "approved", "note": "人工确认"}
    )
    assert status == 200, signed
    assert signed["verdict"] == "approved" and signed["signed"] is True
    assert call(api, "GET", "/api/reports?status=signed")[1]["total"] == 1

    status, body = call(api, "POST", f"/api/reports/{run_id}/sign", {"verdict": "rejected"})
    assert status == 409 and "force" in body["error"]["message"]

    status, forced = call(
        api, "POST", f"/api/reports/{run_id}/sign", {"verdict": "rejected", "force": True}
    )
    assert status == 200, forced
    assert forced["history"][-1]["forced"] is True

    status, body = call(api, "POST", f"/api/reports/{run_id}/void", {"reason": " "})
    assert status == 400
    status, voided = call(
        api, "POST", f"/api/reports/{run_id}/void", {"reason": "被测目标错误"}
    )
    assert status == 200, voided
    assert voided["voided"] is True and voided["void_reason"] == "被测目标错误"
    assert call(api, "GET", "/api/reports?status=voided")[1]["total"] == 1

    status, body = call(api, "POST", f"/api/reports/{run_id}/void", {"reason": "再作废"})
    assert status == 409, body
    status, body = call(
        api, "POST", f"/api/reports/{run_id}/sign", {"verdict": "approved", "force": True}
    )
    assert status == 409, body

    record_path = api.root / ".panguverse" / "web" / "reports" / f"{run_id}.json"
    assert record_path.is_file() and stat.S_IMODE(record_path.stat().st_mode) == 0o600
    actions = [entry["action"] for entry in audit_entries(api)]
    assert "report.sign" in actions and "report.void" in actions
