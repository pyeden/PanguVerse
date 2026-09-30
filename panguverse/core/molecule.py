"""Molecule（分子）：原子的组合；``.seal()`` 后成为一个新原子。

分子 = 一个可执行的步骤序列（API 测试里就是一个测试用例）。
它同时是「用例」和「可复用组件」——这正是 README 里
``原子 → 分子 → 高分子`` 递归组合的落地方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import PlanError


@dataclass
class RetryPolicy:
    """步骤级重试策略（自愈的第一层：确定性重试）。"""

    attempts: int = 1
    delay_ms: int = 0
    backoff: float = 1.0
    on: tuple[str, ...] = ("network", "timeout", "server_error")

    def should_retry(self, kind: str) -> bool:
        if self.attempts <= 1:
            return False
        return "any" in self.on or kind in self.on

    def delay_for(self, attempt: int) -> float:
        if self.delay_ms <= 0:
            return 0.0
        return (self.delay_ms / 1000.0) * (self.backoff ** max(0, attempt - 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "delay_ms": self.delay_ms,
            "backoff": self.backoff,
            "on": list(self.on),
        }

    @classmethod
    def from_spec(cls, spec: Any) -> RetryPolicy | None:
        if spec is None or spec is False:
            return None
        if spec is True:
            return cls(attempts=3, delay_ms=200, backoff=2.0)
        if isinstance(spec, int):
            return cls(attempts=max(1, spec), delay_ms=200, backoff=2.0)
        if isinstance(spec, dict):
            return cls(
                attempts=int(spec.get("attempts", spec.get("times", 3))),
                delay_ms=int(spec.get("delay_ms", spec.get("delay", 200))),
                backoff=float(spec.get("backoff", 2.0)),
                on=tuple(spec.get("on", ("network", "timeout", "server_error"))),
            )
        raise PlanError(f"非法的 retry 配置: {spec!r}")


@dataclass
class Step:
    """分子中的一个步骤。"""

    id: str
    use: str
    with_: dict[str, Any] = field(default_factory=dict)
    capture: dict[str, Any] = field(default_factory=dict)
    when: str | None = None
    foreach: str | None = None
    as_: str = "item"
    name: str = ""
    description: str = ""
    retry: RetryPolicy | None = None
    continue_on_failure: bool = False
    tags: tuple[str, ...] = ()
    optional: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "use": self.use}
        if self.name:
            data["name"] = self.name
        if self.description:
            data["description"] = self.description
        if self.with_:
            data["with"] = self.with_
        if self.capture:
            data["capture"] = self.capture
        if self.when:
            data["when"] = self.when
        if self.foreach:
            data["foreach"] = self.foreach
            data["as"] = self.as_
        if self.retry:
            data["retry"] = self.retry.to_dict()
        if self.continue_on_failure:
            data["continue_on_failure"] = True
        if self.tags:
            data["tags"] = list(self.tags)
        if self.optional:
            data["optional"] = True
        return data

    @classmethod
    def from_spec(cls, spec: dict[str, Any], *, index: int = 0, default_prefix: str = "step") -> Step:
        if not isinstance(spec, dict):
            raise PlanError(f"步骤必须是映射，实际为 {type(spec).__name__}")
        use = spec.get("use") or spec.get("atom") or spec.get("molecule")
        if not use:
            raise PlanError(f"步骤缺少 use/atom 字段: {spec!r}")
        step_id = str(spec.get("id") or spec.get("name") or f"{default_prefix}_{index + 1}")
        return cls(
            id=step_id,
            use=str(use),
            with_=dict(spec.get("with") or spec.get("inputs") or {}),
            capture=dict(spec.get("capture") or {}),
            when=spec.get("when") or spec.get("if"),
            foreach=spec.get("foreach") or spec.get("each"),
            as_=str(spec.get("as") or "item"),
            name=str(spec.get("name") or ""),
            description=str(spec.get("description") or ""),
            retry=RetryPolicy.from_spec(spec.get("retry")),
            continue_on_failure=bool(spec.get("continue_on_failure", False)),
            tags=tuple(spec.get("tags") or ()),
            optional=bool(spec.get("optional", False)),
            extra={
                k: v
                for k, v in spec.items()
                if k
                not in {
                    "id",
                    "name",
                    "description",
                    "use",
                    "atom",
                    "molecule",
                    "with",
                    "inputs",
                    "capture",
                    "when",
                    "if",
                    "foreach",
                    "each",
                    "as",
                    "retry",
                    "continue_on_failure",
                    "tags",
                    "optional",
                }
            },
        )


@dataclass
class Molecule:
    """原子/分子的有序组合，可直接作为一个测试用例执行。"""

    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    case_id: str = ""
    tags: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    secrets: tuple[str, ...] = ()
    source: str = "local"
    owner: Any = None  # 由 PanguAxe/Executor 注入，使 seal() 可无参调用
    version: str = "0.1.0"

    def __post_init__(self) -> None:
        self.tags = tuple(self.tags)
        self.secrets = tuple(self.secrets)
        if not self.case_id:
            self.case_id = _slug(self.name)

    # ------------------------------------------------------------------ #
    def add(self, *steps: Step) -> Molecule:
        self.steps.extend(steps)
        return self

    def seal(self, owner: Any = None) -> Any:
        """把分子封成新原子（注册进原子库），从而可被更高层递归组合。"""
        target = owner or self.owner
        if target is None:
            raise PlanError(
                "seal() 需要执行器：请使用 molecule.seal(axe) 或由 axe.compose() 创建分子"
            )
        return target.seal_molecule(self)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "case_id": self.case_id,
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.description:
            data["description"] = self.description
        if self.tags:
            data["tags"] = list(self.tags)
        if self.secrets:
            data["secrets"] = list(self.secrets)
        if self.meta:
            data["meta"] = self.meta
        return data

    @classmethod
    def from_spec(cls, spec: dict[str, Any], *, source: str = "local") -> Molecule:
        if not isinstance(spec, dict):
            raise PlanError(f"用例必须是映射，实际为 {type(spec).__name__}")
        name = spec.get("name") or spec.get("id") or spec.get("case_id")
        if not name:
            raise PlanError(f"用例缺少 name/id 字段: {spec!r}")
        steps = [
            Step.from_spec(raw, index=i, default_prefix=str(spec.get("id") or spec.get("name") or "step"))
            for i, raw in enumerate(spec.get("steps") or [])
        ]
        if not steps:
            raise PlanError(f"用例 {name} 没有任何步骤")
        ids = [s.id for s in steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise PlanError(f"用例 {name} 存在重复步骤 id: {sorted(dupes)}")
        return cls(
            name=str(name),
            case_id=str(spec.get("case_id") or spec.get("id") or _slug(str(name))),
            description=str(spec.get("description") or ""),
            steps=steps,
            tags=tuple(spec.get("tags") or ()),
            meta=dict(spec.get("meta") or {}),
            secrets=tuple(spec.get("secrets") or ()),
            source=source,
        )

    def __len__(self) -> int:
        return len(self.steps)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Molecule {self.case_id} steps={len(self.steps)}>"


def _slug(text: str) -> str:
    out = []
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {" ", "-", "_", "/"}:
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "case"
