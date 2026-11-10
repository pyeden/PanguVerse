"""工作台 MVP 后端契约测试（``tasks/workbench-mvp-spec.md`` v5.0 §2 / §4）。

覆盖：graphifier 数据边/顺序边、锁定冲突 409、发布门禁与不可变版本、
调试隔离（不入 /api/runs）与 URL 覆盖 400、审计条目、密钥打码、
发布流程绑定项目运行后的 ``wid@rev`` 回溯。
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from panguverse.axe.plan import TestPlan
from panguverse.core.molecule import Molecule, Step
from panguverse.web import service, studio
from panguverse.web.api import WebAPI
from panguverse.web.graph import graph_from_plan


def call(api: WebAPI, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
    route, _, query = path.partition("?")
    response = api.handle(method, route, query, raw)
    body = json.loads(response.body.decode("utf-8")) if response.body else None
    return response.status, body


@pytest.fixture()
def api(tmp_path: Path) -> WebAPI:
    root = tmp_path / "ws"
    root.mkdir()
    return WebAPI(root)


def wait_batch(api: WebAPI, batch_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = call(api, "GET", f"/api/batches/{batch_id}")
        assert status == 200, body
        if body.get("status") in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"batch {batch_id} 未在 {timeout}s 内结束")


def wait_job(api: WebAPI, job_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = call(api, "GET", f"/api/jobs/{job_id}")
        assert status == 200, body
        if body.get("status") in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------- #
# Graphifier
# --------------------------------------------------------------------------- #
def test_graphifier_data_edges_and_order_only_edges() -> None:
    steps = [
        Step(
            id="login",
            use="http_request",
            with_={"method": "POST", "url": "${env.BASE_URL}/login"},
            capture={"token": {"path": "body.token", "secret": True}},
        ),
        Step(
            id="create_order",
            use="http_request",
            with_={
                "method": "POST",
                "url": "${env.BASE_URL}/orders",
                "headers": {"Authorization": "Bearer ${token}"},
            },
            capture={"order_id": "body.id"},
        ),
        Step(
            id="get_order",
            use="http_request",
            with_={"method": "GET", "url": "${env.BASE_URL}/orders/${order_id}"},
        ),
        Step(id="note", use="set_var", with_={"name": "done", "value": True}),
    ]
    plan = TestPlan(cases=[Molecule(name="flow", case_id="flow", steps=steps)], source="rules")
    graph = graph_from_plan(plan)

    edges = {(edge["from"], edge["to"], edge["kind"], edge["via"]): edge for edge in graph["edges"]}
    assert edges[("login", "create_order", "data", "token")]["order_only"] is False
    assert edges[("create_order", "get_order", "data", "order_id")]["order_only"] is False
    # 相邻但无数据依赖 → 顺序边，且明确标记 order_only
    order = edges[("get_order", "note", "order", None)]
    assert order["order_only"] is True
    # 有数据边的相邻节点不再重复画 order 边
    assert ("login", "create_order", "order", None) not in edges


def test_graphifier_does_not_invent_edges_for_unknown_vars() -> None:
    steps = [
        Step(id="first", use="http_request", with_={"url": "${env.BASE_URL}/a"}),
        Step(id="second", use="http_request", with_={"url": "${env.BASE_URL}/b/${missing}"}),
    ]
    plan = TestPlan(cases=[Molecule(name="flow", case_id="flow", steps=steps)], source="rules")
    graph = graph_from_plan(plan)
    assert not [
        edge
        for edge in graph["edges"]
        if edge["kind"] == "data" and edge.get("via") == "missing"
    ]
    # 顺序边仍存在（执行顺序），但没有伪造的数据依赖
    assert any(
        edge["from"] == "first" and edge["to"] == "second" and edge["order_only"] is True
        for edge in graph["edges"]
    )


# --------------------------------------------------------------------------- #
# 草案 / 图 / 校验
# --------------------------------------------------------------------------- #
def test_add_data_edge_via_node_id_persists_new_revision(api: WebAPI) -> None:
    """P1：via 用节点 id 建数据边不得被局部变量 root 遮蔽，必须真正落盘。"""
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201, draft
    wid = draft["wid"]
    base_rev = draft["rev"]
    nodes = draft["graph"]["nodes"]
    from_id = str(nodes[0]["id"])
    to_id = str(nodes[-1]["id"])
    assert from_id != to_id

    status, body = call(
        api,
        "POST",
        f"/api/studio/{wid}/edges",
        {"from": from_id, "to": to_id, "via": from_id},
    )
    assert status == 201, body
    assert body["rev"] == base_rev + 1
    data_edges = [
        edge
        for edge in body["graph"]["edges"]
        if edge["from"] == from_id and edge["to"] == to_id
    ]
    assert len(data_edges) == 1, data_edges
    assert data_edges[0]["kind"] == "data"
    assert data_edges[0]["via"] == from_id
    assert data_edges[0]["order_only"] is False

    # 真正写进了新版本文件，而不是把路径列表当工作区根目录。
    workflow = studio.read_workflow(api.root, wid)
    assert workflow["current_rev"] == base_rev + 1
    revision = studio.read_revision(api.root, wid, base_rev + 1)
    assert any(
        edge.get("from") == from_id
        and edge.get("to") == to_id
        and edge.get("kind") == "data"
        for edge in revision["edges"]
    )

    # 顺序边仍正常：反向补一条 order 边（不与已有 data 边冲突）。
    status, ordered = call(
        api,
        "POST",
        f"/api/studio/{wid}/edges",
        {"from": to_id, "to": from_id, "kind": "order"},
    )
    assert status == 201, ordered
    assert ordered["rev"] == base_rev + 2
    order_edges = [
        edge
        for edge in ordered["graph"]["edges"]
        if edge["from"] == to_id and edge["to"] == from_id
    ]
    assert len(order_edges) == 1, order_edges
    assert order_edges[0]["kind"] == "order"
    assert order_edges[0]["via"] is None
    assert order_edges[0]["order_only"] is True


def test_create_draft_graph_and_permissions(api: WebAPI) -> None:
    status, body = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201, body
    assert body["rev"] == 0
    assert body["domain_pack"] == "local_fs"
    graph = body["graph"]
    assert len(graph["nodes"]) >= 3
    for node in graph["nodes"]:
        assert {"id", "atom", "name", "params", "origin", "locked"} <= set(node)
        assert node["origin"] == "ai"
    assert any(edge["order_only"] is True for edge in graph["edges"])

    wid = body["wid"]
    studio_dir = api.root / ".panguverse" / "studio"
    revision = studio_dir / wid / "revisions" / "0.json"
    assert revision.is_file()
    assert mode_of(revision) == 0o600
    assert mode_of(revision.parent) == 0o700
    assert mode_of(studio_dir) == 0o700
    assert mode_of(studio_dir / wid) == 0o700

    status, listing = call(api, "GET", "/api/studio/drafts")
    assert status == 200
    assert [item["wid"] for item in listing["drafts"]] == [wid]
    assert listing["drafts"][0]["node_count"] == len(graph["nodes"])


def test_domain_pack_switches_intent_templates(api: WebAPI) -> None:
    """W9：同一意图在不同领域包下选出不同模板（无需改代码）。"""
    status, fs_draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201, fs_draft
    fs_atoms = {node["atom"] for node in fs_draft["graph"]["nodes"]}
    assert any(atom.startswith("fs_") or atom.startswith("assert_file") for atom in fs_atoms)

    status, api_draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {
            "intent": "备份配置文件并校验备份存在",
            "domain_pack": "api_testing",
            "exclude_atoms": ["http_request"],
        },
    )
    assert status == 201, api_draft
    api_atoms = {node["atom"] for node in api_draft["graph"]["nodes"]}
    assert fs_atoms - api_atoms  # 文件领域特有原子
    assert api_atoms - fs_atoms  # API 领域特有原子（core 通用断言两边都可能出现）
    assert "http_request" not in api_atoms

    status, listing = call(api, "GET", "/api/studio/drafts?domain_pack=local_fs")
    assert [item["wid"] for item in listing["drafts"]] == [fs_draft["wid"]]


def test_validate_after_delete_reports_downstream(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "登录下单支付", "domain_pack": "api_testing"},
    )
    assert status == 201
    wid = draft["wid"]
    status, deleted = call(api, "DELETE", f"/api/studio/{wid}/nodes/login")
    assert status == 200, deleted
    status, validation = call(api, "POST", f"/api/studio/{wid}/validate", {})
    assert status == 200
    assert validation["ok"] is False
    assert any("token" in item["message"] for item in validation["errors"])
    assert validation["affected_nodes"]
    assert all(item["node_id"] for item in validation["errors"])


# --------------------------------------------------------------------------- #
# 锁定 / replan / apply-diff
# --------------------------------------------------------------------------- #
def test_locked_node_replan_returns_409(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201
    wid = draft["wid"]
    locked_id = draft["graph"]["nodes"][0]["id"]
    status, patched = call(api, "PATCH", f"/api/studio/{wid}/nodes/{locked_id}", {"locked": True})
    assert status == 200, patched
    assert patched["rev"] == 1
    assert any(node["id"] == locked_id and node["locked"] for node in patched["graph"]["nodes"])

    status, body = call(api, "POST", f"/api/studio/{wid}/replan", {"instruction": "数据落盘回读 JSON"})
    assert status == 409, body
    assert body["error"]["code"] == "conflict"
    assert locked_id in body["error"]["locked_nodes"]

    # 版本文件未被 replan 改写
    revision = json.loads(
        (api.root / ".panguverse" / "studio" / wid / "revisions" / "1.json").read_text(
            encoding="utf-8"
        )
    )
    assert any(node["id"] == locked_id and node["locked"] for node in revision["nodes"])


def test_replan_diff_and_apply_creates_new_immutable_rev(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    wid = draft["wid"]
    rev0_path = api.root / ".panguverse" / "studio" / wid / "revisions" / "0.json"
    before = rev0_path.read_bytes()

    status, proposal = call(
        api, "POST", f"/api/studio/{wid}/replan", {"instruction": "数据落盘回读 JSON"}
    )
    assert status == 200, proposal
    assert proposal["proposed_rev"] == 1
    assert proposal["diff"]["nodes"]
    accepted = [change["change_id"] for change in proposal["diff"]["nodes"]]
    status, applied = call(
        api,
        "POST",
        f"/api/studio/{wid}/apply-diff",
        {"proposed_rev": 1, "accepted": accepted},
    )
    assert status == 200, applied
    assert applied["rev"] == proposal["proposed_rev"]
    assert rev0_path.read_bytes() == before  # 旧 rev 不回溯修改


# --------------------------------------------------------------------------- #
# 发布门禁 / 不可变
# --------------------------------------------------------------------------- #
def test_publish_gate_and_immutability(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "登录下单支付", "domain_pack": "api_testing"},
    )
    assert status == 201
    wid = draft["wid"]

    status, body = call(api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "v1"})
    assert status == 409
    assert body["error"]["code"] == "not_approved"

    status, body = call(api, "POST", f"/api/studio/{wid}/state", {"to": "approved"})
    assert status == 409
    assert body["error"]["code"] == "risk_unconfirmed"
    assert body["error"]["risks"]

    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body

    status, body = call(api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "  "})
    assert status == 400
    # 门禁失败不应产生任何任务
    status, listing = call(api, "GET", "/api/tasks")
    assert status == 200
    assert listing["tasks"] == []

    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "首个发布"}
    )
    assert status == 200, published
    assert published["workflow_ref"] == f"{wid}@0"

    rev0_path = api.root / ".panguverse" / "studio" / wid / "revisions" / "0.json"
    before = rev0_path.read_bytes()
    status, patched = call(
        api, "PATCH", f"/api/studio/{wid}/nodes/{draft['graph']['nodes'][0]['id']}", {"name": "改个名"}
    )
    assert status == 200, patched
    assert patched["rev"] == 1
    assert rev0_path.read_bytes() == before
    # 人工改图后状态回到 draft，不能直接发布 rev1
    status, body = call(api, "POST", f"/api/studio/{wid}/publish", {"rev": 1, "message": "v2"})
    assert status == 409


# --------------------------------------------------------------------------- #
# 发布即建任务（方案 A）
# --------------------------------------------------------------------------- #
def _approved_draft(api: WebAPI) -> dict[str, Any]:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201, draft
    status, body = call(
        api, "POST", f"/api/studio/{draft['wid']}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    return draft


def test_publish_creates_and_reuses_workflow_task(api: WebAPI) -> None:
    draft = _approved_draft(api)
    wid = draft["wid"]
    workflow_name = studio.read_workflow(api.root, wid)["name"]

    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "首个发布"}
    )
    assert status == 200, published
    task = published["task"]
    assert task["mode"] == "workflow"
    assert task["workflow_ref"] == f"{wid}@0"
    assert task["name"] == f"{workflow_name} 任务"
    assert task["env"] == {}
    assert task["regression"] is False
    assert task["no_llm"] is False

    # 发布后任务列表立即可见
    status, listing = call(api, "GET", "/api/tasks")
    assert status == 200
    assert [item["task_id"] for item in listing["tasks"]] == [task["task_id"]]
    assert listing["tasks"][0]["workflow_ref"] == f"{wid}@0"

    # 同一 rev 重复发布：reused=true，task_id 与任务数不变
    status, again = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "重复发布"}
    )
    assert status == 200, again
    assert again["reused"] is True
    assert again["task"]["task_id"] == task["task_id"]
    assert again["task"]["workflow_ref"] == f"{wid}@0"
    status, listing2 = call(api, "GET", "/api/tasks")
    assert len(listing2["tasks"]) == 1

    # 发布新 rev：仍是同一个 task_id，只更新 workflow_ref
    node_id = draft["graph"]["nodes"][0]["id"]
    status, patched = call(
        api, "PATCH", f"/api/studio/{wid}/nodes/{node_id}", {"description": "补充说明"}
    )
    assert status == 200, patched
    assert patched["rev"] == 1
    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    status, published2 = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 1, "message": "第二版"}
    )
    assert status == 200, published2
    assert published2["reused"] is False
    assert published2["task"]["task_id"] == task["task_id"]
    assert published2["task"]["name"] == task["name"]
    assert published2["task"]["workflow_ref"] == f"{wid}@1"
    status, listing3 = call(api, "GET", "/api/tasks")
    assert len(listing3["tasks"]) == 1
    assert listing3["tasks"][0]["task_id"] == task["task_id"]


def test_publish_reuses_manual_workflow_task(api: WebAPI) -> None:
    draft = _approved_draft(api)
    wid = draft["wid"]
    # 直接落发布记录（模拟发布前已存在的历史版本），不触发建任务
    studio.publish(api.root, wid, {"rev": 0, "message": "seed"})

    status, manual = call(
        api,
        "POST",
        "/api/tasks",
        {
            "name": "我的手动流程任务",
            "mode": "workflow",
            "workflow_ref": f"{wid}@0",
            "env": {"KEEP_ME": "manual-value"},
            "regression": True,
            "no_llm": True,
        },
    )
    assert status == 201, manual

    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "发布复用"}
    )
    assert status == 200, published
    assert published["reused"] is True
    task = published["task"]
    assert task["task_id"] == manual["task_id"]
    assert task["name"] == "我的手动流程任务"
    assert task["env"]["KEEP_ME"] == "manual-value"
    assert task["regression"] is True
    assert task["no_llm"] is True
    assert task["workflow_ref"] == f"{wid}@0"

    status, listing = call(api, "GET", "/api/tasks")
    assert [item["task_id"] for item in listing["tasks"]] == [manual["task_id"]]


def test_task_run_records_task_trigger(api: WebAPI, tmp_path: Path) -> None:
    """任务入口运行 workflow 应记 trigger=task；直接 /api/runs 仍记 manual。"""
    service.save_last_env(api.root, {"FS_ROOT": str(tmp_path / "fs")})
    draft = _approved_draft(api)
    wid = draft["wid"]
    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "备份流程"}
    )
    assert status == 200, published
    ref = published["workflow_ref"]
    task_id = published["task"]["task_id"]

    status, queued = call(api, "POST", f"/api/tasks/{task_id}/run", {})
    assert status == 202, queued
    job = wait_job(api, queued["job_id"])
    assert job["status"] == "done", job
    run_id = job["run_id"]

    execution = api.root / ".panguverse" / "executions" / run_id / "execution.json"
    record = json.loads(execution.read_text(encoding="utf-8"))
    assert record["trigger"] == "task"
    assert record["workflow_ref"] == ref

    status, listing = call(api, "GET", "/api/executions")
    assert status == 200, listing
    row = next(item for item in listing["executions"] if item["run_id"] == run_id)
    assert row["trigger"] == "task"
    assert row["workflow_ref"] == ref

    # 直接 /api/runs（非任务入口）保持 manual
    status, direct = call(
        api, "POST", "/api/runs", {"mode": "workflow", "workflow_ref": ref}
    )
    assert status == 202, direct
    direct_job = wait_job(api, direct["job_id"])
    assert direct_job["status"] == "done", direct_job
    direct_execution = (
        api.root / ".panguverse" / "executions" / direct_job["run_id"] / "execution.json"
    )
    direct_record = json.loads(direct_execution.read_text(encoding="utf-8"))
    assert direct_record["trigger"] == "manual"
    assert direct_record["workflow_ref"] == ref


# --------------------------------------------------------------------------- #
# 调试隔离 / URL 限制 / 打码
# --------------------------------------------------------------------------- #
def test_debug_is_isolated_and_url_override_rejected(api: WebAPI, tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    service.save_last_env(
        api.root, {"FS_ROOT": str(fs_root), "API_PASSWORD": "super-secret-value"}
    )
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    wid = draft["wid"]
    node_id = draft["graph"]["nodes"][0]["id"]

    status, started = call(
        api,
        "POST",
        f"/api/studio/{wid}/debug",
        {"rev": 0, "node_id": node_id, "mode": "only"},
    )
    assert status == 202, started
    assert started["session_id"].startswith("dbg-")

    cursor = 0
    debug = None
    seen: list[dict[str, Any]] = []
    deadline = time.time() + 30
    while time.time() < deadline:
        status, debug = call(
            api, "GET", f"/api/studio/debug/{started['session_id']}?cursor={cursor}"
        )
        assert status == 200, debug
        seen.extend(debug.get("nodes") or [])
        cursor = max(cursor, int(debug.get("cursor") or 0))
        if debug.get("done"):
            break
        time.sleep(0.02)
    assert debug and debug.get("done") is True
    step_trace = [item for item in seen if item["node_id"] == node_id and item["status"] == "passed"]
    assert step_trace
    final = step_trace[-1]
    assert {"inputs", "outputs", "duration_ms", "status", "error"} <= set(final)
    assert final["duration_ms"] >= 0
    assert debug["variables"]["env"]["API_PASSWORD"] == "***"
    assert "super-secret-value" not in json.dumps(debug, ensure_ascii=False)
    sandbox = (
        api.root
        / ".panguverse"
        / "studio"
        / wid
        / "debug"
        / started["session_id"]
        / "workspace"
    )
    assert debug["sandbox"] == str(sandbox)
    assert debug["variables"]["env"]["FS_ROOT"] == str(sandbox)
    assert debug["variables"]["env"]["PANGUVERSE_FS_ROOT"] == str(sandbox)
    assert service.list_runs(api.root) == []  # 调试不进正式历史

    debug_files = list((api.root / ".panguverse" / "studio" / wid / "debug").glob("*.jsonl"))
    assert debug_files
    assert mode_of(debug_files[0]) == 0o600
    disk_text = debug_files[0].read_text(encoding="utf-8")
    assert "super-secret-value" not in disk_text
    assert "***" in disk_text
    snapshots = list((api.root / ".panguverse" / "studio" / wid / "debug").glob("*.json"))
    assert snapshots and mode_of(snapshots[0]) == 0o600

    status, body = call(
        api,
        "POST",
        f"/api/studio/{wid}/debug",
        {"rev": 0, "node_id": node_id, "mode": "only", "env": {"BASE_URL": "http://evil"}},
    )
    assert status == 400, body
    status, body = call(
        api,
        "POST",
        f"/api/studio/{wid}/debug",
        {"rev": 0, "node_id": node_id, "mode": "only", "base_url": "http://evil"},
    )
    assert status == 400, body


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
def test_debug_runs_in_per_session_sandbox_and_restores_cwd(
    api: WebAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对路径写出的产物与 assert_* 必须落在同一 sandbox，而不是进程 cwd。"""
    from panguverse.core import workspace_packs
    from panguverse.core.atom import Atom

    def write_relative(ctx: Any = None, output_dir: str = ".", title: str = "热点") -> dict[str, str]:
        base = Path(output_dir)
        base.mkdir(parents=True, exist_ok=True)
        (base / "report.html").write_text(f"<h1>{title}</h1>", encoding="utf-8")
        return {"report_html": "report.html"}

    writer = Atom(
        name="test_write_relative",
        fn=write_relative,
        inputs={"output_dir": "string", "title": "string"},
        required=("output_dir",),
        outputs={"report_html": "string"},
    )
    base_registry = service.registry()

    class _Registry:
        def get(self, name: str) -> Any:
            return writer if name == writer.name else base_registry.get(name)

    monkeypatch.setattr(
        workspace_packs, "workspace_registry", lambda root, base: _Registry()
    )
    wid = "w-abcdef12"
    sid = "dbg-w-abcdef12-12345678"
    nodes = [
        {
            "id": "n1",
            "atom": writer.name,
            "name": "写报告",
            "params": {"output_dir": ".", "title": "热点"},
        },
        {
            "id": "n2",
            "atom": "assert_file_exists",
            "name": "确认落盘",
            "params": {"path": "report.html"},
        },
    ]
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    session = studio._DebugSession(
        root=api.root,
        wid=wid,
        rev=0,
        sid=sid,
        node_id="n1",
        mode="downstream",
        nodes=nodes,
        env={},
        actor="test",
    )
    session.start()
    deadline = time.time() + 30
    debug: dict[str, Any] | None = None
    while time.time() < deadline:
        debug = studio.get_debug_session(api.root, sid, cursor=0)
        if debug.get("done"):
            break
        time.sleep(0.02)
    assert debug and debug["done"] is True
    final: dict[str, str] = {}
    for item in debug["nodes"]:
        if item.get("type") == "node" or item.get("node_id"):
            final[str(item["node_id"])] = str(item["status"])
    assert final == {"n1": "passed", "n2": "passed"}, final

    sandbox = api.root / ".panguverse" / "studio" / wid / "debug" / sid / "workspace"
    assert debug["sandbox"] == str(sandbox)
    assert (sandbox / "report.html").is_file()
    assert not (outside / "report.html").exists()
    assert Path.cwd() == outside
    assert debug["variables"]["env"]["FS_ROOT"] == str(sandbox)


