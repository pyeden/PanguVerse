"""普通草稿的发布门禁：结构错误（P001/P101 等）必须与服务端校验一致地 409。

回归背景：``studio.publish`` 的 else 分支曾只把 ``intent_mismatch`` /
``capability_gap`` 当作 blocking，导致 validate 报 P101 的普通草稿仍能 200
发布；前端会拦、服务端可绕过。探索固化的 advisory 豁免不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from panguverse.web import studio
from panguverse.web.api import WebAPI

INTENT = "备份配置文件并校验备份存在"


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    route, _, query = path.partition("?")
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    response = api.handle(method, route, query, raw)
    body = json.loads(response.body.decode("utf-8")) if response.body else None
    return response.status, body


@pytest.fixture()
def api(tmp_path: Path) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    return WebAPI(root)


def create_draft(api: WebAPI) -> dict[str, Any]:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": INTENT, "domain_pack": "local_fs"},
    )
    assert status == 201, draft
    return draft


def approve(api: WebAPI, wid: str) -> None:
    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body


def test_non_explore_p101_blocks_publish(api: WebAPI) -> None:
    """普通草稿引用未知变量：validate 报 P101，publish 同样 409。"""
    draft = create_draft(api)
    wid = draft["wid"]
    node = draft["graph"]["nodes"][0]
    status, patched = call(
        api,
        "PATCH",
        f"/api/studio/{wid}/nodes/{node['id']}",
        {"params": {"path": "config/app.conf", "text": "mode=${undefined_node_var}"}},
    )
    assert status == 200, patched
    rev = patched["rev"]
    assert any(item["code"] == "P101" for item in patched["validation"]["errors"])

    status, validation = call(api, "POST", f"/api/studio/{wid}/validate", {})
    assert status == 200, validation
    assert validation["ok"] is False
    assert any(item["code"] == "P101" for item in validation["errors"])

    approve(api, wid)
    status, body = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": rev, "message": "p101"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "P101"
    assert any(item["code"] == "P101" for item in body["error"]["blocking"])


def test_non_explore_p001_blocks_publish(api: WebAPI) -> None:
    """普通草稿清空节点后为 P001 结构错误，publish 不得放行。"""
    draft = create_draft(api)
    wid = draft["wid"]
    rev = draft["rev"]
    for node in draft["graph"]["nodes"]:
        status, body = call(api, "DELETE", f"/api/studio/{wid}/nodes/{node['id']}")
        assert status == 200, body
        rev = body["rev"]
    approve(api, wid)

    status, validation = call(api, "POST", f"/api/studio/{wid}/validate", {})
    assert status == 200, validation
    assert validation["ok"] is False
    assert any(item["code"] == "P001" for item in validation["errors"])

    status, body = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": rev, "message": "p001"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "P001"


def test_non_explore_missing_atom_blocks_publish(api: WebAPI) -> None:
    """普通草稿缺原子：发布前原子门禁返回 409。"""
    draft = create_draft(api)
    wid = draft["wid"]
    revision = studio.read_revision(api.root, wid, int(draft["rev"]))
    revision["nodes"][0]["atom"] = "atom_not_in_registry"
    studio._atomic_write_json(
        api.root / ".panguverse" / "studio" / wid / "revisions" / f"{draft['rev']}.json",
        revision,
    )
    approve(api, wid)
    status, body = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": draft["rev"], "message": "x"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "atom_missing"
    assert body["error"]["atom"] == "atom_not_in_registry"


def test_non_explore_valid_draft_still_publishable(api: WebAPI) -> None:
    """正常普通草稿仍可发布，结构门禁不能误伤。"""
    draft = create_draft(api)
    wid = draft["wid"]
    approve(api, wid)
    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "正常发布"}
    )
    assert status == 200, published
    assert published["workflow_ref"] == f"{wid}@0"
