"""零依赖 YAML 子集解析器。

为什么自己做：场景文件用 YAML 写最舒服，但项目承诺「克隆下来零依赖即可跑通闭环」。
所以这里实现一个**够用且严格**的子集：

支持
- 注释 ``#``（引号内不算注释）、空行
- 嵌套映射（缩进）、映射列表 ``- key: value``（可继续嵌套）
- 标量：字符串（含引号）、整数、浮点、布尔、null、``${模板}`` 原样保留
- 行内 flow：``[a, b]``、``{k: v}``
- 块字符串 ``|`` / ``>``（含 ``|-`` / ``>-``）

不支持（会显式报错，避免静默解析错）
- 锚点/别名 ``&`` / ``*``、多文档 ``---``、复杂键 ``? ``、标签 ``!!``

当环境里装了 PyYAML 时，:func:`load` 默认仍走本解析器；需要完整 YAML 时用
``load(text, prefer_pyyaml=True)``。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.errors import SpecError


class MiniYamlError(SpecError):
    """YAML 子集解析失败（带行号，便于定位）。"""


_KEY_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]*$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_UNSUPPORTED_PREFIXES = ("&", "*", "%", "@", "`")


def load(text: str, *, prefer_pyyaml: bool = False) -> Any:
    """解析 YAML 文本。"""
    if prefer_pyyaml:
        try:  # pragma: no cover - 取决于环境
            import yaml

            return yaml.safe_load(text)
        except ImportError:
            pass
    return loads(text)


def load_file(path: str, *, prefer_pyyaml: bool = False) -> Any:
    with open(path, encoding="utf-8") as handle:
        return load(handle.read(), prefer_pyyaml=prefer_pyyaml)


def loads(text: str) -> Any:
    lines = _preprocess(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        indent, content = lines[index]
        raise MiniYamlError(f"第 {indent} 列出现无法归属的内容: {content!r}（检查缩进是否一致）")
    return value


# --------------------------------------------------------------------------- #
# 预处理
# --------------------------------------------------------------------------- #
def _preprocess(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pending: str | None = None
    pending_indent = 0
    for raw in text.replace("\t", "    ").splitlines():
        if raw.strip().startswith("---") and raw.strip() in {"---", "..."}:
            raise MiniYamlError("不支持多文档 YAML（--- / ...）")
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if pending is not None:
            # 续行：补齐跨行的行内结构 [ ... ] / { ... }
            pending = f"{pending} {content}"
            if _flow_balanced(pending):
                out.append((pending_indent, pending))
                pending = None
            continue
        if not _flow_balanced(content):
            pending, pending_indent = content, indent
            continue
        out.append((indent, content))
    if pending is not None:
        raise MiniYamlError(f"行内结构没有闭合: {pending!r}")
    return out


def _flow_balanced(text: str) -> bool:
    """行内 [] {} 是否闭合（忽略引号内的括号）。"""
    depth = 0
    in_single = in_double = False
    for char in text:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
    return depth <= 0 and not in_single and not in_double


def _strip_comment(line: str) -> str:
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or line[index - 1] in " \t":
                return line[:index]
    return line


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index
    _, content = lines[index]
    if content.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise MiniYamlError(f"缩进不一致: {content!r}（期望 {indent} 空格）")
        if content.startswith("- "):
            break
        key, sep, remainder = _split_key(content)
        if not sep:
            raise MiniYamlError(f"不是合法的 key: value → {content!r}")
        if key in result:
            raise MiniYamlError(f"重复的键: {key}")
        remainder = remainder.strip()
        if remainder and remainder[0] in _UNSUPPORTED_PREFIXES:
            raise MiniYamlError(f"不支持该 YAML 语法: {content!r}")
        if remainder[:1] in {"|", ">"}:
            value, index = _parse_block_scalar(lines, index + 1, indent, remainder)
            result[key] = value
            continue
        if remainder:
            result[key] = parse_scalar(remainder)
            index += 1
            continue
        # 空值 → 看后续缩进决定是嵌套块还是 null
        if index + 1 < len(lines) and lines[index + 1][0] > indent:
            value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            result[key] = value
        else:
            result[key] = None
            index += 1
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent or not content.startswith("- "):
            break
        if current_indent > indent:
            raise MiniYamlError(f"列表项缩进不一致: {content!r}")
        remainder = content[2:].strip()
        virtual_indent = indent + 2
        if not remainder:
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            else:
                value, index = None, index + 1
            items.append(value)
            continue
        if _looks_like_mapping(remainder):
            merged: list[tuple[int, str]] = [(virtual_indent, remainder)]
            cursor = index + 1
            while cursor < len(lines) and lines[cursor][0] > indent:
                merged.append(lines[cursor])
                cursor += 1
            value, consumed = _parse_block(merged, 0, virtual_indent)
            if consumed != len(merged):
                _, leftover = merged[consumed]
                raise MiniYamlError(f"列表项内出现无法归属的内容: {leftover!r}")
            items.append(value)
            index = cursor
            continue
        items.append(parse_scalar(remainder))
        index += 1
    return items, index


def _parse_block_scalar(
    lines: list[tuple[int, str]], index: int, parent_indent: int, marker: str
) -> tuple[str, int]:
    folded = marker.startswith(">")
    chomp_strip = marker.endswith("-")
    body: list[str] = []
    while index < len(lines) and lines[index][0] > parent_indent:
        body.append(lines[index][1])
        index += 1
    if not body:
        return "", index
    text = " ".join(body) if folded else "\n".join(body)
    if chomp_strip:
        return text.rstrip("\n"), index
    return text + "\n", index


# --------------------------------------------------------------------------- #
# 标量
# --------------------------------------------------------------------------- #
def parse_scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if text[0] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return _unquote_simple(text, '"')
    if text[0] == "'":
        return _unquote_simple(text, "'")
    if text[0] == "[" or text[0] == "{":
        return _parse_flow(text)
    low = text.lower()
    if low in {"null", "~", "none"}:
        return None
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if _INT_RE.match(text):
        try:
            return int(text)
        except ValueError:  # pragma: no cover
            return text
    if _FLOAT_RE.match(text) and ("." in text or "e" in low):
        try:
            return float(text)
        except ValueError:  # pragma: no cover
            return text
    return text


def _unquote_simple(text: str, quote: str) -> str:
    inner = text[1:-1] if len(text) >= 2 and text.endswith(quote) else text[1:]
    if quote == "'":
        return inner.replace("''", "'")
    return inner.replace('\\"', '"').replace("\\n", "\n")


def _parse_flow(text: str) -> Any:
    """解析 ``[a, b]`` / ``{k: v}``（自带递归下降，不依赖 JSON 兼容性）。"""
    value, index = _flow_value(text, 0)
    rest = text[index:].strip()
    if rest:
        raise MiniYamlError(f"行内结构有多余内容: {rest!r}")
    return value


def _flow_value(text: str, index: int) -> tuple[Any, int]:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise MiniYamlError(f"行内结构意外结束: {text!r}")
    char = text[index]
    if char == "[":
        return _flow_list(text, index)
    if char == "{":
        return _flow_map(text, index)
    if char == '"':
        return _flow_double_quoted(text, index)
    if char == "'":
        return _flow_single_quoted(text, index)
    cursor = index
    while cursor < len(text) and text[cursor] not in ",]}":
        cursor += 1
    return parse_scalar(text[index:cursor].strip()), cursor


def _flow_list(text: str, index: int) -> tuple[list[Any], int]:
    index = _skip_ws(text, index + 1)
    items: list[Any] = []
    if index < len(text) and text[index] == "]":
        return items, index + 1
    while True:
        value, index = _flow_value(text, index)
        items.append(value)
        index = _skip_ws(text, index)
        if index >= len(text):
            raise MiniYamlError(f"行内列表缺少 ]: {text!r}")
        if text[index] == ",":
            index = _skip_ws(text, index + 1)
            continue
        if text[index] == "]":
            return items, index + 1
        raise MiniYamlError(f"行内列表分隔符非法: {text!r}")


def _flow_map(text: str, index: int) -> tuple[dict[str, Any], int]:
    index = _skip_ws(text, index + 1)
    result: dict[str, Any] = {}
    if index < len(text) and text[index] == "}":
        return result, index + 1
    while True:
        index = _skip_ws(text, index)
        if index >= len(text):
            raise MiniYamlError(f"行内映射缺少 }}: {text!r}")
        if text[index] == '"':
            key, index = _flow_double_quoted(text, index)
        elif text[index] == "'":
            key, index = _flow_single_quoted(text, index)
        else:
            cursor = index
            while cursor < len(text) and text[cursor] not in ":,}":
                cursor += 1
            key, index = text[index:cursor].strip(), cursor
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise MiniYamlError(f"行内映射缺少 : → {text!r}")
        value, index = _flow_value(text, index + 1)
        result[str(key)] = value
        index = _skip_ws(text, index)
        if index >= len(text):
            raise MiniYamlError(f"行内映射缺少 }}: {text!r}")
        if text[index] == ",":
            index = _skip_ws(text, index + 1)
            continue
        if text[index] == "}":
            return result, index + 1
        raise MiniYamlError(f"行内映射分隔符非法: {text!r}")


def _flow_double_quoted(text: str, index: int) -> tuple[str, int]:
    cursor = index + 1
    buffer = ""
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text):
            buffer += char + text[cursor + 1]
            cursor += 2
            continue
        if char == '"':
            try:
                return json.loads(f'"{buffer}"'), cursor + 1
            except ValueError:
                return buffer, cursor + 1
        buffer += char
        cursor += 1
    raise MiniYamlError(f"行内字符串缺少收尾引号: {text!r}")


def _flow_single_quoted(text: str, index: int) -> tuple[str, int]:
    cursor = index + 1
    buffer = ""
    while cursor < len(text):
        char = text[cursor]
        if char == "'":
            if cursor + 1 < len(text) and text[cursor + 1] == "'":
                buffer += "'"
                cursor += 2
                continue
            return buffer, cursor + 1
        buffer += char
        cursor += 1
    raise MiniYamlError(f"行内字符串缺少收尾引号: {text!r}")


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _split_key(content: str) -> tuple[str, str, str]:
    """拆 ``key: value``；返回 ``(key, sep, remainder)``。"""
    in_single = in_double = False
    for index, char in enumerate(content):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            key = content[:index].strip()
            remainder = content[index + 1 :]
            if not key:
                return "", "", content
            if remainder and not remainder.startswith(" "):
                # 形如 http://x 的裸值，不是键
                continue
            if not (_KEY_RE.match(key) or (key.startswith('"') and key.endswith('"'))):
                continue
            return key.strip('"'), ":", remainder
    return "", "", content


def _looks_like_mapping(text: str) -> bool:
    key, sep, _ = _split_key(text)
    return bool(sep and key)
