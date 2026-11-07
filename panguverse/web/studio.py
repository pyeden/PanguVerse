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
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..axe.plan import PlanRequest
from ..axe.planner import RuleBasedPlanner
from ..core.context import MISSING, RunContext, split_path, walk_path
from ..core.contract import failure_is_retryable
from ..core.errors import AtomFailure, PanguError, StepFailure
from ..core.molecule import Step
from ..core.redact import mask_obj
from . import param_fill, planner_gate, projects
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
#: 站点别名只服务网页域；非网页域不暴露 url，也不改写接口/设备节点参数。
_WEB_DOMAINS = frozenset({"web_research", "web_browser"})
MAX_TEXT = 2000

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "not_approved": 409,
    "risk_unconfirmed": 409,
    "intent_mismatch": 409,
    "capability_gap": 409,
    "not_general": 422,
    "replay_failed": 422,
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
    intent: str | None = None,
    name: str | None = None,
    planner: str | None = None,
    plan_source: str | None = None,
    confidence: float | None = None,
    assumptions: list[str] | None = None,
    capability_gaps: list[str] | None = None,
    param_audit: Mapping[str, Any] | None = None,
    domain_pack: str | None = None,
) -> dict[str, Any]:
    rev = int(workflow.get("current_rev") or 0) + 1
    if intent is not None and str(intent).strip():
        workflow["intent"] = str(intent)
    if domain_pack is not None and str(domain_pack).strip():
        workflow["domain_pack"] = str(domain_pack)
    old_state = str(workflow.get("state") or "draft")
    # 新版本必须自带元数据：缺字段时只继承**上一版**（同一张图的人工编辑），
    # 绝不回退到 workflow 当前值，否则 apply 后会把旧草案的假设/名称带进新版本。
    previous: dict[str, Any] = {}
    if base_rev is not None:
        try:
            previous = read_revision(root, wid, int(base_rev))
        except StudioError:
            previous = {}

    def _inherit(value: Any, key: str) -> Any:
        if value is not None:
            return value
        return previous.get(key)

    resolved_name = _inherit(name, "name")
    if not str(resolved_name or "").strip():
        # 历史版本没有 name 字段时，从图里的 case_name 恢复名称。
        for node in nodes:
            case_name = str(node.get("case_name") or "").strip()
            if case_name:
                resolved_name = case_name
                break
    if not str(resolved_name or "").strip():
        resolved_name = workflow.get("name") or ""
    metadata = {
        "planner": _inherit(planner, "planner"),
        "plan_source": _inherit(plan_source, "plan_source"),
        "confidence": _inherit(confidence, "confidence"),
        "assumptions": _inherit(assumptions, "assumptions"),
        "capability_gaps": _inherit(capability_gaps, "capability_gaps"),
        "param_audit": _inherit(param_audit, "param_audit"),
    }
    revision = {
        "wid": wid,
        "rev": rev,
        "origin": "human" if origin == "human" else "ai",
        "created_by": actor,
        "created_at": time.time(),
        "message": message,
        "base_rev": base_rev,
        "name": str(resolved_name or ""),
        "intent": workflow.get("intent") or "",
        "domain_pack": workflow.get("domain_pack") or "",
        **{key: copy.deepcopy(value) for key, value in metadata.items() if value is not None},
        "env_schema": list(env_schema if env_schema is not None else _env_schema(nodes)),
        "nodes": copy.deepcopy(nodes),
        "edges": copy.deepcopy(edges),
    }
    _write_revision(root, wid, revision)
    workflow["current_rev"] = rev
    workflow["state"] = "draft"
    workflow["name"] = revision["name"]
    workflow["locked"] = [node["id"] for node in nodes if node.get("locked")]
    workflow["env_schema"] = list(revision["env_schema"])
    for key, value in metadata.items():
        if value is not None:
            workflow[key] = copy.deepcopy(value)
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


def _revision_name(revision: Mapping[str, Any], workflow: Mapping[str, Any]) -> str:
    """按版本恢复展示名称；旧版本没有 name 字段时用节点 case_name。"""
    name = str(revision.get("name") or "").strip()
    if name:
        return name
    for node in revision.get("nodes") or []:
        case_name = str(node.get("case_name") or "").strip()
        if case_name:
            return case_name
    return str(workflow.get("name") or "")


def _plan_assumptions(plan: Any, nodes: Iterable[Mapping[str, Any]]) -> list[str]:
    """从新计划 + 最终图重新推导假设，不带旧草案的模板假设。"""
    items: list[str] = []
    for case in getattr(plan, "cases", []) or []:
        meta = getattr(case, "meta", None) or {}
        for item in meta.get("assumptions") or ():
            text_item = str(item).strip()
            if text_item and text_item not in items:
                items.append(text_item)
    env_keys = _env_schema(nodes)
    for key in env_keys:
        text = f"需要 {key}"
        if text not in items:
            items.append(text)
    # 自动填充 URL 后，旧模板里的「需要 X」若已不在图中则丢弃。
    result: list[str] = []
    for item in items:
        match = re.fullmatch(r"需要 ([A-Za-z_][A-Za-z0-9_]*)", item)
        if match and match.group(1) not in env_keys:
            continue
        result.append(item)
    return sorted(dict.fromkeys(result))


def _plan_confidence(plan: Any, domain_pack: str) -> float:
    try:
        value = float((getattr(plan, "meta", None) or {}).get("confidence"))
    except (TypeError, ValueError):
        value = 0.45 if domain_pack == "web_research" else 0.4
    return max(0.0, min(1.0, value))


def _merge_assumptions(
    base: Iterable[str], nodes: Iterable[Mapping[str, Any]]
) -> list[str]:
    """部分接受提案时，按**最终图**校正假设列表。"""
    env_keys = _env_schema(nodes)
    items: list[str] = []
    for item in base or ():
        text_item = str(item).strip()
        if not text_item:
            continue
        match = re.fullmatch(r"需要 ([A-Za-z_][A-Za-z0-9_]*)", text_item)
        if match and match.group(1) not in env_keys:
            continue
        if text_item not in items:
            items.append(text_item)
    for key in env_keys:
        text = f"需要 {key}"
        if text not in items:
            items.append(text)
    return sorted(dict.fromkeys(items))


def _param_audit_for(
    nodes: Iterable[Mapping[str, Any]],
    *,
    url: str = "",
    url_source: str = "",
    autofilled: Iterable[Mapping[str, Any]] = (),
    assumptions: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "url": str(url or ""),
        "url_source": str(url_source or ""),
        "autofilled": [dict(item) for item in autofilled],
        "param_gaps": param_fill.audit_param_gaps(nodes, _registry()),
        "env_inputs": param_fill.audit_env_inputs(nodes, _registry()),
        "assumptions": list(assumptions),
    }


def _baseline_env(root: Path) -> dict[str, Any]:
    from . import service

    baseline = service.load_last_env(root)
    return baseline if isinstance(baseline, dict) else {}


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


def _assumption_env_keys(assumptions: Iterable[str]) -> set[str]:
    keys: set[str] = set()
    for item in assumptions or ():
        for match in re.finditer(r"\b([A-Z][A-Z0-9_]{1,})\b", str(item)):
            keys.add(match.group(1))
    return keys