def test_audit_records_state_publish_and_diff(api: WebAPI) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    wid = draft["wid"]
    status, proposal = call(
        api, "POST", f"/api/studio/{wid}/replan", {"instruction": "数据落盘回读 JSON"}
    )
    accepted = [change["change_id"] for change in proposal["diff"]["nodes"]]
    status, applied = call(
        api,
        "POST",
        f"/api/studio/{wid}/apply-diff",
        {"proposed_rev": proposal["proposed_rev"], "accepted": accepted},
    )
    assert status == 200, applied
    status, _ = call(api, "POST", f"/api/studio/{wid}/state", {"to": "review", "note": "待审"})
    assert status == 200
    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 1, "message": "发布 v2"}
    )
    assert status == 200, published

    audit_path = api.root / ".panguverse" / "studio" / wid / "audit.jsonl"
    assert mode_of(audit_path) == 0o600
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    actions = [entry["action"] for entry in entries]
    assert "draft_created" in actions
    assert "diff_applied" in actions
    assert "state" in actions
    assert "publish" in actions
    published_entry = next(entry for entry in entries if entry["action"] == "publish")
    assert published_entry["from"] in (None, "")
    assert published_entry["to"] == f"{wid}@1"
    assert published_entry["actor"] == "local"
    assert published_entry["note"] == "发布 v2"
    diff_entry = next(entry for entry in entries if entry["action"] == "diff_applied")
    assert diff_entry["from"] == 0 and diff_entry["to"] == 1


