"""Atom Pack（领域包）：平台扩展与共享的唯一载体。

一个领域包 = 一组原子 + 该领域的语义实现 + 一份清单（manifest）。

```
pack = AtomPack(
    name="api_testing", domain="api", version="0.1.0",
    semantics=ApiTestingSemantics(),          # 领域知识全在这里
    atoms=[http_request, assert_status, ...],
    requires=("network",),
)
```

为什么要有包：

- **内核保持领域中立**：重试决策、失败分类、经验标识、计划校验全部走 ``semantics`` 钩子；
- **可共享**：包有名字、版本、能力需求、作者/许可，可以导出成 JSON/YAML 清单被评审、被引用；
- **可声明**：包可以用数据文件（YAML/JSON）定义原子，**不写 Python 也能贡献原子**；
- **可组合**：跨领域包组合时，每个原子仍用它自己领域的语义，互不污染。

声明式包（无需 Python）示例::

    pack:
      name: ops_kit
      domain: ops
      version: 0.1.0
      requires: [filesystem]
    atoms:
      - name: snapshot_config
        description: 把配置复制到备份目录
        inputs: {source: string, target: string}
        outputs: {target: string, bytes: integer}
        steps:
          - id: read
            use: fs_read_text
            with: {path: "${source}"}
          - id: write
            use: fs_write_text
            with: {path: "${target}", text: "${read.text}"}
        outputs_map: {target: "${write.path}", bytes: "${read.size}"}
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atom import Atom
from .contract import NEUTRAL_SEMANTICS, AtomSemantics, Effect
from .errors import AtomFailure, SpecError, StepError, StepFailure
from .molecule import Molecule
from .registry import AtomRegistry

# --------------------------------------------------------------------------- #
# 声明式（数据定义）原子
# --------------------------------------------------------------------------- #
_RUNNER: Any = None


def set_runner(runner: Any) -> None:
    """注册「分子执行器」，供声明式原子在运行期回调（执行器构造时自动调用）。"""
    global _RUNNER
    _RUNNER = runner


def get_runner() -> Any:
    return _RUNNER


def composite_atom(
    *,
    name: str,
    molecule: Molecule,
    description: str = "",
    inputs: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
    output_types: Mapping[str, str] | None = None,
    required_inputs: Sequence[str] = (),
    tags: Iterable[str] = (),
    category: str = "composite",
    domain: str = "generic",
    pack: str = "",
    effects: Iterable[str] = (),
    capabilities: Iterable[str] = (),
    secret_outputs: Iterable[str] = (),
    version: str = "0.1.0",
    runner: Any = None,
) -> Atom:
    """把分子封成一个原子（分子即原子）。

    参数里的两个「输出」含义不同，别混：

    - ``outputs``：**对外输出映射**（``{输出名: "${步骤.字段}"}`` 或字面量），运行期求值；
    - ``output_types``：给 LLM / 静态校验看的**类型声明**（``{输出名: "string"}``）。
    """
    outputs_spec = dict(outputs or {})
    type_hints = dict(output_types or {}) or dict.fromkeys(outputs_spec, "any")
    molecule.meta.setdefault("outputs", outputs_spec)
    molecule.meta.setdefault("output_types", type_hints)
    molecule.meta.setdefault("inputs", dict(inputs or {}))
    molecule.case_id = molecule.case_id or name

    def run_declarative(ctx: Any, **kwargs: Any) -> dict[str, Any]:
        active = runner or get_runner()
        if active is None:
            raise StepError(
                f"声明式原子 {name} 需要在执行器上下文中运行（未找到分子执行器）"
            )
        holder: dict[str, Any] = {}
        case_result = active.run_case(molecule, variables=dict(kwargs), parent=ctx, out=holder)
        if case_result.status in {"failed", "error"}:
            inner = next(
                (step for step in case_result.steps if step.status in {"failed", "error"}), None
            )
            inner_error = (inner.error or {}) if inner is not None else {}
            detail: dict[str, Any] = {
                "case_id": case_result.case_id,
                "steps": len(case_result.steps),
            }
            if inner is not None:
                detail["inner_step"] = inner.step_id
                detail["inner_atom"] = inner.atom
            # 把内部失败的性质透传出来：外层才能用同样的领域语义分类/自愈
            if inner_error.get("failure_kind"):
                detail["failure_kind"] = inner_error["failure_kind"]
            if inner_error.get("failure_class"):
                detail["failure_class"] = inner_error["failure_class"]
            if case_result.status == "failed":
                # 内部是「断言不满足」→ 外层同样是测试失败（failed），不是执行错误
                raise StepFailure(
                    f"组合原子 {name} 失败: {case_result.name}"
                    + (f"（内部 {inner.step_id}: {inner_error.get('message', '')}）" if inner else ""),
                    detail=detail,
                )
            raise AtomFailure(
                f"组合原子 {name} 执行异常: {case_result.name}"
                + (f"（内部 {inner.step_id}: {inner_error.get('message', '')}）" if inner else ""),
                kind=str(inner_error.get("failure_kind") or "composite_failed"),
                failure_class=str(inner_error.get("failure_class") or "internal"),
                detail=detail,
            )
        return collect_molecule_outputs(outputs_spec, case_result, ctx, holder.get("ctx"))

    return Atom(
        name=name,
        fn=run_declarative,
        description=description or molecule.description,
        inputs=dict(inputs or {}),
        outputs=type_hints,
        required=tuple(required_inputs),
        tags=tuple({"composite", *tags}),
        category=category,
        domain=domain,
        pack=pack,
        effects=tuple(effects),
        capabilities=tuple(capabilities),
        secret_outputs=tuple(secret_outputs),
        version=version,
        source="declarative",
    )


def collect_molecule_outputs(
    outputs_spec: Mapping[str, Any],
    case_result: Any,
    parent_ctx: Any,
    child_ctx: Any = None,
) -> dict[str, Any]:
    """按 ``outputs`` 映射从子用例上下文取值（优先用未打码的真实上下文）。"""
    from .errors import PanguError

    if not outputs_spec:
        return {"case_id": case_result.case_id, "status": case_result.status}
    if child_ctx is not None:
        scope = child_ctx
    else:
        snapshot = case_result.variables or {}
        scope = parent_ctx.isolated(**(snapshot.get("vars") or {}))
        scope.env = {**parent_ctx.env, **(snapshot.get("env") or {})}
    resolved: dict[str, Any] = {"status": case_result.status}
    for out_name, expr in outputs_spec.items():
        if isinstance(expr, str) and expr.startswith("${"):
            try:
                resolved[out_name] = scope.resolve(expr)
            except PanguError:
                resolved[out_name] = None
        else:
            resolved[out_name] = expr
    return resolved


# --------------------------------------------------------------------------- #
# 包
# --------------------------------------------------------------------------- #
@dataclass
class AtomPack:
    """一个领域包。"""

    name: str
    domain: str = "generic"
    version: str = "0.1.0"
    description: str = ""
    semantics: AtomSemantics = NEUTRAL_SEMANTICS
    atoms: list[Atom] = field(default_factory=list)
    molecules: list[Molecule] = field(default_factory=list)
    requires: tuple[str, ...] = ()
    author: str = ""
    license: str = ""
    tags: tuple[str, ...] = ()
    source: str = "builtin"  # builtin | local | installed | declarative
    path: str | None = None
    #: 意图模板：自然语言 → 参数化计划（由包提供，内核只收集与打分）
    intent_templates: list[Any] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        self.atoms = list(self.atoms)
        self.molecules = list(self.molecules)
        self.requires = tuple(self.requires)
        self.tags = tuple(self.tags)
        self.intent_templates = list(self.intent_templates)
        # 给原子补上包信息（包名 + 领域），并让步骤语法糖能按包生效
        for atom in self.atoms:
            if not atom.pack:
                atom.pack = self.name
            if atom.domain in {"", "generic"}:
                atom.domain = self.domain
        for template in self.intent_templates:
            if not getattr(template, "pack", ""):
                template.pack = self.name
            if getattr(template, "domain", "generic") in {"", "generic"}:
                template.domain = self.domain

    # ------------------------------------------------------------------ #
    def atom_names(self) -> list[str]:
        return [atom.name for atom in self.atoms]

    def capabilities(self) -> tuple[str, ...]:
        """包级 + 原子级能力需求（去重、保序）。"""
        out: list[str] = list(self.requires)
        for atom in self.atoms:
            for capability in atom.capabilities:
                if capability not in out:
                    out.append(capability)
        return tuple(out)

    def manifest(self) -> dict[str, Any]:
        """可评审、可导出的清单。"""
        return {
            "name": self.name,
            "domain": self.domain,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "license": self.license,
            "tags": list(self.tags),
            "source": self.source,
            "path": self.path,
            "requires": list(self.requires),
            "capabilities": list(self.capabilities()),
            "semantics": getattr(self.semantics, "name", "generic"),
            "atoms": [atom.spec() for atom in self.atoms],
            "intent_templates": [
                template.to_dict() if hasattr(template, "to_dict") else dict(template)
                for template in self.intent_templates
            ],
            "molecules": [molecule.to_dict() for molecule in self.molecules],
        }

    def validate(self) -> list[str]:
        """包级自检：重名、缺描述、能力/副作用未声明等。"""
        problems: list[str] = []
        if not self.name:
            problems.append("包缺少 name")
        seen: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for atom in self.atoms:
            if atom.name in seen:
                problems.append(f"包内原子重名: {atom.name}")
            seen[atom.name] = atom.name
            for alias in atom.aliases:
                if alias in aliases:
                    problems.append(f"别名冲突: {alias}（{aliases[alias]} 与 {atom.name}）")
                aliases[alias] = atom.name
            if not atom.description:
                problems.append(f"原子 {atom.name} 缺少 description")
            if not atom.inputs and not atom.outputs:
                problems.append(f"原子 {atom.name} 没有声明任何输入/输出（契约不完整）")
            for effect in atom.effects:
                if effect not in {item.value for item in Effect}:
                    problems.append(f"原子 {atom.name} 声明了未知副作用: {effect}")
        return problems

    # ------------------------------------------------------------------ #
    @classmethod
    def from_spec(
        cls,
        spec: Mapping[str, Any],
        *,
        source: str = "declarative",
        path: str | None = None,
        semantics: AtomSemantics | None = None,
        atom_factory: Callable[..., Atom] | None = None,
    ) -> AtomPack:
        """从数据（YAML/JSON）构建声明式包。

        ``atom_factory`` 允许调用方替换声明式原子的构建方式（默认 :func:`composite_atom`）。
        """
        if not isinstance(spec, Mapping):
            raise SpecError("包定义必须是映射")
        header = spec.get("pack") or spec
        name = header.get("name")
        if not name:
            raise SpecError("包定义缺少 name")
        pack = cls(
            name=str(name),
            domain=str(header.get("domain") or "generic"),
            version=str(header.get("version") or "0.1.0"),
            description=str(header.get("description") or ""),
            semantics=semantics or NEUTRAL_SEMANTICS,
            requires=tuple(header.get("requires") or ()),
            author=str(header.get("author") or ""),
            license=str(header.get("license") or ""),
            tags=tuple(header.get("tags") or ()),
            source=source,
            path=path,
        )
        factory = atom_factory or composite_atom
        for raw in spec.get("atoms") or []:
            pack.atoms.append(_build_declarative_atom(raw, pack=pack, factory=factory))
        for raw in spec.get("molecules") or []:
            molecule = Molecule.from_spec(dict(raw), source=f"pack:{pack.name}")
            pack.molecules.append(molecule)
        problems = pack.validate()
        if problems:
            where = path or pack.name
            raise SpecError(f"包 {where} 自检不通过:\n- " + "\n- ".join(problems))
        return pack


def _build_declarative_atom(
    raw: Mapping[str, Any], *, pack: AtomPack, factory: Callable[..., Atom]
) -> Atom:
    if not isinstance(raw, Mapping):
        raise SpecError("原子定义必须是映射")
    name = raw.get("name") or raw.get("id")
    if not name:
        raise SpecError("声明式原子缺少 name")
    steps = raw.get("steps")
    if not steps:
        raise SpecError(f"声明式原子 {name} 缺少 steps")
    molecule = Molecule.from_spec(
        {
            "id": raw.get("id") or str(name),
            "name": str(name),
            "description": str(raw.get("description") or ""),
            "tags": list(raw.get("tags") or []),
            "secrets": list(raw.get("secrets") or []),
            "steps": steps,
        },
        source=f"pack:{pack.name}",
    )
    # ``outputs`` 是类型声明（给人和 LLM 看），``outputs_map`` 是求出真实值的映射
    output_types = raw.get("outputs") or {}
    if not isinstance(output_types, Mapping):
        output_types = {}
    outputs_map = raw.get("outputs_map")
    if outputs_map is None:
        # 只声明了类型 → 约定「同名变量」即为输出值
        outputs_map = {str(key): f"${{{key}}}" for key in output_types}
    if not isinstance(outputs_map, Mapping):
        raise SpecError(f"声明式原子 {name} 的 outputs_map 必须是映射")
    inputs = raw.get("inputs") or {}
    required = raw.get("required")
    if required is None and isinstance(inputs, Mapping):
        required = [key for key in inputs if not str(key).endswith("?")]
    return factory(
        name=str(name),
        molecule=molecule,
        description=str(raw.get("description") or ""),
        inputs=inputs if isinstance(inputs, Mapping) else {},
        outputs=outputs_map,
        output_types={str(k): str(v) for k, v in output_types.items()},
        required_inputs=tuple(required or ()),
        tags=tuple(raw.get("tags") or ()),
        category=str(raw.get("category") or "composite"),
        domain=str(raw.get("domain") or pack.domain),
        pack=pack.name,
        effects=tuple(raw.get("effects") or ()),
        capabilities=tuple(raw.get("capabilities") or ()),
        secret_outputs=tuple(raw.get("secret_outputs") or ()),
        version=str(raw.get("version") or pack.version),
    )


# --------------------------------------------------------------------------- #
# 包仓库
# --------------------------------------------------------------------------- #
class PackRegistry:
    """所有已知领域包的集合，并负责组装带语义的原子库。"""

    def __init__(self) -> None:
        self._packs: dict[str, AtomPack] = {}

    # ------------------------------------------------------------------ #
    def register(self, pack: AtomPack, *, replace: bool = False) -> AtomPack:
        if pack.name in self._packs and not replace:
            raise ValueError(f"领域包已存在: {pack.name}（如需覆盖请传 replace=True）")
        self._packs[pack.name] = pack
        return pack

    def get(self, name: str) -> AtomPack:
        if name not in self._packs:
            raise KeyError(f"领域包不存在: {name}；已知: {', '.join(sorted(self._packs))}")
        return self._packs[name]

    def has(self, name: str) -> bool:
        return name in self._packs

    def list(self, *, domain: str | None = None) -> list[AtomPack]:
        packs = sorted(self._packs.values(), key=lambda item: item.name)
        if domain:
            packs = [pack for pack in packs if pack.domain == domain]
        return packs

    def domains(self) -> list[str]:
        return sorted({pack.domain for pack in self._packs.values()})

    def atoms(self) -> list[Atom]:
        out: list[Atom] = []
        for pack in self.list():
            out.extend(pack.atoms)
        return out

    def pack_of(self, atom_name: str) -> AtomPack | None:
        for pack in self.list():
            if any(atom.name == atom_name or atom_name in atom.aliases for atom in pack.atoms):
                return pack
        return None

    def semantics_for(self, atom_name: str) -> AtomSemantics:
        pack = self.pack_of(atom_name)
        return pack.semantics if pack else NEUTRAL_SEMANTICS

    def capabilities(self) -> tuple[str, ...]:
        out: list[str] = []
        for pack in self.list():
            for capability in pack.capabilities():
                if capability not in out:
                    out.append(capability)
        return tuple(out)

    def validate(self, *, strict: bool = False) -> list[str]:
        """跨包校验：原子重名/别名冲突，以及（严格模式）能力冲突。"""
        problems: list[str] = []
        names: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for pack in self.list():
            for atom in pack.atoms:
                if atom.name in names:
                    problems.append(f"原子重名: {atom.name}（{names[atom.name]} 与 {pack.name}）")
                names[atom.name] = pack.name
                for alias in atom.aliases:
                    if alias in aliases:
                        problems.append(f"别名冲突: {alias}（{aliases[alias]} 与 {pack.name}）")
                    aliases[alias] = pack.name
            problems.extend(f"[{pack.name}] {item}" for item in pack.validate())
        if strict:
            for pack in self.list():
                if not pack.version:
                    problems.append(f"[{pack.name}] 缺少版本号")
                if not pack.description:
                    problems.append(f"[{pack.name}] 缺少描述")
        return problems

    def manifest(self) -> dict[str, Any]:
        return {
            "version": 1,
            "domains": self.domains(),
            "capabilities": list(self.capabilities()),
            "atom_count": len(self.atoms()),
            "packs": [pack.manifest() for pack in self.list()],
        }

    def build_atom_registry(self, base: AtomRegistry | None = None) -> AtomRegistry:
        """组装原子库，并把每个原子的领域语义一并挂上。"""
        registry = base or AtomRegistry("platform")
        for pack in self.list():
            registry.register_pack(pack)
        return registry

    def export(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def __len__(self) -> int:
        return len(self._packs)

    def __iter__(self):
        return iter(self.list())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PackRegistry packs={sorted(self._packs)}>"


# --------------------------------------------------------------------------- #
# 内置包发现
# --------------------------------------------------------------------------- #
def builtin_pack_registry(*, reload: bool = False) -> PackRegistry:
    """加载内置领域包（``panguverse.packs``）。"""
    cache_key = "_builtin_pack_registry"
    if not reload:
        cached = globals().get(cache_key)
        if cached is not None:
            return cached
    from ..packs import builtin_packs

    registry = PackRegistry()
    for pack in builtin_packs():
        registry.register(pack, replace=True)
    globals()[cache_key] = registry
    return registry


def load_pack_file(path: str | Path, *, source: str = "local") -> AtomPack:
    """加载一个声明式包文件（``.yaml`` / ``.yml`` / ``.json``）。"""
    from ..spec import miniyaml

    target = Path(path)
    if not target.exists():
        raise SpecError(f"包文件不存在: {target}")
    text = target.read_text(encoding="utf-8")
    data = json.loads(text) if target.suffix.lower() == ".json" else miniyaml.load(text)
    return AtomPack.from_spec(data, source=source, path=str(target))


def load_pack_dir(path: str | Path, *, source: str = "local") -> list[AtomPack]:
    """加载目录下所有 ``*.pack.yaml`` / ``*.pack.json``。"""
    base = Path(path)
    files = sorted([*base.glob("*.pack.yaml"), *base.glob("*.pack.yml"), *base.glob("*.pack.json")])
    return [load_pack_file(item, source=source) for item in files]
