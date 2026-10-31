"""任务定义（Task Def）落盘层。

契约见 ``tasks/web-tasks-crud-spec.md`` §2：任务定义存
``<workspace>/tasks/<task_id>.json``，目录 ``0700``、文件 ``0600``（含 env 明文，
与 ``last_env.json`` 同策略），原子写（临时文件 + ``os.replace``）。

本模块只做「id 校验 + 路径 + 读写」，字段语义（mode/scenario 校验、PATCH 合并）
留在服务层，避免和 API 层互相 import。错误用 :class:`TaskStoreError` 抛出，
``api.py`` 与 ``ApiError`` 一并捕获。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

TASKS_DIRNAME = "tasks"
TASK_ID_RE = re.compile(r"^t-[0-9a-f]{6,12}$")
DIR_MODE = 0o700
FILE_MODE = 0o600

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "internal_error": 500,
}


class TaskStoreError(Exception):
    """带错误码的任务存储异常（与 ``service.ApiError`` 同形，api 层统一渲染）。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _STATUS_BY_CODE.get(code, 500)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def tasks_dir(root: Path) -> Path:
    return Path(root) / TASKS_DIRNAME


def new_task_id() -> str:
    """服务端生成任务 id（``t-`` + 8 位小写十六进制，满足 6–12 位约束）。"""
    return "t-" + uuid.uuid4().hex[:8]


def validate_task_id(task_id: Any) -> str:
    """校验 task_id 格式，**任何**路径拼接前都必须先过这一关。"""
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise TaskStoreError("bad_request", "task_id 非法（应形如 t-8f3a1c）")
    return task_id


def task_path(root: Path, task_id: str) -> Path:
    return tasks_dir(root) / f"{validate_task_id(task_id)}.json"


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _ensure_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(DIR_MODE)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def write_task(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    task_id = validate_task_id(task.get("task_id"))
    _ensure_dir(tasks_dir(root))
    _atomic_write(task_path(root, task_id), task)
    return task


def read_task(root: Path, task_id: str) -> dict[str, Any]:
    path = task_path(root, task_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskStoreError("not_found", "任务不存在") from exc
    except OSError as exc:
        raise TaskStoreError("internal_error", f"读取任务失败: {exc}") from exc
    except ValueError as exc:
        raise TaskStoreError("internal_error", f"任务文件无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskStoreError("internal_error", "任务文件格式非法")
    data["task_id"] = task_id
    return data


def list_tasks(root: Path) -> list[dict[str, Any]]:
    """按 ``updated_at`` 倒序返回全部任务定义（损坏/非法文件跳过）。"""
    directory = tasks_dir(root)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_file() or child.suffix != ".json":
            continue
        task_id = child.stem
        if not TASK_ID_RE.match(task_id):
            continue
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data["task_id"] = task_id
        items.append(data)
    items.sort(key=lambda item: _as_float(item.get("updated_at")), reverse=True)
    return items


def delete_task(root: Path, task_id: str) -> None:
    path = task_path(root, task_id)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise TaskStoreError("not_found", "任务不存在") from exc
    except OSError as exc:
        raise TaskStoreError("internal_error", f"删除任务失败: {exc}") from exc


def touch_updated(task: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    task["updated_at"] = float(now if now is not None else time.time())
    return task


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "FILE_MODE",
    "TASKS_DIRNAME",
    "TASK_ID_RE",
    "TaskStoreError",
    "delete_task",
    "list_tasks",
    "new_task_id",
    "read_task",
    "task_path",
    "tasks_dir",
    "touch_updated",
    "validate_task_id",
    "write_task",
]
