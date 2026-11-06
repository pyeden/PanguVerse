"""Crystallize semantics: LLM abstraction, generality gate, clean replay.

These tests pin the three-stage contract:

* exploration may try anything;
* crystallization induces + abstracts (LLM), but refuses recordings;
* replay/debug are deterministic and never call an LLM.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from panguverse.axe import abstraction, agent_exec
from panguverse.core.registry import default_registry
from panguverse.web import explore, studio


class StaticLLM:
    """Returns one canned JSON payload; counts calls."""

    available = True

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def status(self) -> dict[str, Any]:
        return {"available": True, "model": "static"}

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls += 1
        return json.loads(json.dumps(self.payload))


class ExplodingLLM:
    available = True

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        raise AssertionError("replay must not call an LLM")


def test_static_generality_rejects_recordings_and_paths() -> None:
    recorded = (
        "items = [\n"
        "    ('热点一', '1'),\n"
        "    ('热点二', '2'),\n"
        "    ('热点三', '3'),\n"
        "    ('热点四', '4'),\n"
        "]\n"
    )
    report = abstraction.static_source_check(recorded)
    assert report["ok"] is False
    assert any("业务数据大列表" in item for item in report["violations"])

    paths = (
        "import json\n"
        "DATA = '/Users/futuredeng/.panguverse/explore/e-123/session.json'\n"
        "open('/tmp/report.html', 'w').write('x')\n"
    )
    report = abstraction.static_source_check(paths)
    assert report["ok"] is False
    assert any("绝对路径" in item for item in report["violations"])
    assert any("探索会话/快照" in item for item in report["violations"])

    general = (
        "from pathlib import Path\n"
        "def run(ctx=None, source='', output_dir='.'):\n"
        "    base = Path(output_dir)\n"
        "    base.mkdir(parents=True, exist_ok=True)\n"
        "    (base / 'report.html').write_text('<h1>' + str(source)[:20] + '</h1>', encoding='utf-8')\n"
        "    return {'report_html': 'report.html'}\n"
    )
    assert abstraction.static_source_check(general)["ok"] is True


def test_abstract_script_derives_optional_inputs_from_run_signature() -> None:
    source = (
        "from pathlib import Path\n"
        "def run(source, *, limit=30, title='热点新闻报告', output_dir='.', flag: bool = False):\n"
        "    base = Path(output_dir)\n"
        "    base.mkdir(parents=True, exist_ok=True)\n"
        "    (base / 'report.html').write_text(str(source) + title + str(limit), encoding='utf-8')\n"
        "    return {'report_html': 'report.html'}\n"
    )
    llm = StaticLLM(
        {
            "description": "defaulted inputs",
            "source": source,
            "contract": {
                "inputs": {
                    "source": "string",
                    "limit": "int",
                    "title": "string",
                    "output_dir": "string",
                    "flag": "boolean",
                    "odd": "string?",
                },
                "outputs": {"report_html": "string"},
                "output_paths": {"report_html": "report.html"},
            },
            "checks": [],
        }
    )
    result = abstraction.abstract_script(
        llm,
        intent="生成热点报告",
        script={"path": "scripts/report.py", "seq": 1},
        plan_node=None,
        checks=[],
    )
    inputs = result["contract"]["inputs"]
    assert "source" in inputs  # 签名里无默认值 -> 必填
    for name in ("limit", "title", "output_dir", "flag"):
        assert f"{name}?" in inputs, (name, inputs)
    assert "odd?" in inputs  # 类型后缀 ? 也视为可选

    from panguverse.core.atom import Atom

    atom = Atom(name="repro", fn=lambda **_: {}, inputs=inputs)
    assert atom.required == ("source",), atom.required


def test_plan_requires_a_semantic_check() -> None:
    digest = {
        "intent": "生成报告",
        "success_checks": [{"type": "file_exists", "path": "report.html"}],
        "atom_steps": [],
        "scripts": [],
        "failed_steps": [],
    }
    plan = {
        "summary": "x",
        "nodes": [{"id": "n1", "atom": "fs_write_text", "params": {"path": "report.html", "text": "x"}}],
        "scripts": [],
        "checks": [{"id": "exists", "type": "file_exists", "artifact": "report.html"}],
    }
    catalog = [{"name": "fs_write_text", "inputs": {}, "outputs": {}}]
    with pytest.raises(abstraction.AbstractionError) as excinfo:
        abstraction.plan_crystallization(
            StaticLLM(plan), intent="生成报告", digest=digest, catalog=catalog
        )
    assert excinfo.value.code == "crystallize_plan_invalid"
    # A second LLM round is attempted with feedback, then fails again.
    assert "语义检查" in excinfo.value.message or "file_exists" in excinfo.value.message


def _write_module(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


def test_generality_dynamic_rejects_replayed_recording() -> None:
    session = Path(tempfile.mkdtemp(prefix="pv-general-session-"))
    (session / "report.html").write_text("RECORDED-CONTENT", encoding="utf-8")
    contract = {
        "inputs": {"source?": "string"},
        "outputs": {"report_html": "string"},
        "output_paths": {"report_html": "report.html"},
    }
    constant = (
        "from pathlib import Path\n"
        "def run(ctx=None, source='', output_dir='.'):\n"
        "    base = Path(getattr(ctx, 'workspace', '.') or '.')\n"
        "    (base / 'report.html').write_text('RECORDED-CONTENT', encoding='utf-8')\n"
        "    return {'report_html': 'report.html'}\n"
    )
    module = Path(tempfile.mkdtemp(prefix="pv-general-mod-")) / "constant.py"
    _write_module(module, constant)
    report = abstraction.generality_check(
        module_path=module,
        source=constant,
        contract=contract,
        session_dir=session,
        recorded_outputs={"report_html": "report.html"},
    )
    assert report.ok is False
    assert report.dynamic["matched_recordings"] == ["report.html"]

    general = (
        "from pathlib import Path\n"
        "def run(ctx=None, source='', output_dir='.'):\n"
        "    base = Path(getattr(ctx, 'workspace', '.') or '.')\n"
        "    (base / 'report.html').write_text('<h1>' + str(source)[:40] + '</h1>', encoding='utf-8')\n"
        "    return {'report_html': 'report.html'}\n"
    )
    _write_module(module, general)
    report = abstraction.generality_check(
        module_path=module,
        source=general,
        contract=contract,
        session_dir=session,
        recorded_outputs={"report_html": "report.html"},
    )
    assert report.ok is True, report.to_dict()


def test_graph_has_data_edges_and_semantic_asserts() -> None:
    digest = {
        "intent": "抓取并生成报告",
        "success_checks": [
            {"type": "min_items", "artifact": "report.json", "min_items": 3}
        ],
        "atom_steps": [
            {
                "seq": 1,
                "action_index": 1,
                "atom": "http_request",
                "params": {"url": "https://example.com"},
                "result_keys": ["text", "status_code"],
                "status": 200,
            }
        ],
        "scripts": [],
        "failed_steps": [],
    }
    new_atoms = {
        "build_report": {
            "name": "build_report",
            "pack": "explore_e_test",
            "description": "render",
            "inputs": {"source?": "string"},
            "outputs": {"report_json": "string"},
            "output_paths": {"report_json": "report.json"},
            "effects": ["write"],
            "idempotent": True,
            "sha256": "a" * 64,
            "source_script": "scripts/build.py",
        }
    }
    plan = {
        "summary": "http -> render",
        "nodes": [
            {"id": "n1", "role": "fetch", "atom": "http_request", "trace_seq": 1},
            {
                "id": "n2",
                "role": "render",
                "atom": "new:build_report",
                "params": {"source": "${n1.text}"},
            },
        ],
        "scripts": [
            {"seq": 2, "path": "scripts/build.py", "action": "abstract", "as": "build_report"}
        ],
        "checks": [
            {"id": "items>=3", "type": "min_items", "artifact": "report.json", "min_items": 3}
        ],
    }
    session = Path(tempfile.mkdtemp(prefix="pv-graph-session-"))
    graph = abstraction.build_graph_from_plan(
        plan=plan,
        digest=digest,
        session_dir=session,
        workspace=session,
        eid="e-12345678",
        intent="抓取并生成报告",
        name="",
        new_atoms=new_atoms,
        pending=[],
        dropped_steps=[],
        registry=default_registry(),
    )
    atoms = [node["atom"] for node in graph["nodes"]]
    assert atoms == ["http_request", "build_report", "assert_file_min_items"]
    assert any(edge["kind"] == "data" and edge["from"] == "s1_http_request" for edge in graph["edges"])
    assert graph["checks"] and graph["checks"][0]["type"] == "min_items"


def test_crystallize_rejects_hardcoded_recording_as_not_general(tmp_path: Path) -> None:
    from test_web_explore import AutoCrystallizeLLM, FakeLLM, call, wait_explore

    from panguverse.web.api import WebAPI

    script = (
        "from pathlib import Path\n"
        "items = [\n"
        "    ('热点一', '1'),\n"
        "    ('热点二', '2'),\n"
        "    ('热点三', '3'),\n"
        "    ('热点四', '4'),\n"
        "]\n"
        "Path('report.html').write_text(''.join(i[0] for i in items), encoding='utf-8')\n"
    )
    decisions = [
        {"action": "script", "path": "scripts/report.py", "code": script, "thought": "录制"},
        {
            "action": "final",
            "checks": [{"id": "html", "type": "file_exists", "path": "report.html"}],
            "thought": "落盘",
        },
    ]
    root = tmp_path / "ws"
    root.mkdir()
    api = WebAPI(
        root,
        crystallize_llm=AutoCrystallizeLLM(),
        explore_llm=FakeLLM(decisions),
        explore_registry=default_registry(),
    )
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "生成报告", "max_steps": 4}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    status, draft, _ = call(
        api, "POST", f"/api/studio/explore/{session['eid']}/crystallize", {}
    )
    assert status == 422, draft
    assert draft["error"]["code"] == "not_general"
    pending = draft["error"].get("pending_atoms") or []
    assert pending and pending[0]["reason"] == "not_general"
    assert any(
        "业务数据大列表" in item
        for item in pending[0]["generality"]["violations"]
    )


def test_replay_uses_clean_workspace_after_explore_is_deleted(tmp_path: Path) -> None:
    from test_web_explore import AutoCrystallizeLLM, FakeLLM, call, wait_explore

    from panguverse.core.workspace_packs import workspace_registry
    from panguverse.web.api import WebAPI

    script = (
        "from pathlib import Path\n"
        "Path('reports').mkdir(parents=True, exist_ok=True)\n"
        "letters = 'abc'\n"
        "Path('reports/report.html').write_text('<h1>' + letters + '</h1>', encoding='utf-8')\n"
    )
    decisions = [
        {"action": "script", "path": "scripts/report.py", "code": script, "thought": "生成"},
        {
            "action": "final",
            "checks": [{"id": "html", "type": "file_exists", "path": "reports/report.html"}],
            "thought": "落盘",
        },
    ]
    root = tmp_path / "ws"
    root.mkdir()
    api = WebAPI(
        root,
        crystallize_llm=AutoCrystallizeLLM(),
        explore_llm=FakeLLM(decisions),
        explore_registry=default_registry(),
    )
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "生成报告", "max_steps": 4}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    status, draft, _ = call(
        api, "POST", f"/api/studio/explore/{session['eid']}/crystallize", {}
    )
    assert status == 200, draft
    assert draft["llm_calls_replay"] == 0

    # The workflow must be self-contained: delete every explore artifact.
    shutil.rmtree(root / ".panguverse" / "explore" / session["eid"])

    workflow = studio.read_workflow(root, draft["wid"])
    revision = studio.read_revision(root, draft["wid"], draft["rev"])
    assert workflow["abstraction_plan"]
    assert workflow["atoms_created"]
    assert workflow["llm_calls_replay"] == 0
    assert "dropped_steps" in workflow
    atom_source = Path(workflow["atoms_created"][0]["path"]).read_text(encoding="utf-8")
    assert ".panguverse" not in atom_source
    assert "input_files" not in json.dumps(revision["nodes"], ensure_ascii=False)

    replay = agent_exec.replay_graph(
        nodes=revision["nodes"],
        registry=workspace_registry(root, default_registry()),
        sandbox=tmp_path / "ignored-clean",
        checks=revision["replay_checks"],
        clean=True,
    )
    assert replay["status"] == "passed", replay
    assert replay["llm_calls"] == 0


def test_crystallize_without_llm_returns_503(tmp_path: Path) -> None:
    session = tmp_path / "s"
    session.mkdir()
    (session / "session.json").write_text(
        json.dumps(
            {
                "eid": "e-12345678",
                "intent": "x",
                "verdict": "passed",
                "status": "succeeded",
                "checks": [],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )
    (session / "success.json").write_text(
        json.dumps({"verdict": "passed", "status": "succeeded", "checks": []}),
        encoding="utf-8",
    )
    explore_root = tmp_path / ".panguverse" / "explore"
    explore_root.mkdir(parents=True)
    target = explore_root / "e-12345678"
    shutil.copytree(session, target)
    with pytest.raises(explore.StudioError) as excinfo:
        explore.crystallize(tmp_path, "e-12345678", {}, llm=None)
    assert excinfo.value.code == "llm_required_for_crystallize"
    assert excinfo.value.status == 503
