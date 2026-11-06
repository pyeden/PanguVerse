"""Minimal, portable synthesis of an explore trace into a replayable workflow.

The old crystallizer copied the trace: every successful tool call became a
node and every edge was an ``order`` edge.  That produced 20-node workflows
where only two nodes actually mattered, duplicate URLs, probe steps, and
parameters that only worked because the explore sandbox happened to contain
the right files.

This module keeps the *evidence* and drops the *trajectory*:

1. collect trace steps (atoms, scripts, their real outputs and reads);
2. collapse repeated ``tool+params`` / repeated output writers, keep the last
   successful one;
3. keep only nodes reachable backwards from checks / produced artifacts;
4. rewrite ``${stepN.path}`` references into capture variables and data edges,
   failing loudly on references that cannot be resolved;
5. normalise parameters to the atom contract (ms→s, ``2xx``→200, portable
   script inputs);
6. turn path/status checks into visible ``assert_*`` nodes bound to the node
   that produced the evidence.

Everything here is a pure function over trace events plus optional session
files, so it is unit-testable without an LLM or a server.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomize

STEP_REF_RE = re.compile(r"^\$\{step(\d+)\.([^}]+)\}$")
LAST_REF_RE = re.compile(r"^\$\{last\.([^}]+)\}$")
ANY_REF_RE = re.compile(r"\$\{step(\d+)\.([^}]+)\}")
ANY_LAST_RE = re.compile(r"\$\{last\.([^}]+)\}")

FILE_EXTS = (".html", ".htm", ".md", ".json", ".csv", ".txt")
TEXT_EXT_PRIORITY = {".html": 0, ".htm": 0, ".md": 1, ".txt": 2, ".json": 3, ".csv": 4}
WEB_ATOMS = {"web_open", "http_request"}
FS_PATH_KEYS = {"path", "input_dir", "output_dir", "source_path", "file", "dir"}
MAX_CHECK_BYTES = 2_000_000


class SynthesisError(Exception):
    """Trace cannot be synthesized into a replayable workflow."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass
class Step:
    seq: int
    kind: str  # atom | script
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    action_index: int | None = None
    digest: dict[str, Any] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    error: str = ""
    # scripts
    script_path: str = ""
    source: str | None = None
    source_error: str = ""
    code_artifact: str = ""
    code_sha: str = ""
    output_paths: dict[str, str] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    atom_pack: str = ""
    atom_name: str = ""

    @property
    def outputs(self) -> list[str]:
        return sorted({v for v in self.output_paths.values() if v and v != "files"})


