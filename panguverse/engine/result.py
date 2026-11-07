"""执行结果模型：步骤 / 用例 / 运行三层的结构化结果。

报告层、退出码、自愈层全部只依赖这些结构，因此这里的字段是稳定的对外契约。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"


@dataclass
class HealEvent:
    """一次自愈动作的记录（闭环的「自愈」环节必须可审计）。"""

    step_id: str
    kind: str
    action: str
    success: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "step_id": self.step_id,
            "kind": self.kind,
            "action": self.action,
            "success": self.success,
            "reason": self.reason,
        }
        if self.detail:
            data["detail"] = self.detail
        if self.suggestion:
            data["suggestion"] = self.suggestion
        return data


@dataclass
class StepResult:
    step_id: str
    atom: str
    name: str = ""
    status: str = PASSED
    attempts: int = 1
    retries: int = 0
    iterations: int = 1
    started_at: float = 0.0
    duration_ms: float = 0.0
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    message: str = ""
    #: 领域包声明的「作用对象标识」：HTTP 是接口路径、文件系统是文件路径、SQL 是表名…
    #: 经验库靠它把教训精确绑定到对象上
    target: str = ""
    http: list[dict[str, Any]] = field(default_factory=list)
    heal: list[HealEvent] = field(default_factory=list)
    skipped_reason: str = ""
    tags: list[str] = field(default_factory=list)
    #: 节点固化的产出物映射（``{key: {"path": ..., "type": ...}}``）。
    #: 只供运行后的收集器使用，不进入公开报告。
    produces: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step_id": self.step_id,
            "atom": self.atom,
            "status": self.status,
            "attempts": self.attempts,
            "retries": self.retries,
            "duration_ms": round(self.duration_ms, 2),
            "inputs": self.inputs,
            "outputs": _brief_outputs(self.outputs),
        }
        if self.name:
            data["name"] = self.name
        if self.iterations > 1:
            data["iterations"] = self.iterations
        if self.error:
            data["error"] = self.error
        if self.message:
            data["message"] = self.message
        if self.target:
            data["target"] = self.target
        if self.http:
            data["http"] = self.http
        if self.heal:
            data["heal"] = [h.to_dict() for h in self.heal]
        if self.skipped_reason:
            data["skipped_reason"] = self.skipped_reason
        if self.tags:
            data["tags"] = list(self.tags)
        return data


@dataclass
class CaseResult:
    case_id: str
    name: str
    status: str = PASSED
    steps: list[StepResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    tags: list[str] = field(default_factory=list)
    description: str = ""
    error: dict[str, Any] | None = None
    heal_events: list[HealEvent] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    source: str = "local"

    # ------------------------------------------------------------------ #
    @property
    def healed(self) -> bool:
        return bool(self.heal_events)

    @property
    def http_requests(self) -> int:
        return sum(len(step.http) for step in self.steps)

    def failing_steps(self) -> list[StepResult]:
        return [s for s in self.steps if s.status in (FAILED, ERROR)]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "case_id": self.case_id,
            "name": self.name,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "steps": [s.to_dict() for s in self.steps],
            "http_requests": self.http_requests,
        }
        if self.description:
            data["description"] = self.description
        if self.tags:
            data["tags"] = list(self.tags)
        if self.heal_events:
            data["heal_events"] = [h.to_dict() for h in self.heal_events]
        if self.variables:
            data["variables"] = self.variables
        if self.error:
            data["error"] = self.error
        if self.meta:
            data["meta"] = self.meta
        return data


@dataclass
class RunResult:
    run_id: str
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    phases: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    @property
    def summary(self) -> dict[str, Any]:
        counts = {PASSED: 0, FAILED: 0, ERROR: 0, SKIPPED: 0}
        steps = 0
        http_calls = 0
        healed = 0
        for case in self.cases:
            counts[case.status] = counts.get(case.status, 0) + 1
            steps += len(case.steps)
            http_calls += case.http_requests
            if case.healed:
                healed += 1
        return {
            "total": len(self.cases),
            "passed": counts[PASSED],
            "failed": counts[FAILED],
            "errored": counts[ERROR],
            "skipped": counts[SKIPPED],
            "healed_cases": healed,
            "steps": steps,
            "http_requests": http_calls,
            "duration_ms": round(self.duration_ms, 2),
            "success": counts[FAILED] == 0 and counts[ERROR] == 0,
        }

    @property
    def exit_code(self) -> int:
        summary = self.summary
        if summary["errored"]:
            return 2
        return 0 if summary["success"] else 1

    def add_phase(self, name: str, status: str, detail: str = "", **extra: Any) -> None:
        self.phases.append(
            {"name": name, "status": status, "detail": detail, "ts": time.time(), **extra}
        )

    def to_dict(self, *, mask: bool = True) -> dict[str, Any]:
        from ..core.redact import mask_obj

        secrets = _collect_secrets(self.cases)
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 2),
            "summary": self.summary,
            "plan": self.plan,
            "env": self.env,
            "cases": [c.to_dict() for c in self.cases],
            "phases": self.phases,
            "meta": self.meta,
            "artifacts": self.artifacts,
        }
        if self.notes:
            data["notes"] = self.notes
        return mask_obj(data, secrets) if mask else data


def _collect_secrets(cases: list[CaseResult]) -> list[str]:
    """从用例变量里找出被标记为密钥的明文值（用于按值打码）。"""
    values: list[str] = []
    for case in cases:
        meta_secrets = case.meta.get("secret_values") or []
        if isinstance(meta_secrets, list):
            for value in meta_secrets:
                if isinstance(value, str) and len(value) >= 4 and value not in values:
                    values.append(value)
    return values


#: 报告里单个输出值的字符预算；超限时压缩内容，但绝不改变容器类型。
REPORT_VALUE_LIMIT = 16000
#: 压缩时单个字符串的字符上限（保留可读前缀，便于人工排查）。
REPORT_TEXT_LIMIT = 240
_TRUNC_MARK = "…（已截断）"


def _clip_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(_TRUNC_MARK):
        return text[:limit]
    return text[: limit - len(_TRUNC_MARK)] + _TRUNC_MARK


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(repr(value))


def _compact(value: Any, text_limit: int) -> Any:
    """递归压缩字符串，保留 list/dict 结构与元素类型。"""
    if isinstance(value, str):
        return _clip_text(value, text_limit)
    if isinstance(value, Mapping):
        return {str(key): _compact(item, text_limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item, text_limit) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip_text(str(value), text_limit)


def _fit_sequence(items: list[Any], limit: int) -> list[Any]:
    """保留能塞进预算的最长前缀（至少 1 个元素）；返回的仍是 list。"""
    if not items:
        return []
    low, high, best = 1, len(items), 1
    while low <= high:
        middle = (low + high) // 2
        if _json_size(items[:middle]) <= limit:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return items[:best]


def brief_value(value: Any, limit: int = REPORT_VALUE_LIMIT) -> tuple[Any, bool]:
    """把输出值压到 ``limit`` 字符内，返回 ``(值, 是否截断)``。

    关键约束：**容器类型不变**。列表仍是 ``list``、对象仍是 ``dict``，
    不会像旧实现那样退化成 ``repr()`` 字符串，下游才能继续结构化消费。
    """
    if isinstance(value, str):
        clipped = _clip_text(value, limit)
        return clipped, clipped != value
    if not isinstance(value, (Mapping, list, tuple)):
        if _json_size(value) <= limit:
            return value, False
        return _clip_text(str(value), limit), True
    if _json_size(value) <= limit:
        return value, False

    compact = _compact(value, REPORT_TEXT_LIMIT)
    if _json_size(compact) <= limit:
        return compact, True

    if isinstance(compact, list):
        return _fit_sequence(compact, limit), True
    kept: dict[str, Any] = {}
    for key, item in compact.items():
        candidate = {**kept, key: item}
        if _json_size(candidate) > limit and kept:
            break
        kept[key] = item
    return kept, True


def brief_mapping(
    values: Mapping[str, Any] | None, limit: int = REPORT_VALUE_LIMIT
) -> dict[str, Any]:
    """报告层统一出口：逐字段限长，保留结构并显式标注截断。"""
    out: dict[str, Any] = {}
    for key, value in (values or {}).items():
        bounded, cut = brief_value(value, limit)
        out[key] = bounded
        if cut:
            out.setdefault(f"{key}_truncated", True)
            if isinstance(bounded, list):
                out.setdefault(f"{key}_total", len(value))
    return out


def _brief_outputs(outputs: dict[str, Any], limit: int = REPORT_VALUE_LIMIT) -> dict[str, Any]:
    return brief_mapping(outputs, limit)
