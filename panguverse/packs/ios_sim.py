"""``ios_sim`` 领域包：通过 ``xcrun simctl`` 控制 iOS 模拟器。

契约（``tasks/device-web-atoms-spec.md`` v7.0 §4）::

=====================  =================================================
原子                    语义
=====================  =================================================
ios_sim_list           列出可用模拟器；无模拟器返回 ``[]``，不报错
ios_sim_boot           启动模拟器（已 Booted 直接 ok）
ios_sim_shutdown       关闭模拟器
ios_sim_install        安装 .app，mutate
ios_sim_launch         启动 bundle，返回 pid
ios_sim_terminate      结束 bundle
ios_sim_openurl        打开深链/网页
ios_sim_screenshot     截图 → .panguverse/artifacts/ios/
=====================  =================================================

**明确的能力缺口**：iOS 元素级交互（点击/输入/滑动）本轮不提供。它需要
XCUITest 或 WebDriverAgent，是另一套运行时；规划器遇到这类意图应返回
``capability_gap``，而不是套用安卓的 tap/input 原子。能力缺口在
:data:`IOS_CAPABILITY_GAPS` 中显式声明，并挂在包的 ``capability_gaps`` 属性上。

工具路径：``PANGUVERSE_SIMCTL`` 可覆盖。它既可以指向 ``xcrun``（自动补
``simctl`` 子命令），也可以指向独立的 ``simctl`` 兼容桩程序。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
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
_TOOL_ENV = "PANGUVERSE_SIMCTL"
_XCRUN_CANDIDATES = ("xcrun", "/usr/bin/xcrun")

#: simctl 在「没有已启动模拟器」时返回的诊断文本（真机 code 405）。
_MISSING_DEVICE_MARKERS = (
    "unable to lookup",
    "no booted",
    "current state: shutdown",
)
#: 结构化输出里的 simctl 错误码（``code=405`` / ``"code": 405``）。
_SIMCTL_405_RE = re.compile(r'(?:\bcode\s*=\s*405\b|"code"\s*:\s*405\b)')

#: iOS 交互能力缺口：规划器必须返回 capability_gap，不得伪造原子
IOS_CAPABILITY_GAPS = (
    "ios_tap（iOS 元素点击，需要 XCUITest/WebDriverAgent）",
    "ios_input_text（iOS 文本输入，需要 XCUITest/WebDriverAgent）",
    "ios_swipe（iOS 滑动，需要 XCUITest/WebDriverAgent）",
)


# --------------------------------------------------------------------------- #
# 工具发现 / 进程执行
# --------------------------------------------------------------------------- #
def resolve_simctl() -> list[str]:
    """返回 simctl 命令前缀；``PANGUVERSE_SIMCTL`` 优先且不回退。"""
    override = os.environ.get(_TOOL_ENV)
    if override is not None:
        candidate = str(override).strip()
        resolved = str(Path(candidate).expanduser()) if candidate else ""
        if resolved and Path(resolved).exists() and os.access(resolved, os.X_OK):
            if Path(resolved).name == "xcrun":
                return [resolved, "simctl"]
            return [resolved]
        raise AtomFailure(
            f"未找到 simctl：{_TOOL_ENV}={candidate or '<空>'} 不存在或不可执行；"
            "请安装 Xcode 命令行工具，或把该环境变量指向 xcrun/simctl",
            kind="missing_tool",
            failure_class="missing_resource",
            detail={"tool": "simctl", "env": _TOOL_ENV, "path": candidate},
        )
    for candidate in _XCRUN_CANDIDATES:
        expanded = str(Path(candidate).expanduser())
        if Path(expanded).exists() and os.access(expanded, os.X_OK):
            return [expanded, "simctl"]
        found = shutil.which(candidate)
        if found:
            return [found, "simctl"]
    raise AtomFailure(
        "未找到 xcrun/simctl（需要 Xcode 或 Command Line Tools）；"
        f"请安装后重试，或设置 {_TOOL_ENV}",
        kind="missing_tool",
        failure_class="missing_resource",
        detail={"tool": "simctl", "env": _TOOL_ENV},
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


def _run_simctl(args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    command = [*resolve_simctl(), *args]
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
            detail={"tool": "simctl"},
        ) from exc
    except OSError as exc:
        raise AtomFailure(
            f"工具无法执行：{command[0]}（{exc}）",
            kind="missing_tool",
            failure_class="missing_resource",
            detail={"tool": "simctl", "error": str(exc)},
        ) from exc
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise AtomFailure(
            f"simctl 命令超时（{timeout:g}s）：{' '.join(args[:3])}",
            kind="timeout",
            failure_class="transient",
            detail={"args": args[:5], "timeout": timeout},
        ) from exc
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    # 结构化输出（如 ``list -j``）必须拿完整 stdout 再解析；截断只发生在
    # 展示字段与错误消息层（``_assert_ok`` / ``_command_failed`` / ``clip_output``）。
    return (
        int(proc.returncode or 0),
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _is_missing_device(code: int, out: str, err: str) -> bool:
    """判断 simctl 失败是否等价于「没有已启动的模拟器」。

    真机在 ``simctl shutdown all`` 之后执行 launch/terminate/openurl/screenshot/install
    会返回 exit 405 或消息 ``Unable to lookup in current state: Shutdown``；这类失败
    属于环境缺失，不是命令参数写错，必须归到 ``missing_device``。
    """
    if int(code) == 405:
        return True
    text = f"{out}\n{err}"
    lowered = text.lower()
    if any(marker in lowered for marker in _MISSING_DEVICE_MARKERS):
        return True
    return bool(_SIMCTL_405_RE.search(text))


def _missing_device_failure(code: int, out: str, err: str, *, action: str) -> AtomFailure:
    raw = clip_output(err).strip() or clip_output(out).strip() or f"exit={code}"
    return AtomFailure(
        f"{action}失败：没有已启动的模拟器，请先 boot"
        "（用 ios_sim_list 查看可启动设备，再 ios_sim_boot 或 xcrun simctl boot <udid>）；"
        f"simctl 返回：{raw}",
        kind="missing_device",
        failure_class="missing_resource",
        detail={
            "exit_code": code,
            "stdout": clip_output(out),
            "stderr": clip_output(err),
            "simctl_code": 405,
            "hint": "没有已启动的模拟器，请先 boot",
        },
    )


def _command_failed(code: int, out: str, err: str, *, action: str) -> AtomFailure:
    if _is_missing_device(code, out, err):
        return _missing_device_failure(code, out, err, action=action)
    message = clip_output(err).strip() or clip_output(out).strip() or f"exit={code}"
    return AtomFailure(
        f"{action}失败：{message}",
        kind="device_command_failed",
        failure_class="internal",
        detail={"exit_code": code, "stdout": clip_output(out), "stderr": clip_output(err)},
    )


def _assert_ok(code: int, out: str, err: str, *, action: str) -> str:
    if code != 0:
        raise _command_failed(code, out, err, action=action)
    return clip_output(out or err)


# --------------------------------------------------------------------------- #
# 模拟器列表
# --------------------------------------------------------------------------- #
def _pretty_runtime(runtime: str) -> str:
    value = str(runtime or "")
    prefix = "com.apple.CoreSimulator.SimRuntime."
    if value.startswith(prefix):
        value = value[len(prefix) :]
    return value


def parse_simctl_devices(payload: str) -> list[dict[str, Any]]:
    """解析 ``simctl list -j devices``，只保留可用模拟器。"""
    text = str(payload or "").strip()
    if not text:
        return []
    start = text.find("{")
    if start > 0:
        text = text[start:]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AtomFailure(
            f"simctl 设备列表不是合法 JSON（{len(text)} 字符）: {exc}",
            kind="parse",
            failure_class="invalid_input",
            detail={"chars": len(text), "sample": clip_output(text, 500)},
        ) from exc
    raw = data.get("devices") if isinstance(data, Mapping) else None
    if not isinstance(raw, Mapping):
        return []
    devices: list[dict[str, Any]] = []
    for runtime, items in raw.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if item.get("isAvailable") is False:
                continue
            state = str(item.get("state") or "")
            devices.append(
                {
                    "name": str(item.get("name") or ""),
                    "udid": str(item.get("udid") or ""),
                    "state": state,
                    "booted": state == "Booted",
                    "runtime": _pretty_runtime(str(runtime)),
                    "runtime_id": str(runtime),
                }
            )
    devices.sort(key=lambda item: (item.get("runtime", ""), item.get("name", ""), item.get("udid", "")))
    return devices


def list_simulators() -> list[dict[str, Any]]:
    code, out, err = _run_simctl(["list", "-j", "devices"])
    if code != 0:
        raise _command_failed(code, out, err, action="simctl 列出模拟器")
    return parse_simctl_devices(out)


def _resolve_device(udid: str, devices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = devices if devices is not None else list_simulators()
    target = str(udid or "").strip()
    if not items:
        raise AtomFailure(
            "没有可用的 iOS 模拟器：simctl list 为空；请在 Xcode 中创建/下载模拟器",
            kind="missing_device",
            failure_class="missing_resource",
            detail={"tool": "simctl", "udid": target},
        )
    for item in items:
        if item.get("udid") == target:
            return item
    known = ", ".join(str(item.get("udid")) for item in items[:6])
    raise AtomFailure(
        f"未找到模拟器 {target or '<空>'}（当前可用: {known}）",
        kind="missing_device",
        failure_class="missing_resource",
        detail={"udid": target, "devices": items},
    )


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


# --------------------------------------------------------------------------- #
# 原子
# --------------------------------------------------------------------------- #
@atom(
    "ios_sim_list",
    description="列出可用 iOS 模拟器（name/udid/state/booted/runtime）；无模拟器返回空数组",
    category="device",
    tags=("device", "ios", "simctl", "read", "tool:simctl"),
    inputs={},
    outputs={"devices": "array", "count": "integer", "booted_count": "integer"},
    required=(),
    effects=("read",),
    capabilities=("device",),
    domain="ios_sim",
)
def ios_sim_list(ctx: RunContext) -> dict[str, Any]:
    """列出全部可用模拟器；``booted``/``booted_count`` 供调用方自行判断可用性。"""
    devices = list_simulators()
    return {
        "devices": devices,
        "count": len(devices),
        "booted_count": sum(1 for item in devices if item.get("booted")),
    }


@atom(
    "ios_sim_boot",
    description="启动 iOS 模拟器；已 Booted 时直接返回 ok",
    category="device",
    tags=("device", "ios", "simctl", "mutate", "tool:simctl"),
    inputs={"udid": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "state": "string", "udid": "string"},
    required=("udid",),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="ios_sim",
)
def ios_sim_boot(
    ctx: RunContext,
    udid: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    device = _resolve_device(udid)
    if str(device.get("state")) == "Booted":
        return {"ok": True, "state": "Booted", "udid": device["udid"]}
    code, out, err = _run_simctl(["boot", device["udid"]], timeout=timeout or DEFAULT_TIMEOUT)
    _assert_ok(code, out, err, action="启动模拟器")
    return {"ok": True, "state": "Booted", "udid": device["udid"]}


@atom(
    "ios_sim_shutdown",
    description="关闭 iOS 模拟器；已 Shutdown 时直接返回 ok",
    category="device",
    tags=("device", "ios", "simctl", "mutate", "tool:simctl"),
    inputs={"udid": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "state": "string", "udid": "string"},
    required=("udid",),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="ios_sim",
)
def ios_sim_shutdown(
    ctx: RunContext,
    udid: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    device = _resolve_device(udid)
    if str(device.get("state")) == "Shutdown":
        return {"ok": True, "state": "Shutdown", "udid": device["udid"]}
    code, out, err = _run_simctl(["shutdown", device["udid"]], timeout=timeout or DEFAULT_TIMEOUT)
    _assert_ok(code, out, err, action="关闭模拟器")
    return {"ok": True, "state": "Shutdown", "udid": device["udid"]}


@atom(
    "ios_sim_install",
    description="把 .app 安装到 iOS 模拟器（simctl install）；mutate",
    category="device",
    tags=("device", "ios", "simctl", "mutate", "tool:simctl"),
    inputs={"udid": "string", "app_path": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "udid": "string", "app_path": "string"},
    required=("udid", "app_path"),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="ios_sim",
)
def ios_sim_install(
    ctx: RunContext,
    udid: str,
    app_path: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    path = str(Path(str(app_path or "")).expanduser())
    if not path or not Path(path).exists():
        raise AtomFailure(
            f"待安装的 .app 不存在：{path or '<空>'}",
            kind="file_not_found",
            failure_class="missing_resource",
            detail={"app_path": path},
        )
    device = _resolve_device(udid)
    code, out, err = _run_simctl(
        ["install", device["udid"], path], timeout=timeout or DEFAULT_TIMEOUT
    )
    _assert_ok(code, out, err, action="安装应用")
    return {"ok": True, "udid": device["udid"], "app_path": path}


@atom(
    "ios_sim_launch",
    description="在模拟器上启动 App（simctl launch），返回 pid",
    category="device",
    tags=("device", "ios", "simctl", "mutate", "tool:simctl"),
    inputs={"udid": "string", "bundle_id": "string", "timeout?": "number"},
    outputs={"pid": "integer", "ok": "boolean", "udid": "string", "bundle_id": "string"},
    required=("udid", "bundle_id"),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="ios_sim",
)
def ios_sim_launch(
    ctx: RunContext,
    udid: str,
    bundle_id: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    bundle = str(bundle_id or "").strip()
    if not bundle:
        raise AtomFailure("ios_sim_launch 需要 bundle_id", kind="invalid_input", failure_class="invalid_input")
    device = _resolve_device(udid)
    code, out, err = _run_simctl(
        ["launch", device["udid"], bundle], timeout=timeout or DEFAULT_TIMEOUT
    )
    output = _assert_ok(code, out, err, action="启动应用")
    match = re.search(r":\s*(\d+)\s*$", output.strip())
    pid = int(match.group(1)) if match else 0
    return {
        "pid": pid,
        "ok": pid > 0 or bool(output.strip()),
        "udid": device["udid"],
        "bundle_id": bundle,
    }


@atom(
    "ios_sim_terminate",
    description="结束模拟器上的 App（simctl terminate；未运行视为 ok）",
    category="device",
    tags=("device", "ios", "simctl", "mutate", "tool:simctl"),
    inputs={"udid": "string", "bundle_id": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "udid": "string", "bundle_id": "string"},
    required=("udid", "bundle_id"),
    effects=("mutate",),
    capabilities=("device", "process"),
    domain="ios_sim",
)
def ios_sim_terminate(
    ctx: RunContext,
    udid: str,
    bundle_id: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    bundle = str(bundle_id or "").strip()
    if not bundle:
        raise AtomFailure("ios_sim_terminate 需要 bundle_id", kind="invalid_input", failure_class="invalid_input")
    device = _resolve_device(udid)
    code, out, err = _run_simctl(
        ["terminate", device["udid"], bundle], timeout=timeout or DEFAULT_TIMEOUT
    )
    if code != 0 and _is_missing_device(code, out, err):
        raise _missing_device_failure(code, out, err, action="结束应用")
    if code != 0 and re.search(r"found nothing to terminate|not running", f"{out}\n{err}", re.IGNORECASE):
        return {"ok": True, "udid": device["udid"], "bundle_id": bundle}
    _assert_ok(code, out, err, action="结束应用")
    return {"ok": True, "udid": device["udid"], "bundle_id": bundle}


@atom(
    "ios_sim_openurl",
    description="在模拟器里打开深链/网页（simctl openurl）",
    category="device",
    tags=("device", "ios", "simctl", "mutate", "tool:simctl"),
    inputs={"udid": "string", "url": "string", "timeout?": "number"},
    outputs={"ok": "boolean", "udid": "string", "url": "string"},
    required=("udid", "url"),
    effects=("mutate",),
    capabilities=("device",),
    domain="ios_sim",
)
def ios_sim_openurl(
    ctx: RunContext,
    udid: str,
    url: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    target_url = str(url or "").strip()
    if not target_url:
        raise AtomFailure("ios_sim_openurl 需要 url", kind="invalid_input", failure_class="invalid_input")
    device = _resolve_device(udid)
    code, out, err = _run_simctl(
        ["openurl", device["udid"], target_url], timeout=timeout or DEFAULT_TIMEOUT
    )
    _assert_ok(code, out, err, action="打开 URL")
    return {"ok": True, "udid": device["udid"], "url": target_url}


@atom(
    "ios_sim_screenshot",
    description="模拟器截图（simctl io screenshot）→ .panguverse/artifacts/ios/（0600）",
    category="device",
    tags=("device", "ios", "simctl", "screenshot", "write", "tool:simctl"),
    inputs={"udid": "string", "name?": "string", "timeout?": "number"},
    outputs={"path": "string", "bytes": "integer", "udid": "string"},
    required=("udid",),
    effects=("read", "write"),
    capabilities=("device",),
    domain="ios_sim",
)
def ios_sim_screenshot(
    ctx: RunContext,
    udid: str,
    name: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    device = _resolve_device(udid)
    directory, root = _artifact_dir(ctx, "ios")
    filename = Path(str(name)).name if name else f"ios-{int(time.time() * 1000)}-{os.getpid()}.png"
    if not filename.lower().endswith(".png"):
        filename += ".png"
    path = directory / filename
    code, out, err = _run_simctl(
        ["io", device["udid"], "screenshot", str(path)],
        timeout=timeout or DEFAULT_TIMEOUT,
    )
    if code != 0 or not path.exists() or path.stat().st_size == 0:
        with contextlib.suppress(OSError):
            path.unlink()
        if _is_missing_device(code, out, err):
            raise _missing_device_failure(code, out, err, action="模拟器截图")
        raise _command_failed(code, out, err, action="模拟器截图")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return {
        "path": _relative(path, root),
        "bytes": path.stat().st_size,
        "udid": device["udid"],
    }


# --------------------------------------------------------------------------- #
# 领域语义
# --------------------------------------------------------------------------- #
class IosSimSemantics(GenericSemantics):
    """iOS 模拟器语义：工具/模拟器缺失显式失败，交互缺口不伪装成原子。"""

    name = "ios_sim"

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
        if kind in {"missing_tool", "missing_device"}:
            return [
                Recovery(
                    action="none",
                    reason="模拟器环境不具备执行条件，重试不会改变结果",
                    advice=self.advice(kind=kind, failure_class=failure_class),
                )
            ]
        return super().recoveries(
            atom, kind=kind, failure_class=failure_class, case=case, step=step, inputs=inputs
        )

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        texts = {
            "missing_tool": "未找到 xcrun/simctl：安装 Xcode 或 Command Line Tools",
            "missing_device": (
                "没有已启动的模拟器：先 ios_sim_boot 或 xcrun simctl boot <udid>；"
                "没有设备时在 Xcode 的 Devices 里创建并下载 iOS Simulator"
            ),
            "device_command_failed": "simctl 命令失败：检查 UDID、bundle id 与 .app 路径",
            "timeout": "simctl 命令超时：确认模拟器有响应",
            "parse": "simctl 输出无法解析：确认版本支持 -j JSON 输出",
            "file_not_found": "待安装 .app 不存在：检查 app_path",
        }
        return texts.get(kind) or super().advice(kind=kind, failure_class=failure_class)

    def planning_hints(self) -> tuple[str, ...]:
        return (
            "先跑 ios_sim_list 探测；用 booted/booted_count 判断有无已启动设备，不要编造成功",
            "install/launch/terminate/openurl/boot/shutdown 都是 mutate=true",
            "iOS 元素级点击/输入/滑动不在本包能力内 → 必须返回 capability_gap",
            "截图 path 相对 workspace 根（.panguverse/artifacts/ios/…）",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": "ios_sim",
            "target": "iOS simulator",
            "atoms": [
                "ios_sim_list",
                "ios_sim_boot",
                "ios_sim_shutdown",
                "ios_sim_install",
                "ios_sim_launch",
                "ios_sim_terminate",
                "ios_sim_openurl",
                "ios_sim_screenshot",
            ],
            "read_only": False,
            "needs_tool": "simctl",
            "capability_gaps": list(IOS_CAPABILITY_GAPS),
        }


def build_ios_list() -> dict[str, Any]:
    """「列出 iOS 模拟器」的参数化计划。"""
    return {
        "id": "ios_simulator_list",
        "name": "列出 iOS 模拟器",
        "tags": ["ios", "device", "simulator"],
        "description": "探测本机可用的 iOS 模拟器（无模拟器时返回空数组）",
        "steps": [
            {
                "id": "devices",
                "use": "ios_sim_list",
                "name": "列出 iOS 模拟器",
            }
        ],
    }


def ios_intent_templates() -> list[Any]:
    from ..core.contract import RuleTemplate

    return [
        RuleTemplate(
            name="ios_simulator_list",
            description="列出本机 iOS 模拟器（无模拟器返回空数组）",
            keywords=("ios 模拟器", "ios模拟器", "simctl", "模拟器列表", "xcode 模拟器"),
            builder=build_ios_list,
            tags=("ios", "device"),
            weight=1,
            domain="ios_sim",
            pack="ios_sim",
        )
    ]


def _mark_tool(atom_obj: Atom, tool: str) -> Atom:
    atom_obj.needs_tool = tool  # type: ignore[attr-defined]
    atom_obj.tags = tuple({*atom_obj.tags, f"tool:{tool}"})
    return atom_obj


def build_pack() -> Any:
    from ..core.pack import AtomPack

    atoms: list[Atom] = [
        ios_sim_list,
        ios_sim_boot,
        ios_sim_shutdown,
        ios_sim_install,
        ios_sim_launch,
        ios_sim_terminate,
        ios_sim_openurl,
        ios_sim_screenshot,
    ]
    for item in atoms:
        item.domain = "ios_sim"
        _mark_tool(item, "simctl")
    pack = AtomPack(
        name="ios_sim",
        domain="ios_sim",
        version="0.1.0",
        description=(
            "iOS 模拟器领域包：simctl 列表/启动/安装/启动 App/截图；"
            "明确不提供 iOS 元素级点击与输入（需要 XCUITest/WebDriverAgent）"
        ),
        semantics=IosSimSemantics(),
        atoms=atoms,
        requires=("device", "process"),
        tags=("builtin", "ios", "device", "simctl"),
        license="Apache-2.0",
        intent_templates=ios_intent_templates(),
    )
    # 能力缺口：规划器/工作台可读取该属性并返回 capability_gap
    pack.capability_gaps = list(IOS_CAPABILITY_GAPS)  # type: ignore[attr-defined]
    return pack


__all__ = [
    "IOS_CAPABILITY_GAPS",
    "MAX_OUTPUT_CHARS",
    "IosSimSemantics",
    "build_ios_list",
    "build_pack",
    "clip_output",
    "ios_intent_templates",
    "ios_sim_boot",
    "ios_sim_install",
    "ios_sim_launch",
    "ios_sim_list",
    "ios_sim_openurl",
    "ios_sim_screenshot",
    "ios_sim_shutdown",
    "ios_sim_terminate",
    "list_simulators",
    "parse_simctl_devices",
    "resolve_simctl",
]
