"""Atom（原子）：场景化定义的最小操作单元。

对齐 README 的理念：**原子不是全局通用的**，每个领域定义自己的原语。
一个原子 = 带元数据的可调用对象 + 一份**契约**（类型化 I/O、副作用、能力、密钥标记）：

```python
check_http = Atom(
    name="http_request",
    description="发起一次 HTTP 请求",
    inputs={"method": "string", "url": "string"},
    outputs={"status_code": "integer", "body": "any"},
    effects=("read", "write", "mutate"),   # 副作用强度：重试/并发/回滚决策的依据
    capabilities=("network",),             # 需要被授权的运行时能力
    fn=http_request,
)
```

约定：``fn`` 的第一个参数若命名为 ``ctx`` / ``context``，引擎会注入 :class:`RunContext`；
其余参数由计划中的 ``with`` 块解析后传入。返回值应为 ``Mapping``（会被并入上下文）或 ``None``。

**内核不读任何领域知识**：HTTP 状态码、SQL errno、文件系统异常都属于领域包，见
:mod:`panguverse.core.contract` 与 :mod:`panguverse.packs`。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .context import RunContext
from .contract import Effect, Port, ports_from, retry_safe


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

    # ---- 契约（全部可选，缺省表示「未声明」，内核不会替它猜） ---------- #
    domain: str = "generic"
    pack: str = ""
    effects: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    secret_inputs: tuple[str, ...] = ()
    secret_outputs: tuple[str, ...] = ()
    idempotent: bool | None = None
    #: 副作用可能随入参变化（例如 HTTP 方法决定读写）时提供解析器
    effect_resolver: Callable[[Mapping[str, Any]], Iterable[str]] | None = None
    #: 哪些**输入的名字**本身就是「新建的变量名」（例如 ``set_var.name`` / ``json_get.save_as``）。
    #: 静态校验据此知道某个步骤会产出哪些变量，从而避免把正常引用误判成拼写错误。
    declares_variables: tuple[str, ...] = ()

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
        self.effects = tuple(str(effect) for effect in self.effects)
        self.capabilities = tuple(self.capabilities)
        self.secret_inputs = tuple(self.secret_inputs)
        self.secret_outputs = tuple(self.secret_outputs)
        self.declares_variables = tuple(self.declares_variables)

    # ------------------------------------------------------------------ #
    # 契约视图
    # ------------------------------------------------------------------ #
    def input_ports(self) -> tuple[Port, ...]:
        return ports_from(self.inputs, required=self.required, secret=self.secret_inputs)

    def output_ports(self) -> tuple[Port, ...]:
        ports = list(ports_from(self.outputs, required=(), secret=self.secret_outputs))
        for name in self.secret_outputs:
            if all(port.name != name for port in ports):
                ports.append(Port(name=name, type="any", required=False, secret=True))
        return tuple(ports)

    def effects_for(self, inputs: Mapping[str, Any] | None = None) -> tuple[Effect, ...]:
        """解析本次调用的副作用集合（优先用解析器，其次用静态声明）。"""
        if self.effect_resolver is not None:
            try:
                raw = self.effect_resolver(dict(inputs or {}))
            except Exception:  # noqa: BLE001 - 解析失败退回静态声明
                raw = self.effects
        else:
            raw = self.effects
        out: list[Effect] = []
        for item in raw:
            try:
                out.append(item if isinstance(item, Effect) else Effect(str(item)))
            except ValueError:
                continue
        return tuple(out)

    def retry_safe_for(self, inputs: Mapping[str, Any] | None = None) -> bool:
        """重试是否天然安全（只读/幂等）。显式声明 ``idempotent`` 优先。"""
        if self.idempotent is not None:
            return bool(self.idempotent)
        return retry_safe(self.effects_for(inputs))

    def is_destructive(self, inputs: Mapping[str, Any] | None = None) -> bool:
        return Effect.DESTRUCTIVE in self.effects_for(inputs)

    def secrets_in_outputs(self, outputs: Mapping[str, Any] | None) -> list[tuple[str, Any]]:
        """原子自己声明的「输出即密钥」→ 引擎自动打码（不必依赖场景声明）。"""
        out: list[tuple[str, Any]] = []
        if not outputs:
            return out
        wanted = set(self.secret_outputs)
        if not wanted:
            return out
        for key, value in outputs.items():
            if key in wanted:
                out.append((key, value))
        return out

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
        """给 LLM / ``panguverse atoms`` 的结构化描述（含契约信息）。"""
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "domain": self.domain,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "input_ports": [port.to_dict() for port in self.input_ports()],
            "output_ports": [port.to_dict() for port in self.output_ports()],
            "required": list(self.required),
            "tags": list(self.tags),
            "aliases": list(self.aliases),
            "version": self.version,
            "effects": list(self.effects),
        }
        if self.pack:
            data["pack"] = self.pack
        if self.capabilities:
            data["capabilities"] = list(self.capabilities)
        if self.secret_outputs:
            data["secret_outputs"] = list(self.secret_outputs)
        if self.declares_variables:
            data["declares_variables"] = list(self.declares_variables)
        return data

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
    domain: str = "generic",
    pack: str = "",
    effects: Iterable[str] = (),
    capabilities: Iterable[str] = (),
    secret_inputs: Iterable[str] = (),
    secret_outputs: Iterable[str] = (),
    idempotent: bool | None = None,
    effect_resolver: Callable[[Mapping[str, Any]], Iterable[str]] | None = None,
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
            domain=domain,
            pack=pack,
            effects=tuple(effects),
            capabilities=tuple(capabilities),
            secret_inputs=tuple(secret_inputs),
            secret_outputs=tuple(secret_outputs),
            idempotent=idempotent,
            effect_resolver=effect_resolver,
        )

    return decorate


def _wants_ctx(fn: Callable[..., Any]) -> bool:
    try:
        params = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):  # pragma: no cover - 内建函数
        return False
    return bool(params) and params[0] in {"ctx", "context", "run_context"}
