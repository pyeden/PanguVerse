#!/usr/bin/env python3
"""G6：同一意图多次拆解的稳定性度量。

用法：
  python scripts/plan_stability.py --intent "..." --runs 3 [--workspace ~/.panguverse-dev]

输出每次的节点数、步骤签名与规划来源；末尾给出是否完全一致。
需要 LLM 凭据（PANGUVERSE_API_KEY 等），无凭据时直接失败而不是伪造结果。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from panguverse.core.workspace_packs import workspace_registry  # noqa: E402
from panguverse.web import planner_gate, service, studio  # noqa: E402


def _signature(plan) -> str:
    steps = []
    for case in plan.cases:
        steps.append([getattr(step, "use", "") for step in case.steps])
    raw = json.dumps(steps, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intent", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workspace", default=os.environ.get("PANGUVERSE_WORKSPACE", str(Path.home() / ".panguverse-dev")))
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = parser.parse_args()

    root = Path(args.workspace).expanduser()
    registry = workspace_registry(root, service.registry())
    client = studio._llm_client()
    if not getattr(client, "available", False):
        print("LLM 不可用：请先配置 PANGUVERSE_API_KEY / OPENAI_API_KEY", file=sys.stderr)
        return 2

    results = []
    for index in range(max(1, args.runs)):
        bundle = planner_gate.plan_draft(
            registry,
            intent=args.intent,
            allow_llm=True,
            llm_client=client,
            env={},
        )
        plan = bundle.plan
        steps = sum(len(case.steps) for case in plan.cases)
        signature = _signature(plan)
        results.append({"run": index + 1, "steps": steps, "signature": signature, "source": plan.source, "planner": plan.planner})

    unique = sorted({item["signature"] for item in results})
    summary = {"runs": results, "identical": len(unique) == 1, "unique_signatures": len(unique), "step_counts": sorted({item["steps"] for item in results})}

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f"run {item['run']}: steps={item['steps']} signature={item['signature']} source={item['source']} planner={item['planner']}")
        print(f"identical={summary['identical']} unique_signatures={summary['unique_signatures']} step_counts={summary['step_counts']}")

    return 0 if summary["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
