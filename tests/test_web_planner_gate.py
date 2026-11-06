"""诚实规划 + 语义校验 + 异步调试契约测试（v6.0 §2–§4）。"""

from __future__ import annotations

import json
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from panguverse.axe.plan import PlanRequest
from panguverse.axe.planner import LLMPlanner, RuleBasedPlanner
from panguverse.core.errors import PlanError
from panguverse.core.registry import default_registry
from panguverse.web import service, studio
from panguverse.web.api import WebAPI
from panguverse.web.graph import graph_from_plan

ORDER_INTENT = "登录下单支付"
HOME_INTENT = "打开 https://seres-ai.com 首页，获取首页的文本内容，给出首页的页面布局报告"


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    route, _, query = path.partition("?")
    response = api.handle(method, route, query, raw)
    body = json.loads(response.body.decode("utf-8")) if response.body else None
    return response.status, body


def wait_debug(api: WebAPI, sid: str, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    cursor = 0
    seen: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    while time.time() < deadline:
        status, body = call(api, "GET", f"/api/studio/debug/{sid}?cursor={cursor}")
        assert status == 200, body
        seen.extend(body.get("nodes") or [])
        cursor = max(cursor, int(body.get("cursor") or 0))
        last = body
        if body.get("done"):
            last["all_nodes"] = seen
            return last
        time.sleep(0.02)
    raise AssertionError(f"debug {sid} 未在 {timeout}s 内完成")


@pytest.fixture()
def api(tmp_path: Path) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    return WebAPI(root)


@pytest.fixture()
def page_url():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            body = (
                "<html><head><title>本地页面</title></head><body>"
                "<header><h1>首页</h1></header><main><p>正文内容</p>"
                "<a href='/x'>内部</a></main></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# T1：首页任务能正确表达
# --------------------------------------------------------------------------- #
def test_t1_homepage_intent_produces_web_research_graph(
    api: WebAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 未配置 LLM：计划只保留「抓取 → 正文 → 状态码断言」，不能生成必失败的 summary 节点
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "打开 https://seres-ai.com 首页取文本并给布局报告"},
    )
    assert status == 201, body
    assert body["planner"] in {"template", "rules", "llm"}
    assert body["plan_source"] == body["planner"] or body["plan_source"] in {"template", "rules", "llm"}
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["confidence"] <= 0.5
    assert isinstance(body["assumptions"], list)
    assert body["capability_gaps"] == []

    nodes = body["graph"]["nodes"]
    assert 3 <= len(nodes) <= 6
    atoms = [node["atom"] for node in nodes]
    for required in ("http_get_page", "html_extract_text", "assert_json"):
        assert required in atoms
    assert "llm_summarize" not in atoms
    assert not {"create_order", "pay_order"} & set(atoms)
    assert body["validation"]["ok"] is True
    assert body["validation"]["coverage"]["ratio"] >= 0.6

    assert body["domain_pack"] == "web_research"

    status, full = call(api, "GET", f"/api/studio/{body['wid']}")
    assert status == 200
    assert full["planner"] == body["planner"]
    assert full["plan_source"] == body["plan_source"]
    assert full["confidence"] == body["confidence"]
    assert full["domain_pack"] == "web_research"


def test_web_report_keeps_summary_when_llm_configured(
    api: WebAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置了 LLM 时仍允许追加总结节点（不发起真实调用）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": HOME_INTENT})
    assert status == 201, body
    atoms = [node["atom"] for node in body["graph"]["nodes"]]
    assert "llm_summarize" in atoms


# --------------------------------------------------------------------------- #
# 领域包白名单 / 缺少输入 / 单节点调试（工作台验收 2026-11-05）
# --------------------------------------------------------------------------- #
def test_registered_domain_pack_is_accepted(api: WebAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-1：注册表里存在的 web_browser 必须能建草案。"""
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    intent = "打开百度网页，获取今天的实时热点新闻 https://top.baidu.com/board?tab=realtime"
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": intent, "domain_pack": "web_browser"})
    assert status == 201, body
    assert body["domain_pack"] == "web_browser"
    atoms = [node["atom"] for node in body["graph"]["nodes"]]
    assert "web_open" in atoms
    assert "llm_summarize" not in atoms


def test_unknown_domain_pack_still_rejected(api: WebAPI) -> None:
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": "备份配置文件", "domain_pack": "nope"})
    assert status == 400
    assert body["error"]["code"] == "bad_request"


def test_missing_url_is_missing_inputs_not_capability_gap(
    api: WebAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-2：缺 URL 不是能力缺口，missing_atoms 不能列出已存在原子。"""
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # 用未知站点验证 missing_inputs 契约；已知站点（百度）现在会被别名表解析成 URL。
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "打开某某公司网页，获取今天的实时热点新闻"},
    )
    assert status == 422, body
    error = body["error"]
    assert error["code"] == "missing_inputs"
    assert error["missing_inputs"] == ["url"]
    assert error["missing_atoms"] == []
    assert error["autofilled"] == []
    assert error["param_gaps"] == []
    registry = default_registry()
    assert all(not registry.has(name) for name in error["missing_atoms"])
    assert "graph" not in body


def test_site_alias_resolves_baidu_hotnews(api: WebAPI) -> None:
    # 站点别名是数据表：百度 + 热点 → top.baidu，且显式 URL 优先。
    from panguverse.web import planner_gate

    analysis = planner_gate.analyze_intent("打开百度网站，提取今天的热点新闻")
    assert analysis.url == "https://top.baidu.com/board?tab=realtime"
    assert analysis.url_source == "site_alias"
    assert analysis.site == "baidu"
    assert analysis.missing_inputs == []

    plain = planner_gate.analyze_intent("打开百度网站看看")
    assert plain.url == "https://www.baidu.com"

    explicit = planner_gate.analyze_intent(
        "打开百度网站 https://example.com/hot 提取热点新闻"
    )
    assert explicit.url == "https://example.com/hot"
    assert explicit.url_source == "explicit"

    unknown = planner_gate.analyze_intent("打开某某公司网站，提取热点新闻")
    assert unknown.url == ""
    assert unknown.url_source == ""
    assert unknown.missing_inputs == ["url"]


def test_create_draft_autofills_every_url_port(api: WebAPI) -> None:
    # 自动填充按端口契约生效，不针对具体原子；审计同时给出 autofilled / param_gaps。
    intent = "打开百度网站，提取今天的热点新闻"
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": intent})
    assert status == 201, body
    assert body["url"] == "https://top.baidu.com/board?tab=realtime"
    assert body["url_source"] == "site_alias"
    assert body["autofilled"], body
    assert body["param_gaps"] == [], body
    for node in body["graph"]["nodes"]:
        if "url" in node["params"]:
            assert node["params"]["url"] == body["url"]
    assert all(item["source"] == "site_alias" for item in body["autofilled"])
    assert body["validation"]["ok"] is True

    # 显式 URL 覆盖站点默认值，并记 source=explicit
    status, explicit = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "打开百度网站 https://example.com/hot 提取热点新闻"},
    )
    assert status == 201, explicit
    assert explicit["url"] == "https://example.com/hot"
    assert explicit["url_source"] == "explicit"
    assert all(item["source"] == "explicit" for item in explicit["autofilled"])


