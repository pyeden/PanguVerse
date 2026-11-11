"""任务产出物：收集、落盘、列表、下载与只读分享（契约 v8.0 §2）。

产出物是「一次运行中值得交付的结果」，不是日志。来源两类：

1. **原子声明** ``Atom.produces``（如 ``llm_summarize.produces=["summary_md"]``）；
2. **报告级** AI 总结（``insight.summary_md``）自动成为 ``ai_summary``。

落盘布局::

    <workspace>/.panguverse/artifacts/<project_id|unclassified>/<run_id>/<name>.<ext>
    <workspace>/.panguverse/artifacts/<project_id|unclassified>/<run_id>/manifest.json

目录 0700、文件 0600；manifest 记录 sha256/bytes/node_id。**收集失败不影响运行结论**
（调用方只记告警）。单产物上限 20MB，超出只记元信息 + ``truncated=true``。

分享 token 只读、不可列目录、过期 410、撤销 404；分享记录单独落在
``<workspace>/.panguverse/artifact-shares/<token>.json``（0600）。
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..core.registry import default_registry

STATE_DIRNAME = ".panguverse"
ARTIFACTS_DIRNAME = "artifacts"
SHARES_DIRNAME = "artifact-shares"
MANIFEST_NAME = "manifest.json"
DIR_MODE = 0o700
FILE_MODE = 0o600
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_INLINE_BYTES = 64 * 1024
DEFAULT_SHARE_TTL = 7 * 24 * 3600
MAX_SHARE_TTL = 30 * 24 * 3600
AID_RE = re.compile(r"^ar-[0-9a-f]{6,12}$")
RUN_RE = re.compile(r"^[0-9a-f][0-9a-f-]{5,39}$")
PROJECT_RE = re.compile(r"^p-[0-9a-f]{6,12}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SHARE_TTLS = {
    "1h": 3600,
    "1d": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}
TEXT_TYPES = {"markdown", "json", "xml", "text"}
KNOWN_TYPES = TEXT_TYPES | {"image", "file"}
#: 产出物分类：执行报告（怎么跑的） vs 任务产出（跑出了什么）。
CATEGORY_EXECUTION = "execution"
CATEGORY_TASK_OUTPUT = "task_output"
ARTIFACT_CATEGORIES = {CATEGORY_EXECUTION, CATEGORY_TASK_OUTPUT}
UNCLASSIFIED = "unclassified"
DEFAULT_PRODUCES = {
    "summary_md": "markdown",
    "screenshot": "image",
    "ui_tree": "xml",
    "links": "json",
}
#: 每次运行都会重写的报告类产出物：源文件 → (产物名, 类型, node_id)。
#: 名称保持稳定，重复登记时按名称覆盖而不堆叠。
RUN_OUTPUT_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("report.html", "report.html", "text", "report"),
    ("report.json", "report.json", "json", "report"),
    ("junit.xml", "junit.xml", "xml", "report"),
    ("plan.json", "plan.json", "json", "plan"),
)
#: 原子输出键与 produces 名字不一致时的映射（不改变原子语义）
PRODUCES_ALIASES = {
    "web_screenshot": {"screenshot": ("path",)},
    # ``xml`` 是给报告/UI 的限长回显；``xml_full`` 才是落盘用的完整 XML。
    "android_ui_dump": {"ui_tree": ("xml_full", "xml")},
}
_EXT_BY_TYPE = {
    "markdown": ".md",
    "json": ".json",
    "xml": ".xml",
    "text": ".txt",
    "file": ".bin",
}
_MIME_BY_TYPE = {
    "markdown": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "xml": "application/xml; charset=utf-8",
    "text": "text/plain; charset=utf-8",
    "file": "application/octet-stream",
}
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "gone": 410,
    "internal_error": 500,
}

_LOCK = threading.RLock()
_REGISTRY: Any = None


class ArtifactError(Exception):
    """带错误码的产出物异常（由 ``api.py`` 统一渲染）。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _STATUS_BY_CODE.get(code, 500)


class _ArtifactSourceMissing(Exception):
    """声明为文件的输出指向不存在的路径（收集时转成告警，不产生伪产物）。"""


# --------------------------------------------------------------------------- #
# 路径与原子
# --------------------------------------------------------------------------- #
def artifacts_root(root: Path) -> Path:
    return Path(root) / STATE_DIRNAME / ARTIFACTS_DIRNAME


def shares_root(root: Path) -> Path:
    return Path(root) / STATE_DIRNAME / SHARES_DIRNAME


def _registry() -> Any:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = default_registry()
    return _REGISTRY


def _looks_like_traversal(text: str) -> bool:
    return any(token in text for token in ("..", "/", "\\", "\x00"))


