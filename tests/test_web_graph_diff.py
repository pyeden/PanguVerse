"""第五轮 F1：apply_graph_diff 的插入位置必须是显式 index（index=0 合法）。"""

from __future__ import annotations

from typing import Any

from panguverse.web.graph import apply_graph_diff


def _added(change_id: str, node_id: str, index: Any) -> dict[str, Any]:
    change: dict[str, Any] = {
        "change_id": change_id,
        "id": node_id,
        "action": "added",
        "after": {"id": node_id, "atom": "noop"},
    }
    if index is not None:
        change["index"] = index
    return change


def test_index_zero_inserts_at_front() -> None:
    diff = {
        "nodes": [
            _added("node:first:added", "first", 0),
            _added("node:second:added", "second", 1),
        ],
        "edges": [],
    }
    nodes, edges, applied = apply_graph_diff(
        [], [], diff, ["node:first:added", "node:second:added"]
    )
    assert [node["id"] for node in nodes] == ["first", "second"]
    assert len(applied) == 2
    assert edges == []


def test_index_zero_inserts_before_existing_node() -> None:
    diff = {"nodes": [_added("node:first:added", "first", 0)], "edges": []}
    nodes, _edges, _applied = apply_graph_diff(
        [{"id": "old", "atom": "noop"}], [], diff, ["node:first:added"]
    )
    assert [node["id"] for node in nodes] == ["first", "old"]


def test_missing_index_appends_to_end() -> None:
    diff = {"nodes": [_added("node:tail:added", "tail", None)], "edges": []}
    nodes, _edges, _applied = apply_graph_diff(
        [{"id": "old", "atom": "noop"}], [], diff, ["node:tail:added"]
    )
    assert [node["id"] for node in nodes] == ["old", "tail"]


def test_added_index_ignores_nodes_being_removed() -> None:
    diff = {
        "nodes": [
            _added("node:new:added", "new", 1),
            {
                "change_id": "node:gone:removed",
                "id": "gone",
                "action": "removed",
                "index": 0,
                "before": {"id": "gone", "atom": "noop"},
            },
        ],
        "edges": [],
    }
    nodes, _edges, _applied = apply_graph_diff(
        [{"id": "gone", "atom": "noop"}, {"id": "kept", "atom": "noop"}],
        [],
        diff,
        ["node:new:added", "node:gone:removed"],
    )
    assert [node["id"] for node in nodes] == ["kept", "new"]
