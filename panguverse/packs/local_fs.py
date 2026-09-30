"""``local_fs`` 领域包：文件系统 / 本地数据 —— **一个与 API 完全不同的领域**。

它的存在是为了证明平台的通用性：

- 它有自己的失败词汇（``file_not_found`` / ``permission_denied`` / ``disk_full`` …）；
- 它有自己的经验标识（**文件路径**，而不是 URL 路径）；
- 它有自己的步骤语法糖（``expect:``，而不是 HTTP 的 ``assert:``）；
- 它有自己的安全模型（**沙箱根**：默认只允许在工作目录内读写，越界即拒绝）。

而内核一行都没改：执行、重试、自愈、经验沉淀、报告全部照常工作。
"""

from __future__ import annotations

import errno
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core.atom import Atom, atom
from ..core.context import RunContext
from ..core.contract import FailureClass, Recovery
from ..core.errors import AtomFailure, SpecError, StepFailure
from .core import GenericSemantics, enrich

DEFAULT_ROOT_ENV = ("FS_ROOT", "PANGUVERSE_FS_ROOT")


# --------------------------------------------------------------------------- #
# 沙箱：能力授权的最小落地
# --------------------------------------------------------------------------- #
def sandbox_root(ctx: RunContext) -> Path:
    for key in DEFAULT_ROOT_ENV:
        value = ctx.env.get(key)
        if value:
            return Path(str(value)).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_path(ctx: RunContext, path: str, *, allow_outside: bool = False) -> Path:
    """解析路径并做沙箱校验：默认不允许逃出根目录。"""
    if not path:
        raise AtomFailure("缺少 path", kind="invalid_path", failure_class="invalid_input")
    root = sandbox_root(ctx)
    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if allow_outside or resolved == root or root in resolved.parents:
        return resolved
    raise AtomFailure(
        f"路径越界：{path} 不在沙箱根 {root} 内（如确需访问，显式传 allow_outside: true）",
        kind="path_escape",
        failure_class="permission",
    )


def _io_failure(exc: OSError, path: str) -> AtomFailure:
    """把操作系统异常翻译成**领域失败**（本包独有的知识）。"""
    if isinstance(exc, FileNotFoundError):
        return AtomFailure(f"文件不存在: {path}", kind="file_not_found", failure_class="missing_resource")
    if isinstance(exc, PermissionError):
        return AtomFailure(f"没有权限访问: {path}", kind="permission_denied", failure_class="permission")
    if isinstance(exc, IsADirectoryError):
        return AtomFailure(f"目标是目录而非文件: {path}", kind="is_a_directory", failure_class="invalid_input")
    if isinstance(exc, NotADirectoryError):
        return AtomFailure(f"路径中有非目录: {path}", kind="not_a_directory", failure_class="invalid_input")
    if exc.errno == errno.ENOSPC:
        return AtomFailure(f"磁盘空间不足: {path}", kind="disk_full", failure_class="internal", detail={"errno": exc.errno})
    if exc.errno in {errno.EAGAIN, errno.EBUSY, errno.EACCES}:
        return AtomFailure(f"资源暂时不可用: {path}", kind="resource_busy", failure_class="transient")
    return AtomFailure(f"文件操作失败: {path} ({exc})", kind="io_error", failure_class="internal", detail={"errno": exc.errno})


