"""P1: turn successful explore scripts into workspace-local atoms.

A script that actually produced artifacts during an explore session is not left
as a ``pending_atom``.  It is snapshotted into ``<workspace>/.panguverse/packs``
with a contract, so replay and later workflow runs execute the *generated atom*
instead of rerunning the original script file.

The generated module embeds the proven script source and exposes
``run(ctx=None, input_dir="", input_files=None)``.  It copies the observed
inputs into the run workspace, executes under that cwd, and returns the
declared output paths.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core.workspace_packs import PACKS_REL

SNAKE_RE = re.compile(r"[^a-zA-Z0-9]+")
WRITE_OPEN_RE = re.compile(
    r"open\(\s*['\"]([^'\"]+\.(?:html?|md|json|csv|txt))['\"]\s*,\s*['\"][wax]"
)
WRITE_PATH_RE = re.compile(
    r"Path\(\s*['\"]([^'\"]+\.(?:html?|md|json|csv|txt))['\"]\s*\)"
)
JSON_DUMP_OPEN_RE = re.compile(
    r"json\.dump\([^;\n]*?open\(\s*['\"]([^'\"]+\.json)['\"]\s*,\s*['\"]w"
)
# ``out = Path('reports')`` + ``(out / 'report.html').write_text(...)``
PATH_VAR_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*Path\(\s*['\"]([^'\"]+)['\"]\s*\)")
JOIN_WRITE_RE = re.compile(
    r"(?:([A-Za-z_]\w*)|Path\(\s*['\"]([^'\"]+)['\"]\s*\))"
    r"\s*/\s*['\"]([^'\"]+\.(?:html?|md|json|csv|txt))['\"]"
)
READ_OPEN_RE = re.compile(
    r"open\(\s*['\"]([^'\"]+\.(?:json|txt|html?|csv))['\"]\s*,\s*['\"][^'\"]*r"
)
GLOB_RE = re.compile(r"glob\.glob\(\s*['\"]([^'\"]+)['\"]")
STDOUT_SOURCE_RE = re.compile(r"(?:source|candidate|chosen)\s*:\s*['\"]?([^\s'\"]+)", re.I)
OUTPUT_EXTS = (".html", ".htm", ".md", ".json", ".csv", ".txt")
MAX_SNAPSHOT_FILES = 40
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def snake(name: str) -> str:
    clean = SNAKE_RE.sub("_", str(name or "")).strip("_").lower()
    if not clean:
        clean = "script_atom"
    return clean if not clean[0].isdigit() else f"script_{clean}"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _output_key(rel: str, *, dotted: bool = False) -> str:
    path = Path(rel)
    stem = path.stem
    low = stem.lower()
    if low.endswith("_items") or low in {"items", "report_items"}:
        return "items"
    base = snake(stem)
    # ``report.html`` / ``report.md`` are the canonical explore outputs; keep
    # the extension even when only one of them exists.
    if base == "report":
        dotted = True
    if dotted:
        ext = path.suffix.lstrip(".").lower()
        ext = "html" if ext == "htm" else ext
        return f"{base}_{ext}"
    return base


def infer_outputs(source: str, *, session_dir: Path) -> dict[str, str]:
    """Infer output ports from literal write paths, keeping only real files.

    When one atom writes several files with the same stem (``report.html`` +
    ``report.md``), the port names keep the extension so both are addressable:
    ``report_html`` / ``report_md``.
    """
    candidates: list[str] = []
    for pattern in (WRITE_OPEN_RE, JSON_DUMP_OPEN_RE):
        candidates.extend(pattern.findall(source))
    candidates.extend(WRITE_PATH_RE.findall(source))
    # Path join form: ``base = Path('reports'); base / 'report.html'``.
    path_vars = dict(PATH_VAR_RE.findall(source))
    for var, literal_base, name in JOIN_WRITE_RE.findall(source):
        base = path_vars.get(var) if var else literal_base
        if base:
            candidates.append(f"{base.rstrip('/')}/{name}")
    for name, base in path_vars.items():
        # ``(out / 'x.html')`` already captured above; this keeps direct
        # ``base.write_text`` calls that reference a literal file name.
        for match in re.finditer(
            re.escape(name) + r"\s*/\s*['\"]([^'\"]+\.(?:html?|md|json|csv|txt))['\"]",
            source,
        ):
            candidates.append(f"{base.rstrip('/')}/{match.group(1)}")
    rels: list[str] = []
    for rel in _unique([item.replace("\\", "/") for item in candidates]):
        rel = rel.lstrip("./")
        if not rel or rel.startswith("scripts/") or ".." in Path(rel).parts:
            continue
        if not rel.lower().endswith(OUTPUT_EXTS):
            continue
        if not (session_dir / rel).is_file():
            continue
        rels.append(rel)
    stems: dict[str, int] = {}
    for rel in rels:
        stems[Path(rel).stem.lower()] = stems.get(Path(rel).stem.lower(), 0) + 1
    outputs: dict[str, str] = {}
    used: set[str] = set()
    for rel in rels:
        dotted = stems.get(Path(rel).stem.lower(), 0) > 1
        key = _output_key(rel, dotted=dotted)
        while key in used:
            key = f"{key}_2"
        used.add(key)
        outputs[key] = rel
    if not outputs:
        outputs["output_files"] = "files"
    return outputs


def infer_inputs(source: str) -> tuple[dict[str, str], list[str]]:
    """Return (input contract, inferred glob patterns)."""
    globs = _unique([str(item).replace("\\", "/") for item in GLOB_RE.findall(source)])
    reads = _unique([str(item).replace("\\", "/") for item in READ_OPEN_RE.findall(source)])
    inputs = {"input_dir?": "string", "input_files?": "array"}
    if re.search(r"\bsys\.argv\b", source):
        inputs["args?"] = "array"
    return inputs, globs + [f"literal:{item}" for item in reads if item not in globs]


WRITE_PATTERNS = (
    r"open\(\s*['\"][^'\"]+['\"]\s*,\s*['\"][wax]",
    r"write_text\(",
    r"write_bytes\(",
    r"json\.dump\([^;\n]*?open\(",
    r"shutil\.(?:copy|copy2|copyfile|move)\(",
    r"os\.(?:mkdir|makedirs|remove|unlink|rename|replace)\(",
    r"Path\([^)]*\)\.mkdir\(",
)


def infer_effects(source: str) -> tuple[str, ...]:
    """Infer read/write effects from literal write operations in the source."""
    for pattern in WRITE_PATTERNS:
        if re.search(pattern, source):
            return ("write",)
    return ("read",)


def infer_failure_class(source: str) -> str:
    if re.search(r"^\s*(assert|raise\s+SystemExit)\b", source, re.M):
        return "expectation"
    if "urllib" in source or "requests" in source or "http" in source:
        return "transient"
    return "internal"


def _script_path(rel: str) -> Path | None:
    path = Path(str(rel or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        return None
    return path


def _snapshot_inputs(
    *,
    source: str,
    stdout: str,
    session_dir: Path,
    data_dir: Path,
    output_paths: set[str],
    globs: list[str],
) -> list[str]:
    candidates: list[str] = []
    match = STDOUT_SOURCE_RE.search(stdout or "")
    if match:
        candidates.append(match.group(1).strip())
    # The script's own stdout tells us the exact file it consumed.  Trust it
    # over broad globs, otherwise replay would snapshot unrelated artifacts
    # (including its own report.html) and could pass for the wrong reason.
    scan_globs = [] if candidates else globs
    for pattern in scan_globs:
        if not pattern or pattern.startswith("/") or ".." in Path(pattern).parts:
            continue
        if pattern.startswith("literal:"):
            candidates.append(pattern.split(":", 1)[1])
            continue
        try:
            matches = sorted(session_dir.glob(pattern))
        except (OSError, ValueError):
            continue
        for item in matches:
            if item.is_file():
                try:
                    candidates.append(str(item.relative_to(session_dir)))
                except ValueError:
                    continue
    copied: list[str] = []
    total = 0
    for rel in _unique(candidates):
        rel = rel.replace("\\", "/").lstrip("./")
        if not rel or rel in output_paths:
            continue
        if rel.startswith(("scripts/", "replay/", ".panguverse/")) or ".." in Path(rel).parts:
            continue
        src = session_dir / rel
        if not src.is_file():
            continue
        try:
            if src.stat().st_size > MAX_SNAPSHOT_BYTES:
                continue
        except OSError:
            continue
        dest = data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        copied.append(rel)
        total += dest.stat().st_size
        if len(copied) >= MAX_SNAPSHOT_FILES or total >= MAX_SNAPSHOT_BYTES:
            break
    return copied


def _render_module(
    *,
    atom_name: str,
    pack_name: str,
    eid: str,
    script_rel: str,
    source: str,
    source_sha: str,
    inputs: Mapping[str, str],
    outputs: Mapping[str, str],
    globs: list[str],
    literals: list[str],
) -> str:
    output_pairs = [(key, value) for key, value in outputs.items() if value != "files"]
    return f'''"""Generated explore atom: {atom_name}.

