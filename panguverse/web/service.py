"""服务层：把 CLI 的闭环能力包装成 Web API 可调用的函数。

这里承担三件事：

1. **只读视图**：状态、原子、领域包、场景、运行历史（全部来自磁盘与注册表）；
2. **运行编排**：demo / scenario / intent / regression 四种模式，逐阶段回写 job.phases；
3. **边界校验**：请求体、路径（realpath 前缀）、run_id 正则，越界一律 400。

所有产物落到 ``<workspace>/runs/<run_id>/``，与 CLI 完全一致；
报告统一走 ``panguverse.report.write_all``（内部已按密钥打码）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..axe.axe import PanguAxe
from ..axe.plan import PlanRequest, TestPlan
from ..core.pack import AtomPack, builtin_pack_registry
from ..core.registry import default_registry
from ..demo.e2e import collect_suites, merge_suites
from ..demo.mock_api import MockAPI
from ..engine.result import RunResult
from ..report import DEFAULT_FORMATS, write_all
from ..report.json_report import conclusion as report_conclusion
from ..spec.loader import TestSuite, load_suite
from .jobs import Job, JobManager, sanitize_message

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parents[1]
BUILTIN_SCENARIOS = REPO_ROOT / "scenarios"

LLM_ENV_KEYS = ("PANGUVERSE_API_KEY", "OPENAI_API_KEY")
VALID_MODES = ("demo", "scenario", "intent", "regression")

STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "internal_error": 500,
}

_RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
_REGISTRY_LOCK = threading.Lock()
_REGISTRY: Any = None


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """带错误码的 API 异常：由 api.py 统一渲染成 ``{"error": {...}}``。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or STATUS_BY_CODE.get(code, 500)


