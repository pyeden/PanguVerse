"""项目 / 套件落盘层（Project → Suite）。

契约见 ``tasks/web-refactor-spec.md`` v4.0 §1：项目存
``<workspace>/.panguverse/projects/<project_id>.json``，目录 ``0700``、文件 ``0600``
（含 env 明文，与 ``last_env.json`` / ``tasks/`` 同策略），原子写（临时文件 + ``os.replace``）。

本模块只做「id 校验 + 路径 + 读写」，字段语义（name/scenario 校验、PATCH 合并、
env 优先级）留在服务层，避免和 API 层互相 import。错误用 :class:`ProjectStoreError`
抛出，``api.py`` 与 ``ApiError`` 一并捕获。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".panguverse"
PROJECTS_DIRNAME = "projects"
PROJECT_ID_RE = re.compile(r"^p-[0-9a-f]{6,12}$")
SUITE_ID_RE = re.compile(r"^s-[0-9a-f]{6,12}$")
DIR_MODE = 0o700
FILE_MODE = 0o600

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "internal_error": 500,
}

#: 同一进程内的读-改-写串行化（项目文件很小，锁粒度按进程即可）
_LOCK = threading.RLock()


class ProjectStoreError(Exception):
    """带错误码的项目存储异常（与 ``service.ApiError`` 同形，api 层统一渲染）。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _STATUS_BY_CODE.get(code, 500)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def projects_dir(root: Path) -> Path:
    """``<workspace>/.panguverse/projects``：集中在已被 gitignore 的运行时目录下。"""
    return Path(root) / STATE_DIRNAME / PROJECTS_DIRNAME


def new_project_id() -> str:
    """服务端生成项目 id（``p-`` + 8 位小写十六进制，满足 6–12 位约束）。"""
    return "p-" + uuid.uuid4().hex[:8]


def new_suite_id() -> str:
    """服务端生成套件 id（``s-`` + 8 位小写十六进制）。"""
    return "s-" + uuid.uuid4().hex[:8]


def validate_project_id(project_id: Any) -> str:
    """校验 project_id 格式，**任何**路径拼接前都必须先过这一关。"""
    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise ProjectStoreError("bad_request", "project_id 非法（应形如 p-8f3a1c）")
    return project_id


def validate_suite_id(suite_id: Any) -> str:
    if not isinstance(suite_id, str) or not SUITE_ID_RE.match(suite_id):
        raise ProjectStoreError("bad_request", "suite_id 非法（应形如 s-8f3a1c）")
    return suite_id


def project_path(root: Path, project_id: str) -> Path:
    return projects_dir(root) / f"{validate_project_id(project_id)}.json"


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _ensure_dir(directory: Path) -> None:
    parent = directory.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            parent.chmod(DIR_MODE)
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


def _normalize(project: dict[str, Any]) -> dict[str, Any]:
    """只做结构兜底（不猜业务字段）：env 必须是 dict、suites 必须是 list。"""
    data = dict(project)
    validate_project_id(data.get("project_id"))
    if not isinstance(data.get("env"), dict):
        data["env"] = {}
    suites = data.get("suites")
    if not isinstance(suites, list):
        suites = []
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for suite in suites:
        if not isinstance(suite, dict):
            continue
        suite_id = validate_suite_id(suite.get("suite_id"))
        if suite_id in seen:  # id 必须唯一，重复视为损坏文件
            raise ProjectStoreError("internal_error", "项目文件里套件 id 重复")
        seen.add(suite_id)
        item = dict(suite)
        if not isinstance(item.get("env"), dict):
            item["env"] = {}
        if not isinstance(item.get("tags"), list):
            item["tags"] = []
        cleaned.append(item)
    data["suites"] = cleaned
    return data


def write_project(root: Path, project: dict[str, Any]) -> dict[str, Any]:
    data = _normalize(project)
    with _LOCK:
        _ensure_dir(projects_dir(root))
        _atomic_write(project_path(root, data["project_id"]), data)
    return data


def read_project(root: Path, project_id: str) -> dict[str, Any]:
    path = project_path(root, project_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectStoreError("not_found", "项目不存在") from exc
    except OSError as exc:
        raise ProjectStoreError("internal_error", f"读取项目失败: {exc}") from exc
    except ValueError as exc:
        raise ProjectStoreError("internal_error", f"项目文件无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectStoreError("internal_error", "项目文件格式非法")
    data["project_id"] = validate_project_id(project_id)
    return _normalize(data)


def list_projects(root: Path) -> list[dict[str, Any]]:
    """按 ``updated_at`` 倒序返回全部项目（损坏/非法文件跳过，不拖垮列表）。"""
    directory = projects_dir(root)
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
        project_id = child.stem
        if not PROJECT_ID_RE.match(project_id):
            continue
        try:
            data = read_project(root, project_id)
        except ProjectStoreError:
            continue
        items.append(data)
    items.sort(key=lambda item: _as_float(item.get("updated_at")), reverse=True)
    return items


def modify_project(
    root: Path, project_id: str, mutator: Callable[[dict[str, Any]], Any]
) -> tuple[dict[str, Any], Any]:
    """读-改-写：``mutator`` 就地修改并返回任意结果；返回 ``(项目, 结果)``。

    整个读改写过程持进程内锁，避免并发 PATCH 互相覆盖。
    """
    with _LOCK:
        project = read_project(root, project_id)
        result = mutator(project)
        project["project_id"] = validate_project_id(project_id)
        written = write_project(root, project)
        return written, result


def delete_project(root: Path, project_id: str) -> None:
    """只删项目定义/套件绑定；``runs/`` 下的历史产物一律不动。"""
    path = project_path(root, project_id)
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ProjectStoreError("not_found", "项目不存在") from exc
        except OSError as exc:
            raise ProjectStoreError("internal_error", f"删除项目失败: {exc}") from exc


def touch_updated(project: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    project["updated_at"] = float(now if now is not None else time.time())
    return project


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "PROJECTS_DIRNAME",
    "PROJECT_ID_RE",
    "STATE_DIRNAME",
    "SUITE_ID_RE",
    "ProjectStoreError",
    "delete_project",
    "list_projects",
    "modify_project",
    "new_project_id",
    "new_suite_id",
    "project_path",
    "projects_dir",
    "read_project",
    "touch_updated",
    "validate_project_id",
    "validate_suite_id",
    "write_project",
]
