"""任务执行态（debug / frozen）后端契约测试。

覆盖：无成功运行不能固化、成功运行后固化与列表字段、回到调试保留历史、
固化运行 meta 标记且不触发 LLM 规划、未知 wid / 非法 rev / 必填 message。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

import panguverse.axe.axe as axe_module
from panguverse.web import service, studio
from panguverse.web.api import WebAPI

INTENT = "备份配置文件并校验备份存在"


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    route, _, query = path.partition("?")
    response = api.handle(method, route, query, raw)
    body = json.loads(response.body.decode("utf-8")) if response.body else None
    return response.status, body


@pytest.fixture()
def api(tmp_path: Path) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    service.save_last_env(root, {"FS_ROOT": str(fs_root)})
    return WebAPI(root)


def wait_job(api: WebAPI, job_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = call(api, "GET", f"/api/jobs/{job_id}")
        assert status == 200, body
        if body.get("status") in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def approved_published(api: WebAPI) -> tuple[str, str]:
    status, draft = call(
        api, "POST", "/api/studio/drafts", {"intent": INTENT, "domain_pack": "local_fs"}
    )
    assert status == 201, draft
    wid = draft["wid"]
    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "备份流程"}
    )
    assert status == 200, published
    return wid, published["task"]["task_id"]


def run_task(api: WebAPI, task_id: str) -> dict[str, Any]:
    status, queued = call(api, "POST", f"/api/tasks/{task_id}/run", {})
    assert status == 202, queued
    job = wait_job(api, queued["job_id"])
    assert job["status"] == "done", job
    return job


def test_freeze_without_successful_run_returns_409(api: WebAPI) -> None:
    wid, _task_id = approved_published(api)
    status, body = call(
        api, "POST", f"/api/studio/{wid}/freeze", {"rev": 0, "message": "固化"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "no_successful_run"
    workflow = studio.read_workflow(api.root, wid)
    assert studio.execution_mode_of(workflow) == "debug"
    assert workflow.get("frozen_ref") == ""


def test_freeze_after_success_writes_fields_and_listing(api: WebAPI) -> None:
    wid, task_id = approved_published(api)
    job = run_task(api, task_id)

    status, frozen = call(
        api, "POST", f"/api/studio/{wid}/freeze", {"rev": 0, "message": "固化为生产"}
    )
    assert status == 200, frozen
    ref = f"{wid}@0"
    assert frozen == {
        "wid": wid,
        "execution_mode": "frozen",
        "frozen_ref": ref,
        "goal_checks": frozen["goal_checks"],
    }
    assert frozen["goal_checks"], frozen
    assert all(item["kind"].startswith("assert_") for item in frozen["goal_checks"])
    assert all(item["passed"] is True for item in frozen["goal_checks"])
    assert frozen["goal_checks"][0]["evidence"]["status"] == "passed"

    status, listing = call(api, "GET", "/api/studio/drafts")
    assert status == 200, listing
    row = next(item for item in listing["drafts"] if item["wid"] == wid)
    assert row["execution_mode"] == "frozen"
    assert row["frozen_ref"] == ref
    assert isinstance(row["frozen_at"], (int, float))
    assert row["node_count"] > 0
    assert "archived_at" in row

    workflow = studio.read_workflow(api.root, wid)
    assert workflow["execution_mode"] == "frozen"
    assert workflow["frozen_ref"] == ref
    assert workflow["frozen_by"] == "local"
    assert workflow["goal_checks"] == frozen["goal_checks"]
    # 固化不新增 revision、不改图
    assert workflow["current_rev"] == 0
    revisions = api.root / ".panguverse" / "studio" / wid / "revisions"
    assert sorted(path.name for path in revisions.iterdir()) == ["0.json"]

    status, detail = call(api, "GET", f"/api/studio/{wid}")
    assert status == 200, detail
    assert detail["execution_mode"] == "frozen"
    assert detail["frozen_ref"] == ref
    assert detail["goal_checks"] == frozen["goal_checks"]

    status, run = call(api, "GET", f"/api/runs/{job['run_id']}")
    assert status == 200, run
    assert run["report"]["conclusion"]["success"] is True


def test_unfreeze_keeps_history_and_writes_two_audit_entries(api: WebAPI) -> None:
    wid, task_id = approved_published(api)
    run_task(api, task_id)
    status, frozen = call(
        api, "POST", f"/api/studio/{wid}/freeze", {"rev": 0, "message": "固化"}
    )
    assert status == 200, frozen
    old_ref = frozen["frozen_ref"]

    status, body = call(
        api, "POST", f"/api/studio/{wid}/unfreeze", {"message": "回到调试继续改"}
    )
    assert status == 200, body
    assert body == {
        "wid": wid,
        "execution_mode": "debug",
        "previous_frozen_ref": old_ref,
    }

    workflow = studio.read_workflow(api.root, wid)
    assert workflow["execution_mode"] == "debug"
    assert workflow["frozen_ref"] == old_ref  # 历史保留
    assert workflow["frozen_at"] is not None
    assert workflow["goal_checks"], workflow

    status, listing = call(api, "GET", "/api/studio/drafts")
    assert status == 200
    row = next(item for item in listing["drafts"] if item["wid"] == wid)
    assert row["execution_mode"] == "debug"
    assert row["frozen_ref"] == old_ref

    actions = [entry["action"] for entry in studio.audit_tail(api.root, wid, limit=50)]
    assert actions.count("freeze") == 1
    assert actions.count("unfreeze") == 1
    assert actions.index("freeze") < actions.index("unfreeze")


def test_frozen_run_marks_meta_without_llm_planning(
    api: WebAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, task_id = approved_published(api)
    run_task(api, task_id)
    status, frozen = call(
        api, "POST", f"/api/studio/{wid}/freeze", {"rev": 0, "message": "固化"}
    )
    assert status == 200, frozen

    def forbidden_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("frozen 运行不得调用 LLM/规划器")

    monkeypatch.setattr(axe_module.PanguAxe, "plan", forbidden_plan)

    real_build_axe = service.build_axe
    llm_flags: list[bool] = []

    def recording_build_axe(root: Path, *, enable_llm: bool = True) -> Any:
        llm_flags.append(enable_llm)
        return real_build_axe(root, enable_llm=enable_llm)

    monkeypatch.setattr(service, "build_axe", recording_build_axe)

    job = run_task(api, task_id)
    assert llm_flags and all(flag is False for flag in llm_flags)

    status, run = call(api, "GET", f"/api/runs/{job['run_id']}")
    assert status == 200, run
    report = run["report"]
    meta = report["meta"]
    assert meta["execution_mode"] == "frozen"
    assert meta["frozen_ref"] == frozen["frozen_ref"]
    assert meta["workflow_ref"] == frozen["frozen_ref"]
    assert meta["run_config"]["execution_mode"] == "frozen"
    assert meta["run_config"]["no_llm"] is True
    assert meta["llm_used"] is False


def test_freezing_blocks_unregistered_atom(api: WebAPI) -> None:
    wid, task_id = approved_published(api)
    run_task(api, task_id)
    revision_path = api.root / ".panguverse" / "studio" / wid / "revisions" / "0.json"
    revision = json.loads(revision_path.read_text(encoding="utf-8"))
    revision["nodes"][0]["atom"] = "atom_not_in_registry"
    studio._atomic_write_json(revision_path, revision)

    status, body = call(
        api, "POST", f"/api/studio/{wid}/freeze", {"rev": 0, "message": "固化"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "capability_gap"


def test_freeze_and_unfreeze_validation(api: WebAPI) -> None:
    wid, _task_id = approved_published(api)

    status, body = call(api, "POST", "/api/studio/w-abcdef/freeze", {"message": "x"})
    assert status == 404, body
    assert body["error"]["code"] == "not_found"

    status, body = call(api, "POST", f"/api/studio/{wid}/freeze", {"rev": "bad", "message": "x"})
    assert status == 400, body

    status, body = call(api, "POST", f"/api/studio/{wid}/freeze", {"rev": 99, "message": "x"})
    assert status == 404, body

    status, body = call(api, "POST", f"/api/studio/{wid}/freeze", {"rev": 0})
    assert status == 400, body
    assert body["error"]["code"] == "bad_request"

    status, body = call(api, "POST", f"/api/studio/{wid}/unfreeze", {})
    assert status == 400, body
    assert body["error"]["code"] == "bad_request"
