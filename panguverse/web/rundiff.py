"""运行对比（G4）：把两次运行的变化说清楚，而不是只给通过/失败。

对比维度：
- 用例汇总（通过/失败/异常/耗时）
- 步骤级状态变化（新增/缺失/状态改变/耗时变化）
- 产出物变化（新增/缺失/内容哈希变化）
- 变量变化（顶层键）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _runs_root(root: Path) -> Path:
    return Path(root) / "runs"


def _load(root: Path, run_id: str) -> dict[str, Any] | None:
    path = _runs_root(root) / str(run_id) / "report.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _steps(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for case in report.get("cases") or []:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("case_id") or case.get("name") or "")
        for step in case.get("steps") or []:
            if not isinstance(step, dict):
                continue
            key = f"{case_id}:{step.get('step_id') or step.get('id') or ''}"
            out[key] = {
                "atom": str(step.get("atom") or ""),
                "status": str(step.get("status") or ""),
                "duration_ms": float(step.get("duration_ms") or 0.0),
            }
    return out


def _artifact_paths(report: dict[str, Any]) -> dict[str, str]:
    raw = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    out: dict[str, str] = {}
    for group in ("execution", "task_outputs"):
        items = raw.get(group) if isinstance(raw.get(group), dict) else {}
        for name, path in items.items():
            if isinstance(path, str) and path:
                out[f"{group}:{name}"] = path
    return out


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _pick_base(root: Path, target: dict[str, Any]) -> str | None:
    """默认基线：同一 workflow_ref 的上一次运行；优先成功运行。"""
    ref = str((target.get("meta") or {}).get("workflow_ref") or "")
    started = float(target.get("started_at") or 0)
    candidates: list[tuple[int, float, str]] = []
    base = _runs_root(root)
    if not base.is_dir():
        return None
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name == str(target.get("run_id")):
            continue
        report = _load(root, entry.name)
        if not report:
            continue
        meta = report.get("meta") or {}
        if ref and str(meta.get("workflow_ref") or "") != ref:
            continue
        if float(report.get("started_at") or 0) >= started:
            continue
        success = 1 if (report.get("summary") or {}).get("success") else 0
        candidates.append((success, float(report.get("started_at") or 0), entry.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def diff_runs(root: Path, run_id: str, *, base_id: str | None = None) -> dict[str, Any]:
    target = _load(root, run_id)
    if target is None:
        raise FileNotFoundError(f"run 不存在: {run_id}")
    base_id = base_id or _pick_base(root, target)
    if not base_id:
        return {"run_id": run_id, "base_id": None, "available": False, "reason": "没有可对比的历史运行"}
    base = _load(root, base_id)
    if base is None:
        raise FileNotFoundError(f"基线 run 不存在: {base_id}")

    base_steps, target_steps = _steps(base), _steps(target)
    step_changes: list[dict[str, Any]] = []
    for key in sorted(set(base_steps) | set(target_steps)):
        old = base_steps.get(key)
        new = target_steps.get(key)
        if old is None or new is None:
            step_changes.append({
                "step": key,
                "change": "added" if old is None else "removed",
                "base": old,
                "target": new,
            })
            continue
        status_changed = old["status"] != new["status"]
        duration_changed = abs(old["duration_ms"] - new["duration_ms"]) >= 1.0
        if not status_changed and not duration_changed:
            continue
        step_changes.append({
            "step": key,
            "change": "status_changed" if status_changed else "duration_changed",
            "changed": [name for name, flag in (("status", status_changed), ("duration", duration_changed)) if flag],
            "atom": new["atom"],
            "base_status": old["status"],
            "target_status": new["status"],
            "base_duration_ms": old["duration_ms"],
            "target_duration_ms": new["duration_ms"],
            "duration_delta_ms": round(new["duration_ms"] - old["duration_ms"], 2),
        })

    base_paths, target_paths = _artifact_paths(base), _artifact_paths(target)
    artifact_changes: list[dict[str, Any]] = []
    for name in sorted(set(base_paths) | set(target_paths)):
        old_path, new_path = base_paths.get(name), target_paths.get(name)
        old_hash = _digest(_resolve(root, base_id, old_path)) if old_path else None
        new_hash = _digest(_resolve(root, run_id, new_path)) if new_path else None
        if old_hash is None or new_hash is None:
            artifact_changes.append({"name": name, "change": "added" if old_path is None else "removed"})
        elif old_hash != new_hash:
            artifact_changes.append({"name": name, "change": "content_changed", "base_sha256": old_hash, "target_sha256": new_hash})

    base_vars = _variables(base)
    target_vars = _variables(target)
    variable_changes = []
    for key in sorted(set(base_vars) | set(target_vars)):
        if base_vars.get(key) != target_vars.get(key):
            variable_changes.append({"name": key, "base": _short(base_vars.get(key)), "target": _short(target_vars.get(key))})

    return {
        "run_id": run_id,
        "base_id": base_id,
        "available": True,
        "summary": {"base": base.get("summary") or {}, "target": target.get("summary") or {}},
        "steps": step_changes,
        "artifacts": artifact_changes,
        "variables": variable_changes,
        "counts": {
            "steps": len(step_changes),
            "artifacts": len(artifact_changes),
            "variables": len(variable_changes),
        },
    }


def _resolve(root: Path, run_id: str, value: str) -> Path:
    """产物路径解析：绝对路径直接用；相对路径优先相对本次 run 目录。"""
    path = Path(value)
    if path.is_absolute():
        return path
    return _runs_root(root) / str(run_id) / path


def _variables(report: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for case in report.get("cases") or []:
        if isinstance(case, dict) and isinstance(case.get("variables"), dict):
            merged.update(case["variables"])
    return merged


def _short(value: Any, limit: int = 160) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
