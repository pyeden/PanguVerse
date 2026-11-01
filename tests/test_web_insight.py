"""测试报告「AI 总结」契约测试（``tasks/web-ai-insight-spec.md`` A40–A44）。

全部用 ``WebAPI.handle`` 直接调用，不起端口；LLM 一律用假 client 注入。
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.request
from pathlib import Path
from typing import Any

from panguverse.axe.llm import LLMError
from panguverse.web import insight
from panguverse.web.api import WebAPI

RUN_ID = "abcdef123456"
SECRET = "s3cr3t-pass-9901"


# --------------------------------------------------------------------------- #
# 假报告 / 假 LLM
# --------------------------------------------------------------------------- #
def _case(case_id: str, name: str, status: str, *, atom: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": f"{case_id}_step", "atom": atom, "status": status, "outputs": {}}
    if error is not None:
        step["name"] = f"{name} 步骤"
        step["error"] = error
    return {"case_id": case_id, "name": name, "status": status, "steps": [step]}


def build_report() -> dict[str, Any]:
    cases = [
        _case(
            "cfg_creds",
            "读取凭据文件",
            "error",
            atom="fs_read_text",
            error={
                "kind": "file_not_found",
                "failure_kind": "file_not_found",
                "message": f"文件不存在: /tmp/ws/fs/{SECRET}.env",
            },
        ),
        _case(
            "health",
            "健康检查",
            "failed",
            atom="assert_status",
            error={
                "kind": "assertion",
                "message": "状态码期望 200，实际 401",
                "expected": 200,
                "actual": 401,
                "path": "response.status_code",
            },
        ),
        _case(
            "login_capture",
            "登录并捕获 token",
            "error",
            atom="http_request",
            error={"kind": "engine", "message": "步骤 login 的 capture 路径不存在: body.token"},
        ),
        _case(
            "site_page",
            "站点首页正文",
            "failed",
            atom="assert_body_contains",
            error={
                "kind": "assertion",
                "message": "响应原文未包含 '三木'",
                "expected": "三木",
                "actual": "<!doctype html><html><body>home</body></html>",
            },
        ),
        _case(
            "conn_refused",
            "连接被测服务",
            "error",
            atom="http_request",
            error={"kind": "engine", "message": "连接失败: Connection refused"},
        ),
        _case("ok_case", "通过的用例", "passed", atom="assert_status"),
    ]
    return {
        "run_id": RUN_ID,
        "started_at": 100.0,
        "finished_at": 100.0125,
        "duration_ms": 12.5,
        "summary": {
            "total": 6,
            "passed": 1,
            "failed": 2,
            "errored": 3,
            "skipped": 0,
            "healed_cases": 1,
            "steps": 5,
            "http_requests": 3,
            "duration_ms": 12.5,
            "success": False,
        },
        "conclusion": {"verdict": "存在执行错误", "success": False, "exit_code": 2},
        "plan": {"source": "memory", "env": {"BASE_URL": "http://127.0.0.1:7788", "API_PASSWORD": SECRET}},
        "env": {"BASE_URL": "http://127.0.0.1:7788", "API_PASSWORD": SECRET, "FS_ROOT": "/tmp/ws/fs"},
        "cases": cases,
        "meta": {"plan_source": "memory", "llm_used": False},
    }


class FakeLLM:
    """最小 LLM 替身：记录输入，按配置返回或抛错。"""

    available = True
    model = "fake-model-1"

    def __init__(self, data: Any = None, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.data = data
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.data


def good_llm_output() -> dict[str, Any]:
    return {
        "headline": "配置与认证问题阻断回归，先修沙箱文件",
        "summary_md": "## 结论\n未通过。\n## 失败分布\n- 配置/认证\n## 建议\n- 先修配置",
        "findings": [
            {
                "kind": "config",
                "title": "凭据文件缺失",
                "detail": "先补齐文件",
                "case_ids": ["cfg_creds", "not_a_real_case"],
            }
        ],
        "suggestions": [
            {"priority": "P2", "title": "补字段断言", "detail": "加 schema", "case_ids": ["health"]},
            {"priority": "P0", "title": "修配置", "detail": "补文件", "case_ids": ["cfg_creds"]},
        ],
    }


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class InsightWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_dir = root / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        self.report = build_report()
        self.write_report()
        (self.run_dir / "plan.json").write_text(
            json.dumps({"source": "memory", "env": self.report["env"]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_report(self) -> None:
        (self.run_dir / "report.json").write_text(
            json.dumps(self.report, ensure_ascii=False), encoding="utf-8"
        )

    @property
    def cache_path(self) -> Path:
        return self.root / ".panguverse" / "web" / "insights" / f"{RUN_ID}.json"


def make_api(tmp_path: Path, llm: Any = None) -> tuple[WebAPI, InsightWorkspace]:
    ws = InsightWorkspace(tmp_path / "ws")
    return WebAPI(ws.root, insight_llm=llm), ws


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, dict[str, Any]]:
    raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
    resp = api.handle(method, path, raw_body=raw)
    return resp.status, json.loads(resp.body.decode("utf-8"))


# --------------------------------------------------------------------------- #
# A40：无密钥时 POST 走规则，且 stats 与 report.summary 完全一致
# --------------------------------------------------------------------------- #
def test_a40_post_without_llm_returns_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api, ws = make_api(tmp_path)

    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {"refresh": False})
    assert status == 200, body
    assert body["available"] is True
    assert body["source"] == "rules"
    assert body["model"] is None
    assert body["degraded_reason"].strip()
    assert body["stats"] == ws.report["summary"]  # v3.0.1：全字段逐字相等


def test_a40_get_without_cache_never_generates(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    status, body = call(api, "GET", f"/api/runs/{RUN_ID}/insight")
    assert status == 200 and body == {"available": False}
    assert not ws.cache_path.exists()


def test_a40_bad_run_id_and_missing_run(tmp_path: Path) -> None:
    api, _ws = make_api(tmp_path)
    status, body = call(api, "GET", "/api/runs/nope/insight")
    assert status == 400 and body["error"]["code"] == "bad_request"
    status, body = call(api, "GET", "/api/runs/0123456789ab/insight")
    assert status == 404 and body["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# A41：内容质量（覆盖失败类型、P0 带 case_ids、不编造）
# --------------------------------------------------------------------------- #
def test_a41_rules_cover_failure_kinds_and_traceable(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    _status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    report_ids = {case["case_id"] for case in ws.report["cases"]}

    kinds = {finding["kind"] for finding in body["findings"]}
    assert {"config", "auth", "engine", "assertion"} <= kinds
    assert any(finding["title"].startswith("目标不可达") for finding in body["findings"])
    assert "mixed_target" in kinds  # plan_source=memory + 有失败

    p0 = [item for item in body["suggestions"] if item["priority"] == "P0"]
    assert p0 and all(item["case_ids"] for item in p0)

    for item in body["findings"] + body["suggestions"]:
        assert item["title"].strip()
        assert set(item["case_ids"]) <= report_ids

    assert "缺失文件" in body["summary_md"]
    assert "状态码期望 200，实际 401" in body["summary_md"]
    assert "not_a_real_case" not in json.dumps(body, ensure_ascii=False)


def test_a41_all_pass_still_returns_payload(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    ws.report["cases"] = [_case("ok", "全绿", "passed", atom="assert_status")]
    ws.report["summary"].update({"total": 1, "passed": 1, "failed": 0, "errored": 0, "success": True})
    ws.report["conclusion"] = {"verdict": "通过", "success": True}
    ws.write_report()
    _status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert body["source"] == "rules"
    assert body["findings"] == []
    assert body["suggestions"] and body["suggestions"][0]["priority"] == "P2"
    assert "通过" in body["headline"]


def test_a41_only_assertion_failures_still_have_p0(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    ws.report["cases"] = [
        _case(
            "only_body",
            "正文断言",
            "failed",
            atom="assert_body_contains",
            error={"kind": "assertion", "message": "响应原文未包含 'x'", "expected": "x", "actual": "y"},
        )
    ]
    ws.report["summary"].update({"total": 1, "passed": 0, "failed": 1, "errored": 0})
    ws.write_report()
    _status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    p0 = [item for item in body["suggestions"] if item["priority"] == "P0"]
    assert p0 and p0[0]["case_ids"] == ["only_body"]


# --------------------------------------------------------------------------- #
# A42：缓存（二次 GET 不变、refresh 变化、mtime 失效）
# --------------------------------------------------------------------------- #
def test_a42_cache_reuse_and_refresh(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    _s, first = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert ws.cache_path.is_file()
    assert stat.S_IMODE(ws.cache_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ws.cache_path.parent.stat().st_mode) == 0o700

    status, cached = call(api, "GET", f"/api/runs/{RUN_ID}/insight")
    assert status == 200 and cached["available"] is True
    assert cached["generated_at"] == first["generated_at"]

    time.sleep(0.02)
    _s, refreshed = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {"refresh": True})
    assert refreshed["generated_at"] != first["generated_at"]
    _s, again = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {"refresh": False})
    assert again["generated_at"] == refreshed["generated_at"]


def test_a42_legacy_cache_without_corrections_is_stale(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    _s, first = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    legacy = {key: value for key, value in first.items() if key != "corrections"}
    ws.cache_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    os.utime(ws.cache_path, (time.time() + 30, time.time() + 30))
    _status, body = call(api, "GET", f"/api/runs/{RUN_ID}/insight")
    assert body == {"available": False}


def test_a42_report_mtime_invalidates_cache(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    _s, _body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    future = time.time() + 30
    os.utime(ws.run_dir / "report.json", (future, future))
    _status, body = call(api, "GET", f"/api/runs/{RUN_ID}/insight")
    assert body == {"available": False}


# --------------------------------------------------------------------------- #
# A43：安全（密码/token 不出现在响应与缓存）
# --------------------------------------------------------------------------- #
def test_a43_no_secret_leaks(tmp_path: Path) -> None:
    api, ws = make_api(tmp_path)
    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert status == 200
    cached_text = ws.cache_path.read_text(encoding="utf-8")
    assert SECRET not in json.dumps(body, ensure_ascii=False)
    assert SECRET not in cached_text
    assert "***" in json.dumps(body, ensure_ascii=False)


def test_a43_llm_input_is_masked_and_bounded(tmp_path: Path) -> None:
    llm = FakeLLM(good_llm_output())
    api, _ws = make_api(tmp_path, llm)
    _status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert body["source"] == "llm"
    assert len(llm.calls) == 1
    _system, user = llm.calls[0]
    assert SECRET not in user
    assert len(user.encode("utf-8")) <= insight.LLM_INPUT_LIMIT
    digest = json.loads(user)
    assert "API_PASSWORD" in digest["env_keys"]
    assert all(key != "env" for key in digest)  # 只有键名，没有 env 值子树
    assert digest["env_keys"] and isinstance(digest["env_keys"], list)


# --------------------------------------------------------------------------- #
# A44：LLM 路径（注入假 client 走 llm；失败/超时/坏输出降级为 rules）
# --------------------------------------------------------------------------- #
def test_a44_llm_path_fields_complete(tmp_path: Path) -> None:
    llm = FakeLLM(good_llm_output())
    api, ws = make_api(tmp_path, llm)
    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert status == 200
    assert body["source"] == "llm" and body["model"] == "fake-model-1"
    assert body["degraded_reason"] == ""
    assert body["headline"] == good_llm_output()["headline"]
    assert body["stats"] == ws.report["summary"]
    assert [item["priority"] for item in body["suggestions"]] == ["P0", "P2"]
    assert all(item["case_ids"] != ["not_a_real_case"] for item in body["findings"])
    assert body["findings"][0]["case_ids"] == ["cfg_creds"]
    assert body["corrections"] and body["corrections"][0]["field"] == "stats"


def _lying_llm_output() -> dict[str, Any]:
    """故意瞎说：计数错、case_id 张冠李戴、缺 engine 类、0 条 P0。"""
    return {
        "headline": "6 个用例 5 通过，只剩文件路径问题",
        "summary_md": (
            "## 结论\n6 个用例 5 通过。\n## 失败分布\n- 文件路径 4 个\n- 健康检查 2 个\n"
            "## 建议\n- 先补文件"
        ),
        "findings": [
            {
                "kind": "config",
                "title": "文件路径问题 4 个",
                "detail": "有 4 个文件路径问题",
                "case_ids": ["cfg_creds", "health", "ghost_case"],
            },
            {
                "kind": "assertion",
                "title": "健康检查 2 个",
                "detail": "2 个健康检查失败",
                "case_ids": ["health"],  # health 实际是 auth，整条应被剔除
            },
        ],
        "suggestions": [
            {"priority": "P2", "title": "补字段断言", "detail": "加 schema", "case_ids": ["health"]},
            {"priority": "P1", "title": "修文件路径", "detail": "补文件", "case_ids": ["cfg_creds", "ghost_case"]},
        ],
    }


def test_a44_lying_llm_is_corrected_by_report(tmp_path: Path) -> None:
    ws = InsightWorkspace(tmp_path / "ws")
    api = WebAPI(ws.root, insight_llm=FakeLLM(_lying_llm_output()))
    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert status == 200 and body["source"] == "llm"

    rules = insight.rules_payload(ws.report, {"source": "memory"}, run_id=RUN_ID)
    # 1) stats 全字段与报告逐字相等
    assert body["stats"] == ws.report["summary"]
    # 2) findings 的 kind/case_ids/detail 完全按报告重建（含被模型漏掉的 engine 类）
    assert [(f["kind"], f["case_ids"]) for f in body["findings"]] == [
        (f["kind"], f["case_ids"]) for f in rules["findings"]
    ]
    engine_ids = [
        case_id for finding in body["findings"] if finding["kind"] == "engine"
        for case_id in finding["case_ids"]
    ]
    assert len(engine_ids) == 2  # capture + unreachable 两类都要有
    # 3) 每条 case_id 都真的属于该 kind（含 mixed_target 之外的）
    case_kinds = {f.case_id: f.kind for f in insight.collect_failures(ws.report)}
    for finding in body["findings"]:
        if finding["kind"] == "mixed_target":
            continue
        assert all(case_kinds[case_id] == finding["kind"] for case_id in finding["case_ids"])
    # 4) 有失败时必须至少一条带 case_ids 的 P0，且覆盖配置类失败
    p0 = [item for item in body["suggestions"] if item["priority"] == "P0" and item["case_ids"]]
    assert p0 and "cfg_creds" in p0[0]["case_ids"]
    # 5) summary_md 的数字/用例清单来自校正后数据
    assert "6 个用例，通过 1、失败 2、错误 3" in body["summary_md"]
    assert "文件路径 4 个" not in body["summary_md"]
    assert "5 通过" not in body["summary_md"]
    assert "cfg_creds" in body["summary_md"]
    # 6) 可审计
    fields = {item["field"] for item in body["corrections"]}
    assert {"stats", "findings", "summary_md", "suggestions"} <= fields
    assert not any("ghost_case" in json.dumps(item, ensure_ascii=False) for item in body["findings"])
    # 模型标题含错误数字 → 回退规则结论句
    assert body["headline"] == rules["headline"]


def test_a44_headline_without_digits_is_kept(tmp_path: Path) -> None:
    api, _ws = make_api(tmp_path, FakeLLM(good_llm_output()))
    _status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert body["headline"] == "配置与认证问题阻断回归，先修沙箱文件"
    assert not any(item["field"] == "headline" for item in body["corrections"])


def test_a44_llm_error_degrades_to_rules(tmp_path: Path) -> None:
    llm = FakeLLM(error=LLMError("boom"), delay=0)
    api, _ws = make_api(tmp_path, llm)
    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert status == 200
    assert body["source"] == "rules"
    assert "boom" in body["degraded_reason"]


def test_a44_llm_bad_output_degrades(tmp_path: Path) -> None:
    api, _ws = make_api(tmp_path, FakeLLM({"headline": "", "summary_md": ""}))
    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert status == 200 and body["source"] == "rules"
    assert "headline" in body["degraded_reason"]


def test_a44_llm_timeout_degrades(tmp_path: Path) -> None:
    ws = InsightWorkspace(tmp_path / "ws")
    llm = FakeLLM(good_llm_output(), delay=0.5)
    payload = insight.build_payload(
        ws.report,
        {"source": "memory"},
        run_id=RUN_ID,
        llm_client=llm,
        timeout=0.05,
    )
    assert payload["source"] == "rules"
    assert "超时" in payload["degraded_reason"]


def test_a44_unavailable_client_degrades(tmp_path: Path) -> None:
    class Offline(FakeLLM):
        available = False

    api, _ws = make_api(tmp_path, Offline(good_llm_output()))
    status, body = call(api, "POST", f"/api/runs/{RUN_ID}/insight", {})
    assert status == 200 and body["source"] == "rules"
    assert body["degraded_reason"] == insight.DEGRADED_NO_LLM


# --------------------------------------------------------------------------- #
# axe/llm.py：回环直连（不再走系统代理）
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return b'{"ok": true}'


class _FakeOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        self.urls.append(request.full_url)
        return _FakeResponse()


def test_llm_send_uses_direct_opener_for_loopback(monkeypatch) -> None:
    from panguverse.axe import llm as llm_mod

    opener = _FakeOpener()
    monkeypatch.setattr(llm_mod, "_DIRECT_OPENER", opener)

    def guard(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("回环地址不得走会读系统代理的 urlopen")

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", guard)
    client = llm_mod.LLMClient(api_key="k", backend="openai", base_url="http://127.0.0.1:11434")
    request = urllib.request.Request("http://127.0.0.1:11434/v1/chat/completions", data=b"{}")
    assert client._send(request) == {"ok": True}
    assert opener.urls == ["http://127.0.0.1:11434/v1/chat/completions"]
