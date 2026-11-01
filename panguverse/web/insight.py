"""测试报告的「AI 总结」：LLM 可选 + 规则兜底。

契约：``tasks/web-ai-insight-spec.md`` v3.0。

三条硬约束：

1. **没有密钥是常态**：规则引擎必须能独立给出可用结论，不能是空壳；
2. **不编造**：规则里的用例名、报错、期望/实际全部来自 ``report.json``，
   ``case_ids`` 一律按报告里真实存在的用例过滤；
3. **LLM 只做增强**：输入是打码后的摘要（<=40KB），失败/超时一律降级为规则，
   绝不把 LLM 的统计数字当成真值（``stats`` 永远取自 ``report.summary``）。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..axe.llm import LLMError

#: 单次生成上限（秒）：超时按降级处理（契约 §2.2）
LLM_TIMEOUT = 25.0
#: LLM 输入体积上限（字节）
LLM_INPUT_LIMIT = 40 * 1024
#: 失败条目上限（防止报告异常膨胀）
LLM_MAX_FAILURES = 40

DEGRADED_NO_LLM = "未配置 LLM 密钥，使用离线规则总结"
DEGRADED_TIMEOUT = f"LLM 生成超时（>{LLM_TIMEOUT:.0f}s），已降级为离线规则总结"

KIND_LABELS = {
    "config": "沙箱文件缺失（检查 FS_ROOT / CREDS_FILE）",
    "auth": "认证或权限问题（检查凭据与登录步骤）",
    "engine": "响应结构与该用例预期不一致",
    "assertion": "断言与预期不符",
    "flaky": "执行抖动（重试后仍失败）",
    "mixed_target": "回归库混跑不同被测目标",
}
#: headline 里用的短标签（避免嵌套括号）
KIND_SHORT = {
    "config": "沙箱文件缺失",
    "auth": "认证/权限问题",
    "engine": "响应结构与预期不一致",
    "assertion": "断言不符",
    "flaky": "执行抖动",
}
_ALLOWED_FINDING_KINDS = {"auth", "config", "assertion", "engine", "flaky", "mixed_target"}

_FILE_MSG = "文件不存在"
_CAPTURE_RE = re.compile(r"capture\s*路径不存在|capture path|路径不存在")
_CONN_RE = re.compile(r"(?i)连接失败|连接被拒绝|超时|timed?\s*out|unreachable|refused|connection")
_STATUS_MSG_RE = re.compile(r"状态码期望\s*(\d{3})\s*[，,]\s*实际\s*(\d{3})")
_PRIORITIES = ("P0", "P1", "P2")

_SYSTEM_PROMPT = (
    "你是自动化测试报告的评审助手。根据给定的 JSON 摘要（已打码）总结这次测试，"
    "并给出开发可执行的修复建议。只依据摘要中的真实数据，不要编造用例名、数字或报错。"
    "严禁输出任何密钥、密码、token；不要复述环境变量的值。"
    "只输出一个 JSON 对象，字段为：headline（一句话结论）、summary_md（Markdown，"
    "包含 ## 结论 / ## 失败分布 / ## 建议 三节）、findings（[{kind,title,detail,case_ids}]，"
    "kind 取 auth|config|assertion|engine|flaky|mixed_target）、"
    "suggestions（[{priority,title,detail,case_ids}]，priority 取 P0|P1|P2，"
    "每条必须有可操作动作和真实 case_ids）。"
)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class _Failure:
    """一个失败用例的关键证据（全部来自 report.json）。"""

    case_id: str
    name: str
    status: str
    kind: str = "unknown"
    signature: str = ""
    step: str = ""
    message: str = ""
    expected: Any = None
    actual: Any = None
    status_code: int | None = None
    observed_status: int | None = None
    targets: list[str] = field(default_factory=list)
    atoms: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 报告取数（只读，不做任何推断性补全）
# --------------------------------------------------------------------------- #
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def stats_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """``stats`` 用 ``report.summary`` **全字段**覆盖（v3.0.1 §3.5.1，逐字相等）。"""
    return dict(_dict(report.get("summary")))


def _plan_source(report: dict[str, Any], plan: dict[str, Any] | None) -> str:
    meta = _dict(report.get("meta"))
    source = meta.get("plan_source") or _dict(plan).get("source") or _dict(report.get("plan")).get("source")
    return str(source or "unknown")


def _env_keys(report: dict[str, Any], plan: dict[str, Any] | None) -> list[str]:
    """只给键名，绝不给值（A43）。"""
    for candidate in (
        _dict(plan).get("env"),
        _dict(report.get("plan")).get("env"),
        report.get("env"),
    ):
        if isinstance(candidate, dict) and candidate:
            return sorted(str(key) for key in candidate)
    return []


def _secret_values(report: dict[str, Any]) -> list[str]:
    """收集需要按值打码的明文（env 敏感键 + 报告标记的密钥），只用于遮蔽输出。"""
    from .api import is_sensitive_env_key  # 延迟导入：api -> service -> insight

    values: list[str] = []
    for env in (report.get("env"), _dict(report.get("plan")).get("env")):
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if is_sensitive_env_key(str(key)) and isinstance(value, str) and len(value) >= 3:
                if value not in values:
                    values.append(value)
    for case in _list(report.get("cases")):
        for value in _list(_dict(_dict(case).get("meta")).get("secret_values")):
            if isinstance(value, str) and len(value) >= 4 and value not in values:
                values.append(value)
    return values


def _failing_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in _list(case.get("steps")) if _dict(step).get("status") in ("failed", "error")]


def _first_status_code(error: dict[str, Any], message: str) -> int | None:
    actual = error.get("actual")
    path = str(error.get("path") or "")
    if isinstance(actual, int) and "status" in path.lower():
        return actual
    match = _STATUS_MSG_RE.search(message)
    if match:
        return int(match.group(2))
    return None


def _observed_status(step: dict[str, Any]) -> int | None:
    outputs = _dict(step.get("outputs"))
    code = outputs.get("status_code")
    if isinstance(code, int):
        return code
    for entry in _list(step.get("http")):
        response = _dict(_dict(entry).get("response"))
        if isinstance(response.get("status_code"), int):
            return response["status_code"]
    return None


def _classify(
    *, error_kind: str, failure_kind: str, message: str, expected: Any, status_code: int | None
) -> tuple[str, str]:
    text = message or ""
    if failure_kind == "file_not_found" or _FILE_MSG in text:
        return "config", "file_not_found"
    if _CONN_RE.search(text):
        return "engine", "unreachable"
    if _CAPTURE_RE.search(text):
        return "engine", "capture"
    if status_code in (401, 403):
        return "auth", "auth_status"
    if error_kind == "assertion":
        if isinstance(expected, str) or "未包含" in text or "包含" in text:
            return "assertion", "body"
        return "assertion", "assertion"
    if failure_kind == "flaky" or error_kind == "flaky":
        return "flaky", "flaky"
    return "engine", "other"


def _target_keys(case: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for step in _list(case.get("steps")):
        values: list[str] = []
        target = _dict(step).get("target")
        if isinstance(target, str) and target:
            values.append(target)
        url = _dict(_dict(step).get("inputs")).get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            values.append(url)
        for value in values:
            key = _target_key(value)
            if key and key not in keys:
                keys.append(key)
    return keys


def _target_key(value: str) -> str:
    if value.startswith(("http://", "https://")):
        path = urlsplit(value).path
    elif value.startswith("/"):
        path = value
    else:
        return ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "/"
    prefix = "/" + parts[0]
    # /api/travel/health → /api/travel；/api/health → /api（避免把资源名当目标名）
    if prefix == "/api" and len(parts) >= 3:
        prefix = "/api/" + parts[1]
    return prefix


def collect_failures(report: dict[str, Any]) -> list[_Failure]:
    """把报告里每个失败/错误用例收敛成一条证据（不改写、不补全）。"""
    failures: list[_Failure] = []
    for raw_case in _list(report.get("cases")):
        case = _dict(raw_case)
        if case.get("status") not in ("failed", "error"):
            continue
        steps = _failing_steps(case)
        step = _dict(steps[0]) if steps else {}
        case_error = _dict(case.get("error"))
        step_error = _dict(step.get("error"))
        error = case_error or step_error
        message = str(error.get("message") or step.get("message") or "").strip()
        error_kind = str(error.get("kind") or step_error.get("kind") or "")
        failure_kind = str(error.get("failure_kind") or step_error.get("failure_kind") or "")
        expected = error.get("expected")
        actual = error.get("actual")
        status_code = _first_status_code(error, message)
        kind, signature = _classify(
            error_kind=error_kind,
            failure_kind=failure_kind,
            message=message,
            expected=expected,
            status_code=status_code,
        )
        failures.append(
            _Failure(
                case_id=str(case.get("case_id") or ""),
                name=str(case.get("name") or case.get("case_id") or ""),
                status=str(case.get("status") or ""),
                kind=kind,
                signature=signature,
                step=str(step.get("name") or step.get("step_id") or ""),
                message=message,
                expected=expected,
                actual=actual,
                status_code=status_code,
                observed_status=_observed_status(step) if step else None,
                targets=_target_keys(case),
                atoms=[
                    str(_dict(raw).get("atom") or "")
                    for raw in _list(case.get("steps"))
                    if _dict(raw).get("atom")
                ],
            )
        )
    return failures


# --------------------------------------------------------------------------- #
# 规则兜底
# --------------------------------------------------------------------------- #
def _short(text: Any, limit: int = 160) -> str:
    value = "" if text is None else str(text).replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _group(failures: list[_Failure]) -> dict[str, list[_Failure]]:
    groups: dict[str, list[_Failure]] = {}
    for failure in failures:
        groups.setdefault(failure.kind, []).append(failure)
    return groups


def _case_list(items: list[_Failure], limit: int = 10) -> str:
    ids = [item.case_id for item in items if item.case_id]
    head = "、".join(ids[:limit])
    return head + (f" 等 {len(ids)} 个" if len(ids) > limit else "")


def _missing_paths(items: list[_Failure], limit: int = 3) -> list[str]:
    paths: list[str] = []
    for item in items:
        match = re.search(r"文件不存在[:：]\s*(\S+)", item.message)
        if match and match.group(1) not in paths:
            paths.append(match.group(1))
    return paths[:limit]


def _headline(stats: dict[str, Any], groups: dict[str, list[_Failure]]) -> str:
    total = int(stats.get("total") or 0)
    if total == 0:
        return "本次运行没有用例，无法给出结论"
    parts = [f"{total} 个用例：{stats.get('passed', 0)} 通过 / {stats.get('failed', 0)} 失败 / "
             f"{stats.get('errored', 0)} 错误"]
    if stats.get("skipped"):
        parts.append(f"{stats['skipped']} 跳过")
    if not groups:
        return "".join(parts) + f"；全部通过，耗时 {stats.get('duration_ms', 0)} ms"
    top_kind = max(groups, key=lambda kind: len(groups[kind]))
    top = groups[top_kind]
    label = KIND_SHORT.get(top_kind, KIND_LABELS.get(top_kind, top_kind))
    return "".join(parts) + f"；首要原因：{label}（{len(top)} 个用例）"


def _finding_detail(kind: str, items: list[_Failure], *, with_count: bool = True) -> str:
    chunks = [f"{len(items)} 个用例：{_case_list(items)}"] if with_count else []
    message_shown = False
    if kind == "config":
        paths = _missing_paths(items)
        if paths:
            chunks.append("缺失文件：" + "、".join(paths))
            message_shown = True
    elif kind == "auth":
        codes = sorted({str(item.status_code) for item in items if item.status_code})
        if codes:
            chunks.append("实际返回状态码：" + "、".join(codes))
    elif kind == "engine" and items[0].signature == "capture":
        chunks.append("共同报错：" + _short(items[0].message))
        message_shown = True
        observed = sorted({str(item.observed_status) for item in items if item.observed_status})
        if observed:
            chunks.append("该步骤实际状态码：" + "、".join(observed))
    if not message_shown:
        chunks.append("首个报错：" + _short(items[0].message))
    return "；".join(chunks)


def _finding_buckets(kind: str, items: list[_Failure]) -> list[tuple[str, list[_Failure]]]:
    """同一 kind 下按 signature 拆开：``engine`` 要区分「目标不可达」与「响应结构不符」。"""
    if kind != "engine":
        return [("", items)]
    buckets: list[tuple[str, list[_Failure]]] = []
    for signature in ("unreachable", "capture", "other"):
        subset = [item for item in items if item.signature == signature]
        if subset:
            buckets.append((signature, subset))
    return buckets


def _finding_title(kind: str, signature: str) -> str:
    if kind == "engine" and signature == "unreachable":
        return "目标不可达（检查 BASE_URL 与网络）"
    return KIND_LABELS[kind]


def _findings(
    failures: list[_Failure], groups: dict[str, list[_Failure]], plan_source: str
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for kind in ("config", "auth", "engine", "assertion", "flaky"):
        items = groups.get(kind)
        if not items:
            continue
        for signature, subset in _finding_buckets(kind, items):
            findings.append(
                {
                    "kind": kind,
                    "title": _finding_title(kind, signature),
                    "detail": _finding_detail(kind, subset),
                    "case_ids": [item.case_id for item in subset],
                }
            )
    if plan_source == "memory" and failures:
        targets = sorted({key for item in failures for key in item.targets})
        findings.append(
            {
                "kind": "mixed_target",
                "title": KIND_LABELS["mixed_target"],
                "detail": (
                    f"plan.source=memory（回归库复用）且出现 {len(failures)} 个失败用例，"
                    f"失败用例跨越 {len(targets)} 个接口前缀：{'、'.join(targets)}"
                    if targets
                    else f"plan.source=memory（回归库复用）且出现 {len(failures)} 个失败用例，"
                    "回归用例由同一份 env 回放"
                ),
                "case_ids": [item.case_id for item in failures],
            }
        )
    return findings


def _suggestion(priority: str, title: str, detail: str, case_ids: list[str]) -> dict[str, Any]:
    return {"priority": priority, "title": title, "detail": detail, "case_ids": case_ids}


def _suggestions(
    failures: list[_Failure], groups: dict[str, list[_Failure]], plan_source: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    config = groups.get("config") or []
    auth = groups.get("auth") or []
    engine = groups.get("engine") or []
    capture = [item for item in engine if item.signature == "capture"]
    unreachable = [item for item in engine if item.signature == "unreachable"]
    assertion = groups.get("assertion") or []
    body = [item for item in assertion if item.signature == "body"]
    other_assertion = [item for item in assertion if item.signature != "body"]

    if config:
        paths = _missing_paths(config)
        action = f"补齐缺失文件（{('、'.join(paths))}）" if paths else "让 FS_ROOT / CREDS_FILE 指向真实文件"
        out.append(
            _suggestion(
                "P0",
                "先修配置类失败：FS_ROOT / CREDS_FILE 与用例目标不匹配",
                f"{len(config)} 个用例在读取沙箱文件时中止（{_case_list(config)}）。"
                f"{action}，或把 CREDS_FILE 指向真实路径后复跑；"
                "这些用例的后续断言尚未执行，先修配置再判断是否还有真实缺陷。",
                [item.case_id for item in config],
            )
        )
    if auth:
        out.append(
            _suggestion(
                "P0",
                "认证链路排查：统一登录步骤与凭据来源，消除 401",
                f"{len(auth)} 个用例实际返回 401/403（{_case_list(auth)}）。"
                "检查登录步骤是否在断言之前完成、token 是否写入请求头、凭据是否与目标环境匹配；"
                + ("若配置类失败未修，401 可能只是连带结果，先修上面那条再复跑。" if config else ""),
                [item.case_id for item in auth],
            )
        )
    if unreachable:
        out.append(
            _suggestion(
                "P0",
                "确认被测服务可达（BASE_URL / 网络）",
                f"{len(unreachable)} 个用例因连接失败或超时中止（{_case_list(unreachable)}）。"
                "先确认 BASE_URL 指向的服务已启动、端口可达，再复跑。",
                [item.case_id for item in unreachable],
            )
        )
    if capture:
        steps = sorted({item.step for item in capture if item.step})
        observed = sorted({str(item.observed_status) for item in capture if item.observed_status})
        out.append(
            _suggestion(
                "P1" if (config or auth or unreachable) else "P0",
                "核对接口响应结构：capture 路径与实际返回不一致",
                f"步骤 {('、'.join(steps) or '未知')} 的 capture 路径在响应里不存在"
                f"（{_case_list(capture)}）"
                + (f"；本次该步骤实际状态码 {('、'.join(observed))}" if observed else "")
                + "。按被测接口的真实响应调整 capture 路径，"
                "或把非必需字段改为可选捕获，避免 401/异常响应把用例直接打成 error。",
                [item.case_id for item in capture],
            )
        )
    if body:
        expected = _short(body[0].expected, 40)
        out.append(
            _suggestion(
                "P1",
                "修正正文/页面断言：期望内容与实际响应不一致",
                f"{len(body)} 个用例的正文断言未命中（{_case_list(body)}），"
                f"首个报错：{_short(body[0].message)}"
                + (f"（期望：{expected}）" if expected else "")
                + "。确认断言针对的是当前被测站点/版本，必要时改用稳定的语义锚点。",
                [item.case_id for item in body],
            )
        )
    if other_assertion:
        out.append(
            _suggestion(
                "P1",
                "对齐断言期望与接口实际契约",
                f"{len(other_assertion)} 个用例的断言期望与响应不符（{_case_list(other_assertion)}）。"
                f"首个报错：{_short(other_assertion[0].message)}。"
                "按接口真实契约修正期望值，或改断更具体的字段。",
                [item.case_id for item in other_assertion],
            )
        )
    if plan_source == "memory" and failures:
        targets = sorted({key for item in failures for key in item.targets})
        out.append(
            _suggestion(
                "P1",
                "按场景分组回归：不要用同一套 env 回放多目标用例",
                "本次走的是回归库复用（plan.source=memory），失败用例覆盖 "
                f"{len(targets)} 个接口前缀：{'、'.join(targets) or '未知'}。"
                "不同被测目标混在同一回归池里，任一目标的配置/登录方式变化都会污染其它用例；"
                "建议按场景拆分回归库并各自绑定 env 与登录步骤。",
                [item.case_id for item in failures],
            )
        )
    status_only = [item for item in failures if item.signature == "auth_status" and _status_only(item)]
    if status_only:
        out.append(
            _suggestion(
                "P2",
                "补强断言：仅有状态码断言的用例建议加字段断言",
                f"{len(status_only)} 个用例只断状态码（{_case_list(status_only)}），"
                "401 之外的语义回归无法被发现；建议补关键字段/schema 断言。",
                [item.case_id for item in status_only],
            )
        )
    if failures and not any(item["priority"] == "P0" for item in out):
        # 契约 A41：只要有失败，至少一条 P0（只有断言类失败时把最阻断的一条提为 P0）
        out[0]["priority"] = "P0"
    if not out:
        out.append(
            _suggestion(
                "P2",
                "本次全部通过，可固化为回归基线",
                "没有失败用例需要修复；建议把本次运行保存为回归基线（save_as_task），"
                "后续改动用它做回归对比。",
                [],
            )
        )
    return out


def _status_only(failure: _Failure) -> bool:
    """用例是否**只有**状态码断言（按报告里真实存在的步骤原子判断，不猜）。"""
    asserts = [atom for atom in failure.atoms if atom.startswith("assert_")]
    return bool(asserts) and all(atom == "assert_status" for atom in asserts)


def _summary_md(
    report: dict[str, Any],
    stats: dict[str, Any],
    groups: dict[str, list[_Failure]],
    suggestions: list[dict[str, Any]],
) -> str:
    conclusion = _dict(report.get("conclusion"))
    success = bool(_dict(report.get("summary")).get("success"))
    verdict = "通过" if success else "未通过"
    if not success:
        detail = str(conclusion.get("verdict") or "")
        verdict += f"（{detail}）" if detail else ""
    lines = [
        "## 结论",
        (
            f"{stats.get('total', 0)} 个用例，通过 {stats.get('passed', 0)}、失败 {stats.get('failed', 0)}、"
            f"错误 {stats.get('errored', 0)}、跳过 {stats.get('skipped', 0)}；"
            f"本次**{verdict}**；耗时 {stats.get('duration_ms', 0)} ms；"
            f"自愈 {stats.get('healed_cases', 0)} 个用例。"
        ),
        "",
        "## 失败分布",
    ]
    if not groups:
        lines.append("- 无失败用例。")
    for kind in ("config", "auth", "engine", "assertion", "flaky"):
        items = groups.get(kind)
        if not items:
            continue
        for signature, subset in _finding_buckets(kind, items):
            lines.append(
                f"- **{_finding_title(kind, signature)}**：{len(subset)} 个用例"
                f"（{_case_list(subset, limit=8)}）"
            )
            lines.append(f"  - {_finding_detail(kind, subset, with_count=False)}")
    lines.append("")
    lines.append("## 建议")
    for index, suggestion in enumerate(suggestions, start=1):
        lines.append(
            f"{index}. **{suggestion['priority']} {suggestion['title']}**：{suggestion['detail']}"
        )
    return "\n".join(lines)


def rules_payload(
    report: dict[str, Any], plan: dict[str, Any] | None, *, run_id: str, generated_at: float | None = None
) -> dict[str, Any]:
    """纯规则总结：只依赖磁盘报告，没有 LLM 也能直接用。"""
    stats = stats_from_report(report)
    plan_source = _plan_source(report, plan)
    failures = collect_failures(report)
    groups = _group(failures)
    suggestions = _suggestions(failures, groups, plan_source)
    return {
        "available": True,
        "run_id": run_id,
        "generated_at": time.time() if generated_at is None else generated_at,
        "source": "rules",
        "model": None,
        "degraded_reason": DEGRADED_NO_LLM,
        "headline": _headline(stats, groups),
        "summary_md": _summary_md(report, stats, groups, suggestions),
        "findings": _findings(failures, groups, plan_source),
        "suggestions": suggestions,
        "stats": stats,
        "corrections": [],
    }


# --------------------------------------------------------------------------- #
# LLM 路径
# --------------------------------------------------------------------------- #
def _digest(
    report: dict[str, Any], plan: dict[str, Any] | None, failures: list[_Failure]
) -> dict[str, Any]:
    """LLM 输入：只放打码后的摘要 + 键名，体积 <= 40KB。"""
    summary = _dict(report.get("summary"))
    digest: dict[str, Any] = {
        "summary": dict(summary),
        "plan_source": _plan_source(report, plan),
        "env_keys": _env_keys(report, plan)[:200],
        "targets": sorted({key for item in failures for key in item.targets}),
        "failures": [
            {
                "case": item.name,
                "case_id": item.case_id,
                "step": item.step,
                "failure_kind": item.signature,
                "message": _short(item.message, 400),
                "expected": _short(item.expected, 200),
                "actual": _short(item.actual, 200),
            }
            for item in failures[:LLM_MAX_FAILURES]
        ],
    }
    from ..core.redact import mask_obj  # 复用既有打码规则

    digest = mask_obj(digest, _secret_values(report))
    while digest["failures"] and _size(digest) > LLM_INPUT_LIMIT:
        digest["failures"].pop()
    return digest


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _with_deadline(fn: Any, timeout: float) -> Any:
    """给可能卡住网络调用的 client 一个硬墙钟上限（契约 §2.2 的 25s）。"""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - 异常原样带回主线程再判定
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise LLMError(DEGRADED_TIMEOUT)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _digit_free(text: str) -> bool:
    """判断一段措辞里有没有数字（有数字的模型文本一律不采信）。"""
    return not any(char.isdigit() for char in text)


def _correct_findings(
    model_findings: Any,
    rules_findings: list[dict[str, Any]],
    case_kinds: dict[str, str],
) -> tuple[list[dict[str, Any]], int, int]:
    """按报告重建 findings：kind/case_ids/detail 全部以规则为准（§3.5.2）。

    模型只可能在「单一 kind + 无数字」时贡献标题措辞；错归类的 case_id 一律剔除，
    报告有而模型漏掉的类由规则补齐。
    """
    model_by_kind: dict[str, dict[str, Any]] = {}
    dropped = 0
    for entry in _list(model_findings)[:12]:
        entry = _dict(entry)
        kind = str(entry.get("kind") or "").strip().lower()
        raw_ids = [case_id for case_id in _list(entry.get("case_ids")) if isinstance(case_id, str)]
        if kind == "mixed_target":
            valid = [case_id for case_id in raw_ids if case_id in case_kinds]
        elif kind in _ALLOWED_FINDING_KINDS:
            valid = [case_id for case_id in raw_ids if case_kinds.get(case_id) == kind]
        else:
            dropped += len(raw_ids)
            continue
        dropped += len(raw_ids) - len(valid)
        if not valid:
            continue
        bucket = model_by_kind.setdefault(kind, {"case_ids": [], "title": ""})
        for case_id in valid:
            if case_id not in bucket["case_ids"]:
                bucket["case_ids"].append(case_id)
        title = entry.get("title")
        if not bucket["title"] and isinstance(title, str) and title.strip():
            bucket["title"] = title.strip()

    kind_counts = Counter(rule["kind"] for rule in rules_findings)
    corrected: list[dict[str, Any]] = []
    filled = 0
    for rule in rules_findings:
        kind = rule["kind"]
        model = model_by_kind.get(kind)
        title = rule["title"]
        if model and kind_counts[kind] == 1 and _digit_free(model["title"]):
            title = model["title"]
        if model is None:
            filled += 1
        corrected.append(
            {
                "kind": kind,
                "title": title,
                "detail": rule["detail"],
                "case_ids": list(rule["case_ids"]),
            }
        )
    return corrected, dropped, filled


def _correct_suggestions(
    model_suggestions: Any,
    rules_suggestions: list[dict[str, Any]],
    failures: list[_Failure],
    valid_ids: set[str],
) -> tuple[list[dict[str, Any]], int, str]:
    """建议可保留模型措辞，但 case_ids 必须在报告里；有失败时强制至少一条 P0。"""
    corrected: list[dict[str, Any]] = []
    dropped = 0
    for entry in _list(model_suggestions)[:12]:
        entry = _dict(entry)
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        raw_ids = [case_id for case_id in _list(entry.get("case_ids")) if isinstance(case_id, str)]
        case_ids = [case_id for case_id in raw_ids if case_id in valid_ids]
        dropped += len(raw_ids) - len(case_ids)
        priority = str(entry.get("priority") or "").strip().upper()
        detail = entry.get("detail")
        corrected.append(
            {
                "priority": priority if priority in _PRIORITIES else "P1",
                "title": title.strip(),
                "detail": detail.strip() if isinstance(detail, str) else "",
                "case_ids": case_ids,
            }
        )
    corrected.sort(key=lambda item: _PRIORITIES.index(item["priority"]))

    injected = ""
    rule_p0 = next(
        (item for item in rules_suggestions if item["priority"] == "P0" and item["case_ids"]), None
    )
    model_p0_ids = {
        case_id
        for item in corrected
        if item["priority"] == "P0"
        for case_id in item["case_ids"]
    }
    if failures and rule_p0 is not None and not (set(rule_p0["case_ids"]) & model_p0_ids):
        corrected.insert(0, dict(rule_p0))
        injected = rule_p0["title"]
    if failures and not any(item["priority"] == "P0" and item["case_ids"] for item in corrected):
        if corrected:
            corrected[0]["priority"] = "P0"
            injected = corrected[0]["title"]
    if not corrected and rules_suggestions:
        corrected = [dict(item) for item in rules_suggestions]
        injected = "全部建议（模型未给出可用建议）"
    return corrected, dropped, injected


def _correct_headline(model_headline: str, rules_headline: str, has_failures: bool) -> tuple[str, bool]:
    """标题可保留模型措辞，但不得出现数字或与结果矛盾的「全部通过」。"""
    text = model_headline.strip()
    contradictory = has_failures and any(word in text for word in ("全部通过", "全部成功", "均通过"))
    if not _digit_free(text) or contradictory:
        return rules_headline, True
    return text, False


def _llm_payload(
    client: Any,
    report: dict[str, Any],
    plan: dict[str, Any] | None,
    rules: dict[str, Any],
    failures: list[_Failure],
    *,
    timeout: float,
) -> dict[str, Any]:
    digest = _digest(report, plan, failures)
    payload_text = json.dumps(digest, ensure_ascii=False, default=str)
    if len(payload_text.encode("utf-8")) > LLM_INPUT_LIMIT:  # 防御：截断失败
        raise LLMError("摘要超过 40KB，已放弃 LLM 路径")
    data = _with_deadline(lambda: client.complete_json(_SYSTEM_PROMPT, payload_text), timeout)
    if not isinstance(data, dict):
        raise LLMError("LLM 未返回 JSON 对象")
    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        raise LLMError("LLM 输出缺少 headline")
    if not isinstance(data.get("summary_md"), str) or not data["summary_md"].strip():
        raise LLMError("LLM 输出缺少 summary_md")
    if not isinstance(data.get("findings"), list) or not isinstance(data.get("suggestions"), list):
        raise LLMError("LLM 输出缺少 findings/suggestions 列表")

    valid_ids = {str(_dict(case).get("case_id")) for case in _list(report.get("cases"))}
    valid_ids.discard("None")
    case_kinds = {failure.case_id: failure.kind for failure in failures}
    findings, dropped_findings, filled = _correct_findings(
        data.get("findings"), rules["findings"], case_kinds
    )
    suggestions, dropped_suggestions, injected = _correct_suggestions(
        data.get("suggestions"), rules["suggestions"], failures, valid_ids
    )
    stats = stats_from_report(report)
    groups = _group(failures)
    corrected_headline, headline_replaced = _correct_headline(
        headline, rules["headline"], bool(failures)
    )
    summary_md = _summary_md(report, stats, groups, suggestions)

    corrections: list[dict[str, Any]] = [
        {"field": "stats", "reason": "以 report.summary 全字段覆盖，不采信模型计数"},
        {
            "field": "findings",
            "reason": (
                f"kind/case_ids 按报告重算：剔除 {dropped_findings} 个错归类或不存在的 case_id，"
                f"补齐 {filled} 个模型缺失的类"
            ),
        },
        {"field": "summary_md", "reason": "计数句与用例清单按校正后的 findings/suggestions 重新渲染"},
    ]
    if headline_replaced:
        corrections.append(
            {"field": "headline", "reason": "模型标题含数字或与结果矛盾，改用规则结论句"}
        )
    if injected:
        corrections.append(
            {"field": "suggestions", "reason": f"模型未给出覆盖配置/环境失败的有效 P0，已注入规则建议：{injected}"}
        )
    if dropped_suggestions:
        corrections.append(
            {"field": "suggestions.case_ids", "reason": f"剔除 {dropped_suggestions} 个报告中不存在的 case_id"}
        )
    return {
        **rules,
        "source": "llm",
        "model": str(getattr(client, "model", "") or "") or None,
        "degraded_reason": "",
        "headline": corrected_headline,
        "summary_md": summary_md,
        "findings": findings,
        "suggestions": suggestions,
        "stats": stats,
        "corrections": corrections,
    }


def _mask_payload(payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """出口统一打码：规则里可能带 env 明文（旧报告），LLM 也可能复述敏感值。"""
    from ..core.redact import mask_obj
    from .api import mask_env_payload

    return mask_env_payload(mask_obj(payload, _secret_values(report)))


def build_payload(
    report: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    run_id: str,
    llm_client: Any = None,
    generated_at: float | None = None,
    timeout: float = LLM_TIMEOUT,
) -> dict[str, Any]:
    """生成 §2 的响应体：优先 LLM，失败/缺失一律降级为规则（不抛异常）。"""
    rules = rules_payload(report, plan, run_id=run_id, generated_at=generated_at)
    failures = collect_failures(report)
    if llm_client is None or not getattr(llm_client, "available", False):
        return _mask_payload(rules, report)
    try:
        payload = _llm_payload(llm_client, report, plan, rules, failures, timeout=timeout)
    except LLMError as exc:
        reason = str(exc)
        degraded = DEGRADED_TIMEOUT if reason == DEGRADED_TIMEOUT else f"LLM 生成失败，已降级为离线规则总结：{reason}"
        rules["degraded_reason"] = degraded
        return _mask_payload(rules, report)
    except Exception as exc:  # noqa: BLE001 - 契约要求 LLM 任何异常都降级，不得 500
        rules["degraded_reason"] = f"LLM 生成失败，已降级为离线规则总结：{type(exc).__name__}: {exc}"
        return _mask_payload(rules, report)
    return _mask_payload(payload, report)


__all__ = [
    "DEGRADED_NO_LLM",
    "DEGRADED_TIMEOUT",
    "LLM_INPUT_LIMIT",
    "LLM_TIMEOUT",
    "build_payload",
    "collect_failures",
    "rules_payload",
    "stats_from_report",
]