# --------------------------------------------------------------------------- #
# 原子
# --------------------------------------------------------------------------- #
@atom(
    "fs_write_text",
    description="写入文本文件（自动建目录，可追加）",
    category="filesystem",
    tags=("fs", "write"),
    inputs={"path": "string", "text": "string", "append": "boolean=false", "encoding": "string=utf-8", "mkdir": "boolean=true", "allow_outside": "boolean=false"},
    outputs={"path": "string", "bytes": "integer"},
    required=("path", "text"),
    effects=("write",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_write_text(
    ctx: RunContext,
    path: str,
    text: Any,
    append: bool = False,
    encoding: str = "utf-8",
    mkdir: bool = True,
    allow_outside: bool = False,
) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    payload = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    try:
        if mkdir:
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append else "w", encoding=encoding) as handle:
            handle.write(payload)
    except OSError as exc:
        raise _io_failure(exc, str(target)) from exc
    return {"path": str(target), "bytes": len(payload.encode(encoding))}


@atom(
    "fs_read_text",
    description="读取文本文件（不存在即失败：file_not_found）",
    category="filesystem",
    tags=("fs", "read"),
    inputs={"path": "string", "encoding": "string=utf-8", "allow_outside": "boolean=false"},
    outputs={"text": "string", "size": "integer", "path": "string"},
    required=("path",),
    effects=("read",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_read_text(
    ctx: RunContext, path: str, encoding: str = "utf-8", allow_outside: bool = False
) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    try:
        text = target.read_text(encoding=encoding)
    except OSError as exc:
        raise _io_failure(exc, str(target)) from exc
    return {"text": text, "size": len(text.encode(encoding)), "path": str(target)}


@atom(
    "fs_write_json",
    description="把对象序列化成 JSON 写入文件",
    category="filesystem",
    tags=("fs", "write", "json"),
    inputs={"path": "string", "data": "any", "indent": "integer=2", "allow_outside": "boolean=false"},
    outputs={"path": "string", "bytes": "integer"},
    required=("path", "data"),
    effects=("write",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_write_json(
    ctx: RunContext, path: str, data: Any, indent: int = 2, allow_outside: bool = False
) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    payload = json.dumps(data, ensure_ascii=False, indent=int(indent))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise _io_failure(exc, str(target)) from exc
    return {"path": str(target), "bytes": len(payload.encode("utf-8"))}


@atom(
    "fs_read_json",
    description="读取并解析 JSON 文件",
    category="filesystem",
    tags=("fs", "read", "json"),
    inputs={"path": "string", "allow_outside": "boolean=false", "default": "any"},
    outputs={"data": "any", "path": "string"},
    required=("path",),
    effects=("read",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_read_json(
    ctx: RunContext, path: str, allow_outside: bool = False, default: Any = None
) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if default is not None:
            return {"data": default, "path": str(target)}
        raise _io_failure(FileNotFoundError(str(target)), str(target)) from None
    except OSError as exc:
        raise _io_failure(exc, str(target)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AtomFailure(
            f"不是合法 JSON: {target}（{exc}）", kind="invalid_json", failure_class="invalid_input"
        ) from exc
    return {"data": data, "path": str(target)}


@atom(
    "fs_list",
    description="列出目录下的文件（支持 glob 与递归）",
    category="filesystem",
    tags=("fs", "read"),
    inputs={"path": "string=.", "pattern": "string=*", "recursive": "boolean=false", "allow_outside": "boolean=false"},
    outputs={"files": "array", "names": "array", "count": "integer"},
    required=(),
    effects=("read",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_list(
    ctx: RunContext,
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    allow_outside: bool = False,
) -> dict[str, Any]:
    base = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    if not base.exists():
        raise AtomFailure(f"目录不存在: {base}", kind="file_not_found", failure_class="missing_resource")
    if not base.is_dir():
        raise AtomFailure(f"不是目录: {base}", kind="not_a_directory", failure_class="invalid_input")
    glob_pattern = f"**/{pattern}" if recursive else pattern
    files = sorted(str(item) for item in base.glob(glob_pattern) if item.is_file())
    return {"files": files, "names": [Path(item).name for item in files], "count": len(files)}


@atom(
    "fs_exists",
    description="判断路径是否存在（文件/目录/大小）",
    category="filesystem",
    tags=("fs", "read"),
    inputs={"path": "string", "allow_outside": "boolean=false"},
    outputs={"exists": "boolean", "is_file": "boolean", "is_dir": "boolean", "size": "integer", "path": "string"},
    required=("path",),
    effects=("read",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_exists(ctx: RunContext, path: str, allow_outside: bool = False) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    exists = target.exists()
    return {
        "exists": exists,
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "size": target.stat().st_size if exists and target.is_file() else 0,
        "path": str(target),
    }


@atom(
    "fs_copy",
    description="复制文件（可覆盖）",
    category="filesystem",
    tags=("fs", "write"),
    inputs={"source": "string", "target": "string", "overwrite": "boolean=true", "allow_outside": "boolean=false"},
    outputs={"target": "string", "bytes": "integer", "overwritten": "boolean"},
    required=("source", "target"),
    effects=("write",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_copy(
    ctx: RunContext,
    source: str,
    target: str,
    overwrite: bool = True,
    allow_outside: bool = False,
) -> dict[str, Any]:
    src = resolve_path(ctx, str(source), allow_outside=bool(allow_outside))
    dst = resolve_path(ctx, str(target), allow_outside=bool(allow_outside))
    if not src.is_file():
        raise AtomFailure(f"源文件不存在: {src}", kind="file_not_found", failure_class="missing_resource")
    overwritten = dst.exists()
    if overwritten and not overwrite:
        raise AtomFailure(f"目标已存在且不允许覆盖: {dst}", kind="target_exists", failure_class="conflict")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    except OSError as exc:
        raise _io_failure(exc, str(dst)) from exc
    return {"target": str(dst), "bytes": dst.stat().st_size, "overwritten": overwritten}


@atom(
    "fs_delete",
    description="删除文件或空目录（默认不存在也算成功）",
    category="filesystem",
    tags=("fs", "destructive"),
    inputs={"path": "string", "missing_ok": "boolean=true", "allow_outside": "boolean=false"},
    outputs={"deleted": "boolean", "path": "string"},
    required=("path",),
    effects=("destructive",),
    capabilities=("filesystem",),
    domain="filesystem",
)
def fs_delete(
    ctx: RunContext, path: str, missing_ok: bool = True, allow_outside: bool = False
) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    if not target.exists():
        if missing_ok:
            return {"deleted": False, "path": str(target)}
        raise AtomFailure(f"路径不存在: {target}", kind="file_not_found", failure_class="missing_resource")
    try:
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        raise _io_failure(exc, str(target)) from exc
    return {"deleted": True, "path": str(target)}


@atom(
    "assert_file_exists",
    description="断言文件存在（或不存在）",
    category="filesystem",
    tags=("fs", "assert"),
    inputs={"path": "string", "exists": "boolean=true", "allow_outside": "boolean=false"},
    outputs={"path": "string"},
    required=("path",),
    effects=("pure",),
    domain="filesystem",
)
def assert_file_exists(
    ctx: RunContext, path: str, exists: bool = True, allow_outside: bool = False
) -> dict[str, Any]:
    target = resolve_path(ctx, str(path), allow_outside=bool(allow_outside))
    actual = target.exists()
    if actual != bool(exists):
        raise StepFailure(
            f"文件存在性断言失败: {target} 期望 exists={bool(exists)}，实际 {actual}",
            expected=bool(exists),
            actual=actual,
            path=str(target),
        )
    return {"path": str(target)}


@atom(
    "assert_file_contains",
    description="断言文件内容包含指定片段",
    category="filesystem",
    tags=("fs", "assert"),
    inputs={"path": "string", "text": "string", "encoding": "string=utf-8"},
    outputs={"size": "integer"},
    required=("path", "text"),
    effects=("pure",),
    domain="filesystem",
)
def assert_file_contains(ctx: RunContext, path: str, text: str, encoding: str = "utf-8") -> dict[str, Any]:
    target = resolve_path(ctx, str(path))
    try:
        content = target.read_text(encoding=encoding)
    except OSError as exc:
        raise _io_failure(exc, str(target)) from exc
    if str(text) not in content:
        raise StepFailure(
            f"文件内容未包含 {text!r}",
            expected=text,
            actual=content[:400],
            path=str(target),
        )
    return {"size": len(content)}


# --------------------------------------------------------------------------- #
# 领域语义
# --------------------------------------------------------------------------- #
class LocalFsSemantics(GenericSemantics):
    """文件系统领域语义。"""

    name = "local_fs"

    def failure_class(self, atom, outputs, exc) -> FailureClass:
        if isinstance(exc, PermissionError):
            return FailureClass.PERMISSION
        if isinstance(exc, FileNotFoundError):
            return FailureClass.MISSING_RESOURCE
        if isinstance(exc, (IsADirectoryError, NotADirectoryError)):
            return FailureClass.INVALID_INPUT
        return super().failure_class(atom, outputs, exc)

    def failure_kind(self, atom, outputs, exc) -> str:
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, FileNotFoundError):
            return "file_not_found"
        if isinstance(exc, IsADirectoryError):
            return "is_a_directory"
        if isinstance(exc, NotADirectoryError):
            return "not_a_directory"
        return super().failure_kind(atom, outputs, exc)

    def is_retryable(self, atom, outputs, *, kind: str, failure_class: FailureClass) -> bool:
        return kind in {"resource_busy", "io_transient"} or super().is_retryable(
            atom, outputs, kind=kind, failure_class=failure_class
        )

    def target(self, atom, inputs) -> str:
        """经验标识 = 文件路径（与 HTTP 的 URL 路径是完全不同的标识体系）。"""
        if not isinstance(inputs, Mapping):
            return ""
        for key in ("path", "source", "target"):
            value = inputs.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def recoveries(self, atom, *, kind, failure_class, case=None, step=None, inputs=None) -> list:
        out: list[Recovery] = []
        if failure_class is FailureClass.TRANSIENT:
            out.append(
                Recovery(
                    action="retry",
                    reason=f"{kind} 属于可恢复的 IO 抖动 → 退避重试",
                    delay_ms=100,
                    retry_hint={"attempts": 3, "delay_ms": 100, "backoff": 2.0, "on": ["any"]},
                    advice="文件被占用/资源繁忙：稍后重试；若长期占用，检查是否有其他进程持有",
                )
            )
        return out

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        texts = {
            "file_not_found": "文件不存在：确认路径来源，或在前置步骤先创建（fs_write_text / fs_copy）",
            "permission_denied": "权限不足：确认运行身份，或把工作目录放到沙箱根内（FS_ROOT）",
            "is_a_directory": "目标目录而非文件：补上文件名，或改用 fs_list",
            "not_a_directory": "路径里有非目录项：检查路径拼写",
            "disk_full": "磁盘空间不足：清理临时文件或换目录",
            "resource_busy": "文件被占用：退避重试或先释放句柄",
            "invalid_json": "文件内容不是合法 JSON：先用 fs_read_text 看原文，或修正上游写入",
            "path_escape": "路径越界：默认沙箱只允许在 FS_ROOT 内读写",
            "target_exists": "目标已存在：允许覆盖或换一个目标路径",
        }
        return texts.get(kind) or super().advice(kind=kind, failure_class=failure_class)

    # -- 领域专属语法糖：``expect:`` ------------------------------------- #
    def sugar_keys(self) -> set[str]:
        return {"expect"}

    def sugar(self, step_id: str, step: Mapping[str, Any], *, atom: Atom | None = None) -> list[dict[str, Any]]:
        block = step.get("expect")
        if block is None:
            return []
        if not isinstance(block, Mapping):
            raise SpecError(f"步骤 {step_id} 的 expect 必须是映射")
        allowed = {"exists", "contains", "message"}
        unknown = set(block) - allowed
        if unknown:
            raise SpecError(f"步骤 {step_id} 的 expect 含未知字段: {sorted(unknown)}；可用: {sorted(allowed)}")
        # 目标路径优先用步骤声明的同名输出（write/copy 的输出名不同，这里做一次适配）
        outputs = set(getattr(atom, "outputs", {}) or {})
        path_expr = f"${{{step_id}.path}}"
        if "path" not in outputs:
            for candidate in ("target", "file"):
                if candidate in outputs:
                    path_expr = f"${{{step_id}.{candidate}}}"
                    break
        expanded: list[dict[str, Any]] = []
        if "exists" in block:
            expanded.append(
                {
                    "id": f"{step_id}__assert_file_exists",
                    "use": "assert_file_exists",
                    "name": f"{step_id} 存在性断言",
                    "with": {"path": path_expr, "exists": block["exists"]},
                    "tags": ["assert"],
                }
            )
        if "contains" in block:
            expanded.append(
                {
                    "id": f"{step_id}__assert_file_contains",
                    "use": "assert_file_contains",
                    "name": f"{step_id} 内容包含断言",
                    "with": {"path": path_expr, "text": block["contains"]},
                    "tags": ["assert"],
                }
            )
        if not expanded:
            raise SpecError(f"步骤 {step_id} 的 expect 块没有可展开的断言")
        return expanded

    def planning_hints(self) -> tuple[str, ...]:
        """文件系统领域的规划惯例。"""
        return (
            "所有文件路径都在沙箱根内（相对 env.FS_ROOT 解析），不要写绝对路径或 ..",
            "写文件用 fs_write_text / fs_write_json；写完可用 expect: {exists: true} 校验",
            "读取 JSON 用 fs_read_json，再用 assert_json 断言字段（它与 API 领域共用同一个原子）",
            "删除属于 destructive 副作用：计划里不会自动重试，请显式确认幂等",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": "filesystem",
            "target": "文件路径",
            "sandbox_root_env": list(DEFAULT_ROOT_ENV),
            "sugar": sorted(self.sugar_keys()),
        }


def build_pack() -> Any:
    """构建 ``local_fs`` 包。"""
    from ..core.pack import AtomPack
    from .local_fs_templates import local_fs_intent_templates

    atoms: list[Atom] = [
        fs_write_text,
        fs_read_text,
        fs_write_json,
        fs_read_json,
        fs_list,
        fs_exists,
        fs_copy,
        fs_delete,
        assert_file_exists,
        assert_file_contains,
    ]
    for item in atoms:
        enrich(item, domain="filesystem", pack="local_fs")
    return AtomPack(
        name="local_fs",
        domain="filesystem",
        version="0.1.0",
        description="本地文件系统领域包：文本/JSON 读写、目录列举、复制删除与文件断言（带沙箱根）",
        semantics=LocalFsSemantics(),
        atoms=atoms,
        requires=("filesystem",),
        tags=("builtin", "filesystem", "data"),
        license="Apache-2.0",
        intent_templates=local_fs_intent_templates(),
    )


__all__ = ["LocalFsSemantics", "build_pack", "fs_write_text", "resolve_path", "sandbox_root"]
