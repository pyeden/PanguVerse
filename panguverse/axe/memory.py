"""记忆与沉淀：工作区、回归原子库、经验库（lessons）。

这是闭环里「执行 → 观察 → 沉淀 → 进化」的落点：

- **沉淀**：跑通的用例 ``seal`` 成回归原子，下次不必重新规划；
- **经验**：失败按 ``原子:失败类型`` 归类成 lessons，供自愈器与规划器复用；
- **可追溯**：每次运行的产物（计划/轨迹/报告）都落在工作区 ``runs/<run_id>/``。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.molecule import Molecule

WORKSPACE_DIRNAME = ".panguverse"


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #
class Workspace:
    """``.panguverse/`` 目录：所有跨运行的持久化状态都放在这里。"""

    def __init__(self, root: str | os.PathLike[str] = WORKSPACE_DIRNAME) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ #
    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    @property
    def lessons_path(self) -> Path:
        return self.root / "lessons.jsonl"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def scenarios_dir(self) -> Path:
        return self.root / "scenarios"

    def ensure(self) -> Workspace:
        for path in (self.root, self.runs_dir, self.scenarios_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def run_dir(self, run_id: str, *, create: bool = True) -> Path:
        path = self.runs_dir / run_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, relative: str, data: Any) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_json(self, relative: str, default: Any = None) -> Any:
        path = self.root / relative
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return default

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workspace {self.root}>"


# --------------------------------------------------------------------------- #
# 回归原子库（sealed molecules）
# --------------------------------------------------------------------------- #
@dataclass
class SealedRecord:
    name: str
    molecule: dict[str, Any]
    version: int = 1
    tags: list[str] = field(default_factory=list)
    source_run: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    run_count: int = 0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "tags": list(self.tags),
            "source_run": self.source_run,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_count": self.run_count,
            "description": self.description,
            "molecule": self.molecule,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SealedRecord:
        return cls(
            name=str(data["name"]),
            molecule=dict(data.get("molecule") or {}),
            version=int(data.get("version", 1)),
            tags=list(data.get("tags") or []),
            source_run=str(data.get("source_run", "")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            run_count=int(data.get("run_count", 0)),
            description=str(data.get("description", "")),
        )


class SealedStore:
    """回归用例库：跑通的用例存这里，``regression`` 命令直接复用。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._items: dict[str, SealedRecord] | None = None

    # ------------------------------------------------------------------ #
    def load(self, *, reload: bool = False) -> dict[str, SealedRecord]:
        if self._items is None or reload:
            raw = self.workspace.read_json("registry.json", default={"items": {}}) or {}
            items = raw.get("items") if isinstance(raw, dict) else {}
            self._items = {name: SealedRecord.from_dict(data) for name, data in (items or {}).items()}
        return self._items

    def save(self, molecule: Molecule, *, tags: Iterable[str] = (), source_run: str = "", name: str | None = None) -> SealedRecord:
        items = self.load()
        key = name or molecule.case_id or molecule.name
        existing = items.get(key)
        record = SealedRecord(
            name=key,
            molecule=molecule.to_dict(),
            version=(existing.version + 1) if existing else 1,
            tags=sorted({*(existing.tags if existing else []), *tags, *molecule.tags}),
            source_run=source_run or (existing.source_run if existing else ""),
            created_at=existing.created_at if existing else time.time(),
            updated_at=time.time(),
            run_count=existing.run_count if existing else 0,
            description=molecule.description,
        )
        items[key] = record
        self._flush()
        return record

    def get(self, name: str) -> SealedRecord | None:
        return self.load().get(name)

    def list(self, *, tag: str | None = None) -> list[SealedRecord]:
        items = sorted(self.load().values(), key=lambda r: r.name)
        if tag:
            items = [r for r in items if tag in r.tags]
        return items

    def to_molecules(self, *, tag: str | None = None) -> list[Molecule]:
        molecules: list[Molecule] = []
        for record in self.list(tag=tag):
            spec = dict(record.molecule)
            spec.setdefault("case_id", record.name)
            molecule = Molecule.from_spec(spec, source="memory")
            molecule.meta["sealed_version"] = record.version
            molecule.meta["sealed_source_run"] = record.source_run
            molecules.append(molecule)
        return molecules

    def mark_run(self, names: Iterable[str]) -> None:
        items = self.load()
        changed = False
        for name in names:
            if name in items:
                items[name].run_count += 1
                items[name].updated_at = time.time()
                changed = True
        if changed:
            self._flush()

    def clear(self) -> None:
        self._items = {}
        self._flush()

    def _flush(self) -> None:
        self.workspace.ensure()
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "items": {name: record.to_dict() for name, record in self.load().items()},
        }
        self.workspace.registry_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# 经验库（lessons）
