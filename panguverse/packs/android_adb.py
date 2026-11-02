"""``android_adb`` 领域包：通过 ``adb`` 控制已连接的安卓设备。

契约（``tasks/device-web-atoms-spec.md`` v7.0 §3）::

==================  ======================================================
原子                语义
==================  ======================================================
android_devices     探测设备；无设备返回 ``[]``，不报错
android_launch_app  启动应用（am start / monkey 兜底），mutate
android_stop_app    force-stop，mutate
android_tap         点击坐标，mutate
android_swipe       滑动，mutate
android_input_text  输入文本（空格转义 %s），mutate
android_keyevent    按键，mutate
android_screencap   截图 → .panguverse/artifacts/android/
android_ui_dump     uiautomator dump + XML → nodes
android_find_text   纯解析查找节点
android_wait_text   轮询 ui_dump 等文本出现
==================  ======================================================

失败词汇（**前三类不可重试**）：

- ``missing_tool``：``PANGUVERSE_ADB``/PATH 里找不到 adb；
- ``missing_device``：没有处于 ``device`` 状态的设备；
- ``ambiguous_device``：多台设备但未显式指定 ``serial``；
- ``device_command_failed``：adb 返回非零或输出 Error；
- ``timeout``：命令超时（进程组 SIGKILL）。

``uiautomator dump`` 已知偶发抖动，原子内部只对它重试 1 次；其余原子不自动重试。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core.atom import Atom, atom
from ..core.context import RunContext
from ..core.contract import FailureClass, Recovery
from ..core.errors import AtomFailure, PanguError
from .core import GenericSemantics

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT = 30.0
DEFAULT_INTERVAL = 0.5
#: ``input text`` 单次可安全发送的字符上限（超出容易触发 Binder/输入法异常）
MAX_INPUT_TEXT_CHARS = 500
#: 远端 ``input text`` 会经过设备 shell；这些字符不参数化传参就可能被执行/改写
_SHELL_METACHARS = frozenset(";&|<>$`\\\"'")
_TOOL_ENV = "PANGUVERSE_ADB"
_ADB_CANDIDATES = (
    "adb",
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    "~/Library/Android/sdk/platform-tools/adb",
)
_REMOTE_DUMP = "/sdcard/window_dump.xml"
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_LAUNCH_INJECTED_RE = re.compile(r"Events injected:\s*[1-9]\d*", re.IGNORECASE)
_LAUNCH_STARTED_RE = re.compile(
    r"Status:\s*ok|Warning:\s*Activity not started", re.IGNORECASE
)
_LAUNCH_MISSING_RE = re.compile(
    r"No activities found|monkey aborted|does not exist|Unable to resolve Intent",
    re.IGNORECASE,
)
_LAUNCH_ERROR_RE = re.compile(r"(^|\n)\s*(Error|Exception|Failure)\b", re.IGNORECASE)
_FOCUS_PATTERNS = (
    re.compile(r"mCurrentFocus=.*?([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)"),
    re.compile(r"mFocusedApp=.*?([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)"),
)
_RESUMED_PATTERN = re.compile(r"mResumedActivity:.*?([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)")


# --------------------------------------------------------------------------- #
# 工具发现 / 进程执行
# --------------------------------------------------------------------------- #
def resolve_adb() -> str:
    """解析 adb；``PANGUVERSE_ADB`` 优先且不回退。"""
    override = os.environ.get(_TOOL_ENV)
    if override is not None:
        candidate = str(override).strip()
        resolved = str(Path(candidate).expanduser()) if candidate else ""
        if resolved and Path(resolved).exists() and os.access(resolved, os.X_OK):
            return resolved
        raise AtomFailure(
            f"未找到 adb：{_TOOL_ENV}={candidate or '<空>'} 不存在或不可执行；"
            "请安装 Android platform-tools，或把该环境变量指向 adb",
            kind="missing_tool",
            failure_class="missing_resource",
            detail={"tool": "adb", "env": _TOOL_ENV, "path": candidate},
        )
    for candidate in _ADB_CANDIDATES:
        expanded = str(Path(candidate).expanduser())
        if Path(expanded).exists() and os.access(expanded, os.X_OK):
            return expanded
        found = shutil.which(candidate)
        if found:
            return found
    raise AtomFailure(
        "未找到 adb（PATH 与常见 SDK 路径都没有）；请安装 Android platform-tools，"
        f"或设置 {_TOOL_ENV} 指向它",
        kind="missing_tool",
        failure_class="missing_resource",
        detail={"tool": "adb", "env": _TOOL_ENV},
    )


def clip_output(text: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    value = text if isinstance(text, str) else str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    marker = "…（已截断）"
    return value[: max(0, limit - len(marker))] + marker


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _run(
    command: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    binary: bool = False,
) -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise AtomFailure(
            f"工具不存在：{command[0]}",
            kind="missing_tool",
            failure_class="missing_resource",
            detail={"tool": command[0]},
        ) from exc
    except OSError as exc:
        raise AtomFailure(
            f"工具无法执行：{command[0]}（{exc}）",
            kind="missing_tool",
            failure_class="missing_resource",
            detail={"tool": command[0], "error": str(exc)},
        ) from exc
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise AtomFailure(
            f"adb 命令超时（{timeout:g}s）：{' '.join(command[-3:])}",
            kind="timeout",
            failure_class="transient",
            detail={"command": command[-5:], "timeout": timeout},
        ) from exc
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    if binary:
        return int(proc.returncode or 0), out, err
    return int(proc.returncode or 0), out, err


def _text(data: bytes) -> str:
    """解码命令输出，**不截断**；调用方在返回/报错时再 ``clip_output``。"""
    return data.decode("utf-8", errors="replace")


def _device_args(serial: str) -> list[str]:
    return ["-s", serial] if serial else []


def _run_adb(serial: str, args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    command = [resolve_adb(), *_device_args(serial), *args]
    code, out, err = _run(command, timeout=timeout)
    return code, _text(out), _text(err)


def _run_adb_bytes(serial: str, args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes, str]:
    command = [resolve_adb(), *_device_args(serial), *args]
    code, out, err = _run(command, timeout=timeout, binary=True)
    return code, out, _text(err)


# --------------------------------------------------------------------------- #
# 设备探测
# --------------------------------------------------------------------------- #
def parse_devices(output: str) -> list[dict[str, Any]]:
    """解析 ``adb devices -l``。"""
    devices: list[dict[str, Any]] = []
    for raw in str(output or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1].replace("_", " ")
                break
        devices.append({"serial": serial, "state": state, "model": model})
    return devices


def list_devices() -> list[dict[str, Any]]:
    code, out, err = _run_adb("", ["devices", "-l"])
    if code != 0:
        raise AtomFailure(
            f"adb devices 失败（exit={code}）：{clip_output(err) or clip_output(out)}",
            kind="device_command_failed",
            failure_class="internal",
            detail={"exit_code": code, "stderr": clip_output(err)},
        )
    return parse_devices(out)


def _usable_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in devices if item.get("state") == "device"]


def resolve_serial(serial: str | None = None) -> str:
    """解析目标设备：单设备可省略 serial；多设备必须显式指定。"""
    devices = list_devices()
    requested = str(serial or "").strip()
    if requested:
        for item in devices:
            if item.get("serial") == requested:
                if item.get("state") != "device":
                    raise AtomFailure(
                        f"设备 {requested} 当前状态是 {item.get('state')}（不是 device）；"
                        "请在手机上确认 USB 调试授权",
                        kind="missing_device",
                        failure_class="missing_resource",
                        detail={"serial": requested, "state": item.get("state")},
                    )
                return requested
        raise AtomFailure(
            f"指定的设备未连接：{requested}（当前检测到 {len(devices)} 台）",
            kind="missing_device",
            failure_class="missing_resource",
            detail={"serial": requested, "devices": devices},
        )
    usable = _usable_devices(devices)
    if not usable:
        detail = {"devices": devices}
        suffix = ""
        if devices:
            states = ", ".join(f"{item['serial']}={item['state']}" for item in devices)
            suffix = f"（检测到但不可用：{states}）"
        raise AtomFailure(
            f"未连接设备：adb devices 没有处于 device 状态的安卓设备{suffix}；"
            "请用 USB 连接手机并开启 USB 调试",
            kind="missing_device",
            failure_class="missing_resource",
            detail=detail,
        )
    if len(usable) > 1:
        serials = ", ".join(str(item["serial"]) for item in usable)
        raise AtomFailure(
            f"检测到多台安卓设备（{serials}），请用 serial 明确指定目标设备",
            kind="ambiguous_device",
            failure_class="invalid_input",
            detail={"devices": usable},
        )
    return str(usable[0]["serial"])


def _command_failed(code: int, out: str, err: str, *, action: str) -> AtomFailure:
    message = clip_output(err).strip() or clip_output(out).strip() or f"exit={code}"
    return AtomFailure(
        f"{action}失败：{message}",
        kind="device_command_failed",
        failure_class="internal",
        detail={"exit_code": code, "stdout": clip_output(out), "stderr": clip_output(err)},
    )


def _assert_ok(code: int, out: str, err: str, *, action: str) -> str:
    combined = f"{out}\n{err}"
    if code != 0 or re.search(r"(^|\n)\s*(Error|Exception|Failure)\b", combined):
        raise _command_failed(code, out, err, action=action)
    return clip_output(out)


# --------------------------------------------------------------------------- #
# UI XML 解析
# --------------------------------------------------------------------------- #
def _center(bounds: str) -> dict[str, int] | None:
    match = _BOUNDS_RE.search(bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    return {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2}


def parse_ui_xml(xml: str) -> list[dict[str, Any]]:
    """把 uiautomator dump 的 XML 解析成扁平节点列表。"""
    text = str(xml or "").strip()
    if not text:
        raise AtomFailure(
            "uiautomator dump 返回空 XML",
            kind="parse",
            failure_class="invalid_input",
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AtomFailure(
            f"uiautomator XML 解析失败: {exc}",
            kind="parse",
            failure_class="invalid_input",
            detail={"sample": clip_output(text, 500)},
        ) from exc
    nodes: list[dict[str, Any]] = []
    for element in root.iter("node"):
        attrs = element.attrib
        bounds = str(attrs.get("bounds") or "")
        nodes.append(
            {
                "text": str(attrs.get("text") or ""),
                "resource_id": str(attrs.get("resource-id") or ""),
                "bounds": bounds,
                "center": _center(bounds),
                "class": str(attrs.get("class") or ""),
                "package": str(attrs.get("package") or ""),
                "description": str(attrs.get("content-desc") or ""),
                "clickable": str(attrs.get("clickable") or "").lower() == "true",
                "enabled": str(attrs.get("enabled") or "").lower() == "true",
            }
        )
        if len(nodes) >= 1500:
            break
    return nodes


def _dump_once(serial: str, *, timeout: float) -> tuple[str, list[dict[str, Any]]]:
    code, out, err = _run_adb(
        serial, ["shell", "uiautomator", "dump", _REMOTE_DUMP], timeout=timeout
    )
    if code != 0:
        raise _command_failed(code, out, err, action="uiautomator dump ")
    code, xml, err = _run_adb(serial, ["exec-out", "cat", _REMOTE_DUMP], timeout=timeout)
    if code != 0:
        raise _command_failed(code, xml, err, action="读取 uiautomator dump ")
    if not xml.strip():
        raise AtomFailure(
            "uiautomator dump 返回空内容",
            kind="parse",
            failure_class="invalid_input",
        )
    return xml, parse_ui_xml(xml)


def _dump_with_retry(serial: str, *, timeout: float) -> tuple[str, list[dict[str, Any]]]:
    """``uiautomator dump`` 已知抖动：只重试 1 次；超时不重试。"""
    last: AtomFailure | None = None
    for attempt in range(2):
        try:
            return _dump_once(serial, timeout=timeout)
        except AtomFailure as exc:
            last = exc
            if exc.kind in {"missing_tool", "missing_device", "ambiguous_device", "timeout"}:
                raise
            if attempt == 0:
                time.sleep(0.2)
    assert last is not None
    raise last


def _find_node(nodes: list[dict[str, Any]], wanted: str, match: str = "contains") -> dict[str, Any]:
    mode = str(match or "contains").lower()
    for node in nodes:
        values = [str(node.get("text") or ""), str(node.get("description") or "")]
        for value in values:
            if not value:
                continue
            if mode == "exact" and value == wanted:
                return node
            if mode == "regex":
                try:
                    if re.search(wanted, value):
                        return node
                except re.error as exc:
                    raise AtomFailure(
                        f"正则表达式非法: {wanted}（{exc}）",
                        kind="invalid_input",
                        failure_class="invalid_input",
                    ) from exc
            if mode == "contains" and wanted in value:
                return node
    return {}


def _focus_matches(component: str, package: str) -> bool:
    value = str(component or "").strip()
    return value == package or value.startswith(f"{package}/")


def _focused_component(serial: str, *, timeout: float) -> str:
    """从 dumpsys 读取当前前台组件；读不到返回空串，不视为启动失败。"""
    probes = (
        (["shell", "dumpsys", "window"], _FOCUS_PATTERNS),
        (["shell", "dumpsys", "activity", "activities"], (_RESUMED_PATTERN,)),
    )
    for args, patterns in probes:
        code, out, err = _run_adb(serial, args, timeout=timeout)
        if code != 0:
            continue
        text = f"{out}\n{err}"
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return ""


def _wait_for_focus(
    serial: str, package: str, *, timeout: float, attempts: int = 3
) -> str:
    focused = ""
    for attempt in range(attempts):
        focused = _focused_component(serial, timeout=timeout)
        if _focus_matches(focused, package):
            return focused
        if attempt + 1 < attempts:
            time.sleep(0.2)
    return focused


def _resolve_launcher_activity(serial: str, package: str, *, timeout: float) -> str:
    """解析 package 的 LAUNCHER 组件，供 monkey 不可用时回退 am start。"""
    code, out, err = _run_adb(
        serial,
        [
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            package,
        ],
        timeout=timeout,
    )
    if code != 0:
        return ""
    for line in reversed(f"{out}\n{err}".splitlines()):
        candidate = line.strip()
        if re.fullmatch(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+", candidate):
            return candidate
    return ""


# --------------------------------------------------------------------------- #
# 原子
# --------------------------------------------------------------------------- #
@atom(
    "android_devices",
    description="列出 adb 设备（serial/state/model）；无设备返回空数组而不是报错",
    category="device",
    tags=("device", "android", "adb", "read", "tool:adb"),
    inputs={},
    outputs={"devices": "array", "count": "integer"},
    required=(),
    effects=("read",),
    capabilities=("device",),
    domain="android_adb",
)
def android_devices(ctx: RunContext) -> dict[str, Any]:
    devices = list_devices()
    return {"devices": devices, "count": len(devices)}


@atom(
    "android_launch_app",
    description="启动安卓应用（am start -n；无 activity 时用 monkey 启动 Launcher）",
    category="device",
    tags=("device", "android", "adb", "mutate", "tool:adb"),
    inputs={"package": "string", "activity?": "string", "serial?": "string", "timeout?": "number"},
    outputs={
        "ok": "boolean",
        "output": "string",
        "focused": "string",
        "verified": "boolean",
        "message": "string",
        "serial": "string",
    },
    required=("package",),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="android_adb",
)
def android_launch_app(
    ctx: RunContext,
    package: str,
    activity: str | None = None,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    pkg = str(package or "").strip()
    if not pkg:
        raise AtomFailure("android_launch_app 需要 package", kind="invalid_input", failure_class="invalid_input")
    target = resolve_serial(serial)
    limit = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    if activity:
        act = str(activity).strip()
        component = act if "/" in act else f"{pkg}/{act}"
        args = ["shell", "am", "start", "-W", "-n", component]
    else:
        args = ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"]

    code, out, err = _run_adb(target, args, timeout=limit)
    raw = f"{out}\n{err}"
    if _LAUNCH_MISSING_RE.search(raw):
        raise AtomFailure(
            f"启动应用失败：{clip_output(raw)}",
            kind="device_command_failed",
            failure_class="internal",
            detail={"package": pkg, "serial": target, "output": clip_output(raw)},
        )

    # monkey / am start 都可能出现「实际已拉起但退出码非零」。以输出标记 +
    # dumpsys 前台组件作为权威判定，退出码只用于无法验证时兜底报错。
    success = bool(_LAUNCH_INJECTED_RE.search(raw) or _LAUNCH_STARTED_RE.search(raw))
    probe_timeout = min(5.0, max(1.0, limit))
    focused = _wait_for_focus(target, pkg, timeout=probe_timeout)
    verified = _focus_matches(focused, pkg)
    message = ""

    # monkey 在部分模拟器上会因 SYS_KEYS 警告直接退出（如 251），此时用
    # resolve-activity 回退到 am start -W，避免把已可启动的应用误判为失败。
    if not activity and not success and not verified:
        resolved = _resolve_launcher_activity(target, pkg, timeout=probe_timeout)
        if resolved:
            code2, out2, err2 = _run_adb(
                target, ["shell", "am", "start", "-W", "-n", resolved], timeout=limit
            )
            raw2 = f"{out2}\n{err2}"
            if not _LAUNCH_MISSING_RE.search(raw2):
                raw = f"{raw}\n[fallback am start -W -n {resolved}]\n{raw2}"
                code, out, err = code2, out2, err2
                success = bool(_LAUNCH_STARTED_RE.search(raw2))
                focused = _wait_for_focus(target, pkg, timeout=probe_timeout)
                verified = _focus_matches(focused, pkg)
                message = f"monkey 未确认启动，已回退 am start -W -n {resolved}"

    if not verified and activity and not success and (
        code != 0 or _LAUNCH_ERROR_RE.search(raw)
    ):
        raise _command_failed(code, out, err, action="启动应用")

    if verified:
        confirmed = f"已确认前台组件：{focused}"
        message = f"{message}；{confirmed}" if message else confirmed
    elif success:
        message = (
            f"{message}；启动命令报告成功，但 dumpsys 未确认前台组件"
            f"（focused={focused or '<未知>'}）"
        )
    else:
        message = (
            f"{message}；启动命令未报告明确成功，dumpsys 也未确认前台组件"
            "；按不可验证处理（ok=true, verified=false）"
        )

    return {
        "ok": True,
        "output": clip_output(raw),
        "focused": focused,
        "verified": verified,
        "message": message,
        "serial": target,
    }


@atom(
    "android_stop_app",
    description="强制停止安卓应用（am force-stop）",
    category="device",
    tags=("device", "android", "adb", "mutate", "tool:adb"),
    inputs={"package": "string", "serial?": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "serial": "string"},
    required=("package",),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="android_adb",
)
def android_stop_app(
    ctx: RunContext,
    package: str,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    pkg = str(package or "").strip()
    if not pkg:
        raise AtomFailure("android_stop_app 需要 package", kind="invalid_input", failure_class="invalid_input")
    target = resolve_serial(serial)
    code, out, err = _run_adb(
        target, ["shell", "am", "force-stop", pkg], timeout=timeout or DEFAULT_TIMEOUT
    )
    _assert_ok(code, out, err, action="停止应用")
    return {"ok": True, "serial": target}


@atom(
    "android_tap",
    description="在安卓设备上点击坐标（input tap）",
    category="device",
    tags=("device", "android", "adb", "mutate", "tool:adb"),
    inputs={"x": "integer", "y": "integer", "serial?": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "serial": "string"},
    required=("x", "y"),
    effects=("mutate",),
    capabilities=("device",),
    domain="android_adb",
)
def android_tap(
    ctx: RunContext,
    x: int,
    y: int,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    target = resolve_serial(serial)
    code, out, err = _run_adb(
        target,
        ["shell", "input", "tap", str(int(x)), str(int(y))],
        timeout=timeout or DEFAULT_TIMEOUT,
    )
    _assert_ok(code, out, err, action="点击")
    return {"ok": True, "serial": target}


@atom(
    "android_swipe",
    description="在安卓设备上滑动（input swipe x1 y1 x2 y2 duration）",
    category="device",
    tags=("device", "android", "adb", "mutate", "tool:adb"),
    inputs={
        "x1": "integer",
        "y1": "integer",
        "x2": "integer",
        "y2": "integer",
        "duration?": "integer",
        "serial?": "string",
        "timeout?": "number",
    },
    outputs={"ok": "boolean", "serial": "string"},
    required=("x1", "y1", "x2", "y2"),
    effects=("mutate",),
    capabilities=("device",),
    domain="android_adb",
)
def android_swipe(
    ctx: RunContext,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: int | None = None,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    target = resolve_serial(serial)
    args = ["shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2))]
    if duration is not None:
        args.append(str(max(0, int(duration))))
    code, out, err = _run_adb(target, args, timeout=timeout or DEFAULT_TIMEOUT)
    _assert_ok(code, out, err, action="滑动")
    return {"ok": True, "serial": target}


@atom(
    "android_input_text",
    description="向安卓设备输入文本（空格转义为 %s）",
    category="device",
    tags=("device", "android", "adb", "mutate", "tool:adb"),
    inputs={"text": "string", "serial?": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "serial": "string", "sent": "string", "escaped": "boolean"},
    required=("text",),
    effects=("mutate",),
    capabilities=("device",),
    domain="android_adb",
)
def android_input_text(
    ctx: RunContext,
    text: str,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    value = str(text if text is not None else "")
    if not value:
        raise AtomFailure("android_input_text 需要非空 text", kind="invalid_input", failure_class="invalid_input")
    if len(value) > MAX_INPUT_TEXT_CHARS:
        raise AtomFailure(
            f"android_input_text 文本过长：{len(value)} 字符，上限 {MAX_INPUT_TEXT_CHARS}",
            kind="invalid_input",
            failure_class="invalid_input",
            detail={"length": len(value), "limit": MAX_INPUT_TEXT_CHARS},
        )
    control = [char for char in value if ord(char) < 0x20 or ord(char) == 0x7F]
    if control:
        raise AtomFailure(
            "android_input_text 不支持控制字符（换行/制表等）；请把文本拆成多次输入",
            kind="invalid_input",
            failure_class="invalid_input",
            detail={"control_chars": sorted({f"U+{ord(char):04X}" for char in control})},
        )
    danger = sorted({char for char in value if char in _SHELL_METACHARS})
    if danger:
        raise AtomFailure(
            f"android_input_text 文本含 shell 危险字符：{''.join(danger)}；"
            "为避免在设备端执行任意命令，已拒绝发送",
            kind="invalid_input",
            failure_class="invalid_input",
            detail={"dangerous_chars": danger},
        )
    # ``adb shell input text`` 不支持非 ASCII（中文会触发设备侧 Binder NPE）。
    # 必须在解析设备、调用 adb 之前显式失败，绝不把这类文本发下去。
    if not value.isascii():
        raise AtomFailure(
            "android_input_text 不支持中文/非 ASCII 文本；"
            "请安装并切换到 ADBKeyboard 等输入法后用广播输入，或改用剪贴板粘贴",
            kind="unsupported_input",
            failure_class="invalid_input",
            detail={
                "text": value,
                "non_ascii_chars": sum(1 for char in value if not char.isascii()),
                "hint": "ADBKeyboard / 剪贴板粘贴",
                "retryable": False,
            },
        )
    target = resolve_serial(serial)
    # 经典转义：设备端 shell 把 %s 还原为空格（必须作为单个 argv 传入，不拼命令字符串）
    sent = value.replace(" ", "%s")
    code, out, err = _run_adb(
        target, ["shell", "input", "text", sent], timeout=timeout or DEFAULT_TIMEOUT
    )
    _assert_ok(code, out, err, action="输入文本")
    return {
        "ok": True,
        "serial": target,
        "sent": sent,
        "escaped": sent != value,
    }


@atom(
    "android_keyevent",
    description="发送安卓按键事件（如 KEYCODE_BACK / 4）",
    category="device",
    tags=("device", "android", "adb", "mutate", "tool:adb"),
    inputs={"key": "string", "serial?": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "serial": "string"},
    required=("key",),
    effects=("mutate",),
    capabilities=("device",),
    domain="android_adb",
)
def android_keyevent(
    ctx: RunContext,
    key: str,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    value = str(key or "").strip()
    if not value:
        raise AtomFailure("android_keyevent 需要 key", kind="invalid_input", failure_class="invalid_input")
    target = resolve_serial(serial)
    code, out, err = _run_adb(
        target, ["shell", "input", "keyevent", value], timeout=timeout or DEFAULT_TIMEOUT
    )
    _assert_ok(code, out, err, action="发送按键")
    return {"ok": True, "serial": target}


def _artifact_root(ctx: RunContext) -> Path:
    """产物根目录固定为 ``<workspace>/.panguverse``。

    ``RunContext.workspace`` 可能是 ``Workspace`` 对象（root 已是 ``.panguverse``），
    也可能是用户直接传入的工作区路径（root 是工作区本身）。两种情况都必须落到
    ``<workspace>/.panguverse/artifacts/...``，不能污染工作区根目录。
    """
    workspace = getattr(ctx, "workspace", None)
    root_value = getattr(workspace, "root", workspace)
    base = Path(root_value) if root_value else Path(".")
    return base if base.name == ".panguverse" else base / ".panguverse"


def _artifact_dir(ctx: RunContext, kind: str) -> tuple[Path, Path]:
    root = _artifact_root(ctx)
    target = root / "artifacts" / kind
    target.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
        (root / "artifacts").mkdir(parents=True, exist_ok=True)
        os.chmod(root / "artifacts", 0o700)
        os.chmod(target, 0o700)
    return target, root


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


@atom(
    "android_screencap",
    description="安卓截图（exec-out screencap -p）→ .panguverse/artifacts/android/（0600）",
    category="device",
    tags=("device", "android", "adb", "screenshot", "write", "tool:adb"),
    inputs={"serial?": "string", "name?": "string", "timeout?": "number"},
    outputs={"path": "string", "bytes": "integer", "serial": "string"},
    required=(),
    effects=("read", "write"),
    capabilities=("device",),
    domain="android_adb",
)
def android_screencap(
    ctx: RunContext,
    serial: str | None = None,
    name: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    target = resolve_serial(serial)
    code, data, err = _run_adb_bytes(
        target, ["exec-out", "screencap", "-p"], timeout=timeout or DEFAULT_TIMEOUT
    )
    if code != 0 or not data:
        raise _command_failed(code, "", err, action="截图")
    directory, root = _artifact_dir(ctx, "android")
    filename = Path(str(name)).name if name else f"android-{int(time.time() * 1000)}-{os.getpid()}.png"
    if not filename.lower().endswith(".png"):
        filename += ".png"
    path = directory / filename
    path.write_bytes(data)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return {"path": _relative(path, root), "bytes": len(data), "serial": target}


@atom(
    "android_ui_dump",
    description="uiautomator dump 并解析 UI XML 为节点列表（text/resource-id/bounds）",
    category="device",
    tags=("device", "android", "adb", "read", "tool:adb"),
    inputs={"serial?": "string", "timeout?": "number"},
    outputs={
        "xml": "string",
        "xml_chars": "integer",
        "nodes": "array",
        "count": "integer",
        "serial": "string",
    },
    required=(),
    effects=("read",),
    capabilities=("device",),
    domain="android_adb",
)
def android_ui_dump(
    ctx: RunContext,
    serial: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    target = resolve_serial(serial)
    xml, nodes = _dump_with_retry(target, timeout=timeout or DEFAULT_TIMEOUT)
    return {
        # XML 回显可以截断（完整内容可能远大于 MAX_OUTPUT_CHARS），但必须
        # 保留原始总长度，且解析始终基于截断前的完整 XML。
        "xml": clip_output(xml),
        "xml_chars": len(xml),
        "nodes": nodes,
        "count": len(nodes),
        "serial": target,
    }


@atom(
    "android_find_text",
    description="在 UI XML/节点里查找文本（contains/exact/regex），纯解析无副作用",
    category="device",
    tags=("device", "android", "read"),
    inputs={"text": "string", "xml?": "string", "nodes?": "array", "match?": "string"},
    outputs={"found": "boolean", "node": "object", "matches": "integer"},
    required=("text",),
    effects=("pure",),
    domain="android_adb",
)
def android_find_text(
    ctx: RunContext,
    text: str,
    xml: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
    match: str = "contains",
) -> dict[str, Any]:
    wanted = str(text or "")
    if not wanted:
        raise AtomFailure("android_find_text 需要 text", kind="invalid_input", failure_class="invalid_input")
    if nodes is None:
        if not xml:
            raise AtomFailure(
                "android_find_text 需要 xml 或 nodes（可接 android_ui_dump 的输出）",
                kind="invalid_input",
                failure_class="invalid_input",
            )
        nodes = parse_ui_xml(xml)
    target = _find_node(list(nodes), wanted, match)
    return {"found": bool(target), "node": target, "matches": 1 if target else 0}


@atom(
    "android_wait_text",
    description="轮询 uiautomator dump 直到文本出现；超时明确失败",
    category="device",
    tags=("device", "android", "adb", "wait", "read", "tool:adb"),
    inputs={
        "text": "string",
        "serial?": "string",
        "timeout?": "number",
        "interval?": "number",
        "match?": "string",
    },
    outputs={"found": "boolean", "attempts": "integer", "node": "object", "serial": "string"},
    required=("text",),
    effects=("read",),
    capabilities=("device",),
    domain="android_adb",
)
def android_wait_text(
    ctx: RunContext,
    text: str,
    serial: str | None = None,
    timeout: float | None = None,
    interval: float | None = None,
    match: str = "contains",
) -> dict[str, Any]:
    wanted = str(text or "")
    if not wanted:
        raise AtomFailure("android_wait_text 需要 text", kind="invalid_input", failure_class="invalid_input")
    target = resolve_serial(serial)
    limit = float(timeout) if timeout is not None else 10.0
    step = max(0.1, float(interval if interval is not None else DEFAULT_INTERVAL))
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        _xml, nodes = _dump_with_retry(target, timeout=max(5.0, limit))
        node = _find_node(nodes, wanted, match)
        if node:
            return {"found": True, "attempts": attempts, "node": node, "serial": target}
        remaining = limit - (time.monotonic() - started)
        if remaining <= 0.2:
            break
        time.sleep(min(step, remaining))
    raise AtomFailure(
        f"等待安卓文本超时：{wanted!r} 在 {attempts} 次 UI dump 内未出现",
        kind="timeout",
        failure_class="transient",
        detail={"text": wanted, "attempts": attempts, "serial": target},
    )


# --------------------------------------------------------------------------- #
# 领域语义
# --------------------------------------------------------------------------- #
class AndroidSemantics(GenericSemantics):
    """安卓领域语义：设备/工具类失败必须显式且不自动重试。"""

    name = "android_adb"

    def failure_class(
        self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None
    ) -> FailureClass:
        if isinstance(exc, AtomFailure):
            try:
                return FailureClass(exc.failure_class)
            except ValueError:
                return FailureClass.INTERNAL
        return super().failure_class(atom, outputs, exc)

    def failure_kind(
        self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None
    ) -> str:
        if isinstance(exc, PanguError) and getattr(exc, "kind", ""):
            return str(exc.kind)
        return super().failure_kind(atom, outputs, exc)

    def is_retryable(
        self,
        atom: Any,
        outputs: Mapping[str, Any] | None,
        *,
        kind: str,
        failure_class: FailureClass,
    ) -> bool:
        # 设备状态不可假定可重试；uiautomator 的抖动已在原子内部处理
        return False

    def recoveries(
        self,
        atom: Any,
        *,
        kind: str,
        failure_class: FailureClass,
        case: Any = None,
        step: Any = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> list[Recovery]:
        if kind in {"missing_tool", "missing_device", "ambiguous_device"}:
            return [
                Recovery(
                    action="none",
                    reason="设备环境不具备执行条件，重试不会改变结果",
                    advice=self.advice(kind=kind, failure_class=failure_class),
                )
            ]
        return super().recoveries(
            atom, kind=kind, failure_class=failure_class, case=case, step=step, inputs=inputs
        )

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        texts = {
            "missing_tool": "未找到 adb：安装 Android platform-tools 或设置 PANGUVERSE_ADB",
            "missing_device": "未连接设备：USB 连接手机并开启 USB 调试，确认 adb devices 显示 device",
            "ambiguous_device": "多台设备：在步骤里显式填写 serial（adb devices 的第一列）",
            "device_command_failed": "adb 命令失败：查看 stderr，确认应用/坐标/包名有效",
            "timeout": "adb 命令超时：确认设备有响应，必要时放宽 timeout",
            "parse": "UI XML 无法解析：确认 uiautomator dump 正常产出 XML",
            "invalid_input": "参数不合法：检查文本、坐标、包名与匹配模式",
            "unsupported_input": (
                "不支持中文/非 ASCII 文本：安装 ADBKeyboard 等输入法并用广播输入，"
                "或改用剪贴板粘贴"
            ),
        }
        return texts.get(kind) or super().advice(kind=kind, failure_class=failure_class)

    def planning_hints(self) -> tuple[str, ...]:
        return (
            "先跑 android_devices 探测；无设备时返回空数组，不要编造成功",
            "tap/input/swipe/keyevent/launch/stop 都是 mutate=true，发布前需要风险确认",
            "坐标可从 android_ui_dump 的 nodes[].center 或 bounds 计算",
            "文本查找用 android_find_text(nodes=...)，等待用 android_wait_text",
            "多设备必须显式 serial，否则 ambiguous_device",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": "android_adb",
            "target": "android device",
            "atoms": [
                "android_devices",
                "android_launch_app",
                "android_stop_app",
                "android_tap",
                "android_swipe",
                "android_input_text",
                "android_keyevent",
                "android_screencap",
                "android_ui_dump",
                "android_find_text",
                "android_wait_text",
            ],
            "read_only": False,
            "needs_tool": "adb",
        }


def build_android_launch_shot() -> dict[str, Any]:
    """「启动应用 → 截图」的参数化计划。"""
    return {
        "id": "android_launch_screenshot",
        "name": "安卓启动应用并截图",
        "tags": ["android", "device", "screenshot"],
        "description": "在已连接的安卓设备上启动应用并抓一张截图",
        "steps": [
            {
                "id": "launch",
                "use": "android_launch_app",
                "name": "启动安卓应用",
                "with": {"package": "${env.APP_PACKAGE}"},
            },
            {
                "id": "device",
                "use": "android_devices",
                "name": "确认设备在线",
            },
            {
                "id": "shot",
                "use": "android_screencap",
                "name": "抓取截图",
                "with": {"name": "android-launch.png"},
            },
        ],
    }


def build_android_devices() -> dict[str, Any]:
    """「列出/探测安卓设备」的参数化计划。"""
    return {
        "id": "android_devices_probe",
        "name": "探测安卓设备",
        "tags": ["android", "device", "probe"],
        "description": "列出 adb 设备；无设备时返回空数组",
        "steps": [
            {"id": "devices", "use": "android_devices", "name": "列出安卓设备"},
        ],
    }


def build_android_tap() -> dict[str, Any]:
    """「点击安卓坐标」的参数化计划（坐标可用环境变量覆盖）。"""
    return {
        "id": "android_tap_coordinates",
        "name": "安卓点击坐标",
        "tags": ["android", "device", "tap"],
        "description": "在已连接的安卓设备上点击给定坐标",
        "steps": [
            {
                "id": "devices",
                "use": "android_devices",
                "name": "确认设备在线",
            },
            {
                "id": "tap",
                "use": "android_tap",
                "name": "点击坐标",
                "with": {
                    "x": "${env.ANDROID_X|default:100|int}",
                    "y": "${env.ANDROID_Y|default:200|int}",
                },
            },
        ],
    }


def android_intent_templates() -> list[Any]:
    from ..core.contract import RuleTemplate

    return [
        RuleTemplate(
            name="android_launch_screenshot",
            description="安卓启动应用并截图",
            keywords=("安卓", "android", "adb", "启动应用", "安卓截图", "安卓设备"),
            builder=build_android_launch_shot,
            tags=("android", "device"),
            weight=1,
            domain="android_adb",
            pack="android_adb",
        ),
        RuleTemplate(
            name="android_devices_probe",
            description="探测/列出安卓设备（无设备返回空数组）",
            keywords=("安卓设备", "安卓设备列表", "adb devices", "连接设备"),
            builder=build_android_devices,
            tags=("android", "device", "probe"),
            weight=2,
            domain="android_adb",
            pack="android_adb",
        ),
        RuleTemplate(
            name="android_tap_coordinates",
            description="在安卓设备上点击坐标",
            keywords=("安卓点击", "点击坐标", "安卓 tap", "tap 坐标"),
            builder=build_android_tap,
            tags=("android", "device", "tap"),
            weight=2,
            domain="android_adb",
            pack="android_adb",
        ),
    ]


def _mark_tool(atom_obj: Atom, tool: str) -> Atom:
    atom_obj.needs_tool = tool  # type: ignore[attr-defined]
    atom_obj.tags = tuple({*atom_obj.tags, f"tool:{tool}"})
    return atom_obj


def build_pack() -> Any:
    from ..core.pack import AtomPack

    atoms: list[Atom] = [
        android_devices,
        android_launch_app,
        android_stop_app,
        android_tap,
        android_swipe,
        android_input_text,
        android_keyevent,
        android_screencap,
        android_ui_dump,
        android_find_text,
        android_wait_text,
    ]
    for item in atoms:
        item.domain = "android_adb"
        _mark_tool(item, "adb")
    return AtomPack(
        name="android_adb",
        domain="android_adb",
        version="0.1.0",
        description="安卓设备领域包：adb 设备探测、应用启停、点击/滑动/输入、截图与 UI XML 解析",
        semantics=AndroidSemantics(),
        atoms=atoms,
        requires=("device", "process"),
        tags=("builtin", "android", "device", "adb"),
        license="Apache-2.0",
        intent_templates=android_intent_templates(),
    )


__all__ = [
    "MAX_OUTPUT_CHARS",
    "AndroidSemantics",
    "android_devices",
    "android_find_text",
    "android_input_text",
    "android_intent_templates",
    "android_keyevent",
    "android_launch_app",
    "android_screencap",
    "android_stop_app",
    "android_swipe",
    "android_tap",
    "android_ui_dump",
    "android_wait_text",
    "build_android_launch_shot",
    "build_pack",
    "clip_output",
    "list_devices",
    "parse_devices",
    "parse_ui_xml",
    "resolve_adb",
    "resolve_serial",
]
