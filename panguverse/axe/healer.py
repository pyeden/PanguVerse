"""自愈器：失败 → 分类 → 决策（重新认证 / 修复重试 / 给出修复建议）。

自愈分两层，这是第二层（第一层是步骤级确定性 ``retry``）。

**领域中立**：本文件不知道 HTTP、文件系统或任何具体协议。
它只做四件事：

1. 通过原子所属领域包的 ``semantics`` 拿到 ``(领域标签, 中立类别)``；
2. 命中经验库时复用「已验证的修复策略」；
3. 采用领域包声明的恢复动作（如 API 领域的「重新认证后重试」、文件领域的「锁等待重试」）；
4. 其余情况**不做魔法修补**，给出领域包提供的可执行建议。

安全边界（利用原子契约里的副作用声明）：

- 非幂等副作用（``mutate`` / ``destructive``）默认**不做自动重试**——重试一个付款请求可能造成二次扣款；
  只有计划里显式写了 ``retry`` 才重试（人明确承担风险）；
- 凭证类失败（401/403）例外：请求已被拒绝、没有产生副作用，因此「重新认证后重试」是安全的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.contract import NEUTRAL_SEMANTICS, AtomSemantics, FailureClass, Recovery
from ..core.errors import PanguError
from ..core.molecule import Molecule, RetryPolicy, Step


@dataclass
class HealDecision:
    kind: str = "none"  # reauth | retry | patch_retry | none
    failure_kind: str = "unknown"
    failure_class: str = FailureClass.UNKNOWN.value
    reason: str = ""
    auth_step_id: str | None = None
    delay_ms: int = 0
    retry_override: RetryPolicy | None = None
    suggestion: str = ""
    fix: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


class Healer:
    """把失败翻译成可执行动作（决策逻辑领域无关）。"""

    def __init__(
        self,
        *,
        lessons: Any = None,
        enable_reauth: bool = True,
        enable_retry: bool = True,
        retry_attempts: int = 2,
        retry_delay_ms: int = 200,
        llm: Any = None,
        use_llm_suggestion: bool = True,
    ) -> None:
        self.lessons = lessons
        self.enable_reauth = enable_reauth
        self.enable_retry = enable_retry
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay_ms = retry_delay_ms
        self.llm = llm
        self.use_llm_suggestion = use_llm_suggestion
        self.stats: dict[str, int] = {"reauth": 0, "retry": 0, "patch_retry": 0, "none": 0}

    # ------------------------------------------------------------------ #
    def propose(
        self,
        *,
        case: Molecule,
        step: Step,
        exc: BaseException,
        ctx: Any = None,
        settings: Any = None,
        semantics: AtomSemantics | None = None,
        atom: Any = None,
        resolved_inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> HealDecision:
        sem = semantics or NEUTRAL_SEMANTICS
        kind = sem.failure_kind(atom, outputs, exc)
        failure_class = sem.failure_class(atom, outputs, exc)
        target = sem.target(atom, resolved_inputs) if atom is not None else None
        safe_to_retry = _retry_is_safe(atom, resolved_inputs, failure_class, step)
        recoveries = list(
            sem.recoveries(
                atom,
                kind=kind,
                failure_class=failure_class,
                case=case,
                step=step,
                inputs=resolved_inputs,
            )
        )

        # 1) 重新认证（领域包声明）——凭证失败意味着请求被拒绝，重试是安全的
        reauth = next((rec for rec in recoveries if rec.action == "reauth"), None)
        if reauth is not None and self.enable_reauth and reauth.auth_step_id:
            self.stats["reauth"] += 1
            return HealDecision(
                kind="reauth",
                failure_kind=kind,
                failure_class=failure_class.value,
                reason=reauth.reason or "凭证失效 → 重新认证后重试",
                auth_step_id=reauth.auth_step_id,
                retry_override=RetryPolicy(attempts=1),
                suggestion=reauth.advice or sem.advice(kind=kind, failure_class=failure_class),
                detail=dict(reauth.detail or {}),
            )

        # 2) 经验库：命中「已验证过的修复策略」直接复用
        lesson = self._recall(kind, step, target=target)
        if lesson and isinstance(lesson.fix, dict) and lesson.fix.get("retry") and safe_to_retry:
            policy = RetryPolicy.from_spec(lesson.fix["retry"])
            self.stats["patch_retry"] += 1
            return HealDecision(
                kind="patch_retry",
                failure_kind=kind,
                failure_class=failure_class.value,
                reason=f"命中经验库：{lesson.describe()}",
                retry_override=policy,
                suggestion=lesson.suggestion,
                fix=dict(lesson.fix),
                detail={
                    "lesson": lesson.to_dict() if hasattr(lesson, "to_dict") else {"signature": lesson.signature},
                    "strategy": "lesson-driven-retry",
                },
            )

        # 3) 领域包声明的重试型恢复动作
        retry_recovery = next((rec for rec in recoveries if rec.action == "retry"), None)
        if retry_recovery is not None and self.enable_retry:
            if safe_to_retry:
                policy = (
                    RetryPolicy.from_spec(retry_recovery.retry_hint)
                    if retry_recovery.retry_hint
                    else RetryPolicy(attempts=self.retry_attempts, delay_ms=self.retry_delay_ms, backoff=2.0, on=("any",))
                )
                self.stats["retry"] += 1
                return HealDecision(
                    kind="retry",
                    failure_kind=kind,
                    failure_class=failure_class.value,
                    reason=retry_recovery.reason or f"{kind} 属于可恢复故障 → 退避重试",
                    delay_ms=retry_recovery.delay_ms or self.retry_delay_ms,
                    retry_override=policy,
                    suggestion=retry_recovery.advice,
                    detail=dict(retry_recovery.detail or {"strategy": "domain-retry"}),
                )
            return self._none(
                kind,
                failure_class,
                sem,
                extra=(
                    f"该步骤是「{_effects_label(atom, resolved_inputs)}」副作用，自动重试可能造成重复执行；"
                    "如确认幂等，请在场景里显式写 retry"
                ),
                case=case,
                step=step,
                exc=exc,
            )

        return self._none(kind, failure_class, sem, case=case, step=step, exc=exc, target=target)

    # ------------------------------------------------------------------ #
    def _none(
        self,
        kind: str,
        failure_class: FailureClass,
        sem: AtomSemantics,
        *,
        case: Molecule,
        step: Step,
        exc: BaseException,
        target: str | None = None,
        extra: str = "",
    ) -> HealDecision:
        self.stats["none"] += 1
        suggestion = extra or sem.advice(kind=kind, failure_class=failure_class)
        lesson = self._recall(kind, step, target=target)
        if lesson and lesson.suggestion:
            suggestion = f"{suggestion}（历史经验：{lesson.suggestion}）"
        llm_hint = self._llm_suggestion(case, step, exc)
        if llm_hint:
            suggestion = f"{suggestion} | AI 建议：{llm_hint}"
        return HealDecision(
            kind="none",
            failure_kind=kind,
            failure_class=failure_class.value,
            reason=f"不做自动修补（{kind}/{failure_class.value}）：需要人或规划层介入",
            suggestion=suggestion,
            detail={"atom": step.use, "case_id": case.case_id, "strategy": "advice-only"},
        )

    def _recall(self, kind: str, step: Step, target: str | None = None) -> Any:
        """按「原子 + 作用对象 + 失败类型」回忆经验（对象未知时退回同原子经验）。"""
        if not self.lessons:
            return None
        try:
            return self.lessons.recall(kind=kind, atom=step.use, target=target)
        except TypeError:  # 兼容旧的 recall(kind, atom) 签名
            return self.lessons.recall(kind=kind, atom=step.use)
        except Exception:  # noqa: BLE001 - 经验库不可用不应影响执行
            return None

    def _llm_suggestion(self, case: Molecule, step: Step, exc: BaseException) -> str:
        """有 LLM 时补一条修复建议；失败就静默降级（不阻塞闭环）。"""
        if not (self.llm and getattr(self.llm, "available", False) and self.use_llm_suggestion):
            return ""
        if not isinstance(exc, PanguError):
            return ""
        context = {
            "case": case.name,
            "step": step.id,
            "atom": step.use,
            "inputs": {k: str(v)[:200] for k, v in (step.with_ or {}).items()},
            "error": exc.to_dict(),
        }
        try:
            text = self.llm.complete(
                "你是资深自动化专家。基于失败现场，用一句中文给出可执行的修复建议，"
                "只输出建议本身，不要解释、不要 markdown。",
                f"失败现场(JSON)：{context}",
            )
            return text.strip().splitlines()[0][:200]
        except Exception:  # noqa: BLE001
            return ""

    def summary(self) -> dict[str, int]:
        return dict(self.stats)


# --------------------------------------------------------------------------- #
# 副作用与重试安全性（用原子契约判断，与具体领域无关）
# --------------------------------------------------------------------------- #
def _effects_label(atom: Any, inputs: dict[str, Any] | None) -> str:
    if atom is None:
        return "未知"
    effects = [str(item) for item in atom.effects_for(inputs or {})]
    return "/".join(effects) or "未声明"


def _retry_is_safe(
    atom: Any,
    inputs: dict[str, Any] | None,
    failure_class: FailureClass,
    step: Step,
) -> bool:
    """自动重试是否安全？

    - 计划里显式写了 retry → 由执行器负责，这里不参与；
    - 凭证失败 → 请求已被拒绝，没有副作用，可安全重试；
    - 其余情况看原子契约：只读/幂等副作用才允许自动重试。
    """
    if failure_class is FailureClass.CREDENTIALS:
        return True
    if atom is None:
        return True
    try:
        return bool(atom.retry_safe_for(inputs or {}))
    except Exception:  # noqa: BLE001 - 契约解析失败时保守处理
        return False


def find_auth_step(case: Molecule, *, before: str | None = None) -> Step | None:
    """兼容导出：真实实现在 ``api_testing`` 领域包里（认证步骤的识别属于 HTTP 领域知识）。"""
    from ..packs.api_testing import find_auth_step as impl

    return impl(case, before=before)


__all__ = ["HealDecision", "Healer", "Recovery", "find_auth_step"]
