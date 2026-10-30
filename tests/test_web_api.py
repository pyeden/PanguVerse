"""Web API 契约测试（标准库起服务，全程不走系统代理）。

覆盖 ``tasks/web-app-mvp-spec.md`` §3 / §5 / §6 中后端可验证的部分：
状态、原子、包、场景、运行、任务、错误体、静态白名单、代理直连。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from panguverse.http.client import HttpClient, HttpRequest, is_loopback_url
from panguverse.web.api import WebAPI
from panguverse.web.jobs import FAILED, JobManager
from panguverse.web.server import PanguverseHTTPServer

REPO_ROOT = Path(__file__).resolve().parents[1]

# 测试进程自己也必须直连回环地址（macOS 系统代理会劫持 127.0.0.1 请求）
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class WebClient:
    def __init__(self, base: str, workspace: Path) -> None:
        self.base = base
        self.workspace = workspace

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

    def post_raw(self, path: str, raw: bytes) -> tuple[int, Any]:
        req = urllib.request.Request(self.base + path, data=raw, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with _DIRECT.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def wait_job(self, job_id: str, *, timeout: float = 120.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.get_json(f"/api/jobs/{job_id}")
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
        yield WebClient(f"http://127.0.0.1:{server.server_address[1]}", api.root)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def demo_run(web: WebClient) -> dict[str, Any]:
    """跑一次内置演示（7 个用例，含回归复用），供后续断言复用。"""
    status, data = web.post_json("/api/runs", {"mode": "demo"})
    assert status == 202, data
    assert data["status"] == "queued"
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    return job


def assert_error(body: Any, code: str | None = None) -> None:
    assert isinstance(body, dict), body
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}, error
    assert isinstance(error["message"], str) and error["message"]
    if code is not None:
        assert error["code"] == code, error


# --------------------------------------------------------------------------- #
# 只读视图
# --------------------------------------------------------------------------- #
def test_status_contract(web: WebClient) -> None:
    status, data = web.get_json("/api/status")
    assert status == 200
    for key in (
        "workspace",
        "atoms",
        "packs",
        "regression_count",
        "lessons_count",
        "llm_enabled",
        "http_backend",
        "scenarios",
        "version",
    ):
        assert key in data, key
    assert data["atoms"] >= 24
    assert data["packs"] >= 3
    assert isinstance(data["llm_enabled"], bool)
    assert data["scenarios"] >= 4
    assert data["http_backend"] in {"requests", "urllib"}
    assert "sk-" not in json.dumps(data)  # 不回显任何密钥


def test_atoms_contract_and_filters(web: WebClient) -> None:
    status, data = web.get_json("/api/atoms")
    assert status == 200 and isinstance(data, list) and data
    sample = data[0]
    for key in ("name", "description", "category", "domain", "inputs", "outputs", "required", "tags"):
        assert key in sample, key
    assert "input_ports" in sample and "output_ports" in sample

    status, api_atoms = web.get_json("/api/atoms?domain=api")
    assert status == 200 and api_atoms
    assert all(atom["domain"] == "api" for atom in api_atoms)
    assert len(api_atoms) < len(data)

    status, none = web.get_json("/api/atoms?tag=__no_such_tag__")
    assert status == 200 and none == []


def test_packs_list_and_detail(web: WebClient) -> None:
    status, packs = web.get_json("/api/packs")
    assert status == 200 and len(packs) >= 3
    pack = packs[0]
    for key in ("name", "version", "domain", "builtin", "description", "atom_count", "capabilities", "semantics"):
        assert key in pack, key
    assert isinstance(pack["builtin"], bool)

    status, detail = web.get_json("/api/packs/api_testing")
    assert status == 200
    assert detail["name"] == "api_testing"
    assert detail["atoms"] and isinstance(detail["atoms"][0], str)
    assert detail["manifest"]["name"] == "api_testing"

    status, body = web.get_json("/api/packs/__missing__")
    assert status == 404
    assert_error(body, "not_found")


def test_scenarios_list_builtin(web: WebClient) -> None:
    status, data = web.get_json("/api/scenarios")
    assert status == 200 and data
    by_id = {item["id"]: item for item in data}
    smoke = by_id["scenarios/api_smoke.yaml"]
    assert smoke["source"] == "builtin"
    assert smoke["cases"] == 3
    assert smoke["name"]
    for item in data:
        assert item["cases"] >= 0
        assert "error" not in item or item["cases"] == 0


# --------------------------------------------------------------------------- #
# 请求校验与错误体
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "x"},
        {"mode": "scenario"},
        {"mode": "intent", "intent": "   "},
        {"mode": "demo", "env": [1, 2]},
        {"mode": "demo", "env": {"A": {"nested": 1}}},
        {"mode": "scenario", "scenario": "../../../../etc/passwd"},
        {"mode": "scenario", "scenario": "scenarios/__missing__.yaml"},
    ],
)
def test_invalid_run_requests_are_json_400(web: WebClient, payload: dict[str, Any]) -> None:
    status, body = web.post_json("/api/runs", payload)
    assert status == 400, (payload, status, body)
    assert_error(body, "bad_request")


def test_broken_json_body_is_400(web: WebClient) -> None:
    # 空 body → 400（mode 缺失）
    status, _headers, body = web.request("POST", "/api/runs")
    assert status == 400
    assert_error(json.loads(body.decode()), "bad_request")
    # 非 JSON → 400 且错误体仍是约定结构
    status, data = web.post_raw("/api/runs", b"{not json")
    assert status == 400
    assert_error(data, "bad_request")


def test_unknown_paths_and_jobs(web: WebClient) -> None:
    status, body = web.get_json("/api/jobs/nope")
    assert status == 404
    assert_error(body, "not_found")

    status, body = web.get_json("/api/runs/../../etc")
    assert status in (400, 404)
    assert_error(body)

    status, body = web.get_json("/api/runs/zzzzzz")
    assert status == 400
    assert_error(body, "bad_request")

    status, body = web.get_json("/api/unknown")
    assert status == 404
    assert_error(body, "not_found")


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
def test_static_whitelist_and_traversal(web: WebClient) -> None:
    status, _headers, body = web.request("GET", "/static/../../cli.py")
    assert status == 404
    text = body.decode("utf-8", errors="replace")
    assert "argparse" not in text and "def main" not in text

    status, _headers, body = web.request("GET", "/static/unknown.js")
    assert status == 404

    status, _headers, body = web.request("GET", "/")
    assert status in (200, 503)
    if status == 200:
        assert b"<html" in body.lower()
    else:
        assert "前端资源未就绪" in body.decode("utf-8")


def test_index_returns_503_when_static_missing(web: WebClient, monkeypatch, tmp_path) -> None:
    """前端资源未就绪时 GET / 必须是 503 + 明确文案，而不是崩溃。"""
    from panguverse.web import server as server_module

    monkeypatch.setattr(server_module, "STATIC_DIR", tmp_path / "no-static-here")
    status, _headers, body = web.request("GET", "/")
    assert status == 503
    assert "前端资源未就绪" in body.decode("utf-8")


# --------------------------------------------------------------------------- #
# 运行闭环
# --------------------------------------------------------------------------- #
def test_demo_run_produces_report_artifacts(web: WebClient, demo_run: dict[str, Any]) -> None:
    summary = demo_run["summary"]
    assert summary["total"] >= 7
    assert summary["passed"] == summary["total"]
    assert demo_run["conclusion"]["success"] is True
    assert demo_run["error"] is None
    run_id = demo_run["run_id"]
    assert run_id

    # phases 按执行顺序出现，且覆盖 plan → report
    names = [phase["name"] for phase in demo_run["phases"]]
    for expected in ("plan", "validate", "execute", "heal", "report"):
        assert expected in names, names
    assert names.index("plan") < names.index("execute") < names.index("report")

    status, jobs = web.get_json("/api/jobs")
    assert status == 200 and jobs[0]["job_id"] == demo_run["job_id"]

    status, runs = web.get_json("/api/runs?limit=10")
    assert status == 200
    assert any(entry["run_id"] == run_id for entry in runs)

    status, detail = web.get_json(f"/api/runs/{run_id}")
    assert status == 200
    assert detail["report"]["run_id"] == run_id
    assert detail["plan"]["source"] == "spec"

    status, headers, body = web.request("GET", f"/api/runs/{run_id}/report.html")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower()

    status, headers, body = web.request("GET", f"/api/runs/{run_id}/junit.xml")
    assert status == 200
    assert headers["Content-Type"].startswith("application/xml")
    assert "attachment" in headers["Content-Disposition"]
    assert run_id in headers["Content-Disposition"]


def _demo_against(web: WebClient, mock_api, **overrides: Any) -> dict[str, Any]:
    """跑 demo，并把 BASE_URL 指向外部 mock（模拟「有真实被测服务」的场景）。"""
    env = {
        "BASE_URL": mock_api.base_url,
        "API_USERNAME": "demo",
        "API_PASSWORD": "demo123",
        "MAX_LATENCY_MS": 3000,
        "FS_ROOT": str(web.workspace / ".panguverse" / "fs-demo"),
    }
    env.update(overrides)
    status, data = web.post_json("/api/runs", {"mode": "demo", "env": env})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"] >= 7, job
    return job


def test_regression_without_env_uses_persisted_baseline(web: WebClient, mock_api) -> None:
    """P1-1：demo 成功后 env 落盘；regression 不带 env 也能 7/7。"""
    _demo_against(web, mock_api)

    last_env_path = web.workspace / ".panguverse" / "web" / "last_env.json"
    baseline = json.loads(last_env_path.read_text(encoding="utf-8"))
    last_env = baseline["env"]
    assert last_env["BASE_URL"] == mock_api.base_url
    assert last_env["API_USERNAME"] == "demo"
    assert baseline["target"] is None  # 外部目标：回放 env 本身
    fs_root = Path(last_env["FS_ROOT"])
    assert fs_root == (web.workspace / ".panguverse" / "fs-demo").resolve()
    assert web.workspace.resolve() in fs_root.parents

    status, data = web.post_json("/api/runs", {"mode": "regression"})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"] >= 7, job

    status, detail = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    assert detail["plan"]["source"] == "memory"
    assert detail["plan"]["case_count"] >= 7


def test_demo_without_env_persists_declared_baseline(web: WebClient) -> None:
    """demo 的临时 mock 端口不能进基线：落盘的是场景声明的 BASE_URL。"""
    status, data = web.post_json("/api/runs", {"mode": "demo"})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job

    baseline = json.loads(
        (web.workspace / ".panguverse" / "web" / "last_env.json").read_text(encoding="utf-8")
    )
    assert baseline["target"]["kind"] == "mock"
    assert baseline["target"]["flaky_failures"] == 2
    assert baseline["env"]["BASE_URL"] == "http://127.0.0.1:8899"
    assert Path(baseline["env"]["FS_ROOT"]).is_relative_to(web.workspace.resolve())


def test_demo_then_regression_without_external_service(web: WebClient) -> None:
    """F1（A9）：干净工作区、不预启任何外部服务，demo → regression 都必须 7/7。"""
    status, data = web.post_json("/api/runs", {"mode": "demo"})
    assert status == 202, data
    demo = web.wait_job(data["job_id"])
    assert demo["status"] == "done", demo
    assert demo["summary"]["passed"] == demo["summary"]["total"] >= 7, demo

    baseline = json.loads(
        (web.workspace / ".panguverse" / "web" / "last_env.json").read_text(encoding="utf-8")
    )
    assert baseline["target"]["kind"] == "mock"

    status, data = web.post_json("/api/runs", {"mode": "regression"})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"] >= 7, job

    status, detail = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    assert detail["plan"]["source"] == "memory"


def test_env_credentials_are_masked_in_http(web: WebClient) -> None:
    """F2（A16）：HTTP 不回吐 env 明文，键名保留、值为 ***。"""
    status, data = web.post_json("/api/runs", {"mode": "demo"})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job

    status, _headers, body = web.request("GET", f"/api/runs/{job['run_id']}")
    assert status == 200
    text = body.decode("utf-8")
    assert "demo123" not in text
    assert '"API_PASSWORD"' in text
    assert '"***"' in text

    status, _headers, body = web.request("GET", f"/api/jobs/{job['job_id']}")
    assert status == 200
    assert "demo123" not in body.decode("utf-8")


def test_env_mask_recurses_nested_structures() -> None:
    from panguverse.web.api import mask_env_payload

    payload = {
        "a": {
            "env": {
                "API_PASSWORD": "secret-value",
                "outer": {"API_TOKEN": "tok", "keep": "ok"},
            },
            "b": 1,
        }
    }
    masked = mask_env_payload(payload)
    env = masked["a"]["env"]
    assert env["API_PASSWORD"] == "***"
    assert env["outer"]["API_TOKEN"] == "***"
    assert env["outer"]["keep"] == "ok"
    assert masked["a"]["b"] == 1
    # 不打码原对象（磁盘数据不被改写）
    assert payload["a"]["env"]["API_PASSWORD"] == "secret-value"


def test_all_errored_job_is_failed(web: WebClient) -> None:
    """F3：全部用例 errored（目标不可达）→ failed，error 给首个原因，报告仍落盘。"""
    status, data = web.post_json(
        "/api/runs",
        {"mode": "intent", "intent": "检查健康状态", "env": {"BASE_URL": "http://127.0.0.1:1"}},
    )
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "failed", job
    assert job["error"], job
    assert job["run_id"], job  # 报告仍写出，便于诊断
    status, detail = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    assert detail["report"]["summary"]["errored"] >= 1


def test_assertion_failure_stays_done(web: WebClient) -> None:
    """F3 反向：真实断言失败（非全 errored）仍是 done + conclusion.success=false。"""
    scenarios = web.workspace / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    (scenarios / "cli_fail.yaml").write_text(
        "name: 断言失败\n"
        "cases:\n"
        "  - id: fail_case\n"
        "    name: 断言失败用例\n"
        "    steps:\n"
        "      - id: check\n"
        "        use: assert_json\n"
        "        with:\n"
        "          path: value\n"
        "          equals: 1\n"
        "          source:\n"
        "            value: 2\n",
        encoding="utf-8",
    )
    status, data = web.post_json(
        "/api/runs", {"mode": "scenario", "scenario": "scenarios/cli_fail.yaml"}
    )
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["failed"] == 1
    assert job["conclusion"]["success"] is False


def test_scenario_from_workspace(web: WebClient) -> None:
    scenarios = web.workspace / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    (scenarios / "quick.yaml").write_text(
        "name: 快速场景\n"
        "cases:\n"
        "  - id: log_case\n"
        "    name: 记录一下\n"
        "    steps:\n"
        "      - id: log\n"
        "        use: log\n"
        "        with:\n"
        "          message: hello\n",
        encoding="utf-8",
    )

    status, data = web.post_json(
        "/api/runs", {"mode": "scenario", "scenario": "scenarios/quick.yaml"}
    )
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"] == 1

    status, detail = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    assert detail["plan"]["source"] == "spec"


def test_intent_with_env_runs_offline(
    web: WebClient, mock_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    status, data = web.post_json(
        "/api/runs",
        {"mode": "intent", "intent": "检查健康状态", "env": {"BASE_URL": mock_api.base_url}},
    )
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"] == 1, job

    status, detail = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200
    assert detail["report"]["meta"]["llm_used"] is False
    assert detail["report"]["plan"]["source"] == "rules"


def test_intent_without_env_fails_with_missing_variable(web: WebClient) -> None:
    """P1-2：没有基线也没有 env → failed + 明确缺变量，禁止 done/errored 假成功。"""
    assert not (web.workspace / ".panguverse" / "web" / "last_env.json").exists()
    status, data = web.post_json("/api/runs", {"mode": "intent", "intent": "检查健康状态"})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "failed", job
    assert "缺少环境变量: BASE_URL" in (job["error"] or "")
    assert job["run_id"] == ""
    validate = [p for p in job["phases"] if p["name"] == "validate"]
    assert validate and validate[-1]["status"] == "failed"
    assert not (web.workspace / "runs").exists() or not list((web.workspace / "runs").iterdir())


def _repo_untracked() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line.strip()}


def test_intent_without_env_fails_even_after_successful_run(web: WebClient, mock_api) -> None:
    """A8 的确定性要求：即使已有 env 基线，空 env 的 intent 也必须 failed（不静默继承目标）。"""
    _demo_against(web, mock_api)
    assert (web.workspace / ".panguverse" / "web" / "last_env.json").exists()

    status, data = web.post_json("/api/runs", {"mode": "intent", "intent": "检查健康状态"})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "failed", job
    assert "缺少环境变量: BASE_URL" in (job["error"] or "")


def test_workspace_boundary_no_repo_pollution(web: WebClient, mock_api) -> None:
    """A15：demo + regression + intent 跑完后仓库根目录不新增未跟踪文件。"""
    before = _repo_untracked()
    _demo_against(web, mock_api)

    status, data = web.post_json("/api/runs", {"mode": "regression"})
    assert status == 202, data
    assert web.wait_job(data["job_id"])["status"] == "done"

    status, data = web.post_json(
        "/api/runs",
        {"mode": "intent", "intent": "检查健康状态", "env": {"BASE_URL": mock_api.base_url}},
    )
    assert status == 202, data
    assert web.wait_job(data["job_id"])["status"] == "done"

    assert _repo_untracked() == before
    for name in ("snapshots", "archive"):
        assert not (REPO_ROOT / name).exists(), f"{name} 被写到仓库根目录"


# --------------------------------------------------------------------------- #
# 任务模型
# --------------------------------------------------------------------------- #
def test_job_timeout_marks_failed() -> None:
    manager = JobManager(timeout=0.2)

    def slow(job):
        time.sleep(1.5)
        return {"run_id": "late"}

    job = manager.submit("demo", slow)
    deadline = time.time() + 5
    while time.time() < deadline and job.status not in ("done", "failed"):
        time.sleep(0.05)
    assert job.status == FAILED
    assert job.error == "timeout"


def test_loopback_detection() -> None:
    assert is_loopback_url("http://127.0.0.1:8899/api/health")
    assert is_loopback_url("http://localhost:7788/")
    assert is_loopback_url("http://foo.localhost/")
    assert is_loopback_url("http://[::1]:7788/")
    assert is_loopback_url("http://0.0.0.0:1/")
    assert not is_loopback_url("https://api.example.com/")
    assert not is_loopback_url("")


@pytest.fixture()
def local_http_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("backend", ["requests", "urllib"])
def test_http_client_bypasses_dead_proxy(
    local_http_server: str, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    client = HttpClient(backend=backend)
    try:
        response = client.send(HttpRequest(url=f"{local_http_server}/ping"))
    finally:
        client.close()
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_demo_run_survives_dead_proxy(web: WebClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    status, data = web.post_json("/api/runs", {"mode": "demo", "regression": False})
    assert status == 202, data
    job = web.wait_job(data["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"] >= 7
