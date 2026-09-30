"""Atom Registry（原子库）：原子的注册、发现与检索。

README 里的「原子库」：内置原子 + 自定义原子 + 分子封成的原子，全部在这里统一寻址。
LLM 规划器也通过 ``catalog()`` 拿到可用原子的结构化清单，避免凭空编造原子。

领域包时代新增的职责：**记住每个原子属于哪个包、用哪套领域语义**。
内核（执行器/自愈器/记忆层）只通过 :meth:`AtomRegistry.semantics_for` 询问语义，
从而不需要认识任何具体领域。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .atom import Atom
from .contract import NEUTRAL_SEMANTICS, AtomSemantics
from .errors import AtomNotFoundError


class AtomRegistry:
    """一个可寻址的原子集合。"""

    def __init__(self, name: str = "registry") -> None:
        self.name = name
        self._atoms: dict[str, Atom] = {}
        self._aliases: dict[str, str] = {}
        self._semantics: dict[str, AtomSemantics] = {}
        self._packs: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def register(
        self,
        item: Atom,
        *,
        replace: bool = False,
        semantics: AtomSemantics | None = None,
    ) -> Atom:
        key = item.name
        if key in self._atoms and not replace:
            raise ValueError(f"原子已存在: {key}（如需覆盖请传 replace=True）")
        self._atoms[key] = item
        for alias in item.aliases:
            self._aliases[alias] = key
        if semantics is not None:
            self._semantics[key] = semantics
        return item

    def register_pack(self, pack: Any, *, replace: bool = True) -> Any:
        """注册一个领域包：它的原子连同领域语义一起进入原子库。"""
        self._packs[pack.name] = pack
        for item in pack.atoms:
            self.register(item, replace=replace or self.has(item.name), semantics=pack.semantics)
        return pack

    def register_all(self, items: Iterable[Atom], *, replace: bool = False) -> AtomRegistry:
        for item in items:
            self.register(item, replace=replace)
        return self

    def unregister(self, name: str) -> None:
        atom = self._atoms.pop(name, None)
        if atom:
            for alias in atom.aliases:
                self._aliases.pop(alias, None)
            self._semantics.pop(atom.name, None)

    # ------------------------------------------------------------------ #
    def semantics_for(self, name: str | Atom) -> AtomSemantics:
        """取原子的领域语义（未登记则回退到中立语义）。"""
        key = name.name if isinstance(name, Atom) else self.resolve_name(str(name))
        return self._semantics.get(key, NEUTRAL_SEMANTICS)

    def pack_of(self, name: str | Atom) -> Any:
        key = name.name if isinstance(name, Atom) else self.resolve_name(str(name))
        for pack in self._packs.values():
            if any(atom.name == key for atom in pack.atoms):
                return pack
        return None

    def packs(self) -> list[Any]:
        return sorted(self._packs.values(), key=lambda pack: pack.name)

    def domains(self) -> list[str]:
        return sorted({atom.domain for atom in self._atoms.values() if atom.domain})

    # ------------------------------------------------------------------ #
    def get(self, name: str) -> Atom:
        key = self._aliases.get(name, name)
        if key not in self._atoms:
            raise AtomNotFoundError(
                f"原子不存在: {name}。可用原子: {', '.join(sorted(self._atoms))}"
            )
        return self._atoms[key]

    def resolve_name(self, name: str) -> str:
        return self._aliases.get(name, name)

    def has(self, name: str) -> bool:
        return self._aliases.get(name, name) in self._atoms

    def names(self) -> list[str]:
        return sorted(self._atoms)

    def list(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        domain: str | None = None,
    ) -> list[Atom]:
        out = []
        for item in self._atoms.values():
            if category and item.category != category:
                continue
            if tag and tag not in item.tags:
                continue
            if domain and item.domain != domain:
                continue
            out.append(item)
        return sorted(out, key=lambda a: (a.domain, a.category, a.name))

    def catalog(self) -> list[dict[str, Any]]:
        """给 LLM 的原子清单（稳定排序，便于 prompt 复用）。"""
        return [atom.spec() for atom in self.list()]

    def extend(self, other: AtomRegistry, *, replace: bool = False) -> AtomRegistry:
        self.register_all(list(other._atoms.values()), replace=replace)
        for pack in other.packs():
            self._packs[pack.name] = pack
        return self

    def __len__(self) -> int:
        return len(self._atoms)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def __iter__(self):
        return iter(self.list())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AtomRegistry {self.name} atoms={len(self._atoms)} packs={len(self._packs)}>"


_DEFAULT: AtomRegistry | None = None


def default_registry(*, reload: bool = False) -> AtomRegistry:
    """进程级默认原子库：包含全部内置领域包的原子（含领域语义）。"""
    global _DEFAULT
    if _DEFAULT is None or reload:
        from .pack import builtin_pack_registry

        reg = AtomRegistry("builtin")
        for pack in builtin_pack_registry(reload=reload).list():
            reg.register_pack(pack)
        _DEFAULT = reg
    return _DEFAULT
