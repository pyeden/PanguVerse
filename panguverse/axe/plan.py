"""测试计划：规划层的输出契约（用例集合 + 来源 + 校验结果）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.molecule import Molecule
from ..core.registry import AtomRegistry


@dataclass
class PlanRequest:
    """一次规划请求：要么给自然语言意图，要么给场景规格，要么两者都给。"""

    intent: str = ""
    suite: Any = None  # spec.TestSuite
    env: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    case_ids: list[str] = field(default_factory=list)
    allow_llm: bool = True
    extra_notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.intent:
            return self.intent
        if self.suite is not None:
            return f"<规格 {getattr(self.suite, 'name', '?')}>"
        return "<空请求>"


@dataclass
class TestPlan:
    """可执行的测试计划。"""

    # 名字以 Test 开头只是「测试计划」的本意，告诉 pytest 不要当测试类收集
    __test__ = False

    cases: list[Molecule] = field(default_factory=list)
    source: str = "spec"
    intent: str = ""
    env: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    planner: str = ""

    # ------------------------------------------------------------------ #
    @property
    def size(self) -> int:
        return len(self.cases)

    @property
    def step_count(self) -> int:
        return sum(len(case.steps) for case in self.cases)

    def filtered(self, *, tags: list[str] | None = None, case_ids: list[str] | None = None) -> TestPlan:
        cases = self.cases
        if tags:
            wanted = {t.strip() for t in tags if t.strip()}
            cases = [c for c in cases if wanted & set(c.tags)]
        if case_ids:
            wanted_ids = {c.strip() for c in case_ids if c.strip()}
            cases = [c for c in cases if c.case_id in wanted_ids]
        return TestPlan(
            cases=list(cases),
            source=self.source,
            intent=self.intent,
            env=dict(self.env),
            notes=list(self.notes),
            warnings=list(self.warnings),
            meta=dict(self.meta),
            planner=self.planner,
        )

    def validate(self, registry: AtomRegistry, *, known_variables: Iterable[str] = ()) -> list[str]:
        """执行前校验：返回 **error 级**问题清单（保持「字符串列表」的对外契约）。

        真正的检查实现在 :mod:`panguverse.core.validation`（领域中立，可被 CLI/CI 单独使用）。
        """
        return self.check(registry, known_variables=known_variables).messages(level="error")

    def check(
        self,
        registry: AtomRegistry,
        *,
        known_variables: Iterable[str] = (),
        capabilities: Iterable[str] | None = None,
    ) -> Any:
        """完整校验（含 warning）：返回 :class:`~panguverse.core.validation.CheckResult`。"""
        from ..core.validation import check_plan

        return check_plan(
            self, registry, known_variables=known_variables, capabilities=capabilities
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "planner": self.planner,
            "intent": self.intent,
            "case_count": self.size,
            "step_count": self.step_count,
            "env": self.env,
            "notes": self.notes,
            "warnings": self.warnings,
            "meta": self.meta,
            "cases": [case.to_dict() for case in self.cases],
        }

    def describe(self) -> str:
        lines = [
            f"计划来源: {self.source}" + (f"（{self.planner}）" if self.planner else ""),
            f"用例数: {self.size}，步骤数: {self.step_count}",
        ]
        for case in self.cases:
            lines.append(f"  - {case.case_id}: {case.name}（{len(case.steps)} 步）")
        return "\n".join(lines)