def _semantic_issue(code: str, message: str, *, node_id: str = "", hint: str = "") -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "level": "error", "message": message, "node_id": node_id}
    if hint:
        issue["hint"] = hint
    return issue


def _semantic_checks(
    *,
    nodes: list[Mapping[str, Any]],
    intent: str,
    env_schema: Iterable[str],
    assumptions: Iterable[str],
    available_env: Iterable[str] | None,
    checks: Mapping[str, Any] | None,
    domain_pack: str = "",
    explore: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """意图覆盖 / 环境就绪 / 能力缺口三类语义检查（默认全开）。

    ``explore=True`` 时覆盖率只作 advisory：探索固化图的节点来自真实执行
    轨迹，是否可用由无 LLM 重放与 checks 决定，不再用 60% 阈值误伤。
    """
    enabled = {"intent": True, "env": True, "capability": True}
    if isinstance(checks, Mapping):
        for key in enabled:
            if key in checks and checks[key] is False:
                enabled[key] = False

    coverage = planner_gate.intent_coverage(nodes, intent, equivalents=explore)
    coverage["gate"] = "advisory" if explore else "blocking"
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if enabled["intent"] and intent and coverage["total"] and coverage["ratio"] < 0.6:
        message = (
            f"意图覆盖率偏低：{coverage['matched']}/{coverage['total']}"
            f"（{coverage['ratio']:.0%}）"
        )
        if explore:
            unmatched = "、".join(coverage["unmatched_nodes"]) or "无"
            warning = {
                "code": "intent_coverage_advisory",
                "level": "warning",
                "message": f"{message}，未匹配节点：{unmatched}；探索流程以重放结果为准",
                "node_id": "",
                "ratio": coverage["ratio"],
                "matched": coverage["matched"],
                "total": coverage["total"],
                "unmatched": list(coverage["unmatched_nodes"]),
                "unmatched_details": list(coverage["unmatched_details"]),
                "hint": "探索可能选用了等价原子或新增原子；如需强化可从重放结果与原子映射入手",
            }
            warnings.append(warning)
            coverage["warnings"] = [warning["message"]] + [
                detail["reason"] for detail in coverage["unmatched_details"]
            ]
        else:
            errors.append(
                _semantic_issue(
                    "intent_mismatch",
                    f"{message}，当前图与意图不匹配",
                    hint="删除无关节点，或重新规划；阈值 60%",
                )
            )

    missing_env: list[str] = []
    if enabled["env"] and available_env is not None:
        known = {str(key) for key in available_env} | _assumption_env_keys(assumptions)
        missing_env = [str(key) for key in env_schema if str(key) not in known]
        for key in missing_env:
            errors.append(
                _semantic_issue(
                    "missing_env",
                    f"计划引用了未提供的环境变量 env.{key}",
                    hint="在项目 env / 运行基线中配置，或在 assumptions 中显式声明",
                )
            )

    capability_gaps: list[str] = []
    if enabled["capability"] and intent:
        analysis = planner_gate.analyze_intent(intent, domain_pack=domain_pack)
        capability_gaps = [atom for atom in analysis.missing_atoms if atom not in capability_gaps]
        if capability_gaps:
            errors.append(
                _semantic_issue(
                    "capability_gap",
                    "意图需要当前原子库不具备的能力：" + ", ".join(capability_gaps),
                    hint="；".join(analysis.suggestions),
                )
            )

    return errors, warnings, {
        "coverage": coverage,
        "missing_env": missing_env,
        "capability_gaps": capability_gaps,
    }


def validate_nodes(
    root: Path,
    *,
    wid: str,
    rev: int,
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
    intent: str = "",
    env_schema: Iterable[str] = (),
    assumptions: Iterable[str] = (),
    available_env: Iterable[str] | None = None,
    checks: Mapping[str, Any] | None = None,
    domain_pack: str = "",
    origin: str = "",
    explore_id: str = "",
    registry: Any = None,
) -> dict[str, Any]:
    nodes = list(nodes)
    env_keys = list(env_schema) or _env_schema(nodes)
    if registry is None:
        from ..core.workspace_packs import workspace_registry

        registry = workspace_registry(root, _registry())
    plan = plan_from_nodes(nodes, source="studio", intent=intent)
    result = plan.check(registry, known_variables=set(env_keys))
    errors = [_issue_dict(issue) for issue in result.errors]
    warnings = [_issue_dict(issue) for issue in result.warnings]
    explore = str(origin) == "explore" or bool(explore_id)
    semantic, semantic_warnings, detail = _semantic_checks(
        nodes=nodes,
        intent=intent,
        env_schema=env_keys,
        assumptions=assumptions,
        available_env=available_env,
        checks=checks,
        domain_pack=domain_pack,
        explore=explore,
    )
    errors.extend(semantic)
    warnings.extend(semantic_warnings)
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
        **detail,
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
    # 白名单必须跟注册表同步：硬编码会让实际存在的 web_browser/android_adb/ios_sim
    # 在工作台里可选、却无法创建草案（P0-1）。
    allowed = {str(getattr(pack, "name", "") or "") for pack in _registry().packs()}
    if domain not in allowed:
        raise StudioError("bad_request", f"未知的领域包: {domain}")
    return domain


def _helper_node(node: dict[str, Any]) -> bool:
    atom = str(node.get("atom") or "")
    tags = {str(tag).lower() for tag in (node.get("tags") or ())}
    if atom in planner_gate._HELPER_ATOMS or "assert" in tags:
        node["helper"] = True
        return True
    return False


def _llm_client() -> Any:
    from . import service

    if not service.llm_enabled():
        return None
    from ..axe.llm import LLMClient

    return LLMClient()


def create_draft(root: Path, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    data = _as_dict(payload)
    intent = _clean_text(data.get("intent"), field="intent", required=True, limit=500)
    domain_pack = _clean_domain_pack(data.get("domain_pack"))
    exclude = _clean_exclude(data.get("exclude_atoms"))
    allow_llm = data.get("allow_llm") is True
    try:
        bundle = planner_gate.plan_draft(
            _registry(),
            intent=intent,
            domain_pack=domain_pack,
            exclude_atoms=exclude,
            allow_llm=allow_llm,
            llm_client=_llm_client(),
            env={},
        )
    except planner_gate.PlanGateError as exc:
        raise StudioError(
            exc.code, exc.message, status=exc.status, details=exc.details
        ) from exc
    plan = bundle.plan
    graph = graph_from_plan(plan, origin="ai")
    for node in graph["nodes"]:
        _helper_node(node)

    # 生成阶段就做一次「参数可用性」审计：显式 URL / 站点别名解析出的 URL
    # 按原子输入端口自动填入所有带 url 的必填端口，不针对具体原子写特判。
    # 站点别名只服务网页域：非网页域既不改写节点参数，也不对外暴露误导性的 url 字段。
    analysis = dict(bundle.analysis or {})
    ignored_pack = bool(analysis.get("domain_pack_ignored"))
    pack_message = str(analysis.get("domain_pack_message") or "")
    routed_domain = str(analysis.get("domain") or domain_pack or "")
    # 显式包被护栏忽略时，落库/回显必须使用实际路由到的领域，而不是用户误选的包。
    effective_pack = routed_domain if ignored_pack else (domain_pack or routed_domain)
    if routed_domain not in _WEB_DOMAINS:
        for key in ("url", "url_source", "site"):
            analysis[key] = ""
    resolved_url = str(analysis.get("url") or "").strip()
    url_source = str(analysis.get("url_source") or "")
    autofilled = (
        param_fill.fill_url_params(
            graph["nodes"],
            _registry(),
            resolved_url,
            source=url_source or "explicit",
        )
        if resolved_url
        else []
    )
    param_gaps = param_fill.audit_param_gaps(graph["nodes"], _registry())
    env_inputs = param_fill.audit_env_inputs(graph["nodes"], _registry())
    # 已被站点别名/显式 URL 填掉的环境占位符不再重复声明「需要 XXX」，
    # 只保留图中仍然真实存在的 env 依赖。
    remaining_env: set[str] = set()

    def _collect_env(value: Any) -> None:
        if isinstance(value, str):
            remaining_env.update(re.findall(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)", value))
        elif isinstance(value, Mapping):
            for item in value.values():
                _collect_env(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _collect_env(item)

    for node in graph["nodes"]:
        _collect_env(node.get("params") or {})
    assumptions = [
        item
        for item in bundle.assumptions
        if not (
            (match := re.fullmatch(r"需要 ([A-Za-z_][A-Za-z0-9_]*)", item))
            and match.group(1) not in remaining_env
        )
    ]
    param_audit = {
        "url": resolved_url,
        "url_source": url_source,
        "autofilled": autofilled,
        "param_gaps": param_gaps,
        "env_inputs": env_inputs,
        # 模板假设（如 /api/health 只是模板默认端点）与草案响应保持同一份
        "assumptions": assumptions,
    }

    wid = new_wid()
    env_schema = _env_schema(graph["nodes"])
    name = str(plan.cases[0].name or intent)[:80]
    now = time.time()
    workflow = {
        "wid": wid,
        "name": name,
        "intent": intent,
        "domain_pack": effective_pack or bundle.analysis.get("domain") or "",
        "planner": bundle.planner,
        "plan_source": bundle.plan_source,
        "confidence": bundle.confidence,
        "assumptions": assumptions,
        "capability_gaps": list(bundle.capability_gaps),
        "param_audit": param_audit,
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
        "name": name,
        "intent": intent,
        "domain_pack": workflow["domain_pack"],
        "planner": bundle.planner,
        "plan_source": bundle.plan_source,
        "confidence": bundle.confidence,
        "assumptions": assumptions,
        "capability_gaps": list(bundle.capability_gaps),
        "param_audit": param_audit,
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
            "note": f"意图：{intent}；planner={bundle.planner}；confidence={bundle.confidence}",
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
        assumptions=assumptions,
        available_env=None,
        domain_pack=workflow["domain_pack"],
    )
    return {
        "wid": wid,
        "rev": 0,
        "state": "draft",
        # 成功响应必须回显实际路由到的领域包（模板路径取 analysis.domain）
        "domain_pack": workflow["domain_pack"],
        "domain_pack_ignored": ignored_pack,
        "domain_pack_message": pack_message,
        "message": pack_message,
        "planner": bundle.planner,
        "plan_source": bundle.plan_source,
        "confidence": bundle.confidence,
        "assumptions": assumptions,
        "capability_gaps": list(bundle.capability_gaps),
        "analysis": analysis,
        "url": resolved_url,
        "url_source": url_source,
        "autofilled": autofilled,
        "param_gaps": param_gaps,
        "env_inputs": env_inputs,
        "param_audit": param_audit,
        "graph": graph,
        "validation": validation,
    }


def create_draft_from_graph(
    root: Path,
    *,
    intent: str,
    name: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    actor: str = "explore",
    explore_id: str = "",
    domain_pack: str = "",
    pending_atoms: list[dict[str, Any]] | None = None,
    replay_checks: list[dict[str, Any]] | None = None,
    abstraction: Mapping[str, Any] | None = None,
    registry: Any = None,
) -> dict[str, Any]:
    """Persist an explored (already executed) graph as a draft.

    与 :func:`create_draft` 共用同一套落库/版本/审计/校验逻辑，但**不调用规划器**：
    节点来自真实执行轨迹，参数是观测到的实际值。
    """
    clean_intent = _clean_text(intent, field="intent", required=True, limit=500)
    if not nodes:
        raise StudioError("bad_request", "探索轨迹没有可固化的原子调用")
    resolved_name = str(name or clean_intent)[:80]
    wid = new_wid()
    now = time.time()
    env_schema = _env_schema(nodes)
    assumptions = [f"来源探索：{explore_id}"] if explore_id else []
    workflow = {
        "wid": wid,
        "name": resolved_name,
        "intent": clean_intent,
        "domain_pack": domain_pack,
        "planner": "explore",
        "plan_source": "explore",
        "confidence": 1.0,
        "assumptions": assumptions,
        "capability_gaps": [],
        "param_audit": {"url": "", "url_source": "explore", "autofilled": [], "param_gaps": [], "env_inputs": [], "assumptions": assumptions},
        "state": "draft",
        "current_rev": 0,
        "locked": [],
        "published_ref": "",
        "env_schema": env_schema,
        "created_at": now,
        "updated_at": now,
        "created_by": actor,
        "origin": "explore",
        "explore_id": explore_id,
        "pending_atoms": list(pending_atoms or []),
        "replay_checks": list(replay_checks or []),
        "replay_status": "pending",
        "abstraction_plan": copy.deepcopy((abstraction or {}).get("plan") or {}),
        "atoms_created": copy.deepcopy((abstraction or {}).get("atoms_created") or []),
        "dropped_steps": copy.deepcopy((abstraction or {}).get("dropped_steps") or []),
        "llm_calls_replay": (abstraction or {}).get("replay_llm_calls"),
    }
    revision = {
        "wid": wid,
        "rev": 0,
        "origin": "explore",
        "created_by": actor,
        "created_at": now,
        "message": "探索成功轨迹固化草稿",
        "base_rev": None,
        "name": resolved_name,
        "intent": clean_intent,
        "domain_pack": domain_pack,
        "planner": "explore",
        "plan_source": "explore",
        "confidence": 1.0,
        "assumptions": assumptions,
        "capability_gaps": [],
        "param_audit": workflow["param_audit"],
        "env_schema": env_schema,
        "explore_id": explore_id,
        "replay_checks": copy.deepcopy(replay_checks or []),
        "abstraction_plan": copy.deepcopy((abstraction or {}).get("plan") or {}),
        "atoms_created": copy.deepcopy((abstraction or {}).get("atoms_created") or []),
        "dropped_steps": copy.deepcopy((abstraction or {}).get("dropped_steps") or []),
        "llm_calls_replay": (abstraction or {}).get("replay_llm_calls"),
        "nodes": copy.deepcopy(nodes),
        "edges": copy.deepcopy(edges),
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
            "origin": "explore",
            "note": f"探索 {explore_id} 固化；节点 {len(nodes)}；待封装脚本 {len(pending_atoms or [])}",
        },
    )
    validation = validate_nodes(
        root,
        wid=wid,
        rev=0,
        nodes=nodes,
        edges=edges,
        intent=clean_intent,
        env_schema=env_schema,
        assumptions=assumptions,
        available_env=None,
        domain_pack=domain_pack,
        origin="explore",
        explore_id=explore_id,
        registry=registry,
    )
    return {
        "wid": wid,
        "rev": 0,
        "state": "draft",
        "origin": "explore",
        "explore_id": explore_id,
        "nodes": nodes,
        "edges": edges,
        "pending_atoms": list(pending_atoms or []),
        "validation": validation,
        "replay_status": "pending",
    }


def record_replay(
    root: Path, wid: str, replay: Mapping[str, Any], *, actor: str = "explore"
) -> dict[str, Any]:
    """记录固化后的无 LLM 重放结果；失败时发布门禁拒绝。"""
    workflow = read_workflow(root, wid)
    status = "replay_passed" if str(replay.get("status")) == "passed" else "replay_failed"
    workflow["replay_status"] = status
    workflow["replay"] = {
        "status": replay.get("status"),
        "checks": replay.get("checks") or [],
        "errors": replay.get("errors") or [],
        "llm_calls": replay.get("llm_calls", 0),
        "clean_workspace": replay.get("clean_workspace", False),
        "duration_ms": replay.get("duration_ms"),
        "at": time.time(),
    }
    workflow["llm_calls_replay"] = int(replay.get("llm_calls") or 0)
    workflow["updated_at"] = time.time()
    _write_workflow(root, wid, workflow)
    _append_audit(
        _workflow_dir(root, wid),
        {
            "action": "explore_replay",
            "actor": actor,
            "wid": wid,
            "rev": int(workflow.get("current_rev") or 0),
            "origin": "explore",
            "note": f"无 LLM 重放：{status}",
        },
    )
    return {
        "wid": wid,
        "rev": int(workflow.get("current_rev") or 0),
        "state": workflow.get("state"),
        "replay_status": status,
        "replay": workflow["replay"],
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
    # 版本视图必须只读该 revision 的元数据：历史 rev 的 domain_pack/intent/名称
    # 不能被 workflow 的当前值覆盖（否则 ?rev=0 会显示 replan 后的 web 计划）。
    revision_intent = str(revision.get("intent") or "")
    revision_pack = str(revision.get("domain_pack") or "")
    return {
        "wid": wid,
        "name": _revision_name(revision, workflow),
        "intent": revision_intent or str(workflow.get("intent") or ""),
        "domain_pack": revision_pack or str(workflow.get("domain_pack") or ""),
        "planner": str(revision.get("planner") or ""),
        "plan_source": str(revision.get("plan_source") or ""),
        "confidence": revision.get("confidence", 0.0),
        "assumptions": list(revision.get("assumptions") or []),
        "capability_gaps": list(revision.get("capability_gaps") or []),
        "param_audit": dict(revision.get("param_audit") or {}),
        "state": workflow.get("state"),
        "current_rev": workflow.get("current_rev"),
        "rev": target_rev,
        "origin": revision.get("origin"),
        "explore_id": workflow.get("explore_id") or revision.get("explore_id") or "",
        "pending_atoms": list(workflow.get("pending_atoms") or []),
        "replay_status": workflow.get("replay_status") or "",
        "replay": dict(workflow.get("replay") or {}),
        "created_by": revision.get("created_by"),
        "created_at": revision.get("created_at"),
        "message": revision.get("message"),
        "env_schema": revision.get("env_schema") or [],
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
    from . import service

    validation = validate_nodes(
        root,
        wid=wid,
        rev=int(revision.get("rev") or 0),
        nodes=nodes,
        edges=edges,
        intent=str(workflow.get("intent") or ""),
        env_schema=revision.get("env_schema") or [],
        assumptions=revision.get("assumptions") or [],
        available_env=set(service.load_last_env(root)),
        domain_pack=str(workflow.get("domain_pack") or ""),
        origin=str(workflow.get("origin") or ""),
        explore_id=str(workflow.get("explore_id") or ""),
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

    stored_pack = str(workflow.get("domain_pack") or "")
    workflow_intent = str(workflow.get("intent") or "")
    guard = planner_gate.analyze_intent(
        f"{workflow_intent} {instruction}".strip(), domain_pack=stored_pack
    )
    ignored_pack = bool(guard.domain_pack_ignored)
    routed_pack = str(guard.domain or "")
    effective_pack = "" if ignored_pack else stored_pack
    #: 提案要持久化「护栏纠正后的真实领域」：apply 时不能再用库里的错配包。
    proposal_pack = routed_pack if ignored_pack else (stored_pack or routed_pack)
    # 历史错配的工作流：若新指令本身表达不了域，回退用库内原始意图重排，
    # 避免「重新规划」这类指令退化成默认模板组。
    plan_intent = instruction
    if ignored_pack and not planner_gate.analyze_intent(instruction).hit:
        plan_intent = workflow_intent or instruction

    exclude = _clean_exclude(data.get("exclude_atoms"))
    from . import service

    if not service.llm_enabled() and "llm_summarize" not in exclude:
        exclude.append("llm_summarize")
    plan = _build_plan(
        root,
        intent=plan_intent,
        domain_pack=effective_pack,
        exclude_atoms=exclude,
    )
    proposed = graph_from_plan(
        plan,
        origin="ai",
        locked=[str(node.get("id")) for node in revision.get("nodes") or [] if node.get("locked")],
    )
    autofilled: list[dict[str, Any]] = []
    if guard.url and str(guard.domain or "") in _WEB_DOMAINS:
        autofilled = param_fill.fill_url_params(
            proposed["nodes"],
            _registry(),
            guard.url,
            source=guard.url_source or "site_alias",
        )
    assumptions = _plan_assumptions(plan, proposed["nodes"])
    plan_name = str(plan.cases[0].name or plan_intent)[:80]
    planner_name = str(getattr(plan, "planner", "") or "template")
    plan_source = str(getattr(plan, "source", "") or "template")
    confidence = _plan_confidence(plan, proposal_pack)
    param_audit = _param_audit_for(
        proposed["nodes"],
        url=guard.url if str(guard.domain or "") in _WEB_DOMAINS else "",
        url_source=guard.url_source if str(guard.domain or "") in _WEB_DOMAINS else "",
        autofilled=autofilled,
        assumptions=assumptions,
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
            "intent": plan_intent,
            "domain_pack": proposal_pack,
            "domain_pack_ignored": ignored_pack,
            "domain_pack_message": guard.domain_pack_message,
        }

    proposed_rev = base_rev + 1
    proposal = {
        "wid": wid,
        "base_rev": base_rev,
        "proposed_rev": proposed_rev,
        "instruction": instruction,
        # intent/domain_pack 是护栏纠正后的**真实**规划语义：
        # apply-diff 必须落库这两个值，而不是把 replan 指令当成新意图。
        "intent": plan_intent,
        "domain_pack": proposal_pack,
        "domain_pack_ignored": ignored_pack,
        "domain_pack_message": guard.domain_pack_message,
        "name": plan_name,
        "planner": planner_name,
        "plan_source": plan_source,
        "confidence": confidence,
        "assumptions": assumptions,
        "capability_gaps": [],
        "env_schema": _env_schema(proposed["nodes"]),
        "param_audit": param_audit,
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
        "intent": plan_intent,
        "domain_pack": proposal_pack,
        "name": plan_name,
        "planner": planner_name,
        "confidence": confidence,
        "assumptions": assumptions,
        "domain_pack_ignored": ignored_pack,
        "domain_pack_message": guard.domain_pack_message,
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

    # 提案里持久化的是护栏纠正后的规划语义：apply 必须以它为准，
    # 不能用 replan 指令（如「重新规划」）覆盖工作流的原始意图/领域。
    proposal_pack = str(proposal.get("domain_pack") or "").strip()
    if proposal_pack:
        workflow["domain_pack"] = proposal_pack
    applied_intent = (
        str(proposal.get("intent") or "").strip()
        or str(proposal.get("instruction") or "").strip()
    )
    # apply 后用**提案/新图**重算元数据：旧 revision 只有图，没有名称与假设，
    # 若继续用 workflow 旧值，页面会一直停留在被替换掉的 API 草案上。
    proposal_audit = proposal.get("param_audit") or {}
    applied_assumptions = _merge_assumptions(
        proposal.get("assumptions") or [], nodes
    )
    applied_autofilled = [
        item
        for item in (proposal.get("autofilled") or proposal_audit.get("autofilled") or [])
        if str(item.get("node_id")) in node_ids
    ]
    applied_audit = _param_audit_for(
        nodes,
        url=str(proposal_audit.get("url") or ""),
        url_source=str(proposal_audit.get("url_source") or ""),
        autofilled=applied_autofilled,
        assumptions=applied_assumptions,
    )
    applied_name = (
        str(proposal.get("name") or "").strip()
        or _revision_name(revision, workflow)
    )

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
        audit_note=(
            f"apply-diff proposal={proposed_rev} accepted={len(applied)}"
            f" intent={applied_intent} domain_pack={proposal_pack or workflow.get('domain_pack') or ''}"
        ),
        intent=applied_intent or None,
        name=applied_name,
        planner=str(proposal.get("planner") or ""),
        plan_source=str(proposal.get("plan_source") or ""),
        confidence=proposal.get("confidence"),
        assumptions=applied_assumptions,
        capability_gaps=list(proposal.get("capability_gaps") or []),
        param_audit=applied_audit,
        env_schema=_env_schema(nodes),
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
            assumptions=target.get("assumptions") or [],
            available_env=set(_baseline_env(root)),
            domain_pack=str(workflow.get("domain_pack") or ""),
            origin=str(workflow.get("origin") or ""),
            explore_id=str(workflow.get("explore_id") or ""),
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
    origin = str(workflow.get("origin") or "")
    explore = origin == "explore" or bool(workflow.get("explore_id"))
    if explore:
        replay_status = str(workflow.get("replay_status") or "")
        replay = workflow.get("replay") or {}
        checks = replay.get("checks") if isinstance(replay, Mapping) else None
        failed_checks = [
            item
            for item in (checks or [])
            if isinstance(item, Mapping) and item.get("ok") is False
        ]
        if replay_status == "replay_failed" or failed_checks:
            raise StudioError(
                "replay_failed",
                "固化流程无 LLM 重放未通过，不能发布",
                status=409,
                details={"replay": replay, "failed_checks": failed_checks},
            )
        if replay_status != "replay_passed":
            raise StudioError(
                "replay_pending",
                "固化流程尚无通过的无 LLM 重放，不能发布",
                status=409,
                details={"replay": replay},
            )
    if workflow.get("state") != "approved":
        raise StudioError("not_approved", "只有 approved 状态的工作流才能发布")
    if rev != int(workflow.get("current_rev") or 0):
        raise StudioError("conflict", "只能发布当前版本（请先重新批准最新 rev）")
    revision = read_revision(root, wid, rev)
    validation = validate_nodes(
        root,
        wid=wid,
        rev=rev,
        nodes=revision.get("nodes") or [],
        edges=revision.get("edges") or [],
        intent=str(workflow.get("intent") or ""),
        env_schema=revision.get("env_schema") or [],
        assumptions=revision.get("assumptions") or [],
        available_env=set(_baseline_env(root)),
        domain_pack=str(workflow.get("domain_pack") or ""),
        origin=origin,
        explore_id=str(workflow.get("explore_id") or ""),
    )
    # 探索固化的发布门禁只看无 LLM 重放（已在上方校验），
    # 覆盖率与能力缺口仅作 advisory，避免误伤真实跑通的轨迹。
    if explore:
        blocking: list[dict[str, Any]] = []
    else:
        blocking = [
            item
            for item in validation.get("errors") or []
            if item.get("code") in {"intent_mismatch", "capability_gap"}
        ]
    if blocking:
        first = blocking[0]
        raise StudioError(
            str(first.get("code") or "intent_mismatch"),
            "语义校验未通过，不能发布：" + str(first.get("message") or ""),
            status=409,
            details={"validation": validation, "blocking": blocking},
        )

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


def _upstream_refs(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], node_id: str
) -> list[str]:
    """返回 ``node_id`` 引用的上游变量名（来自数据边或参数里的模板根）。"""
    refs: list[str] = []
    owners: dict[str, str] = {}
    for node in nodes:
        nid = str(node.get("id") or "")
        owners[nid] = nid
        for name in node.get("capture") or {}:
            owners[str(name)] = nid
    for edge in edges or []:
        if str(edge.get("to")) != node_id or str(edge.get("kind")) != "data":
            continue
        label = str(edge.get("via") or edge.get("from") or "")
        if label and label not in refs:
            refs.append(label)
    node = next((item for item in nodes if str(item.get("id")) == node_id), None)
    if node is not None:
        for root in template_roots(node.get("params") or {}):
            owner = owners.get(root)
            if owner and owner != node_id and root not in refs:
                refs.append(root)
    return refs


_DEBUG_SID_RE = re.compile(r"^dbg-(w-[0-9a-f]{6,12})-([0-9a-f]{8})$")
DEBUG_REASONS = (
    "when_false",
    "foreach_empty",
    "upstream_failed",
    "optional",
    "assertion",
    "network",
    "missing_env",
    "missing_llm",
    "missing_input",
    "cancelled",
)
_DEBUG_RUNS: dict[str, _DebugSession] = {}
_DEBUG_LOCK = threading.RLock()


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "null", "none"}
    return bool(value)


def _compact(value: Any, *, limit: int = 2000, depth: int = 0) -> Any:
    """压缩调试输入/输出，避免把整页 HTML 写进会话。"""
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…（截断，共 {len(value)} 字符）"
    if isinstance(value, Mapping):
        return {str(key): _compact(item, limit=limit, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_compact(item, limit=limit, depth=depth + 1) for item in list(value)[:50]]
        if len(value) > 50:
            items.append(f"…（共 {len(value)} 项）")
        return items
    return value


def _exception_fields(exc: BaseException, atom: Any = None) -> tuple[str, str, str]:
    if isinstance(exc, StepFailure):
        return "assertion", "expectation", str(exc)
    if isinstance(exc, AtomFailure):
        kind = str(getattr(exc, "kind", "") or "assertion")
        return kind, str(getattr(exc, "failure_class", "") or "internal"), str(exc)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network", "transient", str(exc)
    if isinstance(exc, PanguError):
        kind = str(getattr(exc, "kind", "") or "assertion")
        return kind, str(getattr(exc, "failure_class", "") or "internal"), str(exc)
    return "assertion", "internal", f"{type(exc).__name__}: {exc}"


def _retryable_for(kind: str, failure_class: str) -> bool:
    """调试报告与运行报告使用同一套重试口径。"""
    return failure_is_retryable(kind, failure_class)


def _reason_for(exc: BaseException, *, atom: Any = None, raw: Any = None) -> str:
    kind, _failure_class, message = _exception_fields(exc, atom)
    if kind in DEBUG_REASONS:
        return kind
    if kind in {"timeout", "llm_error", "server_error", "rate_limit", "not_found", "auth"}:
        return "network" if kind in {"timeout", "llm_error", "server_error", "rate_limit"} else "assertion"
    if kind in {"missing_llm", "empty_summary"}:
        return "missing_llm" if kind == "missing_llm" else "assertion"
    if kind == "missing_env":
        return "missing_env"
    if "env." in str(raw) and "变量不存在" in message:
        return "missing_env"
    return "assertion"


def _append_jsonl(path: Path, payload: Mapping[str, Any], *, seq: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(DIR_MODE)
    data = dict(payload)
    if seq is not None:
        data["seq"] = seq
    line = json.dumps(data, ensure_ascii=False, default=str) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, FILE_MODE)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        path.chmod(FILE_MODE)


def _debug_root(root: Path, wid: str) -> Path:
    return _workflow_dir(root, wid) / "debug"


def _debug_sandbox(root: Path, wid: str, sid: str) -> Path:
    """每次调试会话独立的工作目录：产物、相对路径与断言共享同一根。"""
    return _debug_root(root, wid) / sid / "workspace"


_DEBUG_CWD_LOCK = threading.RLock()


def _debug_node_step(node: Mapping[str, Any]) -> Step:
    spec = {
        "id": node.get("id"),
        "use": node.get("atom") or node.get("use"),
        "name": node.get("name") or "",
        "with": copy.deepcopy(node.get("params") or {}),
        "capture": copy.deepcopy(node.get("capture") or {}),
        "when": node.get("when"),
        "foreach": node.get("foreach"),
        "as": node.get("as"),
        "retry": node.get("retry"),
        "optional": node.get("optional") or False,
        "continue_on_failure": node.get("continue_on_failure") or False,
        "tags": node.get("tags") or [],
    }
    return Step.from_spec(spec)


def _select_nodes_payload(
    nodes: list[dict[str, Any]], node_id: str, mode: str
) -> list[dict[str, Any]]:
    return _select_nodes(nodes, node_id, mode)


class _DebugSession:
    """一次异步调试会话：逐节点写 jsonl，GET 按 cursor 增量读取。"""

    def __init__(
        self,
        *,
        root: Path,
        wid: str,
        rev: int,
        sid: str,
        node_id: str,
        mode: str,
        nodes: list[dict[str, Any]],
        env: dict[str, Any],
        actor: str,
    ) -> None:
        self.root = root
        self.wid = wid
        self.rev = rev
        self.sid = sid
        self.node_id = node_id
        self.mode = mode
        self.nodes = nodes
        self.env = env
        self.actor = actor
        self.sandbox = _debug_sandbox(root, wid, sid)
        self.path = _debug_root(root, wid) / f"{sid}.jsonl"
        self.snapshot_path = _debug_root(root, wid) / f"{sid}.json"
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.done = False
        self.summary = {"passed": 0, "failed": 0, "skipped": 0, "by_reason": {}}
        self.variables: dict[str, Any] = {}
        self.secret_values: list[str] = []
        self.node_status: dict[str, str] = {}
        self.capture_owner: dict[str, str] = {}
        for item in nodes:
            owner = str(item.get("id") or "")
            for name in (item.get("capture") or {}):
                self.capture_owner[str(name)] = owner
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        for key, value in env.items():
            if _SENSITIVE_KEY_RE.search(str(key)) and isinstance(value, str) and len(value) >= 4:
                self.secret_values.append(value)

    # ------------------------------------------------------------------ #
    def _mask(self, payload: Any) -> Any:
        masked = mask_obj(payload, list(self.secret_values))
        from .api import mask_env_payload

        return mask_env_payload(masked)

    def emit(self, event: dict[str, Any]) -> int:
        with self._lock:
            self.seq += 1
            item = self._mask({**event, "seq": self.seq, "at": time.time()})
            self.events.append(item)
            _append_jsonl(self.path, item)
            return self.seq

    def _ensure_sandbox(self) -> Path:
        self.sandbox.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.sandbox.chmod(DIR_MODE)
        return self.sandbox

    def start(self) -> None:
        sandbox = self._ensure_sandbox()
        self.emit(
            {
                "type": "start",
                "session_id": self.sid,
                "wid": self.wid,
                "rev": self.rev,
                "node_id": self.node_id,
                "mode": self.mode,
                "total": len(self.nodes),
                "sandbox": str(sandbox),
                "status": "running",
            }
        )
        for node in self.nodes:
            self.emit(
                {
                    "type": "node",
                    "node_id": str(node.get("id") or ""),
                    "atom": node.get("atom"),
                    "name": node.get("name"),
                    "status": "pending",
                    "reason": "",
                    "attempts": 0,
                    "duration_ms": 0,
                    "error": None,
                }
            )
        self._thread = threading.Thread(target=self._run, name=f"studio-debug-{self.sid}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    def _input_gap_reason(self, step: Step) -> str:
        """必填输入缺失时区分「上游被跳过/失败」与真正的断言/计划问题。"""
        special = {
            "env",
            "secrets",
            "meta",
            "vars",
            "case_id",
            "run_id",
            "item",
            "index",
            "loop",
            "result",
            "status",
            "last_response",
        }
        for root in template_roots(step.with_):
            if root in special:
                continue
            owner = self.capture_owner.get(root, root)
            if self.node_status.get(owner) in {"skipped", "failed"}:
                return "missing_input"
        return ""

    def _run_node(self, node: Mapping[str, Any], ctx: RunContext, registry: Any) -> tuple[str, str, int, dict[str, Any], dict[str, Any]]:
        atom_name = str(node.get("atom") or "")
        atom = registry.get(atom_name)
        step = _debug_node_step(node)

        if step.when not in (None, ""):
            try:
                condition = ctx.resolve(step.when)
            except PanguError as exc:
                return "failed", _reason_for(exc, atom=atom, raw=step.when), 1, {}, {
                    "type": type(exc).__name__,
                    "kind": getattr(exc, "kind", "template"),
                    "message": str(exc),
                }
            if not _truthy(condition):
                return "skipped", "when_false", 0, {"condition": step.when}, {}

        iterations: list[Any] = [None]
        if step.foreach:
            try:
                items = ctx.resolve(step.foreach)
            except PanguError as exc:
                return "failed", _reason_for(exc, atom=atom, raw=step.foreach), 1, {}, {
                    "type": type(exc).__name__,
                    "kind": getattr(exc, "kind", "template"),
                    "message": str(exc),
                }
            if isinstance(items, dict):
                items = list(items.values())
            if not isinstance(items, (list, tuple)):
                return "failed", "assertion", 1, {"foreach": step.foreach}, {
                    "type": "PlanError",
                    "kind": "plan",
                    "message": f"foreach 需要列表，实际 {type(items).__name__}",
                }
            if not items:
                return "skipped", "foreach_empty", 0, {"foreach": step.foreach}, {}
            iterations = list(items)

        collected: list[dict[str, Any]] = []
        attempts_total = 0
        try:
            inputs = ctx.resolve(step.with_)
        except PanguError as exc:
            reason = self._input_gap_reason(step) or _reason_for(exc, atom=atom, raw=step.with_)
            return "failed", reason, 1, {}, {
                "type": type(exc).__name__,
                "kind": getattr(exc, "kind", "template"),
                "message": str(exc),
            }
        missing = [name for name in atom.required if inputs.get(name) is None]
        if missing:
            reason = self._input_gap_reason(step) or "assertion"
            return "failed", reason, 1, _compact(inputs), {
                "type": "PlanError",
                "kind": "plan",
                "failure_class": "invalid_input",
                "message": f"缺少必填输入: {', '.join(missing)}",
                "retryable": False,
            }

        for index, item in enumerate(iterations):
            loop_ctx = ctx
            if step.foreach:
                loop_ctx = ctx.child(
                    **{
                        step.as_: item,
                        "index": index,
                        "loop": {
                            "index": index,
                            "number": index + 1,
                            "length": len(iterations),
                            "first": index == 0,
                            "last": index == len(iterations) - 1,
                        },
                    }
                )
            policy = step.retry
            attempts = max(1, int(policy.attempts)) if policy else 1
            attempts_total += attempts
            while attempts > 0:
                try:
                    outputs = atom.invoke(loop_ctx, loop_ctx.resolve(step.with_))
                    break
                except Exception as exc:
                    kind, failure_class, _message = _exception_fields(exc, atom)
                    if (
                        policy is not None
                        and policy.should_retry(kind, failure_class)
                        and getattr(loop_ctx, "_debug_attempt", False) is False
                        and attempts > 1
                    ):
                        attempts -= 1
                        time.sleep(policy.delay_for(attempts))
                        continue
                    raise
            collected.append(dict(outputs or {}))
            for secret_name, secret_value in atom.secrets_in_outputs(outputs):
                if isinstance(secret_value, str) and len(secret_value) >= 4:
                    self.secret_values.append(secret_value)
                loop_ctx.add_secret(secret_name, secret_value)

        merged = collected[-1] if collected else {}
        if step.foreach:
            merged = {
                **merged,
                "count": len(collected),
                "items": collected,
                "last": collected[-1] if collected else {},
            }
        ctx.vars[step.id] = merged
        for name, raw_spec in (step.capture or {}).items():
            if isinstance(raw_spec, Mapping):
                path = str(raw_spec.get("path") or raw_spec.get("from") or name)
                required = bool(raw_spec.get("required", True))
                secret = bool(raw_spec.get("secret", False))
            else:
                path = str(raw_spec)
                required = True
                secret = False
            value = walk_path(merged, split_path(path))
            if value is MISSING:
                if required:
                    return "failed", "assertion", attempts_total, _compact(inputs), {
                        "type": "StepError",
                        "kind": "capture_missing",
                        "failure_class": "expectation",
                        "message": f"capture 路径不存在: {path}",
                        "retryable": False,
                    }
                continue
            ctx.set(str(name), value)
            if secret:
                ctx.add_secret(str(name), value)
        return "passed", "", attempts_total, _compact(inputs), _compact(merged)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        from ..core.workspace_packs import workspace_registry
        from . import service

        registry = workspace_registry(self.root, service.registry())
        sandbox = self._ensure_sandbox()
        env = dict(self.env)
        # 调试的硬边界就是本次会话的 sandbox：相对路径产物与 assert_*
        # 都以它为根，绝不落到服务进程 cwd。
        env["FS_ROOT"] = str(sandbox)
        env["PANGUVERSE_FS_ROOT"] = str(sandbox)
        ctx = RunContext(env=env, run_id=self.sid, workspace=sandbox)
        passed = failed = skipped = 0
        by_reason: dict[str, int] = {}
        aborted = False
        # os.chdir 是进程级状态；串行化调试执行，避免并发会话互相切目录。
        _DEBUG_CWD_LOCK.acquire()
        previous_cwd = os.getcwd()
        try:
            os.chdir(sandbox)
        except BaseException:
            _DEBUG_CWD_LOCK.release()
            raise
        try:
            for node in self.nodes:
                node_id = str(node.get("id") or "")
                if aborted:
                    skipped += 1
                    self.node_status[node_id] = "skipped"
                    by_reason["upstream_failed"] = by_reason.get("upstream_failed", 0) + 1
                    self.emit(
                        {
                            "type": "node",
                            "node_id": node_id,
                            "atom": node.get("atom"),
                            "name": node.get("name"),
                            "status": "skipped",
                            "reason": "upstream_failed",
                            "attempts": 0,
                            "duration_ms": 0,
                            "error": None,
                        }
                    )
                    continue
                self.emit(
                    {
                        "type": "node",
                        "node_id": node_id,
                        "atom": node.get("atom"),
                        "name": node.get("name"),
                        "status": "running",
                        "reason": "",
                        "attempts": 0,
                        "duration_ms": 0,
                        "error": None,
                    }
                )
                started = time.perf_counter()
                try:
                    status, reason, attempts, inputs, outputs = self._run_node(node, ctx, registry)
                    error = None
                    if status == "failed":
                        error = outputs if isinstance(outputs, dict) and outputs.get("message") else None
                except Exception as exc:  # noqa: BLE001 - 任何失败都要落到节点状态
                    status, reason = "failed", _reason_for(exc, raw=node.get("params"))
                    attempts, inputs, outputs = 1, _compact(node.get("params") or {}), {}
                    kind, failure_class, _message = _exception_fields(exc)
                    error = {
                        "type": type(exc).__name__,
                        "kind": kind,
                        "failure_class": failure_class,
                        "message": str(exc),
                        "retryable": _retryable_for(kind, failure_class),
                    }
                duration_ms = (time.perf_counter() - started) * 1000
                if status == "failed" and (node.get("optional") or False):
                    status, reason = "skipped", "optional"
                self.node_status[node_id] = status
                if status == "passed":
                    passed += 1
                elif status == "skipped":
                    skipped += 1
                    by_reason[reason] = by_reason.get(reason, 0) + 1
                else:
                    failed += 1
                    by_reason[reason] = by_reason.get(reason, 0) + 1
                    if not (node.get("continue_on_failure") or False):
                        aborted = True
                self.emit(
                    {
                        "type": "node",
                        "node_id": node_id,
                        "atom": node.get("atom"),
                        "name": node.get("name"),
                        "status": status,
                        "reason": reason,
                        "attempts": attempts,
                        "duration_ms": round(duration_ms, 2),
                        "error": error,
                        "inputs": inputs,
                        "outputs": outputs if status == "passed" else {},
                    }
                )
            snapshot = ctx.snapshot()
            self.variables = {
                "vars": _compact(snapshot.get("vars") or {}, limit=1000),
                "env": _compact(snapshot.get("env") or {}, limit=500),
            }
        except Exception as exc:  # noqa: BLE001 - 会话线程不能静默死掉
            failed += 1
            by_reason["assertion"] = by_reason.get("assertion", 0) + 1
            self.emit(
                {
                    "type": "node",
                    "node_id": "",
                    "status": "failed",
                    "reason": "assertion",
                    "attempts": 0,
                    "duration_ms": 0,
                    "error": {
                        "type": type(exc).__name__,
                        "kind": "engine",
                        "failure_class": "internal",
                        "message": str(exc),
                        "retryable": False,
                    },
                }
            )
        finally:
            try:
                os.chdir(previous_cwd)
            finally:
                _DEBUG_CWD_LOCK.release()
            summary = {
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "by_reason": dict(sorted(by_reason.items())),
            }
            self.summary = summary
            self.done = True
            end = {
                "type": "end",
                "session_id": self.sid,
                "done": True,
                "status": "ok" if failed == 0 else "failed",
                "summary": summary,
                "variables": self.variables,
            }
            seq = self.emit(end)
            snapshot = {
                "debug_id": self.sid,
                "session_id": self.sid,
                "wid": self.wid,
                "rev": self.rev,
                "workflow_ref": f"{self.wid}@{self.rev}",
                "node_id": self.node_id,
                "mode": self.mode,
                "status": "ok" if failed == 0 else "failed",
                "summary": summary,
                "sandbox": str(self.sandbox),
                "trace": [
                    {
                        "node_id": item.get("node_id"),
                        "atom": item.get("atom"),
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "reason": item.get("reason"),
                        "attempts": item.get("attempts"),
                        "duration_ms": item.get("duration_ms"),
                        "inputs": item.get("inputs"),
                        "outputs": item.get("outputs"),
                        "error": item.get("error"),
                    }
                    for item in self.events
                    if item.get("type") == "node" and item.get("status") != "running"
                ],
                "variables": self.variables,
                "cursor": seq,
                "done": True,
            }
            _atomic_write_json(self.snapshot_path, self._mask(snapshot))
            _append_audit(
                _workflow_dir(self.root, self.wid),
                {
                    "action": "debug",
                    "actor": self.actor,
                    "wid": self.wid,
                    "from": None,
                    "to": self.sid,
                    "rev": self.rev,
                    "note": f"{self.node_id} ({self.mode})",
                },
            )


def debug_workflow(root: Path, wid: str, payload: Any, *, actor: str = "local") -> dict[str, Any]:
    """启动异步调试会话：立即返回 session_id，节点状态由 jsonl 增量读取。"""
    data = _as_dict(payload)
    mode = data.get("mode") or "only"
    if mode not in DEBUG_MODES:
        raise StudioError("bad_request", "mode 只能是 only/from_start/downstream")
    node_id = _clean_text(data.get("node_id"), field="node_id", required=True, limit=200)
    workflow = read_workflow(root, wid)
    if (
        workflow.get("origin") == "explore"
        and workflow.get("replay_status") == "replay_failed"
        and not bool(data.get("force"))
    ):
        raise StudioError(
            "replay_failed",
            "固化流程的无 LLM 重放未通过，调试被阻断；如确需人工排查请显式传 force=true",
            status=409,
            details={"replay": workflow.get("replay") or {}},
        )
    rev = (
        int(workflow.get("current_rev") or 0)
        if data.get("rev") is None
        else _as_rev(data.get("rev"))
    )
    revision = read_revision(root, wid, rev)
    nodes = list(revision.get("nodes") or [])
    selected = _select_nodes_payload(nodes, node_id, mode)
    if mode == "only":
        # 单节点调试只跑选中节点，上游 capture 不会产生值；提前说清楚原因，
        # 不要让执行线程抛 TemplateError 让用户猜。
        deps = _upstream_refs(nodes, list(revision.get("edges") or []), node_id)
        if deps:
            names = "、".join(deps)
            raise StudioError(
                "bad_request",
                f"该节点依赖上游输出（{names}），请用 from_start 调试",
                details={
                    "depends_on": deps,
                    "suggested_mode": "from_start",
                },
            )
    effective_env = _debug_env(root, data)
    if not selected:
        raise StudioError("bad_request", "调试没有选中任何可执行步骤")

    sid = f"dbg-{wid}-{uuid.uuid4().hex[:8]}"
    session = _DebugSession(
        root=root,
        wid=wid,
        rev=rev,
        sid=sid,
        node_id=node_id,
        mode=mode,
        nodes=selected,
        env=effective_env,
        actor=actor,
    )
    with _DEBUG_LOCK:
        _DEBUG_RUNS[sid] = session
    session.start()
    return {
        "session_id": sid,
        "debug_id": sid,
        "wid": wid,
        "rev": rev,
        "workflow_ref": f"{wid}@{rev}",
        "node_id": node_id,
        "mode": mode,
        "status": "running",
        "total": len(selected),
        "cursor": session.seq,
        "done": False,
        "summary": {"passed": 0, "failed": 0, "skipped": 0, "by_reason": {}},
    }


def _session_events(root: Path, sid: str) -> list[dict[str, Any]]:
    match = _DEBUG_SID_RE.match(sid or "")
    if not match:
        raise StudioError("bad_request", "session_id 非法")
    wid = match.group(1)
    with _DEBUG_LOCK:
        session = _DEBUG_RUNS.get(sid)
    if session is not None:
        with session._lock:
            return list(session.events)
    path = _debug_root(root, wid) / f"{sid}.jsonl"
    if not path.is_file():
        raise StudioError("not_found", "调试会话不存在或已过期")
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def get_debug_session(root: Path, sid: str, *, cursor: int = 0) -> dict[str, Any]:
    """按 cursor 增量返回节点状态与原因码（不进 /api/runs）。"""
    events = _session_events(root, sid)
    cursor = max(0, int(cursor or 0))
    pending = [item for item in events if int(item.get("seq") or 0) > cursor]
    nodes = [item for item in pending if item.get("type") == "node"]
    end = next((item for item in reversed(events) if item.get("type") == "end"), None)
    summary = (end or {}).get("summary") or {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "by_reason": {},
    }
    start = next((item for item in events if item.get("type") == "start"), {})
    return {
        "session_id": sid,
        "sandbox": (start or {}).get("sandbox") or "",
        "cursor": max([cursor, *[int(item.get("seq") or 0) for item in pending]]),
        "done": end is not None,
        "status": (end or {}).get("status")
        or ("running" if events else "missing"),
        "nodes": nodes,
        "events": pending,
        "summary": summary,
        "variables": (end or {}).get("variables") or {},
    }


__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "STATE_DIRNAME",
    "STUDIO_DIRNAME",
    "StudioError",
    "apply_diff",
    "audit_tail",
    "create_draft",
    "create_draft_from_graph",
    "debug_workflow",
    "get_debug_session",
    "get_workflow",
    "list_drafts",
    "load_published",
    "plan_from_revision",
    "publish",
    "read_revision",
    "read_workflow",
    "record_replay",
    "replan",
    "review",
    "transition_state",
    "validate_nodes",
]
