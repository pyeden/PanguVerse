"""服务层：把 CLI 的闭环能力包装成 Web API 可调用的函数。

这里承担三件事：

1. **只读视图**：状态、原子、领域包、场景、运行历史（全部来自磁盘与注册表）；
2. **运行编排**：demo / scenario / intent / regression 四种模式，逐阶段回写 job.phases；
3. **边界校验**：请求体、路径（realpath 前缀）、run_id 正则，越界一律 400。

所有产物落到 ``<workspace>/runs/<run_id>/``，与 CLI 完全一致；
报告统一走 ``panguverse.report.write_all``（内部已按密钥打码）。
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
from . import audit, insight, phase_store, projects, tasks_store
from .jobs import Job, JobManager, sanitize_message

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parents[1]
BUILTIN_SCENARIOS = REPO_ROOT / "scenarios"

LLM_ENV_KEYS = ("PANGUVERSE_API_KEY", "OPENAI_API_KEY")
VALID_MODES = ("demo", "scenario", "intent", "regression", "workflow")

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
# env 基线与 workspace 边界
# --------------------------------------------------------------------------- #
WEB_STATE_REL = Path(".panguverse") / "web"
LAST_ENV_FILENAME = "last_env.json"
FS_ROOT_REL = Path(".panguverse") / "fs-demo"
_ENV_TOKEN_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}")


def last_env_path(root: Path) -> Path:
    return root / WEB_STATE_REL / LAST_ENV_FILENAME


MOCK_TARGET_KIND = "mock"


def _read_baseline(root: Path) -> dict[str, Any]:
    """读取基线：v2 为 ``{version, env, target}``，兼容 v1 的扁平 env。"""
    try:
        data = json.loads(last_env_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"env": {}, "target": None}
    if not isinstance(data, dict):
        return {"env": {}, "target": None}
    raw_env = data.get("env")
    if isinstance(raw_env, dict):
        target = data.get("target")
        return {
            "env": {str(key): value for key, value in raw_env.items() if isinstance(key, str)},
            "target": target if isinstance(target, dict) else None,
        }
    return {
        "env": {str(key): value for key, value in data.items() if isinstance(key, str)},
        "target": None,
    }


def load_last_env(root: Path) -> dict[str, Any]:
    """最近一次成功运行的解析后 env；缺失或损坏时返回空 dict（不阻断运行）。"""
    return _read_baseline(root)["env"]


def load_last_target(root: Path) -> dict[str, Any] | None:
    """基线的目标标记（如 ``{"kind": "mock", ...}``）；没有则 None。"""
    return _read_baseline(root)["target"]


def demo_target() -> dict[str, Any]:
    """demo 基线的可复现标记：回归时按同样参数重新拉起进程内 mock。"""
    return {"kind": MOCK_TARGET_KIND, "flaky_failures": 2, "token_ttl": 3600.0}


def save_last_env(
    root: Path, env: dict[str, Any], target: dict[str, Any] | None = None
) -> None:
    """原子写入 ``.panguverse/web/last_env.json``（v2：env + target）。"""
    if not env:
        return
    path = last_env_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": 2, "env": {key: env[key] for key in sorted(env)}, "target": target},
        ensure_ascii=False,
        indent=2,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)  # 基线里可能含密码/token，只给当前用户读
    tmp.replace(path)


def default_fs_root(root: Path) -> str:
    return str((root / FS_ROOT_REL).resolve())


def apply_fs_root(root: Path, env: dict[str, Any], request_env: dict[str, Any]) -> dict[str, Any]:
    """Web 层始终把 FS_ROOT 钉在 workspace 内（请求显式给出则尊重）。

    ``packs/local_fs.py`` 在缺 FS_ROOT 时会回退到 ``Path.cwd()``，会污染仓库；
    这里在 Web 层兜住边界，不改包语义。
    """
    resolved = dict(env)
    if "FS_ROOT" not in (request_env or {}):
        resolved["FS_ROOT"] = default_fs_root(root)
    return resolved


def missing_env_vars(plan: TestPlan, env: dict[str, Any]) -> list[str]:
    """扫描计划里仍未解析的 ``${env.X}``（带 ``|default:`` 的视为可兜底）。"""
    text = json.dumps(plan.to_dict(), ensure_ascii=False, default=str)
    missing: set[str] = set()
    for name, filters in _ENV_TOKEN_RE.findall(text):
        if "default:" in filters:
            continue
        value = env.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.add(name)
    return sorted(missing)


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
    workflow_ref: str = ""
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
    workflow_raw = payload.get("workflow_ref")
    workflow_ref = workflow_raw.strip() if isinstance(workflow_raw, str) else ""
    if mode == "workflow" and not workflow_ref:
        raise ApiError("bad_request", "mode=workflow 时必须提供 workflow_ref")
    regression: bool | None = None
    if "regression" in payload:
        regression = bool(payload.get("regression"))
    return RunRequest(
        mode=mode,
        scenario=scenario_text,
        intent=intent_text,
        workflow_ref=workflow_ref,
        env=_coerce_env(payload.get("env")),
        regression=regression,
        no_llm=bool(payload.get("no_llm", False)),
    )


def submit_run(
    jobs: JobManager,
    root: Path,
    payload: Any,
    *,
    on_finish: Callable[[Job], None] | None = None,
    trigger: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> Job:
    request = parse_run_request(payload)
    if request.mode == "demo":
        return jobs.submit("demo", _demo_task(root, request), on_finish=on_finish)
    if request.mode == "scenario":
        suite = load_scenario_suite(resolve_scenario(root, request.scenario))
        return jobs.submit("scenario", _scenario_task(root, request, suite), on_finish=on_finish)
    if request.mode == "intent":
        return jobs.submit("intent", _intent_task(root, request), on_finish=on_finish)
    if request.mode == "workflow":
        return jobs.submit(
            "workflow",
            _workflow_task(
                root, request, trigger=trigger, extra_meta=extra_meta
            ),
            on_finish=on_finish,
        )
    return jobs.submit("regression", _regression_task(root, request), on_finish=on_finish)


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
def _demo_task(root: Path, request: RunRequest) -> Callable[[Job], dict[str, Any]]:
    regression = True if request.regression is None else request.regression

    def task(job: Job) -> dict[str, Any]:
        suite = merge_suites(collect_suites(BUILTIN_SCENARIOS))
        baseline = load_last_env(root)
        axe = build_axe(root, enable_llm=not request.no_llm)
        with MockAPI(flaky_failures=2) as api:
            # 执行目标是进程内临时 mock：请求 env > 基线 > mock 地址
            exec_env = apply_fs_root(
                root, {**baseline, "BASE_URL": api.base_url, **request.env}, request.env
            )
            # 持久化基线不能存临时 mock 端口：改存可复现标记，回归时按同样参数重新拉起
            uses_mock = "BASE_URL" not in request.env
            persist_env = apply_fs_root(root, {**baseline, **suite.env, **request.env}, request.env)
            persist_target = demo_target() if uses_mock else None
            return _execute(
                axe,
                job,
                plan=axe.plan(PlanRequest(suite=suite, env=exec_env)),
                root=root,
                request_env=request.env,
                baseline=baseline,
                persist_env=persist_env,
                persist_target=persist_target,
                evolve=True,
                regression=regression,
                title="PanguVerse API 自动化测试闭环报告",
                run_config={
                    "mode": "demo",
                    "scenario": "",
                    "intent": request.intent,
                    "regression": bool(regression),
                    "no_llm": bool(request.no_llm),
                    "uses_mock": uses_mock,
                },
            )

    return task


def _scenario_task(
    root: Path,
    request: RunRequest,
    suite: TestSuite,
    *,
    attribution: dict[str, str] | None = None,
    extra_meta: dict[str, Any] | None = None,
    trigger: str = "task",
) -> Callable[[Job], dict[str, Any]]:
    def task(job: Job) -> dict[str, Any]:
        return _scenario_execute(
            root,
            job,
            request,
            suite,
            attribution=attribution,
            extra_meta=extra_meta,
            trigger=trigger,
        )

    return task


def _scenario_execute(
    root: Path,
    job: Job,
    request: RunRequest,
    suite: TestSuite,
    *,
    attribution: dict[str, str] | None = None,
    extra_meta: dict[str, Any] | None = None,
    trigger: str = "task",
) -> dict[str, Any]:
    baseline = load_last_env(root)
    # 这里只放「显式给出的 env」：场景文件里的 FS_ROOT 等声明必须能生效，
    # web 默认值统一在 _execute 里按最低优先级兜底（契约 v4.0：suite.env > project.env > 请求）。
    request_env = dict(request.env)
    axe = build_axe(root, enable_llm=not request.no_llm)
    plan = axe.plan(PlanRequest(suite=suite, env=request_env))
    return _execute(
        axe,
        job,
        plan=plan,
        root=root,
        request_env=request_env,
        baseline=baseline,
        evolve=True,
        regression=bool(request.regression),
        title="PanguVerse 场景运行报告",
        run_config={
            "mode": "scenario",
            "scenario": request.scenario,
            "intent": "",
            "regression": bool(request.regression),
            "no_llm": bool(request.no_llm),
            "uses_mock": False,
            "trigger": trigger,
        },
        attribution=attribution,
        extra_meta=extra_meta,
    )


def _suite_run_task(
    root: Path,
    request: RunRequest,
    attribution: dict[str, str],
    *,
    extra_meta: dict[str, Any] | None = None,
    trigger: str = "task",
) -> Callable[[Job], dict[str, Any]]:
    """项目套件运行：场景在 **job 内**才解析，单套件坏文件只失败自己。"""

    def task(job: Job) -> dict[str, Any]:
        suite = load_scenario_suite(resolve_scenario(root, request.scenario))
        return _scenario_execute(
            root,
            job,
            request,
            suite,
            attribution=attribution,
            extra_meta=extra_meta,
            trigger=trigger,
        )

    return task


def _intent_task(root: Path, request: RunRequest) -> Callable[[Job], dict[str, Any]]:
    def task(job: Job) -> dict[str, Any]:
        # 空 env 的 intent 不回落到基线：A8 要求「不提供 env → failed + 缺少环境变量」，
        # 静默继承上一次目标也可能把测试打到错误的服务上。给了部分 env 时才用基线补齐。
        baseline = load_last_env(root) if request.env else {}
        request_env = apply_fs_root(root, dict(request.env), request.env)
        axe = build_axe(root, enable_llm=not request.no_llm)
        plan = axe.plan(
            PlanRequest(
                intent=request.intent,
                env=request_env,
                allow_llm=not request.no_llm,
            )
        )
        return _execute(
            axe,
            job,
            plan=plan,
            root=root,
            request_env=request_env,
            baseline=baseline,
            evolve=True,
            regression=bool(request.regression),
            title="PanguVerse 意图运行报告",
            run_config={
                "mode": "intent",
                "scenario": "",
                "intent": request.intent,
                "regression": bool(request.regression),
                "no_llm": bool(request.no_llm),
                "uses_mock": False,
            },
        )

    return task


def _workflow_task(
    root: Path,
    request: RunRequest,
    *,
    attribution: dict[str, str] | None = None,
    extra_meta: dict[str, Any] | None = None,
    trigger: str | None = None,
) -> Callable[[Job], dict[str, Any]]:
    """运行已发布的工作流（``<wid>@<rev>``）；只读版本，不再规划/不让 AI 改写。"""

    def task(job: Job) -> dict[str, Any]:
        from . import studio  # 局部导入：避免模块级循环

        published = studio.load_published(root, request.workflow_ref)
        plan = studio.plan_from_revision(published)
        baseline = load_last_env(root)
        request_env = dict(request.env)
        axe = build_axe(root, enable_llm=False)
        trigger_name = trigger or ("manual" if attribution is None else "task")
        trigger_id = (extra_meta or {}).get("trigger_id")
        return _execute(
            axe,
            job,
            plan=plan,
            root=root,
            request_env=request_env,
            baseline=baseline,
            evolve=False,
            regression=bool(request.regression),
            title="PanguVerse 工作流运行报告",
            run_config={
                "mode": "workflow",
                "scenario": "",
                "intent": plan.intent,
                "workflow_ref": request.workflow_ref,
                "regression": bool(request.regression),
                "no_llm": True,
                "uses_mock": False,
                "trigger": trigger_name,
                "trigger_id": trigger_id,
            },
            attribution=attribution,
            extra_meta={
                "workflow_ref": request.workflow_ref,
                "trigger_id": trigger_id,
                "trigger": trigger_name,
                **(extra_meta or {}),
            },
        )

    return task


def _regression_task(root: Path, request: RunRequest) -> Callable[[Job], dict[str, Any]]:
    def task(job: Job) -> dict[str, Any]:
        baseline = load_last_env(root)
        target = load_last_target(root)
        axe = build_axe(root, enable_llm=False)

        if target and target.get("kind") == MOCK_TARGET_KIND:
            # 基线来自 demo：按同样参数重新拉起进程内 mock，绝不复用已失效的端口
            mock = MockAPI(
                flaky_failures=int(target.get("flaky_failures", 2)),
                token_ttl=float(target.get("token_ttl", 3600.0)),
            )
            with mock:
                exec_env = apply_fs_root(root, {**baseline, **request.env}, request.env)
                if "BASE_URL" not in request.env:
                    exec_env["BASE_URL"] = mock.base_url
                plan = axe.regression_plan(env=exec_env)
                return _execute(
                    axe,
                    job,
                    plan=plan,
                    root=root,
                    request_env=exec_env,
                    baseline=baseline,
                    persist_env=baseline,  # 不把新的临时端口写回基线
                    persist_target=target,
                    evolve=False,
                    regression=False,
                    title="PanguVerse 回归复用报告",
                    run_config={
                        "mode": "regression",
                        "scenario": "",
                        "intent": "",
                        "regression": False,
                        "no_llm": False,
                        # 本次目标由基线标记重新拉起：rerun 时不能沿用旧端口
                        "uses_mock": True,
                    },
                )

        exec_env = apply_fs_root(root, {**baseline, **request.env}, request.env)
        plan = axe.regression_plan(env=exec_env)
        return _execute(
            axe,
            job,
            plan=plan,
            root=root,
            request_env=exec_env,
            baseline=baseline,
            evolve=False,
            regression=False,
            title="PanguVerse 回归复用报告",
            run_config={
                "mode": "regression",
                "scenario": "",
                "intent": "",
                "regression": False,
                "no_llm": False,
                "uses_mock": False,
            },
        )

    return task


# --------------------------------------------------------------------------- #
# 执行编排（与 PanguAxe.loop 同序，额外把阶段回写给 job）
# --------------------------------------------------------------------------- #
def _install_phase_persistence(root: Path, job: Job) -> None:
    """job 进入终态时把 phases 原子落盘；失败只当旁路错误。"""
    previous = job.on_finish

    def on_finish(finished: Job) -> None:
        if previous is not None:
            with contextlib.suppress(Exception):  # 原有终态回调失败不阻断 phases 落盘
                previous(finished)
        with contextlib.suppress(Exception):  # 写盘失败不得改变运行结论（A7）
            snapshot = finished.to_dict()
            run_id = str(snapshot.get("run_id") or "")
            if run_id:
                phase_store.write_phases(
                    root,
                    run_id,
                    job_id=str(snapshot.get("job_id") or ""),
                    phases=snapshot.get("phases") or [],
                    finished_at=snapshot.get("finished_at"),
                    status=str(snapshot.get("status") or ""),
                )

    job.on_finish = on_finish


def _execute(
    axe: PanguAxe,
    job: Job,
    *,
    plan: TestPlan,
    root: Path,
    request_env: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    persist_env: dict[str, Any] | None = None,
    persist_target: dict[str, Any] | None = None,
    evolve: bool,
    regression: bool,
    title: str,
    run_config: dict[str, Any] | None = None,
    attribution: dict[str, str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .executions import install_cancellation

    install_cancellation(axe, job)
    _install_phase_persistence(root, job)
    job.add_phase("plan", "ok", f"{plan.size} 个用例来自 {plan.source}")
    if not plan.cases:
        job.add_phase("plan", "failed", "计划中没有任何用例")
        raise RuntimeError("计划中没有任何用例（回归库为空？先跑一次成功的闭环）")

    # 执行 env = 最近一次成功基线 < 计划 env < 请求 env。
    # FS_ROOT 默认值（workspace 内）只在**本次三层都没声明**时兜底：
    # 显式声明（suite.env / project.env / 场景文件 / 请求）永远优先，陈旧的
    # 基线 FS_ROOT 也不能顶掉默认值（契约 v4.0 §1）。
    declared_env = {**plan.env, **request_env}
    candidate_env = {**(baseline or {}), **declared_env}
    effective_env = apply_fs_root(root, candidate_env, declared_env)
    missing = missing_env_vars(plan, effective_env)
    if missing:
        message = "缺少环境变量: " + ", ".join(missing)
        job.add_phase("validate", "failed", message)
        # 先落 failed，避免线程层再包一层异常类名前缀；禁止「done 但全 errored」的假成功
        job.fail(message)
        raise RuntimeError(message)

    axe.validate(plan)
    job.add_phase("validate", "ok", "计划校验通过")
    if job.cancel_requested:
        job.cancel("用户取消")
        return {"run_id": "", "summary": None, "conclusion": None, "cancelled": True}

    run_id = uuid.uuid4().hex[:12]
    job.set_run_id(run_id)
    meta: dict[str, Any] = {
        "intent": plan.intent,
        "planner": plan.planner,
        "plan_source": plan.source,
        "llm_used": bool(plan.meta.get("llm_used")),
        "heal_enabled": bool(axe.healer),
        # 供 /api/runs/{id}/config 与 rerun 还原原始请求（新增字段，不改旧字段语义）
        "run_config": dict(run_config or {}),
    }
    # 运行归属（v4.0 §1）：additive，只在从项目/套件发起时写入；旧报告没有这两个键
    for key in ("project_id", "suite_id"):
        value = (attribution or {}).get(key)
        if isinstance(value, str) and value:
            meta[key] = value
    for key, value in (extra_meta or {}).items():
        if isinstance(key, str) and key and value not in (None, ""):
            meta[key] = value
    run = axe.executor.run(
        plan,
        env=effective_env,
        run_id=run_id,
        meta=meta,
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
    # 产出物收集是**旁路**：失败只告警，绝不改变运行结论（契约 A7）
    try:
        from . import artifacts as artifacts_mod

        collected = artifacts_mod.collect_run(
            root, run, project_id=(attribution or {}).get("project_id")
        )
        count = int(collected.get("count") or 0)
        warnings = [str(item) for item in collected.get("warnings") or []]
        detail = f"已收集 {count} 个产出物" if count else "本次运行没有产出物"
        if warnings:
            job.add_phase("artifacts", "warning", f"{detail}；" + "；".join(warnings))
        else:
            job.add_phase("artifacts", "ok" if count else "skipped", detail)
    except Exception as exc:  # noqa: BLE001 - A7：收集失败不得影响运行结论
        job.add_phase("artifacts", "warning", f"产出物收集失败: {sanitize_message(exc)}")
    cancelled = bool(job.cancel_requested)
    if cancelled:
        job.cancel("用户取消")
    _write_execution_record(
        root,
        run_id,
        job=job,
        summary=summary,
        run_config=run_config,
        attribution=attribution,
        extra_meta=extra_meta,
    )

    # 全部用例 errored（目标不可达等基础设施问题）不算「测试没通过」：判 failed 并给首个原因
    if not cancelled and summary["passed"] == 0 and summary["errored"] == summary["total"]:
        reason = _first_error_message(run) or "全部用例执行错误（目标服务不可达？）"
        job.fail(reason)
        raise RuntimeError(reason)

    # 只有成功的运行才更新 env 基线（失败运行可能带着错误的地址/凭据）
    if summary["success"] and not cancelled:
        save_last_env(
            root,
            persist_env if persist_env is not None else effective_env,
            persist_target,
        )

    if evolve and not cancelled:
        evolved = axe.evolution.evolve(run, seal=True)
        job.add_phase(
            "evolve",
            "ok",
            f"回归原子 +{evolved.get('sealed_count', 0)}，"
            f"经验 +{evolved.get('lesson_count', 0)}，"
            f"回归库共 {evolved.get('regression_size', 0)} 条",
        )

    if regression and not cancelled:
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
        "cancelled": cancelled,
    }


def _write_execution_record(
    root: Path,
    run_id: str,
    *,
    job: Job,
    summary: dict[str, Any],
    run_config: dict[str, Any] | None,
    attribution: dict[str, str] | None,
    extra_meta: dict[str, Any] | None,
) -> None:
    """把执行元信息落到 ``.panguverse/executions/<run_id>/execution.json``。

    目录 ``0700``、文件 ``0600``、原子写；这是**旁路**记录，写失败不影响运行结果。
    """
    try:
        directory = root / ".panguverse" / "executions" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            directory.chmod(0o700)
        config = run_config or {}
        payload = {
            "execution_id": run_id,
            "run_id": run_id,
            "job_id": job.job_id,
            "trigger": str(config.get("trigger") or "task"),
            "mode": str(config.get("mode") or ""),
            "status": job.status
            if job.status in ("done", "failed", "cancelled")
            else ("ok" if summary.get("success") else "failed"),
            "summary": summary,
            "project_id": (attribution or {}).get("project_id"),
            "suite_id": (attribution or {}).get("suite_id"),
            "workflow_ref": (extra_meta or {}).get("workflow_ref")
            or config.get("workflow_ref"),
            "schedule_id": (extra_meta or {}).get("schedule_id")
            or config.get("schedule_id"),
            "trigger_id": (extra_meta or {}).get("trigger_id")
            or config.get("trigger_id"),
            "created_at": time.time(),
        }
        path = directory / "execution.json"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=".execution.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        return


def _first_error_message(run: RunResult) -> str:
    """取第一条失败/异常步骤的可读原因（供 job.error 使用，已打码）。"""
    for case in run.cases:
        error = case.error or {}
        message = error.get("message") if isinstance(error, dict) else None
        if message:
            return sanitize_message(message)
        for step in case.steps:
            if step.status not in ("failed", "error"):
                continue
            step_error = step.error or {}
            message = step_error.get("message") if isinstance(step_error, dict) else None
            if message:
                return sanitize_message(message)
            if step.message:
                return sanitize_message(step.message)
    return ""


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
                # 旧记录没有 phases 时为 []，供 executions 层回退/兼容。
                "phases": report.get("phases") if isinstance(report.get("phases"), list) else [],
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


# --------------------------------------------------------------------------- #
# AI 总结（契约 tasks/web-ai-insight-spec.md §1–§4）
# --------------------------------------------------------------------------- #
INSIGHT_STATE_REL = Path(".panguverse") / "web" / "insights"


def insight_dir(root: Path) -> Path:
    return root / INSIGHT_STATE_REL


def insight_cache_path(root: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    return insight_dir(root) / f"{run_id}.json"


def read_insight_cache(root: Path, run_id: str) -> dict[str, Any] | None:
    """读缓存；报告 ``mtime`` 晚于缓存即视为过期（契约 §1）。"""
    path = insight_cache_path(root, run_id)
    report_path = runs_root(root) / run_id / "report.json"
    try:
        if not path.is_file() or not report_path.is_file():
            return None
        if report_path.stat().st_mtime > path.stat().st_mtime:
            return None
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # v3.0.1 起响应必须带 corrections[]；旧缓存视为过期，避免继续返回未校正的模型输出
    if not isinstance(cached, dict) or "corrections" not in cached:
        return None
    return cached


def write_insight_cache(root: Path, run_id: str, payload: dict[str, Any]) -> Path:
    """原子写：目录 0700、文件 0600（契约 §1）。"""
    directory = insight_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    target = insight_cache_path(root, run_id)
    fd, tmp = tempfile.mkstemp(prefix=".insight-", suffix=".tmp", dir=directory)
    written = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, default=str)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        written = True
    finally:
        if not written:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return target


def get_insight(root: Path, run_id: str) -> dict[str, Any]:
    """``GET``：有缓存原样返回，**不自动生成**（避免打开页面就烧 token）。"""
    get_run(root, run_id)  # run_id 非法 400 / run 不存在 404
    cached = read_insight_cache(root, run_id)
    return cached if cached is not None else {"available": False}


def _default_insight_llm() -> Any:
    """没有密钥是常态：直接返回 None，让 insight 走规则兜底。"""
    if not llm_enabled():
        return None
    from ..axe.llm import LLMClient

    return LLMClient(timeout=insight.LLM_TIMEOUT)


def generate_insight(
    root: Path, run_id: str, payload: Any = None, *, llm_client: Any = None
) -> dict[str, Any]:
    """``POST``：``refresh=false`` 且缓存有效时直接复用；LLM 失败由 insight 内部降级。"""
    data = _as_body(payload)
    refresh = bool(data.get("refresh", False))
    run = get_run(root, run_id)
    if not refresh:
        cached = read_insight_cache(root, run_id)
        if cached is not None:
            return cached
    report = run.get("report")
    if not isinstance(report, dict):
        raise ApiError("internal_error", "report.json 结构非法")
    plan = run.get("plan") if isinstance(run.get("plan"), dict) else None
    client = llm_client if llm_client is not None else _default_insight_llm()
    result = insight.build_payload(report, plan, run_id=run_id, llm_client=client)
    write_insight_cache(root, run_id, result)
    # 报告级 AI 总结自动成为产出物；失败不阻断总结本身
    try:
        from . import artifacts as artifacts_mod

        artifacts_mod.record_insight(
            root, run_id, result.get("summary_md"), report=report
        )
    except Exception:  # noqa: BLE001 - 产出物是旁路
        pass
    return result




# --------------------------------------------------------------------------- #
# 任务定义（CRUD，契约 §2/§3.1–§3.3）
# --------------------------------------------------------------------------- #
TASK_NAME_MAX = 80
#: PATCH/覆盖请求里表示「沿用已保存值」的占位值（与 HTTP 打码保持同一字面量）
ENV_KEEP = "***"


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError("bad_request", "name 必须是字符串")
    name = value.strip()
    if not name:
        raise ApiError("bad_request", "name 不能为空")
    if len(name) > TASK_NAME_MAX:
        raise ApiError("bad_request", f"name 不能超过 {TASK_NAME_MAX} 个字符")
    return name


def _clean_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in VALID_MODES:
        raise ApiError(
            "bad_request",
            f"mode 非法: {value!r}（可选 demo/scenario/intent/regression/workflow）",
        )
    return value


def _clean_text(value: Any, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError("bad_request", f"{field} 必须是字符串")
    return value.strip()


def _validate_task_target(
    root: Path,
    mode: str,
    scenario: str,
    intent: str,
    workflow_ref: str = "",
) -> None:
    if mode == "scenario":
        if not scenario:
            raise ApiError("bad_request", "mode=scenario 时必须提供 scenario")
        resolve_scenario(root, scenario)  # realpath 前缀校验：越界/不存在一律 400
    if mode == "intent" and not intent:
        raise ApiError("bad_request", "mode=intent 时必须提供非空 intent")
    if mode == "workflow":
        if not workflow_ref:
            raise ApiError("bad_request", "mode=workflow 时必须提供 workflow_ref")
        _clean_workflow_ref(root, workflow_ref)


def _merge_task_env(current: dict[str, Any], patch: Any) -> dict[str, Any]:
    """PATCH 的 env 键级合并：``"***"``/``""`` 沿用、``null`` 删除、其余覆盖。"""
    if not isinstance(patch, dict):
        raise ApiError("bad_request", "env 必须是 string → (string|number|bool) 的对象")
    merged = dict(current)
    for key, value in patch.items():
        if not isinstance(key, str) or not key.strip():
            raise ApiError("bad_request", "env 的键必须是非空字符串")
        if value is None:
            merged.pop(key, None)
            continue
        if isinstance(value, str) and value in ("", ENV_KEEP):
            continue  # 前端把已保存的密钥显示为 *** / 留空：视为不动旧值
        if isinstance(value, (bool, str, int, float)):
            merged[key] = value
        else:
            raise ApiError("bad_request", f"env.{key} 必须是 string/number/bool（null 表示删除）")
    return merged


def _as_body(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ApiError("bad_request", "请求体必须是 JSON 对象")
    return payload


def task_summary(task: dict[str, Any]) -> dict[str, Any]:
    """列表摘要：**只暴露 env 键名**，不返回任何值。"""
    env = task.get("env")
    keys = sorted(str(key) for key in env) if isinstance(env, dict) else []
    summary = {
        "task_id": task.get("task_id", ""),
        "name": task.get("name", ""),
        "mode": task.get("mode", ""),
        "scenario": task.get("scenario", ""),
        "intent": task.get("intent", ""),
        "updated_at": task.get("updated_at"),
        "last_run_id": task.get("last_run_id", ""),
        "last_status": task.get("last_status", ""),
        "env_keys": keys,
    }
    if task.get("workflow_ref"):
        summary["workflow_ref"] = task["workflow_ref"]
    if task.get("archived_at"):
        summary["archived_at"] = task["archived_at"]
    return summary


def list_tasks(root: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    tasks = tasks_store.list_tasks(root)
    if not include_archived:
        tasks = [task for task in tasks if not task.get("archived_at")]
    return [task_summary(task) for task in tasks]


def archive_task(
    root: Path, task_id: str, *, archived: bool = True, actor: str = "local"
) -> dict[str, Any]:
    """软归档 / 取消归档；不删配置，历史运行与报告不受影响。"""
    task = tasks_store.read_task(root, task_id)
    old = bool(task.get("archived_at"))
    if archived:
        task["archived_at"] = time.time()
    else:
        task.pop("archived_at", None)
    task["updated_at"] = time.time()
    written = tasks_store.write_task(root, task)
    audit.append(
        root,
        actor=actor,
        action="task.archive" if archived else "task.unarchive",
        target=f"task:{task_id}",
        from_=old,
        to=archived,
        note="",
    )
    return written


def get_task(root: Path, task_id: str) -> dict[str, Any]:
    return tasks_store.read_task(root, task_id)


def create_task(root: Path, payload: Any) -> dict[str, Any]:
    data = _as_body(payload)
    name = _clean_name(data.get("name"))
    mode = _clean_mode(data.get("mode"))
    scenario = _clean_text(data.get("scenario"), field="scenario")
    intent = _clean_text(data.get("intent"), field="intent")
    workflow_ref = _clean_text(data.get("workflow_ref"), field="workflow_ref")
    env = _coerce_env(data.get("env"))
    _validate_task_target(root, mode, scenario, intent, workflow_ref)
    now = time.time()
    task = {
        "task_id": tasks_store.new_task_id(),
        "name": name,
        "mode": mode,
        "scenario": scenario,
        "intent": intent,
        "env": env,
        "regression": bool(data.get("regression", False)),
        "no_llm": bool(data.get("no_llm", False)),
        "created_at": now,
        "updated_at": now,
        "last_run_id": "",
        "last_status": "",
    }
    if workflow_ref:
        task["workflow_ref"] = workflow_ref
    return tasks_store.write_task(root, task)


def ensure_published_workflow_task(root: Path, wid: str, rev: int) -> dict[str, Any]:
    """发布成功后幂等确保存在一条 ``mode:"workflow"`` 任务定义。

    以 ``wid`` 为幂等维度：已有同 ``wid`` 的 workflow 任务时只把
    ``workflow_ref`` 指向新 rev 并刷新 ``updated_at``，保留 task_id/name/env；
    没有时才按发布记录里的工作流名新建。
    """
    from . import studio

    ref = f"{wid}@{int(rev)}"
    published = studio.load_published(root, ref)
    workflow = published.get("workflow") or {}
    record = published.get("record") or {}

    prefix = f"{wid}@"
    candidates = [
        task
        for task in tasks_store.list_tasks(root)
        if task.get("mode") == "workflow"
        and not task.get("archived_at")
        and isinstance(task.get("workflow_ref"), str)
        and str(task["workflow_ref"]).startswith(prefix)
    ]
    now = time.time()
    if candidates:
        # list_tasks 按 updated_at 倒序：复用最近使用的那条，避免重复建任务。
        task = candidates[0]
        task["workflow_ref"] = ref
        task["updated_at"] = now
        return tasks_store.write_task(root, task)

    raw_name = str(record.get("name") or workflow.get("name") or wid).strip() or wid
    name = f"{raw_name} 任务"
    if len(name) > TASK_NAME_MAX:
        name = f"{raw_name[: TASK_NAME_MAX - 3]} 任务"
    task = {
        "task_id": tasks_store.new_task_id(),
        "name": name,
        "mode": "workflow",
        "scenario": "",
        "intent": str(workflow.get("intent") or record.get("intent") or ""),
        "env": {},
        "regression": False,
        "no_llm": False,
        "workflow_ref": ref,
        "created_at": now,
        "updated_at": now,
        "last_run_id": "",
        "last_status": "",
    }
    return tasks_store.write_task(root, task)


def update_task(root: Path, task_id: str, payload: Any) -> dict[str, Any]:
    task = tasks_store.read_task(root, task_id)
    data = _as_body(payload)
    updated = dict(task)
    if "name" in data:
        updated["name"] = _clean_name(data.get("name"))
    if "mode" in data:
        updated["mode"] = _clean_mode(data.get("mode"))
    if "scenario" in data:
        updated["scenario"] = _clean_text(data.get("scenario"), field="scenario")
    if "intent" in data:
        updated["intent"] = _clean_text(data.get("intent"), field="intent")
    if "workflow_ref" in data:
        workflow_ref = _clean_text(data.get("workflow_ref"), field="workflow_ref")
        if workflow_ref:
            updated["workflow_ref"] = workflow_ref
        else:
            updated.pop("workflow_ref", None)
    if "env" in data:
        updated["env"] = _merge_task_env(
            task.get("env") if isinstance(task.get("env"), dict) else {},
            data.get("env"),
        )
    if "regression" in data:
        updated["regression"] = bool(data.get("regression"))
    if "no_llm" in data:
        updated["no_llm"] = bool(data.get("no_llm"))
    _validate_task_target(
        root,
        str(updated.get("mode") or ""),
        str(updated.get("scenario") or ""),
        str(updated.get("intent") or ""),
        str(updated.get("workflow_ref") or ""),
    )
    updated["updated_at"] = time.time()
    return tasks_store.write_task(root, updated)


def delete_task(root: Path, task_id: str) -> None:
    """只删任务定义；``runs/`` 下的历史产物一律不动。"""
    tasks_store.delete_task(root, task_id)


def _env_override(value: Any) -> dict[str, Any]:
    """本次运行/重跑的 env 覆盖：``"***"``/空串/``null`` 视为「不覆盖」。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError("bad_request", "env 必须是 string → (string|number|bool) 的对象")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ApiError("bad_request", "env 的键必须是非空字符串")
        if item is None:
            continue
        if isinstance(item, str) and item in ("", ENV_KEEP):
            continue
        if isinstance(item, (bool, str, int, float)):
            cleaned[key] = item
        else:
            raise ApiError("bad_request", f"env.{key} 必须是 string/number/bool")
    return cleaned


