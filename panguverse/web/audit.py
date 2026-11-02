"""工作台 P0 审计：任务/项目归档、执行取消、报告签署/作废。

与 studio 的 ``audit.jsonl`` 分离：工作台流水放
``<ws>/.panguverse/web/audit.jsonl``，append-only、``0600``、目录 ``0700``。
每条固定 ``actor/action/target/from/to/note``（另附 ``ts``/``at``）。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

AUDIT_REL = Path(".panguverse") / "web"
AUDIT_FILENAME = "audit.jsonl"
DIR_MODE = 0o700
FILE_MODE = 0o600


def audit_path(root: Path) -> Path:
    return Path(root) / AUDIT_REL / AUDIT_FILENAME


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


def append(
    root: Path,
    *,
    actor: str,
    action: str,
    target: str,
    from_: Any = None,
    to: Any = None,
    note: str = "",
) -> dict[str, Any]:
    """追加一条审计；写失败不抛给业务（最佳努力，但正常路径必须成功）。"""
    entry = {
        "ts": time.time(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": actor or "local",
        "action": action,
        "target": target,
        "from": from_,
        "to": to,
        "note": str(note or "")[:2000],
    }
    path = audit_path(root)
    _ensure_dir(path.parent)
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, FILE_MODE)
    return entry


def tail(root: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    path = audit_path(root)
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


__all__ = ["append", "audit_path", "tail"]
