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
from .site_aliases import resolve_site_url

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

#: 设备域：意图里出现设备词时，设备域优先于网页域
_DEVICE_DOMAINS: tuple[str, ...] = ("android_adb", "ios_sim")

#: 强 iOS 词：只有它们出现时才把「模拟器」算作 iOS（避免「安卓模拟器」串味）
_IOS_STRONG_HINTS: tuple[str, ...] = (
    "ios",
    "iphone",
    "ipad",
    "ios 模拟器",
    "ios模拟器",
    "simulator",
    "simctl",
    "xcrun",
    "xcode",
)

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
    # 「获取内容/新闻/热点」也是正文提取目标：只靠「取文本」会漏掉真实用户的说法
    "text": ("取文本", "提取文本", "文本内容", "正文", "文字", "内容", "文本", "新闻", "热点", "信息"),
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
    "text": ("取文本", "提取正文", "页面文本", "文本内容", "正文", "内容", "新闻", "热点", "信息"),
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

# 多段词用 ``#`` 分隔：按顺序出现即可，中间允许插入限定语。
_IOS_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "list": (
        "模拟器列表",
        "列出#模拟器",
        "列举#模拟器",
        "有哪些#模拟器",
        "模拟器#有哪些",
        "列出#设备",
        "设备列表",
        "list",
    ),
    "boot": ("启动#模拟器", "启动模拟器", "启动#simulator", "boot", "开机"),
    "shutdown": ("关闭#模拟器", "关闭模拟器", "shutdown", "关机"),
    "install": ("安装 ipa", "安装#app", "安装#应用", "install"),
    "launch": (
        "启动 app",
        "启动#app",
        "启动#应用",
        "打开#app",
        "launch",
        "safari",
    ),
    "terminate": ("终止#app", "结束#app", "终止应用", "terminate"),
    "openurl": (
        "打开#网页",
        "打开#链接",
        "打开#url",
        "打开#页面",
        "访问#网页",
        "深链",
        "deeplink",
        "openurl",
    ),
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
    #: URL 来源：``explicit``（用户原句里的 URL）或 ``site_alias``（站点别名表解析）。
    url_source: str = ""
    #: 站点别名命中时的站点 key（如 ``baidu``），未命中为空。
    site: str = ""
    actions: list[str] = field(default_factory=list)
    requested_atoms: list[str] = field(default_factory=list)
    missing_atoms: list[str] = field(default_factory=list)
    #: 缺少的是**输入**（如目标 URL），不是原子能力
    missing_inputs: list[str] = field(default_factory=list)
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
            "url_source": self.url_source,
            "site": self.site,
            "actions": list(self.actions),
            "requested_atoms": list(self.requested_atoms),
            "missing_atoms": list(self.missing_atoms),
            "missing_inputs": list(self.missing_inputs),
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
def _word_hit(text: str, word: str) -> bool:
    """单词命中；词里的 ``#`` 表示按顺序出现的多段子串。

    ``列出#模拟器`` 能命中「列出 iOS 模拟器」，但不会命中「启动 iOS 模拟器」，
    这样既容忍限定语插入，又不会把不同动作混为一谈。
    """
    lowered = text.lower()
    target = word.lower()
    if "#" not in target:
        return target in lowered
    cursor = 0
    for part in (chunk.strip() for chunk in target.split("#")):
        if not part:
            continue
        found = lowered.find(part, cursor)
        if found < 0:
            return False
        cursor = found + len(part)
    return True


