"""报告中心：列表、人工签署、作废。

- 签署后锁定：再次签署必须 ``force:true``，历史签名全部进审计；
- 作废必须给理由，作废后不可撤销、不可再签署；
- 报告本体（runs/<id>/report.json）永不被修改；人工结论单独落
  ``<ws>/.panguverse/web/reports/<run_id>.json``。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from . import audit, service

REPORTS_REL = Path(".panguverse") / "web" / "reports"
RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
DIR_MODE = 0o700
FILE_MODE = 0o600
MAX_NOTE = 2000


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


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _validate_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise service.ApiError("bad_request", "run_id 非法（应为 6-32 位小写十六进制）")
    return run_id


def record_path(root: Path, run_id: str) -> Path:
    return Path(root) / REPORTS_REL / f"{_validate_run_id(run_id)}.json"


def read_record(root: Path, run_id: str) -> dict[str, Any]:
    path = record_path(root, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_text(value: Any, *, field: str, required: bool, limit: int = MAX_NOTE) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise service.ApiError("bad_request", f"{field} 必须是字符串")
    if required and not text:
        raise service.ApiError("bad_request", f"{field} 不能为空")
    if len(text) > limit:
        raise service.ApiError("bad_request", f"{field} 不能超过 {limit} 个字符")
    return text


def sign_report(
    root: Path, run_id: str, payload: Any, *, actor: str = "local"
) -> dict[str, Any]:
    _validate_run_id(run_id)
    service.get_run(root, run_id)  # 非法/不存在 → 400/404
    data = payload if isinstance(payload, dict) else {}
    if payload is not None and not isinstance(payload, dict):
        raise service.ApiError("bad_request", "请求体必须是 JSON 对象")
    verdict = _clean_text(data.get("verdict"), field="verdict", required=True, limit=100)
    note = _clean_text(data.get("note"), field="note", required=False)
    force = bool(data.get("force", False))

    record = read_record(root, run_id)
    if record.get("voided"):
        raise service.ApiError("conflict", "报告已作废，不能签署")
    signed = bool(record.get("signed"))
    if signed and not force:
        raise service.ApiError("conflict", "报告已签署；如需覆盖请带 force=true")
    old_verdict = record.get("verdict") or ""
    history = list(record.get("history") or [])
    now = time.time()
    entry = {
        "verdict": verdict,
        "note": note,
        "signed_by": actor,
        "signed_at": now,
        "forced": bool(signed and force),
    }
    history.append(entry)
    record.update(
        {
            "run_id": run_id,
            "signed": True,
            "verdict": verdict,
            "note": note,
            "signed_by": actor,
            "signed_at": now,
            "voided": False,
            "history": history,
        }
    )
    _atomic_write(record_path(root, run_id), record)
    audit.append(
        root,
        actor=actor,
        action="report.sign",
        target=f"report:{run_id}",
        from_=old_verdict or None,
        to=verdict,
        note=note or ("强制重签" if signed and force else ""),
    )
    return record


def void_report(
    root: Path, run_id: str, payload: Any, *, actor: str = "local"
) -> dict[str, Any]:
    _validate_run_id(run_id)
    service.get_run(root, run_id)
    if payload is not None and not isinstance(payload, dict):
        raise service.ApiError("bad_request", "请求体必须是 JSON 对象")
    data = payload or {}
    reason = _clean_text(data.get("reason"), field="reason", required=True)
    record = read_record(root, run_id)
    if record.get("voided"):
        raise service.ApiError("conflict", "报告已作废；作废不可撤销")
    now = time.time()
    record.update(
        {
            "run_id": run_id,
            "voided": True,
            "voided_by": actor,
            "voided_at": now,
            "void_reason": reason,
            "signed": bool(record.get("signed")),
        }
    )
    _atomic_write(record_path(root, run_id), record)
    audit.append(
        root,
        actor=actor,
        action="report.void",
        target=f"report:{run_id}",
        from_=record.get("verdict") or ("signed" if record.get("signed") else "unsigned"),
        to="voided",
        note=reason,
    )
    return record


def _insight(root: Path, run_id: str) -> dict[str, Any] | None:
    try:
        return service.read_insight_cache(root, run_id)
    except Exception:  # noqa: BLE001 - 缓存损坏不能拖垮报告列表
        return None


def _row(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    run_id = str(run.get("run_id") or "")
    meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
    config = meta.get("run_config") if isinstance(meta.get("run_config"), dict) else {}
    record = read_record(root, run_id)
    insight = _insight(root, run_id)
    signed = bool(record.get("signed"))
    voided = bool(record.get("voided"))
    sign_state = "voided" if voided else ("signed" if signed else "unsigned")
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    return {
        "run_id": run_id,
        "project_id": run.get("project_id") or "",
        "suite_id": run.get("suite_id") or "",
        "suite_name": meta.get("suite") or "",
        "trigger": config.get("trigger") or meta.get("trigger") or "manual",
        "status": run.get("status") or "done",
        "conclusion": run.get("conclusion"),
        "summary": summary,
        "passed": int(summary.get("passed") or 0),
        "total": int(summary.get("total") or 0),
        "duration_ms": run.get("duration_ms") or summary.get("duration_ms") or 0,
        "workflow_ref": meta.get("workflow_ref") or "",
        "has_insight": bool(insight and insight.get("available", True)),
        "insight_source": (insight or {}).get("source") or "",
        "sign_state": sign_state,
        "signed": signed,
        "voided": voided,
        "verdict": record.get("verdict") or "",
        "note": record.get("note") or "",
        "signed_by": record.get("signed_by") or "",
        "signed_at": record.get("signed_at"),
        "void_reason": record.get("void_reason") or "",
        "voided_by": record.get("voided_by") or "",
        "voided_at": record.get("voided_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


def list_reports(
    root: Path,
    *,
    project: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    rows = [_row(root, run) for run in service.list_runs(root, limit=500)]
    wanted_project = (project or "").strip()
    wanted_status = (status or "all").strip()
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if wanted_project and wanted_project != "all":
            owner = row.get("project_id") or ""
            if wanted_project in ("unclassified", "none", "_"):
                if owner:
                    continue
            elif owner != wanted_project:
                continue
        if wanted_status not in ("", "all"):
            candidates = {
                row.get("sign_state"),
                row.get("status"),
                "voided" if row.get("voided") else "",
                "signed" if row.get("signed") else "unsigned",
            }
            if wanted_status not in candidates:
                continue
        filtered.append(row)
    total = len(filtered)
    return {
        "reports": filtered[offset : offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


__all__ = [
    "list_reports",
    "read_record",
    "record_path",
    "sign_report",
    "void_report",
]
