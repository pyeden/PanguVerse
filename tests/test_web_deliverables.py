"""任务交付物成为运行产出物（端到端）。

覆盖：
- 固化 ``output_paths`` → ``Atom.produces`` / 节点 ``produces``；
- 一次真实 ``mode=workflow`` task 运行 → ``/api/artifacts?run=`` 同时包含
  平台运行报告与任务交付物（news_data / report_html / report_md）；
- 下载字节与运行 workspace 原文件一致；
- 缺产物只 warning，不改变运行结论；
- 旧原子（只有 ``Atom.produces``）不回归。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from panguverse.axe import abstraction
from panguverse.core.workspace_packs import workspace_registry
from panguverse.web import artifacts
from panguverse.web.api import WebAPI
from panguverse.web.server import PanguverseHTTPServer

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DELIVERABLE_SOURCE = '''import json, os

def run(ctx=None, **inputs):
    out = inputs.get("output_dir") or "."
    os.makedirs(out, exist_ok=True)
    payload = {
        "meta": {"fetched_at": "2026-11-07 10:00:00", "count": 1},
        "items": [{"word": "热点一", "hot": "100"}],
    }
    with open(os.path.join(out, "news_data.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><body><h1>热点新闻报告</h1></body></html>")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("# 热点新闻报告\\n\\n- 热点一\\n")
    return {
        "news_data": "news_data.json",
        "report_html": "report.html",
        "report_md": "report.md",
    }
'''


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
        return status, json.loads(body.decode("utf-8") or "null")

    def post_json(self, path: str, payload: Any) -> tuple[int, Any]:
        status, _headers, body = self.request("POST", path, payload)
        return status, json.loads(body.decode("utf-8") or "null")

    def wait_job(self, job_id: str, *, timeout: float = 90.0) -> dict[str, Any]:
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


def _crystallized_pack(root: Path) -> dict[str, Any]:
    """走真实固化落盘函数：output_paths → produces。"""
    contract = {
        "inputs": {"source?": "string?", "output_dir?": "string?"},
        "outputs": {
            "news_data": "string",
            "report_html": "string",
            "report_md": "string",
        },
        "output_paths": {
            "news_data": "news_data.json",
            "report_html": "report.html",
            "report_md": "report.md",
        },
        "effects": ["write"],
        "idempotent": True,
        "failure_class": "internal",
    }
    info = abstraction.write_atom(
        workspace=root,
        eid="e-deliver",
        name="hot_news_report",
        source=DELIVERABLE_SOURCE,
        contract=contract,
        script={"seq": 1, "path": "scripts/report.py"},
        description="生成热点新闻报告",
        generality=abstraction.GeneralityReport(ok=True),
    )
    abstraction.write_pack_manifest(root, "e-deliver", [info])
    return info


def _publish_workflow(root: Path) -> str:
    """写入一条已批准/已发布的工作流（含节点 produces 映射）。"""
    wid = "w-deadbeef"
    ref = f"{wid}@0"
    nodes = [
        {
            "id": "s1_hot_report",
            "atom": "hot_news_report",
            "name": "生成热点新闻报告",
            "description": "固化原子",
            "params": {},
            "capture": {},
            "when": None,
            "foreach": None,
            "as": "item",
            "retry": None,
            "continue_on_failure": False,
            "tags": ["explore", "new_atom"],
            "optional": False,
            "extra": {"explore_id": "e-deliver", "new_atom": True},
            "case_id": "c1",
            "case_name": "热点新闻报告",
            "origin": "explore",
            "new_atom": True,
            "locked": False,
            "produces": {
                "news_data": {"path": "news_data.json", "type": "json"},
                "report_html": {"path": "report.html", "type": "text"},
                "report_md": {"path": "report.md", "type": "markdown"},
                "missing_deliverable": {"path": "missing.txt", "type": "text"},
            },
        }
    ]
    directory = root / ".panguverse" / "studio" / wid
    (directory / "revisions").mkdir(parents=True)
    (directory / "published").mkdir(parents=True)
    workflow = {
        "wid": wid,
        "name": "热点新闻报告",
        "intent": "抓取热点新闻并生成报告",
        "state": "approved",
        "current_rev": 0,
        "published_ref": ref,
        "origin": "explore",
        "explore_id": "e-deliver",
        "replay_status": "replay_passed",
        "domain_pack": "",
    }
    revision = {
        "wid": wid,
        "rev": 0,
        "name": "热点新闻报告",
        "intent": "抓取热点新闻并生成报告",
        "nodes": nodes,
        "edges": [],
        "replay_checks": [],
        "produces": [
            {"node_id": "s1_hot_report", "name": key, **spec}
            for key, spec in nodes[0]["produces"].items()
        ],
    }
    (directory / "workflow.json").write_text(
        json.dumps(workflow, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "revisions" / "0.json").write_text(
        json.dumps(revision, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "published" / "0.json").write_text(
        json.dumps(
            {
                "workflow_ref": ref,
                "wid": wid,
                "rev": 0,
                "message": "交付物验证",
                "name": workflow["name"],
                "intent": workflow["intent"],
                "domain_pack": "",
                "published_at": time.time(),
                "node_count": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ref


def test_crystallized_atom_exposes_produces(web: WebClient) -> None:
    info = _crystallized_pack(web.workspace)
    expected = {
        "news_data": {"path": "news_data.json", "type": "json"},
        "report_html": {"path": "report.html", "type": "text"},
        "report_md": {"path": "report.md", "type": "markdown"},
    }
    assert info["produces"] == expected

    manifest_path = (
        web.workspace
        / ".panguverse"
        / "packs"
        / "explore_e_deliver"
        / "pack.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["atoms"] if item["name"] == "hot_news_report")
    assert entry["produces"] == expected

    atom = workspace_registry(web.workspace).get("hot_news_report")
    assert atom.produces == ("news_data", "report_html", "report_md")


def test_workflow_task_collects_deliverables(web: WebClient) -> None:
    _crystallized_pack(web.workspace)
    ref = _publish_workflow(web.workspace)

    status, task = web.post_json(
        "/api/tasks",
        {"name": "热点新闻报告交付物", "mode": "workflow", "workflow_ref": ref, "env": {}},
    )
    assert status == 201, task
    status, queued = web.post_json(f"/api/tasks/{task['task_id']}/run", {})
    assert status == 202, queued
    job = web.wait_job(queued["job_id"])
    assert job["status"] == "done", job
    assert job["summary"]["passed"] == job["summary"]["total"], job
    assert job["conclusion"]["success"] is True, job
    run_id = job["run_id"]

    run_workspace = web.workspace / "runs" / run_id / "workspace"
    assert (run_workspace / "news_data.json").is_file()
    assert (run_workspace / "report.html").is_file()
    assert (run_workspace / "report.md").is_file()

    status, listing = web.get_json(f"/api/artifacts?run={run_id}")
    assert status == 200, listing
    by_name = {item["name"]: item for item in listing["items"]}
    assert {"news_data", "report_html", "report_md"} <= set(by_name)
    assert {"report.html", "report.json", "junit.xml", "plan.json"} <= set(by_name)
    # 节点 produces 的 type 覆盖文件后缀推断（report.html 是 text，不是 xml）
    assert by_name["news_data"]["type"] == "json"
    assert by_name["report_html"]["type"] == "text"
    assert by_name["report_md"]["type"] == "markdown"
    assert by_name["news_data"]["node_id"] == "s1_hot_report"
    assert by_name["report_html"]["path"] == "report.html"
    assert by_name["report_md"]["path"] == "report.md"
    assert "missing_deliverable" not in by_name

    # 下载字节 == 运行 workspace 原文件；sha256 与 manifest 一致
    for name, relative in (
        ("news_data", "news_data.json"),
        ("report_html", "report.html"),
        ("report_md", "report.md"),
    ):
        item = by_name[name]
        source = (run_workspace / relative).read_bytes()
        assert item["bytes"] == len(source)
        assert item["sha256"] == hashlib.sha256(source).hexdigest()
        status, _headers, body = web.request(
            "GET", f"/api/artifacts/{item['artifact_id']}/download"
        )
        assert status == 200, (name, body)
        assert body == source, name
        status, detail = web.get_json(f"/api/artifacts/{item['artifact_id']}")
        assert status == 200, (name, detail)
        assert detail["content"] == source.decode("utf-8"), name

    # run 详情：artifacts 同时包含平台报告与交付物；deliverables 单独列出
    status, detail = web.get_json(f"/api/runs/{run_id}")
    assert status == 200, detail
    report = detail["report"]
    assert report["summary"]["success"] is True
    artifact_names = set(report["artifacts"])
    assert {"report.html", "news_data", "report_html", "report_md"} <= artifact_names
    assert report["artifacts"]["report_md"] == "report.md"
    deliverables = {item["name"]: item for item in report["deliverables"]}
    assert set(deliverables) == {"news_data", "report_html", "report_md"}
    assert deliverables["report_html"] == {
        "name": "report_html",
        "type": "text",
        "node_id": "s1_hot_report",
        "path": "report.html",
        "artifact_id": by_name["report_html"]["artifact_id"],
        "bytes": by_name["report_html"]["bytes"],
        "sha256": by_name["report_html"]["sha256"],
    }

    # 缺产物只 warning，不影响结论
    phase = [item for item in job["phases"] if item["name"] == "artifacts"][-1]
    assert phase["status"] == "warning", phase
    assert "missing_deliverable" in phase["detail"]


def test_legacy_atom_produces_still_collected(web: WebClient) -> None:
    """旧包只有 output_paths（没有 produces）：Atom.produces 回退仍可收集。"""
    pack = web.workspace / ".panguverse" / "packs" / "legacy_pack"
    (pack / "atoms").mkdir(parents=True)
    module = (
        "import os\n"
        "def run(ctx=None, **inputs):\n"
        "    out = inputs.get('output_dir') or '.'\n"
        "    os.makedirs(out, exist_ok=True)\n"
        "    with open(os.path.join(out, 'legacy.md'), 'w', encoding='utf-8') as fh:\n"
        "        fh.write('# legacy report\\n')\n"
        "    return {'legacy_report': 'legacy.md'}\n"
    )
    (pack / "atoms" / "legacy_report.py").write_text(module, encoding="utf-8")
    (pack / "pack.json").write_text(
        json.dumps(
            {
                "name": "legacy_pack",
                "domain": "explore",
                "atoms": [
                    {
                        "name": "legacy_report",
                        "entry": "atoms/legacy_report.py",
                        "inputs": {"output_dir?": "string?"},
                        "outputs": {"legacy_report": "string"},
                        "output_paths": {"legacy_report": "legacy.md"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run_id = "aaaabbbbcccc"
    run_workspace = web.workspace / "runs" / run_id / "workspace"
    run_workspace.mkdir(parents=True)
    (run_workspace / "legacy.md").write_text("# legacy report\n", encoding="utf-8")
    run = SimpleNamespace(
        run_id=run_id,
        meta={},
        cases=[
            SimpleNamespace(
                case_id="c1",
                steps=[
                    SimpleNamespace(
                        step_id="s1_legacy",
                        atom="legacy_report",
                        status="passed",
                        outputs={"legacy_report": "legacy.md"},
                    )
                ],
            )
        ],
    )
    result = artifacts.collect_run(web.workspace, run, workspace=run_workspace)
    assert result["count"] == 1, result
    item = result["artifacts"][0]
    assert item["name"] == "legacy_report"
    assert item["node_id"] == "legacy_report"
    assert item["type"] == "markdown"
    assert item["path"] == "legacy.md"