def _hits(text: str, words: Iterable[str]) -> list[str]:
    return [word for word in words if _word_hit(text, word)]


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
    if analysis.url:
        # 显式 URL 永远优先；站点别名只在没有显式 URL 时参与解析。
        analysis.url_source = "explicit"
    else:
        resolution = resolve_site_url(text)
        if resolution is not None:
            analysis.url = resolution.url
            analysis.url_source = resolution.source
            analysis.site = resolution.site

    # 先做领域 + 动作路由（新领域靠词表和动作词参与同一套打分）
    # 设备域优先：同时出现「iOS/模拟器」和「网页/浏览器」时走设备域，
    # 不能把「在 iOS 模拟器上打开网页并截图」抢去 web_browser。
    if domain_pack:
        candidates = [domain_pack]
    else:
        device_hits = [domain for domain in _DEVICE_DOMAINS if _domain_hits(text, domain)]
        if "ios_sim" in device_hits and "android_adb" in device_hits and not _hits(text, _IOS_STRONG_HINTS):
            # 「安卓模拟器」里的「模拟器」不是 iOS
            device_hits.remove("ios_sim")
        candidates = device_hits or [
            "web_browser",
            "web_research",
            "android_adb",
            "ios_sim",
            "local_fs",
            "api_testing",
        ]
    best: tuple[int, str, list[str]] | None = None
    fallback: tuple[int, str] | None = None
    for domain in candidates:
        domain_hits = _domain_hits(text, domain)
        if not domain_hits:
            continue
        actions = [action for action, words in _action_words(domain).items() if _hits(text, words)]
        if not actions:
            if fallback is None or len(domain_hits) > fallback[0]:
                fallback = (len(domain_hits), domain)
            continue
        score = len(domain_hits) + len(actions) * 2
        if best is None or score > best[0]:
            best = (score, domain, actions)
    if best is None and domain_pack:
        # 显式选择领域包时领域词不一定出现（「打开百度网页」没有「浏览器」），
        # 仍要按该包的动作词表识别动作，否则会退化成整包模板乱配。
        explicit_actions = [
            action for action, words in _action_words(domain_pack).items() if _hits(text, words)
        ]
        if explicit_actions:
            best = (0, domain_pack, explicit_actions)
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
    elif fallback is not None:
        # 领域命中但动作没命中：保留领域，便于 422 给出该域的候选原子
        analysis.domain = fallback[1]
    elif domain_pack:
        analysis.domain = domain_pack

    # 缺失动作：按**已路由到的领域**查表，避免把 iOS/安卓点击误判成浏览器点击
    missing: list[str] = []
    suggestions: list[str] = []
    table = _MISSING_BY_DOMAIN.get(analysis.domain, WEB_MISSING_ACTIONS)
    for word, (atom_name, hint) in table.items():
        if _word_hit(text, word) and atom_name not in missing:
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

    # 缺 URL 是**输入缺失**，不是能力缺口：原子都存在，只是没有目标地址。
    # 绝不能把已有原子（http_get_page / web_open）写进 missing_atoms。
    needs_url = bool(
        (
            analysis.domain == "web_research"
            and {"fetch", "text", "structure", "summary"} & set(analysis.actions)
        )
        or (
            analysis.domain == "web_browser"
            and {"open", "text", "screenshot", "wait"} & set(analysis.actions)
        )
    )
    if needs_url and not analysis.url:
        analysis.missing_inputs = ["url"]
        analysis.suggestions.append("请提供目标网页 URL（如 https://example.com）")
        analysis.assumptions.append("需要提供目标网页 URL")
    return analysis


def _candidate_atoms(analysis: IntentAnalysis) -> list[str]:
    """给出该领域可用的候选原子，保证 422 不会出现空的 ``missing_atoms``。"""
    if analysis.requested_atoms:
        return list(analysis.requested_atoms)
    atoms: list[str] = []
    for names in _ACTION_ATOMS.get(analysis.domain, {}).values():
        for atom_name in names:
            if atom_name not in atoms:
                atoms.append(atom_name)
    return atoms


def _mapping_gap(
    analysis: IntentAnalysis,
    *,
    suggestions: Iterable[str] = (),
) -> tuple[str, list[str], list[str]]:
    """把「无法映射」的意图翻译成可解释的 422 明细。

    - 真正的原子缺口 → ``capability_gap`` + ``missing_atoms``；
    - 有领域但没命中动作/模板 → ``intent_unmapped`` + 候选原子；
    - 什么都命中不了 → ``intent_unmapped`` + 明确说明，``missing_atoms`` 用
      ``intent_unmapped`` 占位，绝不返回空数组。
    """
    extra = list(suggestions)
    if analysis.missing_atoms:
        return "capability_gap", list(analysis.missing_atoms), [*analysis.suggestions, *extra]
    candidates = _candidate_atoms(analysis)
    if candidates:
        return (
            "intent_unmapped",
            candidates,
            [
                "意图无法映射到已落地的动作/模板；候选原子：" + "、".join(candidates),
                *analysis.suggestions,
                *extra,
            ],
        )
    return (
        "intent_unmapped",
        ["intent_unmapped"],
        [
            "意图无法映射到已有原子：未命中任何领域或动作词；请把意图拆成更具体的子任务，或显式指定 domain_pack",
            *analysis.suggestions,
            *extra,
        ],
    )


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


