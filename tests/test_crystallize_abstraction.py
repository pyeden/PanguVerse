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
    render = next(node for node in graph["nodes"] if node["atom"] == "build_report")
    assert render["produces"] == {
        "report_json": {"path": "report.json", "type": "json"}
    }
    assert graph["produces"] == [
        {
            "node_id": render["id"],
            "name": "report_json",
            "path": "report.json",
            "type": "json",
        }
    ]


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


def test_generality_runtime_crash_is_not_general() -> None:
    """崩溃原子必须判成不通用：异常不能再被当作“哈希不同”的证据。"""
    session = Path(tempfile.mkdtemp(prefix="pv-general-crash-session-"))
    contract = {
        "inputs": {"source?": "string"},
        "outputs": {"report_html": "string"},
        "output_paths": {"report_html": "report.html"},
    }
    crash = (
        "from pathlib import Path\n"
        "HTML_TEMPLATE = '<style>.g{background:linear-gradient(0deg,#fff 0%,#000 45%)}</style><h1>%s</h1>'\n"
        "def run(ctx=None, source='', output_dir='.'):\n"
        "    base = Path(getattr(ctx, 'workspace', '.') or '.')\n"
        "    (base / 'report.html').write_text(HTML_TEMPLATE % '热点', encoding='utf-8')\n"
        "    return {'report_html': 'report.html'}\n"
    )
    module = Path(tempfile.mkdtemp(prefix="pv-general-crash-mod-")) / "crash.py"
    _write_module(module, crash)

    report = abstraction.generality_check(
        module_path=module,
        source=crash,
        contract=contract,
        session_dir=session,
        recorded_outputs={"report_html": "report.html"},
    )
    assert report.ok is False
    assert report.dynamic["ok"] is False
    assert report.dynamic["matched_recordings"] == []
    assert any(item.startswith("runtime_error:") for item in report.violations)
    assert "unsupported format character" in report.dynamic["error"]


def test_generality_requires_every_declared_output() -> None:
    """成功返回但没有写出全部声明产物，也必须判成不通用。"""
    session = Path(tempfile.mkdtemp(prefix="pv-general-missing-session-"))
    contract = {
        "inputs": {"source?": "string"},
        "outputs": {"report_html": "string", "items": "string"},
        "output_paths": {"report_html": "report.html", "items": "items.json"},
    }
    partial = (
        "from pathlib import Path\n"
        "def run(ctx=None, source='', output_dir='.'):\n"
        "    base = Path(getattr(ctx, 'workspace', '.') or '.')\n"
        "    (base / 'report.html').write_text('<h1>ok</h1>', encoding='utf-8')\n"
        "    return {'report_html': 'report.html', 'items': 'items.json'}\n"
    )
    module = Path(tempfile.mkdtemp(prefix="pv-general-missing-mod-")) / "partial.py"
    _write_module(module, partial)

    report = abstraction.generality_check(
        module_path=module,
        source=partial,
        contract=contract,
        session_dir=session,
        recorded_outputs={},
    )
    assert report.ok is False
    assert any(
        item.startswith("missing_output: items") for item in report.violations
    )


class PercentCssCrystallizeLLM:
    """Plans like the normal test double, but abstracts into a broken %-template."""

    available = True

    def status(self) -> dict[str, Any]:
        return {"available": True, "model": "percent-css", "backend": "test"}

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        if "原子抽象器" in system or user.startswith("待抽象脚本"):
            return {
                "description": "生成带 CSS 渐变的报告",
                "source": (
                    "from pathlib import Path\n"
                    "HTML_TEMPLATE = '<style>.g{background:linear-gradient(0deg,#fff 0%,#000 45%)}</style><h1>%s</h1>'\n"
                    "def run(ctx=None, source='', output_dir='.'):\n"
                    "    base = Path(getattr(ctx, 'workspace', '.') or '.')\n"
                    "    (base / 'report.html').write_text(HTML_TEMPLATE % '热点', encoding='utf-8')\n"
                    "    return {'report_html': 'report.html'}\n"
                ),
                "contract": {
                    "inputs": {"source": "string?", "output_dir": "string?"},
                    "outputs": {"report_html": "string"},
                    "output_paths": {"report_html": "report.html"},
                    "effects": ["write"],
                    "idempotent": True,
                    "failure_class": "transient",
                },
                "checks": [
                    {
                        "id": "html",
                        "type": "regex",
                        "artifact": "report.html",
                        "pattern": "热点",
                    }
                ],
            }
        from test_web_explore import AutoCrystallizeLLM

        return AutoCrystallizeLLM()._plan(user)


def test_percent_css_template_atom_is_rejected(tmp_path: Path) -> None:
    from test_web_explore import FakeLLM, call, wait_explore

    from panguverse.core.registry import default_registry
    from panguverse.web.api import WebAPI

    script = (
        "from pathlib import Path\n"
        "Path('report.html').write_text('<h1>今日热点</h1>', encoding='utf-8')\n"
    )
    decisions = [
        {"action": "script", "path": "scripts/report.py", "code": script, "thought": "生成报告"},
        {
            "action": "final",
            "checks": [{"id": "html", "type": "file_exists", "path": "report.html"}],
            "thought": "产物已落盘",
        },
    ]
    root = tmp_path / "ws"
    root.mkdir()
    api = WebAPI(
        root,
        crystallize_llm=PercentCssCrystallizeLLM(),
        explore_llm=FakeLLM(decisions),
        explore_registry=default_registry(),
    )
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "生成热点报告", "max_steps": 4}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "succeeded", session

    status, body, _ = call(
        api, "POST", f"/api/studio/explore/{session['eid']}/crystallize", {}
    )
    assert status == 422, body
    assert body["error"]["code"] == "not_general"
    assert studio.list_drafts(root)["drafts"] == []
    pending = body["error"]["pending_atoms"]
    assert pending and pending[0]["reason"] == "not_general"
    generality = pending[0]["generality"]
    assert generality["ok"] is False
    assert any(
        "runtime_error" in item for item in generality["violations"]
    )


def test_write_atom_derives_produces_from_output_paths(tmp_path: Path) -> None:
    from panguverse.core.workspace_packs import workspace_registry

    contract = {
        "inputs": {"output_dir?": "string?"},
        "outputs": {"news_data": "string", "report_md": "string"},
        "output_paths": {"news_data": "news_data.json", "report_md": "report.md"},
        "effects": ["write"],
        "idempotent": True,
    }
    source = (
        "def run(ctx=None, output_dir='.'):\n"
        "    return {'news_data': 'news_data.json', 'report_md': 'report.md'}\n"
    )
    info = abstraction.write_atom(
        workspace=tmp_path,
        eid="e-produces",
        name="hot_report",
        source=source,
        contract=contract,
        script={},
        description="生成报告",
        generality=abstraction.GeneralityReport(ok=True),
    )
    assert info["produces"] == {
        "news_data": {"path": "news_data.json", "type": "json"},
        "report_md": {"path": "report.md", "type": "markdown"},
    }
    abstraction.write_pack_manifest(tmp_path, "e-produces", [info])
    manifest = json.loads(
        (tmp_path / ".panguverse" / "packs" / "explore_e_produces" / "pack.json").read_text(
            encoding="utf-8"
        )
    )
    entry = next(item for item in manifest["atoms"] if item["name"] == "hot_report")
    assert entry["produces"] == info["produces"]
    atom = workspace_registry(tmp_path).get("hot_report")
    assert atom.produces == ("news_data", "report_md")
