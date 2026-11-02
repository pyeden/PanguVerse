"""诚实规划门禁：意图 → 领域 + 动作双重命中 → 计划 / 能力缺口。

这个模块解决诊断里的 R1/R3：

- 不再用「零散关键词命中」或「意图为空就用演示模板」糊弄用户；
- 领域 + 动作必须同时命中，否则返回 422 ``capability_gap`` 并列出 ``missing_atoms``；
- LLM 规划要求原子清单 + 端口契约，产出后逐节点校验原子是否存在；
- 规则/模板来源固定低置信度，并在 ``assumptions`` 里列出未提供的环境依赖。

它只依赖领域包与规划器，不碰执行语义；工作台层（``studio.py``）负责把
:class:`PlanGateError` 翻译成 API 错误体。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..axe.plan import PlanRequest, TestPlan
from ..axe.planner import LLMPlanner, RuleBasedPlanner
from ..core.errors import PlanError

URL_RE = re.compile(r"https?://[^\s，。；、）)\]}>\"']+", re.IGNORECASE)

#: 明确「当前原子库表达不了」的网页动作 → 需要的新原子
WEB_MISSING_ACTIONS: dict[str, tuple[str, str]] = {
    "ocr": ("ocr_image", "图片 OCR 原子（ocr_image）"),
    "识别图片": ("ocr_image", "图片 OCR 原子（ocr_image）"),
    "图片文字": ("ocr_image", "图片 OCR 原子（ocr_image）"),
    "文字识别": ("ocr_image", "图片 OCR 原子（ocr_image）"),
    "下载图片": ("http_download_assets", "资源下载原子（http_download_assets）"),
    "下载文件": ("http_download_file", "文件下载原子（http_download_file）"),
    "点击": ("browser_click", "浏览器点击原子（browser_click）"),
    "填表": ("browser_fill", "浏览器表单原子（browser_fill）"),
    "登录网站": ("browser_login", "浏览器登录原子（browser_login）"),
    "pdf": ("pdf_extract_text", "PDF 提取原子（pdf_extract_text）"),
    "翻译": ("llm_translate", "翻译原子（llm_translate）"),
    "视频": ("video_extract", "视频解析原子（video_extract）"),
    "音频": ("audio_transcribe", "音频转写原子（audio_transcribe）"),
}

#: iOS 专属能力缺口：原子库没有元素级交互（需要 XCUITest/WebDriverAgent）
IOS_MISSING_ACTIONS: dict[str, tuple[str, str]] = {
    "点击": ("ios_tap", "iOS 元素点击原子（ios_tap，需要 XCUITest/WebDriverAgent）"),
    "tap": ("ios_tap", "iOS 元素点击原子（ios_tap，需要 XCUITest/WebDriverAgent）"),
    "输入": ("ios_input_text", "iOS 文本输入原子（ios_input_text，需要 XCUITest/WebDriverAgent）"),
    "input": ("ios_input_text", "iOS 文本输入原子（ios_input_text，需要 XCUITest/WebDriverAgent）"),
    "滑动": ("ios_swipe", "iOS 滑动原子（ios_swipe，需要 XCUITest/WebDriverAgent）"),
    "swipe": ("ios_swipe", "iOS 滑动原子（ios_swipe，需要 XCUITest/WebDriverAgent）"),
}

#: 安卓专属能力缺口：点击/输入/截图/UI dump 都已实现；只缺 APK 安装
ANDROID_MISSING_ACTIONS: dict[str, tuple[str, str]] = {
    "安装 apk": ("android_install_app", "安卓 APK 安装原子（android_install_app）"),
    "安装apk": ("android_install_app", "安卓 APK 安装原子（android_install_app）"),
}

#: 领域 → 缺失动作表。**能力缺口 ≠ 环境缺失**：
#: 安卓点击原子存在，只会在运行期因无设备报 missing_device，不能在这里判 gap。
_MISSING_BY_DOMAIN: dict[str, dict[str, tuple[str, str]]] = {
    "web_browser": WEB_MISSING_ACTIONS,
    "web_research": WEB_MISSING_ACTIONS,
    "android_adb": ANDROID_MISSING_ACTIONS,
    "ios_sim": IOS_MISSING_ACTIONS,
}

#: 领域词 → 领域包
_DOMAIN_HINTS: dict[str, tuple[str, ...]] = {
    "web_research": (
        "网页",
        "网站",
        "首页",
        "页面",
        "门户",
        "html",
        "url",
        "http://",
        "https://",
        "抓取",
        "爬取",
        "爬虫",
        "web",
    ),
    "web_browser": (
        "无头浏览器",
        "浏览器",
        "chrome",
        "chromium",
        "browser",
        "打开网页",
        "打开页面",
        "访问网页",
        "页面截图",
        "网页截图",
        "取链接",
        "提取链接",
        "等待文本",
        "渲染后",
        "前端渲染",
    ),
    "android_adb": (
        "安卓",
        "android",
        "adb",
        "安卓设备",
        "安卓手机",
        "安卓模拟器",
        "坐标",
        "uiautomator",
        "ui 控件",
        "界面控件",
    ),
    "ios_sim": (
        "ios",
        "iphone",
        "ipad",
        "ios 模拟器",
        "ios模拟器",
        "模拟器",
        "simulator",
        "simctl",
        "xcrun",
        "xcode",
        "安装 ipa",
    ),
    "local_fs": (
        "文件",
        "目录",
        "配置",
        "本地",
        "磁盘",
        "json 文件",
        "落盘",
        "备份",
        "快照",
        "临时文件",
        ".conf",
        ".json",
        "fs_root",
    ),
    "api_testing": (
        "接口",
        "api",
        "订单",
        "用户",
        "登录",
        "支付",
        "健康",
        "token",
        "鉴权",
        "服务",
        "http 请求",
        "rest",
    ),
}

#: 动作词 → 该动作需要的原子（用于覆盖率和能力缺口）
_ACTION_ATOMS: dict[str, dict[str, tuple[str, ...]]] = {
    "web_research": {
        "fetch": ("http_get_page",),
        "text": ("html_extract_text",),
        "structure": ("html_extract_structure",),
        "summary": ("llm_summarize",),
    },
    "local_fs": {
        "backup": ("fs_copy",),
        "write": ("fs_write_text", "fs_write_json"),
        "read": ("fs_read_text", "fs_read_json"),
        "cleanup": ("fs_delete",),
        "verify": ("assert_file_contains", "fs_exists"),
        "list": ("fs_list",),
    },
    "api_testing": {
        "request": ("http_request",),
        "login": ("http_request", "assert_json"),
        "order": ("http_request", "assert_json"),
        "health": ("http_request", "assert_json"),
        "user": ("http_request", "assert_json"),
        "assert": ("assert_json", "assert_schema"),
    },
    "web_browser": {
        "open": ("web_open",),
        "text": ("web_get_text",),
        "links": ("web_extract_links",),
        "screenshot": ("web_screenshot",),
        "wait": ("web_wait_for_text",),
    },
    "android_adb": {
        "device": ("android_devices",),
        "launch": ("android_launch_app",),
        "stop": ("android_stop_app",),
        "tap": ("android_tap",),
        "swipe": ("android_swipe",),
        "input": ("android_input_text",),
        "key": ("android_keyevent",),
        "screenshot": ("android_screencap",),
        "ui": ("android_ui_dump",),
        "find": ("android_find_text",),
        "wait": ("android_wait_text",),
    },
    "ios_sim": {
        "list": ("ios_sim_list",),
        "boot": ("ios_sim_boot",),
        "shutdown": ("ios_sim_shutdown",),
        "install": ("ios_sim_install",),
        "launch": ("ios_sim_launch",),
        "terminate": ("ios_sim_terminate",),
        "openurl": ("ios_sim_openurl",),
        "screenshot": ("ios_sim_screenshot",),
        "tap": ("ios_tap",),
        "input": ("ios_input_text",),
        "swipe": ("ios_swipe",),
    },
}

_WEB_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "fetch": ("打开", "访问", "抓取", "爬取", "获取", "下载", "请求", "首页", "网页", "页面", "url"),
    "text": ("取文本", "提取文本", "文本内容", "正文", "文字", "内容", "文本"),
    "structure": ("布局", "结构", "版面", "标题", "链接", "导航", "区块", "目录"),
    "summary": ("报告", "总结", "摘要", "分析", "汇总", "建议"),
}

_FS_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "backup": ("备份", "复制", "快照", "归档", "backup"),
    "write": ("写入", "写出", "生成", "落盘", "创建", "保存"),
    "read": ("读取", "回读", "查看", "读出"),
    "cleanup": ("清理", "删除", "释放空间", "cleanup"),
    "verify": ("校验", "断言", "检查", "核对", "确认", "存在"),
    "list": ("列出", "列举", "清单", "目录"),
}

_API_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "login": ("登录", "认证", "鉴权", "token", "login", "signin"),
    "order": ("下单", "订单", "支付", "交易", "order", "pay"),
    "health": ("健康", "探活", "存活", "health", "ping"),
    "user": ("用户", "注册", "增删改查", "crud", "user"),
    "assert": ("断言", "校验", "检查", "验证", "状态码"),
    "request": ("请求", "接口", "api", "http"),
}

_WEB_BROWSER_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "open": ("打开网页", "打开页面", "访问网页", "打开", "访问", "抓取页面"),
    "text": ("取文本", "提取正文", "页面文本", "文本内容", "正文"),
    "links": ("链接", "取链接", "提取链接", "href"),
    "screenshot": ("截图", "截屏", "screenshot"),
    "wait": ("等待文本", "等待出现", "动态加载", "wait for"),
}

_ANDROID_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "device": ("设备列表", "列出设备", "探测设备", "adb devices", "连接设备"),
    "launch": ("启动应用", "启动 app", "打开 app", "launch"),
    "stop": ("停止应用", "关闭应用", "force-stop", "停止"),
    "tap": ("点击", "点击坐标", "tap"),
    "swipe": ("滑动", "swipe"),
    "input": ("输入", "输入文字", "输入文本", "填写", "input text"),
    "key": ("按键", "keyevent", "返回键"),
    "screenshot": ("截图", "截屏", "screencap"),
    "ui": ("ui 控件", "界面控件", "控件树", "uiautomator", "ui dump"),
    "find": ("查找文本", "查找控件", "find text"),
    "wait": ("等待文本", "等待控件"),
}

_IOS_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "list": ("模拟器列表", "列出模拟器", "设备列表", "list"),
    "boot": ("启动模拟器", "boot"),
    "shutdown": ("关闭模拟器", "shutdown"),
    "install": ("安装 ipa", "安装 app", "install"),
    "launch": ("启动 app", "启动应用", "launch"),
    "terminate": ("结束 app", "终止 app", "terminate"),
    "openurl": ("打开 url", "深链", "openurl"),
    "screenshot": ("截图", "截屏", "screenshot"),
    "tap": ("点击", "tap"),
    "input": ("输入", "输入文字", "输入文本", "input text"),
    "swipe": ("滑动", "swipe"),
}

#: 动作 → 模板名（只用于自动领域路由；显式 domain_pack 仍走旧模板打分）
_TEMPLATE_BY_ACTION: dict[str, dict[str, str]] = {
    "web_research": {"fetch": "web_page_report", "text": "web_page_report", "structure": "web_page_report", "summary": "web_page_report"},
    "local_fs": {
        "backup": "fs_config_backup",
        "write": "fs_data_roundtrip",
        "read": "fs_data_roundtrip",
        "cleanup": "fs_cleanup",
    },
    "web_browser": {
        "open": "web_browser_content",
        "text": "web_browser_content",
        "links": "web_browser_links",
        "screenshot": "web_browser_screenshot",
        "wait": "web_browser_wait_text",
    },
    "android_adb": {
        "device": "android_devices_probe",
        "launch": "android_launch_screenshot",
        "screenshot": "android_launch_screenshot",
        "tap": "android_tap_coordinates",
    },
    "ios_sim": {
        "list": "ios_simulator_list",
    },
    "api_testing": {
        "login": "login_flow",
        "order": "order_flow",
        "health": "health_check",
        "user": "user_crud",
        "assert": "auth_negative",
        "request": "health_check",
    },
}

_STOP_TERMS = {"获取", "给我", "帮我", "一个", "一下", "并且", "然后", "以及", "内容", "这个", "那个"}


class PlanGateError(Exception):
    """规划门禁错误（由工作台层翻译为 422/502）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 422,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = dict(details or {})


