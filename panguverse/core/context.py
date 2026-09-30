"""运行上下文与模板解析引擎。

上下文的职责：
1. 承载变量（``vars``）、环境（``env``）、密钥（``secrets``）；
2. 提供一个极小的模板语言 ``${path|filter:arg}``，让原子之间可以串数据；
3. 记录 trace 与 HTTP 往返，供报告层与自愈层消费。

模板语言（刻意保持极小，API 测试场景足够且易调试）::

    ${env.BASE_URL}                  # 取值；整串匹配时保留原始类型
    ${login.body.token}              # 步骤输出：vars["login"]["body"]["token"]
    ${orders[0].id}                  # 支持列表下标
    ${env.X|default:http://127.0.0.1:8899}
    ${order.id|int}                  # 过滤器：int/float/str/bool/json/len/upper/lower/strip
    Bearer ${token}                  # 混排时按字符串拼接

变量路径查找顺序：``vars`` → ``env`` → ``secrets`` → ``meta``。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any

from .errors import TemplateError

_TEMPLATE_RE = re.compile(r"\$\{([^{}]*)\}")
_FULL_RE = re.compile(r"^\$\{([^{}]*)\}$")

MISSING = object()


class RunContext:
    """一次运行（或一个用例、一次循环迭代）的变量环境。"""

    def __init__(
        self,
        *,
        env: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
        run_id: str | None = None,
        workspace: Any = None,
        parent: RunContext | None = None,
        share: bool = False,
    ) -> None:
        self.run_id = run_id or (parent.run_id if parent else uuid.uuid4().hex[:12])
        if share and parent is not None:
            # 子作用域与父作用域共享变量表：子步骤写入的变量对外可见（可预期、好调试）
            self.env = parent.env
            self.vars = parent.vars
            self.secrets = parent.secrets
            self.vars.update(variables or {})
        else:
            self.env = dict(env or {})
            self.vars = dict(variables or {})
            self.secrets = dict(secrets or {})
        self.workspace = workspace if workspace is not None else (parent.workspace if parent else None)
        self.trace: list[dict[str, Any]] = parent.trace if parent else []
        self.http_log: list[dict[str, Any]] = parent.http_log if parent else []
        self.meta: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": time.time(),
        }
        if parent is not None and share:
            self.meta.update(parent.meta)
        self._parent = parent

    # ------------------------------------------------------------------ #
    # 变量读写
    # ------------------------------------------------------------------ #
    def get(self, path: str, default: Any = MISSING) -> Any:
        """按点号/下标路径取值。"""
        value = self._lookup(path)
        if value is MISSING:
            if default is MISSING:
                raise TemplateError(f"变量不存在: {path}")
            return default
        return value

    def set(self, path: str, value: Any) -> None:
        """按路径写值；中间层不存在时按下一段的形状自动创建字典/列表。"""
        parts = _split_path(path)
        if not parts:
            raise TemplateError("变量路径不能为空")
        node: Any = self.vars
        for index, part in enumerate(parts[:-1]):
            wants_list = parts[index + 1].lstrip("-").isdigit()
            node = _ensure_child(node, part, wants_list=wants_list, path=path)
        _assign(node, parts[-1], value, path)

    def add_secret(self, name: str, value: Any) -> None:
        """登记一个需要打码的明文值（同名多值会分别登记）。"""
        if not isinstance(value, str) or len(value) < 4:
            return
        key = name
        index = 2
        while key in self.secrets and self.secrets[key] != value:
            key = f"{name}#{index}"
            index += 1
        self.secrets[key] = value

    def secret_values(self) -> list[str]:
        """所有需要打码的明文值（长度 >= 4，避免误伤短串）。"""
        out: list[str] = []
        for value in self.secrets.values():
            if isinstance(value, str) and len(value) >= 4 and value not in out:
                out.append(value)
        return out

    def _lookup(self, path: str) -> Any:
        if not path:
            return MISSING
        parts = _split_path(path)
        head = parts[0]
        roots: list[Any] = []
        if head == "vars":
            roots.append(self.vars)
            parts = parts[1:]
        elif head == "env":
            roots.append(self.env)
            parts = parts[1:]
        elif head == "secrets":
            roots.append(self.secrets)
            parts = parts[1:]
        elif head == "meta":
            roots.append(self.meta)
            parts = parts[1:]
        else:
            roots.extend([self.vars, self.env, self.secrets, self.meta])
        for root in roots:
            value = _walk(root, parts)
            if value is not MISSING:
                return value
        return MISSING

    # ------------------------------------------------------------------ #
    # 模板解析
    # ------------------------------------------------------------------ #
    def resolve(self, value: Any) -> Any:
        """递归解析模板。整串为单个模板时保留原始类型，否则字符串拼接。"""
        if isinstance(value, str):
            return self._resolve_str(value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.resolve(v) for v in value]
        return value

    def _resolve_str(self, text: str) -> Any:
        full = _FULL_RE.match(text)
        if full:
            return self._eval_expr(full.group(1), raw_ok=True)
        if "${" not in text:
            return text

        def repl(match: re.Match[str]) -> str:
            value = self._eval_expr(match.group(1), raw_ok=False)
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)

        return _TEMPLATE_RE.sub(repl, text)

    def _eval_expr(self, expr: str, *, raw_ok: bool) -> Any:
        expr = expr.strip()
        if not expr:
            raise TemplateError("空的模板表达式 ${}")
        raw = False
        if expr.endswith("!raw"):
            raw = True
            expr = expr[: -len("!raw")].strip()
        head, *filters = _split_filters(expr)
        value = self._lookup(head.strip())
        if value is MISSING:
            has_default = any(f.strip().startswith("default:") for f in filters)
            if not has_default:
                raise TemplateError(
                    f"模板无法解析: ${{{head.strip()}}}（变量不存在；可用 |default:值 兜底）"
                )
            value = None
        for spec in filters:
            value = _apply_filter(value, spec.strip())
        if raw and not raw_ok:
            return value
        return value

    # ------------------------------------------------------------------ #
    # 子上下文 / 记录
    # ------------------------------------------------------------------ #
    def child(self, **variables: Any) -> RunContext:
        """派生一个子作用域：**共享**变量表（写入对外可见），共享 trace 与 http_log。"""
        return RunContext(
            env=self.env,
            variables=variables,
            secrets=self.secrets,
            run_id=self.run_id,
            workspace=self.workspace,
            parent=self,
            share=True,
        )

    def isolated(self, **variables: Any) -> RunContext:
        """派生一个隔离作用域：变量表独立（用于分子输出解析等只读场景）。"""
        return RunContext(
            env={**self.env},
            variables={**self.vars, **variables},
            secrets=dict(self.secrets),
            run_id=self.run_id,
            workspace=self.workspace,
        )

    def emit(self, event: str, **data: Any) -> None:
        """写一条 trace；trace 有上限，避免长跑把内存吃满。"""
        if len(self.trace) < 5000:
            self.trace.append({"ts": time.time(), "event": event, **data})

    def record_http(self, record: dict[str, Any]) -> None:
        if len(self.http_log) < 1000:
            self.http_log.append(record)

    def snapshot(self, *, mask: bool = True) -> dict[str, Any]:
        """给报告用的变量快照（默认对密钥打码）。"""
        from .redact import mask_obj  # 延迟导入避免环

        data = {
            "vars": self.vars,
            "env": self.env,
            "secrets": dict.fromkeys(self.secrets, "***"),
        }
        return mask_obj(data) if mask else data

    def iter_missing(self) -> Iterator[str]:
        """调试辅助：列出未被解析的模板残留。"""
        for path, value in _flatten(self.vars):
            if isinstance(value, str) and "${" in value:
                yield path

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RunContext run={self.run_id} vars={sorted(self.vars)[:8]}>"


# --------------------------------------------------------------------------- #
# 模板细节
# --------------------------------------------------------------------------- #
def _split_path(path: str) -> list[str]:
    """``a.b[0].c`` → ``["a", "b", "0", "c"]``"""
    parts: list[str] = []
    for chunk in path.strip().split("."):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 支持 a[0][1]
        while "[" in chunk:
            name, _, rest = chunk.partition("[")
            if name:
                parts.append(name)
            index, _, chunk = rest.partition("]")
            if index.strip():
                parts.append(index.strip())
            chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _walk(obj: Any, parts: list[str]) -> Any:
    cur = obj
    for part in parts:
        if isinstance(cur, dict):
            if part not in cur:
                return MISSING
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            if not part.lstrip("-").isdigit():
                return MISSING
            index = int(part)
            if index >= len(cur) or index < -len(cur):
                return MISSING
            cur = cur[index]
        else:
            return MISSING
    return cur


def _split_filters(expr: str) -> list[str]:
    """按 ``|`` 拆分路径与过滤器（过滤器参数内的 ``|`` 不拆）。"""
    out: list[str] = []
    buf = ""
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "|" and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    out.append(buf)
    return out


def _apply_filter(value: Any, spec: str) -> Any:
    if not spec:
        return value
    name, _, arg = spec.partition(":")
    name = name.strip().lower()
    arg = arg.strip()
    try:
        if name == "default":
            return value if value is not None else _coerce_scalar(arg)
        if value is None:
            return None
        if name == "int":
            return int(value)
        if name == "float":
            return float(value)
        if name == "str":
            return str(value)
        if name == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
        if name == "json":
            return json.loads(value) if isinstance(value, str) else value
        if name == "len":
            return len(value)
        if name == "upper":
            return str(value).upper()
        if name == "lower":
            return str(value).lower()
        if name == "strip":
            return str(value).strip()
        if name == "urlencode":
            from urllib.parse import quote

            return quote(str(value), safe="")
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"过滤器 {spec} 作用于 {value!r} 失败: {exc}") from exc
    raise TemplateError(f"未知过滤器: {name}")


def _coerce_scalar(text: str) -> Any:
    stripped = text.strip()
    low = stripped.lower()
    if low in {"null", "none", "~"}:
        return None
    if low in {"true", "false"}:
        return low == "true"
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        return float(stripped)
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    return text


def _ensure_child(node: Any, part: str, *, wants_list: bool, path: str) -> Any:
    """取出（必要时创建）路径上的中间容器。"""
    if isinstance(node, list):
        if not part.lstrip("-").isdigit():
            raise TemplateError(f"无法写入 {path}: 列表下标非法 {part!r}")
        position = int(part)
        while len(node) <= position:
            node.append(None)
        child = node[position]
        if not isinstance(child, (dict, list)):
            child = [] if wants_list else {}
            node[position] = child
        return child
    if isinstance(node, dict):
        child = node.get(part)
        if not isinstance(child, (dict, list)):
            child = [] if wants_list else {}
            node[part] = child
        return child
    raise TemplateError(f"无法写入 {path}: {part} 的父节点不是容器")


def _assign(node: Any, part: str, value: Any, path: str) -> None:
    if isinstance(node, list):
        if not part.lstrip("-").isdigit():
            raise TemplateError(f"无法写入 {path}: 列表下标非法 {part!r}")
        position = int(part)
        while len(node) <= position:
            node.append(None)
        node[position] = value
        return
    if isinstance(node, dict):
        node[part] = value
        return
    raise TemplateError(f"无法写入 {path}: 容器类型不支持")


def _flatten(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _flatten(value, f"{prefix}.{key}" if prefix else str(key))
    else:
        yield prefix, obj


# 路径工具对外暴露：原子（提取/断言）需要按同一套规则寻址
split_path = _split_path
walk_path = _walk
MISSING_VALUE = MISSING

# 便捷别名：``TimeoutError`` 与内置同名，故显式导出自己的类型
from .errors import RequestTimeout as RequestTimeoutError  # noqa: E402,F401
