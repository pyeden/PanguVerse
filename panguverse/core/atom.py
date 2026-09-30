"""Atom（原子）：场景化定义的最小操作单元。

对齐 README 的理念：**原子不是全局通用的**，每个领域定义自己的原语。
在 API 自动化测试这个领域里，原子就是
``http_request`` / ``assert_status`` / ``json_get`` 这类最小动作。

一个原子就是一个带元数据的可调用对象：

```python
check_http = Atom(
    name="http_request",
    description="发起一次 HTTP 请求",
    inputs={"method": "string", "url": "string"},
    outputs={"status_code": "int", "body": "any"},
    fn=http_request,
)
```

约定：``fn`` 的第一个参数若命名为 ``ctx`` / ``context``，引擎会注入 :class:`RunContext`；
其余参数由计划中的 ``with`` 块解析后传入。返回值应为 ``Mapping``（会被并入上下文）或 ``None``。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .context import RunContext


@dataclass
class Atom:
    """最小操作单元。[复现] ``Atom(...)`` 或 ``@atom(...)`` 二选一。"""

    name: str
    fn: Callable[..., Any]
    description: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    required: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()
    category: str = "generic"
    aliases: tuple[str, ...] = ()
    wants_ctx: bool = False
    version: str = "0.1.0"
    source: str = "builtin"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Atom 必须有 name")
        if callable(self.fn):
            self.wants_ctx = _wants_ctx(self.fn)
        # ``required=None`` 表示「按 inputs 里的名字推导（带 ? 视为可选）」；
        # 显式传空元组表示「没有任何必填输入」。
        if self.required is None:
            self.required = tuple(name for name in self.inputs if not name.endswith("?"))
        else:
            self.required = tuple(self.required)
        self.tags = tuple(self.tags)
        self.aliases = tuple(self.aliases)

    # ------------------------------------------------------------------ #
    def invoke(self, ctx: RunContext, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """执行原子。``inputs`` 应已由引擎解析完模板。"""
        kwargs = self._normalize_inputs(inputs)
        try:
            result = self.fn(ctx, **kwargs) if self.wants_ctx else self.fn(**kwargs)
        except TypeError as exc:  # 参数不匹配 → 计划有问题，属于 error
            raise TypeError(f"原子 {self.name} 调用失败({exc})；输入={sorted(inputs)}") from exc
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            return {"result": result}
        return dict(result)

    def accepted_params(self) -> set[str]:
        try:
            params = set(inspect.signature(self.fn).parameters)
        except (TypeError, ValueError):  # pragma: no cover
            return set()
        if self.wants_ctx:
            params.discard("ctx")
            params.discard("context")
            params.discard("run_context")
        return params

    def _normalize_inputs(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """把 Python 关键字冲突的名字映射回函数签名（``in`` → ``in_``、``type`` → ``type_``）。"""
        params = self.accepted_params()
        out: dict[str, Any] = {}
        for key, value in inputs.items():
            if key in params:
                out[key] = value
            elif f"{key}_" in params:
                out[f"{key}_"] = value
            else:
                out[key] = value
        return out


    def spec(self) -> dict[str, Any]:
        """给 LLM / ``panguverse atoms`` 的结构化描述。"""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "required": list(self.required),
            "tags": list(self.tags),
            "aliases": list(self.aliases),
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"Atom({self.name})"


def atom(
    name: str | None = None,
    *,
    description: str = "",
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
    required: Iterable[str] | None = None,
    tags: Iterable[str] = (),
    category: str = "generic",
    aliases: Iterable[str] = (),
    source: str = "builtin",
) -> Callable[[Callable[..., Any]], Atom]:
    """装饰器写法：``@atom("http_request", ...) def http_request(ctx, url): ...``"""

    def decorate(fn: Callable[..., Any]) -> Atom:
        doc = (inspect.getdoc(fn) or "").strip()
        first_line = doc.splitlines()[0] if doc else ""
        return Atom(
            name=name or fn.__name__,
            fn=fn,
            description=description or first_line,
            inputs=dict(inputs or {}),
            outputs=dict(outputs or {}),
            required=tuple(required) if required is not None else None,
            tags=tuple(tags),
            category=category,
            aliases=tuple(aliases),
            source=source,
        )

    return decorate


def _wants_ctx(fn: Callable[..., Any]) -> bool:
    try:
        params = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):  # pragma: no cover - 内建函数
        return False
    return bool(params) and params[0] in {"ctx", "context", "run_context"}
