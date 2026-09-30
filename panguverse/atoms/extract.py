"""提取原子：从响应里取值并写回上下文，串起「登录 → 拿 token → 下单」这类链路。"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.atom import atom
from ..core.context import MISSING, RunContext, split_path, walk_path
from ..core.errors import StepError


def resolve_source(ctx: RunContext, source: Any, *, default_key: str = "last_response") -> Any:
    """解析断言/提取的取值来源。

    ``source`` 为空时取 :func:`http_request` 写入的 ``last_response``。
    """
    if source is None:
        value = ctx.vars.get(default_key)
        if value is None:
            raise StepError(
                f"没有可用的 {default_key}：请先用 http_request 发请求，或在步骤里显式指定 source"
            )
        return value
    return source


def lookup(source: Any, path: str | None, *, in_: str | None = "body") -> Any:
    """按路径取值；``in_`` 指定相对根（body/headers/status_code/response）。

    ``path`` 为空时：默认返回 ``in_`` 指定的那一层（HTTP 响应 → 响应体），
    想看整个响应对象请用 ``in: response``。
    """
    if not path:
        if in_ and in_ != "response" and isinstance(source, dict) and in_ in source:
            return source[in_]
        return source
    parts = split_path(str(path))
    target = source
    if in_ and in_ != "response" and (not parts or parts[0] not in _KNOWN_ROOTS):
        if isinstance(source, dict) and in_ in source:
            target = source.get(in_)
        elif in_ == "body":
            target = source if not isinstance(source, dict) else source.get("body")
    value = walk_path(target, parts)
    if value is MISSING:
        raise KeyError(path)
    return value


_KNOWN_ROOTS = {"body", "headers", "status_code", "text", "elapsed_ms", "request", "ok", "reason"}


@atom(
    "json_get",
    description="按路径从响应体（或任意对象）中取值，可存为变量",
    category="extract",
    tags=("extract", "core"),
    aliases=("extract", "get"),
    inputs={
        "path": "string，如 data.items[0].id",
        "source": "any（默认 last_response）",
        "in": "string=body|headers|response",
        "default": "any（缺省值；给了就不报错）",
        "required": "bool=false",
        "save_as": "string（写入变量名）",
    },
    outputs={"value": "any"},
    required=("path",),
)
def json_get(
    ctx: RunContext,
    path: str,
    source: Any = None,
    in_: str = "body",
    default: Any = None,
    required: bool = False,
    save_as: str | None = None,
) -> dict[str, Any]:
    if source is None:
        source = ctx.vars.get("last_response")
    try:
        value = lookup(source, path, in_=in_)
    except (KeyError, TypeError, IndexError) as exc:
        if default is not None:
            value = default
        elif required:
            raise StepError(f"响应中不存在路径 {path!r}") from exc
        else:
            value = None
            ctx.emit("extract_missing", path=path)
    if save_as:
        ctx.set(save_as, value)
    return {"value": value}


@atom(
    "set_var",
    description="把值写入上下文变量（值可以是模板表达式）",
    category="extract",
    tags=("extract",),
    aliases=("set", "var"),
    inputs={"name": "string", "value": "any"},
    outputs={},
    required=("name",),
)
def set_var(ctx: RunContext, name: str, value: Any = None) -> dict[str, Any]:
    ctx.set(str(name), value)
    return {}


@atom(
    "regex_extract",
    description="用正则从文本（默认响应原文）中提取内容",
    category="extract",
    tags=("extract",),
    inputs={"pattern": "string", "source": "string（默认响应 text）", "group": "int=1", "save_as": "string"},
    outputs={"value": "string"},
    required=("pattern",),
)
def regex_extract(
    ctx: RunContext,
    pattern: str,
    source: Any = None,
    group: int = 1,
    save_as: str | None = None,
) -> dict[str, Any]:
    if source is None:
        last = ctx.vars.get("last_response") or {}
        text = last.get("text") if isinstance(last, dict) else str(last)
    else:
        text = source if isinstance(source, str) else json.dumps(source, ensure_ascii=False)
    match = re.search(str(pattern), text or "")
    if not match:
        raise StepError(f"正则未匹配: {pattern}")
    value = match.group(int(group))
    if save_as:
        ctx.set(save_as, value)
    return {"value": value}


@atom(
    "header_get",
    description="取响应头的值（大小写不敏感），可存为变量",
    category="extract",
    tags=("extract",),
    aliases=("get_header",),
    inputs={"name": "string", "source": "object（默认 last_response）", "save_as": "string"},
    outputs={"value": "string"},
    required=("name",),
)
def header_get(
    ctx: RunContext,
    name: str,
    source: Any = None,
    save_as: str | None = None,
) -> dict[str, Any]:
    resp = source if source is not None else ctx.vars.get("last_response")
    headers = (resp or {}).get("headers") if isinstance(resp, dict) else None
    value = None
    for key, item in (headers or {}).items():
        if str(key).lower() == str(name).lower():
            value = item
            break
    if value is None:
        raise StepError(f"响应头不存在: {name}")
    if save_as:
        ctx.set(save_as, value)
    return {"value": value}
