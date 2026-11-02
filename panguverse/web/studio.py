"""工作台后端：草案 / 不可变版本 / 审计 / 隔离调试。

落盘结构（契约 ``tasks/workbench-mvp-spec.md`` §2.1）::

    <ws>/.panguverse/studio/<wid>/workflow.json          # 当前指针与状态
    <ws>/.panguverse/studio/<wid>/revisions/<rev>.json   # 不可变版本
    <ws>/.panguverse/studio/<wid>/audit.jsonl            # append-only 审计
    <ws>/.panguverse/studio/<wid>/proposals/<rev>.json   # replan 提案（未应用）
    <ws>/.panguverse/studio/<wid>/debug/<id>.json        # 调试快照（不进 runs）
    <ws>/.panguverse/studio/<wid>/ui.json                # 画布坐标（不进 rev）
    <ws>/.panguverse/studio/<wid>/published/<rev>.json   # 发布记录（不可变）

目录 ``0700``、文件 ``0600``，全部原子写。唯一允许修改既有文件的动作是
``workflow.json``（指针/状态），版本文件、发布记录只写一次。
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..axe.plan import PlanRequest
from ..axe.planner import RuleBasedPlanner
from ..core.redact import mask_obj
from . import projects
from .graph import (
    apply_graph_diff,
    diff_graphs,
    edge_change_touches_node,
    graph_from_plan,
    node_from_step,
    node_signature,
    plan_from_nodes,
    template_roots,
)

STATE_DIRNAME = ".panguverse"
STUDIO_DIRNAME = "studio"
DIR_MODE = 0o700
FILE_MODE = 0o600

WID_RE = re.compile(r"^w-[0-9a-f]{6,12}$")
WORKFLOW_REF_RE = re.compile(r"^(w-[0-9a-f]{6,12})@(\d+)$")
ENV_TEMPLATE_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)")
_SENSITIVE_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential)")
_URL_KEY_RE = re.compile(
    r"(?i)(^|_)(base[_-]?url|url|uri|host|origin|target|endpoint)$|^(base_url|baseurl|url|uri|host|target|endpoint)$"
)
STATES = ("draft", "review", "approved", "rejected")
TRANSITIONS = ("review", "approved", "rejected")
DEBUG_MODES = ("only", "from_start", "downstream")
MAX_TEXT = 2000

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "not_approved": 409,
    "risk_unconfirmed": 409,
    "internal_error": 500,
}


class StudioError(Exception):
    """带错误码的工作台异常；``details`` 会并入统一错误体（如 ``locked_nodes``）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _STATUS_BY_CODE.get(code, 500)
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# 基础落盘
# --------------------------------------------------------------------------- #
def new_wid() -> str:
    return "w-" + uuid.uuid4().hex[:8]


def validate_wid(wid: Any) -> str:
    if not isinstance(wid, str) or not WID_RE.match(wid):
        raise StudioError("bad_request", "wid 非法（应形如 w-8f3a1c）")
    return wid


def _as_rev(value: Any, *, field: str = "rev") -> int:
    if isinstance(value, bool):
        raise StudioError("bad_request", f"{field} 必须是非负整数")
    try:
        rev = int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError("bad_request", f"{field} 必须是非负整数") from exc
    if rev < 0:
        raise StudioError("bad_request", f"{field} 必须是非负整数")
    return rev


def _as_dict(value: Any, *, field: str = "body") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise StudioError("bad_request", f"{field} 必须是 JSON 对象")
    return value


def _clean_text(value: Any, *, field: str, required: bool = False, limit: int = MAX_TEXT) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise StudioError("bad_request", f"{field} 必须是字符串")
    if required and not text:
        raise StudioError("bad_request", f"{field} 不能为空")
    if len(text) > limit:
        raise StudioError("bad_request", f"{field} 不能超过 {limit} 个字符")
    return text


def studio_root(root: Path) -> Path:
    return Path(root) / STATE_DIRNAME / STUDIO_DIRNAME


def _workflow_dir(root: Path, wid: str) -> Path:
    return studio_root(root) / validate_wid(wid)


def _ensure_dir(directory: Path) -> None:
    """逐级创建目录，并把新目录设为 0700（不碰已存在的上层目录）。"""
    missing: list[Path] = []
    current = directory
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir()
        with contextlib.suppress(OSError):
            path.chmod(DIR_MODE)
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(DIR_MODE)


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool = True) -> None:
    _ensure_dir(path.parent)
    if not overwrite and path.exists():
        raise StudioError("internal_error", f"不可变文件已存在: {path.name}")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _read_json(path: Path, *, not_found: str, broken: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StudioError("not_found", not_found) from exc
    except OSError as exc:
        raise StudioError("internal_error", f"{broken}: {exc}") from exc
    except ValueError as exc:
        raise StudioError("internal_error", f"{broken}: {exc}") from exc
    if not isinstance(data, dict):
        raise StudioError("internal_error", f"{broken}：顶层必须是对象")
    return data


def _audit_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(entry)
    data.setdefault("ts", time.time())
    data.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return data


def _append_audit(dir_path: Path, entry: Mapping[str, Any]) -> None:
    _ensure_dir(dir_path)
    path = dir_path / "audit.jsonl"
    line = json.dumps(_audit_payload(entry), ensure_ascii=False, default=str) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, FILE_MODE)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, FILE_MODE)


def audit_tail(root: Path, wid: str, *, limit: int = 20) -> list[dict[str, Any]]:
    path = _workflow_dir(root, wid) / "audit.jsonl"
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                entries.append(data)
    except OSError:
        return []
    return entries[-max(1, limit) :]


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #
def _revision_path(root: Path, wid: str, rev: int) -> Path:
    return _workflow_dir(root, wid) / "revisions" / f"{_as_rev(rev)}.json"


def read_workflow(root: Path, wid: str) -> dict[str, Any]:
    return _read_json(
        _workflow_dir(root, wid) / "workflow.json",
        not_found="工作流不存在",
        broken="工作流文件无法解析",
    )


def _write_workflow(root: Path, wid: str, workflow: Mapping[str, Any]) -> None:
    _atomic_write_json(_workflow_dir(root, wid) / "workflow.json", workflow)


def read_revision(root: Path, wid: str, rev: int) -> dict[str, Any]:
    return _read_json(
        _revision_path(root, wid, rev),
        not_found=f"版本 {rev} 不存在",
        broken=f"版本 {rev} 文件无法解析",
    )


