"""把工作台里的草稿代码注册为工作区原子包。

只负责「落盘 + 校验可加载」：写 ``.panguverse/packs/<pack>/atoms/<name>.py``
与 ``pack.json``，再用现有 workspace pack 加载器验证。运行期仍由
``workspace_registry`` 按 pinned ref 解析。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core.workspace_packs import MANIFEST_NAME, PACKS_REL
from .studio import StudioError, rebind_node_to_atom

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_RUN_RE = re.compile(r"^\s*def\s+run\s*\(", re.M)


def _text(value: Any, *, field: str, limit: int = 2000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StudioError("bad_request", f"{field} 必须是字符串")
    text = value.strip()
    if len(text) > limit:
        raise StudioError("bad_request", f"{field} 过长（上限 {limit}）")
    return text


def _slug(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not raw:
        raw = "custom_atom"
    if not raw[0].isalpha():
        raw = f"atom_{raw}"
    return raw[:64]


def _ports(value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StudioError("bad_request", f"{field} 必须是对象")
    out: dict[str, str] = {}
    for key, item in value.items():
        name = _text(key, field=f"{field} 的键", limit=80)
        if not name:
            raise StudioError("bad_request", f"{field} 的键不能为空")
        port_type = _text(item, field=f"{field}.{name}", limit=40) or "any"
        out[name] = port_type
    return out


def _str_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise StudioError("bad_request", f"{field} 必须是字符串数组")
    return [_text(item, field=field, limit=80) for item in value if _text(item, field=field, limit=80)]


def register_custom_atom(
    root: str | Path,
    payload: Any,
    *,
    actor: str = "local",
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise StudioError("bad_request", "请求体必须是对象")

    name = _slug(payload.get("name") or payload.get("atom") or "custom_atom")
    if not _NAME_RE.fullmatch(name):
        raise StudioError(
            "bad_request",
            "原子名必须是小写字母开头、只含小写字母/数字/下划线，长度 2-64",
        )
    code = _text(payload.get("code"), field="code", limit=200_000)
    if not code:
        raise StudioError("bad_request", "缺少草稿实现代码")
    if not _RUN_RE.search(code):
        raise StudioError("bad_request", "原子代码必须提供 def run(...) 入口")
    try:
        compile(code, f"<atom:{name}>", "exec")
    except SyntaxError as exc:
        raise StudioError(
            "bad_request",
            f"代码语法错误：{exc.msg}（第 {exc.lineno} 行）",
            status=422,
        ) from exc

    pack = _slug(payload.get("pack") or "custom")
    version = _text(payload.get("version"), field="version", limit=40) or "0.1.0"
    description = _text(payload.get("description"), field="description", limit=500) or (
        f"工作台注册的自定义原子 {name}"
    )
    inputs = _ports(payload.get("inputs"), field="inputs")
    outputs = _ports(payload.get("outputs"), field="outputs")
    effects = _str_list(payload.get("effects"), field="effects")
    capabilities = _str_list(payload.get("capabilities"), field="capabilities")
    idempotent = bool(payload.get("idempotent", True))
    failure_class = _text(payload.get("failure_class"), field="failure_class", limit=80)
    replace = bool(payload.get("replace", False))

    root_path = Path(root)
    pack_dir = root_path / PACKS_REL / pack
    atoms_dir = pack_dir / "atoms"
    atom_path = atoms_dir / f"{name}.py"
    if atom_path.exists() and not replace:
        raise StudioError(
            "conflict",
            f"原子 {name} 已存在；如需覆盖请显式传 replace=true",
            status=409,
        )

    atoms_dir.mkdir(parents=True, exist_ok=True)
    if not code.endswith("\n"):
        code += "\n"
    atom_path.write_text(code, encoding="utf-8")
    code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()

    manifest_path = pack_dir / MANIFEST_NAME
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                manifest = dict(raw)
        except (OSError, ValueError):
            manifest = {}
    manifest.update(
        {
            "name": pack,
            "domain": str(manifest.get("domain") or "custom"),
            "version": str(manifest.get("version") or "0.1.0"),
            "description": str(manifest.get("description") or f"工作区自定义原子包 {pack}"),
            "tags": list(manifest.get("tags") or ["custom", "workspace"]),
            "source": "custom",
        }
    )
    atom_entry = {
        "name": name,
        "description": description,
        "inputs": inputs,
        "outputs": outputs,
        "effects": effects,
        "capabilities": capabilities,
        "idempotent": idempotent,
        "failure_class": failure_class,
        "entry": f"atoms/{name}.py",
        "category": "custom",
        "tags": ["custom", "workspace", pack],
        "origin": "custom",
        "registered_by": actor,
        "version": version,
        "code_sha256": code_sha,
        "registered_at": time.time(),
    }
    existing = manifest.get("atoms")
    atoms = [item for item in existing if isinstance(item, Mapping)] if isinstance(existing, list) else []
    atoms = [item for item in atoms if item.get("name") != name]
    atoms.append(atom_entry)
    manifest["atoms"] = atoms

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)

    if not atom_path.is_file():
        raise StudioError("internal_error", f"原子代码写入失败：{atom_path}")
    # 注册阶段只做静态校验；绑定阶段由 workspace_registry 解析真实原子。
    binding: dict[str, Any] | None = None
    bind = bool(payload.get("bind", True))
    wid = _text(payload.get("wid"), field="wid", limit=120)
    node_id = _text(payload.get("node_id"), field="node_id", limit=200)
    if bind and wid and node_id:
        base_rev = payload.get("rev")
        binding = rebind_node_to_atom(
            root_path,
            wid,
            node_id,
            name,
            base_rev=int(base_rev) if base_rev is not None else None,
            actor=actor,
            message=f"注册原子 {name} 并绑定节点 {node_id}",
        )
    return {
        "atom": atom_entry,
        "pack": pack,
        "pack_path": str(pack_dir),
        "ref": f"atom:{name}@{version}#{code_sha[:12]}",
        "binding": binding,
        "contract": {
            "inputs": inputs,
            "outputs": outputs,
            "effects": effects,
            "capabilities": capabilities,
        },
    }