def test_create_draft_autofills_web_browser_url_params(api: WebAPI) -> None:
    intent = "打开百度网站，提取今天的热点新闻"
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": intent, "domain_pack": "web_browser"},
    )
    assert status == 201, body
    open_node = next(node for node in body["graph"]["nodes"] if node["atom"] == "web_open")
    assert open_node["params"]["url"] == "https://top.baidu.com/board?tab=realtime"
    assert any(item["atom"] == "web_open" and item["param"] == "url" for item in body["autofilled"])


def test_only_mode_upstream_dependency_has_clear_error(api: WebAPI) -> None:
    """P1-1：only 模式引用上游变量时给出 from_start 提示，而不是 TemplateError。"""
    intent = "打开百度网页，获取今天的实时热点新闻 https://top.baidu.com/board?tab=realtime"
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": intent, "domain_pack": "web_browser"})
    assert status == 201, body
    wid = body["wid"]
    text_node = next(node for node in body["graph"]["nodes"] if node["atom"] == "web_get_text")
    status, debug = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": text_node["id"], "mode": "only"}
    )
    assert status == 400, debug
    error = debug["error"]
    assert error["code"] == "bad_request"
    assert "from_start" in error["message"]
    assert error["depends_on"]


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (
            "打开 https://seres-ai.com 首页取文本并给布局报告",
            "web_research",
        ),
        ("启动安卓 app 并截图", "android_adb"),
        ("列出 iOS 模拟器设备列表", "ios_sim"),
    ],
)
def test_draft_response_echoes_routed_domain_pack(
    api: WebAPI, intent: str, expected: str
) -> None:
    """F2 回归：三条意图（web/安卓/iOS）的 201 响应都必须回显实际路由包名。"""
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": intent})
    assert status == 201, body
    assert body["domain_pack"] == expected
    assert body["domain_pack"] not in (None, "")

    status, full = call(api, "GET", f"/api/studio/{body['wid']}")
    assert status == 200
    assert full["domain_pack"] == expected


