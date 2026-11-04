"""Workflow 级触发器测试（M1）。

覆盖：创建/列表/run-now、cron 触发、重叠 skipped_overlap、webhook token
一次性/错误 token/禁用、PATCH enabled/regenerate、重启重算不补跑，以及
execution.json 与报告 meta 里的 trigger/workflow_ref/trigger_id 归因。
"""

from __future__ import annotations

import json
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from panguverse.web import schedules, studio, triggers
from panguverse.web.api import WebAPI
from panguverse.web.server import PanguverseHTTPServer

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class WebClient:
    def __init__(self, base: str, api: WebAPI) -> None:
        self.base = base
        self.api = api
        self.workspace = api.root

    def request(
        self, method: str, path: str, payload: Any = None
    ) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _DIRECT.open(req, timeout=60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def get_json(self, path: str) -> tuple[int, Any]:
        status, _headers, body = self.request("GET", path)
        return status, json.loads(body.decode("utf-8"))

    def post_json(self, path: str, payload: Any) -> tuple[int, Any]:
        status, _headers, body = self.request("POST", path, payload)
        return status, json.loads(body.decode("utf-8"))

    def patch_json(self, path: str, payload: Any) -> tuple[int, Any]:
        status, _headers, body = self.request("PATCH", path, payload)
        return status, json.loads(body.decode("utf-8"))

    def delete(self, path: str) -> int:
        status, _headers, _body = self.request("DELETE", path)
        return status

    def wait_job(self, job_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.get_json(f"/api/jobs/{job_id}")
            assert status == 200, data
            if data["status"] in ("done", "failed", "cancelled"):
                return data
            time.sleep(0.03)
        raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


@pytest.fixture()
def web(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    api = WebAPI(workspace)
    server = PanguverseHTTPServer(("127.0.0.1", 0), api=api, quiet=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    client = WebClient(f"http://127.0.0.1:{server.server_address[1]}", api)
    try:
        yield client
    finally:
        api.stop_scheduler()
        server.shutdown()
        server.server_close()


@pytest.fixture()
def published(web: WebClient) -> tuple[str, str]:
    """发布一条 local_fs workflow，返回 (wid, workflow_ref)。"""
    status, draft = web.post_json(
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201, draft
    wid = draft["wid"]
    status, body = web.post_json(
        f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    status, record = web.post_json(
        f"/api/studio/{wid}/publish", {"rev": 0, "message": "触发器流程"}
    )
    assert status == 200, record
    return wid, record["workflow_ref"]


def _fs_root(tmp_path: Path) -> str:
    root = tmp_path / "wf-root"
    root.mkdir(exist_ok=True)
    return str(root)


def _create(
    web: WebClient,
    wid: str,
    ref: str,
    *,
    kind: str = "schedule",
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow_ref": ref,
        "kind": kind,
        "env_override": {"FS_ROOT": _fs_root(web.workspace.parent)},
    }
    if kind == "schedule":
        payload["schedule"] = {"cron": "0 9 * * *", "tz": "Asia/Shanghai"}
    payload.update(overrides)
    status, data = web.post_json(f"/api/studio/{wid}/triggers", payload)
    assert status == 201, data
    return data


def _trigger_file(web: WebClient, tid: str) -> Path:
    return triggers.triggers_dir(web.workspace) / f"{tid}.json"


def _rewrite(web: WebClient, tid: str, **updates: Any) -> None:
    path = _trigger_file(web, tid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _events(web: WebClient, tid: str) -> list[dict[str, Any]]:
    return triggers.read_events(web.workspace, tid, limit=500)


def _execution(web: WebClient, run_id: str) -> dict[str, Any]:
    path = web.workspace / ".panguverse" / "executions" / run_id / "execution.json"
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# W1 创建 / 列表 / run-now
# --------------------------------------------------------------------------- #
def test_w1_create_list_and_run_now(
    web: WebClient, published: tuple[str, str], tmp_path: Path
) -> None:
    wid, ref = published
    created = _create(web, wid, ref)
    tid = created["trigger_id"]
    assert triggers.TID_RE.match(tid)
    assert created["workflow_ref"] == ref
    assert (created["wid"], created["rev"]) == (wid, 0)
    assert created["kind"] == "schedule"
    assert created["schedule"]["cron"] == "0 9 * * *"
    assert created["next_run_at"] > time.time()

    path = _trigger_file(web, tid)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    status, listing = web.get_json(f"/api/studio/{wid}/triggers")
    assert status == 200 and [item["trigger_id"] for item in listing["triggers"]] == [tid]

    status, result = web.post_json(f"/api/triggers/{tid}/run", {})
    assert status == 202 and result["status"] == "queued"
    job = web.wait_job(result["job_id"])
    assert job["status"] == "done", job
    assert job["mode"] == "workflow"

    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    meta = run["report"]["meta"]
    assert meta["trigger"] == "manual"
    assert meta["workflow_ref"] == ref
    assert meta["trigger_id"] == tid
    assert meta["run_config"]["mode"] == "workflow"

    record = _execution(web, job["run_id"])
    assert (record["mode"], record["trigger"]) == ("workflow", "manual")
    assert record["workflow_ref"] == ref and record["trigger_id"] == tid

    detail = triggers.read_trigger(web.workspace, tid)
    assert detail["last_job_id"] == result["job_id"]
    assert detail["last_status"] == "done"
    assert "triggered" in [event["kind"] for event in _events(web, tid)]


# --------------------------------------------------------------------------- #
# W2 cron 到期触发（注入 tick 时钟）
# --------------------------------------------------------------------------- #
def test_w2_cron_triggers(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    created = _create(
        web, wid, ref, schedule={"cron": "* * * * *", "tz": "Asia/Shanghai"}
    )
    tid = created["trigger_id"]
    now = time.time()
    _rewrite(web, tid, next_run_at=now - 2.0)

    fired = web.api.scheduler.tick(now=now)
    assert [item["trigger_id"] for item in fired] == [tid]
    job = web.wait_job(fired[0]["job_id"])
    assert job["status"] == "done", job

    _status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert run["report"]["meta"]["trigger"] == "schedule"
    assert run["report"]["meta"]["trigger_id"] == tid
    assert _execution(web, job["run_id"])["trigger"] == "schedule"

    detail = triggers.read_trigger(web.workspace, tid)
    assert detail["next_run_at"] is not None and detail["next_run_at"] > now
    assert [event["kind"] for event in _events(web, tid)].count("triggered") == 1


# --------------------------------------------------------------------------- #
# W3 重叠：上次 job 未终态 → skipped_overlap，不产生新 job
# --------------------------------------------------------------------------- #
def test_w3_overlap_is_skipped(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    created = _create(
        web, wid, ref, schedule=None, interval={"minutes": 1}
    )
    tid = created["trigger_id"]

    running = web.api.jobs.create("workflow")  # queued，模拟未终态
    _rewrite(web, tid, last_job_id=running.job_id, last_status="running")
    before = {job.job_id for job in web.api.jobs.recent(100)}

    status, result = web.post_json(f"/api/triggers/{tid}/run", {})
    assert status == 202 and result["status"] == "skipped_overlap"
    assert result["job_id"] is None
    after = {job.job_id for job in web.api.jobs.recent(100)}
    assert after == before
    assert "skipped_overlap" in [event["kind"] for event in _events(web, tid)]
    assert triggers.read_trigger(web.workspace, tid)["last_status"] == "skipped_overlap"


# --------------------------------------------------------------------------- #
# W4 webhook：token 一次性 / 错误 404 / 禁用 409 / payload 打码
# --------------------------------------------------------------------------- #
def test_w4_webhook_token_and_payload(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    created = _create(web, wid, ref, kind="webhook")
    tid = created["trigger_id"]
    token = created["webhook"]["token"]
    assert token and len(token) >= 32
    assert created["webhook"]["token_hint"] == token[-4:]
    assert "token_hash" not in json.dumps(created)
    on_disk = _trigger_file(web, tid).read_text(encoding="utf-8")
    assert token not in on_disk and "token_hash" in on_disk

    status, listing = web.get_json(f"/api/studio/{wid}/triggers")
    assert status == 200
    dumped = json.dumps(listing)
    assert token not in dumped and "token_hash" not in dumped
    assert listing["triggers"][0]["webhook"]["configured"] is True
    assert listing["triggers"][0]["webhook"]["token_hint"] == token[-4:]

    secret = "wh-secret-value"
    status, result = web.post_json(
        f"/api/hooks/workflow/{token}",
        {
            "env": {"FS_ROOT": _fs_root(web.workspace.parent), "API_TOKEN": secret},
            "payload": {"env": {"API_TOKEN": secret}, "note": "ok"},
        },
    )
    assert status == 202 and result["status"] == "queued"
    job = web.wait_job(result["job_id"])
    assert job["status"] == "done", job
    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert run["report"]["meta"]["trigger"] == "webhook"
    assert run["report"]["meta"]["trigger_id"] == tid
    assert _execution(web, job["run_id"])["trigger"] == "webhook"

    triggered = [e for e in _events(web, tid) if e["kind"] == "triggered"][-1]
    assert triggered["detail"]["payload"]["note"] == "ok"
    assert triggered["detail"]["payload"]["env"]["API_TOKEN"] == "***"
    assert triggered["detail"]["env"]["API_TOKEN"] == "***"

    status, body = web.post_json("/api/hooks/workflow/not-a-real-token", {})
    assert status == 404 and body["error"]["code"] == "not_found"

    status, updated = web.patch_json(f"/api/triggers/{tid}", {"enabled": False})
    assert status == 200 and updated["enabled"] is False
    status, body = web.post_json(f"/api/hooks/workflow/{token}", {})
    assert status == 409 and body["error"]["code"] == "conflict"


# --------------------------------------------------------------------------- #
# W5 PATCH：禁用仍可 run-now、重置 token、切换 cron/interval
# --------------------------------------------------------------------------- #
def test_w5_patch_enabled_regenerate_and_schedule(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    hook = _create(web, wid, ref, kind="webhook")
    tid = hook["trigger_id"]
    old_token = hook["webhook"]["token"]

    status, updated = web.patch_json(f"/api/triggers/{tid}", {"enabled": False})
    assert status == 200 and updated["enabled"] is False
    assert updated["next_run_at"] is None

    # 禁用态不允许 webhook，但 manual run-now 仍可用
    status, result = web.post_json(f"/api/triggers/{tid}/run", {})
    assert status == 202
    assert web.wait_job(result["job_id"])["status"] == "done"

    status, reset = web.patch_json(
        f"/api/triggers/{tid}", {"enabled": True, "regenerate_token": True}
    )
    assert status == 200 and reset["enabled"] is True
    new_token = reset["webhook"]["token"]
    assert new_token and new_token != old_token
    status, _body = web.post_json(f"/api/hooks/workflow/{old_token}", {})
    assert status == 404
    status, _result = web.post_json(f"/api/hooks/workflow/{new_token}", {})
    assert status == 202

    # schedule 触发器可切换到 interval，并重算 next_run_at
    cron = _create(web, wid, ref)
    status, switched = web.patch_json(
        f"/api/triggers/{cron['trigger_id']}",
        {"interval": {"minutes": 5}},
    )
    assert status == 200
    assert "schedule" not in switched and switched["interval"] == {"minutes": 5}
    assert switched["next_run_at"] > time.time()


# --------------------------------------------------------------------------- #
# W6 重启：重算 next_run_at、错过写 missed、不补跑
# --------------------------------------------------------------------------- #
def test_w6_restart_recovery_no_catchup(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    created = _create(web, wid, ref, schedule=None, interval={"minutes": 1})
    tid = created["trigger_id"]
    now = time.time()
    _rewrite(web, tid, next_run_at=now - 3600.0, last_run_at=None)

    before = {job.job_id for job in web.api.jobs.recent(100)}
    restarted = schedules.Scheduler(web.workspace, web.api.jobs, web.api.batches)
    restarted.recover(now=now)

    detail = triggers.read_trigger(web.workspace, tid)
    assert detail["next_run_at"] is not None and detail["next_run_at"] > now
    assert "missed" in [event["kind"] for event in _events(web, tid)]
    assert restarted.tick(now=now) == []
    after = {job.job_id for job in web.api.jobs.recent(100)}
    assert after == before  # 错过的时点不补跑

    # 触发器仍在列表中
    status, listing = web.get_json(f"/api/studio/{wid}/triggers")
    assert status == 200 and [item["trigger_id"] for item in listing["triggers"]] == [tid]


# --------------------------------------------------------------------------- #
# W7 校验：workflow_ref 必须已发布、cron/interval 二选一
# --------------------------------------------------------------------------- #
def test_w7_validation(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    base = {"workflow_ref": ref, "kind": "schedule"}

    status, body = web.post_json(
        f"/api/studio/{wid}/triggers", {**base, "schedule": {"cron": "bad"}}
    )
    assert status == 400 and body["error"]["code"] == "bad_request"

    status, body = web.post_json(
        f"/api/studio/{wid}/triggers",
        {
            **base,
            "schedule": {"cron": "* * * * *"},
            "interval": {"minutes": 5},
        },
    )
    assert status == 400

    status, body = web.post_json(
        f"/api/studio/{wid}/triggers",
        {"workflow_ref": "w-000000@9", "kind": "manual"},
    )
    assert status == 404

    status, body = web.post_json(
        f"/api/studio/{wid}/triggers",
        {"workflow_ref": ref, "kind": "webhook", "interval": {"minutes": 5}},
    )
    assert status == 400

    # 已发布记录可被 studio.load_published 校验
    assert studio.load_published(web.workspace, ref)["wid"] == wid


# --------------------------------------------------------------------------- #
# W8 DELETE：只删 trigger，不删历史 run
# --------------------------------------------------------------------------- #
def test_w8_delete_keeps_runs(
    web: WebClient, published: tuple[str, str]
) -> None:
    wid, ref = published
    created = _create(web, wid, ref, kind="manual")
    tid = created["trigger_id"]
    status, result = web.post_json(f"/api/triggers/{tid}/run", {})
    job = web.wait_job(result["job_id"])
    assert job["status"] == "done"
    run_dir = web.workspace / ".panguverse" / "executions" / job["run_id"]

    assert web.delete(f"/api/triggers/{tid}") == 204
    assert not _trigger_file(web, tid).exists()
    assert run_dir.is_dir()
    status, body = web.get_json(f"/api/studio/{wid}/triggers")
    assert status == 200 and body["triggers"] == []
