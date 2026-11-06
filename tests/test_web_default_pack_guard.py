"""第四轮：默认包护栏（不强迫路由）+ health 模板假设。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from panguverse.web import studio
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


def atoms_of(body: dict[str, Any]) -> list[str]:
    return [str(node["atom"]) for node in body["graph"]["nodes"]]


def test_default_no_pack_routes_web_research(api: WebAPI) -> None:
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": WEB_INTENT})
    assert status == 201, body
    assert body["domain_pack"] == "web_research"
    assert body["domain_pack_ignored"] is False
    atoms = atoms_of(body)
    assert {"http_get_page", "html_extract_text", "assert_json"} <= set(atoms)
    assert not {"http_request", "web_open"} & set(atoms)
    fetch = next(node for node in body["graph"]["nodes"] if node["id"] == "fetch_page")
    assert fetch["params"]["url"] == "https://top.baidu.com/board?tab=realtime"
    assert body["param_audit"]["param_gaps"] == []


def test_explicit_conflicting_pack_is_ignored(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": WEB_INTENT, "domain_pack": "api_testing"},
    )
    assert status == 201, body
    assert body["domain_pack"] == "web_research"
    assert body["domain_pack_ignored"] is True
    assert "api_testing" in body["domain_pack_message"]
    assert body["message"] == body["domain_pack_message"]
    atoms = atoms_of(body)
    assert {"http_get_page", "html_extract_text", "assert_json"} <= set(atoms)
    assert "http_request" not in atoms
    assert not any("/api/health" in str(node.get("params", {}).get("url", "")) for node in body["graph"]["nodes"])


def test_explicit_matching_pack_is_respected(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "测试健康接口，断言 200", "domain_pack": "api_testing"},
    )
    assert status == 201, body
    assert body["domain_pack"] == "api_testing"
    assert body["domain_pack_ignored"] is False
    assert "http_request" in atoms_of(body)


def test_health_template_declares_default_endpoint_assumption(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "测试健康接口，断言 200", "domain_pack": "api_testing"},
    )
    assert status == 201, body
    joined = "\n".join(body["assumptions"])
    assert "/api/health" in joined
    assert "模板" in joined and "未" in joined
    assert any("/api/health" in item for item in body["param_audit"]["assumptions"])


def test_replan_historical_mismatch_routes_by_intent(api: WebAPI, tmp_path: Path) -> None:
    # 先造一个 api_testing 草案，再把库内意图/领域改成历史错配形态
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "测试健康接口，断言 200", "domain_pack": "api_testing"},
    )
    assert status == 201, draft
    wid = draft["wid"]
    workflow_path = api.root / ".panguverse" / "studio" / wid / "workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["intent"] = WEB_INTENT
    workflow["domain_pack"] = "api_testing"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    revision_path = api.root / ".panguverse" / "studio" / wid / "revisions" / "0.json"
    revision = json.loads(revision_path.read_text(encoding="utf-8"))
    revision["intent"] = WEB_INTENT
    revision["domain_pack"] = "api_testing"
    revision_path.write_text(json.dumps(revision), encoding="utf-8")

    status, proposal = call(
        api, "POST", f"/api/studio/{wid}/replan", {"instruction": "重新规划"}
    )
    assert status == 200, proposal
    assert proposal["domain_pack"] == "web_research"
    assert proposal["domain_pack_ignored"] is True
    atoms = [str(node["atom"]) for node in proposal["graph"]["nodes"]]
    assert "http_get_page" in atoms
    assert "http_request" not in atoms
    assert not any("/api/health" in str(node.get("params", {}).get("url", "")) for node in proposal["graph"]["nodes"])
    assert proposal["diff"]["nodes"]


def _mismatched_workflow(api: WebAPI) -> str:
    """造一个「库内意图是网页、库内领域包是 api_testing」的历史错配工作流。"""
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "测试健康接口，断言 200", "domain_pack": "api_testing"},
    )
    assert status == 201, draft
    wid = draft["wid"]
    studio_dir = api.root / ".panguverse" / "studio" / wid
    workflow_path = studio_dir / "workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["intent"] = WEB_INTENT
    workflow["domain_pack"] = "api_testing"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    revision_path = studio_dir / "revisions" / "0.json"
    revision = json.loads(revision_path.read_text(encoding="utf-8"))
    revision["intent"] = WEB_INTENT
    revision["domain_pack"] = "api_testing"
    revision_path.write_text(json.dumps(revision), encoding="utf-8")
    return wid


def test_replan_apply_end_to_end_keeps_corrected_intent_and_pack(api: WebAPI) -> None:
    """F2：replan 全量接受后，校验/覆盖率/发布都必须基于纠正后的语义。"""
    wid = _mismatched_workflow(api)

    status, proposal = call(
        api, "POST", f"/api/studio/{wid}/replan", {"instruction": "重新规划"}
    )
    assert status == 200, proposal
    # 提案要持久化护栏纠正后的真实意图与领域，而不是 replan 指令。
    assert proposal["intent"] == WEB_INTENT
    assert proposal["domain_pack"] == "web_research"
    assert proposal["domain_pack_ignored"] is True

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
    # F1：index=0 的新节点必须排在首位，后续节点才引用得到 ${fetch_page}。
    assert applied["graph"]["order"][0] == "fetch_page"
    codes = [item["code"] for item in applied["validation"]["errors"]]
    assert "P101" not in codes, applied["validation"]
    assert "intent_mismatch" not in codes, applied["validation"]
    assert applied["validation"]["coverage"]["ratio"] == 1.0

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
    assert workflow["intent"] == WEB_INTENT
    assert workflow["domain_pack"] == "web_research"
    assert revision["intent"] == WEB_INTENT
    assert revision["domain_pack"] == "web_research"

    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    status, body = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": applied["rev"], "message": "v5"}
    )
    assert status == 200, body
    assert body["workflow_ref"] == f"{wid}@{applied['rev']}"


@pytest.mark.parametrize(
    "intent",
    [
        "打开 https://www.baidu.com 提取今天的热点新闻",
        "打开 example.com 提取今天的热点新闻",
        "打开百度，提取今天的热点新闻",
    ],
)
def test_web_intent_with_url_or_alias_overrides_api_pack(api: WebAPI, intent: str) -> None:
    """F3：URL / 裸域名 / 站点别名都是网页证据，显式 api_testing 应被忽略。"""
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": intent, "domain_pack": "api_testing"})
    assert status == 201, body
    assert body["domain_pack"] == "web_research"
    assert body["domain_pack_ignored"] is True
    atoms = atoms_of(body)
    assert "http_get_page" in atoms
    assert "http_request" not in atoms
    fetch = next(node for node in body["graph"]["nodes"] if node["atom"] == "http_get_page")
    assert str(fetch["params"]["url"]).startswith("https://")
    assert not any("/api/health" in str(node.get("params", {}).get("url", "")) for node in body["graph"]["nodes"])


def test_real_api_path_with_url_keeps_api_pack(api: WebAPI) -> None:
    """/api/... 路径 + 断言仍是真实 API 意图，不能被网页护栏抢走。"""
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "访问 https://seres-ai.com/api/status，断言 200", "domain_pack": "api_testing"},
    )
    assert status == 201, body
    assert body["domain_pack"] == "api_testing"
    assert body["domain_pack_ignored"] is False
    assert "http_request" in atoms_of(body)
