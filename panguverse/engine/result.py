"""执行结果模型：步骤 / 用例 / 运行三层的结构化结果。

报告层、退出码、自愈层全部只依赖这些结构，因此这里的字段是稳定的对外契约。
"""

from __future__ import annotations

import time
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


def _brief_outputs(outputs: dict[str, Any], limit: int = 2000) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in outputs.items():
        text = repr(value)
        out[key] = value if len(text) <= limit else text[:limit] + "..."
    return out
