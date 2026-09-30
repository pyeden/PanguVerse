"""自愈器：失败 → 分类 → 决策（重新认证 / 修复重试 / 给出修复建议）。

自愈分两层，这是第二层（第一层是步骤级确定性 ``retry``）：
- **重新认证**：401/403 → 重跑用例里的认证步骤，再重试失败步骤（token 过期是 API 测试最常见的自愈场景）；
- **修复重试**：网络/超时/5xx → 有限次重试；若经验库里有「已验证的修复策略」，直接复用；
- **修复建议**：断言不符、404、409 等语义问题不做魔法修补，给出可执行的排查建议并沉淀成经验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import PanguError
from ..core.molecule import Molecule, RetryPolicy, Step
from ..engine.executor import classify_exception
from .memory import request_target

TRANSIENT_KINDS = {"network", "timeout", "server_error", "rate_limit"}
AUTH_KINDS = {"auth"}
_AUTH_HINTS = ("login", "auth", "token", "signin", "sign_in", "授权", "登录", "认证")


@dataclass
class HealDecision:
    kind: str = "none"  # reauth | retry | patch_retry | none
    failure_kind: str = "unknown"
    reason: str = ""
    auth_step_id: str | None = None
    delay_ms: int = 0
    retry_override: RetryPolicy | None = None
    suggestion: str = ""
    fix: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


_HEURISTIC_SUGGESTIONS: dict[str, str] = {
    "not_found": "资源不存在：确认 id 的来源是否正确，或在前置步骤先创建资源再断言",
    "conflict": "状态冲突：用例可能被重复执行；改用具唯一性的数据，或先回滚/清理状态",
    "client_error": "请求参数不合法：核对必填字段、类型与格式（多看一次报告里的 request）",
    "assertion": "断言与实现不一致：核对期望值来源；若为接口变更，请同步更新场景断言",
    "plan": "计划有问题：先运行 `panguverse plan` 校验原子名与必填输入",
    "step_error": "原子执行异常：检查报告中的输入、响应与 trace",
    "timeout": "接口过慢：放宽 timeout，或改为轮询 + sleep",
    "network": "网络不可达：确认 BASE_URL、目标服务是否已启动、是否需要代理",
    "rate_limit": "被限流：退避重试或降低并发",
    "atom_error": "原子内部异常：查看该原子的实现与输入",
    "unknown": "查看报告中的请求/响应现场后定位",
}


class Healer:
    """把失败翻译成可执行动作。"""

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
    ) -> HealDecision:
        kind = classify_exception(exc)
        decision: HealDecision

        if kind in AUTH_KINDS:
            auth_step = find_auth_step(case, before=step.id)
            if self.enable_reauth and auth_step is not None:
                decision = HealDecision(
                    kind="reauth",
                    failure_kind=kind,
                    reason=f"凭证失效(401/403) → 重新执行认证步骤 {auth_step.id} 后重试",
                    auth_step_id=auth_step.id,
                    retry_override=RetryPolicy(attempts=1),
                    suggestion="token 已失效：重新认证后重试即可；若频繁失效，检查 token TTL 与登录步骤",
                    detail={"auth_step": auth_step.id, "strategy": "reauth-then-retry"},
                )
                self.stats["reauth"] += 1
                return decision
            return self._none(
                kind,
                step,
                "凭证失效但没有可用的认证步骤（给登录步骤加 tags: [auth]）",
                case=case,
                exc=exc,
            )

        if kind in TRANSIENT_KINDS:
            lesson = self._recall(kind, step)
            if lesson and isinstance(lesson.fix, dict) and lesson.fix.get("retry"):
                policy = RetryPolicy.from_spec(lesson.fix["retry"])
                self.stats["patch_retry"] += 1
                return HealDecision(
                    kind="patch_retry",
                    failure_kind=kind,
                    reason=f"命中经验库：{lesson.describe()}",
                    retry_override=policy,
                    suggestion=lesson.suggestion,
                    fix=dict(lesson.fix),
                    detail={
                        "lesson": lesson.to_dict() if hasattr(lesson, "to_dict") else {"signature": getattr(lesson, "signature", "")},
                        "strategy": "lesson-driven-retry",
                    },
                )
            if self.enable_retry:
                policy = RetryPolicy(
                    attempts=self.retry_attempts,
                    delay_ms=self.retry_delay_ms,
                    backoff=2.0,
                    on=("any",),
                )
                self.stats["retry"] += 1
                return HealDecision(
                    kind="retry",
                    failure_kind=kind,
                    reason=f"{kind} 属于瞬态故障 → 退避重试",
                    delay_ms=self.retry_delay_ms,
                    retry_override=policy,
                    suggestion=_HEURISTIC_SUGGESTIONS.get(kind, ""),
                    detail={"strategy": "backoff-retry"},
                )

        return self._none(kind, step, "", case=case, exc=exc)

    # ------------------------------------------------------------------ #
    def _none(
        self, kind: str, step: Step, extra_reason: str = "", *, case: Molecule, exc: BaseException
    ) -> HealDecision:
        self.stats["none"] += 1
        suggestion = extra_reason or _HEURISTIC_SUGGESTIONS.get(kind, _HEURISTIC_SUGGESTIONS["unknown"])
        lesson = self._recall(kind, step)
        if lesson and lesson.suggestion:
            suggestion = f"{suggestion}（历史经验：{lesson.suggestion}）"
        llm_hint = self._llm_suggestion(case, step, exc)
        if llm_hint:
            suggestion = f"{suggestion} | AI 建议：{llm_hint}"
        return HealDecision(
            kind="none",
            failure_kind=kind,
            reason=f"不做自动修补（{kind}）：需要人或规划层介入",
            suggestion=suggestion,
            detail={"atom": step.use, "case_id": case.case_id, "strategy": "advice-only"},
        )

    def _recall(self, kind: str, step: Step) -> Any:
        """按「原子 + 接口 + 失败类型」回忆经验（接口未知时退回同原子经验）。"""
        if not self.lessons:
            return None
        url = (step.with_ or {}).get("url")
        target = request_target(url) if url else None
        try:
            return self.lessons.recall(kind=kind, atom=step.use, target=target)
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
                "你是资深 API 自动化测试专家。基于失败现场，用一句中文给出可执行的修复建议，"
                "只输出建议本身，不要解释、不要 markdown。",
                f"失败现场(JSON)：{context}",
            )
            return text.strip().splitlines()[0][:200]
        except Exception:  # noqa: BLE001
            return ""

    def summary(self) -> dict[str, int]:
        return dict(self.stats)


def find_auth_step(case: Molecule, *, before: str | None = None) -> Step | None:
    """找出用例里的认证步骤（tag=auth 优先，其次按命名启发式）。"""
    candidates: list[Step] = []
    for step in case.steps:
        if before is not None and step.id == before:
            break
        if "auth" in step.tags:
            candidates.append(step)
            continue
        lowered = f"{step.id} {step.name} {step.use}".lower()
        if any(hint in lowered for hint in _AUTH_HINTS):
            candidates.append(step)
    if not candidates:
        return None
    tagged = [s for s in candidates if "auth" in s.tags]
    return (tagged or candidates)[-1]
