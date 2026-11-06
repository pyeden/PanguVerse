"""LLM-driven crystallization: semantic abstraction + orchestration.

The old crystallizer copied the trace: every successful script became a
``generated atom`` whose module embedded the recorded script source (including
any hard-coded business data) and whose inputs were snapshotted from the explore
sandbox.  That is a recording, not a workflow.

This module implements the second stage of the explore-first loop:

1. **Semantic induction** (:func:`plan_crystallization`): the LLM receives a
   compact digest of the successful trace (steps/tools/params/artifacts/check
   evidence), the atom catalog with port contracts, and the successful script
   sources.  It returns a strict JSON plan: which nodes to keep, which scripts
   to abstract/drop/reuse, and which semantic checks prove success.
2. **Atom abstraction** (:func:`abstract_script`): the LLM rewrites each script
   to keep into a parameterized module: no hard-coded business data, no explore
   snapshot paths, no absolute paths; inputs come from typed ports.
3. **Generality gate** (:func:`generality_check`): static AST checks + a run in
   a clean temporary workspace with different inputs.  A module that still
   embeds recordings or reads the explore snapshot is rejected and reported as
   ``pending reason=not_general``.
4. **Orchestration** (:func:`build_graph_from_plan`): nodes are atoms (existing
   or newly abstracted), data edges come from real parameter references, checks
   become visible ``assert_*`` nodes, and at least one content-semantic check is
   mandatory (item count / regex / freshness).

Everything after the plan is deterministic; replay never calls an LLM.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import itertools
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.workspace_packs import PACKS_REL

MAX_PLAN_NODES = 8
MAX_DIGEST_SCRIPTS = 6
MAX_SOURCE_CHARS = 12_000
MAX_TOTAL_SOURCE_CHARS = 48_000
SNAKE_RE = re.compile(r"[^a-zA-Z0-9]+")
REF_RE = re.compile(r"\$\{([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)\}")
ABS_PATH_RE = re.compile(r"^(?:/(?:Users|home|tmp|private|var|Volumes)/|[A-Za-z]:\\)")
SEMANTIC_CHECK_TYPES = ("min_items", "freshness", "regex")
OUTPUT_EXTS = (".html", ".htm", ".md", ".json", ".csv", ".txt")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class AbstractionError(Exception):
    """Crystallization cannot continue; carries an API-facing code/status."""

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
        self.status = status
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# digest / catalog
# --------------------------------------------------------------------------- #
def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "\n...[truncated]"


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_signature_params(source: str) -> tuple[set[str], set[str]] | None:
    """从 ``run()`` 源码签名推导（带默认值的参数, 全部具名参数）。

    契约里的可选性必须跟真实函数签名一致：``def run(..., limit=30)``
    的 ``limit`` 是可选的，即使抽象器忘了在名字或类型上加 ``?``。
    解析失败或没有 ``run()`` 时返回 ``None``，调用方回退到旧白名单。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "run":
            continue
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        padded = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        optional = {arg.arg for arg, default in zip(positional, padded, strict=False) if default is not None}
        optional.update(
            arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False) if default is not None
        )
        named = {arg.arg for arg in positional} | {arg.arg for arg in args.kwonlyargs}
        return optional, named
    return None


def _safe_rel(raw: Any) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or ".." in Path(text).parts:
        return ""
    return text


def build_digest(
    events: Iterable[Mapping[str, Any]],
    *,
    session_dir: Path,
    checks: list[dict[str, Any]],
    intent: str,
) -> dict[str, Any]:
    """Compact, evidence-rich view of a successful trace."""
    from . import synthesize

    events = list(events)
    steps = synthesize.collect_steps(events, session_dir=Path(session_dir))
    atom_steps: list[dict[str, Any]] = []
    script_steps: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for step in steps:
        if not step.ok:
            failed.append(
                {
                    "seq": step.seq,
                    "kind": step.kind,
                    "name": step.name,
                    "path": step.script_path,
                    "reason": step.error[:300] or "ok=false",
                }
            )
            continue
        if step.kind == "atom":
            atom_steps.append(
                {
                    "seq": step.seq,
                    "action_index": step.action_index,
                    "atom": step.name,
                    "params": step.params or step.actual,
                    "observed_params": step.actual,
                    "result_keys": step.keys,
                    "status": step.digest.get("status", step.digest.get("status_code")),
                }
            )
            continue
        script_steps.append(
            {
                "seq": step.seq,
                "path": step.script_path,
                "code_sha256": step.code_sha,
                "output_paths": step.output_paths,
                "reads": step.reads,
                "source": step.source or "",
                "source_chars": len(step.source or ""),
                "stdout_head": _clip(_event_stdout(events, step.seq), 1200),
            }
        )
    # Keep the latest successful version per script path, then bound the payload.
    by_path: dict[str, dict[str, Any]] = {}
    for item in script_steps:
        by_path[str(item["path"])] = item
    ordered = sorted(by_path.values(), key=lambda item: int(item["seq"]))
    selected: list[dict[str, Any]] = []
    total = 0
    for item in reversed(ordered):
        if len(selected) >= MAX_DIGEST_SCRIPTS:
            break
        source = str(item.get("source") or "")
        if total + len(source) > MAX_TOTAL_SOURCE_CHARS and selected:
            continue
        total += len(source)
        selected.append(item)
    selected.reverse()
    return {
        "intent": intent,
        "success_checks": [dict(item) for item in checks],
        "atom_steps": atom_steps,
        "scripts": selected,
        "failed_steps": failed[:20],
    }


def _event_stdout(events: list[Mapping[str, Any]], seq: int) -> str:
    for event in events:
        if event.get("type") == "script" and int(event.get("seq") or 0) == int(seq):
            return str(event.get("stdout") or "")
    return ""


def atom_catalog(registry: Any, *, limit: int = 80) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if registry is None:
        return items
    try:
        atoms = list(registry.list())
    except Exception:  # noqa: BLE001 - injected registries may be partial
        return items
    for atom in sorted(atoms, key=lambda item: str(getattr(item, "name", "")))[:limit]:
        name = str(getattr(atom, "name", "") or "")
        if not name:
            continue
        items.append(
            {
                "name": name,
                "description": _clip(getattr(atom, "description", ""), 160),
                "inputs": dict(getattr(atom, "inputs", {}) or {}),
                "outputs": dict(getattr(atom, "outputs", {}) or {}),
                "effects": list(getattr(atom, "effects", ()) or ()),
                "idempotent": getattr(atom, "idempotent", None),
            }
        )
    return items


# --------------------------------------------------------------------------- #
# LLM helpers
# --------------------------------------------------------------------------- #
def _llm_available(llm: Any) -> bool:
    if llm is None:
        return False
    available = getattr(llm, "available", True)
    return available is not False


