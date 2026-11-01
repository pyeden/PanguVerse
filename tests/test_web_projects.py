"""项目 / 套件 / 批量运行（``tasks/web-refactor-spec.md`` v4.0 A50–A56）。

全部通过真实 HTTP 服务（端口 0）+ 临时工作区验证；需要「真实报告」的用例
用内置 MockAPI 起一个进程内被测量，跑真场景，绝不手写假 summary 冒充运行结果
（只有 A56 的分页矩阵用磁盘报告夹具直接铺数据，因为它验证的是筛选而不是执行）。
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

from panguverse.demo.mock_api import MockAPI
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

    def wait_batch(self, batch_id: str, *, timeout: float = 180.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.json("GET", f"/api/batches/{batch_id}")
            assert status == 200, data
            if data["status"] in ("done", "failed"):
                return data
            time.sleep(0.05)
        raise AssertionError(f"batch {batch_id} 未在 {timeout}s 内结束")


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


# --------------------------------------------------------------------------- #
# 场景 / 运行夹具
# --------------------------------------------------------------------------- #
PASS_SCENARIO = """
name: 项目冒烟
cases:
  - id: health_ok
    name: health 200
    steps:
      - id: health
        use: http_request
        name: GET /api/health
        with:
          method: GET
          url: ${env.BASE_URL}/api/health
        assert:
          status: 200
"""

FAIL_SCENARIO = """
name: 失败套件
cases:
  - id: bad_404
    name: 期望 200 实际 404
    steps:
      - id: bad
        use: http_request
        name: GET /api/does-not-exist
        with:
          method: GET
          url: ${env.BASE_URL}/api/does-not-exist
        assert:
          status: 200
