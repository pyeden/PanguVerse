"""PanguVerse 命令行：把闭环能力暴露成可编排的命令。

```bash
panguverse demo e2e                       # 一键端到端闭环演示（零依赖、离线可跑）
panguverse atoms                          # 查看当前原子库
panguverse plan --intent "登录下单支付"     # 只看规划结果，不执行
panguverse run scenarios/api_smoke.yaml   # 执行场景并产出报告
panguverse loop --intent "..." --regression  # 完整闭环（含沉淀与回归复用）
panguverse regression                     # 只跑已沉淀的回归用例
panguverse demo api --port 8899           # 单独启动内置 mock API
```
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .axe.axe import PanguAxe
from .axe.memory import Workspace
from .axe.plan import PlanRequest, TestPlan
from .core.errors import PanguError
from .core.registry import default_registry
from .demo.e2e import collect_suites, merge_suites, run_demo
from .demo.mock_api import main as mock_api_main
from .engine.executor import ExecutorSettings
from .engine.result import RunResult
from .report import DEFAULT_FORMATS, write_all
from .spec.loader import TestSuite, load_suite

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_ERROR = 2


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    _setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.handler(args) or 0)
    except PanguError as exc:
        print(f"\n❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"\n❌ 文件不存在: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panguverse",
        description="🪓 PanguVerse —— AI + 自动化的原子编排框架（API 自动化测试闭环）",
    )
    parser.add_argument("--version", action="version", version="panguverse 0.1.0")
    sub = parser.add_subparsers(dest="command")

    # ---- demo ---------------------------------------------------------- #
    demo = sub.add_parser("demo", help="内置演示：一键闭环 / mock API")
    demo_sub = demo.add_subparsers(dest="demo_command")

    demo_e2e = demo_sub.add_parser("e2e", help="一键跑通完整闭环（含回归复用）")
    demo_e2e.add_argument("--scenarios", default=None, help="场景目录或文件")
    demo_e2e.add_argument("--flaky-failures", type=int, default=2, help="mock API 前 N 次注入 500")
    demo_e2e.add_argument("--no-regression", action="store_true", help="不跑回归复用")
    _add_common(demo_e2e, workspace=True, report_dir=True)
    demo_e2e.set_defaults(handler=_cmd_demo_e2e)

    demo_api = demo_sub.add_parser("api", help="启动内置 mock API（被测量）")
    demo_api.add_argument("--host", default="127.0.0.1")
    demo_api.add_argument("--port", type=int, default=8899)
    demo_api.add_argument("--flaky-failures", type=int, default=2)
    demo_api.add_argument("--token-ttl", type=float, default=3600.0)
    demo_api.add_argument("--verbose", action="store_true")
    demo_api.set_defaults(
        handler=lambda args: mock_api_main(
            [
                "--host", args.host,
                "--port", str(args.port),
                "--flaky-failures", str(args.flaky_failures),
                "--token-ttl", str(args.token_ttl),
                *(["--verbose"] if args.verbose else []),
            ]
        )
    )

    # ---- run / loop / plan -------------------------------------------- #
    run = sub.add_parser("run", help="执行场景文件（不做 AI 规划）")
    run.add_argument("scenario", help="场景文件或目录")
    _add_common(run, workspace=True, report_dir=True, formats=True, env=True, filters=True, exec_opts=True)
    run.add_argument("--no-evolve", action="store_true", help="不沉淀用例与经验")
    run.set_defaults(handler=_cmd_run)

    loop = sub.add_parser("loop", help="完整闭环：规划 → 执行 → 自愈 → 报告 → 沉淀 → 回归")
    loop.add_argument("-f", "--scenario", default=None, help="场景文件或目录（与 --intent 可同时给）")
    loop.add_argument("-i", "--intent", default="", help="自然语言测试意图（无密钥时用规则模板兜底）")
    _add_common(loop, workspace=True, report_dir=True, formats=True, env=True, filters=True, exec_opts=True)
    loop.add_argument("--regression", action="store_true", help="沉淀后再跑一遍回归复用")
    loop.add_argument("--no-evolve", action="store_true", help="不沉淀用例与经验")
    loop.add_argument("--no-llm", action="store_true", help="禁用 AI 规划（强制规则兜底）")
    loop.set_defaults(handler=_cmd_loop)

    plan = sub.add_parser("plan", help="只做规划与校验，不执行")
    plan.add_argument("-f", "--scenario", default=None)
    plan.add_argument("-i", "--intent", default="")
    plan.add_argument("--json", action="store_true", help="输出 JSON")
    _add_common(plan, workspace=True, env=True, filters=True)
    plan.add_argument("--no-llm", action="store_true")
    plan.set_defaults(handler=_cmd_plan)

    regression = sub.add_parser("regression", help="执行已沉淀的回归用例")
    _add_common(regression, workspace=True, report_dir=True, formats=True, env=True)
    regression.add_argument("--tag", default="regression", help="按标签筛选沉淀用例")
    regression.add_argument("--json", action="store_true")
    regression.set_defaults(handler=_cmd_regression)

    # ---- 辅助命令 ------------------------------------------------------ #
    atoms = sub.add_parser("atoms", help="列出原子库")
    atoms.add_argument("--category", default=None)
    atoms.add_argument("--tag", default=None)
    atoms.add_argument("--json", action="store_true")
    atoms.set_defaults(handler=_cmd_atoms)

    status = sub.add_parser("status", help="查看工作区、原子库、LLM 与沉淀状态")
    status.add_argument("--json", action="store_true")
    _add_common(status, workspace=True)
    status.set_defaults(handler=_cmd_status)

    report = sub.add_parser("report", help="从 report.json 重新生成报告")
    report.add_argument("source", help="运行目录或 report.json 路径")
    report.add_argument("--format", action="append", default=None, choices=["html", "junit", "json"])
    report.set_defaults(handler=_cmd_report)

    init = sub.add_parser("init", help="初始化工作区与示例场景")
    init.add_argument("--dir", default=".", help="项目目录")
    init.set_defaults(handler=_cmd_init)

    return parser


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    workspace: bool = False,
    report_dir: bool = False,
    formats: bool = False,
    env: bool = False,
    filters: bool = False,
    exec_opts: bool = False,
) -> None:
    if workspace:
        parser.add_argument("--workspace", default=".panguverse", help="工作区目录（默认 .panguverse）")
    if report_dir:
        parser.add_argument("--report-dir", default=None, help="报告输出目录（默认工作区 runs/<run_id>）")
    if formats:
        parser.add_argument(
            "--format", action="append", default=None, choices=["json", "html", "junit"],
            help="报告格式，可重复（默认全部）",
        )
    if env:
        parser.add_argument("--env", action="append", default=[], metavar="K=V", help="覆盖环境变量，可重复")
    if filters:
        parser.add_argument("--tag", action="append", default=[], help="只跑指定标签的用例")
        parser.add_argument("--case", action="append", default=[], help="只跑指定用例 id")
    if exec_opts:
        parser.add_argument("--no-heal", action="store_true", help="关闭自愈器（保留步骤级重试）")
        parser.add_argument("--fail-fast", action="store_true", help="首个用例失败即停止")
        parser.add_argument("--retry", type=int, default=None, metavar="N", help="全局默认重试次数")
        parser.add_argument("--json", action="store_true", help="额外输出 JSON 摘要")


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #
def _cmd_run(args: argparse.Namespace) -> int:
    suite = _load_target(args.scenario)
    env = _parse_env(args.env)
    axe = _build_axe(args)
    plan = axe.plan(PlanRequest(suite=suite, env=env, tags=args.tag, case_ids=args.case))
    run = axe.run(plan, env=env)
    if not args.no_evolve:
        summary = axe.evolution.evolve(run, seal=True)
        print(f"📦 沉淀: 回归原子 +{summary['sealed_count']}，经验 +{summary['lesson_count']}")
    _finish(run, args, axe)
    return run.exit_code


def _cmd_loop(args: argparse.Namespace) -> int:
    env = _parse_env(args.env)
    axe = _build_axe(args, enable_llm=not args.no_llm)
    request = _build_request(args, env)
    result = axe.loop(
        request,
        out_dir=args.report_dir,
        formats=_formats(args),
        evolve=not args.no_evolve,
        regression=args.regression,
    )
    _print_run(result.run)
    if result.regression is not None:
        print("\n🔁 回归复用（直接执行沉淀用例，无需重新规划）")
        _print_run(result.regression)
    print("\n" + result.text())
    if args.json:
        print(json.dumps(_json_summary(result.run, result.plan), ensure_ascii=False, indent=2))
    return result.exit_code


def _cmd_plan(args: argparse.Namespace) -> int:
    env = _parse_env(args.env)
    axe = _build_axe(args, enable_llm=not args.no_llm)
    request = _build_request(args, env)
    plan = axe.plan(request)
    problems = plan.validate(axe.registry)
    if args.json:
        print(json.dumps({**plan.to_dict(), "problems": problems}, ensure_ascii=False, indent=2))
    else:
        print("🪓 计划\n" + plan.describe())
        for note in plan.notes:
            print(f"  · {note}")
        for warning in plan.warnings:
            print(f"  ⚠️ {warning}")
        if problems:
            print("\n❌ 计划校验未通过:")
            for problem in problems:
                print(f"  - {problem}")
            return EXIT_ERROR
        print("\n✅ 计划校验通过（未执行；加 --json 可看完整 JSON）")
    return EXIT_ERROR if problems else EXIT_OK


def _cmd_regression(args: argparse.Namespace) -> int:
    env = _parse_env(args.env)
    axe = _build_axe(args)
    plan = axe.regression_plan(env=env, tag=args.tag)
    if not plan.cases:
        print(f"⚠️ 回归库为空（{axe.workspace.registry_path}）；先跑一次成功的闭环再看")
        return EXIT_OK
    run = axe.run(plan, env=env)
    axe.evolution.sealed.mark_run([case.case_id for case in plan.cases])
    _finish(run, args, axe)
    return run.exit_code


def _cmd_atoms(args: argparse.Namespace) -> int:
    registry = default_registry()
    items = registry.list(category=args.category, tag=args.tag)
    if args.json:
        print(json.dumps([item.spec() for item in items], ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"⚛️  原子库（{len(items)} 个）")
    current_category = None
    for item in items:
        if item.category != current_category:
            current_category = item.category
            print(f"\n[{current_category}]")
        required = ", ".join(item.required) or "-"
        print(f"  {item.name:<20} {item.description}")
        print(f"  {'':<20} 必填: {required}")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    axe = _build_axe(args)
    data = axe.status()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    print("🪓 PanguVerse 状态")
    print(f"  原子库: {data['atoms']} 个")
    print(f"  工作区: {data['workspace']}")
    print(f"  回归库: {data['sealed']} 条，经验库: {data['lessons']} 条")
    print(f"  HTTP 后端: {data['http_backend']}")
    llm = (data.get("planner") or {}).get("llm") or {}
    if llm.get("available"):
        print(f"  AI: {llm.get('backend')} / {llm.get('model')} ✅")
    else:
        print(f"  AI: 未启用（{llm.get('reason', '未配置')}）→ 使用离线规则规划")
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    from .report import html_report, junit

    source = Path(args.source)
    json_path = source / "report.json" if source.is_dir() else source
    if not json_path.exists():
        print(f"❌ 找不到 {json_path}", file=sys.stderr)
        return EXIT_ERROR
    report = json.loads(json_path.read_text(encoding="utf-8"))
    out_dir = json_path.parent
    written: list[str] = []
    for fmt in args.format or ["html"]:
        if fmt == "html":
            written.append(str(html_report.write_html(report, out_dir / "report.html")))
        elif fmt == "junit":
            written.append(str(junit.write_junit(report, out_dir / "junit.xml")))
        else:
            written.append(str(json_path))
    for path in written:
        print(f"📄 {path}")
    return EXIT_OK


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    workspace = Workspace(root / ".panguverse").ensure()
    scenarios = root / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    sample = scenarios / "sample_health.yaml"
    if not sample.exists():
        sample.write_text(_SAMPLE_SCENARIO, encoding="utf-8")
    print(f"✅ 初始化完成\n  工作区: {workspace.root}\n  示例场景: {sample}")
    print("  下一步: panguverse demo e2e")
    return EXIT_OK


def _cmd_demo_e2e(args: argparse.Namespace) -> int:
    result = run_demo(
        scenarios=args.scenarios,
        workspace=args.workspace,
        out_dir=args.report_dir,
        regression=not args.no_regression,
        flaky_failures=args.flaky_failures,
    )
    return result.exit_code


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _build_axe(args: argparse.Namespace, *, enable_llm: bool = True) -> PanguAxe:
    settings = ExecutorSettings(
        heal=not getattr(args, "no_heal", False),
        stop_on_first_failure=bool(getattr(args, "fail_fast", False)),
    )
    if getattr(args, "retry", None):
        from .core.molecule import RetryPolicy

        settings.default_retry = RetryPolicy(
            attempts=max(1, int(args.retry)), delay_ms=200, backoff=2.0,
            on=("network", "timeout", "server_error"),
        )
    workspace = getattr(args, "workspace", ".panguverse")
    return PanguAxe(workspace=workspace, settings=settings, enable_llm=enable_llm)


def _build_request(args: argparse.Namespace, env: dict[str, Any]) -> PlanRequest:
    suite = None
    if getattr(args, "scenario", None):
        suite = _load_target(args.scenario)
    intent = getattr(args, "intent", "") or ""
    if suite is None and not intent:
        raise PanguError("需要 --intent 或 -f/--scenario 指定测试意图/场景")
    return PlanRequest(
        intent=intent,
        suite=suite,
        env={**(suite.env if suite else {}), **env},
        tags=list(getattr(args, "tag", []) or []),
        case_ids=list(getattr(args, "case", []) or []),
        allow_llm=not getattr(args, "no_llm", False),
    )


def _load_target(target: str | Path) -> TestSuite:
    path = Path(target)
    if path.is_dir():
        return merge_suites(collect_suites(path), name=path.name or "scenarios")
    return load_suite(path)


def _parse_env(pairs: Sequence[str] | None) -> dict[str, Any]:
    env: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise PanguError(f"--env 需要 K=V 形式，实际: {pair}")
        key, value = pair.split("=", 1)
        env[key.strip()] = _coerce(value.strip())
    return env


def _coerce(text: str) -> Any:
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.isdigit():
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def _formats(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(args.format) if getattr(args, "format", None) else DEFAULT_FORMATS


def _finish(run: RunResult, args: argparse.Namespace, axe: PanguAxe) -> None:
    out_dir = args.report_dir or axe.workspace.run_dir(run.run_id)
    artifacts = write_all(run, out_dir, formats=_formats(args))
    run.artifacts.update(artifacts)
    _print_run(run)
    for name, path in artifacts.items():
        print(f"📄 {name}: {path}")
    if getattr(args, "json", False):
        print(json.dumps(_json_summary(run, axe.last_plan), ensure_ascii=False, indent=2))


def _print_run(run: RunResult) -> None:
    print("\n🪓 执行结果")
    for case in run.cases:
        icon = {"passed": "✅", "failed": "❌", "error": "💥", "skipped": "⏭️"}.get(case.status, "•")
        heal = f" 🔁自愈×{len(case.heal_events)}" if case.heal_events else ""
        print(f"  {icon} {case.case_id:<24} {case.duration_ms:>7.0f}ms  "
              f"{case.http_requests} 请求{heal}  {case.name}")
        for step in case.steps:
            if step.status in ("failed", "error"):
                message = (step.error or {}).get("message", step.message)
                print(f"      └─ {step.step_id}[{step.atom}] {step.status}: {message}")
            elif step.retries:
                print(f"      └─ {step.step_id} 重试 {step.retries} 次后通过")
        for event in case.heal_events:
            mark = "成功" if event.success else "失败"
            print(f"      └─ 自愈[{event.kind}/{event.action}] {mark}: {event.reason}")
            if event.suggestion:
                print(f"         建议: {event.suggestion}")
    summary = run.summary
    verdict = "✅ 全部通过" if summary["success"] else "❌ 存在失败"
    print(f"  {verdict}：{summary['passed']}/{summary['total']} 通过，"
          f"失败 {summary['failed']}，异常 {summary['errored']}，"
          f"自愈 {summary['healed_cases']}，耗时 {summary['duration_ms'] / 1000:.2f}s")


def _json_summary(run: RunResult, plan: TestPlan | None) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "summary": run.summary,
        "exit_code": run.exit_code,
        "plan": {"source": plan.source, "planner": plan.planner, "cases": plan.size} if plan else None,
        "artifacts": run.artifacts,
        "failed_cases": [
            {"case_id": case.case_id, "status": case.status,
             "error": (case.error or {}).get("message")}
            for case in run.cases
            if case.status in ("failed", "error")
        ],
    }


def _setup_stdio() -> None:
    import contextlib

    for stream in (sys.stdout, sys.stderr):
        # 非 UTF-8 控制台（Windows 常见）下也要能打印中文
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


_SAMPLE_SCENARIO = """name: 示例场景
env:
  BASE_URL: http://127.0.0.1:8899
cases:
  - id: health
    name: 健康检查
    tags: [smoke]
    steps:
      - id: health
        use: http_request
        with:
          method: GET
          url: ${env.BASE_URL}/api/health
        assert:
          status: 200
          json:
            - path: status
              equals: ok
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
