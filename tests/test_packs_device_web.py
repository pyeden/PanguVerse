"""设备与 Web 自动化原子包测试（契约 v7.0 §2–§6）。

覆盖两层：

- **真实路径**（``PANGUVERSE_LIVE=1`` 时才跑）：Chrome 打开 example.com、截图、等待文本；
- **降级/桩路径**（默认必跑）：无设备、无模拟器、工具缺失、多设备歧义、
  uiautomator XML 解析、simctl JSON 解析、产物权限与截断。
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from panguverse.axe.memory import Workspace
from panguverse.core.context import RunContext
from panguverse.core.errors import AtomFailure
from panguverse.core.pack import builtin_pack_registry
from panguverse.core.registry import default_registry
from panguverse.engine.result import StepResult, brief_mapping
from panguverse.packs import android_adb, ios_sim, web_browser
from panguverse.packs.android_adb import (
    android_devices,
    android_find_text,
    android_input_text,
    android_launch_app,
    android_screencap,
    android_tap,
    android_ui_dump,
    android_wait_text,
)
from panguverse.packs.ios_sim import (
    IOS_CAPABILITY_GAPS,
    ios_sim_boot,
    ios_sim_install,
    ios_sim_launch,
    ios_sim_list,
    ios_sim_openurl,
    ios_sim_screenshot,
    ios_sim_terminate,
)
from panguverse.packs.web_browser import (
    web_extract_links,
    web_get_text,
    web_open,
    web_screenshot,
    web_wait_for_text,
)
from panguverse.web import planner_gate

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIVE = os.environ.get("PANGUVERSE_LIVE") == "1"

PAGE_HTML = """<!doctype html>
<html><head><title>示例 Example Page</title></head>
<body>
  <h1>Example Domain</h1>
  <p>这是用于浏览器原子测试的正文段落，长度足够超过五十个字符，
     用来验证文本提取、截图与动态等待逻辑都能正常工作。</p>
  <a href="/inside">内部链接</a>
  <a href="https://example.com/out">外部链接</a>