def safe_message(value: Any) -> str:
    """错误消息统一打码（复用 job 层的实现，避免两套规则）。"""
    return sanitize_message(value)


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def resolve_workspace(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def registry() -> Any:
    """进程内共享的内置原子库（只读使用）。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = default_registry()
        return _REGISTRY


def pack_registry() -> Any:
    return builtin_pack_registry()


def build_axe(root: Path, *, enable_llm: bool = True) -> PanguAxe:
    return PanguAxe(registry=registry(), workspace=str(root), enable_llm=enable_llm)


def llm_enabled() -> bool:
    """只判断密钥是否存在，**绝不回显任何值**。"""
    return any((os.environ.get(key) or "").strip() for key in LLM_ENV_KEYS)


# --------------------------------------------------------------------------- #
# 只读视图
# --------------------------------------------------------------------------- #
def status_payload(root: Path) -> dict[str, Any]:
    axe = build_axe(root, enable_llm=False)
    data = axe.status()
    return {
        "workspace": str(root),
        "atoms": int(data.get("atoms", 0)),
        "packs": len(pack_registry().list()),
        "regression_count": int(data.get("sealed", 0)),
        "lessons_count": int(data.get("lessons", 0)),
        "llm_enabled": llm_enabled(),
        "http_backend": str(data.get("http_backend", "")),
        "scenarios": len(list_scenarios(root)),
        "version": __version__,
    }


def list_atoms(
    *, domain: str | None = None, tag: str | None = None, category: str | None = None
) -> list[dict[str, Any]]:
    items = registry().list(
        category=category or None, tag=tag or None, domain=domain or None
    )
    return [item.spec() for item in items]


def _pack_summary(pack: AtomPack) -> dict[str, Any]:
    return {
        "name": pack.name,
        "version": pack.version,
        "domain": pack.domain,
        "builtin": pack.source == "builtin",
        "description": pack.description,
        "atom_count": len(pack.atoms),
        "capabilities": list(pack.capabilities()),
        "semantics": getattr(pack.semantics, "name", "generic"),
    }


def list_packs(*, domain: str | None = None) -> list[dict[str, Any]]:
    return [_pack_summary(pack) for pack in pack_registry().list(domain=domain or None)]


def get_pack(name: str) -> dict[str, Any]:
    for pack in pack_registry().list():
        if pack.name == name:
            data = _pack_summary(pack)
            data["atoms"] = [atom.name for atom in pack.atoms]
            data["manifest"] = pack.manifest()
            return data
    raise ApiError("not_found", f"领域包不存在: {name}")


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
_SCENARIO_SUFFIXES = {".yaml", ".yml", ".json"}


def _scenario_entry(path: Path, base: Path, *, source: str) -> dict[str, Any]:
    try:
        scenario_id = path.resolve().relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        scenario_id = path.name
    entry: dict[str, Any] = {
        "id": scenario_id,
        "name": path.stem,
        "source": source,
        "cases": 0,
    }
    try:
        suite = load_suite(path)
    except Exception as exc:  # noqa: BLE001 - 单个场景解析失败不能拖垮整个列表
        entry["error"] = safe_message(exc)
        return entry
    entry["name"] = suite.name or path.stem
    entry["cases"] = len(suite.cases)
    return entry


def list_scenarios(root: Path) -> list[dict[str, Any]]:
    """工作区场景优先，其次内置场景；解析失败的文件也要返回（带 error）。"""
    collected: dict[str, dict[str, Any]] = {}
    workspace_dir = root / "scenarios"
    if workspace_dir.is_dir():
        for path in _scenario_files(workspace_dir):
            entry = _scenario_entry(path, root, source="workspace")
            collected[entry["id"]] = entry
    if BUILTIN_SCENARIOS.is_dir():
        for path in _scenario_files(BUILTIN_SCENARIOS):
            entry = _scenario_entry(path, REPO_ROOT, source="builtin")
            collected.setdefault(entry["id"], entry)
    return sorted(
        collected.values(), key=lambda item: (item["source"] != "workspace", item["id"])
    )


def _scenario_files(base: Path) -> list[Path]:
    try:
        files = [path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in _SCENARIO_SUFFIXES]
    except OSError:
        return []
    return sorted(files)


def _within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def resolve_scenario(root: Path, raw: str) -> Path:
    """把请求里的场景标识解析成绝对路径，并做 realpath 前缀校验（防穿越）。"""
    text = (raw or "").strip()
    if not text or "\x00" in text:
        raise ApiError("bad_request", "scenario 非法")
    allowed_bases = (root, REPO_ROOT)
    for base in allowed_bases:
        try:
            candidate = (base / text).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not (_within(candidate, root) or _within(candidate, BUILTIN_SCENARIOS)):
            continue
        if candidate.is_file() or candidate.is_dir():
            return candidate
    raise ApiError("bad_request", f"场景不存在或超出工作区/内置范围: {text}")


def load_scenario_suite(path: Path) -> TestSuite:
    try:
        if path.is_dir():
            return merge_suites(collect_suites(path), name=path.name or "scenarios")
        return load_suite(path)
    except Exception as exc:
        raise ApiError("bad_request", f"场景解析失败: {safe_message(exc)}") from exc


# --------------------------------------------------------------------------- #
# 运行请求
# --------------------------------------------------------------------------- #
@dataclass
class RunRequest:
    mode: str
    scenario: str = ""
    intent: str = ""
    env: dict[str, Any] = field(default_factory=dict)
    #: None 表示请求里没给，按模式取默认（demo 默认 True，其余默认 False）
    regression: bool | None = None
    no_llm: bool = False


def _coerce_env(env: Any) -> dict[str, Any]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ApiError("bad_request", "env 必须是 string → (string|number|bool) 的对象")
    cleaned: dict[str, Any] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key.strip():
            raise ApiError("bad_request", "env 的键必须是非空字符串")
        if value is None:
            raise ApiError("bad_request", f"env.{key} 不能为空（null）")
        if isinstance(value, (bool, str, int, float)):
            cleaned[key] = value
        else:
            raise ApiError("bad_request", f"env.{key} 必须是 string/number/bool")
    return cleaned


def parse_run_request(payload: Any) -> RunRequest:
    if not isinstance(payload, dict):
        raise ApiError("bad_request", "请求体必须是 JSON 对象")
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise ApiError(
            "bad_request", f"mode 非法: {mode!r}（可选 demo/scenario/intent/regression）"
        )
    scenario = payload.get("scenario")
    scenario_text = scenario.strip() if isinstance(scenario, str) else ""
    intent = payload.get("intent")
    intent_text = intent.strip() if isinstance(intent, str) else ""
    if mode == "scenario" and not scenario_text:
        raise ApiError("bad_request", "mode=scenario 时必须提供 scenario")
    if mode == "intent" and not intent_text:
        raise ApiError("bad_request", "mode=intent 时必须提供非空 intent")
    regression: bool | None = None
    if "regression" in payload:
        regression = bool(payload.get("regression"))
    return RunRequest(
        mode=mode,
        scenario=scenario_text,
        intent=intent_text,
        env=_coerce_env(payload.get("env")),
        regression=regression,
        no_llm=bool(payload.get("no_llm", False)),
    )


def submit_run(jobs: JobManager, root: Path, payload: Any) -> Job:
    request = parse_run_request(payload)
    if request.mode == "demo":
        return jobs.submit("demo", _demo_task(root, request))
    if request.mode == "scenario":
        suite = load_scenario_suite(resolve_scenario(root, request.scenario))
        return jobs.submit("scenario", _scenario_task(root, request, suite))
    if request.mode == "intent":
        return jobs.submit("intent", _intent_task(root, request))
    return jobs.submit("regression", _regression_task(root, request))


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
def _demo_task(root: Path, request: RunRequest) -> Callable[[Job], dict[str, Any]]:
    regression = True if request.regression is None else request.regression

    def task(job: Job) -> dict[str, Any]:
        suite = merge_suites(collect_suites(BUILTIN_SCENARIOS))
        axe = build_axe(root, enable_llm=not request.no_llm)
        with MockAPI(flaky_failures=2) as api:
            env = {"BASE_URL": api.base_url, **request.env}
            return _execute(
                axe,
                job,
                plan=axe.plan(PlanRequest(suite=suite, env=env)),
                request_env=env,
                evolve=True,
                regression=regression,
                title="PanguVerse API 自动化测试闭环报告",
            )

    return task


def _scenario_task(
    root: Path, request: RunRequest, suite: TestSuite
) -> Callable[[Job], dict[str, Any]]:
    def task(job: Job) -> dict[str, Any]:
        axe = build_axe(root, enable_llm=not request.no_llm)
        plan = axe.plan(PlanRequest(suite=suite, env=dict(request.env)))
        return _execute(
            axe,
            job,
            plan=plan,
            request_env=dict(request.env),
            evolve=True,
            regression=bool(request.regression),
            title="PanguVerse 场景运行报告",
        )

    return task


def _intent_task(root: Path, request: RunRequest) -> Callable[[Job], dict[str, Any]]:
    def task(job: Job) -> dict[str, Any]:
        axe = build_axe(root, enable_llm=not request.no_llm)
        plan = axe.plan(
            PlanRequest(
                intent=request.intent,
                env=dict(request.env),
                allow_llm=not request.no_llm,
            )
        )
        return _execute(
            axe,
            job,
            plan=plan,
            request_env=dict(request.env),
            evolve=True,
            regression=bool(request.regression),
            title="PanguVerse 意图运行报告",
        )

    return task


def _regression_task(root: Path, request: RunRequest) -> Callable[[Job], dict[str, Any]]:
    def task(job: Job) -> dict[str, Any]:
        axe = build_axe(root, enable_llm=False)
        plan = axe.regression_plan(env=dict(request.env))
        return _execute(
            axe,
            job,
            plan=plan,
            request_env=dict(request.env),
            evolve=False,
            regression=False,
            title="PanguVerse 回归复用报告",
        )

    return task


# --------------------------------------------------------------------------- #
# 执行编排（与 PanguAxe.loop 同序，额外把阶段回写给 job）
# --------------------------------------------------------------------------- #
def _execute(
    axe: PanguAxe,
    job: Job,
    *,
    plan: TestPlan,
    request_env: dict[str, Any],
    evolve: bool,
    regression: bool,
    title: str,
) -> dict[str, Any]:
    job.add_phase("plan", "ok", f"{plan.size} 个用例来自 {plan.source}")
    if not plan.cases:
        job.add_phase("plan", "failed", "计划中没有任何用例")
        raise RuntimeError("计划中没有任何用例（回归库为空？先跑一次成功的闭环）")

    axe.validate(plan)
    job.add_phase("validate", "ok", "计划校验通过")

    effective_env = {**plan.env, **request_env}
    run_id = uuid.uuid4().hex[:12]
    job.set_run_id(run_id)
    run = axe.executor.run(
        plan,
        env=effective_env,
        run_id=run_id,
        meta={
            "intent": plan.intent,
            "planner": plan.planner,
            "plan_source": plan.source,
            "llm_used": bool(plan.meta.get("llm_used")),
            "heal_enabled": bool(axe.healer),
        },
    )
    summary = run.summary
    job.add_phase(
        "execute",
        "ok" if summary["success"] else "failed",
        f"{summary['passed']}/{summary['total']} 通过，"
        f"{summary['failed']} 失败，{summary['errored']} 异常，"
        f"{summary['http_requests']} 次 HTTP 调用",
    )
    has_heal = any(case.heal_events for case in run.cases)
    job.add_phase("heal", "healed" if has_heal else "ok", _heal_detail(run))

    out_dir = axe.workspace.run_dir(run_id)
    plan_path = out_dir / "plan.json"
    plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = write_all(
        run,
        out_dir,
        formats=DEFAULT_FORMATS,
        title=title,
        suite_name=f"PanguVerse.{plan.source}",
    )
    artifacts["plan"] = str(plan_path)
    job.add_phase("report", "ok", f"产物目录 {out_dir}")

    if evolve:
        evolved = axe.evolution.evolve(run, seal=True)
        job.add_phase(
            "evolve",
            "ok",
            f"回归原子 +{evolved.get('sealed_count', 0)}，"
            f"经验 +{evolved.get('lesson_count', 0)}，"
            f"回归库共 {evolved.get('regression_size', 0)} 条",
        )

    if regression:
        regression_plan = axe.regression_plan(env=effective_env)
        if regression_plan.cases:
            regression_run = axe.executor.run(
                regression_plan,
                env=effective_env,
                run_id=f"{run_id}-reg",
                meta={"regression": True},
            )
            axe.evolution.sealed.mark_run([case.case_id for case in regression_plan.cases])
            write_all(
                regression_run,
                out_dir / "regression",
                formats=DEFAULT_FORMATS,
                title="PanguVerse 回归复用报告",
                suite_name="PanguVerse.regression",
            )
            reg_summary = regression_run.summary
            job.add_phase(
                "regression",
                "ok" if reg_summary["success"] else "failed",
                f"回归复用 {reg_summary['passed']}/{reg_summary['total']} 通过",
            )
        else:
            job.add_phase("regression", "skipped", "回归库为空，先跑一次成功的闭环再试")

    return {
        "run_id": run_id,
        "summary": _job_summary(run),
        "conclusion": _job_conclusion(run),
    }


def _heal_detail(run: RunResult) -> str:
    events = [event for case in run.cases for event in case.heal_events]
    retries = sum(step.retries for case in run.cases for step in case.steps)
    if not events and not retries:
        return "本次运行没有触发自愈"
    parts: list[str] = []
    if retries:
        parts.append(f"步骤级重试 {retries} 次")
    if events:
        kinds = ", ".join(sorted({event.kind for event in events}))
        ok = sum(1 for event in events if event.success)
        parts.append(f"自愈决策 {len(events)} 次（成功 {ok}，类型: {kinds}）")
    return "；".join(parts)


def _job_summary(run: RunResult) -> dict[str, Any]:
    summary = run.summary
    return {
        "total": summary["total"],
        "passed": summary["passed"],
        "failed": summary["failed"],
        "errored": summary["errored"],
        "skipped": summary["skipped"],
        "healed_cases": summary["healed_cases"],
        "duration_ms": summary["duration_ms"],
    }


def _job_conclusion(run: RunResult) -> dict[str, Any]:
    conclusion = report_conclusion(run)
    return {
        "verdict": conclusion["verdict"],
        "success": conclusion["success"],
        "exit_code": conclusion["exit_code"],
    }


# --------------------------------------------------------------------------- #
# 运行历史
# --------------------------------------------------------------------------- #
def runs_root(root: Path) -> Path:
    primary = root / "runs"
    if primary.is_dir():
        return primary
    return root / ".panguverse" / "runs"


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ApiError("bad_request", "run_id 非法（应为 6-32 位小写十六进制）")
    return run_id


def list_runs(root: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    base = runs_root(root)
    if not base.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        report_path = child / "report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(report, dict):
            continue
        entries.append(
            {
                "run_id": report.get("run_id") or child.name,
                "dir": str(child),
                "started_at": report.get("started_at"),
                "finished_at": report.get("finished_at"),
                "duration_ms": report.get("duration_ms"),
                "summary": report.get("summary"),
                "conclusion": report.get("conclusion"),
                "meta": report.get("meta"),
            }
        )
    entries.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return entries[: max(1, limit)]


def _run_dir(root: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    directory = runs_root(root) / run_id
    if not _within(directory.resolve(), runs_root(root).resolve()):
        raise ApiError("bad_request", "run_id 非法")
    return directory


def get_run(root: Path, run_id: str) -> dict[str, Any]:
    directory = _run_dir(root, run_id)
    report_path = directory / "report.json"
    if not report_path.is_file():
        raise ApiError("not_found", "run 不存在")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApiError("internal_error", f"report.json 无法解析: {exc}") from exc
    plan_path = directory / "plan.json"
    plan: Any = None
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = None
    return {"report": report, "plan": plan}


def run_artifact(root: Path, run_id: str, filename: str) -> Path:
    directory = _run_dir(root, run_id)
    path = directory / filename
    if not path.is_file():
        raise ApiError("not_found", f"{filename} 不存在")
    return path


__all__ = [
    "BUILTIN_SCENARIOS",
    "REPO_ROOT",
    "STATUS_BY_CODE",
    "ApiError",
    "RunRequest",
    "build_axe",
    "get_pack",
    "get_run",
    "list_atoms",
    "list_packs",
    "list_runs",
    "list_scenarios",
    "llm_enabled",
    "load_scenario_suite",
    "pack_registry",
    "parse_run_request",
    "registry",
    "resolve_scenario",
    "resolve_workspace",
    "run_artifact",
    "runs_root",
    "safe_message",
    "status_payload",
    "submit_run",
    "validate_run_id",
]
