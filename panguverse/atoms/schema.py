"""轻量 JSON Schema 校验（子集实现，零依赖）。

覆盖 API 测试最常用的关键字：``type`` / ``required`` / ``properties`` / ``items`` /
``enum`` / ``const`` / ``minimum`` / ``maximum`` / ``minLength`` / ``maxLength`` /
``pattern`` / ``minItems`` / ``maxItems`` / ``additionalProperties`` / ``nullable`` /
``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref``（仅支持同文档 ``#/definitions`` 与 ``#/$defs``）。

返回错误列表（路径 + 原因），而不是抛异常——由断言原子决定怎么报告。
"""

from __future__ import annotations

import re
from typing import Any


def validate(instance: Any, schema: dict[str, Any], *, path: str = "$", root: dict[str, Any] | None = None) -> list[str]:
    """校验实例，返回错误信息列表（空表示通过）。"""
    root = root if root is not None else schema
    errors: list[str] = []
    _validate(instance, schema, path, root, errors)
    return errors


def _validate(instance: Any, schema: Any, path: str, root: dict[str, Any], errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        target = _resolve_ref(str(schema["$ref"]), root)
        if target is None:
            errors.append(f"{path}: 无法解析 $ref {schema['$ref']}")
            return
        _validate(instance, target, path, root, errors)
        return

    if instance is None and schema.get("nullable") is True:
        return

    for key in ("allOf",):
        for sub in schema.get(key) or []:
            _validate(instance, sub, path, root, errors)

    if "anyOf" in schema:
        subs = schema.get("anyOf") or []
        if not any(not validate(instance, sub, path=path, root=root) for sub in subs):
            errors.append(f"{path}: 不满足 anyOf 中任何一项")
    if "oneOf" in schema:
        matched = sum(1 for sub in (schema.get("oneOf") or []) if not validate(instance, sub, path=path, root=root))
        if matched != 1:
            errors.append(f"{path}: oneOf 需要恰好匹配 1 项，实际 {matched}")

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_ok(instance, t) for t in types):
            errors.append(f"{path}: 类型应为 {expected_type}，实际 {_type_name(instance)}")
            return

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: 值 {_brief(instance)} 不在枚举 {_brief(schema['enum'])} 中")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 值应为常量 {_brief(schema['const'])}，实际 {_brief(instance)}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            errors.append(f"{path}: 长度应 >= {schema['minLength']}，实际 {len(instance)}")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            errors.append(f"{path}: 长度应 <= {schema['maxLength']}，实际 {len(instance)}")
        if "pattern" in schema and not re.search(str(schema["pattern"]), instance):
            errors.append(f"{path}: 不匹配正则 {schema['pattern']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: 应 >= {schema['minimum']}，实际 {instance}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: 应 <= {schema['maximum']}，实际 {instance}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: 应 > {schema['exclusiveMinimum']}，实际 {instance}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: 应 < {schema['exclusiveMaximum']}，实际 {instance}")

    if isinstance(instance, dict):
        for name in schema.get("required") or []:
            if name not in instance:
                errors.append(f"{path}: 缺少必填字段 {name}")
        props = schema.get("properties") or {}
        for name, sub in props.items():
            if name in instance:
                _validate(instance[name], sub, f"{path}.{name}", root, errors)
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                errors.append(f"{path}: 出现未声明的字段 {sorted(extra)}")
        elif isinstance(schema.get("additionalProperties"), dict):
            for name in set(instance) - set(props):
                _validate(instance[name], schema["additionalProperties"], f"{path}.{name}", root, errors)

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            errors.append(f"{path}: 元素个数应 >= {schema['minItems']}，实际 {len(instance)}")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            errors.append(f"{path}: 元素个数应 <= {schema['maxItems']}，实际 {len(instance)}")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                _validate(item, items, f"{path}[{index}]", root, errors)


def _resolve_ref(ref: str, root: dict[str, Any]) -> Any:
    if not ref.startswith("#"):
        return None
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not part:
            continue
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _brief(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."
