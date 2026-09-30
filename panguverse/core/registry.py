"""Atom Registry（原子库）：原子的注册、发现与检索。

README 里的「原子库」：内置原子 + 自定义原子 + 分子封成的原子，全部在这里统一寻址。
LLM 规划器也通过 ``catalog()`` 拿到可用原子的结构化清单，避免凭空编造原子。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .atom import Atom
from .errors import AtomNotFoundError


class AtomRegistry:
    """一个可寻址的原子集合。"""

    def __init__(self, name: str = "registry") -> None:
        self.name = name
        self._atoms: dict[str, Atom] = {}
        self._aliases: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    def register(self, item: Atom, *, replace: bool = False) -> Atom:
        key = item.name
        if key in self._atoms and not replace:
            raise ValueError(f"原子已存在: {key}（如需覆盖请传 replace=True）")
        self._atoms[key] = item
        for alias in item.aliases:
            self._aliases[alias] = key
        return item

    def register_all(self, items: Iterable[Atom], *, replace: bool = False) -> AtomRegistry:
        for item in items:
            self.register(item, replace=replace)
        return self

    def unregister(self, name: str) -> None:
        atom = self._atoms.pop(name, None)
        if atom:
            for alias in atom.aliases:
                self._aliases.pop(alias, None)

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

    def list(self, *, category: str | None = None, tag: str | None = None) -> list[Atom]:
        out = []
        for item in self._atoms.values():
            if category and item.category != category:
                continue
            if tag and tag not in item.tags:
                continue
            out.append(item)
        return sorted(out, key=lambda a: (a.category, a.name))

    def catalog(self) -> list[dict[str, Any]]:
        """给 LLM 的原子清单（稳定排序，便于 prompt 复用）。"""
        return [atom.spec() for atom in self.list()]

    def extend(self, other: AtomRegistry, *, replace: bool = False) -> AtomRegistry:
        self.register_all(list(other._atoms.values()), replace=replace)
        return self

    def __len__(self) -> int:
        return len(self._atoms)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def __iter__(self):
        return iter(self.list())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AtomRegistry {self.name} atoms={len(self._atoms)}>"


_DEFAULT: AtomRegistry | None = None


def default_registry(*, reload: bool = False) -> AtomRegistry:
    """进程级默认原子库：包含全部内置原子。"""
    global _DEFAULT
    if _DEFAULT is None or reload:
        from ..atoms import register_builtins

        reg = AtomRegistry("builtin")
        register_builtins(reg)
        _DEFAULT = reg
    return _DEFAULT