# --------------------------------------------------------------------------- #
# 绑定项目运行 → 报告可回溯 wid@rev（W7 / R4）
# --------------------------------------------------------------------------- #
def test_published_workflow_binds_project_and_run_traces_ref(
    api: WebAPI, tmp_path: Path
) -> None:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    wid = draft["wid"]
    status, body = call(
        api, "POST", f"/api/studio/{wid}/state", {"to": "approved", "risk_ack": True}
    )
    assert status == 200, body
    status, published = call(
        api, "POST", f"/api/studio/{wid}/publish", {"rev": 0, "message": "备份流程 v1"}
    )
    ref = published["workflow_ref"]

    status, project = call(
        api, "POST", "/api/projects", {"name": "工作流项目", "env": {"FS_ROOT": str(tmp_path / "proj")}}
    )
    pid = project["project_id"]
    status, suite = call(
        api, "POST", f"/api/projects/{pid}/suites", {"name": "备份", "workflow_ref": ref}
    )
    assert status == 201, suite
    assert suite["workflow_ref"] == ref

    status, batch = call(api, "POST", f"/api/projects/{pid}/run", {})
    assert status == 202, batch
    final = wait_batch(api, batch["batch_id"])
    assert final["status"] == "done", final
    job_id = batch["jobs"][0]["job_id"]
    status, job = call(api, "GET", f"/api/jobs/{job_id}")
    assert status == 200
    assert job["status"] == "done", job
    run_id = job["run_id"]

    status, run = call(api, "GET", f"/api/runs/{run_id}")
    assert status == 200
    assert run["report"]["meta"]["workflow_ref"] == ref
    assert run["report"]["meta"]["run_config"]["mode"] == "workflow"
    status, config = call(api, "GET", f"/api/runs/{run_id}/config")
    assert config["mode"] == "workflow"
    assert config["workflow_ref"] == ref

    execution = api.root / ".panguverse" / "executions" / run_id / "execution.json"
    assert execution.is_file()
    assert mode_of(execution) == 0o600
    record = json.loads(execution.read_text(encoding="utf-8"))
    assert record["workflow_ref"] == ref
    assert record["trigger"] == "task"

    # 发布记录与版本都是不可变只读对象
    assert (
        api.root / ".panguverse" / "studio" / wid / "published" / "0.json"
    ).is_file()
    loaded = studio.load_published(api.root, ref)
    assert loaded["revision"]["rev"] == 0


