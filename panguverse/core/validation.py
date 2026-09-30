"""静态计划校验：**在执行之前**把计划里的错误挑出来（领域中立）。

为什么要它：平台化以后，原子会由很多人维护、计划会由很多人写。
等到运行时才发现「`${orderid}` 拼错了」「用了别的领域的语法糖」「这个接口经验不足」，
成本远高于在 ``plan`` 阶段就报出来。

校验项：

======  ==============  ==================================================
代码    级别            说明
======  ==============  ==================================================
P001    error          原子不存在（拼写错误 / 包没加载）
P002    error          缺少必填输入
P101    error          模板引用了**没有任何步骤产出**的变量（最常见的手误）
P102    error          用了别的领域包的语法糖（例如在文件原子上写 ``assert:``）
P201    warning        字面量与声明的端口类型不符
P202    warning        非幂等副作用（mutate/destructive）的步骤开了重试
P203    warning        缺少运行时能力授权（如果本次声明了能力白名单）
P204    warning        ``in:`` 之类的相对根取值不在该原子支持的范围内
======  ==============  ==================================================
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .atom import Atom
from .contract import type_name, type_ok
from .errors import AtomNotFoundError
from .molecule import Molecule
from .registry import AtomRegistry

TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
_PATH_HEAD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")
SPECIAL_ROOTS = {"env", "secrets", "meta", "vars", "case_id", "run_id"}
#: 引擎/领域包约定可用的「上一步结果」快捷变量
BUILTIN_VARIABLES = {"last_response", "item", "index", "loop", "result", "status"}


@dataclass
class Issue:
    code: str
    level: str  # error | warning
    message: str
    case_id: str = ""
    step_id: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {"code": self.code, "level": self.level, "message": self.message}
        if self.case_id:
            data["case_id"] = self.case_id
        if self.step_id:
            data["step_id"] = self.step_id
        if self.hint:
            data["hint"] = self.hint
        return data

    def __str__(self) -> str:
        where = ".".join(item for item in (self.case_id, self.step_id) if item)
        text = f"[{self.code}] {self.message}"
        if where:
            text = f"{where}: {text}"
        if self.hint:
            text += f"（{self.hint}）"
        return text


@dataclass
class CheckResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def messages(self, *, level: str | None = None) -> list[str]:
        return [str(issue) for issue in self.issues if level is None or issue.level == level]


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def check_plan(
    plan: Any,
    registry: AtomRegistry,
    *,
    known_variables: Iterable[str] = (),
    capabilities: Iterable[str] | None = None,
) -> CheckResult:
    """校验一个计划（``plan.cases`` 里的每个分子）。"""
    result = CheckResult()
    cases = list(getattr(plan, "cases", []) or [])
    if not cases:
        result.issues.append(Issue("P001", "error", "计划中没有任何用例"))
        return result

    env_keys = set(known_variables)
    seen_ids: set[str] = set()
    for case in cases:
        if case.case_id in seen_ids:
            result.issues.append(Issue("P001", "error", f"用例 id 重复: {case.case_id}", case.case_id))
        seen_ids.add(case.case_id)
        if not case.steps:
            result.issues.append(Issue("P001", "error", "用例没有任何步骤", case.case_id))
            continue
        _check_case(case, registry, env_keys, capabilities, result)
    return result


def _check_case(
    case: Molecule,
    registry: AtomRegistry,
    env_keys: set[str],
    capabilities: Iterable[str] | None,
    result: CheckResult,
) -> None:
    # 可用变量：外部变量 + 之前的步骤 id + 之前捕获的变量
    available: set[str] = set(env_keys) | SPECIAL_ROOTS | BUILTIN_VARIABLES
    sugar_owners = _sugar_owners(registry)

    for step in case.steps:
        atom: Atom | None = None
        try:
            atom = registry.get(step.use)
        except AtomNotFoundError:
            result.issues.append(
                Issue("P001", "error", f"原子不存在: {step.use}", case.case_id, step.id,
                      hint="用 `panguverse atoms` 查看可用原子，或检查是否忘了加载领域包")
            )
            available.add(step.id)
            available.update(step.capture or {})
            continue

        # -- P002 必填输入 -------------------------------------------------- #
        for name in atom.required:
            if step.with_.get(name) is None:
                result.issues.append(
                    Issue("P002", "error",
                          f"缺少必填输入 {name}（原子 {atom.name}）", case.case_id, step.id,
                          hint=f"可用输入: {', '.join(sorted(atom.inputs))}")
                )

        # -- P101 模板引用 -------------------------------------------------- #
        # foreach 会注入循环变量（as 指定的名字 + index/loop），先让它们可用
        step_available = set(available)
        if step.foreach:
            step_available.add(step.as_ or "item")
        for location, text in _templates_in(step):
            for root in _template_roots(text):
                if root and root not in step_available:
                    result.issues.append(
                        Issue("P101", "error",
                              f"{location} 引用了未知变量 ${{{root}}}", case.case_id, step.id,
                              hint=_closest(root, step_available))
                    )

        # -- P102 语法糖归属 ------------------------------------------------ #
        semantics = registry.semantics_for(atom)
        own_keys = semantics.sugar_keys()
        carried = _carried_keys(step)  # 未被展开的额外字段（例如写错领域的糖）
        for key, owner in sugar_owners.items():
            if key in carried and key not in own_keys:
                result.issues.append(
                    Issue("P102", "error",
                          f"原子 {atom.name}（{getattr(semantics, 'name', '?')} 领域）不支持 {key}: 语法",
                          case.case_id, step.id,
                          hint=f"{key}: 是 {owner} 领域的写法；本领域可用: {sorted(own_keys) or '无'}")
                )

        # -- P201 字面量类型 ------------------------------------------------- #
        for port in atom.input_ports():
            value = step.with_.get(port.name)
            if value is None or (isinstance(value, str) and "${" in value):
                continue
            if not type_ok(value, port.type):
                result.issues.append(
                    Issue("P201", "warning",
                          f"输入 {port.name} 声明为 {port.type}，实际是 {type_name(value)}",
                          case.case_id, step.id)
                )

        # -- P202 非幂等 + 重试 --------------------------------------------- #
        if step.retry is not None and step.retry.attempts > 1:
            effects = [str(item) for item in atom.effects_for(step.with_)]
            unsafe = {"mutate", "destructive"} & set(effects)
            if unsafe and not atom.retry_safe_for(step.with_):
                result.issues.append(
                    Issue("P202", "warning",
                          f"步骤有 {('/'.join(sorted(unsafe)))} 副作用且开启了重试，可能重复执行",
                          case.case_id, step.id,
                          hint="确认幂等，或用幂等键；自愈器不会对这类原子做自动重试")
                )

        # -- P203 能力授权 -------------------------------------------------- #
        if capabilities is not None and atom.capabilities:
            missing = [cap for cap in atom.capabilities if cap not in set(capabilities)]
            if missing:
                result.issues.append(
                    Issue("P203", "warning",
                          f"原子需要能力 {', '.join(missing)}，但本次未授权", case.case_id, step.id,
                          hint="用 --allow-capabilities 显式授权")
                )

        # -- P204 相对根取值 ------------------------------------------------ #
        roots = semantics.known_input_roots(atom)
        if roots:
            for key in ("in", "in_"):
                value = step.with_.get(key)
                if isinstance(value, str) and value not in roots:
                    result.issues.append(
                        Issue("P204", "warning",
                              f"{key}: {value} 不是该原子支持的取值", case.case_id, step.id,
                              hint=f"可用: {sorted(roots)}")
                    )

        available.add(step.id)
        available.update(step.capture or {})
        # 有些原子的输入「名字即变量名」（set_var.name / json_get.save_as …）
        for key in atom.declares_variables:
            value = step.with_.get(key)
            if isinstance(value, str) and value and "${" not in value:
                available.add(value)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _sugar_owners(registry: AtomRegistry) -> dict[str, str]:
    """全局已知的语法糖键 → 所属领域包（用于「用错领域写法」的友好报错）。"""
    owners: dict[str, str] = {}
    for pack in registry.packs():
        semantics = pack.semantics
        for key in semantics.sugar_keys():
            owners.setdefault(key, pack.name)
    return owners


def _carried_keys(step: Any) -> set[str]:
    """步骤里未被识别的额外字段（``Step.extra`` 或 dict 的键）。"""
    extra = getattr(step, "extra", None)
    if isinstance(extra, Mapping):
        return set(extra)
    if isinstance(step, Mapping):
        return set(step)
    return set()


def _templates_in(step: Any) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = [
        ("with", step.with_),
        ("when", step.when),
        ("foreach", step.foreach),
    ]
    return [(name, value) for name, value in out if value]


def _template_roots(value: Any) -> list[str]:
    """递归找出模板表达式里的根变量名（``${a.b[0]}`` → ``a``）。"""
    roots: list[str] = []
    if isinstance(value, str):
        for match in TEMPLATE_RE.finditer(value):
            expr = match.group(1).split("|", 1)[0].strip()
            head = _PATH_HEAD_RE.match(expr)
            if head:
                root = head.group(1)
                if root not in roots:
                    roots.append(root)
    elif isinstance(value, Mapping):
        for item in value.values():
            for root in _template_roots(item):
                if root not in roots:
                    roots.append(root)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for root in _template_roots(item):
                if root not in roots:
                    roots.append(root)
    return roots


def _closest(name: str, candidates: Iterable[str]) -> str:
    """给出「你是不是想写 X」的提示（简单的相似度，取编辑距离最近的一个）。"""
    def distance(a: str, b: str) -> int:
        previous = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            current = [i]
            for j, cb in enumerate(b, 1):
                current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
            previous = current
        return previous[-1]

    pool = sorted(candidates)
    if not pool:
        return "计划里还没有任何可用的变量"
    ranked = sorted(pool, key=lambda item: distance(name.lower(), item.lower()))
    best = ranked[0]
    if distance(name.lower(), best.lower()) <= max(2, len(name) // 3):
        return f"是否想写 ${{{best}}}？"
    return f"可用变量: {', '.join(pool[:12])}{' …' if len(pool) > 12 else ''}"
