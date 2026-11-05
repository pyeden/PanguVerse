"""``web_browser`` 领域包：用本机 Chrome 无头模式打开真实渲染后的网页。

与 :mod:`~panguverse.packs.web_research` 的分工：

- ``web_research`` 走 ``urllib``，只拿到服务端返回的 HTML；
- ``web_browser`` 走 Chrome ``--headless=new --dump-dom``，拿到**脚本执行后**的 DOM，
  并能截图、轮询动态文本。

契约（``tasks/device-web-atoms-spec.md`` v7.0 §2）::

=================  ==========================================================
原子               语义
=================  ==========================================================
web_open           打开 URL，返回 status/html/text/title/final_url
web_get_text       从 HTML 提取正文（复用 web_research.extract_text）
web_screenshot     无头截图 → workspace/.panguverse/artifacts/web/
web_extract_links  提取链接（href/text/internal/same_origin_only）
web_wait_for_text  轮询渲染后 DOM 直到文本出现；超时不可重试
=================  ==========================================================

工具路径：``PANGUVERSE_CHROME`` 可覆盖；一旦设置就不会回退到默认路径，
指向不存在的文件时明确失败为 ``missing_tool``（不静默降级、不重试）。

所有子进程都 ``start_new_session=True``，超时后按进程组 ``SIGKILL``，
避免留下 Chrome 僵尸进程；命令诊断输出按 :data:`MAX_OUTPUT_CHARS` 截断。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.atom import Atom, atom
from ..core.context import RunContext
from ..core.contract import FailureClass, Recovery
from ..core.errors import AtomFailure, PanguError
from ..http.client import is_loopback_url
from .core import GenericSemantics
from .web_research import extract_structure, extract_text

#: 命令输出/诊断文本的默认截断长度（契约 §1）
MAX_OUTPUT_CHARS = 4000
#: 渲染 DOM 的采集上限：正文解析需要完整 DOM；诊断输出（stderr/回显）仍按 4000 截断
DOM_MAX_CHARS = 2_000_000
#: Chrome 渲染的虚拟时间预算（毫秒），给前端脚本留出执行时间
DEFAULT_VIRTUAL_TIME_MS = 3000
#: 单次 Chrome 调用的兜底超时
DEFAULT_TIMEOUT = 30.0
DEFAULT_INTERVAL = 0.5

_TOOL_ENV = "PANGUVERSE_CHROME"

#: macOS 常见安装位置 + PATH 里的常见命令名
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)

#: 回环目标必须直连（避免系统代理劫持 127.0.0.1）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_CHROME_FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
)


# --------------------------------------------------------------------------- #
# 工具发现 / 进程执行
# --------------------------------------------------------------------------- #
def resolve_chrome() -> str:
    """解析 Chrome 可执行文件；``PANGUVERSE_CHROME`` 优先且**不回退**。"""
    override = os.environ.get(_TOOL_ENV)
    if override is not None:
        candidate = str(override).strip()
        if candidate and Path(candidate).exists() and os.access(candidate, os.X_OK):
            return candidate
        raise AtomFailure(
            f"未找到 Chrome：{_TOOL_ENV}={candidate or '<空>'} 不存在或不可执行；"
            "请安装 Chrome/Chromium，或把该环境变量指向可执行文件",
            kind="missing_tool",
            failure_class="missing_resource",
            detail={"tool": "chrome", "env": _TOOL_ENV, "path": candidate},
        )
    for candidate in _CHROME_CANDIDATES:
        if os.path.sep in candidate:
            if Path(candidate).exists() and os.access(candidate, os.X_OK):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    raise AtomFailure(
        "未找到 Chrome 可执行文件（已检查 /Applications 与 PATH）；"
        f"请安装 Chrome/Chromium，或设置 {_TOOL_ENV} 指向它",
        kind="missing_tool",
        failure_class="missing_resource",
        detail={"tool": "chrome", "env": _TOOL_ENV},
    )


def clip_output(text: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    """把命令输出截到 ``limit`` 字符，保留可读的截断标记。"""
    value = text if isinstance(text, str) else str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    marker = "…（已截断）"
    keep = max(0, limit - len(marker))
    return value[:keep] + marker


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def run_tool(
    command: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    stdout_limit: int | None = None,
) -> tuple[int, str, str]:
    """执行命令：独立进程组 + 超时 kill + 输出截断。

    返回 ``(returncode, stdout, stderr)``（均为已截断文本）。
    ``stdout_limit`` 只放宽 stdout（例如 ``--dump-dom`` 需要完整 DOM 再解析），
    stderr 始终保持 :data:`MAX_OUTPUT_CHARS`。
    """
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
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
        out, err = proc.communicate(
            input=input_text.encode("utf-8") if input_text is not None else None,
            timeout=max(1.0, float(timeout)),
        )
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise AtomFailure(
            f"命令超时（{timeout:g}s）：{' '.join(command[:3])} …",
            kind="timeout",
            failure_class="transient",
            detail={"command": command[:6], "timeout": timeout},
        ) from exc
    finally:
        if proc.poll() is None:
            _kill_group(proc)

    out_limit = MAX_OUTPUT_CHARS if stdout_limit is None else max(0, int(stdout_limit))
    return (
        int(proc.returncode or 0),
        clip_output(out.decode("utf-8", errors="replace"), out_limit),
        clip_output(err.decode("utf-8", errors="replace")),
    )


# --------------------------------------------------------------------------- #
# 网页/产物工具
# --------------------------------------------------------------------------- #
def _status_probe(url: str, timeout: float) -> tuple[int, str]:
    """用标准库拿 HTTP 状态与最终 URL（只读 1 字节，不下载正文）。"""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "PanguVerse/0.1"})
    opener = _DIRECT_OPENER if is_loopback_url(url) else None
    try:
        response = (
            opener.open(request, timeout=timeout)
            if opener
            else urllib.request.urlopen(request, timeout=timeout)
        )
        try:
            status = int(getattr(response, "status", 200) or 200)
            final_url = str(response.geturl() or url)
            with contextlib.suppress(Exception):
                response.read(1)
        finally:
            response.close()
        return status, final_url
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(Exception):
            exc.close()
        return int(exc.code), str(getattr(exc, "url", url) or url)
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        kind = "timeout" if isinstance(exc, TimeoutError) else "network"
        raise AtomFailure(
            f"网页不可达: {url}（{reason}）",
            kind=kind,
            failure_class="transient",
            detail={"url": url},
        ) from exc


def chrome_dump_dom(url: str, *, timeout: float, virtual_time_ms: int) -> str:
    """``--dump-dom`` 拿到渲染后的 DOM。"""
    chrome = resolve_chrome()
    with tempfile.TemporaryDirectory(prefix="pangu-chrome-") as profile:
        command = [
            chrome,
            *_CHROME_FLAGS,
            f"--user-data-dir={profile}",
            f"--virtual-time-budget={max(200, int(virtual_time_ms))}",
            "--dump-dom",
            url,
        ]
        # 先拿完整 DOM 再解析正文，避免只有 <head> 前 4000 字符可用
        code, out, err = run_tool(command, timeout=timeout, stdout_limit=DOM_MAX_CHARS)
    if code != 0 and not out.strip():
        raise AtomFailure(
            f"Chrome 渲染失败（exit={code}）：{clip_output(err) or '无输出'}",
            kind="parse",
            failure_class="invalid_input",
            detail={"url": url, "exit_code": code},
        )
    if not out.strip():
        raise AtomFailure(
            "Chrome 未返回 DOM（页面为空或被浏览器拦截）",
            kind="parse",
            failure_class="invalid_input",
            detail={"url": url, "stderr": clip_output(err)},
        )
    return out


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
    """创建 ``<workspace>/.panguverse/artifacts/<kind>``（0700），返回 (目录, 相对根)。"""
    root = _artifact_root(ctx)
    target = root / "artifacts" / kind
    target.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
        (root / "artifacts").mkdir(parents=True, exist_ok=True)
        os.chmod(root / "artifacts", 0o700)
        os.chmod(target, 0o700)
    return target, root


def _harden(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _validate_http_url(url: str, *, allow_file: bool = False) -> str:
    target = str(url or "").strip()
    if not target:
        raise AtomFailure("需要 url", kind="invalid_url", failure_class="invalid_input")
    if allow_file and target.startswith("file://"):
        return target
    if not re.match(r"^https?://", target, re.IGNORECASE):
        raise AtomFailure(
            f"只支持 http/https 绝对地址: {target}",
            kind="invalid_url",
            failure_class="invalid_input",
        )
    return target


def _page_payload(url: str, html: str, *, status: int, final_url: str) -> dict[str, Any]:
    structure = extract_structure(html)
    return {
        "status": status,
        "html": html,
        "text": extract_text(html),
        "title": structure.get("title", ""),
        "final_url": final_url,
    }


def _open_page(url: str, *, timeout: float, virtual_time_ms: int) -> dict[str, Any]:
    target = _validate_http_url(url)
    status, final_url = _status_probe(target, min(float(timeout), 15.0))
    html = chrome_dump_dom(final_url, timeout=timeout, virtual_time_ms=virtual_time_ms)
    return _page_payload(final_url, html, status=status, final_url=final_url)


# --------------------------------------------------------------------------- #
# 原子
# --------------------------------------------------------------------------- #
@atom(
    "web_open",
    description="用 Chrome 无头模式打开网页，返回渲染后的 DOM/正文/标题/HTTP 状态",
    category="web",
    tags=("web", "browser", "read", "tool:chrome"),
    inputs={
        "url": "string",
        "timeout": "number",
        "wait_for?": "string",
        "interval?": "number",
        "virtual_time_ms?": "integer",
        "max_chars?": "integer",
    },
    outputs={
        "status": "integer",
        "html": "string",
        "text": "string",
        "title": "string",
        "final_url": "string",
        "attempts": "integer",
    },
    required=("url",),
    effects=("read",),
    capabilities=("browser", "network"),
    domain="web_browser",
)
def web_open(
    ctx: RunContext,
    url: str,
    timeout: float | None = None,
    wait_for: str | None = None,
    interval: float | None = None,
    virtual_time_ms: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    limit = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    budget = int(virtual_time_ms or DEFAULT_VIRTUAL_TIME_MS)
    started = time.monotonic()
    attempts = 1
    payload = _open_page(url, timeout=limit, virtual_time_ms=budget)
    wanted = str(wait_for or "")
    if wanted:
        step = max(0.1, float(interval if interval is not None else DEFAULT_INTERVAL))
        while wanted not in payload["text"]:
            remaining = limit - (time.monotonic() - started)
            if remaining <= 0.2:
                raise AtomFailure(
                    f"等待文本超时：{wanted!r} 在 {attempts} 次渲染内未出现",
                    kind="timeout",
                    failure_class="expectation",
                    detail={"url": url, "text": wanted, "attempts": attempts, "retryable": False},
                )
            time.sleep(min(step, remaining))
            payload = _open_page(
                url,
                timeout=max(1.0, remaining),
                virtual_time_ms=budget,
            )
            attempts += 1
    cap = int(max_chars) if max_chars is not None else MAX_OUTPUT_CHARS
    payload["html"] = clip_output(payload["html"], cap)
    payload["attempts"] = attempts
    ctx.vars["last_page"] = payload
    ctx.emit("browser", url=payload["final_url"], status=payload["status"], title=payload["title"])
    return payload


@atom(
    "web_get_text",
    description="从 HTML 提取可读正文（复用 web_research 的提取规则）",
    category="web",
    tags=("web", "html", "read"),
    inputs={"html": "string"},
    outputs={"text": "string", "chars": "integer"},
    required=("html",),
    effects=("pure",),
    domain="web_browser",
)
def web_get_text(ctx: RunContext, html: str) -> dict[str, Any]:
    text = extract_text(html)
    return {"text": text, "chars": len(text)}


@atom(
    "web_screenshot",
    description="用 Chrome 无头模式截网页图，产物落 .panguverse/artifacts/web/（0600）",
    category="web",
    tags=("web", "browser", "screenshot", "write", "tool:chrome"),
    inputs={
        "url?": "string",
        "html?": "string",
        "width?": "integer",
        "height?": "integer",
        "timeout?": "number",
        "name?": "string",
    },
    outputs={"path": "string", "bytes": "integer", "width": "integer", "height": "integer"},
    required=(),
    effects=("read", "write"),
    capabilities=("browser", "network"),
    domain="web_browser",
    produces=("screenshot",),
)
def web_screenshot(
    ctx: RunContext,
    url: str | None = None,
    html: str | None = None,
    width: int | None = None,
    height: int | None = None,
    timeout: float | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    chrome = resolve_chrome()
    width_value = max(200, int(width or 1280))
    height_value = max(200, int(height or 800))
    limit = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    directory, root = _artifact_dir(ctx, "web")
    filename = Path(str(name)).name if name else f"web-{int(time.time() * 1000)}-{os.getpid()}.png"
    if not filename.lower().endswith(".png"):
        filename += ".png"
    target = directory / filename
    with tempfile.TemporaryDirectory(prefix="pangu-shot-") as tmp:
        source = ""
        if url:
            source = _validate_http_url(url)
        elif html is not None and str(html).strip():
            html_path = Path(tmp) / "page.html"
            html_path.write_text(str(html), encoding="utf-8")
            source = html_path.as_uri()
        else:
            raise AtomFailure(
                "web_screenshot 需要 url 或 html",
                kind="invalid_input",
                failure_class="invalid_input",
            )
        command = [
            chrome,
            *_CHROME_FLAGS,
            f"--user-data-dir={Path(tmp) / 'profile'}",
            f"--window-size={width_value},{height_value}",
            f"--virtual-time-budget={DEFAULT_VIRTUAL_TIME_MS}",
            "--run-all-compositor-stages-before-draw",
            f"--screenshot={target}",
            source,
        ]
        code, _out, err = run_tool(command, timeout=limit)
    if not target.exists() or target.stat().st_size == 0:
        with contextlib.suppress(OSError):
            target.unlink()
        raise AtomFailure(
            f"Chrome 截图失败（exit={code}）：{clip_output(err) or '未生成文件'}",
            kind="parse",
            failure_class="invalid_input",
            detail={"url": source, "exit_code": code},
        )
    _harden(target)
    size = target.stat().st_size
    return {
        "path": _relative(target, root),
        "bytes": size,
        "width": width_value,
        "height": height_value,
    }


@atom(
    "web_extract_links",
    description="提取页面链接（href/text/internal），可选只保留同源链接",
    category="web",
    tags=("web", "html", "read"),
    inputs={"html": "string", "same_origin_only?": "boolean", "base_url?": "string"},
    outputs={"links": "array", "count": "integer"},
    required=("html",),
    effects=("pure",),
    domain="web_browser",
    produces=("links",),
)
def web_extract_links(
    ctx: RunContext,
    html: str,
    same_origin_only: bool = False,
    base_url: str | None = None,
) -> dict[str, Any]:
    structure = extract_structure(html)
    links = list(structure.get("links") or [])
    origin = ""
    if base_url:
        parts = urlsplit(str(base_url))
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    out: list[dict[str, Any]] = []
    for item in links:
        href = str(item.get("href") or "")
        if same_origin_only:
            if origin:
                absolute = href if re.match(r"^[a-z]+://", href, re.IGNORECASE) else origin + (
                    href if href.startswith("/") else "/" + href
                )
                if urlsplit(absolute).netloc != urlsplit(origin).netloc:
                    continue
            elif not item.get("internal"):
                continue
        out.append(
            {
                "href": href,
                "text": str(item.get("text") or ""),
                "internal": bool(item.get("internal")),
            }
        )
    return {"links": out, "count": len(out)}


@atom(
    "web_wait_for_text",
    description="轮询渲染后的页面直到文本出现；超时属于失败且不自动重试",
    category="web",
    tags=("web", "browser", "wait", "read", "tool:chrome"),
    inputs={
        "url": "string",
        "text": "string",
        "timeout?": "number",
        "interval?": "number",
        "virtual_time_ms?": "integer",
    },
    outputs={"found": "boolean", "attempts": "integer", "elapsed_ms": "integer", "url": "string"},
    required=("url", "text"),
    effects=("read",),
    capabilities=("browser", "network"),
    domain="web_browser",
)
def web_wait_for_text(
    ctx: RunContext,
    url: str,
    text: str,
    timeout: float | None = None,
    interval: float | None = None,
    virtual_time_ms: int | None = None,
) -> dict[str, Any]:
    target = _validate_http_url(url)
    wanted = str(text or "")
    if not wanted:
        raise AtomFailure(
            "web_wait_for_text 需要非空 text",
            kind="invalid_input",
            failure_class="invalid_input",
        )
    limit = float(timeout) if timeout is not None else 15.0
    step = max(0.1, float(interval if interval is not None else DEFAULT_INTERVAL))
    budget = int(virtual_time_ms or DEFAULT_VIRTUAL_TIME_MS)
    started = time.monotonic()
    attempts = 0
    last_error: AtomFailure | None = None
    while True:
        remaining = limit - (time.monotonic() - started)
        if attempts > 0 and remaining <= 0.2:
            break
        attempts += 1
        try:
            html = chrome_dump_dom(
                target,
                timeout=max(1.0, remaining if remaining > 0 else limit),
                virtual_time_ms=budget,
            )
        except AtomFailure as exc:
            if exc.kind == "missing_tool":
                raise
            # 单次渲染超时也归入「等待超时」：必须带 attempts 明确失败，不能重试
            last_error = exc
            if exc.kind == "timeout":
                break
        else:
            if wanted in extract_text(html):
                elapsed = int((time.monotonic() - started) * 1000)
                return {"found": True, "attempts": attempts, "elapsed_ms": elapsed, "url": target}
        remaining = limit - (time.monotonic() - started)
        if remaining <= 0.2:
            break
        time.sleep(min(step, remaining))
    elapsed = int((time.monotonic() - started) * 1000)
    detail: dict[str, Any] = {
        "url": target,
        "text": wanted,
        "attempts": attempts,
        "elapsed_ms": elapsed,
    }
    if last_error is not None:
        detail["last_error"] = str(last_error)
    raise AtomFailure(
        f"等待文本超时：{wanted!r} 在 {attempts} 次渲染内未出现（{elapsed}ms）",
        kind="timeout",
        failure_class="expectation",
        detail={**detail, "retryable": False},
    )


# --------------------------------------------------------------------------- #
# 领域语义
# --------------------------------------------------------------------------- #
class WebBrowserSemantics(GenericSemantics):
    """浏览器领域语义：只有网络抖动可重试；工具缺失/等待超时一律不重试。"""

    name = "web_browser"

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
        if kind in {"missing_tool", "timeout", "parse", "invalid_input", "invalid_url"}:
            return False
        return kind == "network" and failure_class is FailureClass.TRANSIENT

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
        if kind == "missing_tool":
            return [
                Recovery(
                    action="none",
                    reason="本机没有可用的 Chrome：无头渲染无法进行",
                    advice="安装 Chrome/Chromium，或设置 PANGUVERSE_CHROME 指向可执行文件",
                )
            ]
        if kind == "timeout" and getattr(atom, "name", "") == "web_wait_for_text":
            return [
                Recovery(
                    action="none",
                    reason="等待文本超时：重试只会重复同样的等待，明确失败",
                    advice="确认文本确实存在；必要时放宽 timeout，或先检查页面渲染结果",
                )
            ]
        return super().recoveries(
            atom, kind=kind, failure_class=failure_class, case=case, step=step, inputs=inputs
        )

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        texts = {
            "missing_tool": "未找到 Chrome：安装 Chrome/Chromium 或设置 PANGUVERSE_CHROME",
            "network": "网页不可达：检查 URL、DNS、代理与目标站点是否在线",
            "timeout": "浏览器等待超时：放宽 timeout 或确认页面能正常渲染",
            "parse": "浏览器未返回可解析的 DOM：确认页面不是下载/拦截/空白页",
            "invalid_url": "URL 非法：只接受 http/https 绝对地址",
            "invalid_input": "输入不合法：检查 url/html 与尺寸参数",
        }
        return texts.get(kind) or super().advice(kind=kind, failure_class=failure_class)

    def target(self, atom: Any, inputs: Mapping[str, Any] | None) -> str:
        if isinstance(inputs, Mapping):
            return str(inputs.get("url") or "")
        return ""

    def planning_hints(self) -> tuple[str, ...]:
        return (
            "需要 JS 渲染后的页面用 web_open（Chrome 无头 --dump-dom）；纯服务端 HTML 可用 web_research.http_get_page",
            "web_open.html 默认截断到 4000 字符；需要完整 DOM 时把 max_chars 调大",
            "链接用 web_extract_links(${open.html})，正文用 web_get_text(${open.html})",
            "动态内容用 web_wait_for_text 轮询；它的 timeout 明确失败且不可重试",
            "web_screenshot 的 path 相对 workspace 根（.panguverse/artifacts/web/…）",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": "web_browser",
            "target": "URL",
            "atoms": [
                "web_open",
                "web_get_text",
                "web_screenshot",
                "web_extract_links",
                "web_wait_for_text",
            ],
            "read_only": False,
            "needs_tool": "chrome",
        }


def build_web_content() -> dict[str, Any]:
    """「打开网页 → 取正文」的参数化计划。"""
    return {
        "id": "web_browser_content",
        "name": "浏览器打开网页并提取正文",
        "tags": ["web", "browser", "content"],
        "description": "用 Chrome 无头打开页面，拿到渲染后的 DOM 与正文",
        "steps": [
            {
                "id": "open",
                "use": "web_open",
                "name": "无头浏览器打开页面",
                # 默认 4000 字符只够 <head>；正文提取需要保留渲染后的完整 DOM
                "with": {"url": "${env.PAGE_URL}", "timeout": 30, "max_chars": 400000},
            },
            {
                "id": "text",
                "use": "web_get_text",
                "name": "获取网页正文",
                "with": {"html": "${open.html}"},
            },
        ],
    }


def build_web_links() -> dict[str, Any]:
    """「打开网页 → 提取链接」的参数化计划。"""
    return {
        "id": "web_browser_links",
        "name": "浏览器打开网页并提取链接",
        "tags": ["web", "browser", "links"],
        "description": "用 Chrome 无头打开页面，提取页面链接",
        "steps": [
            {
                "id": "open",
                "use": "web_open",
                "name": "无头浏览器打开页面",
                "with": {"url": "${env.PAGE_URL}", "timeout": 30},
            },
            {
                "id": "links",
                "use": "web_extract_links",
                "name": "提取链接",
                "with": {"html": "${open.html}"},
            },
        ],
    }


def build_web_screenshot_plan() -> dict[str, Any]:
    """「网页截图」的参数化计划。"""
    return {
        "id": "web_browser_screenshot",
        "name": "网页截图",
        "tags": ["web", "browser", "screenshot"],
        "description": "用 Chrome 无头模式给网页截图",
        "steps": [
            {
                "id": "shot",
                "use": "web_screenshot",
                "name": "网页截图",
                "with": {"url": "${env.PAGE_URL}", "name": "page.png"},
            }
        ],
    }


def build_web_wait_text() -> dict[str, Any]:
    """「等待页面文本出现」的参数化计划。"""
    return {
        "id": "web_browser_wait_text",
        "name": "等待网页文本出现",
        "tags": ["web", "browser", "wait"],
        "description": "轮询渲染后的页面，直到指定文本出现",
        "steps": [
            {
                "id": "wait",
                "use": "web_wait_for_text",
                "name": "等待文本出现",
                "with": {
                    "url": "${env.PAGE_URL}",
                    "text": "${env.WAIT_TEXT}",
                    "timeout": 15,
                },
            }
        ],
    }


def web_browser_intent_templates() -> list[Any]:
    """``web_browser`` 包声明的意图模板。"""
    from ..core.contract import RuleTemplate

    return [
        RuleTemplate(
            name="web_browser_content",
            description="用无头浏览器打开网页并提取渲染后的正文",
            keywords=("无头浏览器", "浏览器打开", "渲染后", "前端渲染", "js 渲染", "取文本", "正文"),
            builder=build_web_content,
            tags=("web", "browser"),
            weight=1,
            domain="web_browser",
            pack="web_browser",
        ),
        RuleTemplate(
            name="web_browser_links",
            description="无头浏览器打开网页并提取链接",
            keywords=("取链接", "提取链接", "页面链接"),
            builder=build_web_links,
            tags=("web", "browser", "links"),
            weight=2,
            domain="web_browser",
            pack="web_browser",
        ),
        RuleTemplate(
            name="web_browser_screenshot",
            description="网页截图",
            keywords=("网页截图", "浏览器截图", "页面截图"),
            builder=build_web_screenshot_plan,
            tags=("web", "browser", "screenshot"),
            weight=2,
            domain="web_browser",
            pack="web_browser",
        ),
        RuleTemplate(
            name="web_browser_wait_text",
            description="等待网页文本出现",
            keywords=("等待文本", "等待出现", "动态加载"),
            builder=build_web_wait_text,
            tags=("web", "browser", "wait"),
            weight=2,
            domain="web_browser",
            pack="web_browser",
        ),
    ]


def _mark_tool(atom_obj: Atom, tool: str) -> Atom:
    atom_obj.needs_tool = tool  # type: ignore[attr-defined]
    atom_obj.tags = tuple({*atom_obj.tags, f"tool:{tool}"})
    return atom_obj


def build_pack() -> Any:
    """构建 ``web_browser`` 包。"""
    from ..core.pack import AtomPack

    atoms: list[Atom] = [
        web_open,
        web_get_text,
        web_screenshot,
        web_extract_links,
        web_wait_for_text,
    ]
    for item in atoms:
        item.domain = "web_browser"
        _mark_tool(item, "chrome")
    return AtomPack(
        name="web_browser",
        domain="web_browser",
        version="0.1.0",
        description="浏览器领域包：Chrome 无头打开网页、提取渲染后 DOM/正文/链接、截图与动态文本等待",
        semantics=WebBrowserSemantics(),
        atoms=atoms,
        requires=("browser", "network"),
        tags=("builtin", "web", "browser", "chrome"),
        license="Apache-2.0",
        intent_templates=web_browser_intent_templates(),
    )


__all__ = [
    "MAX_OUTPUT_CHARS",
    "WebBrowserSemantics",
    "build_pack",
    "build_web_content",
    "build_web_links",
    "build_web_screenshot_plan",
    "build_web_wait_text",
    "chrome_dump_dom",
    "clip_output",
    "resolve_chrome",
    "run_tool",
    "web_browser_intent_templates",
    "web_extract_links",
    "web_get_text",
    "web_open",
    "web_screenshot",
    "web_wait_for_text",
]
