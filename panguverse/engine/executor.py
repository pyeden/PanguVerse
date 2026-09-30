"""执行引擎：把计划里的分子/原子真正跑起来。

引擎负责四件事：
1. **上下文传递**：步骤输出自动并入上下文，``capture`` 映射成可复用的变量；
2. **数据驱动**：``foreach`` / ``when`` 让同一份用例跑多组数据；
3. **自愈第一层**：步骤级 ``retry``（含领域层声明的「软失败」重试）；
4. **自愈第二层**：把失败交给 :class:`~panguverse.axe.healer.Healer` 决策（重新认证 / 修复后重试）。

**领域中立**：本文件不含任何 HTTP / 文件系统 / 数据库知识。
「5xx 该不该重试」「401 是凭证问题」「经验标识是 URL 还是文件路径」全部由原子所属的
领域包（:mod:`panguverse.core.contract` + :mod:`panguverse.packs`）通过 ``semantics`` 提供。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..core.atom import Atom
from ..core.context import MISSING, RunContext, split_path, walk_path
from ..core.contract import NEUTRAL_SEMANTICS, AtomSemantics, FailureClass, neutral_failure_class, neutral_failure_kind
from ..core.errors import (
    PanguError,
    PlanError,
    StepError,
    StepFailure,
)
from ..core.molecule import Molecule, RetryPolicy, Step
from ..core.registry import AtomRegistry, default_registry
from .result import (
    ERROR,
    FAILED,
    PASSED,
    SKIPPED,
    CaseResult,
    HealEvent,
    RunResult,
    StepResult,
)


@dataclass
class ExecutorSettings:
    """执行策略（CLI 参数会覆盖这些默认值）。"""

    default_retry: RetryPolicy | None = None
    heal: bool = True
    heal_budget: int = 2
    continue_on_failure: bool = False
    stop_on_first_failure: bool = False
    timeout: float = 10.0
    #: 本次运行被授权的运行时能力（``network`` / ``filesystem`` / ``shell`` …）。
    #: ``None`` 表示「不做限制」（向后兼容），给了列表则**严格校验**原子声明的能力。
    capabilities: tuple[str, ...] | None = None


class Executor:
    """执行引擎。``Executor`` 同时也是 ``seal()`` 的落点（分子 → 原子）。"""

    def __init__(
        self,
        registry: AtomRegistry | None = None,
        *,
        settings: ExecutorSettings | None = None,
        healer: Any = None,
        workspace: Any = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.settings = settings or ExecutorSettings()
        self.healer = healer
        self.workspace = workspace
        # 声明式（YAML）原子在运行期需要通过「分子执行器」回调
        from ..core.pack import set_runner

        set_runner(self)

    # ------------------------------------------------------------------ #
    # 领域语义
    # ------------------------------------------------------------------ #
    def semantics_for(self, atom: Atom | str) -> AtomSemantics:
        """原子的领域语义（未登记则回退中立语义）。"""
        try:
            return self.registry.semantics_for(atom)
        except Exception:  # noqa: BLE001 - 语义查询失败不应影响执行
            return NEUTRAL_SEMANTICS

    def classify(
        self,
        atom: Atom | None,
        outputs: dict[str, Any] | None,
        exc: BaseException | None,
    ) -> tuple[str, FailureClass]:
        """把失败翻译成 ``(领域标签, 中立类别)``——**唯一**的失败分类入口。"""
        semantics = self.semantics_for(atom) if atom is not None else NEUTRAL_SEMANTICS
        kind = semantics.failure_kind(atom, outputs, exc)
        failure_class = semantics.failure_class(atom, outputs, exc)
        return kind, failure_class

    # ------------------------------------------------------------------ #
    # 编排层入口
    # ------------------------------------------------------------------ #
    def run(
        self,
        plan: Any,
        *,
        env: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        run_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> RunResult:
        """执行一个测试计划（``plan.cases``）。"""
        cases: list[Molecule] = list(getattr(plan, "cases", []) or [])
        if not cases:
            raise PlanError("计划中没有任何用例")
        plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
        plan_env = dict(getattr(plan, "env", {}) or {})
        merged_env = {**plan_env, **(env or {})}
        run = RunResult(
            run_id=run_id or uuid.uuid4().hex[:12],
            plan=plan_dict,
            env=merged_env,
            meta={**(meta or {}), "backend": _backend_name(), "atom_count": len(self.registry)},
        )
        run.add_phase("plan", "ok", f"{len(cases)} 个用例来自 {getattr(plan, 'source', 'unknown')}")
        for case in cases:
            case_result = self.run_case(
                case,
                env=merged_env,
                variables=variables,
                run_id=run.run_id,
            )
            run.cases.append(case_result)
            if self.settings.stop_on_first_failure and case_result.status in (FAILED, ERROR):
                run.notes.append(f"fail-fast：用例 {case.case_id} 失败后停止")
                break
        run.finished_at = time.time()
        summary = run.summary
        run.add_phase(
            "execute",
            "ok" if summary["success"] else "failed",
            f"{summary['passed']}/{summary['total']} 通过，"
            f"{summary['failed']} 失败，{summary['errored']} 异常，"
            f"{summary['http_requests']} 次 HTTP 调用",
        )
        return run

    def run_case(
        self,
        case: Molecule,
        *,
        env: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        run_id: str | None = None,
        parent: RunContext | None = None,
        out: dict[str, Any] | None = None,
    ) -> CaseResult:
        """执行单个用例（分子）。

        ``out`` 是可选的出参：会把真实的 :class:`RunContext` 放进去，
        供组合原子（分子→原子）读取**未打码**的输出值。
        """
        started = time.time()
        result = CaseResult(
            case_id=case.case_id,
            name=case.name,
            tags=list(case.tags),
            description=case.description,
            started_at=started,
            source=case.source,
            meta=dict(case.meta),
        )
        if parent is not None:
            ctx = parent.child(**(variables or {}))
        else:
            ctx = RunContext(
                env=env or {},
                variables=variables or {},
                run_id=run_id,
                workspace=self.workspace,
            )
            ctx.env = {**ctx.env, **(env or {})}
        ctx.vars["case_id"] = case.case_id
        ctx.meta["_case_secrets"] = set(case.secrets)
        heal_budget = self.settings.heal_budget
        aborted = False

        for step in case.steps:
            if aborted:
                result.steps.append(
                    StepResult(
                        step_id=step.id,
                        atom=step.use,
                        name=step.name,
                        status=SKIPPED,
                        skipped_reason="前序步骤失败（continue_on_failure=false）",
                    )
                )
                continue
            try:
                step_result, budget_used = self._execute_step(case, step, ctx, heal_budget)
            except Exception as exc:  # noqa: BLE001 - 兜底：绝不让引擎自身崩掉
                step_result = StepResult(
                    step_id=step.id,
                    atom=step.use,
                    name=step.name,
                    status=ERROR,
                    error={"type": type(exc).__name__, "kind": "engine", "message": str(exc)},
                )
                budget_used = 0
            heal_budget -= budget_used
            result.steps.append(step_result)
            result.heal_events.extend(step_result.heal)
            if step_result.status in (FAILED, ERROR):
                if step.optional or step.continue_on_failure or self.settings.continue_on_failure:
                    continue
                aborted = True

        result.duration_ms = (time.time() - started) * 1000
        result.status = _case_status(result.steps)
        for name in case.secrets:  # 场景里声明的密钥 → 报告打码
            value = ctx.vars.get(name)
            if value is not None:
                ctx.add_secret(name, value)
        result.variables = ctx.snapshot()
        result.trace = list(ctx.trace)
        result.meta["secret_values"] = ctx.secret_values()
        if out is not None:
            out["ctx"] = ctx
        first_problem = next((s for s in result.steps if s.status in (FAILED, ERROR)), None)
        if first_problem and first_problem.error:
            result.error = first_problem.error
        return result

    # ------------------------------------------------------------------ #
    # 步骤执行
    # ------------------------------------------------------------------ #
    def _execute_step(
        self, case: Molecule, step: Step, ctx: RunContext, heal_budget: int
    ) -> tuple[StepResult, int]:
        step_result = StepResult(
            step_id=step.id,
            atom=step.use,
            name=step.name or step.description,
            tags=list(step.tags),
        )
        started = time.perf_counter()
        budget_used = 0

        # 1) 条件步骤
        if step.when not in (None, ""):
            try:
                condition = ctx.resolve(step.when)
            except PanguError as exc:
                step_result.status = ERROR
                step_result.error = exc.to_dict()
                step_result.duration_ms = (time.perf_counter() - started) * 1000
                return step_result, 0
            if not _truthy(condition):
                step_result.status = SKIPPED
                step_result.skipped_reason = f"when 条件不成立: {step.when}"
                step_result.duration_ms = (time.perf_counter() - started) * 1000
                return step_result, 0

        # 2) 原子是否存在
        try:
            target = self.registry.get(step.use)
        except PanguError as exc:
            step_result.status = ERROR
            step_result.error = exc.to_dict()
            step_result.duration_ms = (time.perf_counter() - started) * 1000
            return step_result, 0

        # 3) 数据驱动
        iterations: list[Any] = [None]
        if step.foreach:
            try:
                items = ctx.resolve(step.foreach)
            except PanguError as exc:
                step_result.status = ERROR
                step_result.error = exc.to_dict()
                step_result.duration_ms = (time.perf_counter() - started) * 1000
                return step_result, 0
            if isinstance(items, dict):
                items = list(items.values())
            if not isinstance(items, (list, tuple)):
                step_result.status = ERROR
                step_result.error = {
                    "type": "PlanError",
                    "kind": "plan",
                    "message": f"foreach 需要列表，实际 {type(items).__name__}",
                }
                step_result.duration_ms = (time.perf_counter() - started) * 1000
                return step_result, 0
            iterations = list(items)

        collected: list[dict[str, Any]] = []
        capture_buckets: dict[str, list[Any]] = {}
        for index, item in enumerate(iterations):
            loop_ctx = ctx
            if step.foreach:
                loop_ctx = ctx.child(
                    **{
                        step.as_: item,
                        "index": index,
                        "loop": {
                            "index": index,
                            "number": index + 1,
                            "length": len(iterations),
                            "first": index == 0,
                            "last": index == len(iterations) - 1,
                        },
                    }
                )
            try:
                outputs = self._attempt(step, target, loop_ctx, step_result)
            except Exception as exc:  # noqa: BLE001 - 失败分类与自愈都在下面处理
                kind, failure_class = self.classify(target, None, exc)
                step_result.status = FAILED if isinstance(exc, StepFailure) else ERROR
                step_result.error = _error_dict(exc, kind=kind, failure_class=failure_class)
                step_result.message = str(exc)
                step_result.duration_ms = (time.perf_counter() - started) * 1000
                if step.foreach:
                    step_result.inputs = {"foreach": step.foreach, "failed_at_index": index}
                # 专家经验：领域包定义的「作用对象标识」——HTTP 是接口路径，文件是路径，SQL 是表名
                step_result.target = self.semantics_for(target).target(target, step_result.inputs)
                # 自愈第二层：留给 Healer 决策（重新认证 / 修复重试）
                if self.settings.heal and self.healer is not None and heal_budget > 0:
                    healed, used = self._try_heal(case, step, target, loop_ctx, step_result, exc)
                    budget_used += used
                    if healed:
                        step_result.status = PASSED
                        step_result.error = None
                        step_result.duration_ms = (time.perf_counter() - started) * 1000
                        return step_result, budget_used
                return step_result, budget_used

            collected.append(outputs)
            # 密钥登记只有一个入口，避免「不同代码路径对声明的 secrets 处理不一致」
            if outputs or step.id not in ctx.vars:
                ctx.vars[step.id] = outputs
            if outputs or step.id not in loop_ctx.vars:
                loop_ctx.vars[step.id] = outputs
            # 原子自己声明的「输出即密钥」（领域包可让 token/密钥字段自动打码）
            for secret_name, secret_value in target.secrets_in_outputs(outputs):
                ctx.add_secret(secret_name, secret_value)
            if step.foreach:
                for name, value, secret in self._capture_values(step, outputs, ctx=loop_ctx):
                    capture_buckets.setdefault(name, []).append((value, secret))
            else:
                self._apply_capture(step, outputs, ctx)
        if step.foreach:
            step_result.iterations = len(iterations)
            last = collected[-1] if collected else {}
            # 顶层合并最后一次迭代的输出，便于 `${step.body.x}` 直接引用；
            # `items`/`last`/`count` 提供全量迭代结果（与 HTTP 响应字段不冲突）
            merged = {**last, "count": len(collected), "items": collected, "last": last}
            ctx.vars[step.id] = merged
            step_result.outputs = merged
            for name, bucket in capture_buckets.items():
                ctx.set(name, [value for value, _secret in bucket])
                for value, secret in bucket:
                    if secret:
                        ctx.add_secret(name, value)
        else:
            step_result.outputs = collected[-1] if collected else {}

        step_result.attempts = max(1, step_result.attempts)
        step_result.status = PASSED
        step_result.duration_ms = (time.perf_counter() - started) * 1000
        return step_result, budget_used

    def _attempt(
        self,
        step: Step,
        target: Atom,
        ctx: RunContext,
        step_result: StepResult,
        *,
        policy_override: RetryPolicy | None = None,
    ) -> dict[str, Any]:
        """带重试的单次（或单次迭代）执行。"""
        policy = policy_override or step.retry or self.settings.default_retry
        semantics = self.semantics_for(target)
        self._check_capabilities(target)
        attempt = 0
        while True:
            attempt += 1
            step_result.attempts = attempt
            inputs = ctx.resolve(step.with_)
            missing = [name for name in target.required if inputs.get(name) is None]
            if missing:
                raise PlanError(
                    f"步骤 {step.id} 缺少必填输入: {', '.join(missing)}（原子 {target.name}）"
                )
            marker = len(ctx.http_log)
            step_result.inputs = _brief(inputs)
            step_result.target = semantics.target(target, inputs)
            try:
                outputs = target.invoke(ctx, inputs)
            except (StepFailure, StepError, PanguError, TypeError) as exc:
                kind, failure_class = self.classify(target, None, exc)
                if policy and policy.should_retry(kind, failure_class.value) and attempt < policy.attempts:
                    step_result.retries += 1
                    ctx.emit("retry", step=step.id, attempt=attempt, kind=kind, reason=str(exc))
                    _sleep(policy.delay_for(attempt))
                    continue
                raise
            step_result.http.extend(ctx.http_log[marker:])
            # 领域层声明「这次结果本身算失败」（例如 HTTP 5xx / 429）：仅在配置了重试策略时重试，
            # 不改变「4xx 是合法被测结果」的语义——断言由 assert_* 原子负责。
            soft = semantics.result_status(target, outputs)
            if soft and policy and policy.should_retry(soft[0], soft[1].value) and attempt < policy.attempts:
                step_result.retries += 1
                ctx.emit(
                    "retry",
                    step=step.id,
                    attempt=attempt,
                    kind=soft[0],
                    failure_class=soft[1].value,
                    outputs=_brief(outputs),
                )
                _sleep(policy.delay_for(attempt))
                continue
            return outputs

    def _check_capabilities(self, atom: Atom) -> None:
        """能力授权校验：显式授权列表存在时，原子声明的能力必须都在里面。"""
        granted = self.settings.capabilities
        if granted is None or not atom.capabilities:
            return
        missing = [cap for cap in atom.capabilities if cap not in set(granted)]
        if missing:
            raise StepError(
                f"原子 {atom.name} 需要未授权的能力: {', '.join(missing)}"
                f"（本次已授权: {', '.join(sorted(granted)) or '无'}）"
            )

    def _capture_values(
        self,
        step: Step,
        outputs: dict[str, Any],
        *,
        ctx: RunContext,
    ) -> list[tuple[str, Any, bool]]:
        """解析 ``capture``：返回值列表 ``(变量名, 值, 是否密钥)``。

        「是否密钥」= 步骤里显式声明的 ``secret: true`` **或** 名字出现在场景的 ``secrets:`` 列表里。
        这里必须是唯一的判定入口——否则不同执行路径（正常 / 自愈 / foreach）对密钥的处理会不一致，
        从而把明文漏进报告（曾经的 bug）。
        """
        declared = ctx.meta.get("_case_secrets") or set()
        captured: list[tuple[str, Any, bool]] = []
        for name, spec in (step.capture or {}).items():
            required = True
            secret = False
            if isinstance(spec, dict):
                path = spec.get("path") or spec.get("from") or name
                required = bool(spec.get("required", True))
                secret = bool(spec.get("secret", False))
            else:
                path = str(spec)
            value = walk_path(outputs, split_path(str(path)))
            if value is MISSING:
                if required:
                    raise StepError(f"步骤 {step.id} 的 capture 路径不存在: {path}")
                continue
            captured.append((str(name), value, secret or str(name) in declared))
        return captured

    def _apply_capture(self, step: Step, outputs: dict[str, Any], ctx: RunContext) -> None:
        for name, value, secret in self._capture_values(step, outputs, ctx=ctx):
            ctx.set(name, value)
            if secret:
                ctx.add_secret(name, value)

    # ------------------------------------------------------------------ #
    # 自愈
    # ------------------------------------------------------------------ #
    def _try_heal(
        self,
        case: Molecule,
        step: Step,
        target: Atom,
        ctx: RunContext,
        step_result: StepResult,
        exc: BaseException,
    ) -> tuple[bool, int]:
        decision = self.healer.propose(
            case=case,
            step=step,
            exc=exc,
            ctx=ctx,
            settings=self.settings,
            semantics=self.semantics_for(target),
            atom=target,
            resolved_inputs=step_result.inputs,
        )
        if decision is None or decision.kind == "none":
            if decision is not None and decision.suggestion:
                step_result.heal.append(
                    HealEvent(
                        step_id=step.id,
                        kind=decision.failure_kind,
                        action="suggest",
                        success=False,
                        reason=decision.reason,
                        suggestion=decision.suggestion,
                    )
                )
            return False, 0
        used = 0
        if decision.kind == "reauth" and decision.auth_step_id:
            auth_step = next((s for s in case.steps if s.id == decision.auth_step_id), None)
            if auth_step is None:
                return False, 0
            try:
                auth_atom = self.registry.get(auth_step.use)
                scratch = StepResult(step_id=auth_step.id, atom=auth_step.use)
                outputs = self._attempt(auth_step, auth_atom, ctx, scratch)
                # 重新认证必须把新的 token 写回上下文，否则重试还是用旧凭证
                ctx.vars[auth_step.id] = outputs
                self._apply_capture(auth_step, outputs, ctx)
                used += 1
            except Exception as exc:  # noqa: BLE001 - 重新认证失败 → 放弃自愈
                step_result.heal.append(
                    HealEvent(
                        step_id=step.id,
                        kind=decision.failure_kind,
                        action="reauth",
                        success=False,
                        reason=f"重新执行 {auth_step.id} 失败: {exc}",
                    )
                )
                return False, used
        if decision.kind in {"reauth", "retry", "patch_retry"}:
            if decision.kind == "retry" and decision.delay_ms:
                _sleep(decision.delay_ms / 1000.0)
            try:
                outputs = self._attempt(
                    step,
                    target,
                    ctx,
                    step_result,
                    policy_override=decision.retry_override,
                )
            except Exception:  # noqa: BLE001
                step_result.heal.append(
                    HealEvent(
                        step_id=step.id,
                        kind=decision.failure_kind,
                        action=decision.kind,
                        success=False,
                        reason=decision.reason,
                        suggestion=decision.suggestion,
                    )
                )
                return False, used
            ctx.vars[step.id] = outputs
            self._apply_capture(step, outputs, ctx)
            step_result.outputs = _brief(outputs)
            used += 1
            step_result.heal.append(
                HealEvent(
                    step_id=step.id,
                    kind=decision.failure_kind,
                    action=decision.kind,
                    success=True,
                    reason=decision.reason,
                    detail=dict(decision.detail or {}),
                )
            )
            return True, used
        return False, used

    # ------------------------------------------------------------------ #
    # 分子 → 原子（seal）
    # ------------------------------------------------------------------ #
    def seal_molecule(self, molecule: Molecule) -> Atom:
        """把分子注册成原子，供更高层递归组合（与声明式原子共用同一套实现）。"""
        from ..core.pack import composite_atom

        name = str(molecule.meta.get("atom_name") or molecule.case_id or molecule.name)
        item = composite_atom(
            name=name,
            molecule=molecule,
            description=molecule.description or f"分子原子：{molecule.name}",
            inputs=molecule.meta.get("inputs") or {},
            outputs=molecule.meta.get("outputs") or {},
            output_types=molecule.meta.get("output_types") or {},
            required_inputs=tuple(molecule.meta.get("required_inputs") or ()),
            tags=tuple({"molecule", *molecule.tags}),
            category=str(molecule.meta.get("category") or "molecule"),
            domain=str(molecule.meta.get("domain") or "generic"),
            pack=str(molecule.meta.get("pack") or ""),
            effects=tuple(molecule.meta.get("effects") or ()),
            capabilities=tuple(molecule.meta.get("capabilities") or ()),
            secret_outputs=tuple(molecule.meta.get("secret_outputs") or ()),
            version=str(molecule.meta.get("version") or "0.1.0"),
            runner=self,
        )
        item.source = "molecule"
        molecule.owner = molecule.owner or self
        # 组合原子默认用中立语义；内部失败的性质会通过 AtomFailure 透传出来
        self.registry.register(item, replace=True)
        return item


def _case_status(steps: Iterable[StepResult]) -> str:
    items = list(steps)
    if any(s.status == ERROR for s in items):
        return ERROR
    if any(s.status == FAILED for s in items):
        return FAILED
    if items and all(s.status == SKIPPED for s in items):
        return SKIPPED
    return PASSED


def classify_exception(exc: BaseException) -> str:
    """中立失败标签（不含任何领域知识）。

    领域标签请走 ``Executor.classify()`` / 领域包语义；
    这里只回答「这是超时/网络/断言/计划/内部异常」这类跨领域的问题。
    """
    return neutral_failure_kind(exc)


def classify_failure_class(exc: BaseException) -> FailureClass:
    """中立失败类别（跨领域通用）。"""
    return neutral_failure_class(exc)


def _error_dict(
    exc: BaseException,
    *,
    kind: str | None = None,
    failure_class: FailureClass | None = None,
) -> dict[str, Any]:
    """统一错误结构。

    - ``kind``：异常自身类别（``assertion`` / ``step_error`` / ``atom_not_found`` …）
    - ``failure_kind``：**领域标签**（``server_error`` / ``file_not_found`` …）
    - ``failure_class``：**中立类别**（``transient`` / ``credentials`` …）
    """
    if isinstance(exc, PanguError):
        data = exc.to_dict()
    else:
        data = {"type": type(exc).__name__, "kind": "atom_error", "message": str(exc)}
    data["failure_kind"] = kind or neutral_failure_kind(exc)
    data["failure_class"] = (failure_class or neutral_failure_class(exc)).value
    return data


def _brief(inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in inputs.items():
        text = repr(value)
        out[key] = value if len(text) <= 1500 else text[:1500] + "..."
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "none", "null"}
    return bool(value)


def _sleep(seconds: float) -> None:
    if seconds and seconds > 0:
        time.sleep(seconds)


def _backend_name() -> str:
    from ..http.client import backend_name

    return backend_name()
