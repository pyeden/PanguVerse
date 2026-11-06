"""第六轮：replan→apply 后的元数据必须来自新计划，历史版本不回填当前值。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from panguverse.web.api import WebAPI

WEB_INTENT = "打开百度网站，提取今天的热点新闻"


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
    return WebAPI(root)


def _mismatched_workflow(api: WebAPI) -> str:
    """造一个「库内意图是网页、落库领域包是 api_testing」的历史错配工作流。"""
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "测试健康接口，断言 200", "domain_pack": "api_testing"},
    )
    assert status == 201, draft
    wid = draft["wid"]
    studio_dir = api.root / ".panguverse" / "studio" / wid
    for name in ("workflow.json", "revisions/0.json"):
        path = studio_dir / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["intent"] = WEB_INTENT
        data["domain_pack"] = "api_testing"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return wid


def test_apply_rewrites_metadata_and_revision_view(api: WebAPI) -> None:
    wid = _mismatched_workflow(api)

    status, proposal = call(
        api, "POST", f"/api/studio/{wid}/replan", {"instruction": "重新规划"}
    )
    assert status == 200, proposal
    assert proposal["domain_pack"] == "web_research"
    accepted = [
        change["change_id"]
        for change in proposal["diff"]["nodes"] + proposal["diff"]["edges"]
    ]
    status, applied = call(
        api,
        "POST",
        f"/api/studio/{wid}/apply-diff",
        {"proposed_rev": proposal["proposed_rev"], "accepted": accepted},
    )
    assert status == 200, applied
    assert applied["validation"]["ok"] is True, applied["validation"]

    workflow = json.loads(
        (api.root / ".panguverse" / "studio" / wid / "workflow.json").read_text(
            encoding="utf-8"
        )
    )
    revision = json.loads(
        (api.root / ".panguverse" / "studio" / wid / "revisions" / "1.json").read_text(
            encoding="utf-8"
        )
    )
    assert "网页" in workflow["name"]
    assert "API_PASSWORD" not in " ".join(workflow["assumptions"])
    assert all(
        key in revision
        for key in (
            "name",
            "planner",
            "plan_source",
            "confidence",
            "assumptions",
            "param_audit",
            "env_schema",
        )
    )
    node_ids = {str(node["id"]) for node in revision["nodes"]}
    audit = revision["param_audit"]
    touched = {
        str(item.get("node_id"))
        for item in (audit.get("env_inputs") or []) + (audit.get("autofilled") or [])
    }
    assert touched <= node_ids
    assert "health" not in touched

    status, rev0 = call(api, "GET", f"/api/studio/{wid}?rev=0")
    assert status == 200
    assert rev0["domain_pack"] == "api_testing"
    assert rev0["name"].startswith("健康检查")
    status, rev1 = call(api, "GET", f"/api/studio/{wid}?rev=1")
    assert status == 200
    assert rev1["domain_pack"] == "web_research"
    assert "网页" in rev1["name"]


def test_validate_does_not_inherit_workflow_assumptions(api: WebAPI) -> None:
    status, draft = call(
        api, "POST", "/api/studio/drafts", {"intent": WEB_INTENT}
    )
    assert status == 201, draft
    wid = draft["wid"]
    status, patched = call(
        api,
        "PATCH",
        f"/api/studio/{wid}/nodes/fetch_page",
        {"params": {"url": "${env.R6_MISSING_TOKEN}"}},
    )
    assert status == 200, patched

    workflow_path = api.root / ".panguverse" / "studio" / wid / "workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["assumptions"] = ["需要 R6_MISSING_TOKEN"]
    workflow_path.write_text(json.dumps(workflow, ensure_ascii=False), encoding="utf-8")

    status, validation = call(
        api, "POST", f"/api/studio/{wid}/validate", {"rev": 1}
    )
    assert status == 200, validation
    codes = [item["code"] for item in validation["errors"]]
    assert "missing_env" in codes
    assert "R6_MISSING_TOKEN" in validation["missing_env"]
