"""工作台图模型：线性 ``TestPlan`` ↔ 有向图。

图里的节点 = 一个可执行步骤（原子 + 参数 + 捕获），边只有两种语义：

- ``kind="data"``：真有数据依赖。由 ``capture`` 与 ``${var}`` 推导，``via`` 记录变量名；
- ``kind="order"``：只表示执行顺序，**不假装有数据依赖**，固定带 ``order_only: true``。

推导规则（刻意保守）：

1. 步骤 id 与 ``capture`` 的键是变量生产者；``${env.X}`` / 内置变量（``item`` 等）不产生边；
2. 某个步骤引用了**前面**步骤产出的变量 → 一条数据边；
3. 相邻步骤之间没有任何直接数据边 → 一条 order-only 边，保证图仍可执行；
4. 引用了找不到生产者的变量 → **不建边**，留给静态校验报 P101（绝不伪造依赖）。

本模块只做纯函数，不碰磁盘、不依赖服务层，便于单测与复用。
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..axe.plan import TestPlan
from ..core.molecule import Molecule, Step
from ..core.validation import BUILTIN_VARIABLES, SPECIAL_ROOTS

TEMPLATE_RE = re.compile(r"\$\{([^{}]*)\}")
_PATH_HEAD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")

#: 这些根不是「前序步骤产出」，不能拿来连数据边
NON_DATA_ROOTS = set(SPECIAL_ROOTS) | set(BUILTIN_VARIABLES)

#: 参与 diff 的节点字段（不含位置、锁定、来源等人工/展示字段）
DIFF_NODE_KEYS = (
    "atom",
    "name",
    "params",
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
    "case_id",
)


# --------------------------------------------------------------------------- #
# 模板扫描
# --------------------------------------------------------------------------- #
def template_roots(value: Any) -> list[str]:
    """递归收集模板表达式里的根变量（``${a.b[0]}`` → ``a``），保持出现顺序。"""
    roots: list[str] = []
    if isinstance(value, str):
        for match in TEMPLATE_RE.finditer(value):
            expr = match.group(1).split("|", 1)[0].strip()
            head = _PATH_HEAD_RE.match(expr)
            if not head:
                continue
            root = head.group(1)
            if root not in roots:
                roots.append(root)
    elif isinstance(value, Mapping):
        for item in value.values():
            for root in template_roots(item):
                if root not in roots:
                    roots.append(root)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for root in template_roots(item):
                if root not in roots:
                    roots.append(root)
    return roots


def _step_references(step: Step) -> list[str]:
    roots: list[str] = []
    for source in (step.with_, step.when, step.foreach, step.extra):
        if not source:
            continue
        for root in template_roots(source):
            if root and root not in roots:
                roots.append(root)
    return roots


# --------------------------------------------------------------------------- #
# 节点 / 边
# --------------------------------------------------------------------------- #
def node_from_step(
    step: Step,
    *,
    case_id: str,
    case_name: str = "",
    origin: str = "ai",
    locked: bool = False,
    node_id: str | None = None,
) -> dict[str, Any]:
    """把一个 ``Step`` 变成图节点（保留重建计划所需的全部字段）。"""
    return {
        "id": node_id or step.id,
        "atom": step.use,
        "name": step.name or step.id,
        "description": step.description,
        "params": copy.deepcopy(step.with_),
        "capture": copy.deepcopy(step.capture),
        "when": step.when,
        "foreach": step.foreach,
        "as": step.as_,
        "retry": step.retry.to_dict() if step.retry else None,
        "continue_on_failure": bool(step.continue_on_failure),
        "tags": list(step.tags),
        "optional": bool(step.optional),
        "extra": copy.deepcopy(step.extra),
        "case_id": case_id,
        "case_name": case_name,
        "origin": "human" if origin == "human" else "ai",
        "locked": bool(locked),
    }


def _edge_id(from_id: str, to_id: str, via: str | None, kind: str) -> str:
    suffix = f"data-{via}" if kind == "data" and via else "order"
    return f"e-{from_id}->{to_id}:{suffix}"


def _make_edge(from_id: str, to_id: str, *, via: str | None, kind: str) -> dict[str, Any]:
    order_only = kind != "data"
    return {
        "id": _edge_id(from_id, to_id, via, kind),
        "from": from_id,
        "to": to_id,
        "via": via,
        "kind": "data" if not order_only else "order",
        "order_only": order_only,
    }


def _unique_node_id(raw: str, used: set[str], case_id: str) -> str:
    if raw not in used:
        used.add(raw)
        return raw
    base = f"{case_id}.{raw}" if case_id else raw
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}#{index}"
        index += 1
    used.add(candidate)
    return candidate


def graph_from_plan(
    plan: TestPlan,
    *,
    origin: str = "ai",
    locked: Iterable[str] = (),
) -> dict[str, Any]:
    """线性（可多用例）计划 → 图。"""
    locked_ids = set(locked or ())
    used_ids: set[str] = set()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()

    for case in plan.cases:
        case_id = case.case_id or case.name or "case"
        # 同一用例内：变量名 → 生产者节点；步骤 id 本身也可被 ${step.x} 引用
        producers: dict[str, str] = {}
        case_nodes: list[dict[str, Any]] = []

        for step in case.steps:
            node_id = _unique_node_id(str(step.id), used_ids, case_id)
            node = node_from_step(
                step,
                case_id=case_id,
                case_name=case.name,
                origin=origin,
                locked=node_id in locked_ids,
                node_id=node_id,
            )
            nodes.append(node)
            case_nodes.append(node)

            # 先连边（生产者只含**之前**的步骤）
            for root in _step_references(step):
                if root in NON_DATA_ROOTS:
                    continue
                producer = producers.get(root)
                if not producer or producer == node_id:
                    continue  # 找不到生产者：不建边，交给校验报 P101
                edge = _make_edge(producer, node_id, via=root, kind="data")
                if edge["id"] not in seen_edges:
                    seen_edges.add(edge["id"])
                    edges.append(edge)

            producers[node_id] = node_id
            for name in (step.capture or {}):
                producers[str(name)] = node_id

        # 相邻节点若没有直接数据边，补一条 order-only 边，保证执行顺序可读
        for left, right in itertools.pairwise(case_nodes):
            if any(
                edge["from"] == left["id"]
                and edge["to"] == right["id"]
                and edge["kind"] == "data"
                for edge in edges
            ):
                continue
            edge = _make_edge(left["id"], right["id"], via=None, kind="order")
            if edge["id"] not in seen_edges:
                seen_edges.add(edge["id"])
                edges.append(edge)

    return {
        "nodes": nodes,
        "edges": edges,
        "order": [node["id"] for node in nodes],
        "cases": _case_summaries(nodes),
    }


def _case_summaries(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for node in nodes:
        case_id = str(node.get("case_id") or "case")
        if case_id not in seen:
            seen[case_id] = {
                "case_id": case_id,
                "name": node.get("case_name") or case_id,
                "node_ids": [],
            }
        seen[case_id]["node_ids"].append(node["id"])
    return list(seen.values())


# --------------------------------------------------------------------------- #
# 图 → 计划
# --------------------------------------------------------------------------- #
def _step_spec(node: Mapping[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": node.get("id"),
        "use": node.get("atom") or node.get("use"),
        "name": node.get("name") or "",
        "description": node.get("description") or "",
        "with": copy.deepcopy(node.get("params") or {}),
        "capture": copy.deepcopy(node.get("capture") or {}),
    }
    for key in ("when", "foreach", "as", "retry", "continue_on_failure", "tags", "optional"):
        value = node.get(key)
        if value not in (None, "", [], (), False):
            spec[key] = value
    extra = node.get("extra")
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            spec.setdefault(str(key), copy.deepcopy(value))
    return spec


def plan_from_nodes(
    nodes: Iterable[Mapping[str, Any]],
    *,
    source: str = "studio",
    intent: str = "",
    planner: str = "studio",
    env: dict[str, Any] | None = None,
) -> TestPlan:
    """按 ``case_id`` 分组重建计划（保持节点顺序）。"""
    groups: dict[str, dict[str, Any]] = {}
    for node in nodes:
        case_id = str(node.get("case_id") or "case")
        group = groups.setdefault(
            case_id,
            {"name": node.get("case_name") or case_id, "steps": []},
        )
        group["steps"].append(Step.from_spec(_step_spec(node)))

    cases = [
        Molecule(
            case_id=case_id,
            name=str(group["name"] or case_id),
            steps=list(group["steps"]),
            source=source,
        )
        for case_id, group in groups.items()
    ]
    return TestPlan(
        cases=cases,
        source=source,
        planner=planner,
        intent=intent,
        env=dict(env or {}),
        meta={"node_count": sum(len(case.steps) for case in cases)},
    )


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def node_signature(node: Mapping[str, Any]) -> str:
    """节点语义签名：只包含会影响执行的字段，用于判断「是否被修改」。"""
    data = {key: node.get(key) for key in DIFF_NODE_KEYS}
    data["params"] = data.get("params") or {}
    data["capture"] = data.get("capture") or {}
    data["extra"] = data.get("extra") or {}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def edge_signature(edge: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(edge.get("from") or ""),
        str(edge.get("to") or ""),
        str(edge.get("via") or ""),
        str(edge.get("kind") or "order"),
    )


def diff_graphs(
    base_nodes: Iterable[Mapping[str, Any]],
    base_edges: Iterable[Mapping[str, Any]],
    target_nodes: Iterable[Mapping[str, Any]],
    target_edges: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """返回逐条变更（change_id 稳定，供 apply-diff 的 accepted 引用）。"""
    base_nodes = list(base_nodes)
    target_nodes = list(target_nodes)
    base_by_id = {str(node.get("id")): node for node in base_nodes}
    target_by_id = {str(node.get("id")): node for node in target_nodes}

    node_changes: list[dict[str, Any]] = []
    for index, node in enumerate(target_nodes):
        node_id = str(node.get("id"))
        if node_id not in base_by_id:
            node_changes.append(
                {
                    "change_id": f"node:{node_id}:added",
                    "id": node_id,
                    "action": "added",
                    "index": index,
                    "after": copy.deepcopy(dict(node)),
                }
            )
        elif node_signature(base_by_id[node_id]) != node_signature(node):
            node_changes.append(
                {
                    "change_id": f"node:{node_id}:changed",
                    "id": node_id,
                    "action": "changed",
                    "index": index,
                    "before": copy.deepcopy(dict(base_by_id[node_id])),
                    "after": copy.deepcopy(dict(node)),
                }
            )
    for index, node in enumerate(base_nodes):
        node_id = str(node.get("id"))
        if node_id not in target_by_id:
            node_changes.append(
                {
                    "change_id": f"node:{node_id}:removed",
                    "id": node_id,
                    "action": "removed",
                    "index": index,
                    "before": copy.deepcopy(dict(node)),
                }
            )

    base_sigs = {edge_signature(edge) for edge in base_edges}
    target_sigs = {edge_signature(edge) for edge in target_edges}
    edge_changes: list[dict[str, Any]] = []
    for edge in target_edges:
        if edge_signature(edge) in base_sigs:
            continue
        edge_changes.append(
            {
                "change_id": f"edge:{edge.get('id')}:added",
                "id": str(edge.get("id")),
                "action": "added",
                "after": copy.deepcopy(dict(edge)),
            }
        )
    for edge in base_edges:
        if edge_signature(edge) in target_sigs:
            continue
        edge_changes.append(
            {
                "change_id": f"edge:{edge.get('id')}:removed",
                "id": str(edge.get("id")),
                "action": "removed",
                "before": copy.deepcopy(dict(edge)),
            }
        )

    return {"nodes": node_changes, "edges": edge_changes}


def apply_graph_diff(
    base_nodes: Iterable[Mapping[str, Any]],
    base_edges: Iterable[Mapping[str, Any]],
    diff: Mapping[str, Any],
    accepted: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """按 change_id 应用部分 diff；返回 ``(nodes, edges, applied)``。"""
    accepted_set = {str(item) for item in accepted}
    nodes = [copy.deepcopy(dict(node)) for node in base_nodes]
    edges = [copy.deepcopy(dict(edge)) for edge in base_edges]
    applied: list[dict[str, Any]] = []

    # 先剔除已接受的删除节点：diff 里的 index 是相对**目标图**的位置，
    # 若先插入再删除，index 会把待删的旧节点也算进去（首节点被顶到中间）。
    accepted_removed = {
        str(change.get("id"))
        for change in list((diff or {}).get("nodes") or [])
        if str(change.get("change_id")) in accepted_set
        and change.get("action") == "removed"
    }
    if accepted_removed:
        nodes = [node for node in nodes if str(node.get("id")) not in accepted_removed]

    for change in list((diff or {}).get("nodes") or []):
        change_id = str(change.get("change_id"))
        if change_id not in accepted_set:
            continue
        action = change.get("action")
        node_id = str(change.get("id"))
        if action == "removed":
            nodes = [node for node in nodes if str(node.get("id")) != node_id]
        elif action == "changed":
            for index, node in enumerate(nodes):
                if str(node.get("id")) == node_id:
                    nodes[index] = copy.deepcopy(dict(change["after"]))
                    break
        elif action == "added":
            node = copy.deepcopy(dict(change["after"]))
            # index=0 是合法的插入位置：只有 "缺失/None" 才表示追加到末尾。
            raw_index = change.get("index")
            if raw_index is None:
                index = len(nodes)
            else:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    index = len(nodes)
            nodes.insert(max(0, min(index, len(nodes))), node)
        applied.append(change)

    for change in list((diff or {}).get("edges") or []):
        change_id = str(change.get("change_id"))
        if change_id not in accepted_set:
            continue
        action = change.get("action")
        edge_id = str(change.get("id"))
        if action == "removed":
            edges = [edge for edge in edges if str(edge.get("id")) != edge_id]
        elif action == "added":
            edges.append(copy.deepcopy(dict(change["after"])))
        applied.append(change)

    return nodes, edges, applied


def edge_change_touches_node(change: Mapping[str, Any], node_id: str) -> bool:
    """一条边变更是否牵连某个节点（锁定节点不允许 AI 改动相关边）。"""
    for key in ("before", "after"):
        edge = change.get(key)
        if isinstance(edge, Mapping) and node_id in {
            str(edge.get("from")),
            str(edge.get("to")),
        }:
            return True
    return False


__all__ = [
    "DIFF_NODE_KEYS",
    "NON_DATA_ROOTS",
    "apply_graph_diff",
    "diff_graphs",
    "edge_change_touches_node",
    "graph_from_plan",
    "node_from_step",
    "node_signature",
    "plan_from_nodes",
    "template_roots",
]