Source: explore {eid} script {script_rel}
sha256: {source_sha}

This module is self-contained: replay invokes ``run`` and never reruns the
original script path.  Observed inputs are snapshotted under the pack's
``data/`` directory by the crystallizer.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
from pathlib import Path

SOURCE = {source!r}
INPUT_GLOBS = {json.dumps(globs)}
OUTPUT_FILES = {json.dumps(output_pairs)}
INPUT_FILES = {json.dumps(list(inputs))}
INPUT_LITERALS = {json.dumps(literals)}
DECLARE_OUTPUT_FILES = {"output_files" in outputs}


def _workspace(ctx):
    raw = getattr(ctx, "workspace", None) if ctx is not None else None
    if raw:
        return Path(raw).expanduser().resolve()
    env = getattr(ctx, "env", None) or {{}}
    for key in ("FS_ROOT", "PANGUVERSE_FS_ROOT"):
        value = env.get(key) if hasattr(env, "get") else None
        if value:
            return Path(str(value)).expanduser().resolve()
    return Path.cwd().resolve()


def _copy_inputs(workspace: Path, input_dir: str, input_files):
    base = Path(input_dir).expanduser() if input_dir else (Path(__file__).resolve().parents[1] / "data")
    files = list(input_files or [])
    files.extend(INPUT_LITERALS)
    if not files and base.is_dir():
        for pattern in INPUT_GLOBS:
            if pattern.startswith("literal:"):
                files.append(pattern.split(":", 1)[1])
                continue
            try:
                files.extend(str(item.relative_to(base)) for item in base.glob(pattern) if item.is_file())
            except (OSError, ValueError):
                continue
    copied = []
    base_resolved = base.resolve() if base.exists() else base
    for rel in files:
        rel = str(rel).replace("\\\\", "/").lstrip("./")
        if not rel or ".." in Path(rel).parts:
            continue
        src = (base / rel).resolve()
        try:
            src.relative_to(base_resolved)
        except ValueError:
            continue
        if not src.is_file():
            continue
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel)
    return copied


