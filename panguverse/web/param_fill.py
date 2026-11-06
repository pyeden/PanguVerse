"""草案参数自动填充 / 缺口审计（不针对任何具体原子写特判）。

核心规则：
- 只认**原子声明的输入端口**（``Atom.input_ports()``），凡是必填且端口名带 ``url``
  （``url`` / ``page_url`` / ``target_url`` …）的端口都是目标；
- 当前值缺失、为空、或**整个值就是一个 ``${env.X}`` 占位符**时，用已解析的 URL 覆盖；
- 已经是字面量 URL 的端口也会被覆盖并记录（显式 URL 优先于模板默认值），
  但**与目标 URL 相同**时不产生任何记录，避免 ``previous == value`` 的冗余审计；
- ``${env.BASE_URL}/api/login`` 这类「占位符 + 路径」不覆盖，避免把接口地址改成网页地址；
- 是否覆盖只看端口契约，不看 ``http_get_page`` / ``web_open`` 等原子名，新增带 url
  端口的原子会自动进入自动填充范围。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..core.atom import Atom

#: ``url``、``page_url``、``target_url`` 都算 URL 端口；``curl`` 不算。
URL_PORT_RE = re.compile(r"(^|_)url$", re.IGNORECASE)
#: 整个值就是一个环境变量占位符（允许过滤器）。
ENV_ONLY_RE = re.compile(r"^\s*\$\{env\.[A-Za-z_][A-Za-z0-9_]*(?:\|[^{}]*)?\}\s*$")
ENV_ANY_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)")
#: 占位符里的默认值过滤器，例如 ``${env.MAX_LATENCY_MS|default:2000|int}``。
#: 带默认值的 env 入参在运行期有可用值，不算参数缺口。
ENV_DEFAULT_RE = re.compile(r"\|\s*default\s*:\s*([^{}|]*)")
HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

_MISSING = object()


def is_url_port(name: str) -> bool:
    """端口名是否表示一个 URL 目标（数据驱动，不绑定具体原子）。"""
    return bool(URL_PORT_RE.search(str(name or "")))


def url_port_names(atom: Atom) -> list[str]:
    """原子声明里「必填 + 名字带 url」的输入端口名。"""
    return [
        port.name
        for port in atom.input_ports()
        if port.required and is_url_port(port.name)
    ]


def _env_only(value: Any) -> bool:
    return isinstance(value, str) and bool(ENV_ONLY_RE.match(value))


def _http_literal(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(HTTP_URL_RE.match(text)) and not bool(ENV_ANY_RE.search(text))


def should_fill(value: Any) -> bool:
    """当前值是否可以被解析出的 URL 覆盖。"""
    if value is _MISSING or value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        if _env_only(text):
            return True
        if _http_literal(text):
            return True
    return False


def fill_url_params(
    nodes: Iterable[dict[str, Any]],
    registry: Any,
    url: str,
    *,
    source: str = "site_alias",
) -> list[dict[str, Any]]:
    """把 URL 填进所有节点的 URL 端口，返回自动填写审计记录。

    记录形如 ``{node_id, atom, param, value, source, previous}``；``previous`` 只在
    覆盖了非空值时出现，便于前端解释「模板默认值被替换」。
    """
    target = str(url or "").strip()
    if not target:
        return []
    records: list[dict[str, Any]] = []
    for node in nodes:
        atom_name = str(node.get("atom") or "")
        if not atom_name or not registry.has(atom_name):
            continue
        atom = registry.get(atom_name)
        params = node.get("params")
        if not isinstance(params, dict):
            params = {}
            node["params"] = params
        for param in url_port_names(atom):
            current = params.get(param, _MISSING)
            if not should_fill(current):
                continue
            # 值已经是目标 URL 时不覆盖、不记录：planner 注入与参数填充同值时
            # 不应出现 previous == value 的冗余审计项。
            if current is not _MISSING and isinstance(current, str) and current.strip() == target:
                continue
            record: dict[str, Any] = {
                "node_id": str(node.get("id") or ""),
                "atom": atom_name,
                "param": param,
                "value": target,
                "source": source,
            }
            if current is not _MISSING and current not in (None, ""):
                record["previous"] = current
            params[param] = target
            records.append(record)
    return records


def _required_gap(node_id: str, atom_name: str, param: str, value: Any) -> dict[str, Any]:
    gap: dict[str, Any] = {"node_id": node_id, "atom": atom_name, "param": param}
    if value is _MISSING:
        gap["reason"] = "missing"
    elif value is None or (isinstance(value, str) and not value.strip()):
        gap["reason"] = "empty"
    else:
        gap["reason"] = "unresolved_env"
        gap["value"] = value
    return gap


def audit_param_gaps(nodes: Iterable[Mapping[str, Any]], registry: Any) -> list[dict[str, Any]]:
    """只报「必填端口最终为空」的缺口。

    ``${env.X}`` 占位符不是缺口：有 ``|default:`` 的运行期有默认值，没有默认值的
    也只是需要用户提供环境变量，统一由 :func:`audit_env_inputs` 以 info 形式给出。
    这样 ``api_testing`` 这类模板不会一次报出一串 ``unresolved_env`` 噪声。
    """
    gaps: list[dict[str, Any]] = []
    for node in nodes:
        atom_name = str(node.get("atom") or "")
        if not atom_name or not registry.has(atom_name):
            continue
        atom = registry.get(atom_name)
        params = node.get("params")
        params = params if isinstance(params, Mapping) else {}
        for port in atom.input_ports():
            if not port.required:
                continue
            value = params.get(port.name, _MISSING)
            if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
                gaps.append(_required_gap(str(node.get("id") or ""), atom_name, port.name, value))
    return gaps


def audit_env_inputs(
    nodes: Iterable[Mapping[str, Any]], registry: Any
) -> list[dict[str, Any]]:
    """必填端口里仍是 ``${env.X}`` 的输入清单（不是缺口）。

    记录形如 ``{node_id, atom, param, env, value, status}``：

    - ``status="default"``：占位符带 ``|default:``，运行期有默认值；
    - ``status="info"``：没有默认值，仅提示需要环境变量。
    """
    inputs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for node in nodes:
        atom_name = str(node.get("atom") or "")
        if not atom_name or not registry.has(atom_name):
            continue
        atom = registry.get(atom_name)
        params = node.get("params")
        params = params if isinstance(params, Mapping) else {}
        node_id = str(node.get("id") or "")
        for port in atom.input_ports():
            if not port.required:
                continue
            value = params.get(port.name, _MISSING)
            if not isinstance(value, str):
                continue
            default_match = ENV_DEFAULT_RE.search(value)
            for match in ENV_ANY_RE.finditer(value):
                env_name = match.group(1)
                key = (node_id, atom_name, port.name, env_name)
                if key in seen:
                    continue
                seen.add(key)
                entry: dict[str, Any] = {
                    "node_id": node_id,
                    "atom": atom_name,
                    "param": port.name,
                    "env": env_name,
                    "value": value,
                    "status": "default" if default_match else "info",
                }
                if default_match:
                    entry["default"] = default_match.group(1).strip()
                inputs.append(entry)
    return inputs


__all__ = [
    "audit_env_inputs",
    "audit_param_gaps",
    "fill_url_params",
    "is_url_port",
    "should_fill",
    "url_port_names",
]