def normalize_project(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text in {"unclassified", "none", "_"}:
        return UNCLASSIFIED
    if _looks_like_traversal(text):
        raise ArtifactError("not_found", "产出物不存在")
    if not PROJECT_RE.match(text):
        # 项目过滤不匹配一律 404：既不暴露「存在但无权」，也不让非法 id 变成 400。
        raise ArtifactError("not_found", "产出物不存在")
    return text


def validate_run_id(value: Any) -> str:
    if not isinstance(value, str) or not RUN_RE.match(value):
        if isinstance(value, str) and _looks_like_traversal(value):
            raise ArtifactError("not_found", "产出物不存在")
        raise ArtifactError("bad_request", "run_id 非法")
    return value


def validate_aid(value: Any) -> str:
    if not isinstance(value, str) or not AID_RE.match(value):
        raise ArtifactError("not_found", "产出物不存在")
    return value


def _ensure_dir(directory: Path) -> None:
    """创建缺失目录并把**新目录**收紧到 0700；已存在的目录不改权限。

    不重置已存在目录的 mode：A7 需要真实注入「目录不可写」，运维也可能有意
    收紧/放宽权限；这里只保证新建目录是 0700。
    """
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    directory.mkdir(parents=True, exist_ok=True)
    for path in missing:
        with contextlib.suppress(OSError):
            path.chmod(DIR_MODE)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _run_dir(root: Path, project: str, run_id: str) -> Path:
    return artifacts_root(root) / project / validate_run_id(run_id)


def _manifest_path(root: Path, project: str, run_id: str) -> Path:
    return _run_dir(root, project, run_id) / MANIFEST_NAME


def _empty_manifest(project: str, run_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "run_id": run_id,
        "project_id": None if project == UNCLASSIFIED else project,
        "created_at": time.time(),
        "artifacts": [],
    }


def _load_manifest(path: Path, project: str, run_id: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        return _empty_manifest(project, run_id)
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
        return _empty_manifest(project, run_id)
    return data


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    _atomic_write(
        path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    )


# --------------------------------------------------------------------------- #
# 内容/类型
# --------------------------------------------------------------------------- #
def _workspace_file(root: Path, value: str) -> Path | None:
    """把原子返回的路径解析成工作区内的真实文件。

    设备/Web 原子返回的是相对 ``.panguverse`` 的路径（如
    ``artifacts/web/a.png``），而收集器的 ``root`` 是工作区根目录。这里同时
    尝试工作区根、``.panguverse`` 与 artifacts 根三种基准，且只接受解析后仍
    位于工作区内的普通文件。
    """
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text or len(text) > 4096:
        return None
    try:
        root_abs = Path(root).resolve()
    except (OSError, RuntimeError):
        return None
    candidate_text = Path(text)
    if candidate_text.is_absolute():
        candidates = [candidate_text]
    else:
        candidates = [
            root_abs / text,
            root_abs / STATE_DIRNAME / text,
            artifacts_root(root_abs) / text,
        ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            resolved.relative_to(root_abs)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _looks_like_source_path(value: str, declared_type: str | None = None) -> bool:
    """判断字符串是否是「文件路径」而不是内容本身。

    对 ``screenshot``（声明为 image）来说，原子约定输出路径；因此非空的单行
    字符串都按路径处理，解析失败就告警而不是把路径字符串当图片内容落盘。
    """
    text = str(value or "").strip()
    if "\n" in text or "\r" in text or len(text) > 4096:
        return False
    if text.startswith("data:"):
        return False
    if declared_type == "image":
        return True
    if text.startswith(("artifacts/", f"{STATE_DIRNAME}/")):
        return True
    return Path(text).suffix.lower() in _EXT_MIME


def _read_source_file(path: Path | None, value: str) -> bytes:
    if path is None:
        raise _ArtifactSourceMissing(f"路径不存在或不在工作区内: {value}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _ArtifactSourceMissing(f"路径不可读: {value}: {exc}") from exc


def _to_bytes(root: Path, value: Any, declared_type: str | None = None) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, os.PathLike):
        text = os.fspath(value)
        return _read_source_file(_workspace_file(root, text), text)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    if isinstance(value, str):
        path = _workspace_file(root, value)
        if path is not None:
            return _read_source_file(path, value)
        if _looks_like_source_path(value, declared_type):
            raise _ArtifactSourceMissing(f"路径不存在或不可读: {value}")
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def infer_type(name: str, data: bytes, declared: str | None = None) -> str:
    """按真实字节嗅探类型，``produces`` 声明只作为非图片的提示。

    图片必须由 PNG/JPEG/GIF/WebP 魔数确认；文件名后缀或 ``produces`` 声明都
    不能把一段路径字符串伪装成图片。
    """
    if _image_ext(data) is not None:
        return "image"
    if declared in KNOWN_TYPES:
        return "file" if declared == "image" else declared
    text = _decode(data)
    if text is None:
        return "file"
    stripped = text.strip()
    if stripped.startswith("<"):
        return "xml"
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except ValueError:
            pass
        else:
            return "json"
    if name in DEFAULT_PRODUCES:
        return DEFAULT_PRODUCES[name]
    if text.startswith("#") or "\n## " in text or "```" in text:
        return "markdown"
    return "text"


def _nodes_to_xml(nodes: list[Any]) -> str:
    """只有 ``nodes`` 时重建最小 UI XML，避免产物静默缺失。"""
    root = ET.Element("hierarchy")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        attrs: dict[str, str] = {}
        for key, value in node.items():
            if value is None or isinstance(value, (dict, list)):
                continue
            attrs[str(key)] = str(value)
        ET.SubElement(root, "node", attrs)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._")
    if not cleaned or cleaned.lower() == MANIFEST_NAME:
        cleaned = "artifact"
    return cleaned[:80]


def _safe_file_name(name: str | None) -> str:
    """只接受运行目录内的普通文件名（不含路径分隔符/穿越片段）。"""
    text = str(name or "").strip()
    if not text or text == MANIFEST_NAME or _looks_like_traversal(text):
        return ""
    if Path(text).name != text or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", text):
        return ""
    return text


def _extension(name: str, artifact_type: str, data: bytes) -> str:
    if artifact_type == "image":
        return _image_ext(data) or ".bin"
    return _EXT_BY_TYPE.get(artifact_type, ".bin")


def mime_for(record: dict[str, Any]) -> str:
    kind = str(record.get("type") or "file")
    filename = str(record.get("file") or "")
    ext = Path(filename).suffix.lower()
    if kind == "image" and ext in _EXT_MIME:
        return _EXT_MIME[ext]
    return _MIME_BY_TYPE.get(kind, "application/octet-stream")


# --------------------------------------------------------------------------- #
# 收集
# --------------------------------------------------------------------------- #
def _resolve_output_value(atom_name: str, produces: str, outputs: dict[str, Any]) -> Any:
    if produces in outputs:
        return outputs[produces]
    alias = PRODUCES_ALIASES.get(atom_name, {}).get(produces)
    aliases = alias if isinstance(alias, (tuple, list)) else (alias,)
    for key in aliases:
        if key and key in outputs:
            return outputs[key]
    return None


def _ui_tree_value(outputs: dict[str, Any]) -> Any:
    """``ui_tree`` 优先完整 XML；只有 nodes 时重建；绝不静默返回空。"""
    value = _resolve_output_value("android_ui_dump", "ui_tree", outputs)
    nodes = outputs.get("nodes")
    if isinstance(value, str) and value.strip() and "…（已截断）" not in value:
        return value
    if isinstance(nodes, list) and nodes:
        return _nodes_to_xml(nodes)
    return value


def _dedupe_name(manifest: dict[str, Any], base: str) -> str:
    taken = {
        str(item.get("name"))
        for item in manifest.get("artifacts") or []
        if isinstance(item, dict)
    }
    if base not in taken:
        return base
    seq = 2
    while f"{base}-{seq}" in taken:
        seq += 1
    return f"{base}-{seq}"


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def _store(
    root: Path,
    project: str,
    run_id: str,
    manifest: dict[str, Any],
    name: str,
    value: Any,
    *,
    node_id: str = "",
    step_id: str = "",
    case_id: str = "",
    declared_type: str | None = None,
    replace: bool = False,
    file_name: str | None = None,
    source_path: str | None = None,
    kind: str = "deliverable",
    category: str = CATEGORY_TASK_OUTPUT,
) -> dict[str, Any]:
    data = _to_bytes(root, value, declared_type)
    artifact_type = infer_type(name, data, declared_type)
    base = _safe_name(name)
    base_dir = artifacts_root(root)
    _ensure_dir(base_dir)
    _ensure_dir(base_dir / project)
    run_dir = _run_dir(root, project, run_id)
    _ensure_dir(run_dir)

    if replace:
        existing = next(
            (
                item
                for item in manifest.get("artifacts") or []
                if isinstance(item, dict) and item.get("name") == base
            ),
            None,
        )
    else:
        existing = None
    artifact_name = base if existing else _dedupe_name(manifest, base)
    aid = (
        str(existing.get("artifact_id"))
        if existing and AID_RE.match(str(existing.get("artifact_id") or ""))
        else "ar-" + uuid.uuid4().hex[:8]
    )
    digest = hashlib.sha256(data).hexdigest()
    truncated = len(data) > MAX_ARTIFACT_BYTES
    # 报告类产出物保留原始文件名（report.html 等），其余仍由类型决定后缀。
    filename = _safe_file_name(file_name) or (
        artifact_name + _extension(artifact_name, artifact_type, data)
    )
    if not truncated:
        _atomic_write(run_dir / filename, data)
    record = {
        "artifact_id": aid,
        "run_id": run_id,
        "project_id": None if project == UNCLASSIFIED else project,
        "name": artifact_name,
        "type": artifact_type,
        "bytes": len(data),
        "sha256": digest,
        "node_id": node_id,
        "step_id": step_id,
        "case_id": case_id,
        "file": "" if truncated else filename,
        "truncated": truncated,
        "created_at": time.time(),
        "category": (
            CATEGORY_EXECUTION if category == CATEGORY_EXECUTION else CATEGORY_TASK_OUTPUT
        ),
        "kind": "report" if kind == "report" else "deliverable",
        "path": source_path or filename,
        "_dir": str(run_dir),
    }
    if existing is not None:
        manifest["artifacts"] = [
            item for item in manifest["artifacts"] if item is not existing
        ]
    else:
        manifest.setdefault("artifacts", [])
    manifest["artifacts"].append(record)
    _write_manifest(run_dir / MANIFEST_NAME, manifest)
    return _public_record(record)


def _relative_output(value: Any) -> str | None:
    text = str(value or "").replace("\\", "/").strip()
    if "\n" in text or "\r" in text or len(text) > 4096:
        return None
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or ".." in Path(text).parts:
        return None
    return text


def _locate_absolute(root: Path, workspace: Path, value: str) -> Path | None:
    """解析绝对产物路径，但只允许落在受信根目录内（防目录穿越）。

    原子返回绝对路径是合理约定（如 ``news_data.json`` 的绝对路径），
    不能因为「不是相对路径」就判定源文件缺失。
    """
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        target = candidate.resolve()
    except OSError:
        return None
    for base in (workspace, root, Path(root) / STATE_DIRNAME, artifacts_root(root)):
        try:
            target.relative_to(Path(base).resolve())
        except (OSError, ValueError):
            continue
        return target if target.is_file() else None
    return None


def _run_workspace(root: Path, run_id: str) -> Path:
    """本次运行的持久工作目录：``<runs>/<run_id>/workspace``。"""
    return _runs_root(root) / run_id / "workspace"


def _workspace_output(base: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    try:
        base_abs = Path(base).resolve()
        target = (base_abs / relative).resolve()
        target.relative_to(base_abs)
        return target if target.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _locate_output(root: Path, workspace: Path, relative: str | None) -> Path | None:
    """相对产物路径的解析顺序：run workspace → 工作区根 → ``.panguverse``。

    旧原子把路径写成相对 ``.panguverse``（如 ``artifacts/web/a.png``），新原子
    写在 run workspace；两套布局都要能读到。
    """
    for base in (workspace, root, Path(root) / STATE_DIRNAME, artifacts_root(root)):
        found = _workspace_output(base, relative)
        if found is not None:
            return found
    return None


def _produce_specs(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    """节点 produces → ``[(key, {"path", "type"})]``（兼容字符串简写）。"""
    if not isinstance(raw, Mapping):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for key, value in raw.items():
        spec: dict[str, Any] = {"path": None, "type": None}
        if isinstance(value, Mapping):
            spec["path"] = _relative_output(value.get("path") or value.get("file"))
            spec["type"] = str(value.get("type") or "").strip() or None
        elif isinstance(value, str):
            spec["path"] = _relative_output(value)
        out.append((str(key), spec))
    return out


def _looks_like_file_ref(
    value: str, declared_type: str | None, declared_path: str | None
) -> bool:
    if declared_path:
        return True
    if declared_type == "image":
        return True
    return Path(str(value or "")).suffix.lower() in {
        ".json",
        ".html",
        ".htm",
        ".md",
        ".xml",
        ".txt",
        ".csv",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
    }


def collect_run(
    root: Path,
    run: Any,
    *,
    project_id: str | None = None,
    workspace: str | Path | None = None,
    registry: Any = None,
) -> dict[str, Any]:
    """收集一次运行的任务交付物（失败只返回 warnings，绝不影响运行结论）。

    解析顺序（从强到弱）：

    1. **节点 produces**（固化时写入的 key → 相对路径/类型）；
    2. 原子 ``Atom.produces``（旧原子/旧工作流的语义声明）。

    原子解析用该 workspace 的 registry（``.panguverse/packs`` 里的新原子），
    不会因为全局 registry 缺新原子而整段跳过；相对路径以本次运行 workspace 为根。
    """
    meta = getattr(run, "meta", None)
    meta = meta if isinstance(meta, dict) else {}
    raw_project = project_id or meta.get("project_id")
    try:
        project = normalize_project(raw_project) or UNCLASSIFIED
    except ArtifactError:
        project = UNCLASSIFIED
    run_id = validate_run_id(str(getattr(run, "run_id", "") or ""))
    warnings: list[str] = []
    collected: list[dict[str, Any]] = []
    base_workspace = Path(workspace) if workspace is not None else _run_workspace(root, run_id)
    if not base_workspace.is_dir():
        base_workspace = Path(root)

    active_registry = registry
    if active_registry is None:
        try:
            from ..core.workspace_packs import workspace_registry

            active_registry = workspace_registry(root, _registry())
        except Exception:  # noqa: BLE001 - registry 失败时退回全局库
            active_registry = _registry()

    manifest_path = _manifest_path(root, project, run_id)
    try:
        with _LOCK:
            manifest = _load_manifest(manifest_path, project, run_id)
    except OSError as exc:
        return {
            "count": 0,
            "artifacts": [],
            "warnings": [f"读取 manifest 失败: {exc}"],
        }

    for case in getattr(run, "cases", None) or []:
        case_id = str(getattr(case, "case_id", "") or "")
        for step in getattr(case, "steps", None) or []:
            status = str(getattr(step, "status", "") or "")
            if status in ("failed", "error", "skipped"):
                continue
            atom_name = str(getattr(step, "atom", "") or "")
            outputs = getattr(step, "outputs", None)
            outputs = outputs if isinstance(outputs, dict) else {}
            step_id = str(getattr(step, "step_id", "") or "")

            produces = getattr(step, "produces", None)
            if not isinstance(produces, Mapping) or not produces:
                extra = getattr(step, "extra", None)
                produces = extra.get("produces") if isinstance(extra, Mapping) else None

            specs = _produce_specs(produces)
            from_node = bool(specs)
            atom = None
            if not specs:
                try:
                    atom = active_registry.get(atom_name)
                except Exception:  # noqa: BLE001 - 未注册原子不阻断收集
                    warnings.append(f"atom_not_found: {atom_name}（无法解析 produces）")
                    continue
                specs = [
                    (str(name), {"path": None, "type": DEFAULT_PRODUCES.get(str(name))})
                    for name in (getattr(atom, "produces", ()) or ())
                ]
            if not specs:
                continue

            for name, spec in specs:
                declared_type = spec.get("type")
                declared_path = spec.get("path")
                if atom_name == "android_ui_dump" and name == "ui_tree":
                    raw_value = _ui_tree_value(outputs)
                else:
                    raw_value = _resolve_output_value(atom_name, name, outputs)

                path_text = None
                if isinstance(raw_value, str):
                    candidate = _relative_output(raw_value)
                    if candidate and _locate_output(root, base_workspace, candidate):
                        path_text = candidate
                    elif candidate is None:
                        absolute = _locate_absolute(root, base_workspace, raw_value)
                        if absolute is not None:
                            path_text = str(absolute)
                if path_text is None and declared_path and _locate_output(
                    root, base_workspace, declared_path
                ):
                    path_text = declared_path

                if path_text is not None:
                    source = _locate_output(root, base_workspace, path_text) or _locate_absolute(
                        root, base_workspace, path_text
                    )
                    try:
                        data = source.read_bytes() if source is not None else b""
                    except OSError as exc:
                        warnings.append(f"deliverable_unreadable: {atom_name}.{name}: {exc}")
                        continue
                    value: Any = data
                    source_path = path_text
                else:
                    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
                        warnings.append(f"deliverable_missing: {atom_name}.{name} 没有输出值，已跳过")
                        continue
                    if isinstance(raw_value, str) and _looks_like_file_ref(
                        raw_value, declared_type, declared_path
                    ):
                        warnings.append(
                            f"artifact_source_missing: {atom_name}.{name}: "
                            f"{declared_path or raw_value}"
                        )
                        continue
                    value = raw_value
                    source_path = declared_path

                try:
                    with _LOCK:
                        manifest = _load_manifest(manifest_path, project, run_id)
                        record = _store(
                            root,
                            project,
                            run_id,
                            manifest,
                            name,
                            value,
                            node_id=step_id if from_node else atom_name,
                            step_id=step_id,
                            case_id=case_id,
                            declared_type=declared_type or DEFAULT_PRODUCES.get(name),
                            source_path=source_path,
                            kind="deliverable",
                            category=CATEGORY_TASK_OUTPUT,
                        )
                except _ArtifactSourceMissing as exc:
                    warnings.append(f"artifact_source_missing: {atom_name}.{name}: {exc}")
                    continue
                except (OSError, ArtifactError, ValueError) as exc:
                    warnings.append(f"{atom_name}.{name} 收集失败: {exc}")
                    continue
                collected.append(record)
    return {"count": len(collected), "artifacts": collected, "warnings": warnings}


def artifact_category(record: Mapping[str, Any]) -> str:
    """执行报告 vs 任务产出；旧 manifest 没有 category 时按 kind/名字回退。"""
    category = str(record.get("category") or "")
    if category in ARTIFACT_CATEGORIES:
        return category
    kind = str(record.get("kind") or "")
    if kind == "report":
        return CATEGORY_EXECUTION
    if kind == "deliverable":
        return CATEGORY_TASK_OUTPUT
    name = str(record.get("name") or "")
    return (
        CATEGORY_EXECUTION
        if name in {spec[1] for spec in RUN_OUTPUT_SPECS}
        else CATEGORY_TASK_OUTPUT
    )


def artifact_kind(record: Mapping[str, Any]) -> str:
    """兼容旧调用：执行报告 → report，任务产出 → deliverable。"""
    return (
        "report"
        if artifact_category(record) == CATEGORY_EXECUTION
        else "deliverable"
    )


def record_insight(
    root: Path,
    run_id: str,
    summary_md: Any,
    *,
    project_id: str | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """报告级 AI 总结 → ``ai_summary`` 产出物（同 run 覆盖更新，不重复堆叠）。"""
    if not isinstance(summary_md, str) or not summary_md.strip():
        return None
    meta = (report or {}).get("meta") if isinstance(report, dict) else None
    project_raw = project_id or (meta.get("project_id") if isinstance(meta, dict) else None)
    try:
        project = normalize_project(project_raw) or UNCLASSIFIED
        validated = validate_run_id(run_id)
        manifest_path = _manifest_path(root, project, validated)
        with _LOCK:
            manifest = _load_manifest(manifest_path, project, validated)
            record = _store(
                root,
                project,
                validated,
                manifest,
                "ai_summary",
                summary_md,
                node_id="insight",
                declared_type="markdown",
                replace=True,
                category=CATEGORY_TASK_OUTPUT,
            )
        return record
    except (OSError, ArtifactError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 运行报告类产出物（每次运行自动登记）
# --------------------------------------------------------------------------- #
def _runs_root(root: Path) -> Path:
    """与 service.runs_root 同规则：优先 ``<root>/runs``，兼容旧布局。"""
    primary = Path(root) / "runs"
    return primary if primary.is_dir() else Path(root) / STATE_DIRNAME / "runs"


def _report_meta(run_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    meta = data.get("meta") if isinstance(data, dict) else None
    return meta if isinstance(meta, dict) else {}


def record_run_outputs(
    root: Path,
    run_id: str,
    *,
    project_id: str | None = None,
    run_dir: str | Path | None = None,
) -> dict[str, Any]:
    """把一次运行的报告文件登记为产出物（按名称覆盖，失败只返回 warnings）。

    只扫描固定白名单（report.html / report.json / junit.xml / plan.json），
    运行目录里的轨迹、临时文件和回归子目录不会被登记。
    """
    validated = validate_run_id(run_id)
    directory = Path(run_dir) if run_dir is not None else _runs_root(root) / validated
    if not directory.is_dir():
        return {"count": 0, "artifacts": [], "warnings": []}

    raw_project = project_id or _report_meta(directory).get("project_id")
    try:
        project = normalize_project(raw_project) or UNCLASSIFIED
    except ArtifactError:
        project = UNCLASSIFIED

    manifest_path = _manifest_path(root, project, validated)
    registered: list[dict[str, Any]] = []
    warnings: list[str] = []
    for source, name, declared_type, node_id in RUN_OUTPUT_SPECS:
        path = directory / source
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            warnings.append(f"{source} 读取失败: {exc}")
            continue
        try:
            with _LOCK:
                manifest = _load_manifest(manifest_path, project, validated)
                record = _store(
                    root,
                    project,
                    validated,
                    manifest,
                    name,
                    data,
                    node_id=node_id,
                    declared_type=declared_type,
                    replace=True,
                    file_name=source,
                    source_path=source,
                    kind="report",
                    category=CATEGORY_EXECUTION,
                )
        except (OSError, ArtifactError, ValueError) as exc:
            warnings.append(f"{source} 登记失败: {exc}")
            continue
        registered.append(record)
    return {"count": len(registered), "artifacts": registered, "warnings": warnings}


def _run_manifest_paths(root: Path, run_id: str) -> list[Path]:
    try:
        return sorted(artifacts_root(root).glob(f"*/{run_id}/{MANIFEST_NAME}"))
    except OSError:
        return []


def sync_report_artifact_groups(
    run_dir: str | Path, task_outputs: list[dict[str, Any]] | None = None
) -> bool:
    """把运行目录 report.json 的 artifacts 分为 execution / task_outputs 两组。

    只改报告视图，不改变运行结论；文件不存在或格式不对时静默返回 False。
    """
    directory = Path(run_dir)
    report_path = directory / "report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(report, dict):
        return False
    execution = {
        source: source
        for source in ("report.html", "report.json", "junit.xml", "plan.json")
        if (directory / source).is_file()
    }
    outputs = {
        str(item.get("name")): str(item.get("path") or item.get("file"))
        for item in (task_outputs or [])
        if isinstance(item, dict) and item.get("name") and (item.get("path") or item.get("file"))
    }
    report["artifacts"] = {"execution": execution, "task_outputs": outputs}
    try:
        _atomic_write(
            report_path,
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except OSError:
        return False
    return True


def ensure_run_artifacts(
    root: Path,
    run_id: str,
    *,
    project_id: str | None = None,
    run_dir: str | Path | None = None,
) -> dict[str, Any]:
    """历史 run 惰性回填：无 manifest 且运行目录存在时才登记报告文件。

    不伪造不存在的 run：运行目录缺失或没有任何白名单文件时只返回空结果。
    """
    validated = validate_run_id(run_id)
    empty = {"count": 0, "artifacts": [], "warnings": [], "created": False}
    if _run_manifest_paths(root, validated):
        return empty
    directory = Path(run_dir) if run_dir is not None else _runs_root(root) / validated
    if not directory.is_dir():
        return empty
    result = record_run_outputs(
        root, validated, project_id=project_id, run_dir=directory
    )
    return {**result, "created": bool(result.get("count"))}


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _iter_records(root: Path) -> list[dict[str, Any]]:
    base = artifacts_root(root)
    if not base.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        manifests = sorted(base.glob(f"*/*/{MANIFEST_NAME}"))
    except OSError:
        return []
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("artifacts") or []:
            if not isinstance(item, dict):
                continue
            if not AID_RE.match(str(item.get("artifact_id") or "")):
                continue
            record = dict(item)
            record.setdefault("project_id", data.get("project_id"))
            record["category"] = artifact_category(record)
            record.setdefault(
                "download_url",
                f"/api/artifacts/{record.get('artifact_id')}/download",
            )
            record["_dir"] = str(manifest_path.parent)
            records.append(record)
    return records


def list_artifacts(
    root: Path,
    *,
    project: str | None = None,
    run: str | None = None,
    type: str | None = None,
    category: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    project_filter = normalize_project(project)
    try:
        run_filter = validate_run_id(run) if run else None
    except ArtifactError as exc:
        # 非法 run 过滤同样按「不存在」处理，避免把 400 当成存在性探针。
        raise ArtifactError("not_found", "产出物不存在") from exc
    type_filter = str(type).strip() if type else None
    if type_filter and type_filter not in KNOWN_TYPES:
        raise ArtifactError("bad_request", f"type 非法: {type_filter}")
    category_filter = str(category).strip() if category else None
    if category_filter and category_filter not in ARTIFACT_CATEGORIES:
        raise ArtifactError("bad_request", f"category 非法: {category_filter}")
    records = _iter_records(root)
    if run_filter is not None:
        # 历史 run 可能只有 runs/<id>/ 而没有产出物 manifest：首次查询时惰性回填。
        if not any(record.get("run_id") == run_filter for record in records) and not _run_manifest_paths(
            root, run_filter
        ):
            with contextlib.suppress(Exception):  # 回填失败不应让列表接口 500
                ensure_run_artifacts(root, run_filter)
            records = _iter_records(root)
    items = []
    for record in records:
        if project_filter is not None:
            actual = record.get("project_id") or UNCLASSIFIED
            if actual != project_filter:
                continue
        if run_filter is not None and record.get("run_id") != run_filter:
            continue
        if type_filter is not None and record.get("type") != type_filter:
            continue
        if category_filter is not None and record.get("category") != category_filter:
            continue
        items.append(record)
    items.sort(key=lambda item: (-float(item.get("created_at") or 0), str(item.get("artifact_id"))))
    total = len(items)
    page = items[offset : offset + limit]
    return {"total": total, "items": [_public_record(item) for item in page]}


def find_artifact(
    root: Path, aid: str, *, project: str | None = None
) -> dict[str, Any]:
    validate_aid(aid)
    project_filter = normalize_project(project)
    for record in _iter_records(root):
        if record.get("artifact_id") != aid:
            continue
        if project_filter is not None:
            actual = record.get("project_id") or UNCLASSIFIED
            if actual != project_filter:
                raise ArtifactError("not_found", "产出物不存在")
        return record
    raise ArtifactError("not_found", "产出物不存在")


def _record_path(record: dict[str, Any]) -> Path:
    directory = Path(str(record.get("_dir") or ""))
    filename = str(record.get("file") or "")
    if not filename:
        raise ArtifactError("conflict", "产出物超过 20MB，只保留了元信息", status=409)
    path = (directory / filename).resolve()
    try:
        path.relative_to(directory.resolve())
    except ValueError as exc:
        raise ArtifactError("not_found", "产出物不存在") from exc
    if path.name == MANIFEST_NAME or not path.is_file():
        raise ArtifactError("not_found", "产出物不存在")
    return path


def artifact_bytes(root: Path, record: dict[str, Any]) -> bytes:
    return _record_path(record).read_bytes()


def get_artifact(
    root: Path, aid: str, *, project: str | None = None
) -> dict[str, Any]:
    record = find_artifact(root, aid, project=project)
    payload = _public_record(record)
    payload["download_url"] = f"/api/artifacts/{aid}/download"
    content = None
    if not record.get("truncated") and record.get("type") in TEXT_TYPES:
        try:
            data = artifact_bytes(root, record)
        except (OSError, ArtifactError):
            data = b""
        if 0 < len(data) <= MAX_INLINE_BYTES:
            content = _decode(data)
    payload["content"] = content
    return payload


def download_artifact(root: Path, aid: str, *, project: str | None = None) -> tuple[bytes, str, str]:
    record = find_artifact(root, aid, project=project)
    data = artifact_bytes(root, record)
    return data, str(record.get("file") or "artifact"), mime_for(record)


# --------------------------------------------------------------------------- #
# 分享
# --------------------------------------------------------------------------- #
def _share_path(root: Path, token: str) -> Path:
    if not TOKEN_RE.match(token or ""):
        raise ArtifactError("not_found", "分享不存在")
    return shares_root(root) / f"{token}.json"


def _ttl(value: Any) -> int:
    if value is None:
        return DEFAULT_SHARE_TTL
    if isinstance(value, bool):
        raise ArtifactError("bad_request", "expires_in 非法")
    if isinstance(value, (int, float)):
        seconds = int(value)
        if seconds <= 0 or seconds > MAX_SHARE_TTL:
            raise ArtifactError("bad_request", "expires_in 必须在 1 秒到 30 天之间")
        return seconds
    text = str(value).strip()
    if text in SHARE_TTLS:
        return SHARE_TTLS[text]
    raise ArtifactError("bad_request", "expires_in 只能是 1h/1d/7d/30d")


def create_share(root: Path, aid: str, payload: Any = None) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    record = find_artifact(root, aid)
    seconds = _ttl(data.get("expires_in", data.get("ttl")))
    now = time.time()
    token = secrets.token_urlsafe(24)
    share = {
        "token": token,
        "artifact_id": aid,
        "run_id": record.get("run_id"),
        "project_id": record.get("project_id"),
        "created_at": now,
        "expires_at": now + seconds,
        "revoked": False,
    }
    directory = shares_root(root)
    _ensure_dir(directory)
    _atomic_write(
        _share_path(root, token),
        json.dumps(share, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return {"url_token": token, "expires_at": share["expires_at"]}


def resolve_share(root: Path, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验分享 token：不存在/已撤销 404，过期 410。返回 (share, artifact)。"""
    path = _share_path(root, token)
    try:
        share = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError("not_found", "分享不存在") from exc
    if not isinstance(share, dict):
        raise ArtifactError("not_found", "分享不存在")
    if share.get("revoked") or share.get("token") != token:
        raise ArtifactError("not_found", "分享不存在")
    if float(share.get("expires_at") or 0) <= time.time():
        raise ArtifactError("gone", "分享已过期", status=410)
    record = find_artifact(root, str(share.get("artifact_id") or ""))
    return share, record


def revoke_all_shares(root: Path, aid: str) -> int:
    """撤销某个产出物的全部分享（UI 的 DELETE /share 不带 token）。"""
    validate_aid(aid)
    directory = shares_root(root)
    if not directory.is_dir():
        return 0
    revoked = 0
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return 0
    for path in files:
        try:
            share = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(share, dict) or share.get("artifact_id") != aid:
            continue
        if share.get("revoked"):
            continue
        share["revoked"] = True
        share["revoked_at"] = time.time()
        _atomic_write(path, json.dumps(share, ensure_ascii=False, indent=2).encode("utf-8"))
        revoked += 1
    return revoked


def revoke_share(root: Path, aid: str, token: str) -> None:
    validate_aid(aid)
    path = _share_path(root, token)
    try:
        share = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError("not_found", "分享不存在") from exc
    if not isinstance(share, dict) or share.get("artifact_id") != aid:
        raise ArtifactError("not_found", "分享不存在")
    share["revoked"] = True
    share["revoked_at"] = time.time()
    _atomic_write(path, json.dumps(share, ensure_ascii=False, indent=2).encode("utf-8"))


def share_preview(root: Path, token: str) -> tuple[bytes, str]:
    share, record = resolve_share(root, token)
    kind = str(record.get("type") or "file")
    filename = html.escape(str(record.get("name") or ""))
    if record.get("truncated"):
        body = "<p>该产出物超过 20MB，仅保留元信息。</p>"
    else:
        try:
            data = artifact_bytes(root, record)
        except (OSError, ArtifactError):
            data = b""
        if kind == "image":
            body = (
                f'<img alt="{filename}" src="/api/share/{quote(token)}/raw">'
            )
        else:
            text = _decode(data) or ""
            body = (
                _markdown_html(text)
                if kind == "markdown"
                else f"<pre>{html.escape(text)}</pre>"
            )
    page = _page_html(
        title=str(record.get("name") or "产出物"),
        run_id=str(record.get("run_id") or ""),
        kind=kind,
        bytes_=int(record.get("bytes") or 0),
        sha256=str(record.get("sha256") or ""),
        expires_at=float(share.get("expires_at") or 0),
        body=body,
    )
    return page.encode("utf-8"), share_preview_content_type()


def share_raw(root: Path, token: str) -> tuple[bytes, str, str]:
    _, record = resolve_share(root, token)
    return artifact_bytes(root, record), str(record.get("file") or "artifact"), mime_for(record)


def share_preview_content_type() -> str:
    return "text/html; charset=utf-8"


def _page_html(
    *,
    title: str,
    run_id: str,
    kind: str,
    bytes_: int,
    sha256: str,
    expires_at: float,
    body: str,
) -> str:
    expires = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · PanguVerse</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
header{{padding:16px 24px;border-bottom:1px solid #23262d;background:#151821}}
header h1{{font-size:16px;margin:0 0 6px}}
header p{{margin:2px 0;color:#8b93a1;font-size:12px}}
main{{padding:24px;max-width:960px;margin:0 auto;line-height:1.65}}
pre{{background:#151821;border:1px solid #23262d;border-radius:8px;padding:16px;overflow:auto;white-space:pre-wrap}}
img{{max-width:100%;height:auto;border-radius:8px}}
code{{background:#151821;padding:1px 5px;border-radius:4px}}
a{{color:#7fb2ff}}
</style></head>
<body><header><h1>{html.escape(title)}</h1>
<p>run {html.escape(run_id)} · {html.escape(kind)} · {bytes_} bytes</p>
<p>sha256 {html.escape(sha256)}</p>
<p>分享有效期至 {html.escape(expires)}（只读）</p>
</header><main>{body}</main></body></html>"""


def _inline_markdown(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def _markdown_html(text: str) -> str:
    """极简安全 Markdown → HTML：先转义，再只识别标题/列表/代码块/粗体/行内代码。"""
    lines = html.escape(text).splitlines()
    out: list[str] = []
    in_code = False
    in_list = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_markdown(bullet.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            continue
        out.append(f"<p>{_inline_markdown(line)}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


__all__ = [
    "DEFAULT_PRODUCES",
    "KNOWN_TYPES",
    "MAX_ARTIFACT_BYTES",
    "MAX_INLINE_BYTES",
    "RUN_OUTPUT_SPECS",
    "ArtifactError",
    "artifact_bytes",
    "artifacts_root",
    "collect_run",
    "create_share",
    "download_artifact",
    "ensure_run_artifacts",
    "find_artifact",
    "get_artifact",
    "infer_type",
    "list_artifacts",
    "mime_for",
    "normalize_project",
    "record_insight",
    "record_run_outputs",
    "resolve_share",
    "revoke_all_shares",
    "revoke_share",
    "share_preview",
    "share_raw",
    "shares_root",
    "validate_aid",
]