def test_explicit_web_domain_injects_url(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "打开 https://seres-ai.com 首页取文本并给布局报告", "domain_pack": "web_research"},
    )
    assert status == 201, body
    fetch = next(node for node in body["graph"]["nodes"] if node["atom"] == "http_get_page")
    assert fetch["params"]["url"] == "https://seres-ai.com"


# --------------------------------------------------------------------------- #
# T2：不匹配时不瞎编
# --------------------------------------------------------------------------- #
def test_t2_missing_atoms_returns_422_without_graph(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "打开 https://seres-ai.com 首页并截图做 OCR 文字识别"},
    )
    assert status == 422, body
    error = body["error"]
    assert error["code"] == "capability_gap"
    # 截图原子已实现（web_screenshot），只剩 OCR 是真正的能力缺口
    assert "ocr_image" in set(error["missing_atoms"])
    assert "web_screenshot" not in set(error["missing_atoms"])
    assert error["suggestions"]
    assert "graph" not in body
    assert not list((api.root / ".panguverse" / "studio").glob("w-*"))


def test_t2_unmatched_intent_returns_explained_422(api: WebAPI) -> None:
    status, body = call(
        api, "POST", "/api/studio/drafts", {"intent": "给我泡一杯咖啡"}
    )
    assert status == 422
    error = body["error"]
    # 无法映射时用 intent_unmapped + suggestions 说清，且 missing_atoms 不为空数组
    assert error["code"] in {"capability_gap", "intent_unmapped"}
    assert error["missing_atoms"]
    assert error["suggestions"]
    assert "graph" not in body


# --------------------------------------------------------------------------- #
# T3：语义校验拦住错图
# --------------------------------------------------------------------------- #
def test_t3_order_graph_fails_homepage_intent_coverage(tmp_path: Path) -> None:
    registry = default_registry(reload=True)
    plan = RuleBasedPlanner(registry).plan(PlanRequest(intent=ORDER_INTENT, env={}))
    graph = graph_from_plan(plan)
    validation = studio.validate_nodes(
        tmp_path,
        wid="w-abcdef",
        rev=0,
        nodes=graph["nodes"],
        edges=graph["edges"],
        intent=HOME_INTENT,
        env_schema=[],
        available_env=set(),
        domain_pack="api_testing",
    )
    assert validation["ok"] is False
    assert any(item["code"] == "intent_mismatch" for item in validation["errors"])
    assert validation["coverage"]["ratio"] < 0.6


def test_t3_publish_blocks_semantic_mismatch(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": ORDER_INTENT, "domain_pack": "api_testing"},
    )
    assert status == 201
    wid = draft["wid"]
    status, body = call(api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True})
    assert status == 200, body
    workflow = studio.read_workflow(api.root, wid)
    workflow["intent"] = HOME_INTENT
    studio._write_workflow(api.root, wid, workflow)
    status, body = call(api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "x"})
    assert status == 409, body
    assert body["error"]["code"] == "intent_mismatch"
    assert body["error"]["validation"]["coverage"]["ratio"] < 0.6


# --------------------------------------------------------------------------- #
# T4/T5：异步调试、cursor 递增、跳过原因
# --------------------------------------------------------------------------- #
def test_t4_debug_is_async_and_pollable(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201
    wid = draft["wid"]
    node_id = draft["graph"]["nodes"][0]["id"]
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"rev": 0, "node_id": node_id, "mode": "from_start"}
    )
    assert status == 202, started
    assert started["session_id"].startswith("dbg-")
    result = wait_debug(api, started["session_id"])
    assert result["done"] is True
    assert result["cursor"] > 0
    statuses = [(item["node_id"], item["status"]) for item in result["all_nodes"]]
    assert (node_id, "running") in statuses
    assert any(status == "passed" for _node, status in statuses)
    assert service.list_runs(api.root) == []
    debug_files = list((api.root / ".panguverse" / "studio" / wid / "debug").glob("*.jsonl"))
    assert debug_files
    assert stat.S_IMODE(debug_files[0].stat().st_mode) == 0o600


