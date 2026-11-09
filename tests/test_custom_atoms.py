"""工作区自定义原子注册测试。"""

from __future__ import annotations

import json

import pytest

from panguverse.core.workspace_packs import load_workspace_packs
from panguverse.web.custom_atoms import register_custom_atom
from panguverse.web.studio import StudioError


def _payload(**overrides):
    payload = {
        "name": "parse_price",
        "description": "从 HTML 中提取价格",
        "code": "def run(**inputs):\n    return {'out': inputs.get('text', '')}\n",
        "inputs": {"text": "string"},
        "outputs": {"out": "string"},
        "effects": [],
        "capabilities": [],
        "idempotent": True,
        "pack": "custom",
    }
    payload.update(overrides)
    return payload


def test_register_custom_atom_writes_workspace_pack(tmp_path):
    result = register_custom_atom(tmp_path, _payload(), actor="tester")
    assert result["ref"].startswith("atom:parse_price@0.1.0#")
    assert result["pack"] == "custom"
    atom_path = tmp_path / ".panguverse" / "packs" / "custom" / "atoms" / "parse_price.py"
    manifest_path = tmp_path / ".panguverse" / "packs" / "custom" / "pack.json"
    assert atom_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["atoms"][0]["name"] == "parse_price"
    packs = load_workspace_packs(tmp_path)
    names = [atom.name for pack in packs for atom in pack.atoms]
    assert "parse_price" in names


def test_duplicate_requires_replace(tmp_path):
    register_custom_atom(tmp_path, _payload())
    with pytest.raises(StudioError) as exc:
        register_custom_atom(tmp_path, _payload())
    assert exc.value.code == "conflict"
    replaced = register_custom_atom(tmp_path, _payload(code="def run(**inputs):\n    return {'out': 'v2'}\n", replace=True))
    assert replaced["atom"]["name"] == "parse_price"


def test_rejects_syntax_error(tmp_path):
    with pytest.raises(StudioError) as exc:
        register_custom_atom(tmp_path, _payload(code="def run(:\n    pass\n"))
    assert exc.value.status == 422


def test_rejects_missing_run(tmp_path):
    with pytest.raises(StudioError) as exc:
        register_custom_atom(tmp_path, _payload(code="print('no run')\n"))
    assert "def run" in exc.value.message


def test_rejects_empty_code(tmp_path):
    with pytest.raises(StudioError):
        register_custom_atom(tmp_path, _payload(code="   "))


def test_register_and_bind_workflow_node(tmp_path):
    from panguverse.web import studio

    workflow = studio.create_draft_from_graph(
        tmp_path,
        intent="抓取页面并解析",
        name="绑定测试",
        nodes=[{
            "id": "n1",
            "atom": "http_get_page",
            "name": "抓取",
            "params": {"url": "https://example.com"},
            "capture": {},
        }],
        edges=[],
        actor="tester",
    )
    wid = workflow["wid"]
    result = register_custom_atom(
        tmp_path,
        _payload(wid=wid, node_id="n1"),
        actor="tester",
    )
    assert result["binding"] is not None
    assert result["binding"]["rev"] == 1
    revision = studio.read_revision(tmp_path, wid, 1)
    assert revision["nodes"][0]["atom"] == "parse_price"
    assert revision["base_rev"] == 0


def test_node_implementation_returns_workspace_code(tmp_path):
    from panguverse.web import studio

    workflow = studio.create_draft_from_graph(
        tmp_path,
        intent="抓取页面并解析",
        name="实现回传测试",
        nodes=[{
            "id": "n1",
            "atom": "http_get_page",
            "name": "抓取",
            "params": {"url": "https://example.com"},
            "capture": {},
        }],
        edges=[],
        actor="tester",
    )
    wid = workflow["wid"]
    register_custom_atom(tmp_path, _payload(wid=wid, node_id="n1"), actor="tester")
    payload = studio.node_implementation(tmp_path, wid, "n1")
    assert payload["kind"] == "code"
    assert payload["editable"] is True
    assert payload["atom"] == "parse_price"
    assert payload["pack"] == "custom"
    assert "def run" in payload["code"]
    assert payload["hash"]
    assert payload["ref"].startswith("atom:parse_price@")


def test_node_implementation_builtin_is_readonly(tmp_path):
    from panguverse.web import studio

    workflow = studio.create_draft_from_graph(
        tmp_path,
        intent="抓取页面",
        name="内置实现测试",
        nodes=[{
            "id": "n1",
            "atom": "http_get_page",
            "name": "抓取",
            "params": {"url": "https://example.com"},
            "capture": {},
        }],
        edges=[],
        actor="tester",
    )
    payload = studio.node_implementation(tmp_path, workflow["wid"], "n1")
    assert payload["kind"] == "atom"
    assert payload["editable"] is False
    assert payload["atom"] == "http_get_page"
    assert "# atom: http_get_page@" in payload["code"]