@dataclass
class Selection:
    steps: list[Step]
    checks: list[dict[str, Any]]
    dropped_checks: list[dict[str, Any]]
    pruned: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    script_seqs: list[int]
    out_of_scope: list[dict[str, Any]]
    failed: list[dict[str, Any]]
    #: ``(consumer_seq, "stepN.path" | "last.path") -> producer_seq``
    ref_targets: dict[tuple[int, str], int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# trace collection
# --------------------------------------------------------------------------- #
def _script_step(event: Mapping[str, Any], session_dir: Path | None) -> Step:
    path = str(event.get("path") or "")
    seq = int(event.get("seq") or 0)
    ok = bool(event.get("ok")) and int(event.get("exit_code") or 0) == 0
    step = Step(
        seq=seq,
        kind="script",
        name=atomize.snake(Path(path).stem),
        ok=ok,
        script_path=path,
        code_artifact=str(event.get("code_artifact") or ""),
        code_sha=str(event.get("code_sha256") or ""),
        error=str(event.get("stderr") or "")[-1000:],
    )
    if not ok:
        raw = str(event.get("stderr") or "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        message = lines[-1] if lines else "exit_code≠0"
        if session_dir is not None:
            message = message.replace(str(session_dir), "")
        step.error = message.strip()[:300]
        return step
    if session_dir is None:
        step.source_error = "固化时未提供 session_dir，无法读取脚本快照"
        return step
    source, reason = atomize.load_script_source(event, Path(session_dir))
    if source is None:
        step.source_error = reason
        return step
    step.source = source
    if step.error:
        step.error = step.error.replace(str(session_dir), "").strip()[:300]
    inferred = atomize.infer_outputs(source, session_dir=Path(session_dir))
    step.output_paths = {
        str(key): str(value) for key, value in inferred.items() if value != "files"
    }
    _, declared = atomize.infer_inputs(source)
    step.reads = [str(item) for item in declared]
    return step


def collect_steps(
    events: Iterable[Mapping[str, Any]], *, session_dir: Path | str | None = None
) -> list[Step]:
    """Turn raw trace events into ordered steps (including failed ones)."""
    base = Path(session_dir) if session_dir else None
    steps: list[Step] = []
    pending: Step | None = None
    action_index = 0
    for event in events:
        kind = event.get("type")
        if kind == "tool_call":
            action_index += 1
            pending = Step(
                seq=int(event.get("seq") or 0),
                kind="atom",
                name=str(event.get("tool") or ""),
                params=dict(event.get("params_template") or event.get("params") or {}),
                actual=dict(event.get("params") or {}),
                action_index=action_index,
            )
        elif kind == "tool_result" and pending is not None:
            if str(event.get("tool") or "") != pending.name:
                continue
            pending.ok = bool(event.get("ok"))
            pending.digest = dict(event.get("digest") or {})
            pending.keys = [str(key) for key in pending.digest.get("keys") or []]
            pending.artifacts = [str(item) for item in event.get("artifacts") or []]
            pending.error = str(event.get("error") or "")
            steps.append(pending)
            pending = None
        elif kind == "script":
            steps.append(_script_step(event, base))
    return steps


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _norm_rel(raw: Any) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    parts = Path(text).parts
    if not text or text.startswith("/") or ".." in parts:
        return ""
    return text


def _stem_key(rel: str) -> str:
    stem = Path(rel).stem.lower()
    return re.sub(r"[^a-z0-9]+", "", stem)


def _related_stems(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _ext_group(rel: str) -> str:
    suffix = Path(rel).suffix.lower()
    return ".html" if suffix == ".htm" else suffix


def _read_text(rel: str, session_dir: Path | None) -> str | None:
    if session_dir is None:
        return None
    target = Path(session_dir) / rel
    try:
        if not target.is_file() or target.stat().st_size > MAX_CHECK_BYTES:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _matches(spec: Mapping[str, Any], text: str) -> bool:
    if spec.get("type") == "text_contains":
        return str(spec.get("value") or "") in text
    try:
        return re.search(str(spec.get("pattern") or ""), text) is not None
    except re.error:
        return False


def _sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "value"


def _var(producer_id: str, path: str) -> str:
    return f"{producer_id}_{_sanitize(path.replace('.', '_'))}"


def _walk_refs(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        for match in re.finditer(r"\$\{[^{}]*\}", value):
            yield match.group(0)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_refs(item)


def _normalize_status(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_normalize_status(item) for item in value]
    text = str(value).strip()
    match = re.fullmatch(r"([1-5])[xX]{2}", text)
    if match:
        return int(match.group(1)) * 100
    if re.fullmatch(r"[1-5]", text):
        return int(text) * 100
    if re.fullmatch(r"\d{3}", text):
        return int(text)
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) > 1:
        return [_normalize_status(part) for part in parts]
    return value


def _normalize_params(
    name: str,
    params: Mapping[str, Any],
    *,
    registry: Any = None,
    session_dir: Path | None = None,
) -> dict[str, Any]:
    """Coerce observed parameters to what the atom contract actually accepts."""
    out = dict(params)
    atom = None
    if registry is not None:
        try:
            atom = registry.get(name)
        except Exception:  # noqa: BLE001 - 注入的测试 registry 可能不完整
            atom = None
    if atom is not None:
        accepted = {str(key).rstrip("?") for key in (getattr(atom, "inputs", {}) or {})}
        if accepted:
            out = {key: value for key, value in out.items() if key in accepted}

    timeout = out.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 120:
        seconds = float(timeout) / 1000.0
        out["timeout"] = int(seconds) if seconds.is_integer() else round(seconds, 3)

    if "expect_status" in out:
        out["expect_status"] = _normalize_status(out["expect_status"])

    if name.startswith("fs_") or name.startswith("assert_file"):
        for key in list(out):
            if key not in FS_PATH_KEYS or not isinstance(out[key], str):
                continue
            if "${" in out[key]:
                continue
            raw = out[key]
            candidate = Path(raw)
            if candidate.is_absolute() and session_dir is not None:
                try:
                    out[key] = str(candidate.resolve().relative_to(Path(session_dir).resolve()))
                    continue
                except (OSError, ValueError):
                    pass
            rel = _norm_rel(raw)
            if rel:
                out[key] = rel
    return out


def _canonical_url(params: Mapping[str, Any]) -> str:
    url = str(params.get("url") or "").strip()
    base = str(params.get("base_url") or "").strip()
    if url and not url.startswith(("http://", "https://")) and base:
        url = base.rstrip("/") + (url if url.startswith("/") else "/" + url)
    return url


def _atom_key(step: Step) -> tuple[str, str, str]:
    if step.name in WEB_ATOMS:
        method = str(step.actual.get("method") or step.params.get("method") or "GET").upper()
        body = ""
        if method != "GET":
            body = _stable(
                {
                    key: (step.actual or step.params).get(key)
                    for key in ("json", "data", "form", "params")
                }
            )
        return (step.name, method, _canonical_url(step.actual or step.params) + body)
    return (step.name, "", _stable(step.params))


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def _check_spec(item: Mapping[str, Any]) -> dict[str, Any]:
    spec = item.get("spec") if isinstance(item.get("spec"), Mapping) else item
    return dict(spec or {})


def _select_file_checks(
    specs: list[dict[str, Any]],
    *,
    last_writer: Mapping[str, Step],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep one representative per report family (root vs reports/ duplicate)."""
    candidates: list[tuple[dict[str, Any], str, Step]] = []
    dropped: list[dict[str, Any]] = []
    for spec in specs:
        rel = _norm_rel(spec.get("path"))
        if not rel:
            dropped.append({**spec, "reason": "路径非法或越界", "kind": "check"})
            continue
        writer = last_writer.get(rel)
        if writer is None:
            dropped.append(
                {**spec, "reason": f"本流程没有节点产出 {rel}（不伪造检查）", "kind": "check"}
            )
            continue
        candidates.append((spec, rel, writer))

    groups: dict[str, list[tuple[dict[str, Any], str, Step]]] = defaultdict(list)
    for item in candidates:
        groups[_ext_group(item[1])].append(item)

    kept: list[dict[str, Any]] = []
    for _ext, items in groups.items():
        clusters: list[list[tuple[dict[str, Any], str, Step]]] = []
        for item in items:
            placed = False
            for cluster in clusters:
                if any(
                    _related_stems(_stem_key(cluster[0][1]), _stem_key(item[1]))
                    for _ in (0,)
                ):
                    cluster.append(item)
                    placed = True
                    break
            if not placed:
                clusters.append([item])
        for cluster in clusters:
            best = max(
                cluster,
                key=lambda entry: (
                    2 if "report" in _stem_key(entry[1]) else 0,
                    1 if Path(entry[1]).parent != Path(".") else 0,
                    len(Path(entry[1]).parts),
                    entry[2].seq,
                ),
            )
            kept.append(best[0])
            for other in cluster:
                if other is best:
                    continue
                dropped.append(
                    {
                        **other[0],
                        "reason": f"与 {best[1]} 指向同一产物，保留规范路径",
                        "kind": "check",
                    }
                )
    return kept, dropped


def _select_checks(
    checks: list[dict[str, Any]],
    *,
    last_writer: Mapping[str, Step],
    kept_for_action: Mapping[int, Step],
    session_dir: Path | None,
    all_ok: list[Step],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    specs = [_check_spec(item) for item in checks if _check_spec(item).get("type")]
    file_specs = [spec for spec in specs if spec["type"] == "file_exists"]
    kept, dropped = _select_file_checks(file_specs, last_writer=last_writer)
    seen = {
        (spec["type"], _norm_rel(spec.get("path")), spec.get("value"), spec.get("pattern"))
        for spec in kept
    }
    for spec in kept:
        rel = _norm_rel(spec.get("path"))
        spec["path"] = rel
        writer = last_writer.get(rel)
        if writer is not None:
            spec["_producer_seq"] = writer.seq
        spec["binding"] = "path"

    text_results: list[dict[str, Any]] = []
    for spec in specs:
        if spec["type"] not in ("text_contains", "regex"):
            continue
        artifact = _norm_rel(spec.get("artifact"))
        writer: Step | None = None
        if artifact and artifact in last_writer:
            text = _read_text(artifact, session_dir)
            if text is not None and _matches(spec, text):
                writer = last_writer[artifact]
        if writer is None:
            for rel, candidate in sorted(
                last_writer.items(),
                key=lambda entry: (
                    TEXT_EXT_PRIORITY.get(Path(entry[0]).suffix.lower(), 9),
                    entry[1].seq,
                ),
            ):
                text = _read_text(rel, session_dir)
                if text is not None and _matches(spec, text):
                    artifact, writer = rel, candidate
                    break
        signature = (spec["type"], artifact, spec.get("value"), spec.get("pattern"))
        if signature in seen:
            continue
        seen.add(signature)
        if writer is not None:
            spec["artifact"] = artifact
            spec["binding"] = "artifact"
            spec["_producer_seq"] = writer.seq
            text_results.append(spec)
            continue
        # No file evidence: bind to the step the check named (in-memory result).
        producer = _producer_from_spec(spec, kept_for_action=kept_for_action)
        if producer is not None:
            spec["binding"] = "step"
            spec["_producer_seq"] = producer.seq
            text_results.append(spec)
            continue
        # An unbindable check must still fail replay, never silently pass.
        spec["binding"] = "unbound"
        spec["unbound"] = True
        fallback = max(all_ok, key=lambda item: item.seq) if all_ok else None
        if fallback is not None:
            spec["_fallback_seq"] = fallback.seq
        kept.append(spec)

    kept.extend(text_results)

    status_specs: list[dict[str, Any]] = []
    for spec in specs:
        if spec["type"] != "status_code":
            continue
        producer = _producer_from_spec(spec, kept_for_action=kept_for_action)
        if producer is None:
            producer = next(
                (
                    step
                    for step in reversed(all_ok)
                    if step.digest.get("status") is not None
                    or step.digest.get("status_code") is not None
                ),
                None,
            )
        if producer is None:
            dropped.append({**spec, "reason": "没有可绑定状态码的步骤", "kind": "check"})
            continue
        signature = ("status_code", str(spec.get("equals")), producer.seq)
        if signature in seen:
            continue
        seen.add(signature)
        spec["binding"] = "step"
        spec["_producer_seq"] = producer.seq
        status_specs.append(spec)
    kept.extend(status_specs)
    return kept, dropped


def _producer_from_spec(
    spec: Mapping[str, Any],
    *,
    kept_for_action: Mapping[int, Step],
) -> Step | None:
    raw = spec.get("step")
    if raw in (None, ""):
        return None
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return None
    return kept_for_action.get(index)


def _matching_writers(
    pattern: str, last_writer: Mapping[str, Step], *, own: Step
) -> list[Step]:
    if not pattern or pattern.startswith("literal:"):
        rel = pattern.split(":", 1)[1] if ":" in pattern else pattern
        writer = last_writer.get(_norm_rel(rel))
        return [writer] if writer is not None and writer.seq != own.seq else []
    if pattern.startswith("/") or ".." in Path(pattern).parts:
        return []
    found: list[Step] = []
    for rel, writer in last_writer.items():
        if writer.seq == own.seq:
            continue
        if fnmatch.fnmatch(rel, pattern) and writer not in found:
            found.append(writer)
    return found


def _refs_for(step: Step) -> list[dict[str, Any]]:
    """Return template references found in a step's parameters."""
    refs: list[dict[str, Any]] = []
    for value in _walk_refs(step.params):
        full = STEP_REF_RE.match(value.strip())
        if full:
            refs.append(
                {"kind": "step", "index": int(full.group(1)), "path": full.group(2)}
            )
            continue
        last = LAST_REF_RE.match(value.strip())
        if last:
            refs.append({"kind": "last", "path": last.group(1)})
            continue
        for match in ANY_REF_RE.finditer(value):
            refs.append(
                {"kind": "step", "index": int(match.group(1)), "path": match.group(2)}
            )
        for match in ANY_LAST_RE.finditer(value):
            refs.append({"kind": "last", "path": match.group(1)})
    return refs


def select_workflow(
    steps: list[Step],
    checks: list[dict[str, Any]],
    *,
    session_dir: Path | None = None,
    allowed: set[str] | None = None,
) -> Selection:
    out_of_scope: list[dict[str, Any]] = []
    ok_atoms: list[Step] = []
    failed: list[dict[str, Any]] = []
    for step in steps:
        if step.kind == "atom" and not step.ok:
            failed.append(_pending(step, f"原子执行失败：{step.error or 'ok=false'}"))
            continue
        if step.kind == "script" and not step.ok:
            failed.append(_pending(step, f"脚本执行失败（exit_code≠0）：{step.error or '未知错误'}"))
            continue
        if step.kind == "atom" and allowed is not None and step.name not in allowed:
            item = _pending(step, f"原子 {step.name} 不属于本次路由域")
            item.update({"status": "out_of_scope", "kind": "out_of_scope"})
            out_of_scope.append(item)
            continue
        if step.kind == "atom":
            ok_atoms.append(step)
        # scripts are collected below via output writers

    ok_scripts = [step for step in steps if step.kind == "script" and step.ok and step.source]

    last_by_key: dict[tuple[str, str, str], Step] = {}
    for step in ok_atoms:
        last_by_key[_atom_key(step)] = step
    remap: dict[int, int] = {}
    for step in ok_atoms:
        kept = last_by_key[_atom_key(step)]
        remap[step.seq] = kept.seq
    kept_atoms = {step.seq: step for step in last_by_key.values()}
    by_action = {step.action_index: step for step in ok_atoms if step.action_index}
    kept_for_action = {
        index: kept_atoms[remap[step.seq]]
        for index, step in by_action.items()
        if remap.get(step.seq) in kept_atoms
    }

    last_writer: dict[str, Step] = {}
    for step in ok_scripts:
        for rel in step.outputs:
            last_writer[rel] = step

    all_ok = [*kept_atoms.values(), *ok_scripts]

    kept_checks, dropped_checks = _select_checks(
        checks,
        last_writer=last_writer,
        kept_for_action=kept_for_action,
        session_dir=session_dir,
        all_ok=all_ok,
    )

    script_without_source = [
        step for step in steps if step.kind == "script" and step.ok and step.source is None
    ]
    for step in script_without_source:
        failed.append(_pending(step, step.source_error or "脚本快照不可用，拒绝固化"))

    keep: set[int] = set()
    for spec in kept_checks:
        producer = spec.get("_producer_seq") or spec.get("_fallback_seq")
        if producer:
            keep.add(int(producer))
    if not keep and all_ok:
        keep.add(max(all_ok, key=lambda item: item.seq).seq)

    by_seq = {step.seq: step for step in all_ok}
    unresolved: list[dict[str, Any]] = []
    deps: dict[int, list[Step]] = defaultdict(list)
    ref_targets: dict[tuple[int, str], int] = {}

    changed = True
    while changed:
        changed = False
        for seq in sorted(keep):
            step = by_seq.get(seq)
            if step is None:
                continue
            for ref in _refs_for(step):
                producer = _resolve_ref(ref, step, by_action=by_action, remap=remap, by_seq=by_seq)
                if producer is None:
                    unresolved.append(
                        {
                            "trace_seq": step.seq,
                            "atom": step.name,
                            "reference": ref,
                            "reason": "引用的步骤不存在、已失败或没有产出",
                        }
                    )
                    continue
                key = (
                    f"step{ref['index']}.{ref['path']}"
                    if ref.get("kind") == "step"
                    else f"last.{ref.get('path')}"
                )
                ref_targets[(step.seq, key)] = producer.seq
                if producer.seq != step.seq and producer.seq not in keep:
                    keep.add(producer.seq)
                    changed = True
                deps[step.seq].append(producer)
            if step.kind == "script":
                for pattern in step.reads:
                    for writer in _matching_writers(pattern, last_writer, own=step):
                        if writer.seq not in keep:
                            keep.add(writer.seq)
                            changed = True
                        deps[step.seq].append(writer)

    if unresolved:
        raise SynthesisError(
            "unresolved_reference",
            "轨迹中存在无法解析的步骤引用，不能静默改成 order 边",
            details={"unresolved": unresolved[:20]},
        )

    selected = sorted((by_seq[seq] for seq in keep if seq in by_seq), key=lambda item: item.seq)
    kept_seqs = {step.seq for step in selected}
    pruned: list[dict[str, Any]] = []
    for step in steps:
        if step.seq in kept_seqs:
            continue
        if step.kind == "atom" and not step.ok:
            continue  # already in failed
        if step.kind == "script" and not step.ok:
            continue
        if step.seq in {item.seq for item in ok_atoms} or (
            step.kind == "script" and step.ok and step.source is not None
        ):
            if step.kind == "atom" and remap.get(step.seq) not in (None, step.seq):
                reason = "重复 tool+params，保留最后一次成功调用"
            elif step.kind == "script" and not step.outputs:
                reason = "纯探测脚本（未产出被检查/引用的产物）"
            elif step.kind == "script" and any(
                last_writer.get(rel) is not None and last_writer[rel].seq > step.seq
                for rel in step.outputs
            ):
                reason = "产物被更晚的成功脚本覆盖，保留最后版本"
            else:
                reason = "不在输入→产物/检查路径上"
            pruned.append(_pending(step, reason))

    script_seqs = [step.seq for step in selected if step.kind == "script"]
    return Selection(
        steps=selected,
        checks=kept_checks,
        dropped_checks=dropped_checks,
        pruned=pruned,
        unresolved=unresolved,
        script_seqs=script_seqs,
        out_of_scope=out_of_scope,
        failed=failed,
        ref_targets=ref_targets,
    )


def _resolve_ref(
    ref: Mapping[str, Any],
    current: Step,
    *,
    by_action: Mapping[int, Step],
    remap: Mapping[int, int],
    by_seq: Mapping[int, Step],
) -> Step | None:
    if ref.get("kind") == "last":
        candidates = [step for step in by_seq.values() if step.seq < current.seq]
        return max(candidates, key=lambda item: item.seq) if candidates else None
    index = int(ref.get("index") or 0)
    original = by_action.get(index)
    if original is None or not original.ok:
        return None
    kept_seq = remap.get(original.seq)
    return by_seq.get(kept_seq) if kept_seq is not None else None


def _pending(step: Step, reason: str) -> dict[str, Any]:
    return {
        "name": step.atom_name or step.name or atomize.snake(Path(step.script_path).stem),
        "path": step.script_path if step.kind == "script" else "",
        "code_sha256": step.code_sha,
        "exit_code": 0 if step.ok else -1,
        "status": "pending",
        "kind": "script" if step.kind == "script" else "atom",
        "reason": reason,
        "trace_seq": step.seq,
    }


# --------------------------------------------------------------------------- #
# graph rendering
# --------------------------------------------------------------------------- #
def _script_params(info: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = [str(item) for item in info.get("inputs_snapshot") or []]
    params: dict[str, Any] = {}
    if snapshot:
        # Inputs are snapshotted into the pack's data/ dir; the generated atom
        # defaults to that directory, so no absolute explore path leaks out.
        params["input_files"] = snapshot
    return params


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


def build_graph(
    *,
    selection: Selection,
    materialized: Mapping[int, Mapping[str, Any]],
    checks: list[dict[str, Any]],
    eid: str,
    intent: str,
    name: str,
    registry: Any = None,
    session_dir: Path | None = None,
) -> dict[str, Any]:
    selected = selection.steps
    if not selected:
        raise SynthesisError("empty_workflow", "剪枝后没有可执行节点")

    # Drop checks whose producer did not survive materialization.
    usable_checks: list[dict[str, Any]] = []
    dropped = list(selection.dropped_checks)
    produced_files = {
        rel
        for info in materialized.values()
        for rel in (info.get("output_paths") or {}).values()
    }
    selected_seqs = {step.seq for step in selected}
    for spec in selection.checks:
        producer = spec.get("_producer_seq") or spec.get("_fallback_seq")
        if producer and int(producer) not in selected_seqs:
            dropped.append({**spec, "reason": "产出节点未保留", "kind": "check"})
            continue
        if spec.get("binding") == "artifact" and spec.get("artifact") not in produced_files:
            dropped.append({**spec, "reason": "产物节点未固化", "kind": "check"})
            continue
        usable_checks.append(spec)

    node_ids: dict[int, str] = {}
    for index, step in enumerate(selected, 1):
        if step.kind == "script":
            info = materialized.get(step.seq)
            if info is None:
                continue
            atom_name = str(info.get("name") or step.atom_name or step.name)
        else:
            atom_name = step.name
        node_ids[step.seq] = f"s{index}_{_sanitize(atom_name)}"

    # Only selected steps with a node (scripts must have materialized).
    active = [step for step in selected if step.seq in node_ids]
    if not active:
        raise SynthesisError("empty_workflow", "没有成功固化的可执行节点")

    def has_atom(atom_name: str) -> bool:
        if registry is None:
            return True
        try:
            registry.get(atom_name)
            return True
        except Exception:  # noqa: BLE001 - injected test registries may be partial
            return False

    captures: dict[int, dict[str, Any]] = {step.seq: {} for step in active}
    deps: dict[int, list[tuple[int, str]]] = defaultdict(list)
    params_by_seq: dict[int, dict[str, Any]] = {}

    def producer_node(step_ref: Step) -> tuple[int, str]:
        pid = node_ids[step_ref.seq]
        return step_ref.seq, pid

    def _ref_producer(current: Step, key: str, display: str) -> Step:
        target = selection.ref_targets.get((current.seq, key))
        producer = next((step for step in active if step.seq == target), None)
        if producer is None or producer.seq not in node_ids:
            raise SynthesisError(
                "unresolved_reference",
                f"无法把引用 {display} 绑定到保留节点",
                details={"trace_seq": current.seq},
            )
        return producer

    def _capture(producer: Step, path: str, current: Step) -> str:
        seq, pid = producer_node(producer)
        var = _var(pid, path)
        captures[seq].setdefault(var, {"path": path})
        deps[current.seq].append((seq, var))
        return var

    def rewrite(value: Any, current: Step) -> Any:
        if isinstance(value, str):
            full = STEP_REF_RE.match(value.strip())
            if full:
                key = f"step{int(full.group(1))}.{full.group(2)}"
                producer = _ref_producer(current, key, value)
                return f"${{{_capture(producer, full.group(2), current)}}}"
            last = LAST_REF_RE.match(value.strip())
            if last:
                key = f"last.{last.group(1)}"
                producer = _ref_producer(current, key, value)
                return f"${{{_capture(producer, last.group(1), current)}}}"
            if "${" not in value:
                return value

            def sub_step(match: re.Match[str]) -> str:
                key = f"step{int(match.group(1))}.{match.group(2)}"
                producer = _ref_producer(current, key, match.group(0))
                return f"${{{_capture(producer, match.group(2), current)}}}"

            def sub_last(match: re.Match[str]) -> str:
                key = f"last.{match.group(1)}"
                producer = _ref_producer(current, key, match.group(0))
                return f"${{{_capture(producer, match.group(1), current)}}}"

            value = ANY_REF_RE.sub(sub_step, value)
            return ANY_LAST_RE.sub(sub_last, value)
        if isinstance(value, Mapping):
            return {key: rewrite(item, current) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item, current) for item in value]
        return value

    for step in active:
        if step.kind == "script":
            info = materialized[step.seq]
            params = _script_params(info)
        else:
            params = rewrite(dict(step.params), step)
            params = _normalize_params(
                step.name, params, registry=registry, session_dir=session_dir
            )
        params_by_seq[step.seq] = params

    nodes: list[dict[str, Any]] = []
    for step in active:
        node_id = node_ids[step.seq]
        if step.kind == "script":
            info = materialized[step.seq]
            atom_name = str(info.get("name") or step.name)
            desc = (
                f"探索固化的脚本原子（trace seq={step.seq}，源脚本 {step.script_path}）"
                + (f"；任务：{intent}" if intent else "")
            )
            tags = ["explore", "new_atom", "script"]
            extra = {
                "explore_id": eid,
                "trace_seq": step.seq,
                "new_atom": True,
                "atom_pack": info.get("pack") or "",
                "source_script": step.script_path,
                "output_paths": dict(info.get("output_paths") or {}),
            }
            nodes.append(
                _node(
                    node_id=node_id,
                    atom_name=atom_name,
                    description=desc,
                    params=params_by_seq[step.seq],
                    captures=captures[step.seq],
                    extra=extra,
                    tags=tags,
                    case_name=name or intent,
                    new_atom=True,
                )
            )
        else:
            desc = f"来自探索 {eid} 的可复用原子步骤（trace seq={step.seq}）"
            extra = {"explore_id": eid, "trace_seq": step.seq}
            nodes.append(
                _node(
                    node_id=node_id,
                    atom_name=step.name,
                    description=desc,
                    params=params_by_seq[step.seq],
                    captures=captures[step.seq],
                    extra=extra,
                    tags=["explore"],
                    case_name=name or intent,
                )
            )

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

    for step in active:
        for producer_seq, var in deps[step.seq]:
            if producer_seq in node_ids:
                add_edge(node_ids[producer_seq], node_ids[step.seq], via=var, kind="data")

    # Deterministic order edges only where no data edge already exists.
    for left, right in itertools.pairwise(active):
        left_id, right_id = node_ids[left.seq], node_ids[right.seq]
        if any(
            edge["from"] == left_id and edge["to"] == right_id and edge["kind"] == "data"
            for edge in edges
        ):
            continue
        add_edge(left_id, right_id, via=None, kind="order")

    # ---- checks -> visible assert nodes ---------------------------------- #
    final_checks: list[dict[str, Any]] = []
    seen_checks: set[tuple[Any, ...]] = set()
    next_index = len(nodes) + 1
    for spec in usable_checks:
        producer = spec.get("_producer_seq") or spec.get("_fallback_seq")
        producer_id = node_ids.get(int(producer)) if producer else None
        kind = spec["type"]
        if kind == "file_exists":
            rel = _norm_rel(spec.get("path"))
            if not rel or not producer_id:
                dropped.append({**spec, "reason": "文件检查没有产出节点", "kind": "check"})
                continue
            signature = ("file_exists", rel)
            if signature in seen_checks:
                continue
            seen_checks.add(signature)
            spec = dict(spec)
            spec.update({"path": rel, "binding": "path", "step_id": producer_id})
            if not has_atom("assert_file_exists"):
                final_checks.append({k: v for k, v in spec.items() if not k.startswith("_")})
                continue
            node_id = f"s{next_index}_assert_file_exists"
            next_index += 1
            nodes.append(
                _assert_node(
                    node_id=node_id,
                    atom_name="assert_file_exists",
                    name=f"断言文件存在：{rel}",
                    params={"path": rel},
                    extra={"explore_id": eid, "check_id": spec.get("id"), "asserts": rel},
                    case_name=name or intent,
                )
            )
            add_edge(producer_id, node_id, via=f"file:{rel}", kind="data")
        elif kind in ("text_contains", "regex") and spec.get("binding") == "artifact":
            rel = _norm_rel(spec.get("artifact"))
            if not rel or not producer_id:
                dropped.append({**spec, "reason": "文本检查没有产物节点", "kind": "check"})
                continue
            signature = (kind, rel, spec.get("value"), spec.get("pattern"))
            if signature in seen_checks:
                continue
            seen_checks.add(signature)
            spec = dict(spec)
            spec.update({"artifact": rel, "binding": "artifact", "step_id": producer_id})
            if kind != "text_contains":
                # No built-in file-regex assert atom; keep the deterministic
                # replay check without inventing a node name.
                final_checks.append({k: v for k, v in spec.items() if not k.startswith("_")})
                continue
            atom_name = "assert_file_contains"
            if not has_atom(atom_name):
                final_checks.append({k: v for k, v in spec.items() if not k.startswith("_")})
                continue
            node_id = f"s{next_index}_{atom_name}"
            next_index += 1
            params: dict[str, Any] = {"path": rel}
            if kind == "text_contains":
                params["text"] = str(spec.get("value") or "")
            else:
                params["pattern"] = str(spec.get("pattern") or "")
            nodes.append(
                _assert_node(
                    node_id=node_id,
                    atom_name=atom_name,
                    name=f"断言文件内容：{rel}",
                    params=params,
                    extra={"explore_id": eid, "check_id": spec.get("id"), "asserts": rel},
                    case_name=name or intent,
                )
            )
            add_edge(producer_id, node_id, via=f"artifact:{rel}", kind="data")
        elif kind == "status_code":
            if not producer_id:
                dropped.append({**spec, "reason": "状态码检查没有产出节点", "kind": "check"})
                continue
            expected = _normalize_status(spec.get("equals", 200))
            signature = ("status_code", _stable(expected), producer_id)
            if signature in seen_checks:
                continue
            seen_checks.add(signature)
            spec = dict(spec)
            spec.update({"equals": expected, "binding": "step", "step_id": producer_id})
            if not has_atom("assert_status"):
                final_checks.append({k: v for k, v in spec.items() if not k.startswith("_")})
                continue
            var = _var(producer_id, "status_code")
            captures[int(producer)].setdefault(var, {"path": "status_code"})
            node_id = f"s{next_index}_assert_status"
            next_index += 1
            nodes.append(
                _assert_node(
                    node_id=node_id,
                    atom_name="assert_status",
                    name=f"断言状态码 {expected}",
                    params={"expected": expected, "source": f"${{{var}}}"},
                    extra={"explore_id": eid, "check_id": spec.get("id")},
                    case_name=name or intent,
                )
            )
            add_edge(producer_id, node_id, via=var, kind="data")
        elif spec.get("binding") == "step" and kind in ("text_contains", "regex"):
            # In-memory result evidence: no file to assert, but replay can still
            # bind the check to the producing node's observation.
            spec = dict(spec)
            spec.update({"step_id": producer_id})
        elif spec.get("binding") == "unbound":
            # Keep the check so replay reports a real failure; no fake node.
            spec = dict(spec)
        else:
            dropped.append({**spec, "reason": "检查未能绑定到可重放证据", "kind": "check"})
            continue

        clean = {key: value for key, value in spec.items() if not key.startswith("_")}
        final_checks.append(clean)

    # refresh capture dicts on the already-built nodes (producer captures may
    # have been added while rendering assert nodes).
    for node in nodes:
        seq = int((node.get("extra") or {}).get("trace_seq") or 0)
        if seq and seq in captures:
            node["capture"] = dict(captures[seq])

    return {
        "nodes": nodes,
        "edges": edges,
        "checks": final_checks,
        "dropped_checks": dropped,
        "pending_atoms": [],
        "out_of_scope": list(selection.out_of_scope),
        "scripts": [],
        "intent": intent,
        "name": name or intent,
        "synthesis": {
            "kept": [step.seq for step in active],
            "pruned": selection.pruned,
            "failed": selection.failed,
            "dropped_checks": dropped,
            "node_count": len(nodes),
            "data_edges": sum(1 for edge in edges if edge["kind"] == "data"),
            "order_edges": sum(1 for edge in edges if edge["kind"] == "order"),
        },
    }


__all__ = [
    "Selection",
    "Step",
    "SynthesisError",
    "build_graph",
    "collect_steps",
    "select_workflow",
]
