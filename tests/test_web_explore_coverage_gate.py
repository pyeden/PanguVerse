"""探索固化流程的意图覆盖率门禁：advisory 不阻断，重放仍是硬门禁。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from panguverse.web import service, studio
from panguverse.web.api import WebAPI

INTENT = "打开示例网站提取今天热点新闻输出好看的热点新闻报告"


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    response = api.handle(method, path, "", raw)
    body = json.loads(response.body.decode("utf-8")) if response.body else None
    return response.status, body


@pytest.fixture()
def api(tmp_path: Path) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    return WebAPI(root)


def explore_nodes() -> list[dict[str, Any]]:
    """1 个等价命中（http_request ≈ fetch）+ 1 个未匹配节点，覆盖率 50%。"""
    return [
        {
            "id": "s1_http_request",
            "atom": "http_request",
            "name": "http_request",
            "description": "来自探索的原子步骤",
            "params": {"url": "https://example.com/board", "method": "GET"},
            "capture": {"s1_http_request_text": {"path": "text"}},
            "tags": ["explore"],
            "extra": {"role": "fetch", "explore_id": "e-test"},
        },
        {
            "id": "s2_scratch",
            "atom": "fs_copy",
            "name": "复制中间文件",
            "description": "复制临时文件",
            "params": {"source": "scratch.txt", "target": "copy.txt"},
            "tags": ["explore"],
            "extra": {"role": "transform", "explore_id": "e-test"},
        },
    ]


EDGES = [
    {"id": "e1", "from": "s1_http_request", "to": "s2_scratch", "kind": "data"}
]


def create_explore_draft(api: WebAPI) -> dict[str, Any]:
    return studio.create_draft_from_graph(
        api.root,
        intent=INTENT,
        name=INTENT,
        nodes=explore_nodes(),
        edges=EDGES,
        explore_id="e-test",
        registry=service.registry(),
    )


def approve(api: WebAPI, wid: str) -> None:
    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body


def test_explore_low_coverage_is_advisory_and_publishable(api: WebAPI) -> None:
    draft = create_explore_draft(api)
    wid = draft["wid"]
    validation = draft["validation"]
    assert validation["ok"] is True, validation
    assert validation["coverage"]["gate"] == "advisory"
    assert validation["coverage"]["ratio"] == 0.5
    assert not any(item["code"] == "intent_mismatch" for item in validation["errors"])

    studio.record_replay(
        api.root,
        wid,
        {"status": "passed", "checks": [{"id": "items>=1", "ok": True}]},
    )
    approve(api, wid)

    status, body = call(api, "POST", f"/api/studio/{wid}/validate", {})
    assert status == 200, body
    assert body["ok"] is True
    assert body["coverage"]["gate"] == "advisory"
    assert not any(item["code"] == "intent_mismatch" for item in body["errors"])
    warnings = [item for item in body["warnings"] if item["code"] == "intent_coverage_advisory"]
    assert warnings, body["warnings"]
    warning = warnings[0]
    assert warning["ratio"] == 0.5
    assert warning["unmatched"] == ["s2_scratch"]
    assert "未匹配节点" in warning["message"]
    detail = body["coverage"]["unmatched_details"][0]
    assert detail["node_id"] == "s2_scratch"
    assert "不在预测原子集" in detail["reason"]
    assert "探索可能选用了等价原子或新增原子" in detail["reason"]

    status, published = call(
        api,
        "POST",
        f"/api/studio/{wid}/publish",
        {"rev": 0, "message": "探索固化发布"},
    )
    assert status == 200, published
    assert published["workflow_ref"] == f"{wid}@0"


def test_explore_replay_failed_still_blocks_publish(api: WebAPI) -> None:
    draft = create_explore_draft(api)
    wid = draft["wid"]
    studio.record_replay(
        api.root,
        wid,
        {"status": "failed", "checks": [{"id": "items>=1", "ok": False}]},
    )
    approve(api, wid)
    status, body = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "x"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "replay_failed"


def test_non_explore_low_coverage_still_blocked(api: WebAPI) -> None:
    draft = create_explore_draft(api)
    wid = draft["wid"]
    workflow = studio.read_workflow(api.root, wid)
    workflow["origin"] = "ai"
    workflow["explore_id"] = ""
    studio._write_workflow(api.root, wid, workflow)
    studio.record_replay(
        api.root,
        wid,
        {"status": "passed", "checks": [{"id": "items>=1", "ok": True}]},
    )
    approve(api, wid)

    status, body = call(api, "POST", f"/api/studio/{wid}/validate", {})
    assert status == 200, body
    assert body["ok"] is False
    assert body["coverage"]["gate"] == "blocking"
    assert any(item["code"] == "intent_mismatch" for item in body["errors"])

    status, body = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "x"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "intent_mismatch"
