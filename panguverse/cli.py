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
    atoms.add_argument("--domain", default=None, help="按领域过滤（api / filesystem / core …）")
    atoms.add_argument("--json", action="store_true")
    atoms.set_defaults(handler=_cmd_atoms)

    packs = sub.add_parser("packs", help="领域包管理：列出 / 查看 / 校验 / 导出清单")
    packs_sub = packs.add_subparsers(dest="packs_command")
    packs_list = packs_sub.add_parser("list", help="列出已加载的领域包")
    packs_list.add_argument("--domain", default=None)
    packs_list.add_argument("--json", action="store_true")
    packs_list.set_defaults(handler=_cmd_packs_list)

    packs_show = packs_sub.add_parser("show", help="查看某个领域包的清单")
    packs_show.add_argument("name")
    packs_show.add_argument("--json", action="store_true")
    packs_show.set_defaults(handler=_cmd_packs_show)

    packs_validate = packs_sub.add_parser("validate", help="校验内置包，或校验指定的包文件/目录")
    packs_validate.add_argument("path", nargs="?", default=None, help="*.pack.yaml 文件或目录")
    packs_validate.add_argument("--strict", action="store_true", help="更严格的检查（版本/描述必填）")
    packs_validate.set_defaults(handler=_cmd_packs_validate)

    packs_manifest = packs_sub.add_parser("manifest", help="导出平台上所有领域包的清单（可评审/可共享）")
    packs_manifest.add_argument("--out", default=None, help="输出 JSON 文件路径")
    packs_manifest.set_defaults(handler=_cmd_packs_manifest)

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
        parser.add_argument(
            "--packs", action="append", default=[], metavar="PATH",
            help="加载声明式领域包（*.pack.yaml 文件或目录），可重复",
        )
        parser.add_argument(
            "--allow-capabilities", default=None, metavar="LIST",
            help="授权运行时能力（逗号分隔，如 network,filesystem）；给了就严格校验",
        )


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
    registry = _registry_with_extra_packs(args)
    items = registry.list(category=args.category, tag=args.tag, domain=getattr(args, "domain", None))
    if args.json:
        print(json.dumps([item.spec() for item in items], ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"⚛️  原子库（{len(items)} 个，领域: {', '.join(registry.domains())}）")
    current = None
    for item in items:
        group = f"{item.domain}/{item.category}"
        if group != current:
            current = group
            print(f"\n[{group}]")
        required = ", ".join(item.required) or "-"
        effects = "/".join(str(effect) for effect in item.effects) or "pure"
        print(f"  {item.name:<20} {item.description}")
        print(f"  {'':<20} 必填: {required}  ·  副作用: {effects}")
    return EXIT_OK


def _cmd_packs_list(args: argparse.Namespace) -> int:
    packs = _pack_registry().list(domain=args.domain)
    if args.json:
        print(json.dumps([pack.manifest() for pack in packs], ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"📦 领域包（{len(packs)} 个）")
    for pack in packs:
        print(f"\n  {pack.name}  v{pack.version}  领域={pack.domain}  [{pack.source}]")
        print(f"    {pack.description}")
        print(f"    原子 {len(pack.atoms)} 个 · 能力: {', '.join(pack.capabilities()) or '无'}")
        print(f"    语义: {getattr(pack.semantics, 'name', 'generic')}")
    print("\n提示: panguverse packs show <name> 查看详情；packs validate 做自检")
    return EXIT_OK


def _cmd_packs_show(args: argparse.Namespace) -> int:
    try:
        pack = _pack_registry().get(args.name)
    except KeyError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_ERROR
    manifest = pack.manifest()
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"📦 {manifest['name']} v{manifest['version']}（{manifest['domain']}）")
    print(f"   {manifest['description']}")
    print(f"   来源: {manifest['source']}  路径: {manifest['path'] or '-'}")
    print(f"   能力需求: {', '.join(manifest['capabilities']) or '无'}")
    semantics = pack.semantics.describe()
    print(f"   领域语义: {json.dumps(semantics, ensure_ascii=False)}")
    print(f"\n   原子（{len(pack.atoms)}）:")
    for atom in pack.atoms:
        print(f"     {atom.name:<20} {atom.description}")
        if atom.capabilities:
            print(f"     {'':<20} 能力: {', '.join(atom.capabilities)}  ·  副作用: {'/'.join(atom.effects) or '-'}")
    return EXIT_OK


def _cmd_packs_validate(args: argparse.Namespace) -> int:
    from .core.pack import load_pack_dir, load_pack_file

    problems: list[str] = []
    if args.path:
        target = Path(args.path)
        try:
            packs = load_pack_dir(target) if target.is_dir() else [load_pack_file(target)]
        except PanguError as exc:
            print(f"❌ 加载失败: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"🔍 校验 {len(packs)} 个包（{target}）")
        for pack in packs:
            problems.extend(f"[{pack.name}] {item}" for item in pack.validate())
    else:
        registry = _pack_registry()
        problems = registry.validate(strict=args.strict)
        print(f"🔍 校验内置领域包（{len(registry)} 个，strict={args.strict}）")
    if problems:
        print("❌ 发现问题:")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_ERROR
    print("✅ 包校验通过")
    return EXIT_OK


def _cmd_packs_manifest(args: argparse.Namespace) -> int:
    manifest = _pack_registry().manifest()
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📄 已导出清单: {path}")
        return EXIT_OK
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
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
def _pack_registry() -> Any:
    """内置领域包注册表（供 packs 子命令使用）。"""
    from .core.pack import builtin_pack_registry

    return builtin_pack_registry(reload=True)


def _registry_with_extra_packs(args: argparse.Namespace, *, reload: bool = True) -> Any:
    """内置原子库 + 命令行 ``--packs`` 指定的声明式领域包。"""
    from .core.pack import load_pack_dir, load_pack_file

    registry = default_registry(reload=reload)
    for raw in getattr(args, "packs", []) or []:
        path = Path(raw)
        packs = load_pack_dir(path) if path.is_dir() else [load_pack_file(path)]
        for pack in packs:
            registry.register_pack(pack, replace=True)
    return registry


def _build_axe(args: argparse.Namespace, *, enable_llm: bool = True) -> PanguAxe:
    granted = getattr(args, "allow_capabilities", None)
    settings = ExecutorSettings(
        heal=not getattr(args, "no_heal", False),
        stop_on_first_failure=bool(getattr(args, "fail_fast", False)),
        capabilities=tuple(item.strip() for item in granted.split(",") if item.strip()) if granted else None,
    )
    if getattr(args, "retry", None):
        from .core.molecule import RetryPolicy

        settings.default_retry = RetryPolicy(
            attempts=max(1, int(args.retry)), delay_ms=200, backoff=2.0,
            on=("transient", "timeout", "network"),
        )
    workspace = getattr(args, "workspace", ".panguverse")
    return PanguAxe(
        registry=_registry_with_extra_packs(args),
        workspace=workspace,
        settings=settings,
        enable_llm=enable_llm,
    )


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
