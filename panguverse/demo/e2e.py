"""一键端到端闭环演示：内置 mock API + 全部场景 + 报告 + 沉淀 + 回归复用。

```bash
panguverse demo e2e                 # 完整闭环（含回归复用）
panguverse demo e2e --keep-workspace
```
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..axe.axe import LoopResult, PanguAxe
from ..axe.memory import Workspace
from ..axe.plan import PlanRequest
from ..spec.loader import TestSuite, load_suite
from .mock_api import MockAPI

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIOS = REPO_ROOT / "scenarios"


def collect_suites(scenarios: str | Path | None = None) -> list[TestSuite]:
    """加载场景：可以是目录（全部 .yaml/.json）或单个文件。"""
    base = Path(scenarios) if scenarios else DEFAULT_SCENARIOS
    files = (
        sorted([*base.glob("*.yaml"), *base.glob("*.yml"), *base.glob("*.json")])
        if base.is_dir()
        else [base]
    )
    if not files:
        raise FileNotFoundError(f"没有找到任何场景文件: {base}")
    return [load_suite(path) for path in files]


def merge_suites(suites: Sequence[TestSuite], *, name: str = "PanguVerse 闭环演示") -> TestSuite:
    """把多个场景合并成一个（用例拼接、env 合并、defaults 合并）。"""
    cases: list[dict[str, Any]] = []
    env: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    notes: list[str] = []
    for suite in suites:
        cases.extend(suite.cases)
        env.update(suite.env)
        for key, value in suite.defaults.items():
            if isinstance(value, dict) and isinstance(defaults.get(key), dict):
                defaults[key] = {**defaults[key], **value}
            else:
                defaults[key] = value
        notes.append(f"来自 {suite.name}（{len(suite.cases)} 个用例）")
    return TestSuite(name=name, cases=cases, env=env, defaults=defaults, notes=notes)


def run_demo(
    *,
    scenarios: str | Path | None = None,
    workspace: str | Path | None = None,
    out_dir: str | Path | None = None,
    regression: bool = True,
    formats: Sequence[str] = ("json", "html", "junit"),
    flaky_failures: int = 2,
    env: dict[str, Any] | None = None,
    verbose: bool = True,
) -> LoopResult:
    """启动 mock API，跑完整闭环。返回 :class:`LoopResult`。"""
    suites = collect_suites(scenarios)
    suite = merge_suites(suites)
    if verbose:
        print(f"[demo] 已加载 {len(suites)} 个场景，共 {len(suite.cases)} 个用例")
        print("[demo] 启动内置 mock API 作为被测量…")
    with MockAPI(flaky_failures=flaky_failures) as api:
        base_url = api.base_url
        if verbose:
            print(f"[demo] 被测量地址: {base_url}（登录账户 demo/demo123）")
        merged_env = {"BASE_URL": base_url, **(env or {})}
        axes_workspace = Workspace(workspace or ".panguverse")
        axe = PanguAxe(workspace=axes_workspace)
        request = PlanRequest(suite=suite, env=merged_env)
        if verbose:
            print("[demo] 开始闭环：规划 → 校验 → 执行 → 自愈 → 报告 → 沉淀 → 回归复用\n")
        started = time.time()
        result = axe.loop(
            request,
            out_dir=out_dir,
            formats=formats,
            evolve=True,
            seal=True,
            regression=regression,
            title="PanguVerse API 自动化测试闭环报告",
        )
        if verbose:
            print(result.text())
            print(f"\n[demo] 总耗时 {(time.time() - started):.2f}s")
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 由 CLI 调用
    import argparse

    parser = argparse.ArgumentParser(description="PanguVerse 一键闭环演示")
    parser.add_argument("--scenarios", default=None, help="场景目录或文件（默认仓库 scenarios/）")
    parser.add_argument("--workspace", default=".panguverse", help="工作区目录（沉淀产物）")
    parser.add_argument("--out-dir", default=None, help="报告输出目录")
    parser.add_argument("--no-regression", action="store_true", help="不跑回归复用")
    parser.add_argument("--flaky-failures", type=int, default=2, help="mock API 前 N 次注入 500")
    args = parser.parse_args(argv)

    result = run_demo(
        scenarios=args.scenarios,
        workspace=args.workspace,
        out_dir=args.out_dir,
        regression=not args.no_regression,
        flaky_failures=args.flaky_failures,
    )
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
