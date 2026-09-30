"""规划器：把「意图 / 规格」拆解成原子计划。

三种规划器，按可用性降级，保证闭环永远有路可走：

===============  ==========================================  ==========================
规划器            输入                                        适用
===============  ==========================================  ==========================
``SpecPlanner``  场景 YAML/JSON（精确、团队协作、进版本库）      回归、CI 主线
``LLMPlanner``   自然语言意图（AI 拆解 + 结构化 JSON 约束）      探索期、快速起草
``RuleBasedPlanner`` 自然语言意图（关键词 → 领域模板）         无密钥 / 无网络时的兜底
===============  ==========================================  ==========================

``HybridPlanner`` 负责选择与降级，并把「为什么降级」写进 ``plan.warnings``——
闭环里的每一步都要可解释。
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import PlanError
from ..core.molecule import Molecule
from ..core.registry import AtomRegistry
from .plan import PlanRequest, TestPlan

SYSTEM_PROMPT = """你是 PanguVerse 的「盘古斧」规划器，负责把自动化意图拆解成原子步骤序列。
你只能使用清单里给出的原子（atom），不允许发明新原子。
输出必须是 JSON 对象，结构如下：
{
  "cases": [
    {
      "id": "小写下划线标识",
      "name": "用例中文名",
      "description": "一句话说明",
      "tags": ["标签"],
      "secrets": ["需要打码的变量名"],
      "steps": [
        {
          "id": "step_id",
          "use": "原子名",
          "name": "步骤说明",
          "with": { "参数名": "值（可含 ${env.XXX} 或 ${step_id.输出字段} 模板）" },
          "capture": { "变量名": "步骤输出里的路径" },
          "when": "可选的模板条件",
          "foreach": "可选的模板，解析为列表",
          "retry": { "attempts": 3, "delay_ms": 100, "on": ["transient","timeout","network"] }
        }
      ]
    }
  ]
}
通用约束：
1. 只使用清单里存在的原子与它们的输入名；
2. 会产生断言语义的步骤，紧跟对应的断言原子；
3. 只输出 JSON，不要任何解释文字。
各领域的写法约束见下（来自对应的领域包）："""


def _domain_hints(registry: AtomRegistry) -> list[str]:
    """收集各领域包提供的规划提示片段（内核不硬编码任何领域惯例）。"""
    hints: list[str] = []
    for pack in registry.packs():
        semantics = getattr(pack, "semantics", None)
        provider = getattr(semantics, "planning_hints", None)
        if not callable(provider):
            continue
        for hint in provider():
            hints.append(f"[{pack.name}] {hint}")
    return hints


class SpecPlanner:
    """把场景规格（TestSuite）转成计划。"""

    name = "spec"

    def plan(self, request: PlanRequest) -> TestPlan:
        suite = request.suite
        if suite is None:
            raise PlanError("SpecPlanner 需要 scenario 规格（suite）")
        cases = [Molecule.from_spec(spec, source="spec") for spec in suite.case_specs()]
        plan = TestPlan(
            cases=cases,
            source="spec",
            planner=self.name,
            intent=request.intent or suite.name,
            env={**suite.env, **request.env},
            notes=list(getattr(suite, "notes", []) or []),
            meta={"scenario": getattr(suite, "path", None), "suite": suite.name},
        )
        return plan.filtered(tags=request.tags, case_ids=request.case_ids)


class RuleBasedPlanner:
    """关键词 → 领域模板。零依赖、确定性、可离线。"""

    name = "rules"

    def __init__(
        self,
        registry: AtomRegistry,
        *,
        max_cases: int = 3,
        templates: list[Any] | None = None,
    ) -> None:
        self.registry = registry
        self.max_cases = max_cases
        self._templates = templates

    # ------------------------------------------------------------------ #
    def templates(self) -> list[Any]:
        """收集所有领域包声明的意图模板（内核不内置任何领域模板）。"""
        if self._templates is not None:
            return list(self._templates)
        collected: list[Any] = []
        for pack in self.registry.packs():
            collected.extend(getattr(pack, "intent_templates", None) or [])
        if not collected:
            from ..packs.api_testing_templates import api_intent_templates

            collected = api_intent_templates()
        return collected

    def plan(self, request: PlanRequest) -> TestPlan:
        intent = request.intent or ""
        templates = self.templates()
        notes: list[str] = []
        scored = [(template.score(intent), template) for template in templates]
        hits = [(score, template) for score, template in scored if score > 0]
        if not intent:
            notes.append("意图为空 → 使用领域包提供的默认模板组")
            chosen = self._defaults(templates)
        elif not hits:
            notes.append("没有命中任何关键词 → 使用默认模板组；可改用场景 YAML 精确描述")
            chosen = self._defaults(templates)
        else:
            hits.sort(key=lambda item: (-item[0], item[1].name))
            chosen = [template for _score, template in hits[: self.max_cases]]
            notes.append(
                "命中模板: " + ", ".join(f"{t.name}({t.pack or t.domain})" for t in chosen)
            )
            if len(hits) > len(chosen):
                remaining = ", ".join(t.name for _s, t in hits[len(chosen) :])
                notes.append(f"其余候选未纳入（max_cases={self.max_cases}）: {remaining}")

        from ..spec.loader import expand_case_spec

        specs = [expand_case_spec(template.build(), registry=self.registry) for template in chosen]
        cases = [Molecule.from_spec(spec, source="rules") for spec in specs]
        warnings = []
        if not cases:
            warnings.append("规则模板没有产出任何用例")
        plan = TestPlan(
            cases=cases,
            source="rules",
            planner=self.name,
            intent=intent,
            env=dict(request.env),
            notes=[
                f"离线规则规划：{len(cases)} 个用例（未使用 LLM）",
                *notes,
                *request.extra_notes,
            ],
            warnings=warnings,
            meta={
                "templates": [template.name for template in chosen],
                "template_packs": sorted({template.pack or template.domain for template in chosen}),
                "llm_used": False,
            },
        )
        return plan.filtered(tags=request.tags, case_ids=request.case_ids)

    def _defaults(self, templates: list[Any]) -> list[Any]:
        marked = [template for template in templates if getattr(template, "default", False)]
        pool = marked or templates
        return pool[: self.max_cases]


class LLMPlanner:
    """LLM 规划：严格 JSON + 计划校验 + 一次自修复 + 降级。"""

    name = "llm"

    def __init__(
        self,
        registry: AtomRegistry,
        client: Any,
        *,
        fallback: Any = None,
        max_repair_rounds: int = 1,
    ) -> None:
        self.registry = registry
        self.client = client
        self.fallback = fallback
        self.max_repair_rounds = max_repair_rounds

    def plan(self, request: PlanRequest) -> TestPlan:
        if not getattr(self.client, "available", False):
            return self._degrade(request, "LLM 不可用（未配置密钥）")
        user_prompt = self._build_prompt(request)
        problems: list[str] = []
        for round_index in range(self.max_repair_rounds + 1):
            try:
                data = self.client.complete_json(SYSTEM_PROMPT, user_prompt)
            except Exception as exc:  # noqa: BLE001 - 任何 LLM 失败都要降级而不是中断
                return self._degrade(request, f"LLM 调用失败: {exc}")
            try:
                plan = self._parse(data, request)
            except PlanError as exc:
                problems = [str(exc)]
                user_prompt = self._build_prompt(request, repair=problems)
                continue
            problems = plan.validate(self.registry)
            if not problems:
                plan.notes.append(f"AI 规划成功（{'首轮' if round_index == 0 else f'第 {round_index + 1} 轮修正'}）")
                if round_index:
                    plan.notes.append(f"修正前的问题: {'; '.join(problems) if problems else '无'}")
                return plan
            user_prompt = self._build_prompt(request, repair=problems)
        return self._degrade(request, f"LLM 计划校验未通过: {'; '.join(problems[:3])}")

    # ------------------------------------------------------------------ #
    def _parse(self, data: dict[str, Any], request: PlanRequest) -> TestPlan:
        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise PlanError(f"LLM 返回的 cases 非法: {str(data)[:200]}")
        cases = [Molecule.from_spec(spec, source="llm") for spec in raw_cases]
        return TestPlan(
            cases=cases,
            source="llm",
            planner=self.name,
            intent=request.intent,
            env=dict(request.env),
            notes=[f"AI 规划：{len(cases)} 个用例（模型 {getattr(self.client, 'model', '?')}）"],
            meta={"llm_used": True, "model": getattr(self.client, "model", "")},
        )

    def _build_prompt(self, request: PlanRequest, *, repair: list[str] | None = None) -> str:
        catalog = json.dumps(self.registry.catalog(), ensure_ascii=False, indent=1)
        env_keys = ", ".join(sorted(request.env)) or "（未提供；模板里可用 ${env.NAME} 引用）"
        hints = _domain_hints(self.registry)
        parts = [
            f"自动化意图：{request.intent or '（未提供，按冒烟主线设计）'}",
            "环境变量（可在模板里引用 ${env.NAME}）：" + env_keys,
            "可用原子清单（JSON）：\n" + catalog,
        ]
        if hints:
            parts.append("领域写法约束：\n" + "\n".join(f"- {hint}" for hint in hints))
        if request.suite is not None:
            parts.append(f"已有场景参考：{getattr(request.suite, 'name', '')}")
        if repair:
            parts.append(
                "上一轮输出存在以下问题，请修正后重新输出完整 JSON：\n- " + "\n- ".join(repair)
            )
        return "\n\n".join(parts)

    def _degrade(self, request: PlanRequest, reason: str) -> TestPlan:
        if self.fallback is None:
            raise PlanError(f"LLM 规划失败且没有兜底规划器: {reason}")
        plan = self.fallback.plan(request)
        plan.warnings.append(f"已从 AI 规划降级为规则规划：{reason}")
        plan.notes.append(f"降级原因: {reason}")
        plan.meta["llm_used"] = False
        plan.meta["llm_error"] = reason
        return plan


class HybridPlanner:
    """按输入形态选择规划器：有规格用规格，有密钥用 AI，什么都没有用规则。"""

    name = "hybrid"

    def __init__(
        self,
        registry: AtomRegistry,
        *,
        client: Any = None,
        max_cases: int = 3,
        prefer_llm: bool = True,
    ) -> None:
        self.registry = registry
        self.client = client
        self.spec_planner = SpecPlanner()
        self.rule_planner = RuleBasedPlanner(registry, max_cases=max_cases)
        self.llm_planner = LLMPlanner(registry, client, fallback=self.rule_planner) if client else None
        self.prefer_llm = prefer_llm

    def plan(self, request: PlanRequest) -> TestPlan:
        if request.suite is not None:
            plan = self.spec_planner.plan(request)
            if plan.cases:
                if request.intent:
                    plan.notes.append(f"按场景规格执行；意图仅作备注：{request.intent}")
                return plan
            plan.warnings.append("场景规格里没有选中任何用例")

        use_llm = (
            bool(request.intent)
            and request.allow_llm
            and self.prefer_llm
            and self.llm_planner is not None
            and getattr(self.client, "available", False)
        )
        if use_llm:
            return self.llm_planner.plan(request)  # type: ignore[union-attr]

        plan = self.rule_planner.plan(request)
        if request.intent and not (self.client and getattr(self.client, "available", False)):
            plan.notes.append("未检测到 LLM 密钥 → 使用离线规则规划（配置 PANGUVERSE_API_KEY 可启用 AI 拆解）")
        return plan

    def status(self) -> dict[str, Any]:
        return {
            "planner": "hybrid",
            "llm": self.client.status() if self.client is not None else {"available": False},
            "fallback": "rules",
        }