def _write_revision(root: Path, wid: str, revision: Mapping[str, Any]) -> None:
    rev = _as_rev(revision.get("rev"))
    _atomic_write_json(_revision_path(root, wid, rev), revision, overwrite=False)


def _commit_revision(
    root: Path,
    workflow: dict[str, Any],
    *,
    wid: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    origin: str,
    actor: str,
    message: str,
    base_rev: int | None,
    env_schema: list[str] | None = None,
    audit_note: str = "",
) -> dict[str, Any]:
    rev = int(workflow.get("current_rev") or 0) + 1
    old_state = str(workflow.get("state") or "draft")
    revision = {
        "wid": wid,
        "rev": rev,
        "origin": "human" if origin == "human" else "ai",
        "created_by": actor,
        "created_at": time.time(),
        "message": message,
        "base_rev": base_rev,
        "intent": workflow.get("intent") or "",
        "domain_pack": workflow.get("domain_pack") or "",
        "env_schema": list(env_schema if env_schema is not None else _env_schema(nodes)),
        "nodes": copy.deepcopy(nodes),
        "edges": copy.deepcopy(edges),
    }
    _write_revision(root, wid, revision)
    workflow["current_rev"] = rev
    workflow["state"] = "draft"
    workflow["locked"] = [node["id"] for node in nodes if node.get("locked")]
    workflow["env_schema"] = list(revision["env_schema"])
    workflow["updated_at"] = time.time()
    _write_workflow(root, wid, workflow)
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "revision",
            "actor": actor,
            "wid": wid,
            "from": base_rev,
            "to": rev,
            "rev": rev,
            "origin": revision["origin"],
            "state_from": old_state,
            "state_to": "draft",
            "note": audit_note or message,
        },
    )
    return revision


def _graph_of(revision: Mapping[str, Any]) -> dict[str, Any]:
    nodes = list(revision.get("nodes") or [])
    edges = list(revision.get("edges") or [])
    return {
        "nodes": nodes,
        "edges": edges,
        "order": [node.get("id") for node in nodes],
    }