def test_t5_skipped_nodes_have_reason_codes(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201
    wid = draft["wid"]
    first = draft["graph"]["nodes"][0]["id"]
    second = draft["graph"]["nodes"][1]["id"]

    # when=false → skipped + when_false
    status, _ = call(api, "PATCH", f"/api/studio/{wid}/nodes/{first}", {"when": "false"})
    assert status == 200
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": first, "mode": "only"}
    )
    assert status == 202
    result = wait_debug(api, started["session_id"])
    skipped = [item for item in result["all_nodes"] if item["status"] == "skipped"]
    assert skipped and skipped[-1]["reason"] == "when_false"

    # 上游失败 → downstream 节点 skipped + upstream_failed
    status, _ = call(
        api,
        "PATCH",
        f"/api/studio/{wid}/nodes/{first}",
        {"when": None, "params": {"path": "../escape", "text": "x"}},
    )
    assert status == 200
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": second, "mode": "from_start"}
    )
    assert status == 202
    result = wait_debug(api, started["session_id"])
    upstream = [
        item for item in result["all_nodes"] if item["status"] == "skipped" and item["reason"] == "upstream_failed"
    ]
    assert upstream
    assert result["summary"]["by_reason"].get("upstream_failed", 0) >= 1


# --------------------------------------------------------------------------- #
# T5 原因码：when_false / missing_input / upstream_failed
# --------------------------------------------------------------------------- #
def _final_by_node(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in nodes:
        out[str(item.get("node_id") or "")] = item
    return out


def test_debug_when_false_reason_code(api: WebAPI, page_url: str) -> None:
    status, draft = call(
        api, "POST", "/api/studio/drafts", {"intent": f"打开 {page_url} 首页取文本并给布局报告"}
    )
    assert status == 201, draft
    wid = draft["wid"]
    status, _ = call(api, "PATCH", f"/api/studio/{wid}/nodes/extract_text", {"when": "false"})
    assert status == 200
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": "extract_text", "mode": "from_start"}
    )
    assert status == 202
    result = wait_debug(api, started["session_id"])
    final = _final_by_node(result["all_nodes"])
    assert final["extract_text"]["status"] == "skipped"
    assert final["extract_text"]["reason"] == "when_false"
    assert result["summary"]["by_reason"]["when_false"] == 1


def test_debug_missing_input_reason_code(
    api: WebAPI, page_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 该用例验证「上游被跳过 → 下游缺输入」；需要 LLM 已配置才会生成 summarize 节点，
    # 但规划走模板路径，不会发起真实调用。
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    status, draft = call(
        api, "POST", "/api/studio/drafts", {"intent": f"打开 {page_url} 首页取文本并给布局报告"}
    )
    assert status == 201, draft
    wid = draft["wid"]
    status, _ = call(api, "PATCH", f"/api/studio/{wid}/nodes/extract_text", {"when": "false"})
    assert status == 200
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": "summarize", "mode": "from_start"}
    )
    assert status == 202
    result = wait_debug(api, started["session_id"])
    final = _final_by_node(result["all_nodes"])
    assert final["extract_text"]["status"] == "skipped"
    assert final["extract_text"]["reason"] == "when_false"
    assert final["summarize"]["status"] == "failed"
    assert final["summarize"]["reason"] == "missing_input"
    assert final["summarize"]["error"]["kind"] == "template"
    assert result["summary"]["by_reason"]["missing_input"] == 1


def test_debug_upstream_failed_reason_code(
    api: WebAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    # 同上：仅为了让计划包含 summarize 这个「最下游节点」
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": f"打开 http://127.0.0.1:{port}/ 首页取文本并给布局报告"},
    )
    assert status == 201, draft
    wid = draft["wid"]
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": "summarize", "mode": "from_start"}
    )
    assert status == 202
    result = wait_debug(api, started["session_id"])
    final = _final_by_node(result["all_nodes"])
    assert final["fetch_page"]["status"] == "failed"
    assert final["fetch_page"]["reason"] == "network"
    assert final["extract_text"]["status"] == "skipped"
    assert final["extract_text"]["reason"] == "upstream_failed"
    assert final["summarize"]["status"] == "skipped"
    assert final["summarize"]["reason"] == "upstream_failed"
    assert result["summary"]["by_reason"]["upstream_failed"] >= 2