def _build_web_spec(
    intent: str, analysis: IntentAnalysis, *, llm_ready: bool = True
) -> dict[str, Any]:
    from ..packs.web_research import build_web_page_report

    spec = build_web_page_report()
    if not llm_ready:
        # 没有 LLM 时 summary 节点必然 missing_llm；结构分析只为报告服务，一并去掉。
        # 留下「抓取 → 正文 → 状态码断言」，from_start 才能全绿。
        spec["steps"] = [
            step
            for step in spec.get("steps") or []
            if step.get("use") not in {"llm_summarize", "html_extract_structure"}
        ]
        spec["name"] = "网页正文提取"
        spec["description"] = "抓取页面并提取正文（未配置 LLM，不生成总结节点）"
    # 显式 URL 沿用旧行为直接注入（plan_draft 的调用方可能不经过工作台自动填充层）；
    # 站点别名解析出的 URL 交给 studio 的通用自动填充层处理，保持单一职责。
    if analysis.url and analysis.url_source == "explicit":
        for step in spec.get("steps") or []:
            if step.get("id") == "fetch_page":
                step.setdefault("with", {})["url"] = analysis.url
    return spec


def _build_web_browser_spec(analysis: IntentAnalysis) -> dict[str, Any]:
    """把 web_browser 动作组合成「打开 → 取正文/链接/截图」模板。"""
    from ..packs import web_browser as browser_pack

    actions = set(analysis.actions)
    if "links" in actions:
        spec = browser_pack.build_web_links()
    elif "screenshot" in actions:
        spec = browser_pack.build_web_screenshot_plan()
    elif "wait" in actions:
        spec = browser_pack.build_web_wait_text()
    else:
        spec = browser_pack.build_web_content()
    if analysis.url and analysis.url_source == "explicit":
        for step in spec.get("steps") or []:
            if step.get("use") in {"web_open", "web_screenshot", "web_wait_for_text"}:
                step.setdefault("with", {})["url"] = analysis.url
    return spec


_IOS_SAFARI_BUNDLE = "com.apple.mobilesafari"


def _ios_bundle_id(intent: str) -> str:
    return _IOS_SAFARI_BUNDLE if "safari" in intent.lower() else "${env.IOS_BUNDLE_ID}"


def _build_ios_spec(intent: str, analysis: IntentAnalysis) -> dict[str, Any]:
    """把 iOS 动作组合成 simctl 步骤；元素级交互仍由能力缺口拦截。"""
    actions = set(analysis.actions)
    udid = "${env.IOS_UDID}"
    steps: list[dict[str, Any]] = []
    if "list" in actions:
        steps.append({"id": "devices", "use": "ios_sim_list", "name": "列出 iOS 模拟器"})
    if actions & {"boot", "install", "launch", "openurl", "screenshot"}:
        steps.append({"id": "boot", "use": "ios_sim_boot", "name": "启动模拟器", "with": {"udid": udid}})
    if "install" in actions:
        steps.append(
            {
                "id": "install",
                "use": "ios_sim_install",
                "name": "安装 App",
                "with": {"udid": udid, "app_path": "${env.IOS_APP_PATH}"},
            }
        )
    if "launch" in actions:
        steps.append(
            {
                "id": "launch",
                "use": "ios_sim_launch",
                "name": "启动 App",
                "with": {"udid": udid, "bundle_id": _ios_bundle_id(intent)},
            }
        )
    if "openurl" in actions:
        steps.append(
            {
                "id": "openurl",
                "use": "ios_sim_openurl",
                "name": "打开网页/深链",
                "with": {"udid": udid, "url": analysis.url or "${env.PAGE_URL}"},
            }
        )
    if "terminate" in actions:
        steps.append(
            {
                "id": "terminate",
                "use": "ios_sim_terminate",
                "name": "结束 App",
                "with": {"udid": udid, "bundle_id": _ios_bundle_id(intent)},
            }
        )
    if "shutdown" in actions:
        steps.append({"id": "shutdown", "use": "ios_sim_shutdown", "name": "关闭模拟器", "with": {"udid": udid}})
    if "screenshot" in actions:
        steps.append(
            {
                "id": "shot",
                "use": "ios_sim_screenshot",
                "name": "模拟器截图",
                "with": {"udid": udid, "name": "ios-screenshot.png"},
            }
        )
    labels = {
        "list": "列出模拟器",
        "boot": "启动模拟器",
        "install": "安装 App",
        "launch": "启动 App",
        "openurl": "打开网页",
        "terminate": "结束 App",
        "shutdown": "关闭模拟器",
        "screenshot": "截图",
    }
    ordered = ["list", "boot", "install", "launch", "openurl", "terminate", "shutdown", "screenshot"]
    title = "iOS 模拟器：" + " → ".join(labels[action] for action in ordered if action in actions)
    return {
        "id": "ios_sim_actions",
        "name": title,
        "tags": ["ios", "device", "simulator"],
        "description": "通过 xcrun simctl 执行 iOS 模拟器动作组合",
        "steps": steps,
    }


