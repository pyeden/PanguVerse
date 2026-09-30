"""执行引擎：把计划里的分子/原子真正跑起来。

引擎负责四件事：
1. **上下文传递**：步骤输出自动并入上下文，``capture`` 映射成可复用的变量；
2. **数据驱动**：``foreach`` / ``when`` 让同一份用例跑多组数据；
3. **自愈第一层**：步骤级 ``retry``（含 5xx 软失败重试）；
4. **自愈第二层**：把失败交给 :class:`~panguverse.axe.healer.Healer` 决策（重新认证 / 修复后重试）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..core.atom import Atom
from ..core.context import MISSING, RunContext, split_path, walk_path
from ..core.errors import (
    PanguError,
    PlanError,
    RequestTimeout,
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
                step_result.status = FAILED if isinstance(exc, StepFailure) else ERROR
                step_result.error = _error_dict(exc)
                step_result.message = str(exc)
                step_result.duration_ms = (time.perf_counter() - started) * 1000
                if step.foreach:
                    step_result.inputs = {"foreach": step.foreach, "failed_at_index": index}
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
            try:
                outputs = target.invoke(ctx, inputs)
            except (StepFailure, StepError, PanguError, TypeError) as exc:
                kind = classify_exception(exc)
                if policy and policy.should_retry(kind) and attempt < policy.attempts:
                    step_result.retries += 1
                    ctx.emit("retry", step=step.id, attempt=attempt, kind=kind, reason=str(exc))
                    _sleep(policy.delay_for(attempt))
                    continue
                raise
            step_result.http.extend(ctx.http_log[marker:])
            soft_kind = _soft_failure_kind(outputs)
            if soft_kind and policy and policy.should_retry(soft_kind) and attempt < policy.attempts:
                step_result.retries += 1
                ctx.emit(
                    "retry",
                    step=step.id,
                    attempt=attempt,
                    kind=soft_kind,
                    status_code=outputs.get("status_code"),
                )
                _sleep(policy.delay_for(attempt))
                continue
            return outputs

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
        """把分子注册成原子，供更高层递归组合。"""
        name = str(molecule.meta.get("atom_name") or molecule.case_id or molecule.name)
        outputs_spec = dict(molecule.meta.get("outputs") or {})

        def run_molecule(ctx: RunContext, **inputs: Any) -> dict[str, Any]:
            holder: dict[str, Any] = {}
            case_result = self.run_case(molecule, variables=dict(inputs), parent=ctx, out=holder)
            if case_result.status == FAILED:
                raise StepFailure(
                    f"组合原子 {name} 失败: {case_result.name}",
                    detail={"case_id": case_result.case_id, "steps": len(case_result.steps)},
                )
            if case_result.status == ERROR:
                raise StepError(f"组合原子 {name} 执行异常: {case_result.name}")
            return _collect_molecule_outputs(outputs_spec, case_result, ctx, holder.get("ctx"))

        item = Atom(
            name=name,
            fn=run_molecule,
            description=molecule.description or f"分子原子：{molecule.name}",
            inputs=dict.fromkeys(molecule.meta.get("inputs") or {}, "any"),
            outputs=dict.fromkeys(outputs_spec, "any"),
            required=tuple(molecule.meta.get("required_inputs") or ()),
            tags=tuple({"molecule", *molecule.tags}),
            category="molecule",
            source="molecule",
        )
        molecule.owner = molecule.owner or self
        self.registry.register(item, replace=True)
        return item


def _collect_molecule_outputs(
    outputs_spec: dict[str, Any],
    case_result: CaseResult,
    parent_ctx: RunContext,
    child_ctx: RunContext | None = None,
) -> dict[str, Any]:
    """分子对外输出：按 ``outputs`` 映射从子用例上下文里取值（优先用未打码的真实上下文）。"""
    if not outputs_spec:
        return {"case_id": case_result.case_id, "status": case_result.status}
    if child_ctx is not None:
        scope = child_ctx
    else:  # 兜底：退化到变量快照（可能已被打码）
        snapshot = case_result.variables or {}
        scope = parent_ctx.isolated(**(snapshot.get("vars") or {}))
        scope.env = {**parent_ctx.env, **(snapshot.get("env") or {})}
    resolved: dict[str, Any] = {"status": case_result.status}
    for name, expr in outputs_spec.items():
        if isinstance(expr, str) and expr.startswith("${"):
            try:
                resolved[name] = scope.resolve(expr)
            except PanguError:
                resolved[name] = None
        else:
            resolved[name] = expr
    return resolved


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
    """失败分类：决定「能不能重试」「要不要重新认证」。"""
    if isinstance(exc, RequestTimeout):
        return "timeout"
    if isinstance(exc, StepFailure):
        actual = getattr(exc, "actual", None)
        if isinstance(actual, int):
            return classify_status(actual)
        return "assertion"
    if isinstance(exc, PlanError):
        return "plan"
    if isinstance(exc, StepError):
        return "step_error"
    if isinstance(exc, PanguError):
        return exc.kind
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "network"
    return "atom_error"


def classify_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if status_code == 429:
        return "rate_limit"
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "ok"


def _soft_failure_kind(outputs: dict[str, Any]) -> str | None:
    """5xx 这类「成功返回但结果不可接受」的软失败。"""
    status = outputs.get("status_code") if isinstance(outputs, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        kind = classify_status(status)
        if kind in {"server_error", "rate_limit"}:
            return kind
    return None


def _error_dict(exc: BaseException) -> dict[str, Any]:
    """统一错误结构：``kind`` 是异常类别，``failure_kind`` 是失败分类（401→auth、404→not_found…）。"""
    if isinstance(exc, PanguError):
        data = exc.to_dict()
    else:
        data = {"type": type(exc).__name__, "kind": "atom_error", "message": str(exc)}
    data["failure_kind"] = classify_exception(exc)
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