@dataclass
class IntentAnalysis:
    intent: str
    domain: str = ""
    url: str = ""
    actions: list[str] = field(default_factory=list)
    requested_atoms: list[str] = field(default_factory=list)
    missing_atoms: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(self.domain and self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "url": self.url,
            "actions": list(self.actions),
            "requested_atoms": list(self.requested_atoms),
            "missing_atoms": list(self.missing_atoms),
            "suggestions": list(self.suggestions),
        }


@dataclass
class DraftPlan:
    plan: TestPlan
    planner: str
    plan_source: str
    confidence: float
    assumptions: list[str]
    capability_gaps: list[str]
    analysis: dict[str, Any]


# --------------------------------------------------------------------------- #
# 意图分析
# --------------------------------------------------------------------------- #
def _hits(text: str, words: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [word for word in words if word.lower() in lowered]


def _domain_hits(text: str, domain: str) -> list[str]:
    return _hits(text, _DOMAIN_HINTS.get(domain, ()))


def _action_words(domain: str) -> dict[str, tuple[str, ...]]:
    if domain == "web_browser":
        return _WEB_BROWSER_ACTION_WORDS
    if domain == "web_research":
        return _WEB_ACTION_WORDS
    if domain == "android_adb":
        return _ANDROID_ACTION_WORDS
    if domain == "ios_sim":
        return _IOS_ACTION_WORDS
    if domain == "local_fs":
        return _FS_ACTION_WORDS
    return _API_ACTION_WORDS


def analyze_intent(intent: str, *, domain_pack: str = "") -> IntentAnalysis:
    """把意图拆成「领域 + 动作 + 缺失能力」，**不做任何兜底补全**。

    关键区分：**能力缺口 ≠ 环境缺失**。

    - 原子不存在（例如 iOS 点击需要 ``ios_tap``）→ ``missing_atoms`` / ``capability_gap``；
    - 原子存在但运行期没有设备（例如 ``android_tap`` 无设备）→ 不在这里判 gap，
      由原子在执行时返回 ``missing_device``。
    """
    text = str(intent or "").strip()
    analysis = IntentAnalysis(intent=text)
    urls = [match.group(0).rstrip(".,;:!?") for match in URL_RE.finditer(text)]
    analysis.url = urls[0] if urls else ""

    # 先做领域 + 动作路由（新领域靠词表和动作词参与同一套打分）
    candidates = (
        [domain_pack]
        if domain_pack
        else ["web_browser", "web_research", "android_adb", "ios_sim", "local_fs", "api_testing"]
    )
    best: tuple[int, str, list[str]] | None = None
    for domain in candidates:
        domain_hits = _domain_hits(text, domain)
        actions = [
            action
            for action, words in _action_words(domain).items()
            if _hits(text, words)
        ]
        if not domain_hits or not actions:
            continue
        score = len(domain_hits) + len(actions) * 2
        if best is None or score > best[0]:
            best = (score, domain, actions)
    if best is not None:
        _score, domain, actions = best
        analysis.domain = domain
        analysis.actions = actions
        atoms: list[str] = []
        for action in actions:
            for atom_name in _ACTION_ATOMS.get(domain, {}).get(action, ()):
                if atom_name not in atoms:
                    atoms.append(atom_name)
        analysis.requested_atoms = atoms
    elif domain_pack:
        analysis.domain = domain_pack

    # 缺失动作：按**已路由到的领域**查表，避免把 iOS/安卓点击误判成浏览器点击
    lowered = text.lower()
    missing: list[str] = []
    suggestions: list[str] = []
    table = _MISSING_BY_DOMAIN.get(analysis.domain, WEB_MISSING_ACTIONS)
    for word, (atom_name, hint) in table.items():
        if word.lower() in lowered and atom_name not in missing:
            missing.append(atom_name)
            suggestions.append(f"需要新增原子：{hint}")
    analysis.missing_atoms = missing
    analysis.suggestions = suggestions

    terms: list[str] = []
    domain_terms = _domain_hits(text, analysis.domain) if analysis.domain else []
    for word in [*domain_terms, *analysis.actions]:
        if len(word) >= 2 and word not in terms:
            terms.append(word)
    # 动作词本身对覆盖率更有价值（中文词通常 2 字以上）
    for action in analysis.actions:
        for word in _action_words(analysis.domain).get(action, ()):
            if len(word) >= 2 and word in text and word not in terms:
                terms.append(word)
    analysis.terms = terms

    if analysis.domain == "web_research" and "fetch" in analysis.actions and not analysis.url:
        if "http_get_page" not in analysis.missing_atoms:
            analysis.missing_atoms.insert(0, "http_get_page")
        analysis.suggestions.append("请提供目标网页 URL（如 https://example.com）")
        analysis.assumptions.append("需要提供目标网页 URL")
    if analysis.domain == "web_browser" and not analysis.url and (
        "open" in analysis.actions or "screenshot" in analysis.actions
    ):
        analysis.assumptions.append("需要提供目标网页 URL（env.PAGE_URL）")
    return analysis

# --------------------------------------------------------------------------- #
# 覆盖率（语义校验用）
# --------------------------------------------------------------------------- #
_HELPER_ATOMS = {
    "assert_json",
    "assert_schema",
    "assert_file_contains",
    "assert_file_exists",
    "log",
    "sleep",
    "set_var",
    "json_get",
    "regex_extract",
}


def _node_text(node: Mapping[str, Any]) -> str:
    parts = [
        str(node.get("id") or ""),
        str(node.get("atom") or ""),
        str(node.get("name") or ""),
        str(node.get("description") or ""),
    ]
    params = node.get("params")
    if isinstance(params, Mapping):
        for key in ("url", "path", "source", "text", "instruction", "message"):
            value = params.get(key)
            if isinstance(value, str):
                parts.append(value)
        parts.append(" ".join(str(value) for value in params.values() if isinstance(value, str)))
    return " ".join(parts).lower()


def _is_helper(node: Mapping[str, Any]) -> bool:
    if node.get("helper") is True:
        return True
    tags = {str(tag).lower() for tag in (node.get("tags") or ())}
    if "assert" in tags or "helper" in tags:
        return True
    return str(node.get("atom") or "") in _HELPER_ATOMS


def intent_coverage(nodes: Iterable[Mapping[str, Any]], intent: str) -> dict[str, Any]:
    """意图覆盖率：每个非 helper 节点是否映射到意图动作/实体词。

    - helper 节点（断言/日志/变量）不计入分母（它们服务于动作节点）；
    - 覆盖率 < 60% 由工作台判为 ``intent_mismatch`` 阻断发布。
    """
    analysis = analyze_intent(intent or "")
    nodes = list(nodes)
    actionable = [node for node in nodes if not _is_helper(node)]
    matched: list[str] = []
    unmatched: list[str] = []
    requested = set(analysis.requested_atoms)
    terms = [term.lower() for term in analysis.terms if len(term) >= 2 and term not in _STOP_TERMS]
    for node in actionable:
        atom = str(node.get("atom") or "")
        text = _node_text(node)
        ok = atom in requested
        if not ok:
            ok = any(term in text for term in terms)
        node_id = str(node.get("id") or "")
        if ok:
            matched.append(node_id)
        else:
            unmatched.append(node_id)
    total = len(actionable)
    ratio = 1.0 if total == 0 else round(len(matched) / total, 4)
    return {
        "matched": len(matched),
        "total": total,
        "ratio": ratio,
        "matched_nodes": matched,
        "unmatched_nodes": unmatched,
        "domain": analysis.domain,
    }


# --------------------------------------------------------------------------- #
# 规划
# --------------------------------------------------------------------------- #
def _templates_for(registry: Any, domain: str) -> list[Any]:
    out: list[Any] = []
    for pack in registry.packs():
        for template in getattr(pack, "intent_templates", []) or []:
            if domain and domain not in {
                str(getattr(template, "pack", "") or ""),
                str(getattr(template, "domain", "") or ""),
            }:
                continue
            out.append(template)
    return out


def _template_named(templates: Iterable[Any], name: str) -> Any | None:
    for template in templates:
        if str(getattr(template, "name", "")) == name:
            return template
    return None


def _build_web_spec(intent: str, analysis: IntentAnalysis) -> dict[str, Any]:
    from ..packs.web_research import build_web_page_report

    spec = build_web_page_report()
    if analysis.url:
        for step in spec.get("steps") or []:
            if step.get("id") == "fetch_page":
                step.setdefault("with", {})["url"] = analysis.url
    return spec


def _build_rule_plan(
    registry: Any,
    *,
    intent: str,
    domain: str,
    analysis: IntentAnalysis,
    explicit_domain: bool,
) -> TestPlan:
    from ..spec.loader import expand_case_spec

    templates = _templates_for(registry, domain)
    if not templates:
        raise PlanGateError(
            "capability_gap",
            f"领域包 {domain} 没有可用的意图模板",
            details={"missing_atoms": analysis.missing_atoms, "suggestions": ["安装/启用对应领域包"]},
        )

    if domain == "web_research":
        specs = [expand_case_spec(_build_web_spec(intent, analysis), registry=registry)]
        from ..core.molecule import Molecule

        plan = TestPlan(
            cases=[Molecule.from_spec(spec, source="template") for spec in specs],
            source="template",
            planner="template",
            intent=intent,
            env={},
            notes=["网页研究模板：抓取 → 正文 → 结构 → LLM 报告"],
            meta={"template": "web_page_report", "llm_used": False},
        )
        return plan

    if explicit_domain:
        # 显式选择领域包：保留旧行为（含默认模板组），但置信度必须低并标注人工核对
        planner = RuleBasedPlanner(registry, templates=templates, max_cases=3)
        return planner.plan(PlanRequest(intent=intent, env={}, allow_llm=False))

    chosen_names: list[str] = []
    for action in analysis.actions:
        name = _TEMPLATE_BY_ACTION.get(domain, {}).get(action)
        if name and name not in chosen_names:
            chosen_names.append(name)
    chosen = [template for template in (_template_named(templates, name) for name in chosen_names) if template]
    if not chosen:
        # 领域 + 动作命中但模板未覆盖：视为能力缺口，不产出无关模板
        raise PlanGateError(
            "capability_gap",
            "当前领域包没有覆盖该动作的意图模板",
            details={
                "missing_atoms": analysis.missing_atoms,
                "suggestions": [f"为 {domain} 增加动作模板，或改用场景 YAML 精确描述"],
            },
        )
    specs = []
    for template in chosen:
        spec = template.build()
        if domain == "web_browser" and analysis.url:
            for step in spec.get("steps") or []:
                if step.get("use") in {"web_open", "web_screenshot"}:
                    step.setdefault("with", {})["url"] = analysis.url
        specs.append(expand_case_spec(spec, registry=registry))
    from ..core.molecule import Molecule

    return TestPlan(
        cases=[Molecule.from_spec(spec, source="template") for spec in specs],
        source="template",
        planner="template",
        intent=intent,
        env={},
        notes=[f"规则模板：{', '.join(template.name for template in chosen)}"],
        meta={"templates": [template.name for template in chosen], "llm_used": False},
    )


def _llm_draft(registry: Any, *, intent: str, client: Any, env: Mapping[str, Any] | None) -> TestPlan:
    planner = LLMPlanner(registry, client, fallback=None, max_repair_rounds=1)
    try:
        plan = planner.plan(
            PlanRequest(intent=intent, env=dict(env or {}), allow_llm=True)
        )
    except PlanError as exc:
        message = str(exc)
        missing = _missing_atoms_from_message(message)
        raise PlanGateError(
            "capability_gap" if missing else "planner_failed",
            f"LLM 规划失败：{message}",
            status=422 if missing else 502,
            details={
                "missing_atoms": missing,
                "suggestions": ["在提示词原子清单内重试，或补装缺失领域包"] if missing else [],
            },
        ) from exc
    unknown: list[str] = []
    for case in plan.cases:
        for step in case.steps:
            if not registry.has(step.use) and step.use not in unknown:
                unknown.append(step.use)
    if unknown:
        raise PlanGateError(
            "capability_gap",
            "LLM 计划引用了原子库中不存在的原子（未丢弃，按能力缺口处理）",
            details={
                "missing_atoms": unknown,
                "suggestions": ["补充对应领域包，或让模型只使用清单内原子"],
            },
        )
    return plan


def _missing_atoms_from_message(message: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"原子不存在[:：]\s*([A-Za-z0-9_.-]+)", message):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


def plan_draft(
    registry: Any,
    *,
    intent: str,
    domain_pack: str = "",
    exclude_atoms: Iterable[str] = (),
    allow_llm: bool = True,
    llm_client: Any = None,
    env: Mapping[str, Any] | None = None,
) -> DraftPlan:
    """产出一份「诚实」的草稿计划；不理解时抛 :class:`PlanGateError`。"""
    analysis = analyze_intent(intent, domain_pack=domain_pack)
    explicit = bool(domain_pack)

    if not explicit and not analysis.hit:
        raise PlanGateError(
            "capability_gap",
            "当前原子库无法表达该意图：未同时命中领域与动作",
            details={
                "missing_atoms": analysis.missing_atoms,
                "suggestions": analysis.suggestions
                or ["补充领域包/原子，或把意图拆成更具体的子任务"],
                "intent": intent,
            },
        )
    if analysis.missing_atoms:
        raise PlanGateError(
            "capability_gap",
            "意图包含当前原子库没有的能力",
            details={
                "missing_atoms": analysis.missing_atoms,
                "suggestions": analysis.suggestions,
                "intent": intent,
            },
        )
    if analysis.domain == "web_research" and not analysis.actions:
        raise PlanGateError(
            "capability_gap",
            "意图没有命中网页研究动作，不能套用 web_research 模板",
            details={
                "missing_atoms": analysis.missing_atoms,
                "suggestions": ["明确要抓取/提取正文/分析布局/生成报告，或换用匹配的领域包"],
                "intent": intent,
            },
        )
    if analysis.domain == "web_research" and not analysis.url:
        raise PlanGateError(
            "capability_gap",
            "网页任务缺少目标 URL",
            details={
                "missing_atoms": ["http_get_page"],
                "suggestions": ["在意图中给出 https://... 地址"],
                "intent": intent,
            },
        )

    planner = "template" if not explicit else "rules"
    plan_source = "template" if not explicit else "rules"
    confidence = 0.45 if analysis.domain == "web_research" else 0.4
    plan: TestPlan | None = None
    if allow_llm and llm_client is not None and getattr(llm_client, "available", False):
        plan = _llm_draft(registry, intent=intent, client=llm_client, env=env)
        planner = "llm"
        plan.planner = "llm"
        plan.source = "llm"
        try:
            confidence = float(plan.meta.get("confidence") or confidence)
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = max(0.0, min(1.0, confidence))

    if plan is None:
        plan = _build_rule_plan(
            registry,
            intent=intent,
            domain=analysis.domain or domain_pack,
            analysis=analysis,
            explicit_domain=explicit,
        )

    if plan.planner:
        planner = plan.planner
    if plan.source:
        plan_source = plan.source
    excluded = {str(item) for item in exclude_atoms}
    if excluded:
        kept = []
        for case in plan.cases:
            case.steps = [step for step in case.steps if step.use not in excluded]
            if case.steps:
                kept.append(case)
        plan.cases = kept
    if not plan.cases:
        raise PlanGateError(
            "capability_gap",
            "意图没有生成任何可执行步骤（检查 exclude_atoms / 原子能力）",
            details={"missing_atoms": analysis.missing_atoms, "suggestions": []},
        )

    assumptions = list(analysis.assumptions)
    for step in (step for case in plan.cases for step in case.steps):
        for key in re.findall(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)", str(step.with_)):
            if key not in assumptions and not (env and key in env):
                assumptions.append(f"需要 {key}")
    capability_gaps = list(analysis.missing_atoms)
    return DraftPlan(
        plan=plan,
        planner=planner,
        plan_source=plan_source,
        confidence=confidence,
        assumptions=sorted(dict.fromkeys(assumptions)),
        capability_gaps=capability_gaps,
        analysis=analysis.to_dict(),
    )


__all__ = [
    "DraftPlan",
    "IntentAnalysis",
    "PlanGateError",
    "analyze_intent",
    "intent_coverage",
    "plan_draft",
]
