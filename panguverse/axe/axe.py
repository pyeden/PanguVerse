"""🪓 盘古斧：把「意图 → 计划 → 执行 → 断言 → 报告 → 自愈 → 沉淀 → 复用」串成一个闭环。

这一层是 README 里 ``PanguAxe`` 的落地实现，也是本项目的第一条业务闭环（API 自动化测试）。

```python
from panguverse import PanguAxe

axe = PanguAxe()
result = axe.loop(PlanRequest(intent="测试登录下单支付闭环", env={"BASE_URL": "http://127.0.0.1:8899"}))
print(result.text())
```
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.atom import Atom
from ..core.errors import PlanError, SpecError
from ..core.molecule import Molecule, Step
from ..core.registry import AtomRegistry, default_registry
from ..engine.executor import Executor, ExecutorSettings
from ..engine.result import RunResult
from ..http.client import backend_name as http_backend_name
from ..report import DEFAULT_FORMATS, write_all
from .healer import Healer
from .llm import LLMClient
from .memory import Evolution, LessonStore, SealedStore, Workspace, step_target
from .plan import PlanRequest, TestPlan
from .planner import HybridPlanner


@dataclass
class LoopResult:
    """一次闭环的完整结果。"""

    run: RunResult
    plan: TestPlan
    evolve: dict[str, Any] = field(default_factory=dict)
    regression: RunResult | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    phases: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, Any]:
        return self.run.summary

    @property
    def exit_code(self) -> int:
        codes = [self.run.exit_code]
        if self.regression is not None:
            codes.append(self.regression.exit_code)
        return max(codes)

    def text(self) -> str:
        s = self.summary
        lines = [
            "🪓 PanguVerse 闭环完成",
            f"  运行: {self.run.run_id}  计划来源: {self.plan.source}"
            f"{'（' + self.plan.planner + '）' if self.plan.planner else ''}",
            f"  结果: {s['passed']}/{s['total']} 通过，失败 {s['failed']}，异常 {s['errored']}，"
            f"跳过 {s['skipped']}，自愈 {s['healed_cases']}，HTTP {s['http_requests']} 次，"
            f"耗时 {s['duration_ms'] / 1000:.2f}s",
        ]
        if self.evolve:
            lines.append(
                f"  沉淀: 回归原子 +{self.evolve.get('sealed_count', 0)}，"
                f"经验 +{self.evolve.get('lesson_count', 0)}，回归库共 {self.evolve.get('regression_size', 0)} 条"
            )
        if self.regression is not None:
            rs = self.regression.summary
            lines.append(f"  回归复用: {rs['passed']}/{rs['total']} 通过（无需重新规划）")
        for name, path in self.artifacts.items():
            lines.append(f"  报告[{name}]: {path}")
        return "\n".join(lines)


class PanguAxe:
    """编排大脑：规划 + 执行 + 自愈 + 沉淀。"""

    def __init__(
        self,
        *,
        registry: AtomRegistry | None = None,
        workspace: Workspace | str | None = None,
        settings: ExecutorSettings | None = None,
        llm: Any = None,
        client: Any = None,
        planner: Any = None,
        healer: Healer | None = None,
        enable_llm: bool = True,
        enable_heal: bool = True,
        max_cases: int = 3,
    ) -> None:
        self.registry = registry or default_registry()
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace or ".panguverse")
        self.workspace.ensure()
        self.settings = settings or ExecutorSettings()

        if llm is not None or client is not None:
            self.llm = llm or client
        else:
            self.llm = LLMClient() if enable_llm else None

        self.evolution = Evolution(self.workspace)
        self.sealed: SealedStore = self.evolution.sealed
        self.lessons: LessonStore = self.evolution.lessons
        self.healer = healer or (Healer(lessons=self.lessons, llm=self.llm) if enable_heal else None)
        self.executor = Executor(
            self.registry, settings=self.settings, healer=self.healer, workspace=self.workspace
        )
        self.planner = planner or HybridPlanner(self.registry, client=self.llm, max_cases=max_cases)
        self.last_plan: TestPlan | None = None

    # ------------------------------------------------------------------ #
    # 规划
    # ------------------------------------------------------------------ #
    def plan(self, request: PlanRequest | str | None = None, **kwargs: Any) -> TestPlan:
        if isinstance(request, str) or request is None:
            request = PlanRequest(intent=request or "", **kwargs)
        elif kwargs:
            request = PlanRequest(
                intent=request.intent,
                suite=request.suite,
                env={**request.env, **kwargs.pop("env", {})},
                tags=kwargs.pop("tags", request.tags),
                case_ids=kwargs.pop("case_ids", request.case_ids),
                allow_llm=kwargs.pop("allow_llm", request.allow_llm),
            )
        plan = self.planner.plan(request)
        apply_lessons_to_plan(plan, self.lessons)
        self.last_plan = plan
        return plan

    def validate(self, plan: TestPlan) -> list[str]:
        problems = plan.validate(self.registry)
        if problems:
            raise PlanError("计划校验失败：\n- " + "\n- ".join(problems))
        return problems

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def run(self, plan: TestPlan, *, run_id: str | None = None, **kwargs: Any) -> RunResult:
        self.validate(plan)
        return self.executor.run(plan, run_id=run_id or uuid.uuid4().hex[:12], **kwargs)

    def regression_plan(self, *, env: dict[str, Any] | None = None, tag: str = "regression") -> TestPlan:
        molecules = self.evolution.sealed.to_molecules(tag=tag)
        return TestPlan(
            cases=molecules,
            source="memory",
            planner="memory",
            intent="回归复用：直接执行已沉淀的用例（无需重新规划）",
            env=dict(env or {}),
            notes=[f"从回归库加载 {len(molecules)} 个用例（{self.workspace.registry_path}）"],
        )

    # ------------------------------------------------------------------ #
    # 闭环
    # ------------------------------------------------------------------ #
    def loop(
        self,
        request: PlanRequest | str | None = None,
        *,
        out_dir: str | Path | None = None,
        formats: Sequence[str] = DEFAULT_FORMATS,
        evolve: bool = True,
        seal: bool = True,
        regression: bool = False,
        run_id: str | None = None,
        title: str = "PanguVerse API 自动化测试报告",
        env: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LoopResult:
        """跑完整闭环。``regression=True`` 时会在沉淀后再跑一次回归，证明用例可复用。"""
        started = time.time()
        run_id = run_id or uuid.uuid4().hex[:12]
        if isinstance(request, str) or request is None:
            request = PlanRequest(intent=request or "", env=dict(env or {}), **kwargs)
        elif env:
            request = PlanRequest(
                intent=request.intent,
                suite=request.suite,
                env={**request.env, **env},
                tags=request.tags,
                case_ids=request.case_ids,
                allow_llm=request.allow_llm,
                extra_notes=request.extra_notes,
            )

        # 1) 规划
        plan = self.plan(request)
        # 2) 校验
        self.validate(plan)
        # 场景 env 与命令行动态覆盖合并后，作为本次闭环（含回归复用）的统一环境
        effective_env = {**plan.env, **(env or {})}

        # 3) 执行（含步骤级重试 + 自愈器）
        run = self.executor.run(
            plan,
            env=effective_env,
            run_id=run_id,
            meta={
                "intent": plan.intent,
                "planner": plan.planner,
                "plan_source": plan.source,
                "llm_used": bool(plan.meta.get("llm_used")),
                "heal_enabled": bool(self.healer),
            },
        )
        run.add_phase(
            "heal",
            "ok" if not any(case.heal_events for case in run.cases) else "healed",
            _heal_detail(run),
        )

        # 4) 报告
        target_dir = Path(out_dir) if out_dir else self.workspace.run_dir(run_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        plan_path = target_dir / "plan.json"
        import json as _json

        plan_path.write_text(_json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = write_all(run, target_dir, formats=formats, title=title, suite_name=f"PanguVerse.{plan.source}")
        artifacts["plan"] = str(plan_path)
        run.add_phase("report", "ok", f"产物目录 {target_dir}")

        # 5) 沉淀（进化）
        evolve_summary: dict[str, Any] = {}
        if evolve:
            evolve_summary = self.evolution.evolve(run, seal=seal)
            run.add_phase(
                "evolve",
                "ok",
                f"回归原子 +{evolve_summary.get('sealed_count', 0)}，"
                f"经验 +{evolve_summary.get('lesson_count', 0)}，"
                f"回归库共 {evolve_summary.get('regression_size', 0)} 条",
            )

        # 6) 回归复用（证明沉淀的用例可以脱离规划器直接跑）
        regression_run: RunResult | None = None
        if regression:
            rplan = self.regression_plan(env=effective_env)
            if rplan.cases:
                regression_run = self.executor.run(
                    rplan, env=effective_env, run_id=f"{run_id}-reg", meta={"regression": True}
                )
                self.evolution.sealed.mark_run([case.case_id for case in rplan.cases])
                reg_dir = target_dir / "regression"
                reg_artifacts = write_all(
                    regression_run,
                    reg_dir,
                    formats=formats,
                    title="PanguVerse 回归复用报告",
                    suite_name="PanguVerse.regression",
                )
                artifacts.update({f"regression_{k}": v for k, v in reg_artifacts.items()})
                run.add_phase(
                    "regression",
                    "ok" if regression_run.summary["success"] else "failed",
                    f"回归复用 {regression_run.summary['passed']}/{regression_run.summary['total']} 通过",
                )
            else:
                run.add_phase("regression", "skipped", "回归库为空，先跑一次成功的闭环再试")

        result = LoopResult(
            run=run,
            plan=plan,
            evolve=evolve_summary,
            regression=regression_run,
            artifacts=artifacts,
            phases=list(run.phases),
            timings={"total_ms": (time.time() - started) * 1000},
        )
        return result

    # ------------------------------------------------------------------ #
    # 组合：分子即原子（递归）
    # ------------------------------------------------------------------ #
    def compose(
        self,
        name: str,
        *,
        steps: Iterable[Any] | None = None,
        atoms: Iterable[Any] | None = None,
        description: str = "",
        tags: Iterable[str] = (),
        meta: dict[str, Any] | None = None,
        seal: bool = True,
    ) -> Molecule:
        """把若干原子/分子组合成新的分子；``seal=True`` 时直接注册为新原子。"""
        built: list[Step] = []
        for item in steps or []:
            built.append(_to_step(item))
        for item in atoms or []:
            if isinstance(item, Molecule):
                atom = self.executor.seal_molecule(item)
                built.append(Step(id=item.case_id, use=atom.name, name=item.name))
            elif isinstance(item, Atom):
                built.append(Step(id=item.name, use=item.name, name=item.description))
            else:
                raise SpecError(f"compose 不支持的元素类型: {type(item).__name__}")
        molecule = Molecule(
            name=name,
            case_id=_slug(name),
            description=description,
            steps=built,
            tags=tuple(tags),
            meta=dict(meta or {}),
            source="composed",
            owner=self,
        )
        if seal:
            self.executor.seal_molecule(molecule)
        return molecule

    def seal_molecule(self, molecule: Molecule) -> Atom:
        return self.executor.seal_molecule(molecule)

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "atoms": len(self.registry),
            "planner": self.planner.status() if hasattr(self.planner, "status") else {"planner": "custom"},
            "workspace": str(self.workspace.root),
            "sealed": len(self.sealed.list()),
            "lessons": self.lessons.count(),
            "healer": self.healer.summary() if self.healer else None,
            "http_backend": http_backend_name(),
        }


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def apply_lessons_to_plan(plan: TestPlan, lessons: LessonStore) -> list[str]:
    """把历史经验回灌到计划里：没写 retry 的步骤，按经验补上已验证的重试策略。

    经验按 **原子 + 接口标识** 精确匹配（见 :func:`~panguverse.axe.memory.request_target`），
    避免把一个接口的经验套到另一个接口上。
    """
    applied: list[str] = []
    for case in plan.cases:
        for step in case.steps:
            if step.retry is not None:
                continue
            target = step_target(step)
            lesson = None
            for kind in ("server_error", "rate_limit", "timeout", "network"):
                lesson = lessons.recall(kind=kind, atom=step.use, target=target)
                if lesson and lesson.fix.get("retry"):
                    break
                lesson = None
            if not lesson:
                continue
            from ..core.molecule import RetryPolicy

            step.retry = RetryPolicy.from_spec(lesson.fix["retry"])
            applied.append(
                f"[{case.case_id}.{step.id}] 依据历史经验（{lesson.kind} @ {target or '通用'}）自动补上重试策略"
            )
    if applied:
        plan.notes.extend(applied)
        plan.meta["lessons_applied"] = applied
    return applied


def _to_step(item: Any) -> Step:
    if isinstance(item, Step):
        return item
    if isinstance(item, Molecule):
        raise SpecError("Molecule 请放在 atoms= 里（会先 seal 成原子），或用 steps= 传步骤")
    if isinstance(item, dict):
        return Step.from_spec(item)
    raise SpecError(f"无法把 {type(item).__name__} 转成步骤")


def _heal_detail(run: RunResult) -> str:
    events = [event for case in run.cases for event in case.heal_events]
    retries = sum(step.retries for case in run.cases for step in case.steps)
    if not events and not retries:
        return "本次运行没有触发自愈"
    parts = []
    if retries:
        parts.append(f"步骤级重试 {retries} 次")
    if events:
        kinds = ", ".join(sorted({event.kind for event in events}))
        ok = sum(1 for event in events if event.success)
        parts.append(f"自愈决策 {len(events)} 次（成功 {ok}，类型: {kinds}）")
    return "；".join(parts)


def _slug(text: str) -> str:
    out = [ch if ch.isalnum() else "_" for ch in text.strip().lower()]
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "composed"
