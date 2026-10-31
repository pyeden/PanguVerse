"""任务定义 CRUD + 从运行历史重跑（``tasks/web-tasks-crud-spec.md`` A17–A24）。

全部通过真实 HTTP 服务（端口 0）+ 临时工作区验证，覆盖：
落盘权限、列表/详情打码、PATCH 键级合并、DELETE 不动 runs/、任务运行、
运行配置还原、重跑（含 demo mock 端口重建）与非法输入边界。
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

from panguverse.web.api import WebAPI
from panguverse.web.server import PanguverseHTTPServer

# 测试进程自己也必须直连回环地址（macOS 系统代理会劫持 127.0.0.1 请求）
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, base: str, workspace: Path) -> None:
        self.base = base
        self.workspace = workspace

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

    def json(self, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        status, _headers, body = self.request(method, path, payload)
        return status, json.loads(body.decode("utf-8") or "null")

    def wait_job(self, job_id: str, *, timeout: float = 120.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.json("GET", f"/api/jobs/{job_id}")
            assert status == 200, data
            if data["status"] in ("done", "failed"):
                return data
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


@pytest.fixture()
def web(tmp_path):
    workspace = tmp_path / "ws"
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


def assert_error(body: Any, code: str | None = None) -> None:
    assert isinstance(body, dict), body
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}, error
    assert isinstance(error["message"], str) and error["message"]
    if code is not None:
        assert error["code"] == code, error


def create_task(web: Client, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "订单闭环冒烟",
        "mode": "demo",
        "scenario": "",
        "intent": "",
        "env": {"API_USERNAME": "demo", "API_PASSWORD": "demo123"},
        "regression": False,
        "no_llm": False,
    }
    payload.update(overrides)
    status, task = web.json("POST", "/api/tasks", payload)
    assert status == 201, task
    return task


def task_file(web: Client, task_id: str) -> Path:
    return web.workspace / "tasks" / f"{task_id}.json"


# --------------------------------------------------------------------------- #
# A17：落盘
# --------------------------------------------------------------------------- #
def test_a17_create_task_writes_private_file(web: Client) -> None:
    task = create_task(web)
    assert task["task_id"].startswith("t-")
    assert task["name"] == "订单闭环冒烟"
    assert task["created_at"] == task["updated_at"]

    path = task_file(web, task["task_id"])
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((web.workspace / "tasks").stat().st_mode) == 0o700

    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["task_id"] == task["task_id"]
    assert disk["env"]["API_PASSWORD"] == "demo123"  # 磁盘保持明文（与 last_env.json 同策略）
    assert task["env"]["API_PASSWORD"] == "***"  # HTTP 出口打码

    # 目录里只允许 id 形如 t-xxxx 的任务文件；临时文件不会残留
    assert {p.name for p in (web.workspace / "tasks").iterdir()} == {path.name}


def test_a17_task_id_is_server_generated(web: Client) -> None:
    task = create_task(web, task_id="t-deadbeef", created_at=1.0, last_status="pwned")
    assert task["task_id"] != "t-deadbeef"
    assert task["created_at"] != 1.0
    assert task["last_status"] == ""
    # 客户端指定的 id 不得被当成路径使用
    assert not (web.workspace / "tasks" / "t-deadbeef.json").exists()


# --------------------------------------------------------------------------- #
# A18：列表 / 详情
# --------------------------------------------------------------------------- #
def test_a18_list_and_detail_masking(web: Client) -> None:
    first = create_task(web, name="任务一")
    second = create_task(web, name="任务二", env={"API_PASSWORD": "demo123", "TOKEN": "s3cret"})
    # 更新第一个，让它排到列表最前
    status, _ = web.json("PATCH", f"/api/tasks/{first['task_id']}", {"name": "任务一改"})
    assert status == 200

    status, listing = web.json("GET", "/api/tasks")
    assert status == 200
    assert [item["task_id"] for item in listing["tasks"]] == [first["task_id"], second["task_id"]]
    for item in listing["tasks"]:
        assert set(item) == {
            "task_id",
            "name",
            "mode",
            "scenario",
            "intent",
            "updated_at",
            "last_run_id",
            "last_status",
            "env_keys",
        }
        assert "env" not in item
        assert isinstance(item["env_keys"], list)
    assert listing["tasks"][0]["env_keys"] == ["API_PASSWORD", "API_USERNAME"]
    assert listing["tasks"][1]["env_keys"] == ["API_PASSWORD", "TOKEN"]
    assert "demo123" not in json.dumps(listing, ensure_ascii=False)
    assert "s3cret" not in json.dumps(listing, ensure_ascii=False)

    status, detail = web.json("GET", f"/api/tasks/{second['task_id']}")
    assert status == 200
    assert detail["env"]["API_PASSWORD"] == "***"
    assert detail["env"]["TOKEN"] == "***"
    assert "demo123" not in json.dumps(detail, ensure_ascii=False)
    assert "s3cret" not in json.dumps(detail, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# A19：PATCH 部分更新
# --------------------------------------------------------------------------- #
def test_a19_patch_is_partial(web: Client) -> None:
    task = create_task(web, name="原名", regression=False, no_llm=False)
    status, updated = web.json("PATCH", f"/api/tasks/{task['task_id']}", {"name": "新名"})
    assert status == 200
    assert updated["name"] == "新名"
    assert updated["mode"] == task["mode"]
    assert updated["env"] == task["env"]
    assert updated["created_at"] == task["created_at"]
    assert updated["updated_at"] >= task["updated_at"]

    disk = json.loads(task_file(web, task["task_id"]).read_text(encoding="utf-8"))
    assert disk["name"] == "新名"
    assert disk["env"] == {"API_USERNAME": "demo", "API_PASSWORD": "demo123"}


def test_a19_env_merge_rules(web: Client) -> None:
    task = create_task(
        web,
        env={
            "BASE_URL": "http://127.0.0.1:8899",
            "API_USERNAME": "demo",
            "API_PASSWORD": "demo123",
            "KEEP_ME": "old",
        },
    )
    status, updated = web.json(
        "PATCH",
        f"/api/tasks/{task['task_id']}",
        {
            "env": {
                "API_PASSWORD": "***",  # 打码占位 → 沿用旧值
                "KEEP_ME": "",  # 留空 → 沿用旧值
                "BASE_URL": None,  # 显式删除
                "NEW_KEY": "new-value",  # 覆盖/新增
                "RETRIES": 3,
                "DEBUG": True,
            }
        },
    )
    assert status == 200, updated
    assert updated["env"]["API_PASSWORD"] == "***"  # 出口打码
    disk = json.loads(task_file(web, task["task_id"]).read_text(encoding="utf-8"))
    assert disk["env"] == {
        "API_USERNAME": "demo",
        "API_PASSWORD": "demo123",
        "KEEP_ME": "old",
        "NEW_KEY": "new-value",
        "RETRIES": 3,
        "DEBUG": True,
    }
    assert "BASE_URL" not in disk["env"]


def test_a19_patch_env_still_validates(web: Client) -> None:
    task = create_task(web)
    status, body = web.json(
        "PATCH", f"/api/tasks/{task['task_id']}", {"env": {"BROKEN": {"nested": 1}}}
    )
    assert status == 400
    assert_error(body, "bad_request")

    status, body = web.json("PATCH", f"/api/tasks/{task['task_id']}", {"name": "   "})
    assert status == 400
    assert_error(body, "bad_request")

    status, body = web.json("PATCH", f"/api/tasks/{task['task_id']}", {"mode": "nope"})
    assert status == 400
    assert_error(body, "bad_request")


# --------------------------------------------------------------------------- #
# A20：DELETE 只删任务
# --------------------------------------------------------------------------- #
def test_a20_delete_task_keeps_runs(web: Client) -> None:
    task = create_task(web)
    runs_dir = web.workspace / "runs"
    for run_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        directory = runs_dir / run_id
        directory.mkdir(parents=True)
        (directory / "report.json").write_text("{}", encoding="utf-8")
    before = sorted(p.name for p in runs_dir.iterdir())

    status, _headers, body = web.request("DELETE", f"/api/tasks/{task['task_id']}")
    assert status == 204
    assert body == b""
    assert not task_file(web, task["task_id"]).exists()
    assert sorted(p.name for p in runs_dir.iterdir()) == before

    status, body = web.json("DELETE", f"/api/tasks/{task['task_id']}")
    assert status == 404
    assert_error(body, "not_found")


# --------------------------------------------------------------------------- #
# A21：运行任务
# --------------------------------------------------------------------------- #
def test_a21_run_task_uses_saved_config(web: Client) -> None:
    task = create_task(web, env={"API_USERNAME": "demo", "API_PASSWORD": "demo123"})
    status, body = web.json("POST", f"/api/tasks/{task['task_id']}/run", {})
    assert status == 202, body
    assert set(body) == {"job_id", "status"}
    job = web.wait_job(body["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["total"] == 7
    assert job["summary"]["passed"] == 7
    assert "demo" in json.dumps(job, ensure_ascii=False) or job["conclusion"]["success"] is True

    status, detail = web.json("GET", f"/api/tasks/{task['task_id']}")
    assert status == 200
    assert detail["last_run_id"] == job["run_id"]
    assert detail["last_status"] == "done"
    assert detail["env"] == {"API_USERNAME": "demo", "API_PASSWORD": "***"}
    disk = json.loads(task_file(web, task["task_id"]).read_text(encoding="utf-8"))
    assert disk["env"] == {"API_USERNAME": "demo", "API_PASSWORD": "demo123"}  # 运行不改定义


def test_a21_run_task_env_override_is_not_persisted(web: Client) -> None:
    task = create_task(web, env={"API_USERNAME": "demo"})
    status, body = web.json(
        "POST", f"/api/tasks/{task['task_id']}/run", {"env": {"ONLY_FOR_THIS_RUN": "x"}}
    )
    assert status == 202, body
    job = web.wait_job(body["job_id"])
    assert job["status"] == "done", job
    disk = json.loads(task_file(web, task["task_id"]).read_text(encoding="utf-8"))
    assert "ONLY_FOR_THIS_RUN" not in disk["env"]


# --------------------------------------------------------------------------- #
# A22：从运行历史取配置
# --------------------------------------------------------------------------- #
def test_a22_config_for_passing_run(web: Client) -> None:
    status, body = web.json("POST", "/api/runs", {"mode": "demo"})
    assert status == 202
    job = web.wait_job(body["job_id"])
    assert job["status"] == "done"

    status, config = web.json("GET", f"/api/runs/{job['run_id']}/config")
    assert status == 200
    assert set(config) == {
        "mode",
        "scenario",
        "intent",
        "env",
        "regression",
        "no_llm",
        "plan_source",
        "error",
    }
    assert config["mode"] == "demo"
    assert config["plan_source"] == "spec"
    assert config["error"] is None
    assert config["env"]["API_PASSWORD"] == "***"  # 出口打码
    assert "demo123" not in json.dumps(config, ensure_ascii=False)
    # 磁盘上的 plan.json 保留真值（重跑基线依赖它）
    disk_plan = json.loads(
        (web.workspace / "runs" / job["run_id"] / "plan.json").read_text(encoding="utf-8")
    )
    assert disk_plan["env"]["API_PASSWORD"] == "demo123"


def test_a22_config_reports_first_failed_step(web: Client) -> None:
    status, body = web.json(
        "POST",
        "/api/runs",
        {
            "mode": "scenario",
            "scenario": "scenarios/api_smoke.yaml",
            "env": {"BASE_URL": "http://127.0.0.1:9"},
        },
    )
    assert status == 202, body
    job = web.wait_job(body["job_id"])
    assert job["status"] == "failed", job
    run_id = job["run_id"]
    assert run_id

    status, config = web.json("GET", f"/api/runs/{run_id}/config")
    assert status == 200
    assert config["mode"] == "scenario"
    assert config["scenario"] == "scenarios/api_smoke.yaml"
    error = config["error"]
    assert isinstance(error, dict)
    assert set(error) == {"case", "step", "message", "expected", "actual"}
    assert error["message"]

    status, detail = web.json("GET", f"/api/runs/{run_id}")
    report = detail["report"]
    case = next(c for c in report["cases"] if c["status"] in ("failed", "error"))
    step = next(s for s in case["steps"] if s["status"] in ("failed", "error"))
    assert error["case"] == case["name"]
    assert error["step"] == step["name"]
    assert error["message"] == step["error"]["message"]


def test_a22_config_unknown_run(web: Client) -> None:
    status, body = web.json("GET", "/api/runs/abcdef123456/config")
    assert status == 404
    assert_error(body, "not_found")


# --------------------------------------------------------------------------- #
# A23：重跑
# --------------------------------------------------------------------------- #
def test_a23_rerun_demo_rebuilds_mock_and_keeps_env(web: Client) -> None:
    status, body = web.json("POST", "/api/runs", {"mode": "demo"})
    assert status == 202
    source = web.wait_job(body["job_id"])
    assert source["status"] == "done"

    status, body = web.json(
        "POST", f"/api/runs/{source['run_id']}/rerun", {"env": {"API_PASSWORD": "***"}}
    )
    assert status == 202, body
    rerun_job = web.wait_job(body["job_id"])
    assert rerun_job["status"] == "done", rerun_job
    assert rerun_job["run_id"] != source["run_id"]
    assert rerun_job["summary"]["total"] == 7
    assert rerun_job["summary"]["passed"] == 7

    # 同源：都来自 spec 计划
    status, detail = web.json("GET", f"/api/runs/{rerun_job['run_id']}")
    assert status == 200
    assert detail["report"]["meta"]["plan_source"] == "spec"
    assert detail["plan"]["source"] == "spec"

    # "***" 沿用磁盘真值：磁盘 plan.json 仍是 demo123
    disk_plan = json.loads(
        (web.workspace / "runs" / rerun_job["run_id"] / "plan.json").read_text(encoding="utf-8")
    )
    assert disk_plan["env"]["API_PASSWORD"] == "demo123"
    # demo 的 mock 端口随进程消失：重跑必须重新拉起（端口不同但都通）
    source_plan = json.loads(
        (web.workspace / "runs" / source["run_id"] / "plan.json").read_text(encoding="utf-8")
    )
    assert disk_plan["env"]["BASE_URL"] != source_plan["env"]["BASE_URL"]


def test_a23_rerun_save_as_task(web: Client) -> None:
    status, body = web.json("POST", "/api/runs", {"mode": "demo"})
    assert status == 202
    source = web.wait_job(body["job_id"])
    status, body = web.json(
        "POST",
        f"/api/runs/{source['run_id']}/rerun",
        {"save_as_task": True, "name": "从历史保存的任务", "env": {"API_PASSWORD": "***"}},
    )
    assert status == 202, body
    job = web.wait_job(body["job_id"])
    assert job["status"] == "done", job

    status, listing = web.json("GET", "/api/tasks")
    assert status == 200
    tasks = listing["tasks"]
    assert len(tasks) == 1
    saved = tasks[0]
    assert saved["name"] == "从历史保存的任务"
    assert saved["mode"] == "demo"
    assert saved["last_run_id"] == job["run_id"]
    assert saved["last_status"] == "done"
    disk = json.loads(task_file(web, saved["task_id"]).read_text(encoding="utf-8"))
    assert disk["env"]["API_PASSWORD"] == "demo123"  # 保存的是真值，不是 ***
    assert "BASE_URL" not in disk["env"]  # mock 端口不入库


def test_a23_rerun_rejects_unresolvable_config(web: Client) -> None:
    status, _body = web.json("GET", "/api/runs/abcdef123456/config")
    assert status == 404


# --------------------------------------------------------------------------- #
# A24：非法输入
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "x", "name": "n"},
        {"mode": "demo"},
        {"mode": "demo", "name": ""},
        {"mode": "demo", "name": "   "},
        {"mode": "demo", "name": "x" * 81},
        {"mode": "scenario", "name": "n"},
        {"mode": "scenario", "name": "n", "scenario": "../../../../etc/passwd"},
        {"mode": "scenario", "name": "n", "scenario": "scenarios/__missing__.yaml"},
        {"mode": "intent", "name": "n", "intent": "   "},
        {"mode": "demo", "name": "n", "env": [1, 2]},
        {"mode": "demo", "name": "n", "env": {"A": {"nested": 1}}},
        {"mode": "demo", "name": "n", "env": {"A": None}},
    ],
)
def test_a24_create_rejects_invalid_bodies(web: Client, payload: dict[str, Any]) -> None:
    status, body = web.json("POST", "/api/tasks", payload)
    assert status == 400, body
    assert_error(body, "bad_request")
    assert not (web.workspace / "tasks").exists() or not list((web.workspace / "tasks").iterdir())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/tasks/t-abcdef"),
        ("GET", "/api/tasks/t-0123456789ab"),
        ("PATCH", "/api/tasks/t-abcdef"),
        ("DELETE", "/api/tasks/t-abcdef"),
        ("POST", "/api/tasks/t-abcdef/run"),
    ],
)
def test_a24_unknown_task_is_404(web: Client, method: str, path: str) -> None:
    status, data = web.json(method, path, {} if method in {"PATCH", "POST"} else None)
    assert status == 404, data
    assert_error(data, "not_found")


@pytest.mark.parametrize(
    "task_id",
    ["t-zzzzzz", "t-..", "..%2Fx", "t-0123456789abc", "abc", "t-../../x"],
)
def test_a24_bad_task_id_never_touches_paths(web: Client, task_id: str) -> None:
    status, body = web.json("GET", f"/api/tasks/{task_id}")
    assert status in (400, 404), body
    if status == 400:
        assert_error(body, "bad_request")
    else:
        assert_error(body, "not_found")


def test_a24_create_does_not_accept_traversal_scenario(web: Client) -> None:
    status, body = web.json(
        "POST",
        "/api/tasks",
        {"name": "越界", "mode": "scenario", "scenario": "../../../etc/hosts"},
    )
    assert status == 400
    assert_error(body, "bad_request")
