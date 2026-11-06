"""Exploration agent executor for the explore-first loop.

The agent explores a task with a *real* LLM decision loop: it may call existing
registry atoms, run Python scripts inside the explore session sandbox, and must
finish by proving success with deterministic checks.  There is intentionally no
rule-based fallback: without an LLM the caller gets ``llm_required`` instead of
a fabricated execution.

This module never touches the session trace format directly: callers pass an
``emit(kind, payload)`` callback (see :mod:`panguverse.web.explore`), which keeps
the sandbox executable in isolation and easy to unit test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.context import MISSING, RunContext, split_path, walk_path

MAX_SCRIPT_STDOUT = 20_000
MAX_SCRIPT_STDERR = 4_000
MAX_ARTIFACT_BYTES = 200_000
MAX_ARTIFACT_TEXT = 5_000
DEFAULT_SCRIPT_TIMEOUT = 60.0
MAX_SCRIPT_TIMEOUT = 120.0
MAX_THOUGHT = 2_000
MAX_EVIDENCE = 500
MAX_PROMPT_OBSERVATION = 1_200
MAX_PROMPT_OBSERVATIONS = 6
MAX_PROMPT_SLIM_OBSERVATIONS = 3
MAX_PROMPT_SLIM_OBSERVATION = 800
MAX_PROMPT_BYTES = 8 * 1024

_FULL_STEP_REF_RE = re.compile(r"^\$\{step(\d+)\.([^}]+)\}$")
_FULL_LAST_REF_RE = re.compile(r"^\$\{last\.([^}]+)\}$")
_ARTIFACT_SEQ_RE = re.compile(r"^artifacts/(\d{3,})_")

CHECK_TYPES = ("text_contains", "status_code", "file_exists", "regex")
_TYPE_ALIASES = {
    "contains": "text_contains",
    "text": "text_contains",
    "status": "status_code",
    "file": "file_exists",
    "file_exists": "file_exists",
    "regex_match": "regex",
}


class AgentExecError(Exception):
    """Executable-layer error carrying the unified API error code/status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or {
            "bad_request": 400,
            "not_found": 404,
            "not_general": 422,
        }.get(code, 500)
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# 工具目录
# --------------------------------------------------------------------------- #
def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


def _llm_failure_message(message: Any) -> str:
    """LLM 失败原因只保留一层前缀，避免 ``LLMError: LLM 调用失败`` 套娃。"""
    text = str(message or "").strip()
    for prefix in ("LLM 调用失败：", "LLM 调用失败:", "LLMError:"):
        while text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return f"LLM 调用失败：{text or '未知错误'}"


def _safe_tool_fragment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:64] or "tool"


def tool_entry_from_atom(atom: Any) -> dict[str, Any]:
    try:
        ports = [port.to_dict() for port in atom.input_ports()]
    except Exception:  # noqa: BLE001 - 测试替身可能没有契约视图
        ports = []
    return {
        "name": str(getattr(atom, "name", "")),
        "kind": "atom",
        "description": _clip(str(getattr(atom, "description", "") or ""), 240),
        "inputs": dict(getattr(atom, "inputs", {}) or {}),
        "input_ports": ports,
        "required": list(getattr(atom, "required", ()) or ()),
        "effects": [str(item) for item in (getattr(atom, "effects", ()) or ())],
        "domain": str(getattr(atom, "domain", "") or ""),
        "category": str(getattr(atom, "category", "") or ""),
    }


