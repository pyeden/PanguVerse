"""执行阶段（job.phases）持久化。

存放位置：``<workspace>/.panguverse/executions/<run_id>/phases.json``，与
``execution.json`` 同目录。理由：

- phases 是**执行元信息**（含 artifacts warning），不是不可变报告的一部分；
- 单独文件让写 phases 失败时不可能损坏 ``runs/<run_id>/report.json`` 或
  ``execution.json``（满足 A7「旁路失败不改变运行结论」）；
- 读取时缺失/损坏一律降级为空数组，旧记录无需迁移。

写入用 temp + ``os.replace``，目录 0700、文件 0600。
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

PHASES_FILENAME = "phases.json"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")


def valid_run_id(run_id: str) -> bool:
    return bool(isinstance(run_id, str) and _RUN_ID_RE.match(run_id))


def execution_dir(root: str | Path, run_id: str) -> Path:
    return Path(root) / ".panguverse" / "executions" / run_id


def phases_path(root: str | Path, run_id: str) -> Path:
    return execution_dir(root, run_id) / PHASES_FILENAME


def _normalize(phases: Any) -> list[dict[str, str]]:
    """只保留契约字段 name/status/detail，且全部转成字符串。"""
    if not isinstance(phases, list):
        return []
    out: list[dict[str, str]] = []
    for item in phases:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "name": str(item.get("name") or ""),
                "status": str(item.get("status") or ""),
                "detail": str(item.get("detail") or ""),
            }
        )
    return out


def read_phases(root: str | Path, run_id: str) -> list[dict[str, str]]:
    """读取持久化 phases；不存在、非法 JSON、结构不对都返回空数组。"""
    if not valid_run_id(run_id):
        return []
    try:
        data = json.loads(phases_path(root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return _normalize(data.get("phases"))


def write_phases(
    root: str | Path,
    run_id: str,
    *,
    job_id: str,
    phases: Any,
    finished_at: float | None = None,
    status: str = "",
) -> Path | None:
    """原子写入 phases 快照；run_id 非法时不做任何事。

    这里**不吞异常**：调用方负责把写盘失败当作旁路错误处理，保证运行本身
    不被拖垮；同时失败也不会留下半截文件或覆盖已有报告。
    """
    if not valid_run_id(run_id):
        return None
    directory = execution_dir(root, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
    payload = {
        "run_id": run_id,
        "job_id": str(job_id or ""),
        "status": str(status or ""),
        "finished_at": float(finished_at if finished_at is not None else time.time()),
        "phases": _normalize(phases),
    }
    target = directory / PHASES_FILENAME
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".phases.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target


__all__ = [
    "PHASES_FILENAME",
    "execution_dir",
    "phases_path",
    "read_phases",
    "valid_run_id",
    "write_phases",
]