def _build_rule_plan(
    registry: Any,
    *,
    intent: str,
    domain: str,
    analysis: IntentAnalysis,
    explicit_domain: bool,
    llm_ready: bool = True,
) -> TestPlan:
    from ..spec.loader import expand_case_spec

    templates = _templates_for(registry, domain)
    if not templates:
        code, missing, suggestions = _mapping_gap(analysis, suggestions=[f"安装/启用 {domain} 领域包"])
        raise PlanGateError(
            code,
            f"领域包 {domain} 没有可用的意图模板",
            details={"missing_atoms": missing, "suggestions": suggestions, "intent": intent},
        )

    if domain == "ios_sim" and analysis.actions:
        spec = expand_case_spec(_build_ios_spec(intent, analysis), registry=registry)
        from ..core.molecule import Molecule

        return TestPlan(
            cases=[Molecule.from_spec(spec, source="template")],
            source="template",
            planner="template",
            intent=intent,
            env={},
            notes=["iOS 模拟器模板：simctl 动作组合"],
            meta={"template": "ios_sim_actions", "llm_used": False},
        )

    if domain == "web_browser" and analysis.actions:
        spec = expand_case_spec(_build_web_browser_spec(analysis), registry=registry)
        from ..core.molecule import Molecule

        return TestPlan(
            cases=[Molecule.from_spec(spec, source="template")],
            source="template",
            planner="template",
            intent=intent,
            env={},
            notes=[f"浏览器模板：{spec.get('name') or 'web_browser'}"],
            meta={"template": spec.get("id"), "llm_used": False},
        )

    if domain == "web_research":
        specs = [
            expand_case_spec(
                _build_web_spec(intent, analysis, llm_ready=llm_ready), registry=registry
            )
        ]
        from ..core.molecule import Molecule

        plan = TestPlan(
            cases=[Molecule.from_spec(spec, source="template") for spec in specs],
            source="template",
            planner="template",
            intent=intent,
            env={},
            notes=[
                "网页研究模板：抓取 → 正文 → 状态码断言"
                + (" → 结构 → LLM 报告" if llm_ready else "（未配置 LLM，跳过总结）")
            ],
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
        # 领域 + 动作命中但模板未覆盖：给出候选原子，不产出无关模板
        code, missing, suggestions = _mapping_gap(
            analysis, suggestions=[f"为 {domain} 增加动作模板，或改用场景 YAML 精确描述"]
        )
        raise PlanGateError(
            code,
            "当前领域包没有覆盖该动作的意图模板",
            details={"missing_atoms": missing, "suggestions": suggestions, "intent": intent},
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
        code, missing, suggestions = _mapping_gap(
            analysis,
            suggestions=["补充领域包/原子，或把意图拆成更具体的子任务"],
        )
        raise PlanGateError(
            code,
            "当前原子库无法表达该意图：未同时命中领域与动作",
            details={"missing_atoms": missing, "suggestions": suggestions, "intent": intent},
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
    if analysis.missing_inputs:
        raise PlanGateError(
            "missing_inputs",
            "缺少输入：目标网页 URL",
            details={
                "missing_inputs": list(analysis.missing_inputs),
                "missing_atoms": [],
                "suggestions": list(analysis.suggestions),
                "autofilled": [],
                "param_gaps": [],
                "intent": intent,
            },
        )
    if analysis.domain == "web_research" and not analysis.actions:
        code, missing, suggestions = _mapping_gap(
            analysis,
            suggestions=["明确要抓取/提取正文/分析布局/生成报告，或换用匹配的领域包"],
        )
        raise PlanGateError(
            code,
            "意图没有命中网页研究动作，不能套用 web_research 模板",
            details={"missing_atoms": missing, "suggestions": suggestions, "intent": intent},
        )

    planner = "template" if not explicit else "rules"
    plan_source = "template" if not explicit else "rules"
    confidence = 0.45 if analysis.domain == "web_research" else 0.4
    llm_ready = bool(llm_client is not None and getattr(llm_client, "available", False))
    plan: TestPlan | None = None
    if allow_llm and llm_ready:
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
            llm_ready=llm_ready,
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
        code, missing, suggestions = _mapping_gap(analysis, suggestions=["检查 exclude_atoms 是否把唯一动作排除了"])
        raise PlanGateError(
            code,
            "意图没有生成任何可执行步骤（检查 exclude_atoms / 原子能力）",
            details={"missing_atoms": missing, "suggestions": suggestions, "intent": intent},
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
