"""断言原子：把「期望」变成可执行的原子步骤。

所有断言失败都抛 :class:`AssertionFailed`（= 测试 ``failed``），
并带上 ``expected`` / ``actual`` / ``path``，报告里能直接看到差异。
多个算子同时不满足时，报告会列出全部差异，而不是只报第一个。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.atom import atom
from ..core.context import RunContext
from ..core.errors import AssertionFailed
from .extract import lookup, resolve_source
from .schema import validate as validate_schema

_UNSET = object()


# --------------------------------------------------------------------------- #
# 状态码
# --------------------------------------------------------------------------- #
def status_matches(expected: Any, actual: int) -> bool:
    """支持 ``200`` / ``[200, 201]`` / ``"2xx"`` / ``"200,201"``。"""
    if expected is None:
        return True
    if isinstance(expected, bool):  # pragma: no cover - 防误用
        return bool(expected) == bool(actual)
    if isinstance(expected, int):
        return actual == expected
    if isinstance(expected, (list, tuple, set)):
        return any(status_matches(item, actual) for item in expected)
    text = str(expected).strip()
    if re.fullmatch(r"\d{3}", text):
        return actual == int(text)
    if re.fullmatch(r"[1-5][xX]{2}", text):
        return str(actual).startswith(text[0])
    if re.fullmatch(r"[1-5]", text):
        return str(actual).startswith(text)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > 1:
        return any(status_matches(p, actual) for p in parts)
    return str(actual) == text


@atom(
    "assert_status",
    description="断言 HTTP 状态码（int / 列表 / '2xx' / '200,201'）",
    category="assert",
    tags=("assert", "http", "core"),
    aliases=("expect_status",),
    inputs={"expected": "int|int[]|string", "source": "object（默认 last_response）", "actual": "int", "message": "string"},
    outputs={},
    required=("expected",),
)
def assert_status(
    ctx: RunContext,
    expected: Any,
    source: Any = None,
    actual: Any = None,
    message: str | None = None,
) -> dict[str, Any]:
    if actual is None:
        resp = resolve_source(ctx, source)
        if not isinstance(resp, dict) or "status_code" not in resp:
            raise AssertionFailed(
                f"无法获取状态码：source 不是 HTTP 响应（{type(resp).__name__}）",
                expected=expected,
                actual=resp,
                path="response.status_code",
            )
        actual = resp["status_code"]
    if not status_matches(expected, int(actual)):
        raise AssertionFailed(
            message or f"状态码期望 {expected}，实际 {actual}",
            expected=expected,
            actual=actual,
            path="response.status_code",
        )
    return {"status_code": int(actual)}


# --------------------------------------------------------------------------- #
# 响应体 / JSON 路径
# --------------------------------------------------------------------------- #
@atom(
    "assert_json",
    description="断言响应体（或任意对象）某路径上的值：equals/contains/matches/type/gt/length 等",
    category="assert",
    tags=("assert", "json", "core"),
    aliases=("expect_json",),
    inputs={
        "path": "string（空表示整个对象）",
        "source": "any（默认 last_response）",
        "in": "string=body|headers|response",
        "equals": "any",
        "not_equals": "any",
        "exists": "bool",
        "contains": "any",
        "not_contains": "any",
        "matches": "string（正则）",
        "type": "string（string/integer/number/boolean/array/object/null）",
        "gt": "number",
        "gte": "number",
        "lt": "number",
        "lte": "number",
        "length": "int",
        "message": "string",
    },
    outputs={},
    required=(),
)
def assert_json(
    ctx: RunContext,
    path: str = "",
    source: Any = None,
    in_: str = "body",
    equals: Any = _UNSET,
    not_equals: Any = _UNSET,
    exists: Any = _UNSET,
    contains: Any = _UNSET,
    not_contains: Any = _UNSET,
    matches: Any = _UNSET,
    type_: Any = _UNSET,
    gt: Any = _UNSET,
    gte: Any = _UNSET,
    lt: Any = _UNSET,
    lte: Any = _UNSET,
    length: Any = _UNSET,
    message: str | None = None,
) -> dict[str, Any]:
    src = resolve_source(ctx, source)
    missing = False
    try:
        actual = lookup(src, path, in_=in_)
    except (KeyError, TypeError, IndexError):
        missing = True
        actual = None

    failures: list[str] = []
    checks = 0
    label = path or "<root>"

    def check(ok: bool, description: str) -> None:
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(description)

    if exists is not _UNSET:
        check((not missing) == bool(exists), f"字段 {label} 存在性期望 {bool(exists)}，实际 {not missing}")
    if missing:
        if failures:
            raise _failed(message, failures, label, actual)
        if exists is _UNSET:
            # 没显式写 exists 时，path 本身就是存在性断言
            raise _failed(message, [f"路径 {label} 不存在"], label, None)
        # 只有 exists: false 走到这里，属于通过
        return {"assertion": {"ok": True, "checks": checks}, "actual": None}
    if equals is not _UNSET:
        check(actual == equals, f"{label} 期望等于 {_b(equals)}，实际 {_b(actual)}")
    if not_equals is not _UNSET:
        check(actual != not_equals, f"{label} 期望不等于 {_b(not_equals)}，实际 {_b(actual)}")
    if contains is not _UNSET:
        check(_contains(actual, contains), f"{label} 期望包含 {_b(contains)}，实际 {_b(actual)}")
    if not_contains is not _UNSET:
        check(not _contains(actual, not_contains), f"{label} 期望不包含 {_b(not_contains)}，实际 {_b(actual)}")
    if matches is not _UNSET:
        check(
            isinstance(actual, str) and re.search(str(matches), actual) is not None,
            f"{label} 期望匹配正则 {matches}，实际 {_b(actual)}",
        )
    if type_ is not _UNSET:
        check(_type_ok(actual, type_), f"{label} 类型期望 {type_}，实际 {_type_name(actual)}")
    for op, limit in ((">", gt), (">=", gte), ("<", lt), ("<=", lte)):
        if limit is _UNSET:
            continue
        ok, why = _compare(actual, op, limit)
        check(ok, f"{label} 期望 {op} {_b(limit)}，实际 {_b(actual)}{why}")
    if length is not _UNSET:
        try:
            actual_len = len(actual)
            check(actual_len == int(length), f"{label} 长度期望 {length}，实际 {actual_len}")
        except TypeError:
            check(False, f"{label} 不可求长度，实际 {_b(actual)}")

    if failures:
        raise _failed(message, failures, label, actual)
    return {"assertion": {"ok": True, "checks": checks}, "actual": actual}


def _failed(message: str | None, failures: list[str], label: str, actual: Any) -> AssertionFailed:
    main = message or failures[0]
    detail = {"path": label, "checks": failures} if len(failures) > 1 else {"path": label}
    return AssertionFailed("\n".join([main, *failures[1:]]) if len(failures) > 1 else main,
                           expected=None, actual=actual, path=label, detail=detail)


@atom(
    "assert_schema",
    description="用 JSON Schema（子集）校验响应体结构",
    category="assert",
    tags=("assert", "schema"),
    inputs={"schema": "object", "path": "string（默认整个响应体）", "source": "any", "in": "string=body"},
    outputs={},
    required=("schema",),
)
def assert_schema(
    ctx: RunContext,
    schema: dict[str, Any],
    path: str = "",
    source: Any = None,
    in_: str = "body",
) -> dict[str, Any]:
    src = resolve_source(ctx, source)
    try:
        instance = lookup(src, path, in_=in_)
    except (KeyError, TypeError, IndexError) as exc:
        raise AssertionFailed(
            f"schema 校验目标不存在: {path or '<root>'}", expected=schema, actual=None, path=path
        ) from exc
    errors = validate_schema(instance, schema)
    if errors:
        raise AssertionFailed(
            "JSON Schema 校验失败: " + "; ".join(errors[:3]),
            expected=schema,
            actual=instance,
            path=path or "<root>",
            detail={"errors": errors},
        )
    return {"assertion": {"ok": True, "checks": 1}}


@atom(
    "assert_latency",
    description="断言响应耗时上限（毫秒）",
    category="assert",
    tags=("assert", "performance"),
    inputs={"max_ms": "number", "source": "object（默认 last_response）", "actual": "number"},
    outputs={},
    required=("max_ms",),
)
def assert_latency(
    ctx: RunContext,
    max_ms: Any,
    source: Any = None,
    actual: Any = None,
) -> dict[str, Any]:
    if actual is None:
        resp = resolve_source(ctx, source)
        actual = (resp or {}).get("elapsed_ms")
        if actual is None:
            raise AssertionFailed("无法获取耗时：source 不是 HTTP 响应", expected=max_ms, actual=resp)
    if float(actual) > float(max_ms):
        raise AssertionFailed(
            f"耗时期望 <= {max_ms}ms，实际 {actual}ms",
            expected=max_ms,
            actual=actual,
            path="response.elapsed_ms",
        )
    return {"elapsed_ms": actual}


@atom(
    "assert_header",
    description="断言响应头（大小写不敏感）",
    category="assert",
    tags=("assert", "http"),
    inputs={"name": "string", "equals": "any", "contains": "string", "exists": "bool", "source": "object", "message": "string"},
    outputs={},
    required=("name",),
)
def assert_header(
    ctx: RunContext,
    name: str,
    equals: Any = _UNSET,
    contains: Any = _UNSET,
    exists: Any = _UNSET,
    source: Any = None,
    message: str | None = None,
) -> dict[str, Any]:
    resp = resolve_source(ctx, source)
    headers = (resp or {}).get("headers") or {}
    actual = None
    for key, value in headers.items():
        if str(key).lower() == str(name).lower():
            actual = value
            break
    found = actual is not None
    failures: list[str] = []
    if exists is not _UNSET and found != bool(exists):
        failures.append(f"响应头 {name} 存在性期望 {bool(exists)}，实际 {found}")
    if not found and exists is not True:
        failures.append(f"响应头 {name} 不存在")
    if found:
        if equals is not _UNSET and str(actual) != str(equals):
            failures.append(f"响应头 {name} 期望等于 {_b(equals)}，实际 {_b(actual)}")
        if contains is not _UNSET and str(contains) not in str(actual):
            failures.append(f"响应头 {name} 期望包含 {_b(contains)}，实际 {_b(actual)}")
    if failures:
        raise AssertionFailed(message or failures[0], expected=equals, actual=actual, path=f"headers.{name}")
    return {"header": actual}


@atom(
    "assert_body_contains",
    description="断言响应原文包含指定片段",
    category="assert",
    tags=("assert", "http"),
    aliases=("assert_text",),
    inputs={"text": "string", "source": "any（默认 last_response）"},
    outputs={},
    required=("text",),
)
def assert_body_contains(ctx: RunContext, text: str, source: Any = None) -> dict[str, Any]:
    resp = resolve_source(ctx, source)
    raw = (resp or {}).get("text") or ""
    if str(text) not in str(raw):
        raise AssertionFailed(
            f"响应原文未包含 {text!r}", expected=text, actual=_truncate(str(raw)), path="response.text"
        )
    return {"assertion": {"ok": True}}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, str):
        return str(item) in container
    if isinstance(container, dict):
        return item in container
    if isinstance(container, (list, tuple, set)):
        return item in container
    return False


def _compare(actual: Any, op: str, limit: Any) -> tuple[bool, str]:
    try:
        left, right = float(actual), float(limit)
    except (TypeError, ValueError):
        return False, "（无法比较数值）"
    if op == ">":
        return left > right, ""
    if op == ">=":
        return left >= right, ""
    if op == "<":
        return left < right, ""
    return left <= right, ""


def _type_ok(value: Any, expected: Any) -> bool:
    from .schema import _type_ok as schema_type_ok

    return schema_type_ok(value, str(expected))


def _type_name(value: Any) -> str:
    from .schema import _type_name as schema_type_name

    return schema_type_name(value)


def _b(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _truncate(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"
