"""Explore-first sessions: trace/artifacts on disk, crystallize, replay.

Layout (per session)::

    <ws>/.panguverse/explore/<eid>/session.json
    <ws>/.panguverse/explore/<eid>/trace.jsonl      # append-only
    <ws>/.panguverse/explore/<eid>/artifacts/
    <ws>/.panguverse/explore/<eid>/success.json     # checks + evidence + verdict

The module owns persistence and API-facing orchestration; the decision loop and
sandbox tools live in :mod:`panguverse.axe.agent_exec`.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..axe import agent_exec
from ..core import workspace_packs
from . import studio
from .studio import StudioError

EXPLORE_DIRNAME = "explore"
EID_RE = re.compile(r"^e-[0-9a-f]{8,16}$")
DIR_MODE = 0o700
FILE_MODE = 0o600
MAX_EVENT_TEXT = 4_000
TERMINAL_STATUSES = ("succeeded", "failed", "budget_exceeded", "error")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# --------------------------------------------------------------------------- #
# 基础落盘
# --------------------------------------------------------------------------- #
def explore_root(root: Path) -> Path:
    return Path(root) / ".panguverse" / EXPLORE_DIRNAME


def _session_dir(root: Path, eid: str) -> Path:
    if not isinstance(eid, str) or not EID_RE.match(eid):
        raise StudioError("bad_request", "eid 非法（应形如 e-1a2b3c4d）")
    return explore_root(root) / eid


def _ensure_dir(directory: Path) -> None:
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


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_dir(path.parent)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
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
    except (OSError, ValueError) as exc:
        raise StudioError("internal_error", f"{broken}: {exc}") from exc
    if not isinstance(data, dict):
        raise StudioError("internal_error", f"{broken}：顶层必须是对象")
    return data


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _lock_for(eid: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(eid, threading.Lock())


# --------------------------------------------------------------------------- #
# 写入器
# --------------------------------------------------------------------------- #
class _Writer:
    """Append-only trace writer; finalizes session/success on ``end``."""

    def __init__(self, root: Path, eid: str, meta: dict[str, Any]) -> None:
        self.root = Path(root)
        self.eid = eid
        self.dir = _session_dir(root, eid)
        self.meta = meta
        self.lock = _lock_for(eid)
        events = _read_events(self.dir / "trace.jsonl")
        self.seq = max((int(item.get("seq") or 0) for item in events), default=0)

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "ts": time.time(), "type": kind, **payload}
            _ensure_dir(self.dir)
            line = json.dumps(event, ensure_ascii=False, default=str)
            if len(line) > 200_000:
                # 事件本身不应无限膨胀；超长文本单独落产物
                for key in ("stdout", "stderr", "text"):
                    if isinstance(event.get(key), str):
                        event[key] = event[key][:MAX_EVENT_TEXT] + "...[truncated]"
                line = json.dumps(event, ensure_ascii=False, default=str)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            fd = os.open(str(self.dir / "trace.jsonl"), flags, FILE_MODE)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            with contextlib.suppress(OSError):
                os.chmod(self.dir / "trace.jsonl", FILE_MODE)
        if kind == "end":
            self._finalize(event)
        return event

    def _finalize(self, event: Mapping[str, Any]) -> None:
        status = str(event.get("status") or "error")
        checks = list(event.get("checks") or [])
        summary = dict(event.get("summary") or {})
        verdict = str(event.get("verdict") or ("passed" if status == "succeeded" else "failed"))
        now = time.time()
        meta = dict(self.meta)
        meta.update(
            {
                "status": status,
                "summary": summary,
                "checks": checks,
                "verdict": verdict,
                "artifacts": list_artifacts(self.dir),
                "finished_at": now,
                "updated_at": now,
            }
        )
        if event.get("error"):
            meta["error"] = event.get("error")
        _atomic_write_json(self.dir / "session.json", meta)
        _atomic_write_json(
            self.dir / "success.json",
            {
                "eid": self.eid,
                "verdict": verdict,
                "status": status,
                "checks": checks,
                "summary": summary,
                "criteria": meta.get("criteria") or [],
                "finished_at": now,
            },
        )
        self.meta = meta


def list_artifacts(session_dir: Path) -> list[dict[str, Any]]:
    base = session_dir / "artifacts"
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        items.append(
            {
                "path": str(path.relative_to(session_dir)),
                "bytes": size,
                "content_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            }
        )
    return items


# --------------------------------------------------------------------------- #
# 启动 / 读取
# --------------------------------------------------------------------------- #
def start_explore(
    root: Path,
    payload: Any,
    *,
    actor: str = "local",
    llm: Any = None,
    registry: Any = None,
) -> dict[str, Any]:
    """Validate + create a session, then run the agent in a daemon thread."""
    data = payload if isinstance(payload, dict) else {}
    intent = str(data.get("intent") or "").strip()
    if not intent:
        raise StudioError("bad_request", "intent 不能为空")
    intent = intent[:500]
    raw_criteria = data.get("success_criteria")
    try:
        criteria = agent_exec.normalize_checks(raw_criteria)
    except agent_exec.AgentExecError as exc:
        raise StudioError(exc.code, exc.message, status=exc.status) from exc
    if llm is None:
        llm = studio._llm_client()
    if llm is None or getattr(llm, "available", False) is False:
        raise StudioError(
            "llm_required",
            "探索需要配置 LLM（PANGUVERSE_API_KEY / OPENAI_API_KEY）；未配置时不做假执行",
            status=503,
            details={"llm": _llm_status(llm)},
        )
    try:
        max_steps = int(data.get("max_steps") or 12)
    except (TypeError, ValueError) as exc:
        raise StudioError("bad_request", "max_steps 必须是整数") from exc
    max_steps = max(1, min(50, max_steps))
    allow_scripts = data.get("allow_scripts") is not False
    domain_pack = str(data.get("domain_pack") or "").strip()[:80]
    eid = "e-" + uuid.uuid4().hex[:12]
    session_dir = _session_dir(root, eid)
    _ensure_dir(session_dir / "artifacts")
    now = time.time()
    meta = {
        "eid": eid,
        "intent": intent,
        "success_criteria": raw_criteria,
        "criteria": criteria,
        "domain_pack": domain_pack,
        "allow_scripts": allow_scripts,
        "max_steps": max_steps,
        "model": _llm_status(llm).get("model") or data.get("model") or "",
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "created_by": actor,
        "summary": {},
        "checks": [],
        "artifacts": [],
    }
    _atomic_write_json(session_dir / "session.json", meta)
    _ensure_dir(session_dir)
    (session_dir / "trace.jsonl").touch(mode=FILE_MODE)
    thread = threading.Thread(
        target=_run_session,
        kwargs={
            "root": Path(root),
            "eid": eid,
            "meta": meta,
            "llm": llm,
            "registry": registry,
        },
        name=f"explore-{eid}",
        daemon=True,
    )
    thread.start()
    return {"eid": eid, "status": "running", "cursor": 0}


def _llm_status(llm: Any) -> dict[str, Any]:
    if llm is None:
        return {"available": False, "reason": "未配置 LLM 客户端"}
    try:
        status = llm.status()
        return status if isinstance(status, dict) else {"available": bool(getattr(llm, "available", False))}
    except Exception:  # noqa: BLE001
        return {"available": bool(getattr(llm, "available", False))}


def _run_session(
    *,
    root: Path,
    eid: str,
    meta: dict[str, Any],
    llm: Any,
    registry: Any,
) -> None:
    from . import service

    if registry is None:
        registry = service.registry()
    writer = _Writer(root, eid, meta)
    session_dir = _session_dir(root, eid)
    try:
        agent = agent_exec.ExploreAgent(
            sandbox=session_dir,
            registry=registry,
            llm=llm,
            emit=writer.append,
            allow_scripts=bool(meta.get("allow_scripts")),
            max_steps=int(meta.get("max_steps") or 12),
            intent=str(meta.get("intent") or ""),
            domain_pack=str(meta.get("domain_pack") or ""),
        )
        agent.run(str(meta.get("intent") or ""), meta.get("criteria") or [])
    except agent_exec.AgentExecError as exc:
        writer.append(
            "end",
            {
                "status": "error",
                "verdict": "failed",
                "checks": [],
                "summary": {"reason": exc.message},
                "error": {"code": exc.code, "message": exc.message},
            },
        )
    except Exception as exc:  # noqa: BLE001 - 后台线程异常必须落盘
        writer.append(
            "end",
            {
                "status": "error",
                "verdict": "failed",
                "checks": [],
                "summary": {"reason": f"{type(exc).__name__}: {exc}"},
                "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
            },
        )


def get_session(root: Path, eid: str, *, cursor: int = 0) -> dict[str, Any]:
    session_dir = _session_dir(root, eid)
    meta = _read_json(
        session_dir / "session.json",
        not_found="探索会话不存在",
        broken="探索会话文件无法解析",
    )
    events = _read_events(session_dir / "trace.jsonl")
    cursor = max(0, int(cursor or 0))
    pending = [item for item in events if int(item.get("seq") or 0) > cursor]
    end = next((item for item in reversed(events) if item.get("type") == "end"), None)
    if end is not None and meta.get("status") not in TERMINAL_STATUSES:
        meta = dict(meta)
        meta["status"] = end.get("status")
        meta["verdict"] = end.get("verdict")
        meta["summary"] = end.get("summary") or {}
        meta["checks"] = end.get("checks") or []
    checks = list(meta.get("checks") or [])
    if not checks and end is not None:
        checks = list(end.get("checks") or [])
    return {
        "eid": eid,
        "cursor": max([cursor, *[int(item.get("seq") or 0) for item in pending]]),
        "status": str(meta.get("status") or "missing"),
        "done": bool(meta.get("status") in TERMINAL_STATUSES or end is not None),
        "intent": meta.get("intent") or "",
        "success_criteria": meta.get("success_criteria"),
        "criteria": meta.get("criteria") or [],
        "summary": meta.get("summary") or {},
        "checks": checks,
        "verdict": meta.get("verdict") or "",
        "artifacts": list_artifacts(session_dir),
        "events": pending,
        "error": meta.get("error") or {},
        "model": meta.get("model") or "",
        "created_at": meta.get("created_at"),
        "finished_at": meta.get("finished_at"),
    }


def list_sessions(root: Path, *, limit: int = 50) -> dict[str, Any]:
    base = explore_root(root)
    items: list[dict[str, Any]] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if not child.is_dir() or not EID_RE.match(child.name):
                continue
            try:
                meta = json.loads((child / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            events = _read_events(child / "trace.jsonl")
            end = next((item for item in reversed(events) if item.get("type") == "end"), None)
            items.append(
                {
                    "eid": child.name,
                    "intent": meta.get("intent") or "",
                    "status": meta.get("status") or ((end or {}).get("status") or "missing"),
                    "verdict": meta.get("verdict") or ((end or {}).get("verdict") or ""),
                    "created_at": meta.get("created_at"),
                    "finished_at": meta.get("finished_at"),
                    "summary": meta.get("summary") or ((end or {}).get("summary") or {}),
                }
            )
    items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return {"sessions": items[: max(1, int(limit))]}


# --------------------------------------------------------------------------- #
# 产物
# --------------------------------------------------------------------------- #
def read_artifact(root: Path, eid: str, path: str) -> tuple[bytes, str, str]:
    session_dir = _session_dir(root, eid)
    if not session_dir.is_dir():
        raise StudioError("not_found", "探索会话不存在")
    text = str(path or "").strip()
    if not text:
        raise StudioError("bad_request", "path 不能为空")
    try:
        target = agent_exec.safe_path(session_dir, text, field="path")
    except agent_exec.AgentExecError as exc:
        raise StudioError(exc.code, exc.message, status=exc.status) from exc
    artifacts_base = (session_dir / "artifacts").resolve()
    try:
        target.relative_to(artifacts_base)
    except ValueError as exc:
        raise StudioError("bad_request", "只允许读取 artifacts/ 下的产物") from exc
    if not target.is_file():
        raise StudioError("not_found", "产物不存在")
    data = target.read_bytes()
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in (
        "application/json",
        "application/javascript",
        "image/svg+xml",
    ):
        content_type += "; charset=utf-8"
    return data, content_type, target.name


# --------------------------------------------------------------------------- #
# 固化 + 重放
# --------------------------------------------------------------------------- #
def crystallize(
    root: Path,
    eid: str,
    payload: Any = None,
    *,
    actor: str = "explore",
    registry: Any = None,
    llm: Any = None,
) -> dict[str, Any]:
    from . import service

    data = payload if isinstance(payload, dict) else {}
    session_dir = _session_dir(root, eid)
    meta = _read_json(
        session_dir / "session.json",
        not_found="探索会话不存在",
        broken="探索会话文件无法解析",
    )
    events = _read_events(session_dir / "trace.jsonl")
    try:
        success = _read_json(
            session_dir / "success.json",
            not_found="探索尚未结束，不能固化",
            broken="success.json 无法解析",
        )
    except StudioError as exc:
        # trace 的 end 事件可能先于 success.json 落盘（终态写入有毫秒级窗口）。
        end = next(
            (item for item in reversed(events) if item.get("type") == "end"), None
        )
        if exc.code != "not_found" or end is None:
            raise
        success = {
            "verdict": end.get("verdict") or "",
            "status": end.get("status") or "",
            "checks": list(end.get("checks") or []),
            "summary": dict(end.get("summary") or {}),
        }
        meta = dict(meta)
        meta["verdict"] = success["verdict"]
        meta["status"] = success["status"]
        meta["summary"] = success["summary"]
        meta["checks"] = success["checks"]
    if success.get("verdict") != "passed" or meta.get("verdict") != "passed":
        raise StudioError(
            "not_succeeded",
            "探索未通过确定性校验，不能固化",
            status=409,
            details={"checks": success.get("checks") or []},
        )
    if registry is None:
        registry = service.registry()
    if llm is None:
        llm = studio._llm_client()
    if llm is None or getattr(llm, "available", True) is False:
        raise StudioError(
            "llm_required_for_crystallize",
            "固化需要 LLM 做语义归纳（PANGUVERSE_API_KEY / OPENAI_API_KEY）；未配置时不做假固化",
            status=503,
            details={"llm": _llm_status(llm)},
        )
    try:
        graph = agent_exec.build_crystallized_graph(
            events,
            eid=eid,
            intent=str(meta.get("intent") or ""),
            checks=agent_exec.checks_from_success(success),
            name=str(data.get("name") or ""),
            domain_pack=str(meta.get("domain_pack") or ""),
            registry=registry,
            session_dir=session_dir,
            workspace=root,
            llm=llm,
        )
    except agent_exec.AgentExecError as exc:
        raise StudioError(exc.code, exc.message, status=exc.status, details=exc.details) from exc

    atoms_created = list(graph.get("atoms_created") or [])
    # Runtime atom lookup must see the workspace pack without mutating the
    # process-global registry (one workspace cannot pollute another).
    registry = workspace_packs.workspace_registry(root, registry)
    # 测试注入的轻量 registry 不参与 studio 的 plan 校验（其接口不含 packs）；
    # 生产 registry 则必须带上本 workspace 的原子，否则新原子会被判“不存在”。
    validation_registry = registry if hasattr(registry, "packs") else None

    try:
        replay = agent_exec.replay_graph(
            nodes=graph["nodes"],
            registry=registry,
            sandbox=session_dir / "replay",
            checks=graph["checks"],
            clean=True,
        )
    except Exception as exc:  # noqa: BLE001 - 重放异常也要落成 replay_failed
        replay = {
            "status": "failed",
            "checks": [],
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            "llm_calls": 0,
            "clean_workspace": True,
        }
    created = studio.create_draft_from_graph(
        root,
        intent=str(meta.get("intent") or ""),
        name=str(data.get("name") or graph.get("name") or ""),
        nodes=graph["nodes"],
        edges=graph["edges"],
        actor=actor,
        explore_id=eid,
        domain_pack=str(meta.get("domain_pack") or ""),
        pending_atoms=graph["pending_atoms"],
        replay_checks=graph["checks"],
        abstraction={
            "plan": graph.get("abstraction_plan") or {},
            "atoms_created": atoms_created,
            "dropped_steps": graph.get("dropped_steps") or [],
            "replay_llm_calls": int(replay.get("llm_calls") or 0),
        },
        registry=validation_registry,
    )
    recorded = studio.record_replay(root, created["wid"], replay)
    return {
        "wid": created["wid"],
        "rev": created["rev"],
        "explore_id": eid,
        "atoms_created": atoms_created,
        "pending_atoms": graph["pending_atoms"],
        "out_of_scope": graph.get("out_of_scope") or [],
        "route": graph.get("route") or "",
        "synthesis": graph.get("synthesis") or {},
        "dropped_checks": graph.get("dropped_checks") or [],
        "abstraction_plan": graph.get("abstraction_plan") or {},
        "dropped_steps": graph.get("dropped_steps") or [],
        "replay": replay,
        "llm_calls_replay": int(replay.get("llm_calls") or 0),
        "validation": created.get("validation") or {},
        "checks": graph["checks"],
        "graph": {"nodes": graph["nodes"], "edges": graph["edges"]},
        "state": recorded.get("state") or created.get("state") or "draft",
        "replay_status": recorded.get("replay_status") or "",
    }


__all__ = [
    "EID_RE",
    "EXPLORE_DIRNAME",
    "crystallize",
    "explore_root",
    "get_session",
    "list_sessions",
    "read_artifact",
    "start_explore",
]