"""


def write_scenario(web: Client, name: str, text: str) -> str:
    directory = web.workspace / "scenarios"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return f"scenarios/{name}"


def create_project(web: Client, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "测试项目", "base_url": "https://example.test"}
    payload.update(overrides)
    status, project = web.json("POST", "/api/projects", payload)
    assert status == 201, project
    return project


def add_suite(web: Client, project_id: str, scenario: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "默认套件", "scenario": scenario}
    payload.update(overrides)
    status, suite = web.json("POST", f"/api/projects/{project_id}/suites", payload)
    assert status == 201, suite
    return suite


def run_project(web: Client, project_id: str, payload: Any = None) -> dict[str, Any]:
    status, started = web.json("POST", f"/api/projects/{project_id}/run", payload or {})
    assert status == 202, started
    return started


def project_file(web: Client, project_id: str) -> Path:
    return web.workspace / ".panguverse" / "projects" / f"{project_id}.json"


def disk_project(web: Client, project_id: str) -> dict[str, Any]:
    return json.loads(project_file(web, project_id).read_text(encoding="utf-8"))


def disk_report(web: Client, run_id: str) -> dict[str, Any]:
    path = web.workspace / "runs" / run_id / "report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(
    web: Client,
    run_id: str,
    *,
    project_id: str | None = None,
    suite_id: str | None = None,
    passed: int = 1,
    total: int = 1,
    errored: int = 0,
    started_at: float = 0.0,
) -> None:
    """只给 A56 分页矩阵铺数据用：写一份形状合法的磁盘报告。"""
    directory = web.workspace / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    failed = 0 if passed == total else total - passed
    summary = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "skipped": 0,
        "healed_cases": 0,
        "steps": total,
        "http_requests": total,
        "duration_ms": 10.0,
        "success": passed == total and errored == 0,
    }
    meta: dict[str, Any] = {}
    if project_id:
        meta["project_id"] = project_id
    if suite_id:
        meta["suite_id"] = suite_id
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": started_at + 1.0,
        "duration_ms": 1000.0,
        "summary": summary,
        "conclusion": {
            "verdict": "通过" if summary["success"] else "存在测试失败",
            "success": summary["success"],
            "exit_code": 0 if summary["success"] else 1,
            "failed_cases": [],
        },
        "meta": meta,
    }
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")


# --------------------------------------------------------------------------- #
# A50：项目 CRUD + 落盘
# --------------------------------------------------------------------------- #
def test_a50_project_crud_and_private_storage(web: Client) -> None:
    status, project = web.json(
        "POST",
        "/api/projects",
        {
            "name": "coding-log-web",
            "base_url": "https://seres-ai.com",
            "env": {"BASE_URL": "https://seres-ai.com", "API_PASSWORD": "s3cret"},
            "project_id": "p-deadbeef",  # 客户端指定必须被忽略
        },
    )
    assert status == 201, project
    project_id = project["project_id"]
    assert project_id != "p-deadbeef"
    assert project_id.startswith("p-") and 8 <= len(project_id) <= 14
    assert project["env"]["API_PASSWORD"] == "***"
    assert project["name"] == "coding-log-web"

    # 落盘：目录 0700、文件 0600、客户端不能指定 id
    path = project_file(web, project_id)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    on_disk = disk_project(web, project_id)
    assert on_disk["project_id"] == project_id
    assert on_disk["env"]["API_PASSWORD"] == "s3cret"  # 磁盘保留真值，出口才打码

    # 列表 / 详情
    status, listing = web.json("GET", "/api/projects")
    assert status == 200 and len(listing["projects"]) == 1
    item = listing["projects"][0]
    assert set(item) == {
        "project_id",
        "name",
        "base_url",
        "suite_count",
        "last_run",
        "pass_rate",
    }
    assert item["suite_count"] == 0 and item["last_run"] is None and item["pass_rate"] is None

    status, detail = web.json("GET", f"/api/projects/{project_id}")
    assert status == 200
    assert detail["env"]["API_PASSWORD"] == "***"
    assert detail["suites"] == []

    # PATCH 部分更新
    status, patched = web.json("PATCH", f"/api/projects/{project_id}", {"name": "改名后"})
    assert status == 200 and patched["name"] == "改名后"
    assert patched["base_url"] == "https://seres-ai.com"

    # 删项目不影响 runs
    write_report(
        web,
        "aaaaaaaaaaaa",
        project_id=project_id,
        suite_id="s-111111",
        started_at=100.0,
    )
    status, headers, body = web.request("DELETE", f"/api/projects/{project_id}")
    assert status == 204
    assert body == b""
    assert "Content-Type" not in headers and "Content-Length" not in headers
    assert not path.exists()
    assert (web.workspace / "runs" / "aaaaaaaaaaaa" / "report.json").is_file()
    status, data = web.json("GET", "/api/runs/query?project=" + project_id)
    assert status == 200 and data["total"] == 1

    status, missing = web.json("GET", f"/api/projects/{project_id}")
    assert status == 404
    assert_error(missing, "not_found")
    status, missing = web.json("DELETE", f"/api/projects/{project_id}")
    assert status == 404


# --------------------------------------------------------------------------- #
# A51：套件绑定
# --------------------------------------------------------------------------- #
def test_a51_suite_binding(web: Client) -> None:
    project = create_project(web)
    project_id = project["project_id"]
    write_scenario(web, "pass.yaml", PASS_SCENARIO)

    first = add_suite(
        web,
        project_id,
        "scenarios/pass.yaml",
        name="站点只读巡检",
        env={"WEATHER_DATE": "2026-11-02"},
        tags=["smoke"],
    )
    second = add_suite(web, project_id, "scenarios/pass.yaml", name="站点只读巡检")
    assert first["suite_id"].startswith("s-")
    assert first["suite_id"] != second["suite_id"]  # 同名允许，id 区分
    assert first["tags"] == ["smoke"]

    # scenario 越界 / 不存在 / 客户端指定 suite id
    for bad in ("../outside.yaml", "/etc/passwd", "scenarios/nope.yaml", ""):
        status, body = web.json(
            "POST", f"/api/projects/{project_id}/suites", {"name": "x", "scenario": bad}
        )
        assert status == 400, (bad, body)
        assert_error(body, "bad_request")

    status, body = web.json(
        "POST",
        f"/api/projects/{project_id}/suites",
        {"name": "x", "scenario": "scenarios/pass.yaml", "suite_id": "s-deadbeef"},
    )
    assert status == 201, body
    assert body["suite_id"] != "s-deadbeef"

    # PATCH：name / scenario / env 三态 / tags
    status, patched = web.json(
        "PATCH",
        f"/api/projects/{project_id}/suites/{first['suite_id']}",
        {
            "name": "改过的套件",
            "scenario": "scenarios/pass.yaml",
            "env": {"WEATHER_DATE": "2026-11-03", "NEW_KEY": "v"},
            "tags": ["regression", "regression"],
        },
    )
    assert status == 200, patched
    assert patched["name"] == "改过的套件"
    assert patched["tags"] == ["regression"]
    assert disk_project(web, project_id)["suites"][0]["env"]["NEW_KEY"] == "v"

    status, body = web.json(
        "PATCH",
        f"/api/projects/{project_id}/suites/{first['suite_id']}",
        {"scenario": "../../etc/passwd"},
    )
    assert status == 400
    assert_error(body)

    status, _headers, raw = web.request(
        "DELETE", f"/api/projects/{project_id}/suites/{second['suite_id']}"
    )
    assert status == 204 and raw == b""
    status, detail = web.json("GET", f"/api/projects/{project_id}")
    ids = [suite["suite_id"] for suite in detail["suites"]]
    assert second["suite_id"] not in ids and first["suite_id"] in ids

    status, body = web.json(
        "DELETE", f"/api/projects/{project_id}/suites/{second['suite_id']}"
    )
    assert status == 404
    assert_error(body, "not_found")
    status, body = web.json("GET", "/api/projects/p-zzzzzz")
    assert status == 400
    assert_error(body, "bad_request")


# --------------------------------------------------------------------------- #
# A52：env 三态 + suite.env > project.env
# --------------------------------------------------------------------------- #
def test_a52_env_three_state_and_precedence(web: Client) -> None:
    project = create_project(
        web,
        env={"A": "1", "B": "2", "SECRET_TOKEN": "keep-me", "BASE_URL": "http://127.0.0.1:1"},
    )
    project_id = project["project_id"]
    write_scenario(web, "pass.yaml", PASS_SCENARIO)

    # "***"/空 = 沿用、null = 删键
    status, _ = web.json(
        "PATCH",
        f"/api/projects/{project_id}",
        {"env": {"A": "***", "B": None, "C": "", "D": "4"}},
    )
    assert status == 200
    on_disk = disk_project(web, project_id)["env"]
    assert on_disk["A"] == "1" and "B" not in on_disk and "C" not in on_disk
    assert on_disk["D"] == "4" and on_disk["SECRET_TOKEN"] == "keep-me"

    status, _ = web.json(
        "PATCH", f"/api/projects/{project_id}", {"env": {"SECRET_TOKEN": "***"}}
    )
    assert status == 200
    assert disk_project(web, project_id)["env"]["SECRET_TOKEN"] == "keep-me"
    status, detail = web.json("GET", f"/api/projects/{project_id}")
    assert detail["env"]["SECRET_TOKEN"] == "***"

    # suite.env 覆盖 project.env；两者都覆盖请求 env
    with MockAPI(flaky_failures=0) as mock:
        add_suite(
            web,
            project_id,
            "scenarios/pass.yaml",
            name="套件覆盖",
            env={"BASE_URL": mock.base_url, "X": "suite"},
        )
        add_suite(web, project_id, "scenarios/pass.yaml", name="项目兜底", env={})
        status, _ = web.json(
            "PATCH", f"/api/projects/{project_id}", {"env": {"BASE_URL": mock.base_url}}
        )
        assert status == 200

        started = run_project(web, project_id, {"env": {"BASE_URL": "http://127.0.0.1:1"}})
        batch = web.wait_batch(started["batch_id"])
        assert batch["status"] == "done", batch
        # 两个套件都跑在 mock 上：suite.env 与 project.env 都压过了请求覆盖
        assert batch["aggregate"] == {
            "passed": 2,
            "total": 2,
            "suites_passed": 2,
            "suites_failed": 0,
            "duration_ms": batch["aggregate"]["duration_ms"],
        }
        for entry in batch["suites"]:
            report = disk_report(web, entry["run_id"])
            assert report["env"]["BASE_URL"] == mock.base_url, entry

        # 单个套件覆盖 project.env：project.env 指到死端口，suite.env 仍是 mock
        status, _ = web.json(
            "PATCH", f"/api/projects/{project_id}", {"env": {"BASE_URL": "http://127.0.0.1:1"}}
        )
        first_suite = batch["suites"][0]["suite_id"]
        started = run_project(web, project_id, {"suite_ids": [first_suite]})
        batch = web.wait_batch(started["batch_id"])
        assert batch["aggregate"]["passed"] == 1, batch
        assert disk_report(web, batch["suites"][0]["run_id"])["env"]["BASE_URL"] == mock.base_url


# --------------------------------------------------------------------------- #
# A53：运行归属（新报告带 meta，旧报告=未归类）
# --------------------------------------------------------------------------- #
def test_a53_run_attribution_and_legacy_unclassified(web: Client) -> None:
    write_scenario(web, "pass.yaml", PASS_SCENARIO)
    with MockAPI(flaky_failures=0) as mock:
        project = create_project(web, env={"BASE_URL": mock.base_url})
        project_id = project["project_id"]
        suite = add_suite(web, project_id, "scenarios/pass.yaml", name="归属套件")
        started = run_project(web, project_id)
        batch = web.wait_batch(started["batch_id"])
        assert batch["status"] == "done", batch
        entry = batch["suites"][0]
        assert entry["suite_id"] == suite["suite_id"] and entry["passed"] == 1

        report = disk_report(web, entry["run_id"])
        assert report["meta"]["project_id"] == project_id
        assert report["meta"]["suite_id"] == suite["suite_id"]

        status, data = web.json("GET", f"/api/runs/query?project={project_id}")
        assert status == 200 and data["total"] == 1
        assert data["items"][0]["project_id"] == project_id
        assert data["items"][0]["suite_id"] == suite["suite_id"]

        # 旧报告（无 meta）：正常归类为「未归类」，不报错
        write_report(web, "bbbbbbbbbbbb", started_at=10.0)
        status, data = web.json("GET", "/api/runs/query?project=unclassified")
        assert status == 200
        assert [item["run_id"] for item in data["items"]] == ["bbbbbbbbbbbb"]
        assert data["items"][0]["project_id"] is None
        status, data = web.json("GET", f"/api/runs/query?project={project_id}")
        assert data["total"] == 1

        # 项目删除后运行仍在（归属来自报告，不依赖项目文件）
        status, _headers, _body = web.request("DELETE", f"/api/projects/{project_id}")
        assert status == 204
        status, data = web.json("GET", f"/api/runs/query?project={project_id}")
        assert data["total"] == 1


# --------------------------------------------------------------------------- #
# A54：概览汇总与真实报告一致
# --------------------------------------------------------------------------- #
def test_a54_project_summary_matches_reports(web: Client) -> None:
    write_scenario(web, "pass.yaml", PASS_SCENARIO)
    write_scenario(web, "fail.yaml", FAIL_SCENARIO)
    with MockAPI(flaky_failures=0) as mock:
        project = create_project(web, env={"BASE_URL": mock.base_url})
        project_id = project["project_id"]
        add_suite(web, project_id, "scenarios/pass.yaml", name="通过")
        add_suite(web, project_id, "scenarios/fail.yaml", name="失败")
        started = run_project(web, project_id)
        batch = web.wait_batch(started["batch_id"])
        assert batch["status"] == "done"

        reports = {
            entry["run_id"]: disk_report(web, entry["run_id"]) for entry in batch["suites"]
        }
        expected_passed = sum(report["summary"]["passed"] for report in reports.values())
        expected_total = sum(report["summary"]["total"] for report in reports.values())

        status, summary = web.json("GET", f"/api/projects/{project_id}/summary?limit=5")
        assert status == 200
        assert summary["pass_rate"] == {"passed": expected_passed, "total": expected_total}
        assert {item["run_id"] for item in summary["recent_runs"]} == {
            entry["run_id"] for entry in batch["suites"]
        }
        # failure_top 来自报告里的 failure_kind（断言失败 → assertion），最多 3 条
        assert summary["failure_top"] == [
            {"kind": "assertion", "count": 1, "cases": ["bad_404"]}
        ]

        # limit 只裁剪 recent_runs，不改变全量口径
        status, limited = web.json("GET", f"/api/projects/{project_id}/summary?limit=1")
        assert len(limited["recent_runs"]) == 1
        assert limited["pass_rate"] == summary["pass_rate"]
        assert limited["failure_top"] == summary["failure_top"]

        # 列表里的 last_run / pass_rate 与概览一致
        status, listing = web.json("GET", "/api/projects")
        item = next(item for item in listing["projects"] if item["project_id"] == project_id)
        assert item["pass_rate"] == summary["pass_rate"]
        assert item["suite_count"] == 2
        assert item["last_run"]["run_id"] in reports
        assert item["last_run"]["passed"] + item["last_run"]["total"] >= 0

        # 不存在的项目 → 404
        status, body = web.json("GET", "/api/projects/p-abcdef/summary")
        assert status == 404
        assert_error(body, "not_found")


# --------------------------------------------------------------------------- #
# A55：批量运行（串行、每套件一 run、aggregate 对账、单套件失败不中断）
# --------------------------------------------------------------------------- #
def test_a55_batch_serial_aggregate_reconciles(web: Client) -> None:
    write_scenario(web, "pass.yaml", PASS_SCENARIO)
    write_scenario(web, "fail.yaml", FAIL_SCENARIO)
    with MockAPI(flaky_failures=0) as mock:
        project = create_project(web, env={"BASE_URL": mock.base_url})
        project_id = project["project_id"]
        add_suite(web, project_id, "scenarios/pass.yaml", name="通过")
        add_suite(web, project_id, "scenarios/fail.yaml", name="失败")

        status, started = web.json("POST", f"/api/projects/{project_id}/run", {})
        assert status == 202, started
        assert len(started["jobs"]) == 2
        job_ids = [item["job_id"] for item in started["jobs"]]
        assert len(set(job_ids)) == 2
        assert {item["suite_id"] for item in started["jobs"]} == {
            suite["suite_id"]
            for suite in web.json("GET", f"/api/projects/{project_id}")[1]["suites"]
        }

        batch = web.wait_batch(started["batch_id"])
        assert batch["status"] == "done", batch
        assert len(batch["suites"]) == 2
        assert len({entry["run_id"] for entry in batch["suites"]}) == 2  # 每套件一个 run
        for job_id in job_ids:  # 每个套件一个 job，且能在 /api/jobs 查到
            job = web.wait_job(job_id)
            assert job["status"] == "done"

        # 串行：后一个 run 开始时间不早于前一个 run 结束时间
        ordered = sorted(
            (disk_report(web, entry["run_id"]) for entry in batch["suites"]),
            key=lambda report: report["started_at"],
        )
        assert ordered[0]["finished_at"] <= ordered[1]["started_at"]

        # aggregate 与各 run 真实数字逐项对账
        reports = [disk_report(web, entry["run_id"]) for entry in batch["suites"]]
        assert batch["aggregate"] == {
            "passed": sum(report["summary"]["passed"] for report in reports),
            "total": sum(report["summary"]["total"] for report in reports),
            "suites_passed": 1,
            "suites_failed": 1,
            "duration_ms": round(sum(report["summary"]["duration_ms"] for report in reports)),
        }


def test_a55_single_suite_failure_does_not_stop_batch(web: Client) -> None:
    write_scenario(web, "pass.yaml", PASS_SCENARIO)
    write_scenario(web, "broken.yaml", PASS_SCENARIO)
    with MockAPI(flaky_failures=0) as mock:
        project = create_project(web, env={"BASE_URL": mock.base_url})
        project_id = project["project_id"]
        add_suite(web, project_id, "scenarios/pass.yaml", name="前")
        add_suite(web, project_id, "scenarios/broken.yaml", name="坏")
        add_suite(web, project_id, "scenarios/pass.yaml", name="后")
        # 绑定后才把文件写坏：批次里该套件失败，但不能中断后面的套件
        (web.workspace / "scenarios" / "broken.yaml").write_text(
            "cases: [", encoding="utf-8"
        )

        started = run_project(web, project_id)
        batch = web.wait_batch(started["batch_id"])
        assert batch["status"] == "failed"
        assert [entry["status"] for entry in batch["suites"]] == ["done", "failed", "done"]
        assert batch["suites"][1]["error"]
        assert batch["suites"][1]["run_id"] == ""
        assert batch["aggregate"]["passed"] == 2
        assert batch["aggregate"]["total"] == 2
        assert batch["aggregate"]["suites_passed"] == 2
        assert batch["aggregate"]["suites_failed"] == 1


# --------------------------------------------------------------------------- #
# A56：历史筛选分页
# --------------------------------------------------------------------------- #
def test_a56_query_filters_and_pagination(web: Client) -> None:
    project = create_project(web)
    project_id = project["project_id"]
    suite_a = "s-aaaaaa"
    suite_b = "s-bbbbbb"

    runs: list[tuple[str, str | None, str | None, float]] = [
        # run_id, project, suite, started_at
        ("000000000001", project_id, suite_a, 100.0),
        ("000000000002", project_id, suite_a, 200.0),
        ("000000000003", project_id, suite_a, 300.0),
        ("000000000004", project_id, suite_b, 400.0),
        ("000000000005", project_id, suite_b, 500.0),
        ("000000000006", project_id, suite_a, 600.0),
        ("000000000007", None, None, 700.0),
        ("000000000008", None, None, 800.0),
    ]
    for run_id, pid, sid, started in runs:
        if run_id == "000000000003":
            write_report(web, run_id, project_id=pid, suite_id=sid, passed=0, total=1, started_at=started)
        elif run_id == "000000000006":
            write_report(web, run_id, project_id=pid, suite_id=sid, passed=0, total=0, errored=1, started_at=started)
        else:
            write_report(web, run_id, project_id=pid, suite_id=sid, started_at=started)

    status, data = web.json("GET", f"/api/runs/query?project={project_id}")
    assert status == 200 and data["total"] == 6
    assert len(data["items"]) == 6

    # 分页不重不漏
    seen: list[str] = []
    for offset in (0, 2, 4):
        status, page = web.json("GET", f"/api/runs/query?project={project_id}&offset={offset}&limit=2")
        assert status == 200 and page["total"] == 6
        seen.extend(item["run_id"] for item in page["items"])
    assert len(seen) == 6 == len(set(seen))
    assert set(seen) == {run_id for run_id, pid, _sid, _at in runs if pid}

    # suite / status 组合筛选
    status, data = web.json("GET", f"/api/runs/query?project={project_id}&suite={suite_b}")
    assert data["total"] == 2
    status, data = web.json("GET", "/api/runs/query?status=failed")
    assert data["total"] == 1 and data["items"][0]["run_id"] == "000000000003"
    status, data = web.json("GET", "/api/runs/query?status=errored")
    assert data["total"] == 1 and data["items"][0]["run_id"] == "000000000006"
    status, data = web.json("GET", "/api/runs/query?status=passed")
    assert data["total"] == 6
    status, data = web.json("GET", "/api/runs/query?status=done")
    assert data["total"] == 8  # done = 已跑完的任意结论

    # 未归类
    status, data = web.json("GET", "/api/runs/query?project=unclassified")
    assert data["total"] == 2

    # 非法参数
    for path in (
        "/api/runs/query?status=weird",
        "/api/runs/query?offset=-1",
        "/api/runs/query?limit=abc",
        "/api/runs/query?project=not-a-project",
        "/api/runs/query?suite=xyz",
    ):
        status, body = web.json("GET", path)
        assert status == 400, (path, body)
        assert_error(body, "bad_request")

    # 兼容：/api/runs 仍是数组（A17–A46 不变）
    status, legacy = web.json("GET", "/api/runs")
    assert status == 200 and isinstance(legacy, list) and len(legacy) == 8

    # 未知 batch → 404
    status, body = web.json("GET", "/api/batches/deadbeefcafe")
    assert status == 404
    assert_error(body, "not_found")


# --------------------------------------------------------------------------- #
# A52 补充：web 层默认值（FS_ROOT）必须是最低优先级
# --------------------------------------------------------------------------- #
def test_a52_fs_root_default_is_lowest_priority(web: Client) -> None:
    write_scenario(web, "pass.yaml", PASS_SCENARIO)
    default_root = str((web.workspace / ".panguverse" / "fs-demo").resolve())
    with MockAPI(flaky_failures=0) as mock:
        # (a) suite.env 声明 FS_ROOT：请求里的覆盖不得盖掉它
        suite_project = create_project(web, env={"BASE_URL": mock.base_url})
        add_suite(
            web,
            suite_project["project_id"],
            "scenarios/pass.yaml",
            env={"FS_ROOT": "/tmp/pv-suite-root"},
        )
        started = run_project(
            web, suite_project["project_id"], {"env": {"FS_ROOT": "/tmp/pv-request-root"}}
        )
        batch = web.wait_batch(started["batch_id"])
        assert batch["aggregate"]["passed"] == 1, batch
        suite_env = disk_report(web, batch["suites"][0]["run_id"])["env"]
        assert suite_env["FS_ROOT"] == "/tmp/pv-suite-root", suite_env

        # (b) suite 未声明、project.env 声明 → 取 project 的值
        project_root = create_project(
            web, env={"BASE_URL": mock.base_url, "FS_ROOT": "/tmp/pv-project-root"}
        )
        add_suite(web, project_root["project_id"], "scenarios/pass.yaml")
        started = run_project(web, project_root["project_id"])
        batch = web.wait_batch(started["batch_id"])
        assert batch["aggregate"]["passed"] == 1, batch
        project_env = disk_report(web, batch["suites"][0]["run_id"])["env"]
        assert project_env["FS_ROOT"] == "/tmp/pv-project-root", project_env

        # (c) 三者都没给 → 才用 web 默认（workspace 内 fs-demo）
        bare_project = create_project(web, env={"BASE_URL": mock.base_url})
        add_suite(web, bare_project["project_id"], "scenarios/pass.yaml")
        started = run_project(web, bare_project["project_id"], {"env": {"BASE_URL": mock.base_url}})
        batch = web.wait_batch(started["batch_id"])
        assert batch["aggregate"]["passed"] == 1, batch
        bare_env = disk_report(web, batch["suites"][0]["run_id"])["env"]
        assert bare_env["FS_ROOT"] == default_root, bare_env
        assert bare_env["FS_ROOT"] != "/tmp/pv-suite-root"