# --------------------------------------------------------------------------- #
@dataclass
class Lesson:
    signature: str
    kind: str
    atom: str = ""
    target: str = ""
    failure_class: str = ""
    case_id: str = ""
    step_id: str = ""
    message: str = ""
    suggestion: str = ""
    fix: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "kind": self.kind,
            "atom": self.atom,
            "target": self.target,
            "failure_class": self.failure_class,
            "case_id": self.case_id,
            "step_id": self.step_id,
            "message": self.message,
            "suggestion": self.suggestion,
            "fix": self.fix,
            "run_id": self.run_id,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lesson:
        return cls(
            signature=str(data.get("signature", "")),
            kind=str(data.get("kind", "")),
            atom=str(data.get("atom", "")),
            target=str(data.get("target", "")),
            failure_class=str(data.get("failure_class", "")),
            case_id=str(data.get("case_id", "")),
            step_id=str(data.get("step_id", "")),
            message=str(data.get("message", "")),
            suggestion=str(data.get("suggestion", "")),
            fix=dict(data.get("fix") or {}),
            run_id=str(data.get("run_id", "")),
            ts=float(data.get("ts", time.time())),
        )

    def describe(self) -> str:
        where = f"{self.atom}@{self.target}" if self.target else (self.atom or self.case_id)
        label = self.kind or self.failure_class or "unknown"
        text = f"[{label}] {where}: {self.message}"
        if self.suggestion:
            text += f" → 建议: {self.suggestion}"
        return text


