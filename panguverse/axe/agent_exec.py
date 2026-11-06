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
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..core.context import MISSING, RunContext, split_path, walk_path

MAX_SCRIPT_STDOUT = 20_000
MAX_SCRIPT_STDERR = 4_000
MAX_ARTIFACT_BYTES = 200_000
MAX_ARTIFACT_TEXT = 5_000
DEFAULT_SCRIPT_TIMEOUT = 60.0
MAX_SCRIPT_TIMEOUT = 120.0
MAX_THOUGHT = 2_000
MAX_EVIDENCE = 500

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
        self.status = status or {"bad_request": 400, "not_found": 404}.get(code, 500)
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# 工具目录
# --------------------------------------------------------------------------- #
def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


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


def build_tool_catalog(registry: Any, *, allow_scripts: bool = True) -> list[dict[str, Any]]:
    """Tool catalog = registry atoms + built-in exploration tools."""
    catalog: list[dict[str, Any]] = []
    try:
        atoms = list(registry.list())
    except AttributeError:
        atoms = list(registry or [])
    for atom in atoms:
        entry = tool_entry_from_atom(atom)
        if entry["name"]:
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
            spec["value"] = str(data.get("value") or "")
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
    base = base_dir / "artifacts"
    if not base.is_dir():
        return None
    candidates = [
        path
        for path in base.rglob("*")
        if path.is_file() and path.suffix.lower() in {".txt", ".md", ".html", ".htm", ".json"}
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
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

    return False, f"未知检查类型: {kind}"


def _text_for(
    spec: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    base_dir: Path,
    extra_texts: list[str] | None = None,
) -> str | None:
    if _has_step_binding(spec):
        if observation is None:
            return None
        return _text_of_result(observation.get("result") or {})
    artifact = spec.get("artifact")
    if artifact:
        try:
            target = safe_path(base_dir, str(artifact), field="artifact")
        except AgentExecError:
            return None
        if target.is_file():
            return target.read_text(encoding="utf-8", errors="replace")
        return None
    if observation is not None:
        text = _text_of_result(observation.get("result") or {})
        if text:
            return text
    for text in reversed(extra_texts or []):
        if text and text.strip():
            return text
    return _latest_artifact_text(base_dir)


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
    ) -> None:
        self.sandbox = Path(sandbox)
        self.registry = registry
        self.llm = llm
        self.emit = emit
        self.allow_scripts = allow_scripts
        self.max_steps = max(1, min(50, int(max_steps)))
        self.total_timeout = max(1.0, float(total_timeout))
        self.clock = clock
        self.catalog = build_tool_catalog(registry, allow_scripts=allow_scripts)
        self.tool_names = {entry["name"] for entry in self.catalog}
        self.observations: list[dict[str, Any]] = []
        self.scripts: list[dict[str, Any]] = []
        self.script_outputs: list[str] = []
        self.pending_feedback = ""
        self.last_tool_sig = ""
        self.repeat_count = 0
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
        checks_spec = normalize_checks(success_criteria)
        feedback = ""
        saw_final = False
        last_checks: list[dict[str, Any]] = []
        actions_used = 0
        llm_turns = 0
        while actions_used < self.max_steps:
            llm_turns += 1
            if llm_turns > self.max_steps + 6:
                return self._finish(
                    "budget_exceeded",
                    checks=last_checks,
                    summary={
                        "reason": f"LLM 回合超限（>{self.max_steps + 6}），停止以避免空转",
                        "steps": actions_used,
                    },
                )
            if self.clock() - self.started >= self.total_timeout:
                return self._finish(
                    "budget_exceeded",
                    checks=last_checks,
                    summary={
                        "reason": f"总时长超过 {self.total_timeout:.0f}s",
                        "steps": actions_used,
                    },
                )
            decision = self._decide(intent, checks_spec, actions_used + 1, feedback)
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
                specs = checks_spec or normalize_checks(proposed)
                if not specs:
                    raise AgentExecError(
                        "bad_request",
                        "成功标准必须落成可执行检查（text_contains/status_code/file_exists/regex）",
                    )
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
        status = "failed" if saw_final else "budget_exceeded"
        return self._finish(
            status,
            checks=last_checks,
            summary={
                "reason": f"达到 max_steps={self.max_steps}（仅计 tool/script 动作）",
                "steps": actions_used,
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
        )
        recent = [
            {
                "step": index,
                "tool": item["tool"],
                "ok": item["ok"],
                "digest": item["digest"],
            }
            for index, item in enumerate(self.observations, 1)
        ]
        payload = {
            "intent": intent,
            "step": step_no,
            "max_steps": self.max_steps,
            "remaining_seconds": round(
                max(0.0, self.total_timeout - (self.clock() - self.started)), 1
            ),
            "tools": self.catalog,
            "observations": recent[-6:],
            "criteria": criteria,
            "last_failure": feedback,
            "instructions": (
                "给出下一步决策。若已具备证据，用 final + 确定性 checks 收尾。"
                if step_no > 1
                else "先观察真实世界：打开目标或读取数据。"
            ),
        }
        user = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            # 真实模型有时会在 JSON 前后夹带解释或多个对象；complete_json 会整段
            # 解析失败。这里取原始文本并抽取真正的 action 对象。
            if hasattr(self.llm, "complete"):
                decision = _extract_decision(self.llm.complete(system, user))
            else:
                decision = self.llm.complete_json(system, user)
        except Exception as exc:  # noqa: BLE001 - LLM 故障必须显式失败
            raise AgentExecError(
                "llm_error", f"LLM 调用失败: {type(exc).__name__}: {exc}", status=502
            ) from exc
        if not isinstance(decision, dict):
            raise AgentExecError("llm_invalid", "LLM 未返回 JSON 对象", status=502)
        return decision

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
        signature = tool + "\n" + json.dumps(
            dict(params), ensure_ascii=False, sort_keys=True, default=str
        )
        if signature == self.last_tool_sig:
            self.repeat_count += 1
        else:
            self.last_tool_sig = signature
            self.repeat_count = 1
        if self.repeat_count >= 2:
            message = "同一 tool+params 已连续重复 2 次：已执行过，请改用 final 或其他动作"
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
            ctx = RunContext(workspace=str(self.sandbox))
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
        target = safe_path(self.sandbox, path_text, field="path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
        outcome = _run_python(target, cwd=self.sandbox, timeout=DEFAULT_SCRIPT_TIMEOUT)
        rel = str(target.relative_to(self.sandbox.resolve()))
        artifacts = self._write_artifacts(
            step_no,
            f"script_{Path(rel).stem}",
            {},
            {"stdout": outcome.get("stdout", ""), "stderr": outcome.get("stderr", "")},
        )
        event = {
            "path": rel,
            "code_sha256": code_sha,
            "exit_code": outcome["exit_code"],
            "ok": bool(outcome["ok"]),
            "stdout": outcome["stdout"],
            "stderr": outcome["stderr"],
            "duration_ms": outcome["duration_ms"],
            "artifacts": artifacts,
            "step": step_no,
        }
        self.scripts.append(
            {"path": rel, "code_sha256": code_sha, "exit_code": outcome["exit_code"], "ok": bool(outcome["ok"])}
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
) -> dict[str, Any]:
    """Turn a successful trace into graph nodes/edges plus pending scripts."""
    calls: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    scripts: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind == "tool_call":
            current = {
                "seq": int(event.get("seq") or 0),
                "tool": str(event.get("tool") or ""),
                "params": dict(event.get("params_template") or event.get("params") or {}),
                "actual_params": dict(event.get("params") or {}),
                "ok": False,
                "digest": {},
            }
        elif kind == "tool_result" and current is not None:
            if current["tool"] == event.get("tool"):
                current["ok"] = bool(event.get("ok"))
                current["digest"] = dict(event.get("digest") or {})
                calls.append(current)
                current = None
        elif kind == "script":
            scripts.append(event)

    successful = [item for item in calls if item["ok"] and item["tool"]]
    if not successful:
        raise AgentExecError("bad_request", "探索轨迹没有成功的原子调用可固化")

    call_order = {call["seq"]: index for index, call in enumerate(successful, 1)}
    order_to_node: dict[int, str] = {
        index: f"s{index}_{re.sub(r'[^A-Za-z0-9_]+', '_', call['tool'])}"
        for index, call in enumerate(successful, 1)
    }
    seq_to_node = {call["seq"]: order_to_node[i] for i, call in enumerate(successful, 1)}
    nodes: list[dict[str, Any]] = []
    for index, call in enumerate(successful, 1):
        node_id = order_to_node[index]
        nodes.append(
            {
                "id": node_id,
                "atom": call["tool"],
                "name": call["tool"],
                "description": f"来自探索 {eid} 第 {index} 步（trace seq={call['seq']}）",
                "params": {},
                "capture": {},
                "when": None,
                "foreach": None,
                "as": "item",
                "retry": None,
                "continue_on_failure": False,
                "tags": ["explore"],
                "optional": False,
                "extra": {"explore_id": eid, "trace_seq": call["seq"]},
                "case_id": "explore",
                "case_name": name or intent or "explore",
                "origin": "explore",
                "locked": False,
            }
        )

    # Second pass: params may reference outputs of earlier nodes, so captures
    # must be attached to their producer node before templates are built.
    captures_by_node: dict[str, dict[str, Any]] = {node["id"]: node["capture"] for node in nodes}
    node_by_id = {node["id"]: node for node in nodes}
    for index, call in enumerate(successful, 1):
        node_id = order_to_node[index]
        params = {}
        for key, value in call["params"].items():
            params[key] = _crystallize_param(
                value,
                nodes_by_seq=seq_to_node,
                order_to_node=order_to_node,
                capture_by_node=captures_by_node,
                captures=node_by_id[node_id]["capture"],
                current_node=node_id,
            )
        node_by_id[node_id]["params"] = params

    edges: list[dict[str, Any]] = []
    for left, right in zip(nodes, nodes[1:]):
        edges.append(
            {
                "id": f"e-{left['id']}->{right['id']}:order",
                "from": left["id"],
                "to": right["id"],
                "via": None,
                "kind": "order",
                "order_only": True,
            }
        )

    replay_checks: list[dict[str, Any]] = []
    for spec in checks:
        item = dict(spec)
        step = item.get("step")
        if step not in (None, ""):
            try:
                node_id = order_to_node.get(int(step))
            except (TypeError, ValueError):
                node_id = None
            if node_id:
                item["step_id"] = node_id
        artifact = str(item.get("artifact") or "")
        match = _ARTIFACT_SEQ_RE.match(artifact)
        if match:
            node_id = call_order.get(int(match.group(1)))
            if node_id:
                item["step_id"] = order_to_node[node_id]
        replay_checks.append(item)

    pending_atoms = [
        {
            "name": re.sub(r"[^A-Za-z0-9_]+", "_", Path(str(item.get("path") or "script")).stem),
            "path": str(item.get("path") or ""),
            "code_sha256": str(item.get("code_sha256") or ""),
            "exit_code": item.get("exit_code"),
            "status": "pending",
            "reason": "P0 只固化已有原子，脚本步骤待 P1 封装",
            "trace_seq": item.get("seq"),
        }
        for item in scripts
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "checks": replay_checks,
        "pending_atoms": pending_atoms,
        "scripts": scripts,
        "intent": intent,
        "name": name or intent,
    }


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


def replay_graph(
    *,
    nodes: list[dict[str, Any]],
    registry: Any,
    sandbox: Path,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic replay: execute atom nodes only, no LLM involved."""
    import shutil

    sandbox = Path(sandbox)
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(workspace=str(sandbox))
    observations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    started = time.monotonic()
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
    results = check_observations(checks, observations=observations, base_dir=sandbox)
    passed = not errors and all(item["ok"] for item in results) and bool(checks)
    return {
        "status": "passed" if passed else "failed",
        "checks": results,
        "errors": errors,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "nodes": [node.get("id") for node in nodes],
    }
