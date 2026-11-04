"""调度/触发契约测试（v8.0 §1，验收 S1–S8）。

全部走真实 HTTP + 临时工作区；调度触发用**可注入时钟**的 ``Scheduler.tick``
（另有一条手工真实等待的验证，见实现报告），避免测试套件固定等待 1 分钟。
"""

from __future__ import annotations

import contextlib
import json
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from panguverse.web import schedules
from panguverse.web.api import WebAPI
from panguverse.web.batches import Batch
from panguverse.web.server import PanguverseHTTPServer

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class WebClient:
    def __init__(self, base: str, api: WebAPI) -> None:
        self.base = base
        self.api = api
        self.workspace = api.root

    def request(self, method: str, path: str, payload: Any = None) -> tuple[int, dict[str, str], bytes]:
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
def web(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 每个测试一个极小的真实场景：set_var 纯原子，毫秒级完成
    (workspace / "noop.yaml").write_text(
        "name: sched-noop\n"
        "cases:\n"
        "  - id: c1\n"
        "    name: noop\n"
        "    steps:\n"
        "      - id: s1\n"
        "        use: set_var\n"
        "        with: {name: x, value: '1'}\n",
        encoding="utf-8",
    )
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


def _create_project(web: WebClient) -> tuple[str, str]:
    status, project = web.post_json("/api/projects", {"name": "调度项目", "env": {}})
    assert status == 201, project
    status, suite = web.post_json(
        f"/api/projects/{project['project_id']}/suites",
        {"name": "noop", "scenario": "noop.yaml"},
    )
    assert status == 201, suite
    return project["project_id"], suite["suite_id"]


def _create(web: WebClient, project_id: str, trigger: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {
        "name": extra.pop("name", "调度"),
        "target": {"kind": "project", "project_id": project_id, "suite_ids": []},
        "trigger": trigger,
        **extra,
    }
    status, data = web.post_json("/api/schedules", payload)
    assert status == 201, data
    return data


def _schedule_file(web: WebClient, sid: str) -> Path:
    return schedules.schedules_dir(web.workspace) / f"{sid}.json"


def _rewrite_schedule(web: WebClient, sid: str, **updates: Any) -> None:
    path = _schedule_file(web, sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _events(web: WebClient, sid: str) -> list[dict[str, Any]]:
    return schedules.read_events(web.workspace, sid, limit=500)


# --------------------------------------------------------------------------- #
# S1 CRUD + 落盘权限 + 客户端 sid 被忽略
# --------------------------------------------------------------------------- #
def test_s1_crud_and_permissions(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(
        web,
        project_id,
        {"kind": "cron", "expr": "0 9 * * *"},
        sid="sc-attacker",
    )
    sid = created["sid"]
    assert sid != "sc-attacker"
    assert schedules.SID_RE.match(sid)
    assert created["next_run_at"] > time.time()

    path = _schedule_file(web, sid)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    status, detail = web.get_json(f"/api/schedules/{sid}")
    assert status == 200 and detail["name"] == "调度"

    status, updated = web.patch_json(f"/api/schedules/{sid}", {"name": "改名", "enabled": False})
    assert status == 200 and updated["name"] == "改名"
    assert updated["enabled"] is False and updated["next_run_at"] is None

    status, listing = web.get_json("/api/schedules")
    assert status == 200 and listing["schedules"] == []
    status, listing = web.get_json("/api/schedules?include_disabled=1")
    assert status == 200 and [item["sid"] for item in listing["schedules"]] == [sid]

    assert web.delete(f"/api/schedules/{sid}") == 204
    status, body = web.get_json(f"/api/schedules/{sid}")
    assert status == 404 and body["error"]["code"] == "not_found"
    assert not path.exists()


# --------------------------------------------------------------------------- #
# S2 cron 计算：0 9 * * * 连续三次
# --------------------------------------------------------------------------- #
def test_s2_cron_next_three_times(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "cron", "expr": "0 9 * * *"})
    zone = ZoneInfo("Asia/Shanghai")
    next_local = datetime.fromtimestamp(created["next_run_at"], zone)
    assert (next_local.hour, next_local.minute) == (9, 0)
    assert next_local > datetime.now(zone)

    base = datetime(2026, 11, 3, 9, 30, tzinfo=zone).timestamp()
    first = schedules.compute_next_run({"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}, base)
    second = schedules.compute_next_run({"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}, first)
    third = schedules.compute_next_run({"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}, second)
    assert datetime.fromtimestamp(first, zone).strftime("%Y-%m-%d %H:%M") == "2026-11-04 09:00"
    assert datetime.fromtimestamp(second, zone).strftime("%Y-%m-%d %H:%M") == "2026-11-05 09:00"
    assert datetime.fromtimestamp(third, zone).strftime("%Y-%m-%d %H:%M") == "2026-11-06 09:00"

    # 支持 */n / a-b / a,b，且非法表达式 400
    assert schedules.compute_next_run({"kind": "cron", "expr": "*/15 * * * *"}, 0) % 900 == 0
    status, body = web.post_json(
        "/api/schedules",
        {"name": "bad", "target": {"kind": "project", "project_id": project_id}, "trigger": {"kind": "cron", "expr": "bad"}},
    )
    assert status == 400 and body["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# S3 interval：到期 tick 真实触发（注入时钟，不等待 1 分钟）
# --------------------------------------------------------------------------- #
def test_s3_interval_triggers_schedule(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "interval", "minutes": 1})
    sid = created["sid"]
    assert created["next_run_at"] >= time.time() + 55

    _rewrite_schedule(web, sid, next_run_at=time.time() - 2)
    fired = web.api.scheduler.tick()
    assert len(fired) == 1 and fired[0]["status"] == "triggered"
    job = web.wait_job(fired[0]["job_id"])
    assert job["status"] == "done", job

    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    assert run["report"]["meta"]["trigger"] == "schedule"
    assert run["report"]["meta"]["schedule_id"] == sid
    status, batch = web.get_json(f"/api/batches/{fired[0]['batch_id']}")
    assert status == 200 and batch["status"] == "done"

    detail = schedules.read_schedule(web.workspace, sid)
    assert detail["next_run_at"] is not None and detail["next_run_at"] > time.time()
    assert [event["kind"] for event in _events(web, sid)].count("triggered") == 1


# --------------------------------------------------------------------------- #
# S4 run-now：立即产生 batch，meta.trigger=manual
# --------------------------------------------------------------------------- #
def test_s4_run_now(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "cron", "expr": "0 9 * * *"})
    sid = created["sid"]
    status, result = web.post_json(f"/api/schedules/{sid}/run-now", {})
    assert status == 202 and result["status"] == "triggered"
    assert result["batch_id"] and result["job_id"]
    job = web.wait_job(result["job_id"])
    assert job["status"] == "done", job
    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert run["report"]["meta"]["trigger"] == "manual"
    assert run["report"]["meta"]["schedule_id"] == sid


# --------------------------------------------------------------------------- #
# S5 重叠保护：上次 batch 仍 running → skipped_overlap，不产生新 batch
# --------------------------------------------------------------------------- #
def test_s5_overlap_is_skipped(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "interval", "minutes": 1})
    sid = created["sid"]

    running = Batch("deadbeef0001", project_id, [])
    running._set_running()
    web.api.batches._batches[running.batch_id] = running
    _rewrite_schedule(web, sid, last_batch_id=running.batch_id, last_status="running")

    before = {batch.batch_id for batch in web.api.batches.recent(100)}
    status, result = web.post_json(f"/api/schedules/{sid}/run-now", {})
    assert status == 202 and result["status"] == "skipped_overlap"
    assert result["job_id"] is None and result["batch_id"] == running.batch_id
    after = {batch.batch_id for batch in web.api.batches.recent(100)}
    assert after == before  # 没有新 batch
    assert "skipped_overlap" in [event["kind"] for event in _events(web, sid)]
    assert schedules.read_schedule(web.workspace, sid)["last_status"] == "skipped_overlap"


# --------------------------------------------------------------------------- #
# S6 webhook：正确 token 202 / 错误 404 / 禁用 409 / 可重置
# --------------------------------------------------------------------------- #
def test_s6_webhook_token(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "webhook"})
    sid = created["sid"]
    token = created["webhook_token"]
    assert token and len(token) >= 32
    assert created["trigger"]["token"] == token  # 展示一次（UI 读 trigger.token）
    status, detail = web.get_json(f"/api/schedules/{sid}")
    assert status == 200
    assert "token" not in detail["trigger"] and "webhook_token" not in detail
    assert detail["trigger"]["configured"] is True

    status, result = web.post_json(f"/api/hooks/{token}", {})
    assert status == 202 and result["status"] == "triggered"
    job = web.wait_job(result["job_id"])
    assert job["status"] == "done"
    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert run["report"]["meta"]["trigger"] == "webhook"

    status, body = web.post_json("/api/hooks/not-a-real-token", {})
    assert status == 404 and body["error"]["code"] == "not_found"

    status, updated = web.patch_json(f"/api/schedules/{sid}", {"enabled": False})
    assert status == 200 and updated["enabled"] is False
    status, body = web.post_json(f"/api/hooks/{token}", {})
    assert status == 409 and body["error"]["code"] == "conflict"

    status, reset = web.patch_json(
        f"/api/schedules/{sid}", {"enabled": True, "trigger": {"kind": "webhook", "reset_token": True}}
    )
    assert status == 200
    new_token = reset["webhook_token"]
    assert new_token and new_token != token
    status, _body = web.post_json(f"/api/hooks/{token}", {})
    assert status == 404
    status, result = web.post_json(f"/api/hooks/{new_token}", {})
    assert status == 202


# --------------------------------------------------------------------------- #
# S7 重启恢复：next_run_at 重算，错过的不补跑，写 missed
# --------------------------------------------------------------------------- #
def test_s7_restart_recovery(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "interval", "minutes": 1})
    sid = created["sid"]
    now = time.time()
    _rewrite_schedule(web, sid, next_run_at=now - 3600, last_run_at=None)

    restarted = schedules.Scheduler(web.workspace, web.api.jobs, web.api.batches)
    restarted.recover(now=now)

    detail = schedules.read_schedule(web.workspace, sid)
    assert detail["next_run_at"] is not None and detail["next_run_at"] > now
    kinds = [event["kind"] for event in _events(web, sid)]
    assert "missed" in kinds
    # 错过的时点不补跑
    assert web.api.batches.recent(100) == []
    assert restarted.tick(now=now) == []

    # 重启后调度仍存在并可读
    status, fetched = web.get_json(f"/api/schedules/{sid}")
    assert status == 200 and fetched["sid"] == sid


# --------------------------------------------------------------------------- #
# S8 关闭态：cron/webhook 不触发，run-now 仍允许
# --------------------------------------------------------------------------- #
def test_s8_disabled_schedule(web: WebClient) -> None:
    project_id, _ = _create_project(web)
    created = _create(web, project_id, {"kind": "cron", "expr": "* * * * *", "enabled": False})
    sid = created["sid"]
    _rewrite_schedule(web, sid, enabled=False, next_run_at=time.time() - 5)
    assert web.api.scheduler.tick() == []
    assert web.api.batches.recent(100) == []

    hook = _create(web, project_id, {"kind": "webhook"}, enabled=False)
    status, _body = web.post_json(f"/api/hooks/{hook['webhook_token']}", {})
    assert status == 409

    status, result = web.post_json(f"/api/schedules/{sid}/run-now", {})
    assert status == 202 and result["status"] == "triggered"
    job = web.wait_job(result["job_id"])
    assert job["status"] == "done"
    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert run["report"]["meta"]["trigger"] == "manual"


# --------------------------------------------------------------------------- #
# S9 异常隔离：同一 tick 内一个调度抛错，另一个仍必须被触发
# --------------------------------------------------------------------------- #
def _project_with_schedule(web: WebClient, name: str) -> tuple[str, dict[str, Any]]:
    status, project = web.post_json("/api/projects", {"name": name, "env": {}})
    assert status == 201, project
    status, suite = web.post_json(
        f"/api/projects/{project['project_id']}/suites",
        {"name": "noop", "scenario": "noop.yaml"},
    )
    assert status == 201, suite
    created = _create(
        web,
        project["project_id"],
        {"kind": "interval", "minutes": 1},
        name=name,
    )
    return project["project_id"], created


def test_s9_tick_isolates_failing_schedule(web: WebClient, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_pid, bad = _project_with_schedule(web, "坏调度")
    _good_pid, good = _project_with_schedule(web, "好调度")
    now = time.time()
    for item in (bad, good):
        _rewrite_schedule(web, item["sid"], next_run_at=now - 1.0, last_run_at=None)

    # 让坏调度的项目在磁盘上消失：trigger_schedule 会抛 ProjectStoreError（非 ScheduleError）
    assert web.delete(f"/api/projects/{bad_pid}") == 204

    real_all = schedules._all_schedules

    def ordered(root: Path, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        items = real_all(root, include_disabled=include_disabled)
        return sorted(items, key=lambda item: 0 if item["sid"] == bad["sid"] else 1)

    monkeypatch.setattr(schedules, "_all_schedules", ordered)

    fired = web.api.scheduler.tick(now=now)
    assert [item["schedule_id"] for item in fired] == [good["sid"]]
    job = web.wait_job(fired[0]["job_id"])
    assert job["status"] == "done", job

    bad_detail = schedules.read_schedule(web.workspace, bad["sid"])
    assert bad_detail["next_run_at"] is not None and bad_detail["next_run_at"] > now
    kinds = [event["kind"] for event in _events(web, bad["sid"])]
    assert "tick_failed" in kinds

    error = web.api.scheduler.last_tick_error
    assert error is not None
    assert error["sid"] == bad["sid"]
    assert error["type"] == "ProjectStoreError"
    assert isinstance(error["at"], (int, float))


# --------------------------------------------------------------------------- #
# S10 持续抛错：重试频率不超过 interval，且 next_run_at 不落后于 now
# --------------------------------------------------------------------------- #
def test_s10_failing_schedule_retry_respects_interval(web: WebClient) -> None:
    bad_pid, bad = _project_with_schedule(web, "长期坏调度")
    sid = bad["sid"]
    base = time.time()
    _rewrite_schedule(web, sid, next_run_at=base - 0.5, last_run_at=None)
    assert web.delete(f"/api/projects/{bad_pid}") == 204

    # 模拟 tick=0.2s 的 5 秒窗口；旧代码在第一次 tick 就会把异常抛出来
    last_now = base
    for index in range(26):
        last_now = base + index * 0.2
        web.api.scheduler.tick(now=last_now)

    def failed_count() -> int:
        return sum(1 for event in _events(web, sid) if event["kind"] == "tick_failed")

    assert failed_count() == 1

    detail = schedules.read_schedule(web.workspace, sid)
    assert detail["next_run_at"] is not None
    assert detail["next_run_at"] >= last_now
    assert detail["next_run_at"] == pytest.approx(base + 60.0, abs=0.01)

    # 到期前不重试；越过 interval 后才允许下一次尝试（频率不超过 interval）
    web.api.scheduler.tick(now=base + 59.9)
    assert failed_count() == 1
    web.api.scheduler.tick(now=base + 60.1)
    assert failed_count() == 2
    detail = schedules.read_schedule(web.workspace, sid)
    assert detail["next_run_at"] == pytest.approx(base + 120.1, abs=0.01)


# --------------------------------------------------------------------------- #
# S11 /api/status 暴露 last_tick_at / last_tick_error（线程存活自检）
# --------------------------------------------------------------------------- #
def test_s11_status_exposes_last_tick_error(web: WebClient) -> None:
    bad_pid, bad = _project_with_schedule(web, "状态坏调度")
    now = time.time()
    _rewrite_schedule(web, bad["sid"], next_run_at=now - 1.0, last_run_at=None)
    assert web.delete(f"/api/projects/{bad_pid}") == 204

    with contextlib.suppress(Exception):
        web.api.scheduler.tick(now=now)

    status, data = web.get_json("/api/status")
    assert status == 200, data
    assert data["last_tick_at"] == pytest.approx(now, abs=1e-6)
    error = data["last_tick_error"]
    assert error["sid"] == bad["sid"]
    assert error["type"] == "ProjectStoreError"
    assert isinstance(error["at"], (int, float))


# --------------------------------------------------------------------------- #
# S12 幂等键先落盘：创建批次前就写回 last_run_at，失败也不会在同一时点重复触发
# --------------------------------------------------------------------------- #
def test_s12_trigger_failure_records_idempotency_before_start(web: WebClient) -> None:
    bad_pid, bad = _project_with_schedule(web, "触发失败调度")
    now = time.time()
    _rewrite_schedule(web, bad["sid"], next_run_at=now - 1.0, last_run_at=None)
    assert web.delete(f"/api/projects/{bad_pid}") == 204

    assert web.api.scheduler.tick(now=now) == []

    detail = schedules.read_schedule(web.workspace, bad["sid"])
    assert detail["last_run_at"] == pytest.approx(now, abs=1e-6)
    assert detail["last_status"] == "trigger_failed"
    assert detail["next_run_at"] is not None and detail["next_run_at"] > now