def _task_run_payload(task: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    env = dict(task.get("env") or {})
    env.update(overrides)
    return {
        "mode": task.get("mode") or "",
        "scenario": task.get("scenario") or "",
        "intent": task.get("intent") or "",
        "workflow_ref": task.get("workflow_ref") or "",
        "env": env,
        "regression": bool(task.get("regression", False)),
        "no_llm": bool(task.get("no_llm", False)),
    }


def record_task_run(root: Path, task_id: str, job: Job) -> None:
    """运行结束后回写 ``last_run_id``/``last_status``（尽力而为，绝不影响 job 状态）。"""
    try:
        task = tasks_store.read_task(root, task_id)
    except (tasks_store.TaskStoreError, OSError):
        return
    if job.run_id:
        task["last_run_id"] = job.run_id
    task["last_status"] = job.status
    try:
        tasks_store.write_task(root, task)
    except (tasks_store.TaskStoreError, OSError):
        return


def run_task(
    jobs: JobManager, root: Path, task_id: str, payload: Any = None
) -> Job:
    task = tasks_store.read_task(root, task_id)
    data = _as_body(payload)
    overrides = _env_override(data.get("env")) if "env" in data else {}
    body = _task_run_payload(task, overrides)
    return submit_run(
        jobs,
        root,
        body,
        on_finish=lambda job: record_task_run(root, task_id, job),
        trigger="task",
    )


# --------------------------------------------------------------------------- #
# 从运行历史还原配置 / 重跑（契约 §3.4）
# --------------------------------------------------------------------------- #
def _report_meta(report: dict[str, Any]) -> dict[str, Any]:
    meta = report.get("meta")
    return meta if isinstance(meta, dict) else {}


def _report_run_config(report: dict[str, Any]) -> dict[str, Any]:
    config = _report_meta(report).get("run_config")
    return config if isinstance(config, dict) else {}


def _disk_env(report: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """磁盘上的**真实** env：plan.json 最准（未打码），report 兜底。"""
    report_plan = report.get("plan")
    candidates: list[Any] = [
        plan.get("env"),
        report_plan.get("env") if isinstance(report_plan, dict) else None,
        report.get("env"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return {str(key): value for key, value in candidate.items() if isinstance(key, str)}
    return {}


def _safe_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return safe_message(text) if text.strip() else ""


def _first_failure(report: dict[str, Any]) -> dict[str, Any] | None:
    """第一个非 passed 步骤（用例名 + 步骤名 + message/expected/actual）。"""
    cases = report.get("cases")
    if not isinstance(cases, list):
        return None
    for case in cases:
        if not isinstance(case, dict):
            continue
        steps = case.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                if step.get("status") not in ("failed", "error"):
                    continue
                error = step.get("error")
                error = error if isinstance(error, dict) else {}
                return {
                    "case": case.get("name") or case.get("case_id") or "",
                    "step": step.get("name") or step.get("step_id") or step.get("atom") or "",
                    "message": _safe_text(error.get("message") or step.get("message")),
                    "expected": error.get("expected"),
                    "actual": error.get("actual"),
                    "retryable": bool(error.get("retryable", False)),
                }
        if case.get("status") in ("failed", "error"):
            error = case.get("error")
            error = error if isinstance(error, dict) else {}
            if error:
                return {
                    "case": case.get("name") or case.get("case_id") or "",
                    "step": "",
                    "message": _safe_text(error.get("message")),
                    "expected": error.get("expected"),
                    "actual": error.get("actual"),
                    "retryable": bool(error.get("retryable", False)),
                }
    return None


def _run_config_core(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = get_run(root, run_id)
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    meta = _report_meta(report)
    config = _report_run_config(report)
    plan_source = str(meta.get("plan_source") or plan.get("source") or "unknown")

    mode = config.get("mode")
    if mode not in VALID_MODES:
        # 旧运行没有 run_config：能判定就判定，否则按契约回 scenario 并在 plan_source 里说明来源
        mode = "regression" if plan_source == "memory" else "scenario"
    scenario = config.get("scenario") if isinstance(config.get("scenario"), str) else ""
    intent = config.get("intent") if isinstance(config.get("intent"), str) else ""
    workflow_ref = (
        config.get("workflow_ref") if isinstance(config.get("workflow_ref"), str) else ""
    )
    if not workflow_ref and isinstance(meta.get("workflow_ref"), str):
        workflow_ref = meta["workflow_ref"]
    if not intent and mode == "intent":
        intent = str(plan.get("intent") or "")

    resolved = {
        "mode": mode,
        "scenario": scenario,
        "intent": intent,
        "env": _disk_env(report, plan),
        "regression": bool(config.get("regression", False)),
        "no_llm": bool(config.get("no_llm", False)),
        "plan_source": plan_source,
        "error": _first_failure(report),
    }
    if workflow_ref:
        resolved["workflow_ref"] = workflow_ref
    return resolved, meta


def run_config(root: Path, run_id: str) -> dict[str, Any]:
    """``GET /api/runs/{run_id}/config``：env 为磁盘真值，HTTP 出口统一打码。"""
    return _run_config_core(root, run_id)[0]


def rerun(jobs: JobManager, root: Path, run_id: str, payload: Any = None) -> Job:
    """以磁盘真实 env 为基线重跑；请求里的 ``"***"`` 一律视为沿用。"""
    config, meta = _run_config_core(root, run_id)
    data = _as_body(payload)
    overrides = _env_override(data.get("env")) if "env" in data else {}
    env = dict(config["env"])
    env.update(overrides)
    mode = config["mode"]
    raw_config = meta.get("run_config")
    uses_mock = bool(raw_config.get("uses_mock")) if isinstance(raw_config, dict) else False
    if uses_mock and "BASE_URL" not in overrides:
        # 进程内 mock 的端口随上次进程消失，重跑必须重新拉起而不是沿用旧端口
        env.pop("BASE_URL", None)
    if mode == "scenario" and not config["scenario"]:
        raise ApiError("bad_request", "无法从该运行还原场景配置（缺少 scenario）")
    body = {
        "mode": mode,
        "scenario": config["scenario"],
        "intent": config["intent"],
        "workflow_ref": config.get("workflow_ref", ""),
        "env": env,
        "regression": config["regression"],
        "no_llm": config["no_llm"],
    }
    if bool(data.get("save_as_task", False)):
        name = _clean_name(data.get("name") or f"重跑 {run_id}")
        task = create_task(root, {**body, "name": name})
        return run_task(jobs, root, task["task_id"])
    return submit_run(jobs, root, body)


def save_run_as_task(root: Path, run_id: str, payload: Any = None) -> dict[str, Any]:
    """``POST /api/runs/{run_id}/save_as_task``：只建任务、不运行、不产生新 run。

    env 由服务端从磁盘（``plan.json`` 优先）复制**真值**，前端即使回传 ``"***"``
    也不会被当成值；``mode/scenario/intent/regression/no_llm`` 走 ``run_config``
    与旧报告退化规则，目标无法判定时直接 400（不猜）。
    """
    config, meta = _run_config_core(root, run_id)
    data = _as_body(payload)
    mode = config["mode"]
    scenario = config["scenario"]
    intent = config["intent"]
    if mode == "scenario" and not scenario:
        raise ApiError("bad_request", "无法从该运行还原场景配置（缺少 scenario）")
    if mode == "intent" and not intent:
        raise ApiError("bad_request", "无法从该运行还原意图配置（缺少 intent）")
    env = dict(config["env"])
    raw_config = meta.get("run_config")
    uses_mock = bool(raw_config.get("uses_mock")) if isinstance(raw_config, dict) else False
    if uses_mock:
        env.pop("BASE_URL", None)  # mock 端口随进程消失，不能写进任务定义
    name = _clean_name(data.get("name") or f"重跑 {run_id}")
    return create_task(
        root,
        {
            "name": name,
            "mode": mode,
            "scenario": scenario,
            "intent": intent,
            "env": env,
            "regression": config["regression"],
            "no_llm": config["no_llm"],
        },
    )


# --------------------------------------------------------------------------- #
# 项目 / 套件（契约 tasks/web-refactor-spec.md v4.0 §1–§2）
# --------------------------------------------------------------------------- #
PROJECT_NAME_MAX = TASK_NAME_MAX
BASE_URL_MAX = 500
TAGS_MAX = 20
TAG_MAX = 40
RUN_STATUSES = ("all", "passed", "failed", "errored", "done")
#: 历史筛选里表示「没有项目/套件归属」的保留值（旧报告）
UNCLASSIFIED = {"unclassified", "none", "_"}


def _clean_base_url(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError("bad_request", "base_url 必须是字符串")
    text = value.strip()
    if len(text) > BASE_URL_MAX:
        raise ApiError("bad_request", f"base_url 不能超过 {BASE_URL_MAX} 个字符")
    if any(char in text for char in ("\x00", "\n", "\r")):
        raise ApiError("bad_request", "base_url 含非法字符")
    return text


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError("bad_request", "tags 必须是字符串数组")
    if len(value) > TAGS_MAX:
        raise ApiError("bad_request", f"tags 最多 {TAGS_MAX} 个")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError("bad_request", "tags 必须是字符串数组")
        tag = item.strip()
        if not tag:
            raise ApiError("bad_request", "tags 不能包含空字符串")
        if len(tag) > TAG_MAX:
            raise ApiError("bad_request", f"单个 tag 不能超过 {TAG_MAX} 个字符")
        if tag not in tags:
            tags.append(tag)
    return tags


def _clean_suite_scenario(root: Path, value: Any) -> str:
    scenario = _clean_text(value, field="scenario")
    if not scenario:
        raise ApiError("bad_request", "scenario 不能为空")
    resolve_scenario(root, scenario)  # realpath 前缀校验：越界/不存在一律 400
    return scenario


def _clean_workflow_ref(root: Path, value: Any) -> str:
    """套件绑定已发布流程：``<wid>@<rev>``，并确认发布记录存在（只读发布版本）。"""
    ref = _clean_text(value, field="workflow_ref")
    if not ref:
        return ""
    from . import studio

    try:
        studio.load_published(root, ref)
    except studio.StudioError as exc:
        raise ApiError("bad_request", f"workflow_ref 不可用: {exc.message}") from exc
    return ref


def _find_suite(project: dict[str, Any], suite_id: str) -> dict[str, Any]:
    projects.validate_suite_id(suite_id)
    for suite in project.get("suites") or []:
        if isinstance(suite, dict) and suite.get("suite_id") == suite_id:
            return suite
    raise ApiError("not_found", "套件不存在")


def create_project(root: Path, payload: Any) -> dict[str, Any]:
    data = _as_body(payload)
    now = time.time()
    project = {
        "project_id": projects.new_project_id(),
        "name": _clean_name(data.get("name")),
        "base_url": _clean_base_url(data.get("base_url")),
        "env": _coerce_env(data.get("env")),
        "suites": [],
        "created_at": now,
        "updated_at": now,
    }
    return projects.write_project(root, project)


def update_project(root: Path, project_id: str, payload: Any) -> dict[str, Any]:
    data = _as_body(payload)

    def mutate(project: dict[str, Any]) -> None:
        if "name" in data:
            project["name"] = _clean_name(data.get("name"))
        if "base_url" in data:
            project["base_url"] = _clean_base_url(data.get("base_url"))
        if "env" in data:
            current = project.get("env") if isinstance(project.get("env"), dict) else {}
            project["env"] = _merge_task_env(current, data.get("env"))
        project["updated_at"] = time.time()

    project, _ = projects.modify_project(root, project_id, mutate)
    return project


def delete_project(root: Path, project_id: str) -> None:
    """只删项目文件；``runs/`` 下任何历史产物都不动（A50）。"""
    projects.delete_project(root, project_id)


def add_suite(root: Path, project_id: str, payload: Any) -> dict[str, Any]:
    data = _as_body(payload)
    scenario_raw = _clean_text(data.get("scenario"), field="scenario")
    workflow_ref = _clean_workflow_ref(root, data.get("workflow_ref"))
    if bool(scenario_raw) == bool(workflow_ref):
        raise ApiError("bad_request", "scenario 与 workflow_ref 必须且只能提供一个")
    scenario = _clean_suite_scenario(root, scenario_raw) if scenario_raw else ""
    suite = {
        "suite_id": projects.new_suite_id(),
        "name": _clean_name(data.get("name")),
        "scenario": scenario,
        "workflow_ref": workflow_ref,
        "env": _coerce_env(data.get("env")),
        "tags": _clean_tags(data.get("tags")),
    }

    def mutate(project: dict[str, Any]) -> None:
        project.setdefault("suites", []).append(suite)
        project["updated_at"] = time.time()

    projects.modify_project(root, project_id, mutate)
    return suite


def update_suite(
    root: Path, project_id: str, suite_id: str, payload: Any
) -> dict[str, Any]:
    data = _as_body(payload)

    def mutate(project: dict[str, Any]) -> dict[str, Any]:
        suite = _find_suite(project, suite_id)
        if "name" in data:
            suite["name"] = _clean_name(data.get("name"))
        if "scenario" in data:
            suite["scenario"] = _clean_text(data.get("scenario"), field="scenario")
        if "workflow_ref" in data:
            suite["workflow_ref"] = _clean_text(data.get("workflow_ref"), field="workflow_ref")
        if "scenario" in data or "workflow_ref" in data:
            scenario = str(suite.get("scenario") or "")
            workflow_ref = str(suite.get("workflow_ref") or "")
            if bool(scenario) == bool(workflow_ref):
                raise ApiError("bad_request", "scenario 与 workflow_ref 必须且只能提供一个")
            if scenario:
                suite["scenario"] = _clean_suite_scenario(root, scenario)
            if workflow_ref:
                suite["workflow_ref"] = _clean_workflow_ref(root, workflow_ref)
        if "env" in data:
            current = suite.get("env") if isinstance(suite.get("env"), dict) else {}
            suite["env"] = _merge_task_env(current, data.get("env"))
        if "tags" in data:
            suite["tags"] = _clean_tags(data.get("tags"))
        project["updated_at"] = time.time()
        return dict(suite)

    _, updated = projects.modify_project(root, project_id, mutate)
    return updated


def delete_suite(root: Path, project_id: str, suite_id: str) -> None:
    def mutate(project: dict[str, Any]) -> None:
        _find_suite(project, suite_id)
        project["suites"] = [
            suite
            for suite in project.get("suites") or []
            if not (isinstance(suite, dict) and suite.get("suite_id") == suite_id)
        ]
        project["updated_at"] = time.time()

    projects.modify_project(root, project_id, mutate)


# --------------------------------------------------------------------------- #
# 运行索引（按报告 meta 归类，旧报告 = 未归类）
# --------------------------------------------------------------------------- #
def _run_status(report: dict[str, Any]) -> str:
    """报告 → 运行状态：``errored | passed | failed``（缺 summary 的老报告 → done）。"""
    summary = report.get("summary")
    if not isinstance(summary, dict) or "total" not in summary:
        return "done"
    if _as_int(summary.get("errored")):
        return "errored"
    if summary.get("success"):
        return "passed"
    return "failed"


def scan_runs(root: Path) -> list[dict[str, Any]]:
    """扫描 ``runs/``，返回带归属与状态的最新在前列表（含原始 report，供聚合）。"""
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
        meta = report.get("meta") if isinstance(report.get("meta"), dict) else None
        project_id = meta.get("project_id") if isinstance(meta, dict) else None
        suite_id = meta.get("suite_id") if isinstance(meta, dict) else None
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
                "status": _run_status(report),
                "project_id": project_id if isinstance(project_id, str) and project_id else None,
                "suite_id": suite_id if isinstance(suite_id, str) and suite_id else None,
                "report": report,
            }
        )
    entries.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return entries


def _public_run(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "report"}


def _group_project_runs(root: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in scan_runs(root):
        project_id = entry.get("project_id")
        if project_id:
            grouped.setdefault(project_id, []).append(entry)
    return grouped


def _pass_rate(entries: list[dict[str, Any]]) -> dict[str, int] | None:
    if not entries:
        return None
    seen = False
    passed = 0
    total = 0
    for entry in entries:
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        seen = True
        passed += _as_int(summary.get("passed"))
        total += _as_int(summary.get("total"))
    return {"passed": passed, "total": total} if seen else None


def _last_run(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    entry = entries[0]
    summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
    conclusion = entry.get("conclusion") if isinstance(entry.get("conclusion"), dict) else {}
    return {
        "run_id": entry.get("run_id", ""),
        "status": entry.get("status", "done"),
        "verdict": conclusion.get("verdict", ""),
        "at": entry.get("started_at"),
        "passed": _as_int(summary.get("passed")),
        "total": _as_int(summary.get("total")),
    }


def list_project_summaries(
    root: Path, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    grouped = _group_project_runs(root)
    items: list[dict[str, Any]] = []
    for project in projects.list_projects(root):
        if project.get("archived_at") and not include_archived:
            continue
        entries = grouped.get(project["project_id"], [])
        item = {
            "project_id": project["project_id"],
            "name": project.get("name", ""),
            "base_url": project.get("base_url", ""),
            "suite_count": len(project.get("suites") or []),
            "last_run": _last_run(entries),
            "pass_rate": _pass_rate(entries),
        }
        if project.get("archived_at"):
            item["archived_at"] = project["archived_at"]
        items.append(item)
    return items


def archive_project(
    root: Path, project_id: str, *, archived: bool = True, actor: str = "local"
) -> dict[str, Any]:
    """软归档项目；只改项目定义，不动 runs/ 历史。"""
    from . import projects as project_store

    old = False

    def mutate(project: dict[str, Any]) -> None:
        nonlocal old
        old = bool(project.get("archived_at"))
        if archived:
            project["archived_at"] = time.time()
        else:
            project.pop("archived_at", None)
        project["updated_at"] = time.time()

    written, _ = project_store.modify_project(root, project_id, mutate)
    audit.append(
        root,
        actor=actor,
        action="project.archive" if archived else "project.unarchive",
        target=f"project:{project_id}",
        from_=old,
        to=archived,
        note="",
    )
    return written


def get_project_detail(root: Path, project_id: str) -> dict[str, Any]:
    project = projects.read_project(root, project_id)
    entries = _group_project_runs(root).get(project_id, [])
    detail = dict(project)
    detail["suite_count"] = len(project.get("suites") or [])
    detail["last_run"] = _last_run(entries)
    detail["pass_rate"] = _pass_rate(entries)
    return detail


def _failure_top(entries: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    """按报告里每个失败步骤的 ``failure_kind`` 聚合（最多 3 条，带 case_ids）。"""
    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        report = entry.get("report") if isinstance(entry.get("report"), dict) else {}
        cases = report.get("cases")
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict) or case.get("status") not in ("failed", "error"):
                continue
            kinds: set[str] = set()
            steps = case.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict) or step.get("status") not in ("failed", "error"):
                        continue
                    error = step.get("error")
                    if isinstance(error, dict):
                        kind = error.get("failure_kind") or error.get("kind")
                        if isinstance(kind, str) and kind:
                            kinds.add(kind)
            error = case.get("error")
            if isinstance(error, dict):
                kind = error.get("failure_kind") or error.get("kind")
                if isinstance(kind, str) and kind:
                    kinds.add(kind)
            if not kinds:
                kinds.add("unknown")
            case_id = str(case.get("case_id") or case.get("name") or "")
            for kind in kinds:
                bucket = buckets.setdefault(kind, {"kind": kind, "count": 0, "cases": []})
                bucket["count"] += 1
                if case_id and case_id not in bucket["cases"]:
                    bucket["cases"].append(case_id)
    ordered = sorted(buckets.values(), key=lambda item: (-item["count"], item["kind"]))
    return ordered[: max(1, limit)]


def project_summary(root: Path, project_id: str, *, limit: int = 5) -> dict[str, Any]:
    """概览：最近 ``limit`` 条运行 + 全量通过率 + 全量失败归类（口径见 docs/web-api.md）。"""
    projects.read_project(root, project_id)  # 不存在 → 404
    entries = _group_project_runs(root).get(project_id, [])
    return {
        "recent_runs": [_public_run(entry) for entry in entries[: max(1, limit)]],
        "pass_rate": _pass_rate(entries),
        "failure_top": _failure_top(entries),
    }


# --------------------------------------------------------------------------- #
# 历史筛选分页（GET /api/runs/query）
# --------------------------------------------------------------------------- #
def _match_owner(value: str | None, wanted: str | None, field: str) -> bool:
    if not wanted or wanted == "all":
        return True
    if wanted in UNCLASSIFIED:
        return not value
    if field == "project":
        projects.validate_project_id(wanted)
    else:
        projects.validate_suite_id(wanted)
    return value == wanted


def query_runs(
    root: Path,
    *,
    project: str | None = None,
    suite: str | None = None,
    status: str = "all",
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    if status not in RUN_STATUSES:
        raise ApiError(
            "bad_request", f"status 非法: {status!r}（可选 {'/'.join(RUN_STATUSES)}）"
        )
    entries = [
        entry
        for entry in scan_runs(root)
        if _match_owner(entry.get("project_id"), project, "project")
        and _match_owner(entry.get("suite_id"), suite, "suite")
        # done = 已跑完的任意结论；其余按报告 summary 精确匹配
        and (status in ("all", "done") or entry.get("status") == status)
    ]
    return {
        "total": len(entries),
        "items": [_public_run(entry) for entry in entries[offset : offset + limit]],
    }


def run_summary(root: Path, run_id: str) -> dict[str, Any] | None:
    """批量聚合用：读磁盘报告的 ``summary``（读不到返回 None，绝不抛错）。"""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        return None
    try:
        report = json.loads(
            (runs_root(root) / run_id / "report.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    summary = report.get("summary") if isinstance(report, dict) else None
    return summary if isinstance(summary, dict) else None


# --------------------------------------------------------------------------- #
# 从项目发起批量运行（§2–§3）
# --------------------------------------------------------------------------- #
def _merged_suite_env(
    root: Path, project: dict[str, Any], suite: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """优先级：``suite.env`` > ``project.env`` > 本次请求覆盖（契约 §1 硬要求）。"""
    project_env = project.get("env") if isinstance(project.get("env"), dict) else {}
    suite_env = suite.get("env") if isinstance(suite.get("env"), dict) else {}
    merged = dict(overrides)
    merged.update(project_env)
    merged.update(suite_env)
    return merged


def start_project_batch(
    jobs: JobManager,
    batches: Any,
    root: Path,
    project_id: str,
    payload: Any = None,
    *,
    trigger: str = "task",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``POST /api/projects/{pid}/run``：登记全部 job 后交给 BatchManager 串行执行。

    ``trigger``/``extra_meta`` 由调度器注入（``schedule|webhook|manual`` +
    ``schedule_id``），普通项目运行保持 ``task`` 不变。
    """
    project = projects.read_project(root, project_id)
    data = _as_body(payload)
    suites = list(project.get("suites") or [])
    requested = data.get("suite_ids")
    if requested is not None:
        if not isinstance(requested, list) or any(
            not isinstance(item, str) for item in requested
        ):
            raise ApiError("bad_request", "suite_ids 必须是字符串数组")
        if requested:  # 空数组 = 不限定 → 全部套件（与前端「一键全跑」一致）
            known = {
                suite.get("suite_id"): suite
                for suite in suites
                if isinstance(suite, dict)
            }
            for suite_id in requested:
                projects.validate_suite_id(suite_id)
                if suite_id not in known:
                    raise ApiError("bad_request", f"套件不存在: {suite_id}")
            wanted = set(requested)
            suites = [suite for suite in suites if suite.get("suite_id") in wanted]
    if not suites:
        raise ApiError("bad_request", "项目没有可运行的套件")
    overrides = _env_override(data.get("env")) if "env" in data else {}

    items = []
    for suite in suites:
        suite_id = projects.validate_suite_id(suite.get("suite_id"))
        suite_name = str(suite.get("name") or "")
        scenario = str(suite.get("scenario") or "")
        workflow_ref = str(suite.get("workflow_ref") or "")
        attribution = {"project_id": project_id, "suite_id": suite_id}
        env = _merged_suite_env(root, project, suite, overrides)
        if workflow_ref:
            request = RunRequest(
                mode="workflow",
                workflow_ref=workflow_ref,
                env=env,
            )
            job = jobs.create("workflow")
            task = _workflow_task(
                root,
                request,
                attribution=attribution,
                extra_meta=extra_meta,
                trigger=trigger,
            )
        else:
            request = RunRequest(mode="scenario", scenario=scenario, env=env)
            job = jobs.create("scenario")
            task = _suite_run_task(
                root,
                request,
                attribution,
                extra_meta=extra_meta,
                trigger=trigger,
            )
        items.append(
            {
                "suite_id": suite_id,
                "name": suite_name,
                "job": job,
                "task": task,
            }
        )

    from .batches import BatchItem  # 局部导入：保持 service → batches 无模块级依赖

    batch = batches.submit(
        project_id,
        [
            BatchItem(
                suite_id=item["suite_id"],
                name=item["name"],
                job=item["job"],
                task=item["task"],
            )
            for item in items
        ],
    )
    return {
        "batch_id": batch.batch_id,
        "jobs": [
            {"job_id": item["job"].job_id, "suite_id": item["suite_id"]} for item in items
        ],
    }


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "BUILTIN_SCENARIOS",
    "MOCK_TARGET_KIND",
    "REPO_ROOT",
    "STATUS_BY_CODE",
    "ApiError",
    "RunRequest",
    "add_suite",
    "apply_fs_root",
    "build_axe",
    "create_project",
    "create_task",
    "default_fs_root",
    "delete_project",
    "delete_suite",
    "delete_task",
    "demo_target",
    "get_pack",
    "get_project_detail",
    "get_run",
    "get_task",
    "list_atoms",
    "list_packs",
    "list_project_summaries",
    "list_runs",
    "list_scenarios",
    "list_tasks",
    "llm_enabled",
    "load_last_env",
    "load_last_target",
    "load_scenario_suite",
    "missing_env_vars",
    "pack_registry",
    "parse_run_request",
    "project_summary",
    "query_runs",
    "record_task_run",
    "registry",
    "rerun",
    "resolve_scenario",
    "resolve_workspace",
    "run_artifact",
    "run_config",
    "run_summary",
    "run_task",
    "runs_root",
    "safe_message",
    "save_last_env",
    "save_run_as_task",
    "scan_runs",
    "start_project_batch",
    "status_payload",
    "submit_run",
    "task_summary",
    "update_project",
    "update_suite",
    "update_task",
    "validate_run_id",
]