class LessonStore:
    """append-only 的经验库；同签名会累积 ``count``，便于判断「老毛病又犯了」。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def all(self) -> list[Lesson]:
        path = self.workspace.lessons_path
        if not path.exists():
            return []
        lessons: list[Lesson] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lessons.append(Lesson.from_dict(json.loads(line)))
            except ValueError:
                continue
        return lessons

    def record(
        self,
        *,
        kind: str,
        atom: str = "",
        target: str = "",
        failure_class: str = "",
        case_id: str = "",
        step_id: str = "",
        message: str = "",
        suggestion: str = "",
        fix: dict[str, Any] | None = None,
        run_id: str = "",
    ) -> Lesson:
        lesson = Lesson(
            signature=signature_of(atom=atom, kind=kind, target=target),
            kind=kind,
            atom=atom,
            target=target,
            failure_class=failure_class,
            case_id=case_id,
            step_id=step_id,
            message=message,
            suggestion=suggestion,
            fix=dict(fix or {}),
            run_id=run_id,
        )
        self.workspace.ensure()
        with self.workspace.lessons_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(lesson.to_dict(), ensure_ascii=False) + "\n")
        return lesson

    def recall(
        self,
        *,
        kind: str | None = None,
        failure_class: str | None = None,
        atom: str = "",
        target: str | None = None,
    ) -> Lesson | None:
        """按「原子 + 作用对象 + 失败类型/类别」回忆最近一次学到的东西。

        - ``target`` 指定时**必须精确命中作用对象**（HTTP 接口 / 文件路径 / 表名…），
          否则会把 A 对象的经验套到 B 对象上；传 ``None`` 表示未指定，允许退回同原子经验；
        - ``kind`` 是领域标签，``failure_class`` 是中立类别；用中立类别检索能让经验跨领域复用
          （``transient`` 在任何行业都成立）。
        """
        lessons = self.all()

        def matches(item: Lesson) -> bool:
            if kind is not None and item.kind != kind:
                return False
            if failure_class is not None and item.failure_class != failure_class:
                return False
            return not (atom and item.atom != atom)

        candidates = [item for item in lessons if matches(item)]
        if target is not None:
            exact = [item for item in candidates if item.target == target]
            return exact[-1] if exact else None
        return candidates[-1] if candidates else None

    def count(self) -> int:
        return len(self.all())

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for lesson in self.all():
            key = lesson.kind or lesson.failure_class or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts


def signature_of(*, atom: str, kind: str, target: str = "") -> str:
    """经验签名：``原子@作用对象:失败类型``（对象为空时退化为 ``原子@:失败类型``）。"""
    return f"{atom or '*'}{'@' + target if target else '@'}:{kind}"


def step_target(step: Any, semantics: Any = None) -> str:
    """取步骤的作用对象标识（由领域包语义决定：URL 路径 / 文件路径 / 表名…）。

    优先用执行期已算好的 ``StepResult.target``；只有拿不到语义时返回空字符串。
    """
    cached = getattr(step, "target", None)
    if isinstance(cached, str) and cached:
        return cached
    if semantics is None:
        return ""
    with_block = getattr(step, "with_", None)
    if with_block is None and isinstance(step, dict):
        with_block = step.get("with") or {}
    if with_block is None and isinstance(step, dict):
        with_block = step
    if not isinstance(with_block, Mapping):
        return ""
    return semantics.target(None, with_block)


# --------------------------------------------------------------------------- #
# 进化：把一次运行的结果沉淀成资产
# --------------------------------------------------------------------------- #
class Evolution:
    """一次运行结束后：跑通的用例 → 回归原子；失败 → 经验。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.sealed = SealedStore(workspace)
        self.lessons = LessonStore(workspace)

    # ------------------------------------------------------------------ #
    def evolve(self, run: Any, *, seal: bool = True, tag: str = "regression") -> dict[str, Any]:
        """``run`` 为 :class:`~panguverse.engine.result.RunResult`。"""
        specs = {case.get("case_id"): case for case in (run.plan.get("cases") or [])}
        sealed: list[dict[str, Any]] = []
        lessons: list[dict[str, Any]] = []

        if seal:
            for case in run.cases:
                if case.status != "passed":
                    continue
                spec = specs.get(case.case_id)
                if not spec:
                    continue
                molecule = Molecule.from_spec(dict(spec), source="memory")
                molecule.tags = tuple({*molecule.tags, tag})
                record = self.sealed.save(molecule, tags=[tag], source_run=run.run_id)
                sealed.append(
                    {"name": record.name, "version": record.version, "tags": record.tags}
                )

        for case in run.cases:
            for step in case.steps:
                # 作用对象标识由执行期按领域语义算好（HTTP 接口路径 / 文件路径 / 表名…）
                target = step.target or ""
                failure_class = (step.error or {}).get("failure_class", "")
                if step.status not in ("failed", "error"):
                    # 重试后成功的步骤同样是「观察」：把已验证的重试策略沉淀成经验，
                    # 供后续计划自动补强（见 apply_lessons_to_plan）
                    if step.status == "passed" and step.retries > 0:
                        lesson = self.lessons.record(
                            kind=(step.error or {}).get("failure_kind") or "transient",
                            atom=step.atom,
                            target=target,
                            failure_class="transient",
                            case_id=case.case_id,
                            step_id=step.step_id,
                            message=f"{step.atom}@{target or '-'} 抖动后重试 {step.retries} 次成功",
                            suggestion="历史验证过：该对象遇到瞬态故障需要退避重试",
                            fix={
                                "retry": {
                                    "attempts": max(2, step.retries + 1),
                                    "delay_ms": 100,
                                    "backoff": 2.0,
                                    "on": ["transient"],
                                }
                            },
                            run_id=run.run_id,
                        )
                        lessons.append(lesson.to_dict())
                    continue
                kind = (step.error or {}).get("failure_kind") or (step.error or {}).get("kind", "unknown")
                suggestion = ""
                for event in step.heal:
                    suggestion = event.suggestion or suggestion
                # 瞬态故障的失败先给出「可验证的修复假设」，下次规划自动套用（进化闭环）。
                # 判据用**中立类别**（transient），因此对 HTTP、文件、数据库等任何领域都成立。
                fix: dict[str, Any] = {}
                if failure_class == "transient":
                    fix = {
                        "retry": {
                            "attempts": 3,
                            "delay_ms": 200,
                            "backoff": 2.0,
                            "on": ["transient"],
                        }
                    }
                    suggestion = suggestion or "瞬态故障：建议对该对象启用重试（已沉淀为待验证策略）"
                lesson = self.lessons.record(
                    kind=kind,
                    atom=step.atom,
                    target=target,
                    failure_class=failure_class,
                    case_id=case.case_id,
                    step_id=step.step_id,
                    message=step.message or (step.error or {}).get("message", ""),
                    suggestion=suggestion,
                    fix=fix,
                    run_id=run.run_id,
                )
                lessons.append(lesson.to_dict())

        return {
            "sealed": sealed,
            "sealed_count": len(sealed),
            "lessons": lessons,
            "lesson_count": len(lessons),
            "regression_size": len(self.sealed.list()),
        }

    def regression_molecules(self) -> list[Molecule]:
        return self.sealed.to_molecules(tag="regression")