</body></html>"""


# --------------------------------------------------------------------------- #
# 公共夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ctx(tmp_path: Path) -> RunContext:
    workspace = Workspace(tmp_path / ".panguverse")
    workspace.ensure()
    return RunContext(workspace=workspace)


@pytest.fixture()
def page_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def fake_chrome_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    dom = tmp_path / "dom.html"
    dom.write_text(PAGE_HTML, encoding="utf-8")
    env = {
        "PANGUVERSE_CHROME": str(FIXTURES / "fake_chrome"),
        "FAKE_CHROME_DOM": str(dom),
        "FAKE_CHROME_PNG": str(FIXTURES / "tiny.png"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture()
def fake_adb_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    log = tmp_path / "adb.log"
    env = {
        "PANGUVERSE_ADB": str(FIXTURES / "fake_adb"),
        "FAKE_ADB_MODE": "one",
        "FAKE_ADB_LOG": str(log),
        "FAKE_ADB_XML": str(FIXTURES / "android_ui_dump.xml"),
        "FAKE_ADB_PNG": str(FIXTURES / "tiny.png"),
        "FAKE_ADB_STATE_DIR": str(tmp_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture()
def fake_sim_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    log = tmp_path / "simctl.log"
    env = {
        "PANGUVERSE_SIMCTL": str(FIXTURES / "fake_simctl"),
        "FAKE_SIM_MODE": "normal",
        "FAKE_SIM_LOG": str(log),
        "FAKE_SIM_JSON": str(FIXTURES / "simctl_devices.json"),
        "FAKE_SIM_JSON_EMPTY": str(FIXTURES / "simctl_devices_empty.json"),
        "FAKE_SIM_PNG": str(FIXTURES / "tiny.png"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def _mode(stat_result: os.stat_result) -> int:
    return stat.S_IMODE(stat_result.st_mode)


# --------------------------------------------------------------------------- #
# 包注册 / 契约元数据
# --------------------------------------------------------------------------- #
def test_three_packs_registered_with_contract_metadata() -> None:
    registry = builtin_pack_registry(reload=True)
    expected = {"web_browser": 5, "android_adb": 11, "ios_sim": 8}
    for name, count in expected.items():
        assert registry.has(name)
        pack = registry.get(name)
        assert len(pack.atoms) == count
        assert pack.version == "0.1.0"
        assert pack.description
        for atom in pack.atoms:
            assert atom.description
            assert atom.effects
            assert getattr(atom, "needs_tool", "") in {"chrome", "adb", "simctl"}
            assert f"tool:{atom.needs_tool}" in atom.tags
    assert registry.get("ios_sim").capability_gaps == list(IOS_CAPABILITY_GAPS)


@pytest.mark.parametrize(
    "module, atom_name",
    [
        (android_adb, "android_tap"),
        (android_adb, "android_input_text"),
        (android_adb, "android_launch_app"),
        (android_adb, "android_stop_app"),
        (android_adb, "android_swipe"),
        (android_adb, "android_keyevent"),
        (ios_sim, "ios_sim_install"),
        (ios_sim, "ios_sim_launch"),
    ],
)
def test_mutation_flags_are_accurate(module, atom_name: str) -> None:
    atom = getattr(module, atom_name)
    assert "mutate" in atom.effects


@pytest.mark.parametrize(
    "module, atom_name",
    [
        (web_browser, "web_get_text"),
        (web_browser, "web_extract_links"),
        (android_adb, "android_devices"),
        (android_adb, "android_find_text"),
        (android_adb, "android_ui_dump"),
        (android_adb, "android_screencap"),
        (ios_sim, "ios_sim_list"),
        (ios_sim, "ios_sim_screenshot"),
    ],
)
def test_read_atoms_are_not_mutating(module, atom_name: str) -> None:
    atom = getattr(module, atom_name)
    assert "mutate" not in atom.effects


# --------------------------------------------------------------------------- #
# Web（桩 + 本地 HTTP）
# --------------------------------------------------------------------------- #
def test_web_open_local_page(fake_chrome_env, page_server: str, ctx: RunContext) -> None:
    out = web_open.invoke(ctx, {"url": page_server, "timeout": 10})
    assert out["status"] == 200
    assert "Example" in out["title"]
    assert len(out["text"]) > 50
    assert out["final_url"] == page_server
    assert out["attempts"] == 1
    assert len(out["html"]) <= 4000


def test_web_open_truncation_can_be_raised(fake_chrome_env, page_server: str, ctx: RunContext) -> None:
    out = web_open.invoke(ctx, {"url": page_server, "timeout": 10, "max_chars": 200000})
    assert "示例 Example Page" in out["html"]


def test_web_get_text_reuses_web_research_rules() -> None:
    out = web_get_text.invoke(RunContext(), {"html": PAGE_HTML})
    assert "Example Domain" in out["text"]
    assert "内部链接" in out["text"]
    assert out["chars"] == len(out["text"])


def test_web_extract_links_internal_and_same_origin() -> None:
    out = web_extract_links.invoke(RunContext(), {"html": PAGE_HTML})
    assert out["count"] == 2
    by_text = {item["text"]: item for item in out["links"]}
    assert by_text["内部链接"]["internal"] is True
    assert by_text["外部链接"]["internal"] is False

    same = web_extract_links.invoke(
        RunContext(),
        {"html": PAGE_HTML, "same_origin_only": True, "base_url": "http://127.0.0.1:9999/"},
    )
    assert [item["text"] for item in same["links"]] == ["内部链接"]


def test_web_screenshot_artifact_permissions(fake_chrome_env, page_server: str, ctx: RunContext) -> None:
    out = web_screenshot.invoke(ctx, {"url": page_server, "name": "shot", "width": 640, "height": 480})
    assert out["path"].startswith("artifacts/web/")
    assert out["path"].endswith(".png")
    assert out["bytes"] > 0
    target = Path(ctx.workspace.root) / out["path"]
    assert target.exists() and target.stat().st_size == out["bytes"]
    assert _mode(target.stat()) == 0o600
    assert _mode((Path(ctx.workspace.root) / "artifacts" / "web").stat()) == 0o700


def test_web_screenshot_requires_url_or_html(ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        web_screenshot.invoke(ctx, {})
    assert excinfo.value.kind == "missing_tool" or excinfo.value.kind == "invalid_input"
    # 没设置 PANGUVERSE_CHROME 时可能先报 missing_tool；两者都必须是显式失败
    assert excinfo.value.failure_class in {"missing_resource", "invalid_input"}


def test_web_wait_for_text_positive(fake_chrome_env, page_server: str, ctx: RunContext) -> None:
    out = web_wait_for_text.invoke(
        ctx, {"url": page_server, "text": "Example Domain", "timeout": 3, "interval": 0.1}
    )
    assert out["found"] is True
    assert out["attempts"] >= 1
    assert out["elapsed_ms"] >= 0


def test_web_wait_for_text_timeout_not_retryable(fake_chrome_env, page_server: str, ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        web_wait_for_text.invoke(
            ctx,
            {"url": page_server, "text": "__PANGUVERSE_NOT_THERE__", "timeout": 0.8, "interval": 0.1},
        )
    failure = excinfo.value
    assert failure.kind == "timeout"
    assert failure.detail["attempts"] >= 1
    semantics = web_browser.WebBrowserSemantics()
    assert semantics.is_retryable(
        web_wait_for_text, {}, kind="timeout", failure_class=web_browser.FailureClass.TRANSIENT
    ) is False


def test_web_command_timeout_kills_process_group(
    monkeypatch: pytest.MonkeyPatch, page_server: str, ctx: RunContext, tmp_path: Path
) -> None:
    slow = tmp_path / "slow-chrome"
    slow.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setenv("PANGUVERSE_CHROME", str(slow))
    started = time.monotonic()
    with pytest.raises(AtomFailure) as excinfo:
        web_open.invoke(ctx, {"url": page_server, "timeout": 1})
    elapsed = time.monotonic() - started
    assert excinfo.value.kind == "timeout"
    assert elapsed < 6  # 超时后进程组被杀，不会挂 30 秒


def test_web_missing_tool_is_explicit(monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("PANGUVERSE_CHROME", "/definitely/not/chrome")
    with pytest.raises(AtomFailure) as excinfo:
        web_open.invoke(ctx, {"url": "https://example.com", "timeout": 5})
    assert excinfo.value.kind == "missing_tool"
    assert excinfo.value.failure_class == "missing_resource"
    assert "未找到 Chrome" in str(excinfo.value)
    semantics = web_browser.WebBrowserSemantics()
    assert semantics.is_retryable(
        web_open, {}, kind="missing_tool", failure_class=web_browser.FailureClass.MISSING_RESOURCE
    ) is False


# --------------------------------------------------------------------------- #
# Android：D5–D7 + 有设备分支（假 adb）
# --------------------------------------------------------------------------- #
def test_d5_android_devices_without_device_returns_empty(
    fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "none")
    out = android_devices.invoke(ctx, {})
    assert out["devices"] == []
    assert out["count"] == 0


def test_d6_android_tap_without_device_fails_missing_device(
    fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "none")
    with pytest.raises(AtomFailure) as excinfo:
        android_tap.invoke(ctx, {"x": 10, "y": 20})
    failure = excinfo.value
    assert failure.kind == "missing_device"
    assert "未连接设备" in str(failure)
    log = Path(fake_adb_env["FAKE_ADB_LOG"]).read_text(encoding="utf-8")
    assert "input tap" not in log  # 没有设备就不发命令
    assert log.count("devices") == 1  # 不自动重试
    semantics = android_adb.AndroidSemantics()
    assert semantics.is_retryable(
        android_tap, {}, kind="missing_device", failure_class=android_adb.FailureClass.MISSING_RESOURCE
    ) is False


def test_d7_android_missing_tool(monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("PANGUVERSE_ADB", "/definitely/not/adb")
    with pytest.raises(AtomFailure) as excinfo:
        android_devices.invoke(ctx, {})
    assert excinfo.value.kind == "missing_tool"
    assert "未找到 adb" in str(excinfo.value)
    assert excinfo.value.failure_class == "missing_resource"


def test_android_ambiguous_device(fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "two")
    with pytest.raises(AtomFailure) as excinfo:
        android_tap.invoke(ctx, {"x": 10, "y": 20})
    assert excinfo.value.kind == "ambiguous_device"
    assert "多台安卓设备" in str(excinfo.value)
    assert "SERIAL-A" in str(excinfo.value)


def test_android_explicit_serial_resolves(fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "two")
    out = android_tap.invoke(ctx, {"x": 10, "y": 20, "serial": "SERIAL-B"})
    assert out["ok"] is True
    assert out["serial"] == "SERIAL-B"


def test_android_ui_dump_parses_xml_and_find_text(fake_adb_env, ctx: RunContext) -> None:
    out = android_ui_dump.invoke(ctx, {})
    assert out["count"] == 4
    login = next(node for node in out["nodes"] if node["text"] == "登录")
    assert login["resource_id"] == "com.example:id/login"
    assert login["bounds"] == "[100,200][500,300]"
    assert login["center"] == {"x": 300, "y": 250}
    assert login["clickable"] is True

    found = android_find_text.invoke(ctx, {"text": "登录", "nodes": out["nodes"]})
    assert found["found"] is True
    assert found["node"]["resource_id"] == "com.example:id/login"

    desc = android_find_text.invoke(ctx, {"text": "头像", "xml": out["xml"], "match": "contains"})
    assert desc["found"] is True
    assert desc["node"]["description"] == "头像"

    missing = android_find_text.invoke(ctx, {"text": "不存在", "nodes": out["nodes"], "match": "exact"})
    assert missing["found"] is False
    assert missing["node"] == {}


def test_android_ui_dump_parses_xml_longer_than_clip_limit(
    fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """回归：uiautomator XML > MAX_OUTPUT_CHARS 时不得先截断再解析。"""
    large = FIXTURES / "android_ui_dump_large.xml"
    assert len(large.read_text(encoding="utf-8")) > 8000
    monkeypatch.setenv("FAKE_ADB_XML", str(large))

    out = android_ui_dump.invoke(ctx, {})
    assert out["count"] == 70
    assert out["xml_chars"] > 8000
    # 回显仍限长，但完整 XML 另外保留给产物收集
    assert len(out["xml"]) <= 4000
    assert "已截断" in out["xml"]
    assert out["xml_full"] == large.read_text(encoding="utf-8")

    deep = next(node for node in out["nodes"] if node["text"] == "深层节点")
    assert deep["bounds"] == "[0,690][1080,699]"
    assert deep["center"] == {"x": 540, "y": 694}
    found = android_find_text.invoke(
        ctx, {"text": "深层节点", "nodes": out["nodes"], "match": "exact"}
    )
    assert found["found"] is True


def test_android_ui_dump_retries_once(fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "dump_fail_once")
    out = android_ui_dump.invoke(ctx, {})
    assert out["count"] == 4
    log = Path(fake_adb_env["FAKE_ADB_LOG"]).read_text(encoding="utf-8")
    assert log.count("uiautomator dump") == 2  # 第一次失败，重试一次成功


def test_android_ui_dump_fails_after_one_retry(fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "fail")
    with pytest.raises(AtomFailure) as excinfo:
        android_ui_dump.invoke(ctx, {})
    assert excinfo.value.kind == "device_command_failed"
    log = Path(fake_adb_env["FAKE_ADB_LOG"]).read_text(encoding="utf-8")
    assert log.count("uiautomator dump") == 2


def test_android_wait_text_timeout(fake_adb_env, ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        android_wait_text.invoke(ctx, {"text": "不存在的文本", "timeout": 0.4, "interval": 0.1})
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.detail["attempts"] >= 1


def test_android_screencap_artifact(fake_adb_env, ctx: RunContext) -> None:
    out = android_screencap.invoke(ctx, {"name": "cap"})
    assert out["path"].startswith("artifacts/android/")
    target = Path(ctx.workspace.root) / out["path"]
    assert target.exists()
    assert out["bytes"] == target.stat().st_size == 70
    assert _mode(target.stat()) == 0o600
    assert _mode((Path(ctx.workspace.root) / "artifacts" / "android").stat()) == 0o700


def test_android_launch_and_output_truncation(fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    out = android_launch_app.invoke(ctx, {"package": "com.example", "activity": ".MainActivity"})
    assert out["ok"] is True
    assert out["serial"] == "SERIAL-A"

    monkeypatch.setenv("FAKE_ADB_MODE", "long")
    long_out = android_launch_app.invoke(ctx, {"package": "com.example", "activity": ".MainActivity"})
    assert len(long_out["output"]) <= 4000
    assert "已截断" in long_out["output"]


def test_android_launch_monkey_nonzero_with_events_is_ok(
    fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """回归：monkey 退出码非零但已注入事件时不得误判失败。"""
    monkeypatch.setenv("FAKE_ADB_MODE", "monkey_nonzero_events")
    out = android_launch_app.invoke(ctx, {"package": "com.example"})
    assert out["ok"] is True
    assert out["verified"] is True
    assert out["focused"] == "com.example/.MainActivity"
    assert "已确认前台组件" in out["message"]
    log = Path(fake_adb_env["FAKE_ADB_LOG"]).read_text(encoding="utf-8")
    assert "monkey -p com.example" in log


def test_android_launch_monkey_unverified_returns_ok_false_verified(
    fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "monkey_nonzero_noevents")
    monkeypatch.setenv("FAKE_ADB_FOCUS_PKG", "com.other.app")
    out = android_launch_app.invoke(ctx, {"package": "com.example"})
    assert out["ok"] is True
    assert out["verified"] is False
    assert "com.other.app" in out["focused"]
    assert "不可验证" in out["message"]


def test_android_launch_am_failure_when_focus_other_raises(
    fake_adb_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    monkeypatch.setenv("FAKE_ADB_MODE", "am_exit1_nofocus")
    with pytest.raises(AtomFailure) as excinfo:
        android_launch_app.invoke(
            ctx, {"package": "com.example", "activity": ".MainActivity"}
        )
    assert excinfo.value.kind == "device_command_failed"
    assert "启动应用失败" in str(excinfo.value)


def test_android_input_text_escapes_spaces(fake_adb_env, ctx: RunContext) -> None:
    out = android_input_text.invoke(ctx, {"text": "hello world"})
    assert out["ok"] is True
    assert out["sent"] == "hello%sworld"
    assert out["escaped"] is True
    log = Path(fake_adb_env["FAKE_ADB_LOG"]).read_text(encoding="utf-8")
    assert "input text hello%sworld" in log


def test_android_input_text_plain_text_not_escaped(fake_adb_env, ctx: RunContext) -> None:
    out = android_input_text.invoke(ctx, {"text": "qatest"})
    assert out["ok"] is True
    assert out["sent"] == "qatest"
    assert out["escaped"] is False


def test_android_input_text_rejects_non_ascii_before_adb(
    fake_adb_env, ctx: RunContext
) -> None:
    """回归：中文曾让远端 ``input text`` 抛 Binder NPE；现在必须显式 unsupported_input。"""
    with pytest.raises(AtomFailure) as excinfo:
        android_input_text.invoke(ctx, {"text": "qa test 中文"})
    failure = excinfo.value
    assert failure.kind == "unsupported_input"
    assert failure.failure_class == "invalid_input"
    assert "ADBKeyboard" in str(failure)
    assert "剪贴板" in str(failure)
    assert failure.detail["retryable"] is False
    # 连 adb devices 都不应调用（必须在设备解析前拒绝）
    log = Path(fake_adb_env["FAKE_ADB_LOG"])
    assert not log.exists() or log.read_text(encoding="utf-8") == ""
    assert android_adb.AndroidSemantics().is_retryable(
        android_input_text,
        {},
        kind="unsupported_input",
        failure_class=android_adb.FailureClass.INVALID_INPUT,
    ) is False


def test_android_input_text_rejects_dangerous_chars(fake_adb_env, ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        android_input_text.invoke(ctx, {"text": "qa;reboot"})
    assert excinfo.value.kind == "invalid_input"
    assert "危险字符" in str(excinfo.value)


def test_android_input_text_rejects_too_long(fake_adb_env, ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        android_input_text.invoke(ctx, {"text": "a" * (android_adb.MAX_INPUT_TEXT_CHARS + 1)})
    assert excinfo.value.kind == "invalid_input"
    assert "文本过长" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# iOS：D8–D9 + 有模拟器分支（假 simctl）
# --------------------------------------------------------------------------- #
def test_d8_ios_list_without_simulator_returns_empty(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    monkeypatch.setenv("FAKE_SIM_MODE", "empty")
    out = ios_sim_list.invoke(ctx, {})
    assert out["devices"] == []
    assert out["count"] == 0
    assert out["booted_count"] == 0


def test_d9_ios_launch_without_simulator_fails_missing_device(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    monkeypatch.setenv("FAKE_SIM_MODE", "empty")
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_launch.invoke(ctx, {"udid": "AAAA-1111-2222", "bundle_id": "com.example.app"})
    assert excinfo.value.kind == "missing_device"
    assert "没有可用的 iOS 模拟器" in str(excinfo.value)
    semantics = ios_sim.IosSimSemantics()
    assert semantics.is_retryable(
        ios_sim_launch, {}, kind="missing_device", failure_class=ios_sim.FailureClass.MISSING_RESOURCE
    ) is False


def test_ios_list_marks_booted_per_device(fake_sim_env, ctx: RunContext) -> None:
    out = ios_sim_list.invoke(ctx, {})
    assert out["count"] == 2
    assert out["booted_count"] == 1
    assert all(isinstance(item["booted"], bool) for item in out["devices"])
    booted = next(item for item in out["devices"] if item["udid"] == "BBBB-3333-4444")
    assert booted["state"] == "Booted"
    assert booted["booted"] is True
    shutdown = next(item for item in out["devices"] if item["udid"] == "AAAA-1111-2222")
    assert shutdown["state"] == "Shutdown"
    assert shutdown["booted"] is False


def _sample_device(index: int) -> dict[str, object]:
    return {
        "name": f"iPhone {index}",
        "udid": f"UDID-{index:04d}",
        "state": "Shutdown",
        "booted": False,
        "runtime": "iOS-18-6",
        "runtime_id": "com.apple.CoreSimulator.SimRuntime.iOS-18-6",
    }


def test_ios_list_report_output_keeps_devices_as_list(fake_sim_env, ctx: RunContext) -> None:
    """报告层的 ``outputs.devices`` 必须是 JSON 数组，而不是 repr 字符串。"""
    out = ios_sim_list.invoke(ctx, {})
    assert type(out["devices"]) is list
    assert all(type(item) is dict for item in out["devices"])

    payload = StepResult(step_id="list", atom="ios_sim_list", outputs=out).to_dict()["outputs"]
    assert type(payload["devices"]) is list
    assert payload["devices"]
    assert all(isinstance(item, dict) for item in payload["devices"])
    assert payload["devices"][0]["udid"]


def test_ios_list_caps_at_200_and_keeps_list(
    monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    devices = [_sample_device(index) for index in range(205)]
    monkeypatch.setattr(ios_sim, "list_simulators", lambda: devices)

    out = ios_sim_list.invoke(ctx, {})
    assert type(out["devices"]) is list
    assert len(out["devices"]) == ios_sim.MAX_LISTED_DEVICES == 200
    assert out["count"] == 200
    assert out["total"] == 205
    assert out["truncated"] is True
    assert all(isinstance(item, dict) for item in out["devices"])

    payload = StepResult(step_id="list", atom="ios_sim_list", outputs=out).to_dict()["outputs"]
    assert type(payload["devices"]) is list
    assert all(isinstance(item, dict) for item in payload["devices"])


def test_ios_list_report_does_not_stringify_normal_device_list(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """12 台真机规模的列表（repr 超过旧 2000 预算）也必须保持数组。"""
    devices = [_sample_device(index) for index in range(12)]
    monkeypatch.setattr(ios_sim, "list_simulators", lambda: devices)
    out = ios_sim_list.invoke(ctx, {})

    payload = StepResult(step_id="list", atom="ios_sim_list", outputs=out).to_dict()["outputs"]
    assert type(payload["devices"]) is list
    assert len(payload["devices"]) == 12
    assert all(isinstance(item, dict) for item in payload["devices"])


def test_android_ui_dump_nodes_stay_structured_in_report(
    fake_adb_env, ctx: RunContext
) -> None:
    out = android_ui_dump.invoke(ctx, {})
    assert type(out["nodes"]) is list
    nodes = [dict(item) for item in out["nodes"] * 200]
    assert len(json.dumps(nodes, ensure_ascii=False)) > 8000

    payload = StepResult(
        step_id="dump",
        atom="android_ui_dump",
        outputs={**out, "nodes": nodes, "count": len(nodes)},
    ).to_dict()["outputs"]
    assert type(payload["nodes"]) is list
    assert payload["nodes"]
    assert all(isinstance(item, dict) for item in payload["nodes"])
    assert payload["nodes_total"] == len(nodes)


def test_brief_mapping_preserves_arrays_of_dicts() -> None:
    links = [
        {"href": f"https://example.com/{index}", "text": f"link-{index}", "internal": True}
        for index in range(500)
    ]
    out = brief_mapping({"links": links, "count": len(links)})
    assert type(out["links"]) is list
    assert all(isinstance(item, dict) for item in out["links"])
    assert out["links_truncated"] is True
    assert out["links_total"] == 500
    assert out["count"] == 500


def test_ios_launch_without_booted_device_is_missing_device(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """真机 ``simctl shutdown all`` 后 launch 返回 405，应归为 missing_device。"""
    monkeypatch.setenv("FAKE_SIM_MODE", "nostate")
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_launch.invoke(ctx, {"udid": "AAAA-1111-2222", "bundle_id": "com.example.app"})
    failure = excinfo.value
    assert failure.kind == "missing_device"
    assert failure.failure_class == "missing_resource"
    assert "请先 boot" in str(failure)
    assert failure.detail["simctl_code"] == 405
    semantics = ios_sim.IosSimSemantics()
    assert (
        semantics.is_retryable(
            ios_sim_launch,
            {},
            kind="missing_device",
            failure_class=ios_sim.FailureClass.MISSING_RESOURCE,
        )
        is False
    )


@pytest.mark.parametrize(
    "operation, inputs",
    [
        (ios_sim_launch, {"bundle_id": "com.example.app"}),
        (ios_sim_terminate, {"bundle_id": "com.example.app"}),
        (ios_sim_openurl, {"url": "https://example.com"}),
        (ios_sim_screenshot, {}),
        (ios_sim_install, {"app_path": "__APP__"}),
    ],
)
def test_ios_actions_405_json_map_to_missing_device(
    fake_sim_env,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ctx: RunContext,
    operation,
    inputs: dict[str, str],
) -> None:
    """JSON 形式的 simctl 405 也必须映射到 missing_device（不可重试）。"""
    monkeypatch.setenv("FAKE_SIM_MODE", "json405")
    app = tmp_path / "App.app"
    app.mkdir()
    payload = {"udid": "AAAA-1111-2222"}
    payload.update({key: (str(app) if value == "__APP__" else value) for key, value in inputs.items()})
    with pytest.raises(AtomFailure) as excinfo:
        operation.invoke(ctx, payload)
    failure = excinfo.value
    assert failure.kind == "missing_device"
    assert failure.failure_class == "missing_resource"
    assert "请先 boot" in str(failure)


def test_ios_missing_tool(monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("PANGUVERSE_SIMCTL", "/definitely/not/simctl")
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_list.invoke(ctx, {})
    assert excinfo.value.kind == "missing_tool"
    assert "未找到 simctl" in str(excinfo.value)


def test_ios_list_parses_json(fake_sim_env, ctx: RunContext) -> None:
    out = ios_sim_list.invoke(ctx, {})
    assert out["count"] == 2  # isAvailable=false 的旧运行时被过滤
    udids = {item["udid"] for item in out["devices"]}
    assert udids == {"AAAA-1111-2222", "BBBB-3333-4444"}
    first = out["devices"][0]
    assert first["runtime"] == "iOS-17-5"
    assert first["state"] in {"Shutdown", "Booted"}


def test_ios_list_parses_json_longer_than_clip_limit(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """回归：simctl list -j 输出 > MAX_OUTPUT_CHARS（真机 6612）时不得先截断。"""
    large = FIXTURES / "simctl_devices_large.json"
    assert len(large.read_text(encoding="utf-8")) > 4000
    monkeypatch.setenv("FAKE_SIM_JSON", str(large))

    out = ios_sim_list.invoke(ctx, {})
    assert out["count"] == 26  # 27 台里 1 台 isAvailable=false 被过滤
    names = {item["name"] for item in out["devices"]}
    assert "pv_iphone" in names
    booted = next(item for item in out["devices"] if item["name"] == "pv_iphone")
    assert booted["state"] == "Booted"
    assert booted["udid"] == "UDID-0-0-ABCD-EFGH-IJKL-MNOPQRSTUVWX"
    assert booted["runtime"] == "iOS-18-6"


def test_ios_launch_parses_pid(fake_sim_env, ctx: RunContext) -> None:
    out = ios_sim_launch.invoke(ctx, {"udid": "AAAA-1111-2222", "bundle_id": "com.example.app"})
    assert out["pid"] == 4242
    assert out["ok"] is True
    log = Path(fake_sim_env["FAKE_SIM_LOG"]).read_text(encoding="utf-8")
    assert "launch AAAA-1111-2222 com.example.app" in log


def test_ios_launch_unknown_udid(fake_sim_env, ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_launch.invoke(ctx, {"udid": "NOPE", "bundle_id": "com.example.app"})
    assert excinfo.value.kind == "missing_device"
    assert "未找到模拟器" in str(excinfo.value)


def test_ios_boot_already_booted_skips_command(fake_sim_env, ctx: RunContext) -> None:
    out = ios_sim_boot.invoke(ctx, {"udid": "BBBB-3333-4444"})
    assert out["state"] == "Booted"
    log_path = Path(fake_sim_env["FAKE_SIM_LOG"])
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "boot BBBB" not in log


def test_ios_screenshot_artifact(fake_sim_env, ctx: RunContext) -> None:
    out = ios_sim_screenshot.invoke(ctx, {"udid": "BBBB-3333-4444", "name": "sim"})
    assert out["path"].startswith("artifacts/ios/")
    target = Path(ctx.workspace.root) / out["path"]
    assert target.exists() and out["bytes"] == 70
    assert _mode(target.stat()) == 0o600
    assert _mode((Path(ctx.workspace.root) / "artifacts" / "ios").stat()) == 0o700


def test_ios_screenshot_shutdown_device_is_missing_device(fake_sim_env, ctx: RunContext) -> None:
    """预检：Shutdown 设备截图必须归为 missing_device，而不是发命令再报内部错误。"""
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_screenshot.invoke(ctx, {"udid": "AAAA-1111-2222"})
    failure = excinfo.value
    assert failure.kind == "missing_device"
    assert failure.failure_class == "missing_resource"
    assert "请先 boot" in str(failure)
    assert failure.detail["simctl_code"] == 405
    log_path = Path(fake_sim_env["FAKE_SIM_LOG"])
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "io AAAA-1111-2222 screenshot" not in log


def test_ios_screenshot_booted_405_json_maps_to_missing_device(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """预检通过后 simctl 仍返回 405 JSON：兵底逻辑保持 missing_device。"""
    monkeypatch.setenv("FAKE_SIM_MODE", "json405")
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_screenshot.invoke(ctx, {"udid": "BBBB-3333-4444"})
    assert excinfo.value.kind == "missing_device"
    assert excinfo.value.detail["simctl_code"] == 405


def test_ios_screenshot_png_error_text_is_missing_device(
    fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext
) -> None:
    """真机文案 ``Detected file type`` / ``Error creating the image`` 也要识别。"""
    monkeypatch.setenv("FAKE_SIM_MODE", "pngfail")
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_screenshot.invoke(ctx, {"udid": "BBBB-3333-4444"})
    failure = excinfo.value
    assert failure.kind == "missing_device"
    assert failure.failure_class == "missing_resource"
    assert failure.detail["simctl_code"] == 405


def test_ios_install_missing_app(fake_sim_env, ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        ios_sim_install.invoke(ctx, {"udid": "AAAA-1111-2222", "app_path": "/nope/App.app"})
    assert excinfo.value.kind == "file_not_found"
    assert excinfo.value.failure_class == "missing_resource"


def test_ios_terminate_not_running_is_ok(fake_sim_env, monkeypatch: pytest.MonkeyPatch, ctx: RunContext) -> None:
    monkeypatch.setenv("FAKE_SIM_MODE", "notrunning")
    out = ios_sim_terminate.invoke(ctx, {"udid": "AAAA-1111-2222", "bundle_id": "com.example.app"})
    assert out["ok"] is True


# --------------------------------------------------------------------------- #
# 规划器领域路由 / 能力缺口（与运行时环境缺失区分）
# --------------------------------------------------------------------------- #
def test_intent_routes_web_browser_page_content() -> None:
    intent = "用无头浏览器打开网页 https://example.com 并提取正文"
    analysis = planner_gate.analyze_intent(intent)
    assert analysis.domain == "web_browser"
    assert {"web_open", "web_get_text"} <= set(analysis.requested_atoms)
    assert analysis.missing_atoms == []

    draft = planner_gate.plan_draft(default_registry(reload=True), intent=intent, allow_llm=False)
    atoms = [step.use for case in draft.plan.cases for step in case.steps]
    assert atoms[0] == "web_open"
    assert "web_get_text" in atoms
    assert draft.analysis["domain"] == "web_browser"
    assert draft.capability_gaps == []
    open_step = next(step for case in draft.plan.cases for step in case.steps if step.use == "web_open")
    assert open_step.with_["url"] == "https://example.com"


def test_intent_routes_android_tap_without_capability_gap() -> None:
    intent = "在安卓设备上点击坐标 100,200"
    analysis = planner_gate.analyze_intent(intent)
    assert analysis.domain == "android_adb"
    assert "android_tap" in analysis.requested_atoms
    # 原子存在：无设备是运行期 missing_device，不是能力缺口
    assert analysis.missing_atoms == []

    draft = planner_gate.plan_draft(default_registry(reload=True), intent=intent, allow_llm=False)
    atoms = [step.use for case in draft.plan.cases for step in case.steps]
    assert "android_devices" in atoms
    assert "android_tap" in atoms
    assert draft.capability_gaps == []


def test_intent_ios_click_returns_capability_gap() -> None:
    intent = "点击 iOS 模拟器上的登录按钮"
    analysis = planner_gate.analyze_intent(intent)
    assert analysis.domain == "ios_sim"
    assert "ios_tap" in analysis.missing_atoms
    assert analysis.suggestions

    with pytest.raises(planner_gate.PlanGateError) as excinfo:
        planner_gate.plan_draft(default_registry(reload=True), intent=intent, allow_llm=False)
    failure = excinfo.value
    assert failure.code == "capability_gap"
    assert failure.status == 422
    assert "ios_tap" in failure.details["missing_atoms"]
    assert failure.details["suggestions"]


# --------------------------------------------------------------------------- #
# D1–D4：真实 Chrome + example.com（显式开关，默认跳过）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LIVE, reason="设置 PANGUVERSE_LIVE=1 才跑真实 Chrome/网络验收")
def test_live_d1_web_open_example(ctx: RunContext) -> None:
    out = web_open.invoke(ctx, {"url": "https://example.com", "timeout": 30, "max_chars": 200000})
    assert out["status"] == 200
    assert "Example" in out["title"]
    assert len(out["text"]) > 50
    assert out["final_url"].startswith("https://example.com")


@pytest.mark.skipif(not LIVE, reason="设置 PANGUVERSE_LIVE=1 才跑真实 Chrome/网络验收")
def test_live_d2_web_extract_links_example(ctx: RunContext) -> None:
    opened = web_open.invoke(ctx, {"url": "https://example.com", "timeout": 30, "max_chars": 200000})
    links = web_extract_links.invoke(ctx, {"html": opened["html"]})
    assert links["count"] > 0
    for item in links["links"]:
        expected_internal = item["href"].startswith(("/", "#", "?", "./", "../")) or "://" not in item["href"]
        assert item["internal"] is expected_internal


@pytest.mark.skipif(not LIVE, reason="设置 PANGUVERSE_LIVE=1 才跑真实 Chrome/网络验收")
def test_live_d3_web_screenshot_example(ctx: RunContext) -> None:
    out = web_screenshot.invoke(ctx, {"url": "https://example.com", "name": "example", "timeout": 30})
    target = Path(ctx.workspace.root) / out["path"]
    assert target.exists()
    assert out["bytes"] > 0
    assert _mode(target.stat()) == 0o600
    assert out["path"].startswith("artifacts/web/")


@pytest.mark.skipif(not LIVE, reason="设置 PANGUVERSE_LIVE=1 才跑真实 Chrome/网络验收")
def test_live_d4_web_wait_for_text_example(ctx: RunContext) -> None:
    positive = web_wait_for_text.invoke(
        ctx, {"url": "https://example.com", "text": "Example Domain", "timeout": 20, "interval": 0.3}
    )
    assert positive["found"] is True
    assert positive["attempts"] >= 1


def test_web_wait_for_text_report_step_is_not_retryable(
    fake_chrome_env, page_server: str, registry
) -> None:
    """报告步骤的 error 体必须显式带 retryable=false（等待超时）。"""
    from panguverse.axe.plan import TestPlan
    from panguverse.core.molecule import Molecule
    from panguverse.engine.executor import Executor

    molecule = Molecule.from_spec(
        {
            "id": "wait",
            "name": "等待不存在的文本",
            "steps": [
                {
                    "id": "wait",
                    "use": "web_wait_for_text",
                    "with": {
                        "url": page_server,
                        "text": "__PANGUVERSE_NOT_THERE__",
                        "timeout": 0.8,
                        "interval": 0.1,
                    },
                }
            ],
        }
    )
    result = Executor(registry).run(TestPlan(cases=[molecule], source="rules"))
    step = result.cases[0].steps[0]
    assert step.status in {"failed", "error"}
    assert step.error is not None
    assert step.error["failure_kind"] == "timeout"
    assert step.error["retryable"] is False
    # 报告序列化（API /api/runs 的 steps[].error）同样带字段
    payload = result.to_dict()["cases"][0]["steps"][0]
    assert payload["error"]["retryable"] is False


@pytest.mark.skipif(not LIVE, reason="设置 PANGUVERSE_LIVE=1 才跑真实 Chrome/网络验收")
def test_live_d4_web_wait_for_text_negative(ctx: RunContext) -> None:
    with pytest.raises(AtomFailure) as excinfo:
        web_wait_for_text.invoke(
            ctx,
            {
                "url": "https://example.com",
                "text": "PANGUVERSE_TEXT_NOT_PRESENT_9c1f",
                "timeout": 2.0,
                "interval": 0.3,
            },
        )
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.detail["attempts"] >= 1
    assert web_browser.WebBrowserSemantics().is_retryable(
        web_wait_for_text, {}, kind="timeout", failure_class=web_browser.FailureClass.TRANSIENT
    ) is False
