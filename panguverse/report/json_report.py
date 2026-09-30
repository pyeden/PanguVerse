"""JSON 报告：机器可读的完整运行记录（CI 归档、二次分析、diff 基线）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_report(run: Any, *, mask: bool = True) -> dict[str, Any]:
    """把 RunResult 转成可序列化的字典（默认对密钥打码）。"""
    data = run.to_dict(mask=mask)
    data["generator"] = "panguverse"
    data["report_version"] = 1
    data["conclusion"] = conclusion(run)
    return data


def conclusion(run: Any) -> dict[str, Any]:
    summary = run.summary
    if summary["success"]:
        verdict = "通过"
    elif summary["errored"]:
        verdict = "存在执行错误"
    else:
        verdict = "存在测试失败"
    return {
        "verdict": verdict,
        "success": summary["success"],
        "exit_code": run.exit_code,
        "healed_cases": summary["healed_cases"],
        "failed_cases": [
            {"case_id": case.case_id, "name": case.name, "status": case.status}
            for case in run.cases
            if case.status in ("failed", "error")
        ],
    }


def write_json(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