def _extract_json(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    # Fall back to the first balanced top-level object.
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_str:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_str = False
                continue
            if char == '"':
                in_str = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(data, dict):
                        return data
                    break
        start = text.find("{", start + 1)
    return {}


def _complete_json(llm: Any, system: str, user: str) -> dict[str, Any]:
    if not _llm_available(llm):
        raise AbstractionError(
            "llm_required_for_crystallize",
            "固化需要 LLM 做语义归纳（PANGUVERSE_API_KEY / OPENAI_API_KEY）；未配置时不做假固化",
            status=503,
        )
    try:
        if hasattr(llm, "complete_json"):
            data = llm.complete_json(system, user)
        else:
            data = _extract_json(llm.complete(system, user))
    except AbstractionError:
        raise
    except Exception as exc:
        message = str(exc)
        unavailable = "未配置" in message or "api key" in message.lower()
        raise AbstractionError(
            "llm_required_for_crystallize" if unavailable else "llm_failed",
            f"固化 LLM 调用失败：{type(exc).__name__}: {message[:400]}",
            status=503 if unavailable else 502,
        ) from exc
    if not isinstance(data, dict) or not data:
        raise AbstractionError("crystallize_plan_invalid", "LLM 未返回 JSON 对象")
    return data


PLAN_SYSTEM = """你是 PanguVerse 的「流程固化器」。输入是一次已经跑通的探索轨迹摘要、原子清单（含端口契约）和成功脚本源码。
你的职责不是录制轨迹，而是归纳：这个任务真正依赖哪些步骤？哪些脚本可以抽象成通用原子？怎样用确定性检查证明本次产物有效？

硬性要求：
1. 只保留必要步骤（最多 8 个节点），删除探测、重复调用、一次性胶水和失败步骤。
2. 优先复用已有原子（atom 用清单里的名字）。脚本只有在包含可复用逻辑且产出被检查的产物时，才允许抽象成新原子（atom 写 "new:<蛇形名>"），并在 scripts 里 action=abstract。
3. 原子参数必须来自真实引用：用 ${节点id.端口} 表示上游输出；不要编造不存在的端口。禁止 order 胶水依赖。
4. 检查必须是「本次产物」的语义检查，至少包含一个 min_items / freshness / regex；file_exists 不能作为唯一检查。
5. 禁止把硬编码数据、探索快照路径或绝对路径带进新原子；如果某个成功脚本只是录制数据，action 必须是 drop，并写明原因。
6. 只输出一个 JSON 对象，不要 markdown 代码块，不要解释。

JSON schema：
{
  "summary": "一句话说明成功依赖什么",
  "nodes": [
    {"id": "n1", "role": "fetch|extract|render|transform|assert", "atom": "已有原子名或 new:新原子名",
     "trace_seq": 5, "params": {"url": "https://...", "source": "${n1.text}"}, "outputs": ["port"]}
  ],
  "scripts": [
    {"seq": 40, "path": "scripts/x.py", "action": "abstract|drop|reuse", "as": "新原子名",
     "reason": "为什么保留/丢弃"}
  ],
  "checks": [
    {"id": "items>=5", "type": "min_items", "artifact": "news_data.json", "min_items": 5},
    {"id": "fresh", "type": "freshness", "artifact": "news_data.json", "max_age_seconds": 86400},
    {"id": "has-title", "type": "regex", "artifact": "report.html", "pattern": "热点"}
  ]
}

注意：
- nodes[].params 省略时，对已有原子表示复用轨迹中的真实参数；对 new: 原子必须给出参数。
- checks[].artifact 必须是某个节点声明的输出文件（相对路径）。
- scripts 必须覆盖摘要里所有成功脚本：保留、抽象或明确 drop。"""


def _plan_user_prompt(digest: Mapping[str, Any], catalog: list[dict[str, Any]]) -> str:
    return (
        "轨迹摘要：\n"
        + json.dumps(digest, ensure_ascii=False, indent=2)
        + "\n\n可用原子（端口契约）：\n"
        + json.dumps(catalog, ensure_ascii=False, indent=2)
        + "\n\n请输出固化计划 JSON。"
    )


def _snake(name: str) -> str:
    clean = SNAKE_RE.sub("_", str(name or "")).strip("_").lower()
    if not clean:
        clean = "script_atom"
    return clean if not clean[0].isdigit() else f"atom_{clean}"


def _normalize_plan(
    raw: Mapping[str, Any],
    *,
    digest: Mapping[str, Any],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    existing = {item["name"] for item in catalog}
    script_by_seq = {int(item["seq"]): item for item in digest.get("scripts") or []}
    successful_seqs = set(script_by_seq)

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise AbstractionError("crystallize_plan_invalid", "计划缺少 nodes")
    if len(raw_nodes) > MAX_PLAN_NODES:
        raise AbstractionError(
            "crystallize_plan_invalid",
            f"计划节点数 {len(raw_nodes)} 超过上限 {MAX_PLAN_NODES}；请合并/删除胶水步骤",
        )
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_nodes, 1):
        if not isinstance(item, Mapping):
            raise AbstractionError("crystallize_plan_invalid", f"nodes[{index}] 必须是对象")
        node_id = str(item.get("id") or f"n{index}").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", node_id):
            raise AbstractionError("crystallize_plan_invalid", f"节点 id 非法: {node_id}")
        if node_id in seen_ids:
            raise AbstractionError("crystallize_plan_invalid", f"节点 id 重复: {node_id}")
        seen_ids.add(node_id)
        atom = str(item.get("atom") or "").strip()
        if not atom:
            raise AbstractionError("crystallize_plan_invalid", f"节点 {node_id} 缺少 atom")
        params = item.get("params") if isinstance(item.get("params"), Mapping) else {}
        trace_seq = item.get("trace_seq")
        try:
            trace_seq_int = int(trace_seq) if trace_seq not in (None, "") else None
        except (TypeError, ValueError):
            trace_seq_int = None
        nodes.append(
            {
                "id": node_id,
                "role": str(item.get("role") or "").strip()[:40],
                "atom": atom,
                "trace_seq": trace_seq_int,
                "params": dict(params),
                "outputs": [str(port) for port in (item.get("outputs") or []) if port],
            }
        )

    raw_scripts = raw.get("scripts")
    scripts: list[dict[str, Any]] = []
    abstract_names: set[str] = set()
    covered: set[int] = set()
    for item in raw_scripts if isinstance(raw_scripts, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if seq not in successful_seqs or seq in covered:
            continue
        covered.add(seq)
        action = str(item.get("action") or "drop").strip().lower()
        if action not in ("abstract", "drop", "reuse"):
            action = "drop"
        name = _snake(item.get("as") or "")
        if action == "abstract":
            abstract_names.add(name)
        scripts.append(
            {
                "seq": seq,
                "path": str(item.get("path") or script_by_seq[seq].get("path") or ""),
                "action": action,
                "as": name if action == "abstract" else "",
                "reason": _clip(item.get("reason") or "", 300),
            }
        )
    # Anything the LLM forgot is dropped explicitly; nothing leaks by default.
    for seq in sorted(successful_seqs - covered):
        scripts.append(
            {
                "seq": seq,
                "path": str(script_by_seq[seq].get("path") or ""),
                "action": "drop",
                "as": "",
                "reason": "计划未提及，默认丢弃（拒绝把录制数据带进流程）",
            }
        )
    scripts.sort(key=lambda item: item["seq"])

    for node in nodes:
        atom = node["atom"]
        if atom.startswith("new:"):
            name = _snake(atom.split(":", 1)[1])
            if name not in abstract_names:
                raise AbstractionError(
                    "crystallize_plan_invalid",
                    f"节点 {node['id']} 引用了未声明抽象的新原子: {atom}",
                )
        elif atom not in existing:
            raise AbstractionError(
                "crystallize_plan_invalid",
                f"节点 {node['id']} 的原子不在清单中: {atom}",
            )

    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        raise AbstractionError("crystallize_plan_invalid", "计划缺少 checks")
    normalized_checks: list[dict[str, Any]] = []
    for item in checks:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "").strip()
        if kind not in ("min_items", "freshness", "regex", "text_contains", "status_code", "file_exists"):
            continue
        entry = dict(item)
        entry["id"] = str(entry.get("id") or f"{kind}_{len(normalized_checks) + 1}")
        entry["type"] = kind
        if kind in ("min_items", "freshness", "regex", "text_contains"):
            artifact = _safe_rel(entry.get("artifact") or entry.get("path"))
            node_ref = str(entry.get("node") or entry.get("step_id") or "")
            if not artifact and not node_ref:
                raise AbstractionError(
                    "crystallize_plan_invalid",
                    f"检查 {entry['id']} 缺少合法 artifact 或 node",
                )
            if artifact:
                entry["artifact"] = artifact
            else:
                entry["node"] = node_ref
            if kind == "min_items":
                try:
                    entry["min_items"] = int(entry.get("min_items") or entry.get("min") or 1)
                except (TypeError, ValueError):
                    entry["min_items"] = 1
                if entry["min_items"] < 1:
                    entry["min_items"] = 1
            if kind == "freshness":
                try:
                    entry["max_age_seconds"] = int(
                        entry.get("max_age_seconds") or entry.get("max_age") or 86400
                    )
                except (TypeError, ValueError):
                    entry["max_age_seconds"] = 86400
            if kind == "regex" and not str(entry.get("pattern") or "").strip():
                raise AbstractionError(
                    "crystallize_plan_invalid", f"检查 {entry['id']} 缺少 pattern"
                )
        normalized_checks.append(entry)
    if not normalized_checks:
        raise AbstractionError("crystallize_plan_invalid", "checks 全部非法")
    semantic = [
        entry
        for entry in normalized_checks
        if entry["type"] in SEMANTIC_CHECK_TYPES
        and (entry.get("artifact") or entry.get("node"))
    ]
    if not semantic:
        raise AbstractionError(
            "crystallize_plan_invalid",
            "至少需要一个内容语义检查（min_items / freshness / regex），不能只有 file_exists",
        )

    _validate_refs(nodes)
    return {
        "summary": _clip(raw.get("summary") or "", 500),
        "nodes": nodes,
        "scripts": scripts,
        "checks": normalized_checks,
        "raw": dict(raw),
    }


def _validate_refs(nodes: list[dict[str, Any]]) -> None:
    by_id = {node["id"]: node for node in nodes}
    order = {node["id"]: index for index, node in enumerate(nodes)}
    for node in nodes:
        for value in _walk_strings(node["params"]):
            for match in REF_RE.finditer(value):
                producer = match.group(1)
                if producer == "last" or re.fullmatch(r"step\d+", producer):
                    # Legacy trace refs are translated later; validate there.
                    continue
                if producer not in by_id:
                    raise AbstractionError(
                        "crystallize_plan_invalid",
                        f"节点 {node['id']} 引用了不存在的上游 {producer}",
                    )
                if order[producer] >= order[node["id"]]:
                    raise AbstractionError(
                        "crystallize_plan_invalid",
                        f"节点 {node['id']} 只能引用更早节点（{producer}）",
                    )


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def plan_crystallization(
    llm: Any,
    *,
    intent: str,
    digest: Mapping[str, Any],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    system = PLAN_SYSTEM
    user = _plan_user_prompt(digest, catalog)
    raw = _complete_json(llm, system, user)
    try:
        return _normalize_plan(raw, digest=digest, catalog=catalog)
    except AbstractionError as first:
        if first.code != "crystallize_plan_invalid":
            raise
        retry_system = system + "\n\n上一次输出未通过校验：" + first.message + "。请修正并只输出 JSON。"
        raw = _complete_json(llm, retry_system, user)
        return _normalize_plan(raw, digest=digest, catalog=catalog)


# --------------------------------------------------------------------------- #
# script abstraction
# --------------------------------------------------------------------------- #
ABSTRACT_SYSTEM = """你是 PanguVerse 的「原子抽象器」。输入是一个在探索中跑通的 Python 脚本、它的输入输出证据，以及目标契约。
把脚本改写成**可复用原子模块**，而不是保留录制结果。

硬性要求（违反即拒绝固化）：
1. 模块必须定义 `run(ctx=None, **inputs)`（或显式列出 inputs 中的参数）；返回 dict，把输出端口映射到相对路径。
2. 去掉所有写死的业务数据（新闻列表、固定标题、录制结果）；数据必须来自输入参数 `source`/`url`/`options` 或运行时抓取。
3. 禁止读取探索会话目录/快照：不得出现 .panguverse、explore/e-、session.json、success.json、trace.jsonl、artifacts/ 等路径。
4. 禁止绝对路径；输出写到 `output_dir`（默认 "."）下，相对路径。
5. 优先接受 `source` 输入（HTML 或 JSON 文本）：如果 source 非空，必须从 source 解析，不得联网。url 仅在 source 为空时使用。
6. 输出文件必须包含数据时间戳（如 meta.fetched_at），供 freshness 检查。
7. 不依赖 panguverse 内部模块，只用标准库；失败时抛异常，不要静默返回空数据冒充成功。
8. 只输出一个 JSON 对象，不要 markdown。
9. 同时给出**与你的输出格式完全一致**的语义检查 checks（至少一个 min_items 或 freshness）：
   artifact 必须是 output_paths 里的相对路径，pattern 必须匹配你实际写出的 HTML/JSON 文本。
10. 解析 source 时必须递归查找条目列表，不能假设 data 是 list；常见结构包括
    {"data": {"cards": [{"content": [{"query": "...", "hotScore": 123}]}]}}。
    条目标题字段可能是 word/query/title/name。
11. 禁止用 `%` 运算符格式化可能包含 CSS 百分比的模板。CSS（如
    `linear-gradient(90deg, #fff 0%, #000 45%)`）里的 `%` 会被当成格式说明符，
    运行时抛 `ValueError: unsupported format character ','`。模板文本中出现 CSS `%`
    时，必须改用 f-string、`str.format(...)` 或 `str.replace(...)` 占位替换；
    绝不能写 `HTML_TEMPLATE % {...}` / `"%s" % ...`。

JSON schema：
{
  "description": "一句话说明原子做什么",
  "source": "完整 Python 模块源码（含 run 函数）",
  "contract": {
    "inputs": {"source": "string?", "url": "string?", "output_dir": "string?"},
    "outputs": {"items": "array", "report_html": "string"},
    "output_paths": {"report_html": "report.html"},
    "effects": ["read", "write"],
    "idempotent": true,
    "failure_class": "transient"
  },
  "checks": [
    {"id": "items>=5", "type": "min_items", "artifact": "news_data.json", "min_items": 5},
    {"id": "fresh", "type": "freshness", "artifact": "news_data.json", "max_age_seconds": 86400},
    {"id": "has-title", "type": "regex", "artifact": "report.html", "pattern": "热点"}
  ]
}"""


def _abstract_user_prompt(
    *,
    intent: str,
    script: Mapping[str, Any],
    plan_node: Mapping[str, Any] | None,
    checks: list[dict[str, Any]],
) -> str:
    payload = {
        "intent": intent,
        "script": {
            "seq": script.get("seq"),
            "path": script.get("path"),
            "output_paths": script.get("output_paths"),
            "reads": script.get("reads"),
            "stdout_head": script.get("stdout_head"),
            "source": _clip(script.get("source"), MAX_SOURCE_CHARS),
        },
        "planned_role": plan_node.get("role") if plan_node else "",
        "expected_checks": checks,
    }
    return (
        "待抽象脚本：\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n请输出抽象后的原子 JSON。"
    )


def abstract_script(
    llm: Any,
    *,
    intent: str,
    script: Mapping[str, Any],
    plan_node: Mapping[str, Any] | None,
    checks: list[dict[str, Any]],
    feedback: str = "",
) -> dict[str, Any]:
    user = _abstract_user_prompt(
        intent=intent, script=script, plan_node=plan_node, checks=checks
    )
    if feedback:
        user += "\n\n上一次抽象的问题（必须修复）：\n" + feedback
    raw = _complete_json(llm, ABSTRACT_SYSTEM, user)
    source = raw.get("source")
    contract = raw.get("contract")
    if not isinstance(source, str) or "def run" not in source:
        raise AbstractionError(
            "crystallize_abstraction_invalid",
            f"脚本 {script.get('path')} 的抽象结果缺少 run()",
            details={"seq": script.get("seq")},
        )
    if not isinstance(contract, Mapping):
        raise AbstractionError(
            "crystallize_abstraction_invalid",
            f"脚本 {script.get('path')} 的抽象结果缺少 contract",
            details={"seq": script.get("seq")},
        )
    inputs = contract.get("inputs") if isinstance(contract.get("inputs"), Mapping) else {}
    outputs = contract.get("outputs") if isinstance(contract.get("outputs"), Mapping) else {}
    output_paths = (
        contract.get("output_paths") if isinstance(contract.get("output_paths"), Mapping) else {}
    )
    clean_outputs: dict[str, str] = {}
    for key, value in outputs.items():
        clean_outputs[str(key)] = str(value or "string")
    clean_paths: dict[str, str] = {}
    for key, value in output_paths.items():
        rel = _safe_rel(value)
        if rel and rel.lower().endswith(OUTPUT_EXTS):
            clean_paths[str(key)] = rel
    if not clean_outputs and clean_paths:
        clean_outputs = dict.fromkeys(clean_paths, "string")
    if not clean_outputs:
        raise AbstractionError(
            "crystallize_abstraction_invalid",
            f"脚本 {script.get('path')} 未声明输出端口",
            details={"seq": script.get("seq")},
        )
    optional_inputs = {
        "source",
        "html",
        "text",
        "url",
        "output_dir",
        "options",
        "input_dir",
        "input_files",
    }
    signature = _run_signature_params(source)
    signature_optional, signature_named = signature if signature else (set(), set())
    normalized_inputs: dict[str, str] = {}
    for key, value in inputs.items():
        name = str(key)
        type_name = str(value)
        base = name.rstrip("?")
        optional = (
            name.endswith("?")
            or type_name.strip().endswith("?")
            or base in signature_optional
            # 名字不在 run() 签名里时保留旧白名单兜底；签名里声明的参数
            # 严格按「有没有默认值」判断，不能被白名单误标成可选。
            or (base not in signature_named and base in optional_inputs)
        )
        if optional and not name.endswith("?"):
            name = f"{name}?"
        normalized_inputs[name] = type_name
    checks: list[dict[str, Any]] = []
    known_paths = list(clean_paths.values())
    for item in raw.get("checks") if isinstance(raw.get("checks"), list) else []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "")
        artifact = _safe_rel(item.get("artifact") or item.get("path"))
        if not artifact:
            continue
        if artifact not in known_paths:
            by_name = [rel for rel in known_paths if Path(rel).name == Path(artifact).name]
            if len(by_name) == 1:
                artifact = by_name[0]
            else:
                continue
        entry: dict[str, Any] = {
            "id": str(item.get("id") or f"{kind}_{len(checks) + 1}"),
            "type": kind,
            "artifact": artifact,
        }
        if kind == "min_items":
            try:
                entry["min_items"] = max(1, int(item.get("min_items") or item.get("min") or 1))
            except (TypeError, ValueError):
                entry["min_items"] = 1
        elif kind == "freshness":
            try:
                entry["max_age_seconds"] = int(
                    item.get("max_age_seconds") or item.get("max_age") or 86400
                )
            except (TypeError, ValueError):
                entry["max_age_seconds"] = 86400
            if item.get("pattern"):
                entry["pattern"] = str(item["pattern"])
        elif kind == "regex":
            pattern = str(item.get("pattern") or "").strip()
            if not pattern:
                continue
            entry["pattern"] = pattern
        else:
            continue
        checks.append(entry)
    return {
        "description": _clip(raw.get("description") or "", 300),
        "source": source,
        "contract": {
            "inputs": normalized_inputs,
            "outputs": clean_outputs,
            "output_paths": clean_paths,
            "effects": list(contract.get("effects") or ["write"]),
            "idempotent": bool(contract.get("idempotent", True)),
            "failure_class": str(contract.get("failure_class") or "internal"),
        },
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# generality gate
# --------------------------------------------------------------------------- #
@dataclass
class GeneralityReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    static: dict[str, Any] = field(default_factory=dict)
    dynamic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "static": dict(self.static),
            "dynamic": dict(self.dynamic),
        }


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value) if isinstance(node.value, (str, int, float)) else ""
    if isinstance(node, (ast.Tuple, ast.List)):
        return " ".join(_literal_text(item) for item in node.elts)
    return ""