# --------------------------------------------------------------------------- #
# 校验 / 风险
# --------------------------------------------------------------------------- #
def _env_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, str):
        for match in ENV_TEMPLATE_RE.finditer(value):
            key = match.group(1)
            if key not in keys:
                keys.append(key)
    elif isinstance(value, Mapping):
        for item in value.values():
            for key in _env_keys(item):
                if key not in keys:
                    keys.append(key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for key in _env_keys(item):
                if key not in keys:
                    keys.append(key)
    return keys


def _env_schema(nodes: Iterable[Mapping[str, Any]]) -> list[str]:
    keys: list[str] = []
    for node in nodes:
        for source in (
            node.get("params"),
            node.get("when"),
            node.get("foreach"),
            node.get("extra"),
        ):
            for key in _env_keys(source):
                if key not in keys:
                    keys.append(key)
    return sorted(keys)


def _registry() -> Any:
    from . import service

    return service.registry()


def _risks(nodes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    registry = _registry()
    risks: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id"))
        atom_name = str(node.get("atom") or "")
        if not registry.has(atom_name):
            continue
        atom = registry.get(atom_name)
        effects = atom.effects_for(node.get("params") or {})
        for effect in effects:
            value = str(effect)
            if value not in ("mutate", "destructive"):
                continue
            level = "high" if value == "destructive" else "medium"
            risks.append(
                {
                    "id": f"risk:{node_id}:{value}",
                    "node_id": node_id,
                    "kind": "side_effect",
                    "effect": value,
                    "level": level,
                    "message": f"节点 {node.get('name') or node_id} 会执行 {value} 副作用",
                }
            )
        secret_outputs = list(getattr(atom, "secret_outputs", ()) or ())
        capture = node.get("capture") or {}
        secret_capture = [
            name
            for name, spec in (capture.items() if isinstance(capture, Mapping) else [])
            if (isinstance(spec, Mapping) and spec.get("secret"))
            or _SENSITIVE_KEY_RE.search(str(name))
        ]
        if secret_outputs or secret_capture:
            names = sorted(set(secret_outputs) | set(secret_capture))
            risks.append(
                {
                    "id": f"risk:{node_id}:secret",
                    "node_id": node_id,
                    "kind": "secret",
                    "level": "medium",
                    "message": f"节点 {node.get('name') or node_id} 会读写密钥：{', '.join(names)}",
                }
            )
    return risks


def _issue_dict(issue: Any) -> dict[str, Any]:
    data = issue.to_dict()
    data["node_id"] = data.get("step_id") or ""
    return data


def _downstream_nodes(nodes: Iterable[Mapping[str, Any]], edges: Iterable[Mapping[str, Any]], roots: Iterable[str]) -> list[str]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge.get("from")), set()).add(str(edge.get("to")))
    seen = {str(item) for item in roots if item}
    queue = list(seen)
    while queue:
        current = queue.pop(0)
        for child in adjacency.get(current, set()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return sorted(node for node in seen if node)


def validate_nodes(
    root: Path,
    *,
    wid: str,
    rev: int,
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
    intent: str = "",
    env_schema: Iterable[str] = (),
) -> dict[str, Any]:
    nodes = list(nodes)
    plan = plan_from_nodes(nodes, source="studio", intent=intent)
    result = plan.check(_registry(), known_variables=set(env_schema))
    errors = [_issue_dict(issue) for issue in result.errors]
    warnings = [_issue_dict(issue) for issue in result.warnings]
    error_nodes = {item.get("node_id") for item in errors if item.get("node_id")}
    affected = _downstream_nodes(nodes, edges, error_nodes)
    return {
        "wid": wid,
        "rev": rev,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "risks": _risks(nodes),
        "affected_nodes": affected,
    }


# --------------------------------------------------------------------------- #
# 草案
# --------------------------------------------------------------------------- #
def _build_plan(
    root: Path,
    *,
    intent: str,
    domain_pack: str,
    exclude_atoms: Iterable[str] = (),
) -> Any:
    from . import service

    registry = service.registry()
    templates: list[Any] = []
    for pack in registry.packs():
        for template in getattr(pack, "intent_templates", []) or []:
            if domain_pack and domain_pack not in {
                str(getattr(template, "pack", "") or ""),
                str(getattr(template, "domain", "") or ""),
            }:
                continue
            templates.append(template)
    if not templates:
        hint = f"领域包 {domain_pack} 没有可用的意图模板" if domain_pack else "没有可用的意图模板"
        raise StudioError("bad_request", hint)
    planner = RuleBasedPlanner(registry, templates=templates, max_cases=3)
    plan = planner.plan(PlanRequest(intent=intent, env={}, allow_llm=False))

    excluded = {str(item) for item in exclude_atoms}
    if excluded:
        kept = []
        for case in plan.cases:
            case.steps = [step for step in case.steps if step.use not in excluded]
            if case.steps:
                kept.append(case)
        plan.cases = kept
    if not plan.cases:
        raise StudioError("bad_request", "意图没有生成任何可执行步骤（检查 exclude_atoms）")
    return plan


def _clean_exclude(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise StudioError("bad_request", "exclude_atoms 必须是字符串数组")
    return [item.strip() for item in value if item.strip()]


def _clean_domain_pack(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StudioError("bad_request", "domain_pack 必须是字符串")
    domain = value.strip()
    if not domain:
        return ""
    allowed = {"api_testing", "local_fs", "core"}
    if domain not in allowed:
        raise StudioError("bad_request", f"未知的领域包: {domain}")
    return domain


def create_draft(root: Path, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    intent = _clean_text(data.get("intent"), field="intent", required=True, limit=500)
    domain_pack = _clean_domain_pack(data.get("domain_pack"))
    exclude = _clean_exclude(data.get("exclude_atoms"))
    plan = _build_plan(root, intent=intent, domain_pack=domain_pack, exclude_atoms=exclude)

    graph = graph_from_plan(plan, origin="ai")
    wid = new_wid()
    env_schema = _env_schema(graph["nodes"])
    name = str(plan.cases[0].name or intent)[:80]
    now = time.time()
    workflow = {
        "wid": wid,
        "name": name,
        "intent": intent,
        "domain_pack": domain_pack,
        "state": "draft",
        "current_rev": 0,
        "locked": [],
        "published_ref": "",
        "env_schema": env_schema,
        "created_at": now,
        "updated_at": now,
        "created_by": actor,
    }
    revision = {
        "wid": wid,
        "rev": 0,
        "origin": "ai",
        "created_by": actor,
        "created_at": now,
        "message": "AI 生成初稿",
        "base_rev": None,
        "intent": intent,
        "domain_pack": domain_pack,
        "env_schema": env_schema,
        "nodes": graph["nodes"],
        "edges": graph["edges"],
    }
    _write_revision(root, wid, revision)
    _write_workflow(root, wid, workflow)
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "draft_created",
            "actor": actor,
            "wid": wid,
            "from": None,
            "to": "draft",
            "rev": 0,
            "origin": "ai",
            "note": f"意图：{intent}",
        },
    )
    validation = validate_nodes(
        root,
        wid=wid,
        rev=0,
        nodes=graph["nodes"],
        edges=graph["edges"],
        intent=intent,
        env_schema=env_schema,
    )
    return {
        "wid": wid,
        "rev": 0,
        "state": "draft",
        "graph": graph,
        "validation": validation,
    }


def list_drafts(
    root: Path, *, status: str | None = None, domain_pack: str | None = None
) -> dict[str, Any]:
    base = studio_root(root)
    items: list[dict[str, Any]] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if not child.is_dir() or not WID_RE.match(child.name):
                continue
            try:
                workflow = read_workflow(root, child.name)
            except StudioError:
                continue
            if status and workflow.get("state") != status:
                continue
            if domain_pack and workflow.get("domain_pack") != domain_pack:
                continue
            try:
                revision = read_revision(
                    root, child.name, int(workflow.get("current_rev") or 0)
                )
                node_count = len(revision.get("nodes") or [])
            except StudioError:
                node_count = 0
            items.append(
                {
                    "wid": workflow.get("wid"),
                    "name": workflow.get("name"),
                    "intent": workflow.get("intent"),
                    "domain_pack": workflow.get("domain_pack"),
                    "state": workflow.get("state"),
                    "current_rev": workflow.get("current_rev"),
                    "published_ref": workflow.get("published_ref"),
                    "node_count": node_count,
                    "created_at": workflow.get("created_at"),
                    "updated_at": workflow.get("updated_at"),
                }
            )
    items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return {"drafts": items}


def _ui_path(root: Path, wid: str) -> Path:
    return _workflow_dir(root, wid) / "ui.json"


def _read_ui(root: Path, wid: str) -> dict[str, Any]:
    path = _ui_path(root, wid)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_workflow(root: Path, wid: str, *, rev: int | None = None) -> dict[str, Any]:
    workflow = read_workflow(root, wid)
    target_rev = int(workflow.get("current_rev") or 0) if rev is None else _as_rev(rev)
    revision = read_revision(root, wid, target_rev)
    return {
        "wid": wid,
        "name": workflow.get("name"),
        "intent": workflow.get("intent"),
        "domain_pack": workflow.get("domain_pack"),
        "state": workflow.get("state"),
        "current_rev": workflow.get("current_rev"),
        "rev": target_rev,
        "origin": revision.get("origin"),
        "created_by": revision.get("created_by"),
        "created_at": revision.get("created_at"),
        "message": revision.get("message"),
        "env_schema": revision.get("env_schema") or workflow.get("env_schema") or [],
        "locked": workflow.get("locked") or [],
        "published_ref": workflow.get("published_ref") or "",
        "graph": _graph_of(revision),
        "ui": _read_ui(root, wid),
    }


# --------------------------------------------------------------------------- #
# 节点 / 边编辑
# --------------------------------------------------------------------------- #
_NODE_FIELDS = {
    "name",
    "atom",
    "params",
    "with",
    "capture",
    "when",
    "foreach",
    "as",
    "retry",
    "continue_on_failure",
    "tags",
    "optional",
    "extra",
    "description",
    "locked",
    "ui",
    "case_id",
    "case_name",
}


def _find_node(nodes: Iterable[Mapping[str, Any]], node_id: str) -> dict[str, Any]:
    for node in nodes:
        if str(node.get("id")) == node_id:
            return dict(node)
    raise StudioError("not_found", f"节点不存在: {node_id}")


def _apply_node_patch(node: dict[str, Any], data: Mapping[str, Any]) -> None:
    unknown = sorted(set(data) - _NODE_FIELDS)
    if unknown:
        raise StudioError("bad_request", f"节点字段不支持: {', '.join(unknown)}")
    if "atom" in data:
        atom = data.get("atom")
        if not isinstance(atom, str) or not atom.strip():
            raise StudioError("bad_request", "atom 必须是非空字符串")
        if not _registry().has(atom.strip()):
            raise StudioError("bad_request", f"原子不存在: {atom.strip()}")
        node["atom"] = atom.strip()
    if "name" in data:
        node["name"] = _clean_text(data.get("name"), field="name", limit=200)
    if "description" in data:
        node["description"] = _clean_text(data.get("description"), field="description")
    if "params" in data or "with" in data:
        params = data.get("params", data.get("with"))
        if params is not None and not isinstance(params, dict):
            raise StudioError("bad_request", "params 必须是对象")
        node["params"] = copy.deepcopy(params or {})
    if "capture" in data:
        capture = data.get("capture")
        if capture is not None and not isinstance(capture, dict):
            raise StudioError("bad_request", "capture 必须是对象")
        node["capture"] = copy.deepcopy(capture or {})
    for key in ("when", "foreach", "as"):
        if key in data:
            value = data.get(key)
            if value is not None and not isinstance(value, str):
                raise StudioError("bad_request", f"{key} 必须是字符串或 null")
            node[key] = value.strip() if isinstance(value, str) else None
    if "retry" in data:
        retry = data.get("retry")
        if retry is not None and not isinstance(retry, (dict, bool, int)):
            raise StudioError("bad_request", "retry 必须是对象/布尔/整数或 null")
        node["retry"] = copy.deepcopy(retry)
    for key in ("continue_on_failure", "optional"):
        if key in data:
            node[key] = bool(data.get(key))
    if "locked" in data:
        node["locked"] = bool(data.get("locked"))
    if "tags" in data:
        tags = data.get("tags")
        if tags is not None and (
            not isinstance(tags, list) or any(not isinstance(item, str) for item in tags)
        ):
            raise StudioError("bad_request", "tags 必须是字符串数组")
        node["tags"] = [item.strip() for item in (tags or []) if item.strip()]
    if "extra" in data:
        extra = data.get("extra")
        if extra is not None and not isinstance(extra, dict):
            raise StudioError("bad_request", "extra 必须是对象")
        node["extra"] = copy.deepcopy(extra or {})
    if "case_id" in data:
        node["case_id"] = _clean_text(data.get("case_id"), field="case_id", required=True, limit=120)
    if "case_name" in data:
        node["case_name"] = _clean_text(data.get("case_name"), field="case_name", limit=200)
    node["origin"] = "human"


def _update_ui(root: Path, wid: str, ui: Any) -> dict[str, Any]:
    if not isinstance(ui, dict):
        raise StudioError("bad_request", "ui 必须是对象")
    current = _read_ui(root, wid)
    for node_id, position in ui.items():
        if not isinstance(position, dict):
            raise StudioError("bad_request", f"ui.{node_id} 必须是 {{x,y}}")
        x = position.get("x")
        y = position.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise StudioError("bad_request", f"ui.{node_id}.x/y 必须是数字")
        current[str(node_id)] = {"x": float(x), "y": float(y)}
    _atomic_write_json(_ui_path(root, wid), current)
    return current


def _node_payload(root: Path, wid: str, revision: Mapping[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    nodes = list(revision.get("nodes") or [])
    edges = list(revision.get("edges") or [])
    validation = validate_nodes(
        root,
        wid=wid,
        rev=int(revision.get("rev") or 0),
        nodes=nodes,
        edges=edges,
        intent=str(workflow.get("intent") or ""),
        env_schema=revision.get("env_schema") or [],
    )
    return {
        "wid": wid,
        "rev": revision.get("rev"),
        "state": workflow.get("state"),
        "graph": _graph_of(revision),
        "validation": validation,
        "ui": _read_ui(root, wid),
    }


def patch_node(
    root: Path, wid: str, node_id: str, payload: Any, *, actor: str = "local"
) -> dict[str, Any]:
    data = _as_dict(payload)
    workflow = read_workflow(root, wid)
    revision = read_revision(root, wid, int(workflow.get("current_rev") or 0))
    nodes = [dict(node) for node in revision.get("nodes") or []]

    if set(data) == {"ui"}:
        ui = _update_ui(root, wid, data.get("ui"))
        return {
            "wid": wid,
            "rev": workflow.get("current_rev"),
            "state": workflow.get("state"),
            "ui": ui,
            "graph": _graph_of(revision),
        }

    node = _find_node(nodes, node_id)
    _apply_node_patch(node, data)
    for index, item in enumerate(nodes):
        if str(item.get("id")) == node_id:
            nodes[index] = node
            break
    if "ui" in data:
        _update_ui(root, wid, data.get("ui"))
    new_rev = _commit_revision(
        root,
        workflow,
        wid=wid,
        nodes=nodes,
        edges=list(revision.get("edges") or []),
        origin="human",
        actor=actor,
        message=f"人工修改节点 {node_id}",
        base_rev=int(revision.get("rev") or 0),
        audit_note=f"PATCH node {node_id}",
    )
    return _node_payload(root, wid, new_rev, workflow)


def _make_node(data: Mapping[str, Any], *, case_id: str, case_name: str) -> dict[str, Any]:
    atom = data.get("atom")
    if not isinstance(atom, str) or not atom.strip():
        raise StudioError("bad_request", "atom 必须是非空字符串")
    atom = atom.strip()
    if not _registry().has(atom):
        raise StudioError("bad_request", f"原子不存在: {atom}")
    spec: dict[str, Any] = {
        "id": data.get("id"),
        "use": atom,
        "name": data.get("name") or "",
        "description": data.get("description") or "",
        "with": data.get("params", data.get("with")) or {},
        "capture": data.get("capture") or {},
    }
    for key in ("when", "foreach", "as", "retry", "continue_on_failure", "tags", "optional"):
        if key in data and data.get(key) is not None:
            spec[key] = data.get(key)
    extra = data.get("extra")
    if isinstance(extra, Mapping):
        spec.update(extra)
    from ..core.molecule import Step

    step = Step.from_spec(spec)
    node = node_from_step(
        step,
        case_id=case_id,
        case_name=case_name,
        origin="human",
        locked=bool(data.get("locked", False)),
    )
    return node


def add_node(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    workflow = read_workflow(root, wid)
    revision = read_revision(root, wid, int(workflow.get("current_rev") or 0))
    nodes = [dict(node) for node in revision.get("nodes") or []]
    edges = [dict(edge) for edge in revision.get("edges") or []]

    last_case = nodes[-1].get("case_id") if nodes else "flow"
    last_case_name = nodes[-1].get("case_name") if nodes else "流程"
    case_id = str(data.get("case_id") or last_case or "flow")
    case_name = str(data.get("case_name") or last_case_name or case_id)
    node = _make_node(data, case_id=case_id, case_name=case_name)
    if any(str(item.get("id")) == node["id"] for item in nodes):
        raise StudioError("conflict", f"节点 id 已存在: {node['id']}")

    after = data.get("after")
    if after is None:
        index = len(nodes)
    else:
        index = next(
            (pos + 1 for pos, item in enumerate(nodes) if str(item.get("id")) == str(after)),
            None,
        )
        if index is None:
            raise StudioError("not_found", f"after 节点不存在: {after}")
    nodes.insert(index, node)

    # 仅补 order 边，绝不伪造数据依赖
    if index > 0:
        _ensure_order_edge(edges, str(nodes[index - 1]["id"]), node["id"])
    if index + 1 < len(nodes):
        _ensure_order_edge(edges, node["id"], str(nodes[index + 1]["id"]))

    new_rev = _commit_revision(
        root,
        workflow,
        wid=wid,
        nodes=nodes,
        edges=edges,
        origin="human",
        actor=actor,
        message=f"新增节点 {node['id']}",
        base_rev=int(revision.get("rev") or 0),
        audit_note=f"POST node {node['id']}",
    )
    return _node_payload(root, wid, new_rev, workflow)


def _ensure_order_edge(edges: list[dict[str, Any]], from_id: str, to_id: str) -> None:
    if from_id == to_id:
        return
    if any(
        str(edge.get("from")) == from_id and str(edge.get("to")) == to_id for edge in edges
    ):
        return
    edges.append(
        {
            "id": f"e-{from_id}->{to_id}:order",
            "from": from_id,
            "to": to_id,
            "via": None,
            "kind": "order",
            "order_only": True,
        }
    )


def delete_node(
    root: Path, wid: str, node_id: str, *, actor: str = "local"
) -> dict[str, Any]:
    workflow = read_workflow(root, wid)
    revision = read_revision(root, wid, int(workflow.get("current_rev") or 0))
    nodes = [dict(node) for node in revision.get("nodes") or []]
    _find_node(nodes, node_id)
    index = next(pos for pos, item in enumerate(nodes) if str(item.get("id")) == node_id)
    nodes.pop(index)
    edges = [
        dict(edge)
        for edge in revision.get("edges") or []
        if node_id not in {str(edge.get("from")), str(edge.get("to"))}
    ]
    if 0 < index < len(nodes) + 1 and index - 1 < len(nodes) and index < len(nodes):
        _ensure_order_edge(edges, str(nodes[index - 1]["id"]), str(nodes[index]["id"]))
    new_rev = _commit_revision(
        root,
        workflow,
        wid=wid,
        nodes=nodes,
        edges=edges,
        origin="human",
        actor=actor,
        message=f"删除节点 {node_id}",
        base_rev=int(revision.get("rev") or 0),
        audit_note=f"DELETE node {node_id}",
    )
    return _node_payload(root, wid, new_rev, workflow)


def add_edge(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    workflow = read_workflow(root, wid)
    revision = read_revision(root, wid, int(workflow.get("current_rev") or 0))
    nodes = [dict(node) for node in revision.get("nodes") or []]
    edges = [dict(edge) for edge in revision.get("edges") or []]

    from_id = _clean_text(data.get("from"), field="from", required=True, limit=200)
    to_id = _clean_text(data.get("to"), field="to", required=True, limit=200)
    _find_node(nodes, from_id)
    _find_node(nodes, to_id)
    if from_id == to_id:
        raise StudioError("bad_request", "不允许自环边")
    via = data.get("via")
    via_text = _clean_text(via, field="via", limit=200) if via not in (None, "") else None
    kind = data.get("kind")
    if kind in (None, ""):
        kind = "data" if via_text else "order"
    if kind not in ("data", "order"):
        raise StudioError("bad_request", "kind 只能是 data/order")
    if kind == "data":
        if not via_text:
            raise StudioError("bad_request", "数据边必须提供 via（变量名）")
        producer = next(node for node in nodes if str(node.get("id")) == from_id)
        produced = set((producer.get("capture") or {}).keys()) | {from_id}
        root = template_roots("${" + via_text + "}")
        name = root[0] if root else via_text
        if name not in produced:
            raise StudioError(
                "bad_request",
                f"via={via_text} 不是节点 {from_id} 产出的变量（不得伪造数据依赖）",
            )
    else:
        via_text = None
    for edge in edges:
        if (
            str(edge.get("from")) == from_id
            and str(edge.get("to")) == to_id
            and str(edge.get("via") or "") == str(via_text or "")
            and str(edge.get("kind") or "order") == kind
        ):
            raise StudioError("conflict", "边已存在")
    edge = {
        "id": f"e-{from_id}->{to_id}:{'data-' + via_text if kind == 'data' else 'order'}",
        "from": from_id,
        "to": to_id,
        "via": via_text,
        "kind": kind,
        "order_only": kind != "data",
    }
    edges.append(edge)
    new_rev = _commit_revision(
        root,
        workflow,
        wid=wid,
        nodes=nodes,
        edges=edges,
        origin="human",
        actor=actor,
        message=f"新增边 {from_id}→{to_id}",
        base_rev=int(revision.get("rev") or 0),
        audit_note=f"POST edge {edge['id']}",
    )
    return _node_payload(root, wid, new_rev, workflow)


# --------------------------------------------------------------------------- #
# replan / apply-diff
# --------------------------------------------------------------------------- #
def _locked_conflicts(
    base_nodes: Iterable[Mapping[str, Any]], target_nodes: Iterable[Mapping[str, Any]]
) -> list[str]:
    target_by_id = {str(node.get("id")): node for node in target_nodes}
    conflicts: list[str] = []
    for node in base_nodes:
        if not node.get("locked"):
            continue
        node_id = str(node.get("id"))
        target = target_by_id.get(node_id)
        if target is None or node_signature(node) != node_signature(target):
            conflicts.append(node_id)
    return sorted(set(conflicts))


def replan(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    instruction = _clean_text(data.get("instruction"), field="instruction", required=True)
    workflow = read_workflow(root, wid)
    base_rev = int(workflow.get("current_rev") or 0)
    revision = read_revision(root, wid, base_rev)

    plan = _build_plan(
        root,
        intent=instruction,
        domain_pack=str(workflow.get("domain_pack") or ""),
        exclude_atoms=_clean_exclude(data.get("exclude_atoms")),
    )
    proposed = graph_from_plan(
        plan,
        origin="ai",
        locked=[str(node.get("id")) for node in revision.get("nodes") or [] if node.get("locked")],
    )
    conflicts = _locked_conflicts(revision.get("nodes") or [], proposed["nodes"])
    if conflicts:
        raise StudioError(
            "conflict",
            "锁定节点不允许被 replan 修改",
            details={"locked_nodes": conflicts},
        )

    diff = diff_graphs(
        revision.get("nodes") or [],
        revision.get("edges") or [],
        proposed["nodes"],
        proposed["edges"],
    )
    if not diff["nodes"] and not diff["edges"]:
        return {
            "wid": wid,
            "base_rev": base_rev,
            "proposed_rev": None,
            "diff": diff,
            "locked_nodes": [],
        }

    proposed_rev = base_rev + 1
    proposal = {
        "wid": wid,
        "base_rev": base_rev,
        "proposed_rev": proposed_rev,
        "instruction": instruction,
        "nodes": proposed["nodes"],
        "edges": proposed["edges"],
        "diff": diff,
        "created_by": actor,
        "created_at": time.time(),
    }
    _atomic_write_json(
        _workflow_dir(root, wid) / "proposals" / f"{proposed_rev}.json", proposal
    )
    return {
        "wid": wid,
        "base_rev": base_rev,
        "proposed_rev": proposed_rev,
        "diff": diff,
        "locked_nodes": [],
        "graph": proposed,
    }


def apply_diff(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    proposed_rev = _as_rev(data.get("proposed_rev"), field="proposed_rev")
    accepted = data.get("accepted")
    if not isinstance(accepted, list) or any(not isinstance(item, str) for item in accepted):
        raise StudioError("bad_request", "accepted 必须是字符串数组")
    if not accepted:
        raise StudioError("bad_request", "accepted 不能为空（请至少接受一项变更）")

    workflow = read_workflow(root, wid)
    proposal = _read_json(
        _workflow_dir(root, wid) / "proposals" / f"{proposed_rev}.json",
        not_found=f"提案 {proposed_rev} 不存在",
        broken=f"提案 {proposed_rev} 无法解析",
    )
    if int(proposal.get("base_rev") or 0) != int(workflow.get("current_rev") or 0):
        raise StudioError("conflict", "提案已过期：请重新 replan")

    revision = read_revision(root, wid, int(workflow.get("current_rev") or 0))
    diff = proposal.get("diff") or {}
    known = {
        str(change.get("change_id"))
        for change in list(diff.get("nodes") or []) + list(diff.get("edges") or [])
    }
    unknown = sorted(set(accepted) - known)
    if unknown:
        raise StudioError("bad_request", f"未知的变更 id: {', '.join(unknown[:5])}")

    locked_ids = {
        str(node.get("id")) for node in revision.get("nodes") or [] if node.get("locked")
    }
    conflicts: list[str] = []
    for change in diff.get("nodes") or []:
        if str(change.get("change_id")) not in accepted:
            continue
        node_id = str(change.get("id"))
        if node_id in locked_ids and change.get("action") in ("changed", "removed"):
            conflicts.append(node_id)
    for change in diff.get("edges") or []:
        if str(change.get("change_id")) not in accepted:
            continue
        for node_id in sorted(locked_ids):
            if edge_change_touches_node(change, node_id):
                conflicts.append(node_id)
    if conflicts:
        raise StudioError(
            "conflict",
            "锁定节点不允许被 apply-diff 修改",
            details={"locked_nodes": sorted(set(conflicts))},
        )

    nodes, edges, applied = apply_graph_diff(
        revision.get("nodes") or [],
        revision.get("edges") or [],
        diff,
        accepted,
    )
    node_ids = {str(node.get("id")) for node in nodes}
    dangling = [
        edge
        for edge in edges
        if str(edge.get("from")) not in node_ids or str(edge.get("to")) not in node_ids
    ]
    if dangling:
        # 用户可能只接受「删节点」而拒绝「删边」：不静默改图，但也不让部分接受失败。
        # 丢弃悬空边并在响应里明确列出，前端可提示重新整理连线。
        dangling_ids = {str(edge.get("id")) for edge in dangling}
        edges = [edge for edge in edges if str(edge.get("id")) not in dangling_ids]

    new_rev = _commit_revision(
        root,
        workflow,
        wid=wid,
        nodes=nodes,
        edges=edges,
        origin="human",
        actor=actor,
        message=f"应用 AI 建议（提案 {proposed_rev}）",
        base_rev=int(revision.get("rev") or 0),
        audit_note=f"apply-diff proposal={proposed_rev} accepted={len(applied)}",
    )
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "diff_applied",
            "actor": actor,
            "wid": wid,
            "from": revision.get("rev"),
            "to": new_rev.get("rev"),
            "rev": new_rev.get("rev"),
            "proposal": proposed_rev,
            "accepted": list(accepted),
            "note": str(proposal.get("instruction") or ""),
        },
    )
    payload = _node_payload(root, wid, new_rev, workflow)
    payload["dropped_edges"] = sorted(str(edge.get("id")) for edge in dangling)
    return payload


# --------------------------------------------------------------------------- #
# 评审 / 状态 / 发布
# --------------------------------------------------------------------------- #
def review(root: Path, wid: str, payload: Any) -> dict[str, Any]:
    data = _as_dict(payload)
    if "rev" not in data:
        raise StudioError("bad_request", "rev 不能为空")
    rev = _as_rev(data.get("rev"))
    base_rev = _as_rev(data.get("base_rev", max(0, rev - 1)), field="base_rev")
    workflow = read_workflow(root, wid)
    base = read_revision(root, wid, base_rev)
    target = read_revision(root, wid, rev)
    diff = diff_graphs(
        base.get("nodes") or [],
        base.get("edges") or [],
        target.get("nodes") or [],
        target.get("edges") or [],
    )
    return {
        "wid": wid,
        "base_rev": base_rev,
        "rev": rev,
        "state": workflow.get("state"),
        "diff": diff,
        "risks": _risks(target.get("nodes") or []),
        "validation": validate_nodes(
            root,
            wid=wid,
            rev=rev,
            nodes=target.get("nodes") or [],
            edges=target.get("edges") or [],
            intent=str(workflow.get("intent") or ""),
            env_schema=target.get("env_schema") or [],
        ),
        "audit_tail": audit_tail(root, wid, limit=20),
    }


def transition_state(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    to = data.get("to")
    if to not in TRANSITIONS:
        raise StudioError("bad_request", "to 只能是 review/approved/rejected")
    note = _clean_text(data.get("note"), field="note", limit=MAX_TEXT)
    workflow = read_workflow(root, wid)
    revision = read_revision(root, wid, int(workflow.get("current_rev") or 0))
    old_state = str(workflow.get("state") or "draft")
    risks = _risks(revision.get("nodes") or [])

    if to == "approved" and risks:
        ack = data.get("risk_ack") is True
        confirmed = data.get("confirmed_risks")
        if confirmed is None:
            confirmed_set: set[str] = set()
        elif isinstance(confirmed, list):
            confirmed_set = {str(item) for item in confirmed}
        else:
            raise StudioError("bad_request", "confirmed_risks 必须是字符串数组")
        unconfirmed = [risk for risk in risks if not ack and risk["id"] not in confirmed_set]
        if unconfirmed:
            raise StudioError(
                "risk_unconfirmed",
                "存在未确认的风险，不能批准",
                details={"risks": unconfirmed, "locked_nodes": []},
            )

    workflow["state"] = to
    workflow["updated_at"] = time.time()
    _write_workflow(root, wid, workflow)
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "state",
            "actor": actor,
            "wid": wid,
            "from": old_state,
            "to": to,
            "rev": workflow.get("current_rev"),
            "note": note,
        },
    )
    return {
        "wid": wid,
        "state": to,
        "rev": workflow.get("current_rev"),
        "risks": risks,
        "note": note,
    }


def publish(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    rev = _as_rev(data.get("rev"))
    message = _clean_text(data.get("message"), field="message", required=True)
    workflow = read_workflow(root, wid)
    if workflow.get("state") != "approved":
        raise StudioError("not_approved", "只有 approved 状态的工作流才能发布")
    if rev != int(workflow.get("current_rev") or 0):
        raise StudioError("conflict", "只能发布当前版本（请先重新批准最新 rev）")
    revision = read_revision(root, wid, rev)

    ref = f"{wid}@{rev}"
    published_path = _workflow_dir(root, wid) / "published" / f"{rev}.json"
    if published_path.exists():
        record = _read_json(
            published_path,
            not_found="发布记录不存在",
            broken="发布记录无法解析",
        )
        return {
            "workflow_ref": ref,
            "wid": wid,
            "rev": rev,
            "message": record.get("message"),
            "published_at": record.get("published_at"),
            "reused": True,
        }

    record = {
        "workflow_ref": ref,
        "wid": wid,
        "rev": rev,
        "message": message,
        "name": workflow.get("name"),
        "intent": workflow.get("intent"),
        "domain_pack": workflow.get("domain_pack"),
        "published_by": actor,
        "published_at": time.time(),
        "node_count": len(revision.get("nodes") or []),
    }
    _atomic_write_json(published_path, record, overwrite=False)
    old_ref = workflow.get("published_ref") or ""
    workflow["published_ref"] = ref
    workflow["updated_at"] = time.time()
    _write_workflow(root, wid, workflow)
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "publish",
            "actor": actor,
            "wid": wid,
            "from": old_ref or None,
            "to": ref,
            "rev": rev,
            "note": message,
        },
    )
    return {
        "workflow_ref": ref,
        "wid": wid,
        "rev": rev,
        "message": message,
        "published_at": record["published_at"],
        "reused": False,
    }


def load_published(root: Path, ref: Any) -> dict[str, Any]:
    if not isinstance(ref, str):
        raise StudioError("bad_request", "workflow_ref 必须是字符串")
    match = WORKFLOW_REF_RE.match(ref.strip())
    if not match:
        raise StudioError("bad_request", "workflow_ref 非法（应形如 w-8f3a1c@3）")
    wid, rev = match.group(1), int(match.group(2))
    record_path = _workflow_dir(root, wid) / "published" / f"{rev}.json"
    record = _read_json(
        record_path,
        not_found="工作流未发布或版本不存在",
        broken="发布记录无法解析",
    )
    if record.get("workflow_ref") != f"{wid}@{rev}":
        raise StudioError("internal_error", "发布记录与引用不一致")
    revision = read_revision(root, wid, rev)
    workflow = read_workflow(root, wid)
    return {
        "workflow_ref": f"{wid}@{rev}",
        "wid": wid,
        "rev": rev,
        "workflow": workflow,
        "revision": revision,
        "record": record,
    }


def plan_from_revision(published: Mapping[str, Any], *, source: str = "studio") -> Any:
    revision = published.get("revision") or published
    workflow = published.get("workflow") or {}
    plan = plan_from_nodes(
        revision.get("nodes") or [],
        source=source,
        intent=str(workflow.get("intent") or revision.get("intent") or ""),
        planner="studio",
    )
    plan.meta = {
        **plan.meta,
        "workflow_ref": published.get("workflow_ref")
        or f"{revision.get('wid')}@{revision.get('rev')}",
        "wid": revision.get("wid"),
        "rev": revision.get("rev"),
    }
    return plan


# --------------------------------------------------------------------------- #
# 调试（隔离，不入 /api/runs）
# --------------------------------------------------------------------------- #
_FORBIDDEN_DEBUG_KEYS = {"base_url", "baseUrl", "url", "uri", "host", "target", "endpoint"}


def _is_url_key(key: str) -> bool:
    return bool(_URL_KEY_RE.search(key))


def _debug_env(root: Path, data: Mapping[str, Any]) -> dict[str, Any]:
    from . import service

    for key in data:
        if key in _FORBIDDEN_DEBUG_KEYS:
            raise StudioError(
                "bad_request", f"调试不允许覆盖 {key}（只能使用项目已配置 env / last_env 基线）"
            )
    allowed: dict[str, Any] = {}
    baseline = service.load_last_env(root)
    if isinstance(baseline, dict):
        allowed.update(baseline)
    project_id = data.get("project_id")
    if project_id not in (None, ""):
        project = projects.read_project(root, str(project_id))
        project_env = project.get("env")
        if isinstance(project_env, dict):
            allowed.update(project_env)

    requested = data.get("env")
    if requested is not None:
        if not isinstance(requested, dict):
            raise StudioError("bad_request", "env 必须是对象")
        for key, value in requested.items():
            if not isinstance(key, str) or not key.strip():
                raise StudioError("bad_request", "env 的键必须是非空字符串")
            if _is_url_key(key):
                raise StudioError(
                    "bad_request",
                    f"调试不允许覆盖 URL 类环境变量 {key}（只能使用项目已配置 env / last_env 基线）",
                )
            if key not in allowed:
                raise StudioError(
                    "bad_request", f"调试 env 只能使用项目已配置或 last_env 基线：{key}"
                )
            if allowed.get(key) != value:
                raise StudioError("bad_request", f"调试不允许覆盖基线环境变量：{key}")

    # 只有请求显式覆盖 FS_ROOT 才会顶掉基线；这里把「已配置的 FS_ROOT」视为显式，
    # 否则 apply_fs_root 会把项目/last_env 的目录覆盖成 workspace 默认目录。
    explicit = {"FS_ROOT": allowed["FS_ROOT"]} if "FS_ROOT" in allowed else {}
    effective = service.apply_fs_root(root, allowed, explicit)
    return dict(effective)


def _select_nodes(
    nodes: list[dict[str, Any]], node_id: str, mode: str
) -> list[dict[str, Any]]:
    index = next(
        (pos for pos, node in enumerate(nodes) if str(node.get("id")) == node_id), None
    )
    if index is None:
        raise StudioError("not_found", f"节点不存在: {node_id}")
    if mode == "only":
        return [nodes[index]]
    if mode == "from_start":
        return nodes[: index + 1]
    return nodes[index:]


def debug_workflow(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    from . import service

    data = _as_dict(payload)
    mode = data.get("mode") or "only"
    if mode not in DEBUG_MODES:
        raise StudioError("bad_request", "mode 只能是 only/from_start/downstream")
    node_id = _clean_text(data.get("node_id"), field="node_id", required=True, limit=200)
    workflow = read_workflow(root, wid)
    rev = (
        int(workflow.get("current_rev") or 0)
        if data.get("rev") is None
        else _as_rev(data.get("rev"))
    )
    revision = read_revision(root, wid, rev)
    nodes = list(revision.get("nodes") or [])
    selected = _select_nodes(nodes, node_id, mode)
    effective_env = _debug_env(root, data)

    plan = plan_from_nodes(
        selected,
        source="studio-debug",
        intent=f"调试 {node_id}",
        planner="studio",
    )
    if not plan.cases:
        raise StudioError("bad_request", "调试没有选中任何可执行步骤")

    debug_id = "dbg-" + uuid.uuid4().hex[:12]
    axe = service.build_axe(root, enable_llm=False)
    run = axe.executor.run(
        plan,
        env=effective_env,
        run_id=debug_id,
        meta={
            "debug": True,
            "trigger": "debug",
            "workflow_ref": f"{wid}@{rev}",
            "node_id": node_id,
            "mode": mode,
        },
    )

    capture_sources: dict[str, str] = {}
    for node in nodes:
        for name in (node.get("capture") or {}):
            capture_sources[str(name)] = str(node.get("id"))
    for node in nodes:
        capture_sources.setdefault(str(node.get("id")), str(node.get("id")))

    trace: list[dict[str, Any]] = []
    variables: dict[str, Any] = {}
    env_snapshot: dict[str, Any] = {}
    secret_values: list[str] = []
    for case in run.cases:
        secret_values.extend(
            value for value in (case.meta.get("secret_values") or []) if isinstance(value, str)
        )
        snapshot = case.variables or {}
        for name, value in (snapshot.get("vars") or {}).items():
            variables[name] = {
                "value": value,
                "source": capture_sources.get(str(name), "runtime"),
                "from_snapshot": True,
                "case_id": case.case_id,
            }
        if isinstance(snapshot.get("env"), dict):
            env_snapshot.update(snapshot["env"])
        for step in case.steps:
            item = step.to_dict()
            trace.append(
                {
                    "node_id": step.step_id,
                    "case_id": case.case_id,
                    "atom": step.atom,
                    "name": step.name,
                    "status": item.get("status"),
                    "duration_ms": item.get("duration_ms"),
                    "attempts": item.get("attempts"),
                    "inputs": item.get("inputs"),
                    "outputs": item.get("outputs"),
                    "error": item.get("error"),
                    "skipped_reason": step.skipped_reason,
                }
            )

    result = {
        "debug_id": debug_id,
        "wid": wid,
        "rev": rev,
        "workflow_ref": f"{wid}@{rev}",
        "node_id": node_id,
        "mode": mode,
        "status": "ok" if run.summary.get("success") else "failed",
        "summary": run.summary,
        "trace": trace,
        "variables": {"vars": variables, "env": env_snapshot},
    }
    # 双保险：先按已知密钥值/key 名打码，再走 HTTP 层同一套 env 打码
    masked = mask_obj(result, secret_values)
    from .api import mask_env_payload

    masked = mask_env_payload(masked)
    debug_dir = _workflow_dir(root, wid) / "debug"
    _atomic_write_json(debug_dir / f"{debug_id}.json", masked)
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "debug",
            "actor": actor,
            "wid": wid,
            "from": None,
            "to": debug_id,
            "rev": rev,
            "note": f"{node_id} ({mode})",
        },
    )
    return masked


__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "STATE_DIRNAME",
    "STUDIO_DIRNAME",
    "StudioError",
    "apply_diff",
    "audit_tail",
    "create_draft",
    "debug_workflow",
    "get_workflow",
    "list_drafts",
    "load_published",
    "plan_from_revision",
    "publish",
    "read_revision",
    "read_workflow",
    "replan",
    "review",
    "transition_state",
    "validate_nodes",
]
