"""内置原子库：API 自动化测试领域的第一批原语。

对齐 README：原子**针对场景定义**。这里是 API 测试领域的场景原语集合，
其他领域（运维、财务……）可以照这个模式自带原子库。
"""

from __future__ import annotations

from ..core.atom import Atom
from ..core.registry import AtomRegistry
from . import assertions, extract, flow, http

_GROUPS = (http, extract, assertions, flow)


def builtin_atoms() -> list[Atom]:
    """返回全部内置原子（保持模块内定义顺序）。"""
    items: list[Atom] = []
    for module in _GROUPS:
        for value in vars(module).values():
            if isinstance(value, Atom):
                items.append(value)
    return items


def register_builtins(registry: AtomRegistry) -> AtomRegistry:
    for item in builtin_atoms():
        if not registry.has(item.name):
            registry.register(item)
    return registry


__all__ = ["builtin_atoms", "register_builtins"]