def static_source_check(source: str) -> dict[str, Any]:
    """AST scan: no hard-coded business lists, no explore/snapshot paths, no abs paths."""
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"ok": False, "violations": [f"源码语法错误: {exc.msg} (line {exc.lineno})"]}

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and len(node.elts) > 3:
            texts = [_literal_text(item) for item in node.elts]
            if all(texts) and any(CJK_RE.search(text) for text in texts):
                violations.append(
                    f"第 {getattr(node, 'lineno', 0)} 行存在疑似业务数据大列表（{len(node.elts)} 条）"
                )
        elif isinstance(node, ast.Dict) and len(node.keys) > 3:
            texts = [_literal_text(value) for value in node.values if value is not None]
            if texts and all(texts) and any(CJK_RE.search(text) for text in texts):
                violations.append(
                    f"第 {getattr(node, 'lineno', 0)} 行存在疑似录制数据字典（{len(node.keys)} 项）"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            if ABS_PATH_RE.match(text):
                violations.append(
                    f"第 {getattr(node, 'lineno', 0)} 行存在绝对路径: {text[:60]}"
                )
            lowered = text.lower()
            if any(
                marker in lowered
                for marker in (
                    ".panguverse",
                    "session.json",
                    "success.json",
                    "trace.jsonl",
                    "/explore/e-",
                    "artifacts/",
                )
            ):
                violations.append(
                    f"第 {getattr(node, 'lineno', 0)} 行引用探索会话/快照: {text[:60]}"
                )
    return {"ok": not violations, "violations": violations}


def _recorded_hashes(session_dir: Path, output_paths: Mapping[str, str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in output_paths.values():
        safe = _safe_rel(rel)
        if not safe:
            continue
        target = Path(session_dir) / safe
        if not target.is_file():
            # The atom may use a different directory prefix than the recorded
            # script; match the same filename anywhere in the session.
            name = Path(safe).name
            matches = [
                item
                for item in Path(session_dir).rglob(name)
                if item.is_file() and "artifacts" not in item.parts
            ]
            target = max(matches, key=lambda item: item.stat().st_mtime) if matches else None
        if target is not None and target.is_file():
            hashes[safe] = _hash_bytes(target.read_bytes())
    return hashes


def _synthetic_source(seed: str) -> str:
    payload = {
        "data": {
            "cards": [
                {
                    "content": [
                        {
                            "word": f"通用性测试条目-{seed}-alpha",
                            "desc": "generality fixture alpha",
                            "hotScore": 111,
                            "url": f"https://example.invalid/{seed}/a",
                            "hotTag": "测试",
                        },
                        {
                            "word": f"通用性测试条目-{seed}-beta",
                            "desc": "generality fixture beta",
                            "hotScore": 222,
                            "url": f"https://example.invalid/{seed}/b",
                            "hotTag": "测试",
                        },
                        {
                            "word": f"通用性测试条目-{seed}-gamma",
                            "desc": "generality fixture gamma",
                            "hotScore": 333,
                            "url": f"https://example.invalid/{seed}/c",
                            "hotTag": "测试",
                        },
                    ]
                }
            ]
        }
    }
    return json.dumps(payload, ensure_ascii=False)


class _GeneralityCtx:
    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace)
        self.env = {"FS_ROOT": str(workspace)}


def dynamic_generality_check(
    *,
    module_path: Path,
    contract: Mapping[str, Any],
    recorded: Mapping[str, str],
    seed: str | None = None,
) -> dict[str, Any]:
    """Run the atom in a clean temp workspace with different inputs.

    Success requires all of the following:

    * the run itself must not raise (no runtime_error);
    * every declared ``output_paths`` entry must be written;
    * the produced artifacts must not reproduce a recorded one byte-for-byte.

    A crash is never accepted as proof of generality: the atom must actually
    work on different inputs and emit all of its declared outputs.
    """
    seed = seed or uuid.uuid4().hex[:8]
    temp = Path(tempfile.mkdtemp(prefix="pv-generality-"))
    produced: dict[str, str] = {}
    error = ""
    result: dict[str, Any] = {}
    violations: list[str] = []
    previous_cwd = Path.cwd()
    try:
        os.chdir(temp)
        spec = importlib.util.spec_from_file_location(
            f"pv_generality_{seed}", module_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载原子模块")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run = getattr(module, "run", None)
        if not callable(run):
            raise RuntimeError("原子模块缺少 run()")
        inputs: dict[str, Any] = {}
        names = {str(key).rstrip("?") for key in (contract.get("inputs") or {})}
        if "source" in names:
            inputs["source"] = _synthetic_source(seed)
        if "html" in names:
            inputs["html"] = _synthetic_source(seed)
        if "text" in names:
            inputs["text"] = _synthetic_source(seed)
        if "url" in names:
            inputs["url"] = f"https://example.invalid/panguverse-generality/{seed}"
        if "output_dir" in names:
            inputs["output_dir"] = "."
        if "options" in names:
            inputs["options"] = {"limit": 3}
        result = run(ctx=_GeneralityCtx(temp), **inputs) or {}
        if not isinstance(result, Mapping):
            result = {}
    except Exception as exc:  # noqa: BLE001 - 任何执行异常都必须判定为不通用
        error = f"{type(exc).__name__}: {exc}"[:500]
        violations.append(f"runtime_error: {error}")
    finally:
        os.chdir(previous_cwd)
    declared = {
        str(key): _safe_rel(rel)
        for key, rel in (contract.get("output_paths") or {}).items()
    }
    for key, safe in declared.items():
        if not safe:
            violations.append(f"invalid_output_path: {key}")
            continue
        target = temp / safe
        if target.is_file():
            produced[key] = _hash_bytes(target.read_bytes())
        else:
            violations.append(f"missing_output: {key} -> {safe}")
    matched = sorted(
        declared[key]
        for key, digest in produced.items()
        if declared.get(key) and recorded.get(declared[key]) == digest
    )
    if matched:
        violations.append(
            "matched_recording: 不同输入仍产出与录制完全相同的文件 -> "
            + ", ".join(matched)
        )
    ok = not violations
    return {
        "ok": ok,
        "seed": seed,
        "error": error,
        "violations": violations,
        "produced": produced,
        "recorded_hashes": dict(recorded),
        "matched_recordings": matched,
    }


def generality_check(
    *,
    module_path: Path,
    source: str,
    contract: Mapping[str, Any],
    session_dir: Path,
    recorded_outputs: Mapping[str, str],
) -> GeneralityReport:
    report = GeneralityReport(ok=True)
    static = static_source_check(source)
    report.static = static
    if not static["ok"]:
        report.ok = False
        report.violations.extend(static["violations"])
    recorded = _recorded_hashes(session_dir, recorded_outputs)
    dynamic = dynamic_generality_check(
        module_path=module_path,
        contract=contract,
        recorded=recorded,
    )
    report.dynamic = dynamic
    if not dynamic["ok"]:
        report.ok = False
        report.violations.extend(dynamic.get("violations") or [])
    return report


# --------------------------------------------------------------------------- #
# atom materialization
# --------------------------------------------------------------------------- #
def _pack_name(eid: str) -> str:
    return "explore_" + re.sub(r"[^a-z0-9]+", "_", eid.lower()).strip("_")


def write_atom(
    *,
    workspace: Path,
    eid: str,
    name: str,
    source: str,
    contract: Mapping[str, Any],
    script: Mapping[str, Any],
    description: str,
    generality: GeneralityReport,
) -> dict[str, Any]:
    pack = _pack_name(eid)
    pack_dir = Path(workspace) / PACKS_REL / pack
    atoms_dir = pack_dir / "atoms"
    atoms_dir.mkdir(parents=True, exist_ok=True)
    atom_path = atoms_dir / f"{name}.py"
    atom_path.write_text(source, encoding="utf-8")
    sha = _hash_bytes(source.encode("utf-8"))
    return {
        "name": name,
        "pack": pack,
        "path": str(atom_path),
        "entry": f"atoms/{name}.py",
        "description": description,
        "inputs": dict(contract.get("inputs") or {}),
        "outputs": dict(contract.get("outputs") or {}),
        "output_paths": dict(contract.get("output_paths") or {}),
        "effects": list(contract.get("effects") or ["write"]),
        "idempotent": bool(contract.get("idempotent", True)),
        "failure_class": str(contract.get("failure_class") or "internal"),
        "origin": "explore",
        "explore_id": eid,
        "trace_seq": int(script.get("seq") or 0),
        "source_script": str(script.get("path") or ""),
        "code_artifact": str(script.get("code_artifact") or ""),
        "code_sha256": str(script.get("code_sha256") or ""),
        "sha256": sha,
        "generality": generality.to_dict(),
    }


def write_pack_manifest(workspace: Path, eid: str, atoms: list[dict[str, Any]]) -> str:
    pack = _pack_name(eid)
    pack_dir = Path(workspace) / PACKS_REL / pack
    if not atoms:
        return ""
    manifest_atoms = []
    for item in atoms:
        entry = {
            key: item.get(key)
            for key in (
                "name",
                "description",
                "inputs",
                "outputs",
                "output_paths",
                "effects",
                "idempotent",
                "failure_class",
                "entry",
                "origin",
                "explore_id",
                "trace_seq",
                "source_script",
                "code_artifact",
                "code_sha256",
            )
        }
        entry["category"] = "script"
        entry["tags"] = ["explore", "new_atom", "script"]
        manifest_atoms.append(entry)
    manifest = {
        "name": pack,
        "domain": "explore",
        "version": "0.2.0",
        "description": f"探索 {eid} 语义抽象出的通用原子",
        "tags": ["explore", "generated", "abstracted"],
        "source": "explore",
        "atoms": manifest_atoms,
    }
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "pack.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(pack_dir)


def discard_atoms(*, workspace: Path, eid: str, names: Iterable[str]) -> list[str]:
    """Remove atoms materialized by a crystallize attempt that failed replay.

    Atom packs are per explore session, so deleting the newly written modules
    and their ``pack.json`` entries keeps a failed attempt from polluting the
    workspace atom library.
    """
    pack = _pack_name(eid)
    pack_dir = Path(workspace) / PACKS_REL / pack
    atoms_dir = pack_dir / "atoms"
    remove = {str(name) for name in names if name}
    removed: list[str] = []
    for name in sorted(remove):
        if Path(name).name != name or name in (".", ".."):
            continue
        target = atoms_dir / f"{name}.py"
        if target.is_file():
            target.unlink()
            removed.append(name)
    manifest_path = pack_dir / "pack.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if isinstance(manifest, Mapping):
            remaining = [
                item
                for item in (manifest.get("atoms") or [])
                if isinstance(item, Mapping) and str(item.get("name") or "") not in remove
            ]
            if remaining:
                payload = dict(manifest)
                payload["atoms"] = remaining
                manifest_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            else:
                manifest_path.unlink(missing_ok=True)
    if atoms_dir.is_dir() and not any(atoms_dir.iterdir()):
        atoms_dir.rmdir()
    if pack_dir.is_dir() and not any(pack_dir.iterdir()):
        pack_dir.rmdir()
    return removed


# --------------------------------------------------------------------------- #
# graph orchestration
# --------------------------------------------------------------------------- #
def _sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "value"


def _node(
    *,
    node_id: str,
    atom_name: str,
    description: str,
    params: Mapping[str, Any],
    captures: Mapping[str, Any],
    extra: Mapping[str, Any],
    tags: list[str],
    case_name: str,
    new_atom: bool = False,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "atom": atom_name,
        "name": atom_name,
        "description": description,
        "params": dict(params),
        "capture": dict(captures),
        "when": None,
        "foreach": None,
        "as": "item",
        "retry": None,
        "continue_on_failure": False,
        "tags": tags,
        "optional": False,
        "extra": dict(extra),
        "case_id": "explore",
        "case_name": case_name or "explore",
        "origin": "explore",
        "new_atom": bool(new_atom),
        "locked": False,
    }


def _assert_node(
    *,
    node_id: str,
    atom_name: str,
    name: str,
    params: Mapping[str, Any],
    extra: Mapping[str, Any],
    case_name: str,
) -> dict[str, Any]:
    node = _node(
        node_id=node_id,
        atom_name=atom_name,
        description=name,
        params=params,
        captures={},
        extra=extra,
        tags=["explore", "assert"],
        case_name=case_name,
    )
    node["name"] = name
    return node


def _params_for_existing(
    node: Mapping[str, Any],
    digest: Mapping[str, Any],
) -> dict[str, Any]:
    if node.get("params"):
        return dict(node["params"])
    seq = node.get("trace_seq")
    if seq is None:
        return {}
    for step in digest.get("atom_steps") or []:
        if int(step.get("seq") or 0) == int(seq):
            return dict(step.get("params") or {})
    return {}


def _legacy_refs(
    params: Mapping[str, Any],
    *,
    digest: Mapping[str, Any],
    plan_nodes: list[dict[str, Any]],
    kept: list[int],
    current_id: str,
) -> dict[str, Any]:
    """Translate recorded ``${stepN.port}`` refs into plan node ids.

    Recorded parameters reference explore action indexes; a plan may reuse
    them verbatim when it omits ``params``.  Resolve them against the plan's
    trace_seq anchors instead of leaking a trace-only namespace into replay.
    """
    step_ids: dict[int, str] = {}
    for item in digest.get("atom_steps") or []:
        try:
            index = int(item.get("action_index") or 0)
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        for node in plan_nodes:
            if node.get("trace_seq") is not None and int(node["trace_seq"]) == seq:
                step_ids[index] = str(node["id"])
                break
    ordered_ids = [str(plan_nodes[index]["id"]) for index in kept]
    previous = ""
    for plan_id in ordered_ids:
        if plan_id == current_id:
            break
        previous = plan_id
    text = json.dumps(params, ensure_ascii=False)
    text = re.sub(
        r"\$\{step(\d+)\.([^}]+)\}",
        lambda m: (
            f"${{{step_ids[int(m.group(1))]}.{m.group(2)}}}"
            if int(m.group(1)) in step_ids
            else m.group(0)
        ),
        text,
    )
    if previous:
        text = re.sub(
            r"\$\{last\.([^}]+)\}", lambda m: f"${{{previous}.{m.group(1)}}}", text
        )
    return json.loads(text)


def _rewrite_refs(
    value: Any,
    *,
    node_index: Mapping[str, int],
    node_ids: Mapping[str, str],
    captures: dict[int, dict[str, Any]],
    deps: dict[int, list[tuple[int, str]]],
    current_index: int,
    current_id: str,
) -> Any:
    if isinstance(value, str):
        def sub(match: re.Match[str]) -> str:
            producer = match.group(1)
            port = match.group(2)
            if producer not in node_index:
                raise AbstractionError(
                    "unresolved_reference",
                    f"节点 {current_id} 引用未知上游 {producer}",
                    details={"reference": match.group(0), "node": current_id},
                )
            index = node_index[producer]
            if index >= current_index:
                raise AbstractionError(
                    "crystallize_plan_invalid", f"节点 {current_id} 只能引用更早节点"
                )
            producer_id = node_ids[producer]
            var = f"{producer_id}_{_sanitize(port)}"
            captures[index].setdefault(var, {"path": port})
            deps[current_index].append((index, var))
            return f"${{{var}}}"

        return REF_RE.sub(sub, value)
    if isinstance(value, Mapping):
        return {
            key: _rewrite_refs(
                item,
                node_index=node_index,
                node_ids=node_ids,
                captures=captures,
                deps=deps,
                current_index=current_index,
                current_id=current_id,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite_refs(
                item,
                node_index=node_index,
                node_ids=node_ids,
                captures=captures,
                deps=deps,
                current_index=current_index,
                current_id=current_id,
            )
            for item in value
        ]
    return value


_PARAM_ALIASES = {
    "board_json": "source",
    "source_text": "source",
    "source_html": "source",
    "html": "source",
    "text": "source",
    "content": "source",
    "raw": "source",
    "source_url": "url",
    "target_url": "url",
    "page_url": "url",
    "out_dir": "output_dir",
    "output_directory": "output_dir",
}


def _reconcile_new_atom_params(
    params: Mapping[str, Any], contract_inputs: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep only parameters the abstracted atom actually accepts.

    The plan is induced before the atom contract exists, so the LLM may guess
    names (``board_json``/``source_url``) or pass output filenames.  The atom
    owns its output paths; unknown/unsupported keys are dropped instead of
    crashing replay with ``unexpected keyword argument``.
    """
    accepted = {str(key).rstrip("?") for key in contract_inputs}
    out: dict[str, Any] = {}
    for key, value in params.items():
        name = _PARAM_ALIASES.get(str(key), str(key)).rstrip("?")
        if name in accepted:
            out[name] = value
    if "output_dir" in out:
        # Contract output paths are relative to the atom's output root; a
        # guessed subdirectory would make checks look at the wrong place.
        out["output_dir"] = "."
    return out


def _normalize_source_ref(
    params: Mapping[str, Any],
    *,
    plan_nodes: list[dict[str, Any]],
    kept: list[int],
) -> dict[str, Any]:
    """A new atom's ``source`` expects raw text; ``body`` may be a dict."""
    out = dict(params)
    source = out.get("source")
    if not isinstance(source, str):
        return out
    match = re.fullmatch(
        r"\$\{([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)\}", source.strip()
    )
    if not match:
        return out
    producer, port = match.group(1), match.group(2)
    producer_handle = next(
        (
            plan_nodes[index]
            for index in kept
            if str(plan_nodes[index]["id"]) == producer
        ),
        None,
    )
    if producer_handle is None:
        return out
    atom = str(producer_handle["atom"])
    if atom == "web_open":
        # ``web_open.html`` is a rendered page; the atom's own fetch path is
        # the proven one for HTML.  Passing a truncated page as ``source``
        # makes parsers fail before they can fall back.
        out.pop("source", None)
        return out
    if port in ("body", "json", "value", "data") and atom in (
        "http_request",
        "http_get_page",
    ):
        out["source"] = f"${{{producer}.text}}"
    return out


def _producer_for_artifact(
    rel: str,
    *,
    plan_nodes: list[dict[str, Any]],
    new_atoms: Mapping[str, Mapping[str, Any]],
    kept: list[int],
) -> int | None:
    for index in kept:
        node = plan_nodes[index]
        atom = node["atom"]
        if atom.startswith("new:"):
            name = _snake(atom.split(":", 1)[1])
            info = new_atoms.get(name)
            if info and rel in (info.get("output_paths") or {}).values():
                return index
        else:
            params = node.get("params") or {}
            if str(params.get("path") or "") == rel and atom in (
                "fs_write_text",
                "fs_write_json",
            ):
                return index
    return None


def _artifact_candidates(
    plan_nodes: list[dict[str, Any]],
    new_atoms: Mapping[str, Mapping[str, Any]],
    kept: list[int],
) -> list[str]:
    out: list[str] = []
    for index in kept:
        atom = str(plan_nodes[index]["atom"])
        if atom.startswith("new:"):
            info = new_atoms.get(_snake(atom.split(":", 1)[1]))
            if info:
                out.extend(str(rel) for rel in (info.get("output_paths") or {}).values())
    return out


def _stem_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", Path(text).stem.lower())


def _resolve_artifact(
    rel: str,
    plan_nodes: list[dict[str, Any]],
    new_atoms: Mapping[str, Mapping[str, Any]],
    kept: list[int],
) -> str:
    """Bind a planned check path to an actual atom output path.

    The plan is produced before the atom contract exists, so it may guess a
    directory (``reports/``) or a stem.  Exact path wins, then basename, then
    related stems of the same extension.  Never invent a file.
    """
    if not rel:
        return ""
    candidates = _artifact_candidates(plan_nodes, new_atoms, kept)
    if rel in candidates:
        return rel
    name = Path(rel).name
    same_name = [item for item in candidates if Path(item).name == name]
    if len(same_name) == 1:
        return same_name[0]
    suffix = Path(rel).suffix.lower()
    stem = _stem_key(rel)
    related = [
        item
        for item in candidates
        if Path(item).suffix.lower() == suffix
        and stem
        and (_stem_key(item) in stem or stem in _stem_key(item))
    ]
    if len(related) == 1:
        return related[0]
    same_ext = [item for item in candidates if Path(item).suffix.lower() == suffix]
    if len(same_ext) == 1:
        return same_ext[0]
    return rel


def _prune_plan_nodes(
    plan_nodes: list[dict[str, Any]],
    *,
    new_atoms: Mapping[str, Mapping[str, Any]],
    checks: list[dict[str, Any]],
    dropped_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop probes/glue: nodes nobody consumes and no check depends on."""
    referenced: set[str] = set()
    for node in plan_nodes:
        for value in _walk_strings(node.get("params") or {}):
            for match in REF_RE.finditer(value):
                referenced.add(match.group(1))
    all_indices = list(range(len(plan_nodes)))
    producer_ids: set[str] = set()
    for spec in checks:
        artifact = _safe_rel(spec.get("artifact") or spec.get("path"))
        if not artifact:
            node_ref = str(spec.get("node") or spec.get("step_id") or "")
            if node_ref:
                producer_ids.add(node_ref)
            continue
        artifact = _resolve_artifact(
            artifact, plan_nodes, new_atoms, all_indices
        )
        index = _producer_for_artifact(
            artifact, plan_nodes=plan_nodes, new_atoms=new_atoms, kept=all_indices
        )
        if index is not None:
            producer_ids.add(str(plan_nodes[index]["id"]))
    kept = [
        node
        for index, node in enumerate(plan_nodes)
        if str(node["id"]) in referenced
        or str(node["id"]) in producer_ids
        or (not checks and index == len(plan_nodes) - 1)
    ]
    kept_ids = {str(node["id"]) for node in kept}
    for node in plan_nodes:
        if str(node["id"]) not in kept_ids:
            dropped_steps.append(
                {
                    "trace_seq": node.get("trace_seq"),
                    "reason": "没有任何下游引用或检查依赖（探测/胶水步骤）",
                    "kind": "node",
                    "node_id": node.get("id"),
                }
            )
    return kept


def _check_is_semantic(spec: Mapping[str, Any]) -> bool:
    return spec.get("type") in SEMANTIC_CHECK_TYPES


def _has_atom(registry: Any, name: str) -> bool:
    if registry is None:
        return True
    try:
        registry.get(name)
        return True
    except Exception:  # noqa: BLE001 - 注入的测试 registry 可能不完整
        return False


def _assert_spec(node: Mapping[str, Any], *, node_id: str, producer_id: str) -> dict[str, Any]:
    spec = dict(node)
    spec.update({"binding": "artifact", "step_id": producer_id})
    return spec


def build_graph_from_plan(
    *,
    plan: Mapping[str, Any],
    digest: Mapping[str, Any],
    session_dir: Path,
    workspace: Path,
    eid: str,
    intent: str,
    name: str,
    new_atoms: Mapping[str, Mapping[str, Any]],
    pending: list[dict[str, Any]],
    dropped_steps: list[dict[str, Any]],
    registry: Any = None,
) -> dict[str, Any]:
    plan_nodes = _prune_plan_nodes(
        list(plan.get("nodes") or []),
        new_atoms=new_atoms,
        checks=list(plan.get("checks") or []),
        dropped_steps=dropped_steps,
    )
    kept_indices: list[int] = []
    usable: list[dict[str, Any]] = []
    for index, node in enumerate(plan_nodes):
        atom = str(node["atom"])
        if atom.startswith("new:"):
            atom_name = _snake(atom.split(":", 1)[1])
            if atom_name not in new_atoms:
                dropped_steps.append(
                    {
                        "trace_seq": node.get("trace_seq"),
                        "reason": f"新原子 {atom_name} 未通过通用性检查，节点已移除",
                        "kind": "node",
                    }
                )
                continue
        kept_indices.append(index)
        usable.append(node)
    if not usable:
        raise AbstractionError(
            "not_general",
            "计划中的节点全部被拒绝（新原子未通过通用性检查），不能固化",
            details={"pending_atoms": pending, "dropped_steps": dropped_steps},
        )

    node_ids: dict[str, str] = {}
    for order, index in enumerate(kept_indices, 1):
        node = plan_nodes[index]
        atom = str(node["atom"])
        atom_name = _snake(atom.split(":", 1)[1]) if atom.startswith("new:") else atom
        node_ids[str(node["id"])] = f"s{order}_{_sanitize(atom_name)}"

    node_index = {str(plan_nodes[index]["id"]): order for order, index in enumerate(kept_indices)}
    captures: dict[int, dict[str, Any]] = {index: {} for index in kept_indices}
    deps: dict[int, list[tuple[int, str]]] = {index: [] for index in kept_indices}

    nodes: list[dict[str, Any]] = []
    for index in kept_indices:
        node = plan_nodes[index]
        plan_id = str(node["id"])
        atom = str(node["atom"])
        params = _params_for_existing(node, digest) if not atom.startswith("new:") else dict(node["params"])
        params = _legacy_refs(
            params,
            digest=digest,
            plan_nodes=plan_nodes,
            kept=kept_indices,
            current_id=plan_id,
        )
        if atom.startswith("new:"):
            params = _reconcile_new_atom_params(
                params, (new_atoms[_snake(atom.split(":", 1)[1])].get("inputs") or {})
            )
            params = _normalize_source_ref(
                params, plan_nodes=plan_nodes, kept=kept_indices
            )
            info = new_atoms[_snake(atom.split(":", 1)[1])]
            accepted = {
                str(key).rstrip("?") for key in (info.get("inputs") or {})
            }
            if "source" in accepted and "source" not in params:
                for prev_index in reversed(kept_indices[: kept_indices.index(index)]):
                    port = {
                        "http_request": "text",
                        "web_open": "html",
                        "http_get_page": "html",
                        "web_get_text": "text",
                    }.get(str(plan_nodes[prev_index]["atom"]))
                    if port:
                        params["source"] = (
                            f"${{{plan_nodes[prev_index]['id']}.{port}}}"
                        )
                        break
        params = _rewrite_refs(
            params,
            node_index=node_index,
            node_ids=node_ids,
            captures=captures,
            deps=deps,
            current_index=index,
            current_id=plan_id,
        )
        if atom.startswith("new:"):
            atom_name = _snake(atom.split(":", 1)[1])
            info = new_atoms[atom_name]
            desc = f"探索 {eid} 抽象的新原子：{info.get('description') or atom_name}"
            extra = {
                "explore_id": eid,
                "trace_seq": node.get("trace_seq"),
                "new_atom": True,
                "atom_pack": info.get("pack") or "",
                "source_script": info.get("source_script") or "",
                "output_paths": dict(info.get("output_paths") or {}),
                "role": node.get("role") or "",
                "sha256": info.get("sha256"),
            }
            nodes.append(
                _node(
                    node_id=node_ids[plan_id],
                    atom_name=atom_name,
                    description=desc,
                    params=params,
                    captures=captures[index],
                    extra=extra,
                    tags=["explore", "new_atom", "script"],
                    case_name=name or intent,
                    new_atom=True,
                )
            )
        else:
            params = _normalize_existing_params(atom, params)
            desc = f"来自探索 {eid} 的原子步骤（trace seq={node.get('trace_seq')}）"
            extra = {
                "explore_id": eid,
                "trace_seq": node.get("trace_seq"),
                "role": node.get("role") or "",
            }
            nodes.append(
                _node(
                    node_id=node_ids[plan_id],
                    atom_name=atom,
                    description=desc,
                    params=params,
                    captures=captures[index],
                    extra=extra,
                    tags=["explore"],
                    case_name=name or intent,
                )
            )

    # ---- checks -> assert nodes + replay checks ------------------------- #
    final_checks: list[dict[str, Any]] = []
    dropped_checks: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    next_index = len(nodes) + 1
    for spec in plan.get("checks") or []:
        kind = str(spec.get("type"))
        if kind == "status_code":
            producer_index = _producer_for_status(spec, plan_nodes, kept_indices)
            if producer_index is None:
                dropped_checks.append({**spec, "reason": "没有可绑定状态码的节点"})
                continue
            producer_id = node_ids[str(plan_nodes[producer_index]["id"])]
            var = f"{producer_id}_status_code"
            captures[producer_index].setdefault(var, {"path": "status_code"})
            deps.setdefault(producer_index, [])
            clean = {
                "id": spec.get("id"),
                "type": "status_code",
                "equals": int(spec.get("equals", 200)),
                "step_id": producer_id,
                "binding": "step",
            }
            if not _has_atom(registry, "assert_status"):
                final_checks.append(clean)
                continue
            final_checks.append(clean)
            nodes.append(
                _assert_node(
                    node_id=f"s{next_index}_assert_status",
                    atom_name="assert_status",
                    name=f"断言状态码 {clean['equals']}",
                    params={"expected": clean["equals"], "source": f"${{{var}}}"},
                    extra={"explore_id": eid, "check_id": spec.get("id")},
                    case_name=name or intent,
                )
            )
            next_index += 1
            continue

        node_ref = str(spec.get("node") or spec.get("step_id") or "")
        if not (_safe_rel(spec.get("artifact") or spec.get("path"))) and node_ref:
            producer_index = next(
                (
                    index
                    for index in kept_indices
                    if str(plan_nodes[index]["id"]) == node_ref
                ),
                None,
            )
            if producer_index is None:
                dropped_checks.append({**spec, "reason": f"引用了不存在的节点 {node_ref}"})
                continue
            producer_id = node_ids[node_ref]
            port = str(spec.get("port") or "text")
            var = f"{producer_id}_{_sanitize(port)}"
            captures[producer_index].setdefault(var, {"path": port})
            clean = {
                key: value for key, value in spec.items() if not key.startswith("_")
            }
            clean.update({"binding": "step", "step_id": producer_id})
            if kind == "regex":
                atom_name = "assert_text_matches"
                params = {"pattern": str(spec.get("pattern") or ""), "source": f"${{{var}}}"}
            elif kind == "text_contains":
                atom_name = "assert_body_contains"
                params = {"text": str(spec.get("value") or ""), "source": f"${{{var}}}"}
            else:
                dropped_checks.append({**spec, "reason": "该检查需要 artifact"})
                continue
            if not _has_atom(registry, atom_name):
                final_checks.append(clean)
                continue
            final_checks.append(clean)
            nodes.append(
                _assert_node(
                    node_id=f"s{next_index}_{atom_name}",
                    atom_name=atom_name,
                    name=f"断言 {node_ref} 输出内容",
                    params=params,
                    extra={"explore_id": eid, "check_id": spec.get("id")},
                    case_name=name or intent,
                )
            )
            next_index += 1
            continue

        raw_artifact = _safe_rel(spec.get("artifact") or spec.get("path"))
        artifact = _resolve_artifact(
            raw_artifact, plan_nodes, new_atoms, kept_indices
        )
        if not artifact:
            dropped_checks.append({**spec, "reason": "artifact 非法"})
            continue
        producer_index = _producer_for_artifact(
            artifact, plan_nodes=plan_nodes, new_atoms=new_atoms, kept=kept_indices
        )
        if producer_index is None:
            dropped_checks.append({**spec, "reason": f"没有节点产出 {artifact}"})
            continue
        if kind == "file_exists" and _check_is_semantic(spec):
            pass
        if kind in ("file_exists", "min_items", "freshness", "regex", "text_contains"):
            producer_id = node_ids[str(plan_nodes[producer_index]["id"])]
            signature = (kind, artifact, str(spec.get("pattern") or spec.get("value") or spec.get("min_items")))
            if signature in seen:
                continue
            seen.add(signature)
            clean = {key: value for key, value in spec.items() if not key.startswith("_")}
            clean.update({"artifact": artifact, "step_id": producer_id})
            if kind == "file_exists":
                clean["binding"] = "path"
                atom_name = "assert_file_exists"
                params: dict[str, Any] = {"path": artifact}
                label = f"断言文件存在：{artifact}"
            elif kind == "min_items":
                clean["binding"] = "artifact"
                atom_name = "assert_file_min_items"
                params = {"path": artifact, "min_items": int(spec.get("min_items") or 1)}
                if spec.get("pattern"):
                    params["pattern"] = str(spec["pattern"])
                label = f"断言 {artifact} 条目数 ≥ {params['min_items']}"
            elif kind == "freshness":
                clean["binding"] = "artifact"
                atom_name = "assert_file_freshness"
                params = {
                    "path": artifact,
                    "max_age_seconds": int(spec.get("max_age_seconds") or 86400),
                }
                if spec.get("pattern"):
                    params["pattern"] = str(spec["pattern"])
                label = f"断言 {artifact} 时间戳新鲜"
            elif kind == "regex":
                clean["binding"] = "artifact"
                atom_name = "assert_file_matches"
                params = {"path": artifact, "pattern": str(spec.get("pattern") or "")}
                label = f"断言 {artifact} 匹配 {params['pattern']}"
            else:
                clean["binding"] = "artifact"
                atom_name = "assert_file_contains"
                params = {"path": artifact, "text": str(spec.get("value") or "")}
                label = f"断言 {artifact} 内容包含"
            if not _has_atom(registry, atom_name):
                final_checks.append(clean)
                continue
            final_checks.append(clean)
            nodes.append(
                _assert_node(
                    node_id=f"s{next_index}_{atom_name}",
                    atom_name=atom_name,
                    name=label,
                    params=params,
                    extra={
                        "explore_id": eid,
                        "check_id": spec.get("id"),
                        "asserts": artifact,
                    },
                    case_name=name or intent,
                )
            )
            next_index += 1

    if not final_checks:
        raise AbstractionError(
            "crystallize_plan_invalid",
            "没有可绑定到本次产物的检查，拒绝固化",
            details={"dropped_checks": dropped_checks},
        )
    if not any(item.get("type") in SEMANTIC_CHECK_TYPES for item in final_checks):
        raise AbstractionError(
            "crystallize_plan_invalid",
            "固化后的检查缺少内容语义检查（min_items / freshness / regex）",
        )

    # ---- edges ----------------------------------------------------------- #
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()

    def add_edge(left: str, right: str, *, via: str | None, kind: str) -> None:
        edge_id = f"e-{left}->{right}:{'data-' + _sanitize(via) if kind == 'data' and via else 'order'}"
        if edge_id in seen_edges:
            return
        seen_edges.add(edge_id)
        edges.append(
            {
                "id": edge_id,
                "from": left,
                "to": right,
                "via": via,
                "kind": kind,
                "order_only": kind != "data",
            }
        )

    ordered = [node_ids[str(plan_nodes[index]["id"])] for index in kept_indices]
    for index in kept_indices:
        for producer_index, var in deps.get(index, []):
            add_edge(
                node_ids[str(plan_nodes[producer_index]["id"])],
                node_ids[str(plan_nodes[index]["id"])],
                via=var,
                kind="data",
            )
    for left, right in itertools.pairwise(ordered):
        add_edge(left, right, via=None, kind="order")

    for spec in final_checks:
        producer_id = str(spec.get("step_id") or "")
        if not producer_id:
            continue
        assert_node = next(
            (
                node
                for node in nodes
                if (node.get("extra") or {}).get("check_id") == spec.get("id")
            ),
            None,
        )
        if assert_node is None:
            continue
        artifact = spec.get("artifact")
        if artifact:
            add_edge(producer_id, assert_node["id"], via=f"file:{artifact}", kind="data")
        elif spec.get("type") == "status_code":
            add_edge(producer_id, assert_node["id"], via="status_code", kind="data")

    for node in nodes:
        node_id = str(node.get("id"))
        for index in kept_indices:
            if node_ids[str(plan_nodes[index]["id"])] == node_id:
                node["capture"] = dict(captures.get(index) or {})
                break

    return {
        "nodes": nodes,
        "edges": edges,
        "checks": final_checks,
        "dropped_checks": dropped_checks,
        "pending_atoms": list(pending),
        "dropped_steps": list(dropped_steps),
        "out_of_scope": [],
        "scripts": [],
        "intent": intent,
        "name": name or intent,
        "abstraction_plan": {
            "summary": plan.get("summary") or "",
            "nodes": [
                {key: value for key, value in node.items() if key != "params"}
                for node in plan.get("nodes") or []
            ],
            "scripts": list(plan.get("scripts") or []),
            "checks": list(plan.get("checks") or []),
        },
        "synthesis": {
            "mode": "llm-abstraction",
            "node_count": len(nodes),
            "data_edges": sum(1 for edge in edges if edge["kind"] == "data"),
            "order_edges": sum(1 for edge in edges if edge["kind"] == "order"),
        },
    }


def _normalize_existing_params(atom: str, params: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(params)
    timeout = out.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 120:
        seconds = float(timeout) / 1000.0
        out["timeout"] = int(seconds) if seconds.is_integer() else round(seconds, 3)
    if "expect_status" in out:
        value = out["expect_status"]
        text = str(value).strip()
        match = re.fullmatch(r"([1-5])[xX]{2}", text)
        if match:
            out["expect_status"] = int(match.group(1)) * 100
        elif re.fullmatch(r"\d{3}", text):
            out["expect_status"] = int(text)
    return out


def _producer_for_status(
    spec: Mapping[str, Any],
    plan_nodes: list[dict[str, Any]],
    kept: list[int],
) -> int | None:
    raw = spec.get("node") or spec.get("step_id")
    if raw:
        for index in kept:
            if str(plan_nodes[index]["id"]) == str(raw):
                return index
    for index in reversed(kept):
        atom = str(plan_nodes[index]["atom"])
        if atom in ("http_request", "web_open", "assert_status"):
            return index
    return kept[-1] if kept else None


__all__ = [
    "AbstractionError",
    "GeneralityReport",
    "abstract_script",
    "atom_catalog",
    "build_digest",
    "build_graph_from_plan",
    "discard_atoms",
    "dynamic_generality_check",
    "generality_check",
    "plan_crystallization",
    "static_source_check",
    "write_atom",
    "write_pack_manifest",
]