def build_tool_catalog(
    registry: Any,
    *,
    allow_scripts: bool = True,
    allowed_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Tool catalog = registry atoms + built-in exploration tools.

    ``allowed_names`` 非空时只暴露这些原子（用于按意图/领域收窄）。
    """
    catalog: list[dict[str, Any]] = []
    try:
        atoms = list(registry.list())
    except AttributeError:
        atoms = list(registry or [])
    for atom in atoms:
        entry = tool_entry_from_atom(atom)
        if not entry["name"]:
            continue
        if allowed_names is not None and entry["name"] not in allowed_names:
            continue
        catalog.append(entry)
    catalog.append(
        {
            "name": "artifact_read",
            "kind": "builtin",
            "description": "读取探索目录内的产物片段（只读，路径必须在会话目录内）",
            "inputs": {"path": "string", "max_chars?": "integer"},
            "required": ["path"],
            "effects": ["read"],
        }
    )
    if allow_scripts:
        catalog.append(
            {
                "name": "script_run",
                "kind": "builtin",
                "description": "在探索目录内用 python -I 执行脚本（stdout/stderr 截断）",
                "inputs": {"path": "string", "timeout?": "number"},
                "required": ["path"],
                "effects": ["write"],
            }
        )
    return catalog


WEB_ROUTES = frozenset({"web_browser", "web_research"})
API_ROUTES = frozenset({"api_testing", "api"})
DEVICE_ROUTES = frozenset({"android_adb", "ios_sim"})
CORE_DOMAINS = frozenset({"core"})
FS_DOMAINS = frozenset({"filesystem"})


def route_analysis(intent: str, domain_pack: str = "") -> Any:
    """用 planner_gate 的领域路由（尊重显式 domain_pack）分析意图。"""
    try:
        from ..web.planner_gate import analyze_intent

        return analyze_intent(str(intent or ""), domain_pack=str(domain_pack or ""))
    except Exception:  # noqa: BLE001 - 路由失败时退回「非设备域」保守目录
        return None


def _route_atom_domains(analysis: Any) -> set[str] | None:
    route = str(getattr(analysis, "domain", "") or "")
    if route in DEVICE_ROUTES:
        return {route}
    if route in API_ROUTES:
        return {"api"}
    if route in WEB_ROUTES:
        return {"web_browser", "web_research"}
    if route in CORE_DOMAINS | FS_DOMAINS:
        return {route}
    if route:
        return {route}
    return None


def allowed_atom_names(registry: Any, analysis: Any) -> set[str] | None:
    """按路由域收窄原子：路由域 + core + local_fs；设备域仅设备意图可见。"""
    domains = _route_atom_domains(analysis)
    names: set[str] = set()
    for atom in list(getattr(registry, "list", lambda: [])() or []):
        name = str(getattr(atom, "name", "") or "")
        domain = str(getattr(atom, "domain", "") or "")
        if not name:
            continue
        if domains is None:
            # 没有明确路由：只排除设备域，避免网页任务误用 android/ios。
            if domain in DEVICE_ROUTES:
                continue
            names.add(name)
            continue
        if domain in domains or domain in CORE_DOMAINS or domain in FS_DOMAINS:
            names.add(name)
        # 网页任务需要通用 HTTP 能力；只放行 http_request，不暴露 api_testing 的断言/提取原子。
        elif domains & WEB_ROUTES and name == "http_request":
            names.add(name)
    return names


def tool_catalog_for_intent(
    registry: Any,
    intent: str,
    *,
    domain_pack: str = "",
    allow_scripts: bool = True,
) -> list[dict[str, Any]]:
    analysis = route_analysis(intent, domain_pack)
    return build_tool_catalog(
        registry,
        allow_scripts=allow_scripts,
        allowed_names=allowed_atom_names(registry, analysis),
    )


# --------------------------------------------------------------------------- #
# 校验（确定性）
# --------------------------------------------------------------------------- #
def normalize_checks(raw: Any) -> list[dict[str, Any]]:
    """Normalize user/LLM success criteria into executable check specs."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise AgentExecError("bad_request", "success_criteria 必须是检查列表")
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise AgentExecError(
                "bad_request", f"检查 #{index + 1} 必须是对象（含 type/参数）"
            )
        data = dict(item)
        kind = str(data.get("type") or "").strip()
        kind = _TYPE_ALIASES.get(kind, kind)
        if kind not in CHECK_TYPES:
            raise AgentExecError(
                "bad_request",
                f"检查 #{index + 1} 类型不支持: {kind or '<空>'}；"
                f"可用 {', '.join(CHECK_TYPES)}",
            )
        spec: dict[str, Any] = {
            "id": str(data.get("id") or f"check-{index + 1}"),
            "type": kind,
        }
        if kind == "text_contains":
            raw_value = data.get("value")
            for alias in ("text", "contains", "needle", "expected", "match"):
                if not raw_value and data.get(alias) not in (None, ""):
                    raw_value = data.get(alias)
                    break
            spec["value"] = str(raw_value or "")
            if not spec["value"]:
                raise AgentExecError("bad_request", "text_contains 缺少 value")
        elif kind == "regex":
            spec["pattern"] = str(data.get("pattern") or "")
            if not spec["pattern"]:
                raise AgentExecError("bad_request", "regex 缺少 pattern")
            try:
                re.compile(spec["pattern"])
            except re.error as exc:
                raise AgentExecError("bad_request", f"正则表达式非法: {exc}") from exc
        elif kind == "status_code":
            try:
                spec["equals"] = int(data.get("equals", 200))
            except (TypeError, ValueError) as exc:
                raise AgentExecError("bad_request", "status_code.equals 必须是整数") from exc
        elif kind == "file_exists":
            spec["path"] = str(data.get("path") or "")
            if not spec["path"]:
                raise AgentExecError("bad_request", "file_exists 缺少 path")
        for key in ("step", "step_id", "artifact"):
            if data.get(key) not in (None, ""):
                spec[key] = data[key]
        checks.append(spec)
    return checks


def checks_from_success(success: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Recover the normalized specs stored in ``success.json``."""
    specs: list[dict[str, Any]] = []
    for item in (success or {}).get("checks") or []:
        if not isinstance(item, Mapping):
            continue
        spec = item.get("spec") if isinstance(item.get("spec"), Mapping) else item
        if isinstance(spec, Mapping) and spec.get("type"):
            specs.append(dict(spec))
    return specs


def _has_step_binding(spec: Mapping[str, Any]) -> bool:
    return spec.get("step") not in (None, "") or spec.get("step_id") not in (None, "")


def _json_objects(text: str) -> list[dict[str, Any]]:
    """从可能夹带解释/多个 JSON 的 LLM 文本里提取所有顶层对象。"""
    found: list[dict[str, Any]] = []
    length = len(text)
    index = 0
    while index < length:
        start = text.find("{", index)
        if start < 0:
            break
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for pos in range(start, length):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = pos
                    break
        if end < 0:
            break
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            data = None
        if isinstance(data, dict):
            found.append(data)
        index = end + 1
    return found


def _extract_decision(text: str) -> dict[str, Any]:
    """优先取最后一个带 action 的 JSON 对象；兼容多段输出/前后解释。"""
    objects = _json_objects(text)
    for data in reversed(objects):
        if str(data.get("action") or "").strip() in {"tool", "script", "final"}:
            return data
    if objects:
        return objects[0]
    raise ValueError(f"没有可解析的 JSON 对象: {text[:200]}")


def _observation_for(spec: Mapping[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按显式 step/step_id 取观察；没有绑定则返回 None（交给默认证据选择）。"""
    step_id = spec.get("step_id")
    if step_id not in (None, ""):
        for item in observations:
            if item.get("step_id") == step_id:
                return item
        return None
    step = spec.get("step")
    if step not in (None, ""):
        try:
            index = int(step)
        except (TypeError, ValueError):
            return None
        if 1 <= index <= len(observations):
            return observations[index - 1]
        return None
    return None


def _default_status_observation(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """默认绑定：最近一次真的带 status/status_code 的观察。"""
    for item in reversed(observations):
        result = item.get("result") or {}
        if result.get("status") is not None or result.get("status_code") is not None:
            return item
    return None


def _default_text_observation(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """默认绑定：最近一次带正文类输出的观察。"""
    for item in reversed(observations):
        text = _text_of_result(item.get("result") or {})
        if isinstance(text, str) and text.strip():
            return item
    return None


def _latest_artifact_text(base_dir: Path) -> str | None:
    base = Path(base_dir)
    if not base.is_dir():
        return None
    rank = {".html": 0, ".htm": 0, ".md": 1, ".txt": 2, ".json": 3}
    candidates: list[tuple[int, int, float, Path]] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in rank:
            continue
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in {"scripts", ".panguverse", "replay"}:
            continue
        if path.name in {"session.json", "success.json"} or path.name.startswith("."):
            continue
        # Prefer rendered products in the sandbox root over artifact-wrapper
        # JSON; otherwise an unbound text check inspects metadata, not the report.
        in_artifacts = 1 if rel.parts and rel.parts[0] == "artifacts" else 0
        candidates.append(
            (rank[path.suffix.lower()], in_artifacts, -path.stat().st_mtime, path)
        )
    candidates.sort(key=lambda item: item[:3])
    for _rank, _artifact, _mtime, path in candidates:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:300_000]
        except OSError:
            continue
    return None


def _text_of_result(result: Mapping[str, Any]) -> str | None:
    for key in ("text", "body", "stdout", "content", "html"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return None


_ITEM_TAG_RE = re.compile(
    r"<li\b|<article\b|class=[\"'](?:item|card|hot-item|news-item)[\"']", re.I
)


def _count_items(text: str, pattern: str) -> int:
    if pattern:
        try:
            return len(re.findall(pattern, text))
        except re.error:
            return 0
    try:
        data = json.loads(text)
    except ValueError:
        return len(_ITEM_TAG_RE.findall(text))
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("items", "data", "results", "list", "hot", "hotList"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                for nested in ("items", "list", "content"):
                    if isinstance(value.get(nested), list):
                        return len(value[nested])
        for value in data.values():
            if isinstance(value, list):
                return len(value)
    return 0


_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y年%m月%d日",
)


def _parse_time(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is None else parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _find_timestamp(text: str, pattern: str) -> datetime | None:
    if pattern:
        try:
            match = re.search(pattern, text)
        except re.error:
            return None
        if match:
            raw = match.group(1) if match.groups() else match.group(0)
            return _parse_time(raw)
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    stack = [data] if data is not None else []
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if str(key).lower() in (
                    "fetched_at",
                    "generated_at",
                    "updated_at",
                    "created_at",
                    "timestamp",
                    "date",
                ):
                    parsed = _parse_time(value)
                    if parsed is not None:
                        return parsed
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _eval_one(
    spec: Mapping[str, Any],
    *,
    observations: list[dict[str, Any]],
    base_dir: Path,
    extra_texts: list[str] | None = None,
) -> tuple[bool, str]:
    kind = spec["type"]
    if kind == "file_exists":
        try:
            target = safe_path(base_dir, str(spec["path"]), field="path")
        except AgentExecError as exc:
            return False, exc.message
        if not target.is_file():
            return False, f"文件不存在: {spec['path']}"
        return True, f"{spec['path']} 存在（{target.stat().st_size} bytes）"

    if kind == "status_code":
        observation = (
            _observation_for(spec, observations)
            if _has_step_binding(spec)
            else _default_status_observation(observations)
        )
        if observation is None:
            return False, "没有可用的 status 证据（最近观察均无 status/status_code）"
        result = observation.get("result") or {}
        actual = result.get("status", result.get("status_code"))
        expected = int(spec.get("equals", 200))
        return (actual == expected), f"status={actual!r}，期望 {expected}"

    if kind in ("text_contains", "regex"):
        observation = (
            _observation_for(spec, observations)
            if _has_step_binding(spec)
            else _default_text_observation(observations)
        )
        text = _text_for(spec, observation, base_dir, extra_texts or [])
        if text is None:
            return False, "找不到可校验的文本来源（观察与 artifacts 均无文本）"
        if kind == "text_contains":
            needle = str(spec["value"])
            position = text.find(needle)
            if position < 0:
                return False, f"文本未包含 {needle!r}（来源长度 {len(text)}）"
            start = max(0, position - 60)
            end = min(len(text), position + len(needle) + 60)
            return True, text[start:end]
        match = re.search(str(spec["pattern"]), text)
        if match is None:
            return False, f"正则未匹配: {spec['pattern']}"
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        return True, text[start:end]

    if kind == "min_items":
        rel = str(spec.get("artifact") or spec.get("path") or "")
        try:
            target = safe_path(base_dir, rel, field="artifact")
        except AgentExecError as exc:
            return False, exc.message
        if not target.is_file():
            return False, f"产物不存在: {rel}"
        text = target.read_text(encoding="utf-8", errors="replace")
        minimum = int(spec.get("min_items") or 1)
        count = _count_items(text, str(spec.get("pattern") or ""))
        return count >= minimum, f"{rel} 条目数 {count}，要求 ≥ {minimum}"

    if kind == "freshness":
        rel = str(spec.get("artifact") or spec.get("path") or "")
        try:
            target = safe_path(base_dir, rel, field="artifact")
        except AgentExecError as exc:
            return False, exc.message
        if not target.is_file():
            return False, f"产物不存在: {rel}"
        text = target.read_text(encoding="utf-8", errors="replace")
        stamp = _find_timestamp(text, str(spec.get("pattern") or ""))
        if stamp is None:
            return False, f"{rel} 找不到可解析的时间戳"
        max_age = int(spec.get("max_age_seconds") or 86400)
        age = (datetime.now() - stamp).total_seconds()
        return age <= max_age, f"{rel} 时间戳 {stamp.isoformat(sep=' ')}，距今 {int(age)}s（上限 {max_age}s）"

    return False, f"未知检查类型: {kind}"


def _text_for(
    spec: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    base_dir: Path,
    extra_texts: list[str] | None = None,
) -> str | None:
    # An artifact binding is a deterministic file path that replay also
    # produces, so it always wins over an in-memory observation.
    artifact = spec.get("artifact")
    if artifact:
        try:
            target = safe_path(base_dir, str(artifact), field="artifact")
        except AgentExecError:
            return None
        if target.is_file():
            return target.read_text(encoding="utf-8", errors="replace")
        return None
    if _has_step_binding(spec):
        if observation is None:
            return None
        return _text_of_result(observation.get("result") or {})
    if observation is not None:
        text = _text_of_result(observation.get("result") or {})
        if text:
            return text
    # Produced files are stronger, replayable evidence than transient stdout.
    artifact_text = _latest_artifact_text(base_dir)
    if artifact_text:
        return artifact_text
    for text in reversed(extra_texts or []):
        if text and text.strip():
            return text
    return None


def check_observations(
    checks: list[dict[str, Any]],
    *,
    observations: list[dict[str, Any]],
    base_dir: Path,
    extra_texts: list[str] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in checks:
        try:
            ok, evidence = _eval_one(
                spec,
                observations=observations,
                base_dir=base_dir,
                extra_texts=extra_texts,
            )
        except Exception as exc:  # noqa: BLE001 - 检查失败不能吞掉，要落成证据
            ok, evidence = False, f"检查执行失败: {type(exc).__name__}: {exc}"
        results.append(
            {
                "id": spec.get("id") or spec.get("type"),
                "type": spec["type"],
                "spec": dict(spec),
                "ok": bool(ok),
                "evidence": _clip(str(evidence), MAX_EVIDENCE),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# 沙箱路径 / 脚本
# --------------------------------------------------------------------------- #
def safe_path(base: Path, raw: str, *, field: str = "path") -> Path:
    """Resolve a relative path inside ``base``; reject absolute/escaping paths."""
    text = str(raw or "").strip()
    if not text:
        raise AgentExecError("bad_request", f"{field} 不能为空")
    candidate = Path(text)
    if candidate.is_absolute():
        raise AgentExecError("bad_request", f"{field} 必须是相对路径: {text}")
    base_resolved = base.resolve()
    target = (base_resolved / candidate).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise AgentExecError(
            "bad_request", f"{field} 越界（禁止路径穿越）: {text}"
        ) from exc
    return target


def _run_python(path: Path, *, cwd: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            env=None,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return {
            "exit_code": -1,
            "ok": False,
            "timeout": True,
            "stdout": _clip(stdout, MAX_SCRIPT_STDOUT),
            "stderr": _clip(stderr, MAX_SCRIPT_STDERR),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "exit_code": proc.returncode,
        "ok": proc.returncode == 0,
        "timeout": False,
        "stdout": _clip(proc.stdout or "", MAX_SCRIPT_STDOUT),
        "stderr": _clip(proc.stderr or "", MAX_SCRIPT_STDERR),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ExploreAgent:
    """LLM-driven explorer: tools + scripts, deterministic verification."""

    def __init__(
        self,
        *,
        sandbox: Path,
        registry: Any,
        llm: Any,
        emit: Callable[[str, dict[str, Any]], dict[str, Any]],
        allow_scripts: bool = True,
        max_steps: int = 12,
        total_timeout: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        intent: str = "",
        domain_pack: str = "",
    ) -> None:
        self.sandbox = Path(sandbox)
        self.registry = registry
        self.llm = llm
        self.emit = emit
        self.allow_scripts = allow_scripts
        self.max_steps = max(1, min(50, int(max_steps)))
        self.total_timeout = max(1.0, float(total_timeout))
        self.clock = clock
        self.intent = str(intent or "")
        self.domain_pack = str(domain_pack or "")
        self.catalog = tool_catalog_for_intent(
            registry,
            self.intent,
            domain_pack=self.domain_pack,
            allow_scripts=allow_scripts,
        )
        self.tool_names = {entry["name"] for entry in self.catalog}
        self.observations: list[dict[str, Any]] = []
        self.scripts: list[dict[str, Any]] = []
        self.script_outputs: list[str] = []
        self.pending_feedback = ""
        self.last_tool_sig = ""
        self.repeat_count = 0
        self.seen_tool_keys: set[str] = set()
        self.seen_script_keys: set[str] = set()
        self.started = self.clock()

    # ------------------------------------------------------------------ #
    def run(self, intent: str, success_criteria: Any = None) -> dict[str, Any]:
        if self.llm is None or getattr(self.llm, "available", True) is False:
            raise AgentExecError(
                "llm_required",
                "探索需要配置 LLM（PANGUVERSE_API_KEY / OPENAI_API_KEY）；"
                "未配置时不做假执行",
                status=503,
            )
        if str(intent or "") != self.intent:
            self.intent = str(intent or "")
            self.catalog = tool_catalog_for_intent(
                self.registry,
                self.intent,
                domain_pack=self.domain_pack,
                allow_scripts=self.allow_scripts,
            )
            self.tool_names = {entry["name"] for entry in self.catalog}
        checks_spec = normalize_checks(success_criteria)
        has_user_criteria = bool(checks_spec)
        feedback = ""
        saw_final = False
        final_checks_retry_used = False
        last_checks: list[dict[str, Any]] = []
        actions_used = 0
        llm_turns = 0
        while actions_used < self.max_steps:
            llm_turns += 1
            if llm_turns > self.max_steps + 6:
                return self._auto_finalize(
                    actions_used=actions_used,
                    checks_spec=checks_spec,
                    given_criteria=has_user_criteria,
                    last_checks=last_checks,
                    reason=f"LLM 回合超限（>{self.max_steps + 6}）",
                )
            if self.clock() - self.started >= self.total_timeout:
                return self._auto_finalize(
                    actions_used=actions_used,
                    checks_spec=checks_spec,
                    given_criteria=has_user_criteria,
                    last_checks=last_checks,
                    reason=f"总时长超过 {self.total_timeout:.0f}s",
                )
            try:
                decision = self._decide(intent, checks_spec, actions_used + 1, feedback)
            except AgentExecError as exc:
                if exc.code not in {"llm_error", "llm_invalid"}:
                    raise
                # _decide 已用精简上下文重试过一次；仍失败时不能把会话打成
                # error：有真实产物就按 file_exists 收尾，没有才 failed。
                return self._auto_finalize(
                    actions_used=actions_used,
                    checks_spec=checks_spec,
                    given_criteria=has_user_criteria,
                    last_checks=last_checks,
                    reason=_llm_failure_message(exc.message),
                )
            action = str(decision.get("action") or "").strip()
            thought = str(decision.get("thought") or "").strip()
            if thought:
                self.emit("thought", {"text": _clip(thought, MAX_THOUGHT)})
            if action in ("tool", "script"):
                # 预算只计真实动作（tool/script），不计 thought / final 重试。
                actions_used += 1
                self.pending_feedback = ""
                if action == "tool":
                    self._do_tool(decision, actions_used)
                else:
                    self._do_script(decision, actions_used)
                feedback = self.pending_feedback
                # 给定的 success_criteria 每一步都立即评估；满足即自动收尾。
                checks = self._evaluate(checks_spec)
                if checks:
                    last_checks = checks
                    if all(item["ok"] for item in checks):
                        self.emit("verify", {"checks": checks, "verdict": "passed"})
                        return self._finish(
                            "succeeded",
                            checks=checks,
                            summary={"steps": actions_used, "verdict": "passed", "auto_verified": True},
                        )
                continue
            if action == "final":
                saw_final = True
                proposed = decision.get("checks")
                try:
                    specs = checks_spec or normalize_checks(proposed)
                except AgentExecError as exc:
                    # LLM 的 final checks 缺字段/类型非法：给一次反馈修正，
                    # 连续非法才以可读原因失败（不能把会话打成 error）。
                    feedback = (
                        f"你的 final checks 非法：{exc.message}。"
                        "请重发 final，给出可执行检查（type + 参数）。"
                    )
                    if final_checks_retry_used:
                        if not has_user_criteria:
                            # The model failed twice on schema, but real files may
                            # still prove completion.  Close on those files; if
                            # there are none, this still returns failed.
                            return self._auto_finalize(
                                actions_used=actions_used,
                                checks_spec=checks_spec,
                                given_criteria=has_user_criteria,
                                last_checks=last_checks,
                                reason=f"final checks 连续非法：{exc.message}",
                                saw_final=True,
                            )
                        return self._finish(
                            "failed",
                            checks=last_checks,
                            summary={
                                "reason": f"final checks 连续非法：{exc.message}",
                                "steps": actions_used,
                            },
                        )
                    final_checks_retry_used = True
                    self.emit("feedback", {"text": feedback})
                    continue
                if not specs:
                    feedback = (
                        "final 缺少可执行 checks：请补充 text_contains/status_code/"
                        "file_exists/regex 检查后再 final。"
                    )
                    if final_checks_retry_used:
                        if not has_user_criteria:
                            return self._auto_finalize(
                                actions_used=actions_used,
                                checks_spec=checks_spec,
                                given_criteria=has_user_criteria,
                                last_checks=last_checks,
                                reason="final 未提供可执行 checks（已反馈一次仍未修正）",
                                saw_final=True,
                            )
                        return self._finish(
                            "failed",
                            checks=last_checks,
                            summary={
                                "reason": "final 未提供可执行 checks（已反馈一次仍未修正）",
                                "steps": actions_used,
                            },
                        )
                    final_checks_retry_used = True
                    self.emit("feedback", {"text": feedback})
                    continue
                checks_spec = specs
                checks = self._evaluate(specs)
                verdict = "passed" if checks and all(item["ok"] for item in checks) else "failed"
                self.emit("verify", {"checks": checks or [], "verdict": verdict})
                last_checks = checks or []
                if verdict == "passed":
                    return self._finish(
                        "succeeded",
                        checks=checks or [],
                        summary={"steps": actions_used, "verdict": "passed"},
                    )
                failed = [item for item in checks or [] if not item["ok"]]
                feedback = "确定性校验未通过：" + "；".join(
                    f"{item['id']}: {item['evidence']}" for item in failed[:5]
                )
                continue
            raise AgentExecError(
                "llm_invalid",
                f"LLM 决策非法：action 必须是 tool/script/final，收到 {action or '<空>'}",
                status=502,
            )
        return self._auto_finalize(
            actions_used=actions_used,
            checks_spec=checks_spec,
            given_criteria=has_user_criteria,
            last_checks=last_checks,
            reason=f"达到 max_steps={self.max_steps}（仅计 tool/script 动作）",
            saw_final=saw_final,
        )

    def _candidate_outputs(self) -> list[str]:
        """Files that can serve as deterministic completion evidence."""
        base = self.sandbox.resolve()
        rank = {".html": 0, ".htm": 0, ".md": 1, ".txt": 2, ".csv": 3, ".json": 4}
        found: list[tuple[int, float, str]] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in rank:
                continue
            try:
                rel = str(path.relative_to(base))
            except ValueError:
                continue
            parts = Path(rel).parts
            if parts and parts[0] in {
                "scripts",
                ".panguverse",
                "replay",
                "__pycache__",
                "artifacts",
            }:
                continue
            if path.name in {"session.json", "success.json"} or path.name.startswith("."):
                continue
            found.append((rank[suffix], -path.stat().st_mtime, rel))
        found.sort()
        return [rel for _rank, _mtime, rel in found[:8]]

    def _suggest_file_checks(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        used: set[str] = set()
        for index, rel in enumerate(self._candidate_outputs(), 1):
            base = Path(rel).stem
            if base.lower() in {"report", "hot_news_report"} and Path(rel).suffix.lower() == ".html":
                name = "report_html_exists"
            else:
                name = f"file_{_sanitize_var(base)}_{index}"
            while name in used:
                name = f"{name}_{index}"
            used.add(name)
            checks.append({"id": name, "type": "file_exists", "path": rel})
        return checks

    def _auto_finalize(
        self,
        *,
        actions_used: int,
        checks_spec: list[dict[str, Any]],
        given_criteria: bool,
        last_checks: list[dict[str, Any]],
        reason: str,
        saw_final: bool = False,
    ) -> dict[str, Any]:
        """Budget exhausted: close on real produced files, never on empty checks.

        Given success criteria are respected: if they were not met at exhaustion
        we fail rather than invent unrelated file checks.  For no-criteria
        sessions we generate ``file_exists`` checks from actual outputs.
        """
        if given_criteria:
            passed = bool(last_checks) and all(item["ok"] for item in last_checks)
            status = "succeeded" if passed else "failed"
            tail = "给定成功标准已满足" if passed else "给定成功标准未满足"
            return self._finish(
                status,
                checks=last_checks,
                summary={"reason": f"{reason}；{tail}", "steps": actions_used},
            )
        suggestions = self._suggest_file_checks()
        if not suggestions:
            return self._finish(
                "failed",
                checks=last_checks,
                summary={
                    "reason": reason + "；没有可校验的产物文件，不能伪装成功",
                    "steps": actions_used,
                },
            )
        checks = self._evaluate(suggestions)
        verdict = "passed" if checks and all(item["ok"] for item in checks) else "failed"
        self.emit(
            "verify",
            {"checks": checks, "verdict": verdict, "auto_finalized": True},
        )
        if verdict == "passed":
            return self._finish(
                "succeeded",
                checks=checks,
                summary={
                    "reason": reason + "；已用产物文件自动收尾",
                    "steps": actions_used,
                    "verdict": "passed",
                    "auto_finalized": True,
                    "saw_final": bool(saw_final),
                },
            )
        return self._finish(
            "failed",
            checks=checks,
            summary={
                "reason": reason + "；自动收尾检查未通过",
                "steps": actions_used,
                "verdict": "failed",
                "auto_finalized": True,
            },
        )

    def _evaluate(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not specs:
            return []
        return check_observations(
            specs,
            observations=self.observations,
            base_dir=self.sandbox,
            extra_texts=self.script_outputs,
        )

    # ------------------------------------------------------------------ #
    def _finish(
        self,
        status: str,
        *,
        checks: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        summary = {
            **summary,
            "tool_calls": len(self.observations),
            "scripts": len(self.scripts),
            "duration_ms": int((self.clock() - self.started) * 1000),
        }
        self.emit(
            "end",
            {
                "status": status,
                "verdict": "passed" if status == "succeeded" else "failed",
                "checks": checks,
                "summary": summary,
            },
        )
        return {
            "status": status,
            "verdict": "passed" if status == "succeeded" else "failed",
            "checks": checks,
            "summary": summary,
        }

    def _decide(
        self,
        intent: str,
        criteria: list[dict[str, Any]],
        step_no: int,
        feedback: str,
    ) -> dict[str, Any]:
        system = (
            "你是 PanguVerse 的探索执行器。你必须真的把事情跑通，而不是编造结果。\n"
            "每一步只输出一个 JSON 对象，不要输出 Markdown。格式三选一：\n"
            '{"action":"tool","tool":"<工具名>","params":{...},"thought":"..."}\n'
            '{"action":"script","path":"scripts/step.py","code":"<python 源码>","thought":"..."}\n'
            '{"action":"final","checks":[{"id":"...","type":"text_contains|status_code|file_exists|regex",...}],"thought":"..."}\n'
            "规则：\n"
            "- 只能调用工具清单里的工具；参数名必须匹配 inputs。\n"
            "- 引用前面工具输出用 ${stepN.field}（N 为工具调用序号，从 1 开始）。\n"
            "- 脚本在工作目录内执行（python -I），路径必须是相对路径。\n"
            "- final 的 checks 必须是确定性检查；LLM 自述不算通过。\n"
            "- 给定 criteria 一旦满足就立即 final；不要重复打开同一页面或重复落盘。\n"
            "- 没有可执行检查时不得 final。\n"
            "- 如果 last_failure 指出 checks 非法，必须修正字段后重发 final。\n"
            "- 剩余步数 <=2 时必须立即 final；已有产物优先用 file_exists。\n"
        )
        # 系统提示也算在 8KB 预算里，给用户提示留出剩余空间。
        prompt_budget = MAX_PROMPT_BYTES - len(system.encode("utf-8"))
        try:
            return self._decide_once(
                system,
                intent,
                criteria,
                step_no,
                feedback,
                slim=False,
                prompt_budget=prompt_budget,
            )
        except AgentExecError as exc:
            if exc.code not in {"llm_error", "llm_invalid"}:
                raise
            self.emit(
                "feedback",
                {"text": f"LLM 决策失败：{exc.message}；改用精简上下文重试一次"},
            )
            try:
                return self._decide_once(
                    system,
                    intent,
                    criteria,
                    step_no,
                    feedback,
                    slim=True,
                    prompt_budget=prompt_budget,
                )
            except AgentExecError as retry_exc:
                raise AgentExecError(
                    "llm_error",
                    _llm_failure_message(retry_exc.message),
                    status=502,
                ) from retry_exc

    def _decide_once(
        self,
        system: str,
        intent: str,
        criteria: list[dict[str, Any]],
        step_no: int,
        feedback: str,
        *,
        slim: bool,
        prompt_budget: int,
    ) -> dict[str, Any]:
        payload = self._decision_payload(intent, criteria, step_no, feedback, slim=slim)
        user = self._serialize_prompt(payload, prompt_budget)
        try:
            # 真实模型有时会在 JSON 前后夹带解释或多个对象；complete_json 会整段
            # 解析失败。这里取原始文本并抽取真正的 action 对象。
            if hasattr(self.llm, "complete"):
                decision = _extract_decision(self.llm.complete(system, user))
            else:
                decision = self.llm.complete_json(system, user)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            raise AgentExecError("llm_error", message, status=502) from exc
        if not isinstance(decision, dict):
            raise AgentExecError("llm_invalid", "LLM 未返回 JSON 对象", status=502)
        return decision

    def _decision_payload(
        self,
        intent: str,
        criteria: list[dict[str, Any]],
        step_no: int,
        feedback: str,
        *,
        slim: bool,
    ) -> dict[str, Any]:
        remaining_steps = max(0, self.max_steps - (step_no - 1))
        suggested_checks = self._suggest_file_checks() if remaining_steps <= 2 else []
        if remaining_steps <= 2:
            hint = (
                f"预算将尽（剩余 {remaining_steps} 步）：不要再重复 tool/script；"
                "立即 final，用确定性 file_exists/text_contains 检查收尾。"
            )
            if suggested_checks:
                hint += " 可直接使用 suggested_checks（来自真实产物文件）。"
            instructions = hint
        else:
            instructions = (
                "给出下一步决策。若已具备证据，用 final + 确定性 checks 收尾。"
                if step_no > 1
                else "先观察真实世界：打开目标或读取数据。"
            )
        if slim:
            instructions = "精简上下文重试：只保留最近观察，直接给出可执行决策。" + instructions
        obs_limit = MAX_PROMPT_SLIM_OBSERVATION if slim else MAX_PROMPT_OBSERVATION
        obs_count = MAX_PROMPT_SLIM_OBSERVATIONS if slim else MAX_PROMPT_OBSERVATIONS
        payload: dict[str, Any] = {
            "intent": _clip(str(intent or ""), 300 if slim else 500),
            "step": step_no,
            "max_steps": self.max_steps,
            "remaining_steps": remaining_steps,
            "remaining_seconds": round(
                max(0.0, self.total_timeout - (self.clock() - self.started)), 1
            ),
            "tools": self._compact_catalog(),
            "observations": self._prompt_observations(obs_count, obs_limit),
            "criteria": criteria,
            "last_failure": _clip(str(feedback or ""), 600 if slim else 1_200),
            "suggested_checks": suggested_checks,
            "instructions": instructions,
        }
        if slim:
            payload["slim_context"] = True
        return payload

    def _compact_catalog(self) -> list[dict[str, Any]]:
        """工具目录压缩：只保留可执行决策需要的字段，避免 8KB 提示被目录撑爆。"""
        tools: list[dict[str, Any]] = []
        for entry in self.catalog:
            inputs: dict[str, str] = {}
            for key, raw in (entry.get("inputs") or {}).items():
                hint = re.split(r"[（(]", str(raw), maxsplit=1)[0].strip()
                inputs[str(key)] = _clip(hint, 32)
            item: dict[str, Any] = {
                "name": str(entry.get("name") or ""),
                "description": _clip(str(entry.get("description") or ""), 60),
                "inputs": inputs,
            }
            required = entry.get("required") or []
            if required:
                item["required"] = [str(name) for name in required]
            tools.append(item)
        return tools

    def _prompt_observations(
        self, limit: int, obs_limit: int
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, item in enumerate(self.observations, 1):
            digest_text = json.dumps(
                item.get("digest") or {}, ensure_ascii=False, default=str
            )
            entry: dict[str, Any] = {
                "step": index,
                "tool": item.get("tool") or "",
                "ok": bool(item.get("ok")),
                "digest": digest_text,
            }
            serialized = json.dumps(entry, ensure_ascii=False)
            if len(serialized) > obs_limit:
                delta = len(serialized) - len(digest_text)
                entry["digest"] = _clip(digest_text, max(20, obs_limit - delta))
            items.append(entry)
        return items[-limit:]

    def _serialize_prompt(self, payload: dict[str, Any], limit: int) -> str:
        def dump(value: dict[str, Any]) -> str:
            return json.dumps(value, ensure_ascii=False, default=str)

        def fits(text: str) -> bool:
            return len(text.encode("utf-8")) <= limit

        text = dump(payload)
        if fits(text):
            return text
        observations = list(payload.get("observations") or [])
        for cap, keep in ((400, 6), (200, 3), (100, 2), (60, 1)):
            payload["observations"] = [
                {
                    "step": item.get("step"),
                    "tool": item.get("tool"),
                    "ok": item.get("ok"),
                    "digest": _clip(str(item.get("digest") or ""), cap),
                }
                for item in observations[-keep:]
            ]
            text = dump(payload)
            if fits(text):
                return text
        payload["tools"] = [
            {"name": tool.get("name"), "inputs": tool.get("inputs") or {}}
            for tool in payload.get("tools") or []
        ]
        payload["suggested_checks"] = []
        payload["last_failure"] = _clip(str(payload.get("last_failure") or ""), 200)
        text = dump(payload)
        if fits(text):
            return text
        payload["tools"] = [tool.get("name") for tool in payload.get("tools") or []]
        payload["observations"] = []
        text = dump(payload)
        if fits(text):
            return text
        payload["intent"] = _clip(str(payload.get("intent") or ""), 80)
        payload["instructions"] = _clip(str(payload.get("instructions") or ""), 100)
        text = dump(payload)
        if fits(text):
            return text
        # 理论上到不了这里；最后只保留最小可决策字段。
        minimal = {
            "intent": _clip(str(payload.get("intent") or ""), 80),
            "step": payload.get("step"),
            "max_steps": payload.get("max_steps"),
            "instructions": _clip(str(payload.get("instructions") or ""), 100),
            "tools": [str(name) for name in payload.get("tools") or []],
        }
        return dump(minimal)

    # ------------------------------------------------------------------ #
    def _do_tool(self, decision: Mapping[str, Any], step_no: int) -> None:
        tool = str(decision.get("tool") or "").strip()
        params = decision.get("params")
        if not isinstance(params, Mapping):
            raise AgentExecError("llm_invalid", "tool 决策缺少 params 对象", status=502)
        if tool not in self.tool_names:
            raise AgentExecError(
                "tool_not_found",
                f"工具不存在: {tool or '<空>'}；可用工具: {', '.join(sorted(self.tool_names)[:20])}",
                status=400,
            )
        signature = self._tool_dedupe_key(tool, params)
        if signature in self.seen_tool_keys:
            if tool == "fs_list":
                message = (
                    "同一 tool+params 已执行过（fs_list 按相同根目录去重）："
                    "已跳过重复调用，请改用 final 或其他动作"
                )
            else:
                message = "同一 tool+params 已执行过：已跳过重复调用，请改用 final 或其他动作"
            self.emit("thought", {"text": f"系统：{message}"})
            self.emit(
                "tool_result",
                {
                    "tool": tool,
                    "ok": False,
                    "digest": {},
                    "artifacts": [],
                    "error": message,
                    "skipped": True,
                },
            )
            self.pending_feedback = message
            return
        self.seen_tool_keys.add(signature)
        try:
            resolved = self._resolve_refs(dict(params))
        except AgentExecError as exc:
            raw_params = dict(params)
            call_event = self.emit(
                "tool_call", {"tool": tool, "params": raw_params, "step": step_no}
            )
            digest = digest_result({})
            self.observations.append(
                {
                    "index": len(self.observations) + 1,
                    "tool": tool,
                    "params": raw_params,
                    "ok": False,
                    "result": {},
                    "digest": digest,
                    "artifacts": [],
                }
            )
            self.emit(
                "tool_result",
                {"tool": tool, "ok": False, "digest": digest, "artifacts": [], "error": exc.message},
            )
            return
        call_event = self.emit(
            "tool_call",
            {
                "tool": tool,
                "params": resolved,
                "params_template": dict(params),
                "step": step_no,
                **self._tool_signature(tool, resolved),
            },
        )
        ok, result, error = self._invoke(tool, resolved)
        digest = digest_result(result if ok else {})
        digest.update(self._tool_signature(tool, resolved))
        artifacts = self._write_artifacts(
            int(call_event.get("seq") or step_no), tool, resolved, result if ok else {}
        )
        self.observations.append(
            {
                "index": len(self.observations) + 1,
                "tool": tool,
                "params": resolved,
                "ok": ok,
                "result": result if ok else (result or {}),
                "digest": digest,
                "artifacts": artifacts,
            }
        )
        payload: dict[str, Any] = {
            "tool": tool,
            "ok": ok,
            "digest": digest,
            "artifacts": artifacts,
        }
        if error:
            payload["error"] = _clip(error, 1_000)
        self.emit("tool_result", payload)

    def _tool_dedupe_key(self, tool: str, params: Mapping[str, Any]) -> str:
        if tool == "fs_list":
            root = str(params.get("path") or ".")
            return "fs_list\nroot=" + Path(root).as_posix()
        payload = json.dumps(dict(params), ensure_ascii=False, sort_keys=True, default=str)
        if tool == "script_run":
            payload += "\n" + json.dumps(self._tool_signature(tool, params), sort_keys=True)
        return tool + "\n" + payload

    def _tool_signature(self, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """script_run 的代码指纹：执行哪份代码必须可追溯。"""
        if tool != "script_run":
            return {}
        try:
            target = safe_path(self.sandbox, str(params.get("path") or ""), field="path")
        except AgentExecError:
            return {}
        if not target.is_file():
            return {}
        return {"code_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    def _invoke(self, tool: str, params: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
        try:
            if tool == "artifact_read":
                target = safe_path(self.sandbox, str(params.get("path") or ""), field="path")
                if not target.is_file():
                    return False, {}, f"产物不存在: {params.get('path')}"
                raw = target.read_bytes()[:MAX_ARTIFACT_BYTES]
                text = raw.decode("utf-8", errors="replace")
                limit = int(params.get("max_chars") or MAX_SCRIPT_STDOUT)
                return True, {"path": str(params.get("path")), "bytes": len(raw), "text": _clip(text, limit)}, ""
            if tool == "script_run":
                if not self.allow_scripts:
                    return False, {}, "脚本执行被禁用（allow_scripts=false）"
                target = safe_path(self.sandbox, str(params.get("path") or ""), field="path")
                if not target.is_file():
                    return False, {}, f"脚本不存在: {params.get('path')}"
                timeout = float(params.get("timeout") or DEFAULT_SCRIPT_TIMEOUT)
                outcome = _run_python(target, cwd=self.sandbox, timeout=min(timeout, MAX_SCRIPT_TIMEOUT))
                return bool(outcome["ok"]), outcome, "" if outcome["ok"] else _clip(outcome["stderr"], 1_000)
            atom = self.registry.get(tool)
            ctx = RunContext(
                workspace=str(self.sandbox), env={"FS_ROOT": str(self.sandbox)}
            )
            result = atom.invoke(ctx, params)
            return True, dict(result), ""
        except Exception as exc:  # noqa: BLE001 - 工具失败要进入观测，由 LLM 决定是否补救
            message = f"{type(exc).__name__}: {exc}"
            return False, {}, _clip(message, 1_000)

    def _do_script(self, decision: Mapping[str, Any], step_no: int) -> None:
        path_text = str(decision.get("path") or f"scripts/step_{step_no}.py")
        code = decision.get("code")
        if not isinstance(code, str) or not code.strip():
            raise AgentExecError("llm_invalid", "script 决策缺少 code 源码", status=502)
        if not self.allow_scripts:
            self.emit(
                "script",
                {
                    "path": path_text,
                    "ok": False,
                    "exit_code": -1,
                    "error": "脚本执行被禁用（allow_scripts=false）",
                },
            )
            return
        code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
        script_key = path_text + "\n" + code_sha
        if script_key in self.seen_script_keys:
            message = "同一脚本 path+sha 已执行过：已跳过重复执行，请改用 final 或其他动作"
            self.emit("thought", {"text": f"系统：{message}"})
            self.emit(
                "script",
                {
                    "path": path_text,
                    "code_sha256": code_sha,
                    "exit_code": 0,
                    "ok": False,
                    "skipped": True,
                    "error": message,
                },
            )
            self.pending_feedback = message
            return
        self.seen_script_keys.add(script_key)
        target = safe_path(self.sandbox, path_text, field="path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        # Snapshot the exact code version before executing it.  The same script
        # path is overwritten by later LLM turns, so the trace must carry a
        # content-addressed copy for crystallization.
        code_name = f"{step_no:03d}_script_{_safe_tool_fragment(Path(path_text).stem)}_code.py"
        code_path = self.sandbox / "artifacts" / code_name
        code_path.parent.mkdir(parents=True, exist_ok=True)
        code_path.write_text(code, encoding="utf-8")
        code_artifact = str(code_path.relative_to(self.sandbox.resolve()))
        outcome = _run_python(target, cwd=self.sandbox, timeout=DEFAULT_SCRIPT_TIMEOUT)
        rel = str(target.relative_to(self.sandbox.resolve()))
        artifacts = self._write_artifacts(
            step_no,
            f"script_{Path(rel).stem}",
            {},
            {"stdout": outcome.get("stdout", ""), "stderr": outcome.get("stderr", "")},
        )
        artifacts.insert(0, code_artifact)
        event = {
            "path": rel,
            "code_sha256": code_sha,
            "code_artifact": code_artifact,
            "exit_code": outcome["exit_code"],
            "ok": bool(outcome["ok"]),
            "stdout": outcome["stdout"],
            "stderr": outcome["stderr"],
            "duration_ms": outcome["duration_ms"],
            "artifacts": artifacts,
            "step": step_no,
        }
        self.scripts.append(
            {
                "path": rel,
                "code_sha256": code_sha,
                "code_artifact": code_artifact,
                "exit_code": outcome["exit_code"],
                "ok": bool(outcome["ok"]),
            }
        )
        self.script_outputs.append(
            (outcome.get("stdout") or "") + "\n" + (outcome.get("stderr") or "")
        )
        self.emit("script", event)

    # ------------------------------------------------------------------ #
    def _resolve_refs(self, params: dict[str, Any]) -> dict[str, Any]:
        return {key: self._resolve_value(value) for key, value in params.items()}

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, str):
            match = _FULL_STEP_REF_RE.match(value.strip())
            if match:
                index = int(match.group(1))
                path = match.group(2)
                if 1 <= index <= len(self.observations):
                    found = walk_path(self.observations[index - 1]["result"], split_path(path))
                    if found is not MISSING:
                        return found
                raise AgentExecError("bad_request", f"引用的步骤输出不存在: {value}")
            match = _FULL_LAST_REF_RE.match(value.strip())
            if match:
                if not self.observations:
                    raise AgentExecError("bad_request", f"没有可引用的上一步输出: {value}")
                found = walk_path(
                    self.observations[-1]["result"], split_path(match.group(1))
                )
                if found is not MISSING:
                    return found
                raise AgentExecError("bad_request", f"引用的上一步输出不存在: {value}")
            return value
        if isinstance(value, Mapping):
            return {key: self._resolve_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(item) for item in value]
        return value

    # ------------------------------------------------------------------ #
    def _write_artifacts(
        self,
        seq: int,
        tool: str,
        params: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> list[str]:
        if not result:
            return []
        artifacts_dir = self.sandbox / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{seq:03d}_{_safe_tool_fragment(tool)}"
        written: list[str] = []
        payload_result: dict[str, Any] = {}
        for key, value in result.items():
            if isinstance(value, str) and len(value) > MAX_ARTIFACT_TEXT:
                suffix = "html" if key == "html" else "txt"
                filename = f"{stem}_{key}.{suffix}"
                (artifacts_dir / filename).write_text(value, encoding="utf-8")
                written.append(f"artifacts/{filename}")
                payload_result[key] = f"<artifacts/{filename}>"
            elif isinstance(value, str):
                payload_result[key] = _clip(value, MAX_ARTIFACT_TEXT)
            else:
                payload_result[key] = value
        record = {
            "seq": seq,
            "tool": tool,
            "params": dict(params),
            "result": payload_result,
        }
        payload = json.dumps(record, ensure_ascii=False, default=str)
        if len(payload) > MAX_ARTIFACT_BYTES:
            payload = payload[:MAX_ARTIFACT_BYTES]
        filename = f"{stem}.json"
        (artifacts_dir / filename).write_text(payload, encoding="utf-8")
        written.insert(0, f"artifacts/{filename}")
        return written


def digest_result(result: Mapping[str, Any]) -> dict[str, Any]:
    digest: dict[str, Any] = {"keys": sorted(str(key) for key in (result or {}))}
    for key in ("status", "status_code", "title", "final_url", "chars", "exit_code", "attempts", "bytes"):
        if key in result:
            digest[key] = result[key]
    text = result.get("text") or result.get("body") or result.get("stdout")
    if isinstance(text, str):
        digest["text_chars"] = len(text)
        digest["text_head"] = _clip(text, 160)
    html = result.get("html")
    if isinstance(html, str):
        digest["html_bytes"] = len(html)
    return digest


# --------------------------------------------------------------------------- #
# 固化 / 重放
# --------------------------------------------------------------------------- #
def _sanitize_var(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "value"


def build_crystallized_graph(
    events: list[dict[str, Any]],
    *,
    eid: str,
    intent: str,
    checks: list[dict[str, Any]],
    name: str = "",
    domain_pack: str = "",
    registry: Any = None,
    session_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    llm: Any = None,
    feedback: str = "",
) -> dict[str, Any]:
    """Crystallize a successful trace through LLM semantic abstraction.

    The LLM only *induces* (which steps matter, how to generalize scripts, what
    proves success).  Materialization, generality gates, graph construction and
    replay are deterministic.  Without an LLM crystallization is refused: a
    recording must never masquerade as a workflow.
    """
    from . import abstraction

    if registry is None:
        raise AgentExecError("internal_error", "固化需要原子注册表")
    if session_dir is None or workspace is None:
        raise AgentExecError("bad_request", "固化需要探索会话目录与工作区")
    base_dir = Path(session_dir)
    root = Path(workspace)
    if not isinstance(llm, object) or llm is None or getattr(llm, "available", True) is False:
        raise AgentExecError(
            "llm_required_for_crystallize",
            "固化需要 LLM 做语义归纳（PANGUVERSE_API_KEY / OPENAI_API_KEY）；未配置时不做假固化",
            status=503,
        )
    catalog = abstraction.atom_catalog(registry)
    digest = abstraction.build_digest(
        events, session_dir=base_dir, checks=checks, intent=intent
    )
    try:
        plan = abstraction.plan_crystallization(
            llm, intent=intent, digest=digest, catalog=catalog
        )
    except abstraction.AbstractionError as exc:
        raise AgentExecError(
            exc.code, exc.message, status=exc.status, details=exc.details
        ) from exc

    script_by_seq = {
        int(item.get("seq") or 0): item for item in digest.get("scripts") or []
    }
    nodes_by_new: dict[str, dict[str, Any]] = {}
    for node in plan.get("nodes") or []:
        atom = str(node.get("atom") or "")
        if atom.startswith("new:"):
            nodes_by_new[abstraction._snake(atom.split(":", 1)[1])] = node

    pending: list[dict[str, Any]] = []
    dropped_steps: list[dict[str, Any]] = [
        {
            "trace_seq": item.get("seq"),
            "path": item.get("path"),
            "action": "drop",
            "reason": f"失败步骤不固化：{item.get('reason') or 'ok=false'}",
            "kind": item.get("kind"),
        }
        for item in digest.get("failed_steps") or []
    ]
    created: list[dict[str, Any]] = []
    new_atoms: dict[str, dict[str, Any]] = {}
    abstraction_checks: list[dict[str, Any]] = []
    for item in plan.get("scripts") or []:
        seq = int(item.get("seq") or 0)
        script = script_by_seq.get(seq)
        if script is None:
            continue
        if item.get("action") != "abstract":
            dropped_steps.append(
                {
                    "trace_seq": seq,
                    "path": script.get("path"),
                    "action": item.get("action"),
                    "reason": item.get("reason") or "计划未保留",
                    "kind": "script",
                }
            )
            continue
        atom_name = str(item.get("as") or "")
        plan_node = nodes_by_new.get(atom_name)
        if plan_node is None:
            dropped_steps.append(
                {
                    "trace_seq": seq,
                    "path": script.get("path"),
                    "action": "abstract",
                    "reason": "抽象结果没有被任何节点使用",
                    "kind": "script",
                }
            )
            continue
        try:
            abstracted = abstraction.abstract_script(
                llm,
                intent=intent,
                script=script,
                plan_node=plan_node,
                checks=list(plan.get("checks") or []),
                feedback=feedback,
            )
        except abstraction.AbstractionError as exc:
            pending.append(
                {
                    "name": atom_name,
                    "path": script.get("path"),
                    "code_sha256": script.get("code_sha256"),
                    "trace_seq": seq,
                    "status": "pending",
                    "kind": "script",
                    "reason": exc.message,
                }
            )
            continue
        # Write the candidate module to the workspace pack, then gate it.  A
        # rejected atom is removed from disk so nothing can silently use it.
        info = abstraction.write_atom(
            workspace=root,
            eid=eid,
            name=atom_name,
            source=abstracted["source"],
            contract=abstracted["contract"],
            script=script,
            description=abstracted["description"],
            generality=abstraction.GeneralityReport(ok=False),
        )
        report = abstraction.generality_check(
            module_path=Path(info["path"]),
            source=abstracted["source"],
            contract=abstracted["contract"],
            session_dir=base_dir,
            recorded_outputs=script.get("output_paths") or {},
        )
        has_source = any(
            str(key).rstrip("?") == "source"
            for key in (abstracted["contract"].get("inputs") or {})
        )
        if report.ok and has_source and not report.dynamic.get("produced"):
            # A "general" atom that emits nothing for a source input cannot
            # replay a real run; ask the abstraction LLM to fix its parser once.
            repair_feedback = (
                (feedback + "\n\n" if feedback else "")
                + "用合成 source 运行后没有产出任何声明的输出文件。"
                "必须递归解析 source（例如 JSON 的 data.cards[].content[]），"
                "不要假设 data 一定是 list；source 非空时不得联网。"
                "样例 source：\n"
                + abstraction._synthetic_source("repair")
            )
            try:
                retried = abstraction.abstract_script(
                    llm,
                    intent=intent,
                    script=script,
                    plan_node=plan_node,
                    checks=list(plan.get("checks") or []),
                    feedback=repair_feedback,
                )
            except abstraction.AbstractionError:
                retried = None
            if retried is not None:
                abstracted = retried
                info = abstraction.write_atom(
                    workspace=root,
                    eid=eid,
                    name=atom_name,
                    source=abstracted["source"],
                    contract=abstracted["contract"],
                    script=script,
                    description=abstracted["description"],
                    generality=abstraction.GeneralityReport(ok=False),
                )
                report = abstraction.generality_check(
                    module_path=Path(info["path"]),
                    source=abstracted["source"],
                    contract=abstracted["contract"],
                    session_dir=base_dir,
                    recorded_outputs=script.get("output_paths") or {},
                )
        if not report.ok:
            Path(info["path"]).unlink(missing_ok=True)
            pending.append(
                {
                    "name": atom_name,
                    "path": script.get("path"),
                    "code_sha256": script.get("code_sha256"),
                    "trace_seq": seq,
                    "status": "pending",
                    "kind": "script",
                    "reason": "not_general",
                    "generality": report.to_dict(),
                }
            )
            dropped_steps.append(
                {
                    "trace_seq": seq,
                    "path": script.get("path"),
                    "action": "reject",
                    "reason": "not_general",
                    "violations": report.violations,
                    "kind": "script",
                }
            )
            continue
        info = abstraction.write_atom(
            workspace=root,
            eid=eid,
            name=atom_name,
            source=abstracted["source"],
            contract=abstracted["contract"],
            script=script,
            description=abstracted["description"],
            generality=report,
        )
        new_atoms[atom_name] = info
        created.append(info)
        abstraction_checks.extend(abstracted.get("checks") or [])

    abstraction.write_pack_manifest(root, eid, created)
    if abstraction_checks:
        produced = {
            str(rel)
            for info in new_atoms.values()
            for rel in (info.get("output_paths") or {}).values()
        }
        produced_names = {Path(rel).name for rel in produced}

        def _plan_check_mentions_new(spec: Mapping[str, Any]) -> bool:
            rel = str(spec.get("artifact") or spec.get("path") or "").replace("\\", "/")
            return rel in produced or Path(rel).name in produced_names

        original_checks = list(plan.get("checks") or [])
        merged = [
            item for item in original_checks if not _plan_check_mentions_new(item)
        ] + abstraction_checks
        if not any(
            item.get("type") in ("min_items", "freshness", "regex") for item in merged
        ):
            merged.extend(
                item
                for item in original_checks
                if item.get("type") in ("min_items", "freshness", "regex")
            )
        plan["checks"] = merged
    try:
        graph = abstraction.build_graph_from_plan(
            plan=plan,
            digest=digest,
            session_dir=base_dir,
            workspace=root,
            eid=eid,
            intent=intent,
            name=name,
            new_atoms=new_atoms,
            pending=pending,
            dropped_steps=dropped_steps,
            registry=registry,
        )
    except abstraction.AbstractionError as exc:
        raise AgentExecError(
            exc.code, exc.message, status=exc.status, details=exc.details
        ) from exc
    graph["atoms_created"] = created
    graph["pending_atoms"] = pending
    deduped_steps: list[dict[str, Any]] = []
    seen_steps: set[tuple[Any, ...]] = set()
    for item in dropped_steps:
        key = (
            item.get("trace_seq"),
            item.get("path"),
            item.get("node_id"),
            item.get("action"),
            item.get("reason"),
        )
        if key in seen_steps:
            continue
        seen_steps.add(key)
        deduped_steps.append(item)
    graph["dropped_steps"] = deduped_steps
    graph["digest"] = {
        "atom_steps": len(digest.get("atom_steps") or []),
        "scripts": len(digest.get("scripts") or []),
    }
    return graph


def _crystallize_param(
    value: Any,
    *,
    nodes_by_seq: Mapping[int, str],
    order_to_node: Mapping[int, str],
    capture_by_node: Mapping[str, dict[str, Any]],
    captures: dict[str, Any],
    current_node: str,
) -> Any:
    if isinstance(value, str):
        match = _FULL_STEP_REF_RE.match(value.strip())
        if not match:
            return value
        index = int(match.group(1))
        path = match.group(2)
        source_node = order_to_node.get(index)
        if not source_node:
            return value
        var = f"{source_node}_{_sanitize_var(path.replace('.', '_'))}"
        bucket = capture_by_node.get(source_node) if capture_by_node else captures
        if bucket is not None:
            bucket.setdefault(var, {"path": path})
        return f"${{{var}}}"
    if isinstance(value, Mapping):
        return {
            key: _crystallize_param(
                item,
                nodes_by_seq=nodes_by_seq,
                order_to_node=order_to_node,
                capture_by_node=capture_by_node,
                captures=captures,
                current_node=current_node,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _crystallize_param(
                item,
                nodes_by_seq=nodes_by_seq,
                order_to_node=order_to_node,
                capture_by_node=capture_by_node,
                captures=captures,
                current_node=current_node,
            )
            for item in value
        ]
    return value


_TEMPLATE_RE = re.compile(r"\$\{([^{}]+)\}")
_FULL_TEMPLATE_RE = re.compile(r"^\$\{([^{}]+)\}$")


def resolve_templates(value: Any, ctx: RunContext) -> Any:
    if isinstance(value, str):
        full = _FULL_TEMPLATE_RE.match(value.strip())
        if full:
            return ctx.get(full.group(1).strip())
        return _TEMPLATE_RE.sub(lambda m: str(ctx.get(m.group(1).strip())), value)
    if isinstance(value, Mapping):
        return {key: resolve_templates(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_templates(item, ctx) for item in value]
    return value


_TEXT_CHECK_KINDS = ("text_contains", "regex")
_TEXT_EXT_PRIORITY = {".html": 0, ".htm": 0, ".md": 1, ".txt": 2, ".json": 3, ".csv": 4}


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _matches_check(spec: Mapping[str, Any], text: str) -> bool:
    if spec.get("type") == "text_contains":
        return str(spec.get("value") or "") in text
    try:
        return re.search(str(spec.get("pattern") or ""), text) is not None
    except re.error:
        return False


def _node_by_trace(nodes: list[dict[str, Any]]) -> dict[int, str]:
    out: dict[int, str] = {}
    for node in nodes:
        seq = (node.get("extra") or {}).get("trace_seq")
        if seq not in (None, ""):
            try:
                out[int(seq)] = str(node.get("id") or "")
            except (TypeError, ValueError):
                continue
    return out


def _status_step_from_events(events: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> str:
    by_trace = _node_by_trace(nodes)
    current_seq: int | None = None
    latest: str = ""
    for event in events:
        if event.get("type") == "tool_call":
            try:
                current_seq = int(event.get("seq") or 0)
            except (TypeError, ValueError):
                current_seq = None
        elif event.get("type") == "tool_result" and current_seq is not None:
            digest = event.get("digest") or {}
            if digest.get("status") is not None or digest.get("status_code") is not None:
                node_id = by_trace.get(current_seq)
                if node_id:
                    latest = node_id
            current_seq = None
    return latest


def bind_checks_to_evidence(
    checks: list[dict[str, Any]],
    *,
    nodes: list[dict[str, Any]],
    session_dir: Path,
    events: list[dict[str, Any]] | None = None,
    produced_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Attach every check to evidence that replay will actually reproduce.

    - text/regex: prefer a produced artifact file whose content matched during
      exploration (e.g. ``report.html``), then a mapped step/observation.
    - status: bind to the node whose result carried status.
    - file_exists: inherently path-bound.
    Unbindable text/status checks are marked ``unbound`` so replay cannot pass.
    """
    session_dir = Path(session_dir)
    events = list(events or [])
    produced = {
        str(item).replace("\\", "/").lstrip("./")
        for item in (produced_paths or set())
        if item and ".." not in Path(str(item)).parts
    }
    candidates = sorted(
        produced,
        key=lambda rel: (_TEXT_EXT_PRIORITY.get(Path(rel).suffix.lower(), 9), rel),
    )
    node_ids = {str(node.get("id") or "") for node in nodes}
    latest_status_node = _status_step_from_events(events, nodes)
    bound: list[dict[str, Any]] = []
    for raw in checks:
        item = dict(raw)
        kind = str(item.get("type") or "")
        if kind == "file_exists":
            item["binding"] = "path"
            bound.append(item)
            continue
        if kind in _TEXT_CHECK_KINDS:
            artifact = str(item.get("artifact") or "").replace("\\", "/").lstrip("./")
            if artifact and artifact in produced and (session_dir / artifact).is_file():
                text = _read_text_file(session_dir / artifact)
                if text is not None and _matches_check(item, text):
                    item["artifact"] = artifact
                    item["binding"] = "artifact"
                    bound.append(item)
                    continue
            found = ""
            for rel in candidates:
                text = _read_text_file(session_dir / rel)
                if text is not None and _matches_check(item, text):
                    found = rel
                    break
            if found:
                item["artifact"] = found
                item["binding"] = "artifact"
            elif str(item.get("step_id") or "") in node_ids:
                item["binding"] = "step"
            else:
                item["binding"] = "unbound"
                item["unbound"] = True
            bound.append(item)
            continue
        if kind == "status_code":
            step_id = str(item.get("step_id") or "")
            if step_id not in node_ids and latest_status_node:
                step_id = latest_status_node
                item["step_id"] = step_id
            if step_id in node_ids:
                item["binding"] = "step"
            else:
                item["binding"] = "unbound"
                item["unbound"] = True
            bound.append(item)
            continue
        item["binding"] = "path"
        bound.append(item)
    return bound




def replay_graph(
    *,
    nodes: list[dict[str, Any]],
    registry: Any,
    sandbox: Path,
    checks: list[dict[str, Any]],
    clean: bool = False,
) -> dict[str, Any]:
    """Deterministic replay: execute atom nodes only, no LLM involved.

    With ``clean=True`` the replay runs in a fresh temporary workspace and
    removes it afterwards; generated atoms therefore cannot read explore
    artifacts/data.  Any attempted LLM call is counted and fails the replay.
    """
    import shutil
    import tempfile

    from . import llm as llm_module

    sandbox = Path(sandbox)
    cleanup = False
    if clean:
        sandbox = Path(tempfile.mkdtemp(prefix="pv-replay-clean-"))
        cleanup = True
    elif sandbox.exists():
        shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
    else:
        sandbox.mkdir(parents=True, exist_ok=True)

    llm_calls = {"count": 0}
    original_complete = llm_module.LLMClient.complete

    def _forbidden(self, *args: Any, **kwargs: Any) -> str:
        llm_calls["count"] += 1
        raise llm_module.LLMError("重放期间禁止调用 LLM")

    llm_module.LLMClient.complete = _forbidden  # type: ignore[method-assign]
    ctx = RunContext(workspace=str(sandbox), env={"FS_ROOT": str(sandbox)})
    observations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    started = time.monotonic()
    previous_cwd = Path.cwd()
    try:
        os.chdir(sandbox)
        for index, node in enumerate(nodes, 1):
            atom_name = str(node.get("atom") or "")
            step_id = str(node.get("id") or f"step{index}")
            try:
                atom = registry.get(atom_name)
                params = resolve_templates(dict(node.get("params") or {}), ctx)
                result = atom.invoke(ctx, params)
            except Exception as exc:  # noqa: BLE001 - 重放失败要落成证据
                errors.append(
                    {
                        "step_id": step_id,
                        "atom": atom_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                observations.append(
                    {
                        "index": index,
                        "step_id": step_id,
                        "tool": atom_name,
                        "params": dict(node.get("params") or {}),
                        "ok": False,
                        "result": {},
                    }
                )
                continue
            for var, spec in (node.get("capture") or {}).items():
                path = spec.get("path") if isinstance(spec, Mapping) else spec
                found = walk_path(result, split_path(str(path or var)))
                if found is not MISSING:
                    ctx.set(str(var), found)
            observations.append(
                {
                    "index": index,
                    "step_id": step_id,
                    "tool": atom_name,
                    "params": dict(node.get("params") or {}),
                    "ok": True,
                    "result": dict(result),
                }
            )
    finally:
        os.chdir(previous_cwd)
        llm_module.LLMClient.complete = original_complete  # type: ignore[method-assign]

    unbound = [
        str(item.get("id") or item.get("type"))
        for item in checks
        if item.get("unbound") or item.get("binding") == "unbound"
    ]
    if unbound:
        errors.append(
            {
                "step_id": "",
                "atom": "",
                "error": "未绑定证据的检查，不能判定 replay_passed: " + ", ".join(unbound),
            }
        )
    results = check_observations(checks, observations=observations, base_dir=sandbox)
    passed = (
        not errors
        and not unbound
        and bool(checks)
        and all(item["ok"] for item in results)
        and llm_calls["count"] == 0
    )
    payload = {
        "status": "passed" if passed else "failed",
        "checks": results,
        "unbound": unbound,
        "errors": errors,
        "llm_calls": llm_calls["count"],
        "clean_workspace": cleanup,
        "workspace": str(sandbox),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "nodes": [node.get("id") for node in nodes],
    }
    if cleanup:
        shutil.rmtree(sandbox, ignore_errors=True)
    return payload