def test_param_fill_is_port_contract_driven() -> None:
    """自动填充只看原子输入端口，不认具体原子名；新增 url 端口自动纳入。"""
    from panguverse.core.atom import Atom
    from panguverse.core.registry import AtomRegistry
    from panguverse.web import param_fill

    registry = AtomRegistry("test")
    registry.register(
        Atom(
            name="fake_fetch",
            fn=lambda **_: None,
            inputs={"target_url": "string", "method?": "string"},
            required=("target_url",),
        )
    )
    nodes = [{"id": "n1", "atom": "fake_fetch", "params": {"target_url": "${env.PAGE_URL}"}}]
    records = param_fill.fill_url_params(
        nodes, registry, "https://example.test/hot", source="site_alias"
    )
    assert nodes[0]["params"]["target_url"] == "https://example.test/hot"
    assert records == [
        {
            "node_id": "n1",
            "atom": "fake_fetch",
            "param": "target_url",
            "value": "https://example.test/hot",
            "source": "site_alias",
            "previous": "${env.PAGE_URL}",
        }
    ]
    assert param_fill.audit_param_gaps(nodes, registry) == []

    # 占位符 + 路径的组合不能被网页 URL 覆盖；它是 env 入参，不是参数缺口。
    composite = [{"id": "n2", "atom": "fake_fetch", "params": {"target_url": "${env.BASE_URL}/api"}}]
    assert param_fill.fill_url_params(composite, registry, "https://example.test/hot") == []
    assert composite[0]["params"]["target_url"] == "${env.BASE_URL}/api"
    assert param_fill.audit_param_gaps(composite, registry) == []
    env_inputs = param_fill.audit_env_inputs(composite, registry)
    assert [(item["env"], item["status"]) for item in env_inputs] == [("BASE_URL", "info")]

    # 带 |default: 的 env 占位符同样不是缺口，只是 default 型入参。
    defaulted = [
        {"id": "n4", "atom": "fake_fetch", "params": {"target_url": "${env.MAX_LATENCY_MS|default:2000|int}"}}
    ]
    assert param_fill.audit_param_gaps(defaulted, registry) == []
    default_inputs = param_fill.audit_env_inputs(defaulted, registry)
    assert default_inputs[0]["status"] == "default"
    assert default_inputs[0]["default"] == "2000"

    # 已经是目标 URL 时不产生 previous == value 的冗余记录。
    same = [{"id": "n5", "atom": "fake_fetch", "params": {"target_url": "https://example.test/hot"}}]
    assert param_fill.fill_url_params(same, registry, "https://example.test/hot") == []
    assert same[0]["params"]["target_url"] == "https://example.test/hot"

    missing = [{"id": "n3", "atom": "fake_fetch", "params": {}}]
    assert param_fill.audit_param_gaps(missing, registry)[0]["reason"] == "missing"
    blank = [{"id": "n6", "atom": "fake_fetch", "params": {"target_url": "   "}}]
    assert param_fill.audit_param_gaps(blank, registry)[0]["reason"] == "empty"