# --------------------------------------------------------------------------- #
# T6：无 LLM 时不生成总结节点，from_start 必须全绿
# --------------------------------------------------------------------------- #
def test_t6_no_llm_draft_skips_summary_and_debug_passes(
    api: WebAPI, page_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    status, draft = call(api, "POST", "/api/studio/drafts", {"intent": f"打开 {page_url} 首页取文本并给布局报告"})
    assert status == 201, draft
    atoms = [node["atom"] for node in draft["graph"]["nodes"]]
    assert "llm_summarize" not in atoms
    wid = draft["wid"]
    last = draft["graph"]["nodes"][-1]["id"]
    status, started = call(
        api, "POST", f"/api/studio/{wid}/debug", {"node_id": last, "mode": "from_start"}
    )
    assert status == 202
    result = wait_debug(api, started["session_id"])
    failures = [item for item in result["all_nodes"] if item["status"] == "failed"]
    assert failures == []
    assert result["status"] == "ok"
    assert result["summary"]["passed"] == len(draft["graph"]["nodes"])
    assert [item for item in result["all_nodes"] if item["node_id"] == "summarize"] == []


# --------------------------------------------------------------------------- #
# LLM 规划：提示词含端口契约，且逐节点校验原子存在
# --------------------------------------------------------------------------- #
class FakeLLM:
    available = True
    model = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        return self.payload


def test_llm_planner_prompt_has_ports_and_rejects_unknown_atoms() -> None:
    registry = default_registry(reload=True)
    bad = {
        "cases": [
            {
                "id": "bad",
                "name": "bad",
                "steps": [{"id": "x", "use": "order_create", "with": {"url": "x"}}],
            }
        ]
    }
    planner = LLMPlanner(registry, FakeLLM(bad))
    with pytest.raises(PlanError) as exc:
        planner.plan(PlanRequest(intent="下单", allow_llm=True))
    assert "原子不存在" in str(exc.value)
    prompt = planner._build_prompt(PlanRequest(intent="下单", env={}))
    assert "端口契约" in prompt
    assert "http_get_page" in prompt
    assert "required" in prompt


# --------------------------------------------------------------------------- #
# iOS 正向路由：设备域优先 + 能力缺口不返回空数组
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("intent", "required_atoms"),
    [
        ("列出 iOS 模拟器", {"ios_sim_list"}),
        (
            "启动 iOS 模拟器上的 Safari 并截图",
            {"ios_sim_boot", "ios_sim_launch", "ios_sim_screenshot"},
        ),
        (
            "在 iOS 模拟器上打开网页并截图",
            {"ios_sim_openurl", "ios_sim_screenshot"},
        ),
    ],
)
def test_ios_intents_route_to_ios_sim(
    api: WebAPI, intent: str, required_atoms: set[str]
) -> None:
    status, body = call(api, "POST", "/api/studio/drafts", {"intent": intent})
    assert status == 201, body
    assert body["domain_pack"] == "ios_sim"
    atoms = [node["atom"] for node in body["graph"]["nodes"]]
    assert required_atoms <= set(atoms)
    # 设备域优先：不得再被 web_browser 抢走
    assert not [atom for atom in atoms if atom.startswith("web_")]

    status, full = call(api, "GET", f"/api/studio/{body['wid']}")
    assert status == 200
    assert full["domain_pack"] == "ios_sim"


def test_ios_safari_intent_binds_safari_bundle(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "启动 iOS 模拟器上的 Safari 并截图"},
    )
    assert status == 201, body
    launch = next(node for node in body["graph"]["nodes"] if node["atom"] == "ios_sim_launch")
    assert launch["params"]["bundle_id"] == "com.apple.mobilesafari"
    shot = next(node for node in body["graph"]["nodes"] if node["atom"] == "ios_sim_screenshot")
    assert shot["params"]["name"] == "ios-screenshot.png"


def test_ios_click_still_returns_capability_gap(api: WebAPI) -> None:
    status, body = call(
        api, "POST", "/api/studio/drafts", {"intent": "点击 iOS 模拟器上的登录按钮"}
    )
    assert status == 422, body
    error = body["error"]
    assert error["code"] == "capability_gap"
    assert error["missing_atoms"] == ["ios_tap"]
    assert error["suggestions"]
    assert "graph" not in body


def test_ios_domain_without_action_returns_candidate_atoms(api: WebAPI) -> None:
    status, body = call(
        api, "POST", "/api/studio/drafts", {"intent": "在 iOS 模拟器上随便操作一下"}
    )
    assert status == 422, body
    error = body["error"]
    assert error["code"] == "intent_unmapped"
    # 不允许空数组：至少要给出该领域的候选原子
    assert error["missing_atoms"]
    assert "ios_sim_list" in error["missing_atoms"]
