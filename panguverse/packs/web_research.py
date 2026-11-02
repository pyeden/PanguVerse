"""``web_research`` 领域包：网页抓取 / 正文提取 / 结构分析 / LLM 总结。

这是与 API 测试、本地文件系统并列的第三个领域。它证明「新增行业 = 新增包」：

- 有自己的失败词汇：``network`` / ``parse`` / ``missing_llm``；
- 有自己的只读副作用集合：全部为 ``read`` / ``pure``，不会 mutate 任何外部状态；
- 有自己的经验标识：目标 URL（``target``）；
- 有自己的规划提示：先取页面，再抽正文与结构，最后用 LLM 产出报告。

契约（``tasks/workbench-accuracy-spec.md`` v6.0 §1）：

===============  ==========================================================
原子              语义
===============  ==========================================================
http_get_page    GET 页面；按响应 charset 解码（缺省 utf-8 再兜底）；
                 4xx/5xx 不抛异常；连接失败 ``failure_kind=network``
html_extract_text  去 script/style/标签、压缩空白、保留段落换行
html_extract_structure  标题/标题层级/链接/顶层区块计数
llm_summarize    必须走 ``LLMClient``；无密钥 → ``missing_llm`` 硬失败，
                 **绝不产出占位**
===============  ==========================================================
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from ..core.atom import Atom, atom
from ..core.context import RunContext
from ..core.contract import FailureClass, Recovery
from ..core.errors import AtomFailure, PanguError
from ..http.client import DEFAULT_TIMEOUT, is_loopback_url
from .core import GenericSemantics, enrich

#: 回环目标必须直连（macOS 系统代理也会劫持 127.0.0.1）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_MAX_CHARS = 6000

_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas"}
_SECTION_TAGS = {"header", "nav", "main", "section", "article", "aside", "footer"}
_COUNT_TAGS = (
    "a",
    "div",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "img",
    "li",
    "main",
    "nav",
    "p",
    "section",
    "table",
    "ul",
)

_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([A-Za-z0-9._-]+)""", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9._-]+)""", re.IGNORECASE
)
_FALLBACK_CHARSETS = ("utf-8", "gb18030", "big5", "shift_jis", "latin-1")


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def _declared_charset(headers: Mapping[str, Any] | None, raw: bytes) -> str:
    content_type = ""
    if headers:
        content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
    match = _CHARSET_RE.search(content_type)
    if match:
        return match.group(1).strip().lower()
    match = _META_CHARSET_RE.search(raw[:8192])
    if match:
        return match.group(1).decode("ascii", errors="ignore").strip().lower()
    return ""


def decode_html(raw: bytes, headers: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """按声明 charset 解码；缺省 utf-8，再逐级兜底。返回 ``(text, charset)``。"""
    declared = _declared_charset(headers, raw)
    candidates: list[str] = []
    for name in (declared, *_FALLBACK_CHARSETS):
        if name and name not in candidates:
            candidates.append(name)
    for name in candidates:
        try:
            return raw.decode(name), name
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


# --------------------------------------------------------------------------- #
# HTML 解析工具
# --------------------------------------------------------------------------- #
def _squash(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._pending_newline = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS or tag == "br":
            self._pending_newline = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._pending_newline = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _squash(data)
        if not text:
            return
        if self._pending_newline and self.parts:
            self.parts.append("\n")
        self.parts.append(text)
        self._pending_newline = False

    def text(self) -> str:
        joined = "".join(self.parts)
        lines = [_squash(line) for line in joined.split("\n")]
        return "\n".join(line for line in lines if line)


def extract_text(html: str) -> str:
    if not isinstance(html, str):
        html = str(html or "")
    if not html.strip():
        return ""
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise AtomFailure(
            f"HTML 文本解析失败: {exc}", kind="parse", failure_class="invalid_input"
        ) from exc
    return parser.text()


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self.headings: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.sections: list[dict[str, Any]] = []
        self._body_depth = 0
        self._in_body = False
        self._section: dict[str, Any] | None = None
        self._heading: dict[str, Any] | None = None
        self._anchor: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {str(key).lower(): (value or "") for key, value in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag == "body":
            self._in_body = True
            self._body_depth = 0
        elif self._in_body:
            self._body_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = {"level": int(tag[1]), "text": ""}
        if tag == "a":
            href = attrs_map.get("href", "").strip()
            if href:
                self._anchor = {"href": href, "text": "", "internal": _is_internal(href)}
        if tag in _SECTION_TAGS and self._section is None and self._in_body:
            self._section = {
                "tag": tag,
                "id": attrs_map.get("id") or attrs_map.get("class") or "",
                "text_chars": 0,
                "headings": 0,
                "links": 0,
            }

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if self._heading is not None and tag == f"h{self._heading['level']}":
            text = _squash(self._heading["text"])
            if text:
                self.headings.append({"level": self._heading["level"], "text": text})
                if self._section is not None:
                    self._section["headings"] += 1
            self._heading = None
        if self._anchor is not None and tag == "a":
            self._anchor["text"] = _squash(self._anchor["text"])
            self.links.append(dict(self._anchor))
            if self._section is not None:
                self._section["links"] += 1
            self._anchor = None
        if tag == "body":
            self._flush_section()
            self._in_body = False
        elif self._in_body:
            self._body_depth = max(0, self._body_depth - 1)
            if self._body_depth == 0 and tag in _SECTION_TAGS:
                self._flush_section()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _squash(data)
        if not text:
            return
        if self._in_title:
            self.title += (" " if self.title else "") + text
        if self._heading is not None:
            self._heading["text"] += (" " if self._heading["text"] else "") + text
        if self._anchor is not None:
            self._anchor["text"] += (" " if self._anchor["text"] else "") + text
        if self._section is not None:
            self._section["text_chars"] += len(text)
        elif self._in_body and self._body_depth == 0:
            self._section = {
                "tag": "#text",
                "id": "",
                "text_chars": len(text),
                "headings": 0,
                "links": 0,
            }

    def _flush_section(self) -> None:
        if self._section is None:
            return
        if self._section["text_chars"] or self._section["headings"] or self._section["links"]:
            self.sections.append(dict(self._section))
        self._section = None


def _is_internal(href: str) -> bool:
    if href.startswith(("#", "/", "?", "./", "../")):
        return True
    parsed = urlsplit(href)
    return not parsed.scheme and not parsed.netloc


def extract_structure(html: str) -> dict[str, Any]:
    if not isinstance(html, str):
        html = str(html or "")
    parser = _StructureParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise AtomFailure(
            f"HTML 结构解析失败: {exc}", kind="parse", failure_class="invalid_input"
        ) from exc
    counts = {tag: int(parser.counts.get(tag, 0)) for tag in _COUNT_TAGS}
    counts["headings"] = len(parser.headings)
    counts["links"] = len(parser.links)
    counts["sections"] = len(parser.sections)
    return {
        "title": _squash(parser.title),
        "headings": parser.headings,
        "links": parser.links[:500],
        "sections": parser.sections[:40],
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# 原子
# --------------------------------------------------------------------------- #
@atom(
    "http_get_page",
    description="GET 一个网页，按响应 charset 解码并返回 HTML/正文；4xx/5xx 不抛异常",
    category="web",
    tags=("web", "http", "read"),
    inputs={"url": "string", "headers": "object", "timeout": "number"},
    outputs={
        "status": "integer",
        "html": "string",
        "text": "string",
        "final_url": "string",
        "charset": "string",
    },
    required=("url",),
    effects=("read",),
    capabilities=("network",),
    domain="web_research",
)
def http_get_page(
    ctx: RunContext,
    url: str,
    headers: Mapping[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    target = str(url or "").strip()
    if not target:
        raise AtomFailure("http_get_page 需要 url", kind="invalid_url", failure_class="invalid_input")
    if not re.match(r"^https?://", target, re.IGNORECASE):
        raise AtomFailure(
            f"只支持 http/https 绝对地址: {target}",
            kind="invalid_url",
            failure_class="invalid_input",
        )
    request = urllib.request.Request(
        target,
        headers={str(k): str(v) for k, v in dict(headers or {}).items()},
        method="GET",
    )
    opener = _DIRECT_OPENER if is_loopback_url(target) else None
    wait = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    try:
        if opener is not None:
            response = opener.open(request, timeout=wait)
        else:
            response = urllib.request.urlopen(request, timeout=wait)
        try:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read()
            final_url = str(response.geturl() or target)
            response_headers = dict(response.headers.items())
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 是合法被测结果：读回 body，返回 status，不抛异常
        status = int(exc.code)
        raw = exc.read() if hasattr(exc, "read") else b""
        final_url = str(getattr(exc, "url", target) or target)
        response_headers = dict(exc.headers.items()) if exc.headers else {}
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        kind = "timeout" if isinstance(exc, TimeoutError) else "network"
        raise AtomFailure(
            f"网页不可达: {target}（{reason}）",
            kind=kind,
            failure_class="transient",
            detail={"url": target},
        ) from exc

    encoding = str(response_headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        with contextlib.suppress(OSError):
            raw = gzip.decompress(raw)
    html, charset = decode_html(raw, response_headers)
    text = extract_text(html)
    payload = {
        "status": status,
        "html": html,
        "text": text,
        "final_url": final_url,
        "charset": charset,
    }
    ctx.record_http(
        {
            "at": time.time(),
            "request": {"method": "GET", "url": target, "headers": dict(headers or {})},
            "response": {
                "status_code": status,
                "url": final_url,
                "charset": charset,
                "size_bytes": len(raw),
            },
        }
    )
    ctx.vars["last_response"] = payload
    ctx.emit("http", method="GET", url=final_url, status=status)
    return payload


@atom(
    "html_extract_text",
    description="从 HTML 提取可读文本：去 script/style、去标签、压缩空白、保留段落换行",
    category="web",
    tags=("web", "html", "read"),
    inputs={"html": "string"},
    outputs={"text": "string", "chars": "integer"},
    required=("html",),
    effects=("pure",),
    domain="web_research",
)
def html_extract_text(ctx: RunContext, html: str) -> dict[str, Any]:
    text = extract_text(html)
    return {"text": text, "chars": len(text)}


@atom(
    "html_extract_structure",
    description="提取页面结构：标题、标题层级、链接（内/外链）与顶层区块计数",
    category="web",
    tags=("web", "html", "read"),
    inputs={"html": "string"},
    outputs={
        "title": "string",
        "headings": "array",
        "links": "array",
        "sections": "array",
        "counts": "object",
    },
    required=("html",),
    effects=("pure",),
    domain="web_research",
)
def html_extract_structure(ctx: RunContext, html: str) -> dict[str, Any]:
    return extract_structure(html)


@atom(
    "llm_summarize",
    description="调用 LLM 把文本/页面结构总结成 Markdown；无密钥硬失败（missing_llm），绝不产出占位",
    category="web",
    tags=("web", "llm", "report"),
    inputs={"text": "string", "instruction": "string", "max_chars": "integer"},
    outputs={"summary_md": "string", "model": "string"},
    required=("text",),
    effects=("read",),
    capabilities=("network", "needs_llm"),
    domain="web_research",
)
def llm_summarize(
    ctx: RunContext,
    text: Any,
    instruction: str = "请根据给定内容生成一份结构清晰的中文 Markdown 报告。",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    from ..axe.llm import LLMClient, LLMError

    client = LLMClient()
    if not getattr(client, "available", False):
        raise AtomFailure(
            "未配置 LLM，无法生成总结（请设置 PANGUVERSE_API_KEY 或 OPENAI_API_KEY）",
            kind="missing_llm",
            failure_class="credentials",
            detail={"capability": "needs_llm"},
        )
    content = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    limit = max(200, int(max_chars or DEFAULT_MAX_CHARS))
    if len(content) > limit:
        content = content[:limit] + f"\n\n…（已截断，原长 {len(content)} 字符）"
    system = (
        "你是网页研究助手。只根据用户提供的页面文本与结构信息作答，"
        "不要编造页面中不存在的事实。输出 Markdown，包含标题、要点与布局观察。"
    )
    prompt = f"{instruction.strip()}\n\n--- 页面内容 ---\n{content}"
    try:
        summary = client.complete(system, prompt)
    except LLMError as exc:
        kind = "missing_llm" if "未配置" in str(exc) else "llm_error"
        raise AtomFailure(
            f"LLM 总结失败: {exc}", kind=kind, failure_class="transient"
        ) from exc
    if not str(summary or "").strip():
        raise AtomFailure(
            "LLM 返回了空总结（不生成占位内容）",
            kind="empty_summary",
            failure_class="internal",
        )
    return {"summary_md": str(summary).strip(), "model": getattr(client, "model", "")}


# --------------------------------------------------------------------------- #
# 领域语义
# --------------------------------------------------------------------------- #
_RETRYABLE = {"network", "timeout", "llm_error"}


class WebResearchSemantics(GenericSemantics):
    """网页研究领域语义：失败分类、恢复策略、目标标识与规划提示。"""

    name = "web_research"

    def failure_class(
        self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None
    ) -> FailureClass:
        if isinstance(exc, AtomFailure):
            try:
                return FailureClass(exc.failure_class)
            except ValueError:
                return FailureClass.INTERNAL
        if isinstance(exc, TimeoutError):
            return FailureClass.TRANSIENT
        if isinstance(exc, ConnectionError):
            return FailureClass.TRANSIENT
        return super().failure_class(atom, outputs, exc)

    def failure_kind(
        self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None
    ) -> str:
        if isinstance(exc, PanguError):
            kind = getattr(exc, "kind", "") or ""
            if kind:
                return kind
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, ConnectionError):
            return "network"
        return super().failure_kind(atom, outputs, exc)

    def is_retryable(
        self,
        atom: Any,
        outputs: Mapping[str, Any] | None,
        *,
        kind: str,
        failure_class: FailureClass,
    ) -> bool:
        return kind in _RETRYABLE or super().is_retryable(
            atom, outputs, kind=kind, failure_class=failure_class
        )

    def target(self, atom: Any, inputs: Mapping[str, Any] | None) -> str:
        if not isinstance(inputs, Mapping):
            return ""
        url = inputs.get("url")
        return str(url) if isinstance(url, str) and url else ""

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
        if kind in {"network", "timeout"}:
            return [
                Recovery(
                    action="retry",
                    reason=f"{kind} 属于瞬态网络故障 → 退避重试",
                    delay_ms=200,
                    retry_hint={
                        "attempts": 3,
                        "delay_ms": 200,
                        "backoff": 2.0,
                        "on": ["network", "timeout"],
                    },
                    advice="确认 URL 可访问、DNS/代理正常；本机地址请检查服务是否启动",
                )
            ]
        if kind == "missing_llm":
            return [
                Recovery(
                    action="none",
                    reason="缺少 LLM 密钥：总结无法进行，必须显式失败而不是产出占位",
                    advice="设置 PANGUVERSE_API_KEY（或 OPENAI_API_KEY / 本机 ollama）后重试",
                )
            ]
        return []

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        texts = {
            "network": "网页不可达：检查 URL、DNS、代理与目标站点是否在线",
            "timeout": "请求超时：放宽 timeout，或检查目标站点响应速度",
            "parse": "HTML 解析失败：确认拿到的是 HTML（不是二进制/压缩包）",
            "invalid_url": "URL 非法：只接受 http/https 绝对地址",
            "missing_llm": "未配置 LLM：设置 PANGUVERSE_API_KEY / OPENAI_API_KEY 后再生成总结",
            "llm_error": "LLM 调用失败：检查模型服务、密钥与网络",
            "empty_summary": "LLM 返回空内容：重试或换模型；不要用占位文本冒充总结",
        }
        return texts.get(kind) or super().advice(kind=kind, failure_class=failure_class)

    def planning_hints(self) -> tuple[str, ...]:
        return (
            "网页任务先用 http_get_page 抓取页面（只读，按 charset 解码；4xx/5xx 返回 status 不抛异常）",
            "正文用 html_extract_text（${fetch_page.html}）；布局/标题/链接用 html_extract_structure",
            "最后用 llm_summarize 生成报告：text 传正文，instruction 里可带 ${extract_structure}",
            "llm_summarize 需要 needs_llm 能力；没有密钥时必须失败，禁止造假总结",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": "web_research",
            "target": "URL",
            "atoms": ["http_get_page", "html_extract_text", "html_extract_structure", "llm_summarize"],
            "read_only": True,
        }


def build_web_page_report() -> dict[str, Any]:
    """「打开网页 → 取正文 → 分析布局 → LLM 报告」的参数化计划。"""
    return {
        "id": "web_page_report",
        "name": "网页内容与布局报告",
        "tags": ["web", "research", "report"],
        "description": "抓取页面、提取正文与结构，最后用 LLM 汇总布局报告",
        "steps": [
            {
                "id": "fetch_page",
                "use": "http_get_page",
                "name": "抓取网页",
                "with": {"url": "${env.PAGE_URL}"},
            },
            {
                "id": "extract_text",
                "use": "html_extract_text",
                "name": "提取正文文本",
                "with": {"html": "${fetch_page.html}"},
            },
            {
                "id": "extract_structure",
                "use": "html_extract_structure",
                "name": "分析页面结构",
                "with": {"html": "${fetch_page.html}"},
            },
            {
                "id": "check_status",
                "use": "assert_json",
                "name": "确认页面状态码",
                "with": {"source": "${fetch_page}", "path": "status", "gte": 200, "lt": 400},
            },
            {
                "id": "summarize",
                "use": "llm_summarize",
                "name": "生成布局报告",
                "with": {
                    "text": "正文：\n${extract_text.text}",
                    "instruction": (
                        "请根据以下页面结构信息与正文，给出中文页面布局报告（Markdown）："
                        "标题层级、主要内容区块、导航链接概览与可读性观察。\n"
                        "结构信息：${extract_structure}"
                    ),
                },
            },
        ],
    }


def web_research_intent_templates() -> list[Any]:
    """``web_research`` 包声明的意图模板。"""
    from ..core.contract import RuleTemplate

    return [
        RuleTemplate(
            name="web_page_report",
            description="网页正文提取与布局报告",
            keywords=(
                "网页",
                "网站",
                "首页",
                "页面",
                "抓取",
                "爬取",
                "取文本",
                "正文",
                "布局",
                "结构",
                "摘要",
                "总结",
                "报告",
                "http://",
                "https://",
            ),
            builder=build_web_page_report,
            tags=("web", "research"),
            weight=2,
            domain="web_research",
            pack="web_research",
        )
    ]


def build_pack() -> Any:
    """构建 ``web_research`` 包。"""
    from ..core.pack import AtomPack

    atoms: list[Atom] = [http_get_page, html_extract_text, html_extract_structure, llm_summarize]
    for item in atoms:
        enrich(item, domain="web_research", pack="web_research")
    return AtomPack(
        name="web_research",
        domain="web_research",
        version="0.1.0",
        description="网页研究领域包：抓取页面、提取正文/结构、调用 LLM 生成布局报告（全只读）",
        semantics=WebResearchSemantics(),
        atoms=atoms,
        requires=("network",),
        tags=("builtin", "web", "research"),
        license="Apache-2.0",
        intent_templates=web_research_intent_templates(),
    )


__all__ = [
    "WebResearchSemantics",
    "build_pack",
    "build_web_page_report",
    "decode_html",
    "extract_structure",
    "extract_text",
    "html_extract_structure",
    "html_extract_text",
    "http_get_page",
    "llm_summarize",
    "web_research_intent_templates",
]