def run(ctx=None, input_dir="", input_files=None):
    workspace = _workspace(ctx)
    workspace.mkdir(parents=True, exist_ok=True)
    copied = _copy_inputs(workspace, input_dir, input_files)
    out, err = io.StringIO(), io.StringIO()
    previous = Path.cwd()
    try:
        os.chdir(workspace)
        namespace = {{"__name__": "__panguverse_generated_atom__", "__file__": str(Path(__file__).resolve())}}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(compile(SOURCE, "<generated atom {atom_name}>", "exec"), namespace)
    except BaseException as exc:
        tail = err.getvalue()[-1200:]
        raise RuntimeError(f"原子 {atom_name} 执行失败: {{type(exc).__name__}}: {{exc}}" + (f"; stderr: {{tail}}" if tail else "")) from exc
    finally:
        os.chdir(previous)
    result = {{"stdout": out.getvalue()[-4000:], "stderr": err.getvalue()[-4000:], "inputs_used": copied}}
    missing = []
    for key, rel in OUTPUT_FILES:
        target = workspace / rel
        if not target.is_file():
            missing.append(rel)
            continue
        result[key] = rel
    if missing:
        raise RuntimeError(f"原子 {atom_name} 未产出声明的输出: {{', '.join(missing)}}")
    if DECLARE_OUTPUT_FILES:
        result["files"] = [
            str(item.relative_to(workspace)) for item in workspace.rglob("*") if item.is_file()
        ]
    return result
