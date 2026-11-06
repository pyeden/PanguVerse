"""Explore-first P0：会话/工具循环/校验/固化/重放的契约测试。"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from panguverse.core.atom import Atom
from panguverse.web import studio
from panguverse.axe import agent_exec
from panguverse.web.api import WebAPI
from panguverse.web.explore import EID_RE

HTML = """<!doctype html><html><head><title>百度热搜</title></head><body>
<div class="hot">热搜第一名：某地暴雨</div>
<div class="hot">热搜第二名：新手机发布</div>
<div class="hot">热搜第三名：联赛夺冠</div>
</body></html>"""


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any, bytes]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    route, _, query = path.partition("?")
    response = api.handle(method, route, query, raw)
    body = response.body or b""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        parsed = None
    return response.status, parsed, body


def wait_explore(api: WebAPI, eid: str, *, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = call(api, "GET", f"/api/studio/explore/{eid}")
        assert status == 200, body
        if body.get("done"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"探索 {eid} 未在 {timeout}s 内结束")


# --------------------------------------------------------------------------- #
# 假工具：真实 HTTP + 真实文本提取，但不依赖 Chrome
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # noqa: D401
        return


@pytest.fixture()
def hot_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hot"
    finally:
        server.shutdown()
        server.server_close()


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _web_open(ctx, url: str) -> dict[str, Any]:
    with _DIRECT.open(url, timeout=5) as response:
        html = response.read().decode("utf-8", errors="replace")
        return {
            "status": response.status,
            "html": html,
            "text": re.sub(r"<[^>]+>", "", html),
            "title": "百度热搜",
        }


def _web_get_text(ctx, html: str) -> dict[str, Any]:
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    return {"text": text, "chars": len(text)}


class FakeRegistry:
    def __init__(self, atoms: list[Atom]) -> None:
        self._atoms = {atom.name: atom for atom in atoms}

    def list(self):
        return sorted(self._atoms.values(), key=lambda atom: atom.name)

    def get(self, name: str) -> Atom:
        return self._atoms[name]


def make_registry(counting: list[int] | None = None) -> FakeRegistry:
    def web_open(ctx, url: str) -> dict[str, Any]:
        if counting is not None:
            counting[0] += 1
            if counting[0] > 1:
                raise RuntimeError("重放时目标不可用")
        return _web_open(ctx, url)

    return FakeRegistry(
        [
            Atom(
                name="web_open",
                fn=web_open,
                description="打开网页",
                inputs={"url": "string"},
                outputs={"status": "integer", "html": "string", "text": "string", "title": "string"},
                required=("url",),
                effects=("read",),
                domain="web_browser",
                category="web",
            ),
            Atom(
                name="web_get_text",
                fn=_web_get_text,
                description="提取正文",
                inputs={"html": "string"},
                outputs={"text": "string", "chars": "integer"},
                required=("html",),
                effects=("pure",),
                domain="web_browser",
                category="web",
            ),
        ]
    )


class FakeLLM:
    available = True

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"available": True, "model": "fake-llm", "backend": "test"}

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.prompts.append(user)
        if not self.decisions:
            raise AssertionError("FakeLLM 决策序列已耗尽")
        return json.loads(json.dumps(self.decisions.pop(0)))


def baidu_decisions(url: str) -> list[dict[str, Any]]:
    return [
        {
            "action": "tool",
            "tool": "web_open",
            "params": {"url": url},
            "thought": "先打开百度热搜页面",
        },
        {
            "action": "tool",
            "tool": "web_get_text",
            "params": {"html": "${step1.html}"},
            "thought": "提取热搜正文",
        },
        {
            "action": "final",
            "checks": [
                {"id": "titles>=3", "type": "text_contains", "value": "热搜第三名", "step": 2},
                {"id": "status200", "type": "status_code", "equals": 200, "step": 1},
                {"id": "regex-rank", "type": "regex", "pattern": "热搜第[一二三]名", "step": 2},
            ],
            "thought": "标题数量与状态码均有证据，结束",
        },
    ]


def new_api(tmp_path: Path, *, llm: Any, registry: FakeRegistry) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    return WebAPI(root, explore_llm=llm, explore_registry=registry)


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #
def test_explore_baidu_hot_search_crystallize_and_replay(tmp_path: Path, hot_server: str) -> None:
    api = new_api(tmp_path, llm=FakeLLM(baidu_decisions(hot_server)), registry=make_registry())

    status, body, _ = call(
        api,
        "POST",
        "/api/studio/explore",
        {"intent": "打开百度热搜并提取热搜标题", "max_steps": 6},
    )
    assert status == 202, body
    eid = body["eid"]
    assert EID_RE.match(eid)

    session = wait_explore(api, eid)
    assert session["status"] == "succeeded", session
    assert session["verdict"] == "passed"
    assert len(session["checks"]) == 3
    assert all(item["ok"] for item in session["checks"])
    assert all(item["evidence"] for item in session["checks"])

    types = [event["type"] for event in session["events"]]
    assert types == [
        "thought",
        "tool_call",
        "tool_result",
        "thought",
        "tool_call",
        "tool_result",
        "thought",
        "verify",
        "end",
    ], types
    seqs = [event["seq"] for event in session["events"]]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))

    # cursor 增量读取
    status, page, _ = call(api, "GET", f"/api/studio/explore/{eid}?cursor=6")
    assert status == 200
    assert page["events"][0]["seq"] == 7
    assert page["cursor"] == session["cursor"]

    # 产物可读 + 路径穿越拒绝
    artifacts = session["artifacts"]
    assert artifacts
    status, _, raw = call(
        api, "GET", f"/api/studio/explore/{eid}/artifact?path={artifacts[0]['path']}"
    )
    assert status == 200 and len(raw) > 0
    status, body, _ = call(
        api, "GET", f"/api/studio/explore/{eid}/artifact?path=../../etc/passwd"
    )
    assert status == 400, body
    assert body["error"]["code"] == "bad_request"

    # 固化 + 无 LLM 重放
    status, draft, _ = call(api, "POST", f"/api/studio/explore/{eid}/crystallize", {})
    assert status == 200, draft
    assert draft["replay"]["status"] == "passed", draft["replay"]
    assert draft["atoms_created"] == []
    assert draft["pending_atoms"] == []
    nodes = draft["graph"]["nodes"]
    assert [node["atom"] for node in nodes] == ["web_open", "web_get_text"]
    assert nodes[0]["params"]["url"] == hot_server
    assert nodes[1]["params"]["html"] == "${s1_web_open_html}"
    assert nodes[0]["capture"]["s1_web_open_html"]["path"] == "html"

    status, workflow, _ = call(api, "GET", f"/api/studio/{draft['wid']}")
    assert status == 200
    assert workflow["origin"] == "explore"
    assert workflow["explore_id"] == eid
    assert workflow["replay_status"] == "replay_passed"

    status, listing, _ = call(api, "GET", "/api/studio/explore?limit=10")
    assert status == 200
    assert listing["sessions"][0]["eid"] == eid


def test_explore_requires_llm_and_never_fakes_execution(
    tmp_path: Path, hot_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("PANGUVERSE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    api = WebAPI(root, explore_registry=make_registry())
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开百度热搜", "max_steps": 3}
    )
    assert status == 503, body
    assert body["error"]["code"] == "llm_required"
    assert not (root / ".panguverse" / "explore").exists()


def test_explore_budget_exceeded(tmp_path: Path, hot_server: str) -> None:
    decisions = [
        {
            "action": "tool",
            "tool": "web_open",
            "params": {"url": f"{hot_server}?i={index}"},
            "thought": "再看一次",
        }
        for index in range(5)
    ]
    api = new_api(tmp_path, llm=FakeLLM(decisions), registry=make_registry())
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开百度热搜", "max_steps": 2}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "budget_exceeded"
    assert session["verdict"] == "failed"
    assert [event["type"] for event in session["events"]].count("tool_call") == 2
    status, body, _ = call(api, "POST", f"/api/studio/explore/{session['eid']}/crystallize", {})
    assert status == 409, body
    assert body["error"]["code"] == "not_succeeded"


def test_given_criteria_auto_verify_without_final(tmp_path: Path, hot_server: str) -> None:
    """给定 criteria 时，tool_result 后立即自动 verify，不依赖 LLM 发 final。"""
    llm = FakeLLM(
        [
            {
                "action": "tool",
                "tool": "web_open",
                "params": {"url": hot_server},
                "thought": "打开页面",
            }
        ]
    )
    criteria = [
        {"id": "status", "type": "status_code", "equals": 200},
        {"id": "hot", "type": "text_contains", "value": "热搜"},
        {"id": "rank", "type": "regex", "pattern": "热搜第[一二三]名"},
    ]
    api = new_api(tmp_path, llm=llm, registry=make_registry())
    status, body, _ = call(
        api,
        "POST",
        "/api/studio/explore",
        {"intent": "打开百度热搜", "success_criteria": criteria, "max_steps": 6},
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "succeeded", session
    assert all(item["ok"] for item in session["checks"])
    assert len(llm.prompts) == 1  # 没有第二次决策，更没有 final
    types = [event["type"] for event in session["events"]]
    assert types == ["thought", "tool_call", "tool_result", "verify", "end"]
    assert session["summary"].get("auto_verified") is True


def test_check_default_binding_ignores_later_non_evidence_observation() -> None:
    from panguverse.axe.agent_exec import check_observations

    observations = [
        {
            "index": 1,
            "tool": "web_open",
            "ok": True,
            "result": {"status": 200, "text": "百度热搜 热搜指数 7904280"},
        },
        {"index": 2, "tool": "fs_list", "ok": True, "result": {"count": 2, "files": []}},
    ]
    specs = [
        {"id": "status", "type": "status_code", "equals": 200},
        {"id": "hot", "type": "text_contains", "value": "热搜"},
        {"id": "rank", "type": "regex", "pattern": "热搜指数\\s*\\d+"},
    ]
    results = check_observations(specs, observations=observations, base_dir=Path("/tmp"))
    assert [item["ok"] for item in results] == [True, True, True], results


def test_duplicate_tool_call_is_skipped_with_feedback(tmp_path: Path, hot_server: str) -> None:
    decision = {
        "action": "tool",
        "tool": "web_open",
        "params": {"url": hot_server},
        "thought": "重复打开",
    }
    api = new_api(
        tmp_path,
        llm=FakeLLM([decision, dict(decision)]),
        registry=make_registry(),
    )
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开页面", "max_steps": 2}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "budget_exceeded"
    assert len([event for event in session["events"] if event["type"] == "tool_call"]) == 1
    skipped = [
        event
        for event in session["events"]
        if event["type"] == "tool_result" and event.get("skipped")
    ]
    assert len(skipped) == 1
    assert "已执行过" in skipped[0]["error"]
    assert any(
        event["type"] == "thought" and "已执行过" in event.get("text", "")
        for event in session["events"]
    )


def test_explore_script_path_traversal_is_rejected(tmp_path: Path, hot_server: str) -> None:
    decisions = [
        {
            "action": "script",
            "path": "../escape.py",
            "code": "print('x')",
            "thought": "尝试越界写脚本",
        }
    ]
    api = new_api(tmp_path, llm=FakeLLM(decisions), registry=make_registry())
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "写脚本", "max_steps": 1}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "error"
    assert "路径穿越" in session["error"].get("message", "")
    assert not (tmp_path / "ws" / ".panguverse" / "escape.py").exists()


def test_tool_catalog_narrows_scope_by_intent() -> None:
    from panguverse.axe.agent_exec import tool_catalog_for_intent
    from panguverse.core.registry import default_registry

    registry = default_registry()
    web_names = {
        item["name"]
        for item in tool_catalog_for_intent(
            registry, "打开百度网站，提取今天的热点新闻", allow_scripts=True
        )
    }
    assert "web_open" in web_names
    assert "http_request" in web_names
    assert "fs_write_text" in web_names
    assert "android_devices" not in web_names
    assert "ios_sim_list" not in web_names

    api_names = {
        item["name"]
        for item in tool_catalog_for_intent(
            registry, "测试接口健康检查", domain_pack="api_testing", allow_scripts=True
        )
    }
    assert "http_request" in api_names
    assert "assert_json" in api_names
    assert "fs_write_text" in api_names
    assert "web_open" not in api_names
    assert "android_devices" not in api_names


def test_crystallize_filters_out_of_scope_atoms() -> None:
    from panguverse.axe.agent_exec import build_crystallized_graph
    from panguverse.core.registry import default_registry

    events = [
        {
            "seq": 1,
            "type": "tool_call",
            "tool": "android_devices",
            "params": {},
        },
        {"seq": 2, "type": "tool_result", "tool": "android_devices", "ok": True, "digest": {}},
        {
            "seq": 3,
            "type": "tool_call",
            "tool": "http_request",
            "params": {"url": "https://top.baidu.com/board?tab=realtime", "method": "GET"},
        },
        {"seq": 4, "type": "tool_result", "tool": "http_request", "ok": True, "digest": {"status_code": 200}},
    ]
    graph = build_crystallized_graph(
        events,
        eid="e-12345678",
        intent="打开百度网站，提取今天的热点新闻",
        checks=[
            {"id": "status", "type": "status_code", "equals": 200, "step": 1},
            {"id": "hot", "type": "text_contains", "value": "热搜", "step": 2},
        ],
        registry=default_registry(),
    )
    assert [node["atom"] for node in graph["nodes"]] == ["http_request"]
    assert graph["nodes"][0]["params"]["url"] == "https://top.baidu.com/board?tab=realtime"
    assert [item["name"] for item in graph["out_of_scope"]] == ["android_devices"]
    assert any(
        item.get("status") == "out_of_scope" and item.get("name") == "android_devices"
        for item in graph["pending_atoms"]
    )


def test_crystallize_keeps_script_steps_as_pending_atoms() -> None:
    events = [
        {"seq": 1, "type": "tool_call", "tool": "web_open", "params": {"url": "http://x"}},
        {"seq": 2, "type": "tool_result", "tool": "web_open", "ok": True, "digest": {}},
        {
            "seq": 3,
            "type": "script",
            "path": "scripts/rank.py",
            "code_sha256": "a" * 64,
            "exit_code": 0,
            "ok": True,
        },
    ]
    graph = agent_exec.build_crystallized_graph(
        events,
        eid="e-12345678",
        intent="排名",
        checks=[{"id": "c1", "type": "status_code", "equals": 200, "step": 1}],
    )
    assert [node["atom"] for node in graph["nodes"]] == ["web_open"]
    assert graph["pending_atoms"] == [
        {
            "name": "rank",
            "path": "scripts/rank.py",
            "code_sha256": "a" * 64,
            "exit_code": 0,
            "status": "pending",
            "kind": "script",
            "reason": "P0 只固化已有原子，脚本步骤待 P1 封装",
            "trace_seq": 3,
        }
    ]
    assert graph["checks"][0]["step_id"] == "s1_web_open"


def test_replay_failure_blocks_publish(tmp_path: Path, hot_server: str) -> None:
    counting = [0]
    api = new_api(
        tmp_path,
        llm=FakeLLM(baidu_decisions(hot_server)),
        registry=make_registry(counting=counting),
    )
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开百度热搜并提取标题", "max_steps": 4}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "succeeded", session

    status, draft, _ = call(api, "POST", f"/api/studio/explore/{session['eid']}/crystallize", {})
    assert status == 200, draft
    assert draft["replay"]["status"] == "failed"

    workflow = studio.read_workflow(api.root, draft["wid"])
    assert workflow["replay_status"] == "replay_failed"

    status, body, _ = call(
        api, "POST", f"/api/studio/{draft['wid']}/state", {"to": "approved", "note": "试批准"}
    )
    assert status == 200, body
    status, body, _ = call(
        api, "POST", f"/api/studio/{draft['wid']}/publish", {"rev": 0, "message": "发布"}
    )
    assert status == 409, body
    assert body["error"]["code"] == "replay_failed"


# --------------------------------------------------------------------------- #
# P1: final checks 修复反馈 + 脚本原子固化
# --------------------------------------------------------------------------- #
def test_final_checks_invalid_gets_one_feedback_then_succeeds(
    tmp_path: Path, hot_server: str
) -> None:
    decisions = [
        {
            "action": "tool",
            "tool": "web_open",
            "params": {"url": hot_server},
            "thought": "打开页面",
        },
        {
            "action": "final",
            "checks": [{"id": "bad", "type": "text_contains"}],
            "thought": "先尝试收尾（缺 value）",
        },
        {
            "action": "final",
            "checks": [{"id": "ok", "type": "text_contains", "value": "热搜", "step": 1}],
            "thought": "补上 value 后收尾",
        },
    ]
    llm = FakeLLM(decisions)
    api = new_api(tmp_path, llm=llm, registry=make_registry())
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开百度热搜", "max_steps": 5}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "succeeded", session
    assert session["verdict"] == "passed"
    assert all(item["ok"] for item in session["checks"])
    assert "非法" in llm.prompts[2]
    assert any(event["type"] == "feedback" for event in session["events"])


def test_final_checks_still_invalid_fails_with_readable_reason(
    tmp_path: Path, hot_server: str
) -> None:
    decisions = [
        {
            "action": "tool",
            "tool": "web_open",
            "params": {"url": hot_server},
            "thought": "打开页面",
        },
        {
            "action": "final",
            "checks": [{"id": "bad", "type": "text_contains"}],
            "thought": "第一次非法",
        },
        {
            "action": "final",
            "checks": [{"id": "bad", "type": "text_contains"}],
            "thought": "仍然非法",
        },
    ]
    api = new_api(tmp_path, llm=FakeLLM(decisions), registry=make_registry())
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开百度热搜", "max_steps": 5}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "failed", session
    assert session["status"] != "error"
    assert "final checks 连续非法" in session["summary"]["reason"]
    assert session["error"] == {}


def test_final_without_checks_prompts_for_checks_and_never_succeeds_silently(
    tmp_path: Path, hot_server: str
) -> None:
    decisions = [
        {
            "action": "tool",
            "tool": "web_open",
            "params": {"url": hot_server},
            "thought": "打开页面",
        },
        {"action": "final", "thought": "没有 checks", "checks": []},
        {
            "action": "final",
            "checks": [{"id": "ok", "type": "text_contains", "value": "热搜", "step": 1}],
            "thought": "补检查",
        },
    ]
    llm = FakeLLM(decisions)
    api = new_api(tmp_path, llm=llm, registry=make_registry())
    status, body, _ = call(
        api, "POST", "/api/studio/explore", {"intent": "打开百度热搜", "max_steps": 5}
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "succeeded", session
    assert "缺少可执行 checks" in llm.prompts[2]


def test_script_step_is_crystallized_into_workspace_atom_and_replayed(
    tmp_path: Path,
) -> None:
    from panguverse.core.registry import default_registry
    from panguverse.core.workspace_packs import load_workspace_packs

    script = (
        "import json\n"
        "items = [{'word': '热点一'}, {'word': '热点二'}]\n"
        "with open('input.json', 'w', encoding='utf-8') as f:\n"
        "    json.dump({'items': items}, f, ensure_ascii=False)\n"
        "with open('report.html', 'w', encoding='utf-8') as f:\n"
        "    f.write('<h1>今日热点</h1>' + ''.join(i['word'] for i in items))\n"
        "with open('report_items.json', 'w', encoding='utf-8') as f:\n"
        "    json.dump(items, f, ensure_ascii=False)\n"
        "print('source: input.json')\n"
    )
    decisions = [
        {"action": "script", "path": "scripts/build_report.py", "code": script, "thought": "生成报告"},
        {
            "action": "final",
            "checks": [
                {"id": "html", "type": "file_exists", "path": "report.html"},
                {"id": "items", "type": "file_exists", "path": "report_items.json"},
            ],
            "thought": "产物已落盘",
        },
    ]
    llm = FakeLLM(decisions)
    root = tmp_path / "ws"
    root.mkdir()
    api = WebAPI(root, explore_llm=llm, explore_registry=default_registry())
    status, body, _ = call(
        api,
        "POST",
        "/api/studio/explore",
        {"intent": "生成热点报告", "max_steps": 4},
    )
    assert status == 202, body
    session = wait_explore(api, body["eid"])
    assert session["status"] == "succeeded", session

    status, draft, _ = call(
        api, "POST", f"/api/studio/explore/{session['eid']}/crystallize", {}
    )
    assert status == 200, draft
    created = draft["atoms_created"]
    assert [item["name"] for item in created] == ["build_report"]
    atom = created[0]
    assert atom["effects"] == ["write"]
    assert atom["idempotent"] is True
    assert atom["failure_class"]
    assert atom["outputs"]["report_html"] == "string"
    assert atom["outputs"]["items"] == "string"
    assert atom["output_paths"]["report_html"] == "report.html"
    assert atom["output_paths"]["items"] == "report_items.json"
    atom_path = Path(atom["path"])
    assert atom_path.is_file()
    assert root / ".panguverse" / "packs" in atom_path.parents
    nodes = draft["graph"]["nodes"]
    assert [node["atom"] for node in nodes] == ["build_report"]
    assert nodes[0]["origin"] == "explore"
    assert nodes[0]["new_atom"] is True
    assert Path(nodes[0]["params"]["input_dir"]).parent.name == atom["pack"]
    assert draft["pending_atoms"] == []
    assert draft["replay"]["status"] == "passed", draft["replay"]
    packs = load_workspace_packs(root)
    assert any(pack.name == atom["pack"] for pack in packs)