# --------------------------------------------------------------------------- #
# 任务重命名 / 归档 / 列表过滤
# --------------------------------------------------------------------------- #
def _new_draft(api: WebAPI) -> dict[str, Any]:
    status, draft = call(
        api,
        "POST",
        "/api/studio/drafts",
        {"intent": "备份配置文件并校验备份存在", "domain_pack": "local_fs"},
    )
    assert status == 201, draft
    return draft


def _audit_entries(api: WebAPI, wid: str) -> list[dict[str, Any]]:
    path = api.root / ".panguverse" / "studio" / wid / "audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_rename_workflow_updates_name_without_touching_graph(api: WebAPI) -> None:
    draft = _new_draft(api)
    wid = draft["wid"]
    studio_dir = api.root / ".panguverse" / "studio" / wid
    workflow_before = json.loads((studio_dir / "workflow.json").read_text(encoding="utf-8"))
    revision_before = (studio_dir / "revisions" / "0.json").read_bytes()

    status, body = call(api, "PATCH", f"/api/studio/{wid}", {"name": "  备份配置  "})
    assert status == 200, body
    assert body == {"wid": wid, "name": "备份配置", "current_rev": 0}

    workflow_after = json.loads((studio_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow_after["name"] == "备份配置"
    assert workflow_after["current_rev"] == 0
    # 版本文件（图/节点/环境）保持不可变
    assert (studio_dir / "revisions" / "0.json").read_bytes() == revision_before

    status, detail = call(api, "GET", f"/api/studio/{wid}")
    assert status == 200, detail
    assert detail["name"] == "备份配置"
    assert detail["current_rev"] == 0
    assert detail["graph"]["nodes"] == draft["graph"]["nodes"]
    assert detail["graph"]["edges"] == draft["graph"]["edges"]

    status, listing = call(api, "GET", "/api/studio/drafts")
    assert status == 200
    assert listing["drafts"][0]["name"] == "备份配置"
    assert listing["drafts"][0]["current_rev"] == 0

    entry = _audit_entries(api, wid)[-1]
    assert entry["action"] == "rename"
    assert entry["from"] == workflow_before["name"]
    assert entry["to"] == "备份配置"
    assert entry["wid"] == wid
    assert entry["actor"] == "local"


def test_rename_workflow_validation(api: WebAPI) -> None:
    draft = _new_draft(api)
    wid = draft["wid"]

    for payload in ({"name": ""}, {"name": "   "}, {"name": None}, {"name": 123}):
        status, body = call(api, "PATCH", f"/api/studio/{wid}", payload)
        assert status == 400, (payload, body)
        assert body["error"]["code"] == "bad_request"

    status, body = call(api, "PATCH", f"/api/studio/{wid}", {"name": "名" * 201})
    assert status == 400, body
    assert body["error"]["code"] == "bad_request"

    # 边界值 200 字符合法
    status, body = call(api, "PATCH", f"/api/studio/{wid}", {"name": "名" * 200})
    assert status == 200, body
    assert len(body["name"]) == 200

    status, body = call(api, "PATCH", "/api/studio/w-abcdef12", {"name": "不存在"})
    assert status == 404, body
    assert body["error"]["code"] == "not_found"

    status, body = call(api, "PATCH", "/api/studio/not-a-wid", {"name": "x"})
    assert status == 400, body
    assert body["error"]["code"] == "bad_request"


def test_archive_hides_draft_and_include_archived_lists_it(api: WebAPI) -> None:
    archived_draft = _new_draft(api)
    visible_draft = _new_draft(api)
    wid = archived_draft["wid"]

    status, body = call(api, "POST", f"/api/studio/{wid}/archive", {"archived": True})
    assert status == 200, body
    assert body["wid"] == wid
    assert isinstance(body["archived_at"], float)

    workflow = json.loads(
        (api.root / ".panguverse" / "studio" / wid / "workflow.json").read_text(encoding="utf-8")
    )
    assert workflow["archived_at"] == body["archived_at"]

    status, listing = call(api, "GET", "/api/studio/drafts")
    assert status == 200
    assert [item["wid"] for item in listing["drafts"]] == [visible_draft["wid"]]

    status, listing = call(api, "GET", "/api/studio/drafts?include_archived=1")
    assert status == 200
    assert {item["wid"] for item in listing["drafts"]} == {wid, visible_draft["wid"]}
    archived_item = next(item for item in listing["drafts"] if item["wid"] == wid)
    assert archived_item["archived_at"] == body["archived_at"]
    assert archived_item["node_count"] == len(archived_draft["graph"]["nodes"])

    status, body = call(api, "POST", f"/api/studio/{wid}/archive", {"archived": False})
    assert status == 200, body
    assert body["archived_at"] is None
    assert "archived_at" not in json.loads(
        (api.root / ".panguverse" / "studio" / wid / "workflow.json").read_text(encoding="utf-8")
    )

    status, listing = call(api, "GET", "/api/studio/drafts")
    assert {item["wid"] for item in listing["drafts"]} == {wid, visible_draft["wid"]}

    actions = [entry["action"] for entry in _audit_entries(api, wid)]
    assert "archive" in actions and "unarchive" in actions


def test_archive_is_idempotent_and_validates_input(api: WebAPI) -> None:
    draft = _new_draft(api)
    wid = draft["wid"]

    status, first = call(api, "POST", f"/api/studio/{wid}/archive", {"archived": True})
    assert status == 200, first
    status, second = call(api, "POST", f"/api/studio/{wid}/archive", {"archived": True})
    assert status == 200, second
    assert second["archived_at"] == first["archived_at"]

    for payload in ({}, {"archived": "yes"}, {"archived": 1}, {"archived": None}):
        status, body = call(api, "POST", f"/api/studio/{wid}/archive", payload)
        assert status == 400, (payload, body)
        assert body["error"]["code"] == "bad_request"

    status, body = call(api, "POST", "/api/studio/w-abcdef12/archive", {"archived": True})
    assert status == 404, body
    assert body["error"]["code"] == "not_found"