'''


def _node_for(
    *,
    atom_name: str,
    pack_name: str,
    eid: str,
    intent: str,
    seq: int,
    script_rel: str,
    data_dir: Path,
    input_files: list[str],
) -> dict[str, Any]:
    # Inputs are snapshotted into the pack's data/ directory; the generated
    # atom defaults to it, so the graph never carries an absolute explore path.
    params: dict[str, Any] = {}
    if input_files:
        params["input_files"] = list(input_files)
    task = f"；任务：{intent}" if intent else ""
    return {
        "id": f"sc{seq}_{atom_name}",
        "atom": atom_name,
        "name": atom_name,
        "description": f"来自探索 {eid} 的成功脚本步骤（trace seq={seq}）{task}",
        "params": params,
        "capture": {},
        "when": None,
        "foreach": None,
        "as": "item",
        "retry": None,
        "continue_on_failure": False,
        "tags": ["explore", "new_atom", "script"],
        "optional": False,
        "extra": {
            "explore_id": eid,
            "trace_seq": seq,
            "new_atom": True,
            "atom_pack": pack_name,
            "source_script": script_rel,
        },
        "case_id": "explore",
        "case_name": intent or "explore",
        "origin": "explore",
        "new_atom": True,
        "locked": False,
    }


def _pending_script(event: Mapping[str, Any], reason: str) -> dict[str, Any]:
    rel = str(event.get("path") or "")
    return {
        "name": re.sub(r"[^A-Za-z0-9_]+", "_", Path(rel or "script").stem),
        "path": rel,
        "code_sha256": str(event.get("code_sha256") or ""),
        "code_artifact": str(event.get("code_artifact") or ""),
        "exit_code": event.get("exit_code"),
        "status": "pending",
        "kind": "script",
        "reason": reason,
        "trace_seq": event.get("seq"),
    }


def load_script_source(
    event: Mapping[str, Any], session_dir: Path
) -> tuple[str | None, str]:
    """Public wrapper: read the trace's content-addressed script snapshot."""
    return _load_snapshot_source(event, Path(session_dir))


def _load_snapshot_source(
    event: Mapping[str, Any], session_dir: Path
) -> tuple[str | None, str]:
    """Read the code version recorded by the trace, never the live script file."""
    code_artifact = str(event.get("code_artifact") or "").strip()
    expected = str(event.get("code_sha256") or "").strip()
    if not code_artifact:
        return None, "trace 缺少 code_artifact（旧轨迹或未快照），拒绝固化"
    rel = _script_path(code_artifact)
    if rel is None or not rel.parts or rel.parts[0] != "artifacts":
        return None, f"code_artifact 路径非法：{code_artifact}"
    target = session_dir / rel
    if not target.is_file():
        return None, f"code_artifact 不存在：{code_artifact}"
    raw = target.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if not expected:
        return None, "trace 缺少 code_sha256，拒绝固化"
    if actual != expected:
        return None, f"code_artifact sha 不一致：期望 {expected[:12]}…，实际 {actual[:12]}…，拒绝固化"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "code_artifact 不是 UTF-8 源码，拒绝固化"


def materialize_script_atoms(
    *,
    workspace: Path,
    session_dir: Path,
    scripts: list[dict[str, Any]],
    eid: str,
    intent: str = "",
    only_seqs: set[int] | None = None,
    include_nodes: bool = True,
) -> dict[str, Any]:
    """Create/refresh one atom per script path using its last *successful* trace.

    The live ``scripts/<name>.py`` file may have been overwritten by a later
    failed attempt.  Crystallization therefore reads only the per-execution
    ``code_artifact`` and verifies its sha against the selected trace event.
    """
    workspace = Path(workspace)
    session_dir = Path(session_dir)

    by_path: dict[str, list[dict[str, Any]]] = {}
    for event in scripts:
        rel = _script_path(str(event.get("path") or ""))
        if rel is None:
            continue
        by_path.setdefault(str(rel), []).append(event)

    pending: list[dict[str, Any]] = []
    if only_seqs is not None:
        wanted = {int(item) for item in only_seqs}
        chosen_events: list[tuple[str, dict[str, Any]]] = []
        for rel, events in by_path.items():
            for event in events:
                if int(event.get("seq") or 0) not in wanted:
                    continue
                if event.get("ok") and int(event.get("exit_code") or 0) == 0:
                    chosen_events.append((rel, event))
                else:
                    pending.append(
                        _pending_script(event, "选中的脚本版本执行失败，拒绝固化")
                    )
        chosen_events.sort(key=lambda item: int(item[1].get("seq") or 0))
    else:
        chosen_events = []
        for rel, events in by_path.items():
            events = sorted(events, key=lambda item: int(item.get("seq") or 0))
            good = [
                item
                for item in events
                if item.get("ok") and int(item.get("exit_code") or 0) == 0
            ]
            chosen = good[-1] if good else None
            if chosen is not None:
                chosen_events.append((rel, chosen))
            for event in events:
                if event is chosen:
                    continue
                if event.get("ok") and int(event.get("exit_code") or 0) == 0:
                    pending.append(
                        _pending_script(
                            event,
                            f"同路径存在更新的成功版本 trace seq={chosen.get('seq')}，本版本未固化",
                        )
                    )
                else:
                    pending.append(
                        _pending_script(
                            event,
                            f"脚本执行失败（exit_code={event.get('exit_code')}），不固化",
                        )
                    )
        chosen_events.sort(key=lambda item: int(item[1].get("seq") or 0))

    pack_name = "explore_" + re.sub(r"[^a-z0-9]+", "_", eid.lower()).strip("_")
    pack_dir = workspace / PACKS_REL / pack_name
    atoms_dir = pack_dir / "atoms"
    data_dir = pack_dir / "data"

    manifest_atoms: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    used_names: set[str] = set()
    if chosen_events:
        atoms_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

    path_totals = Counter(Path(rel).stem for rel, _event in chosen_events)
    for rel, event in chosen_events:
        source, reason = _load_snapshot_source(event, session_dir)
        if source is None:
            pending.append(_pending_script(event, reason))
            continue
        seq = int(event.get("seq") or 0)
        base_name = snake(Path(rel).stem)
        atom_name = base_name
        if path_totals[Path(rel).stem] > 1 or atom_name in used_names:
            atom_name = f"{base_name}_s{int(event.get('seq') or 0)}"
        suffix = 2
        candidate = atom_name
        while candidate in used_names:
            candidate = f"{atom_name}_{suffix}"
            suffix += 1
        atom_name = candidate
        used_names.add(atom_name)

        output_paths_map = infer_outputs(source, session_dir=session_dir)
        output_types = {
            key: ("array" if value == "files" else "string")
            for key, value in output_paths_map.items()
        }
        outputs = output_paths_map
        output_paths = {value for value in outputs.values() if value != "files"}
        inputs, declared_reads = infer_inputs(source)
        globs = [item for item in declared_reads if not item.startswith("literal:")]
        literal_reads = [
            item.split(":", 1)[1] for item in declared_reads if item.startswith("literal:")
        ]
        stdout = str(event.get("stdout") or "")
        snapshot = _snapshot_inputs(
            source=source,
            stdout=stdout,
            session_dir=session_dir,
            data_dir=data_dir,
            output_paths=output_paths,
            globs=[*globs, *[f"literal:{item}" for item in literal_reads]],
        )
        module_globs = globs
        failure_class = infer_failure_class(source)
        effects = infer_effects(source)
        code = _render_module(
            atom_name=atom_name,
            pack_name=pack_name,
            eid=eid,
            script_rel=rel,
            source=source,
            source_sha=str(event.get("code_sha256") or ""),
            inputs=inputs,
            outputs=outputs,
            globs=module_globs,
            literals=literal_reads,
        )
        atom_path = atoms_dir / f"{atom_name}.py"
        atom_path.write_text(code, encoding="utf-8")

        description = (
            f"探索 {eid} 固化脚本 {rel}（trace seq={seq}）："
            f"读取 {snapshot or ['(无快照输入)']}，产出 {list(outputs)}"
            + (f"；任务：{intent}" if intent else "")
        )
        atom_manifest = {
            "name": atom_name,
            "description": description,
            "inputs": inputs,
            "outputs": output_types,
            "output_paths": output_paths_map,
            "effects": list(effects),
            "idempotent": True,
            "failure_class": failure_class,
            "entry": f"atoms/{atom_name}.py",
            "category": "script",
            "tags": ["explore", "new_atom", "script"],
            "origin": "explore",
            "explore_id": eid,
            "trace_seq": seq,
            "source_script": rel,
            "code_artifact": str(event.get("code_artifact") or ""),
            "code_sha256": str(event.get("code_sha256") or ""),
        }
        manifest_atoms.append(atom_manifest)
        created.append(
            {
                "name": atom_name,
                "pack": pack_name,
                "path": str(atom_path),
                "entry": f"atoms/{atom_name}.py",
                "description": description,
                "inputs": dict(inputs),
                "outputs": dict(output_types),
                "output_paths": dict(output_paths_map),
                "effects": list(effects),
                "idempotent": True,
                "failure_class": failure_class,
                "origin": "explore",
                "explore_id": eid,
                "trace_seq": seq,
                "source_script": rel,
                "code_artifact": str(event.get("code_artifact") or ""),
                "code_sha256": str(event.get("code_sha256") or ""),
                "inputs_snapshot": snapshot,
            }
        )
        if include_nodes:
            nodes.append(
                _node_for(
                    atom_name=atom_name,
                    pack_name=pack_name,
                    eid=eid,
                    intent=intent,
                    seq=seq,
                    script_rel=rel,
                    data_dir=data_dir,
                    input_files=snapshot,
                )
            )

    if not manifest_atoms:
        return {
            "atoms_created": [],
            "nodes": [],
            "pending": pending,
            "pack": "",
            "pack_path": "",
            "selected_seqs": [],
        }
    manifest = {
        "name": pack_name,
        "domain": "explore",
        "version": "0.1.0",
        "description": f"探索 {eid} 固化的脚本原子",
        "tags": ["explore", "generated"],
        "source": "explore",
        "atoms": manifest_atoms,
    }
    (pack_dir / "pack.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "atoms_created": created,
        "nodes": nodes,
        "pending": pending,
        "pack": pack_name,
        "pack_path": str(pack_dir),
        "selected_seqs": sorted(int(event.get("seq") or 0) for _rel, event in chosen_events),
    }


def merge_script_nodes(graph: Mapping[str, Any], script_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert script nodes into trace order and rebuild deterministic order edges."""
    merged = dict(graph)
    nodes = [*list(graph.get("nodes") or []), *script_nodes]
    nodes.sort(key=lambda node: (int((node.get("extra") or {}).get("trace_seq") or 0), str(node.get("id"))))
    edges = []
    for left, right in itertools.pairwise(nodes):
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
    merged["nodes"] = nodes
    merged["edges"] = edges
    return merged


__all__ = [
    "infer_failure_class",
    "infer_inputs",
    "infer_outputs",
    "load_script_source",
    "materialize_script_atoms",
    "merge_script_nodes",
    "snake",
]
