"""JUnit XML 报告：让 CI（GitHub Actions / Jenkins / GitLab CI）直接消费测试结果。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr


def render(report: dict[str, Any], *, suite_name: str = "PanguVerse") -> str:
    cases = report.get("cases", [])
    plan = report.get("plan", {}) or {}
    source = str(plan.get("source", "unknown"))

    failures = sum(1 for case in cases if case.get("status") == "failed")
    errors = sum(1 for case in cases if case.get("status") == "error")
    skipped = sum(1 for case in cases if case.get("status") == "skipped")
    total_time = sum(float(case.get("duration_ms", 0)) for case in cases) / 1000.0

    lines: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        f'<testsuites name={quoteattr(suite_name)} tests="{len(cases)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}" time="{total_time:.3f}">'
    )
    lines.append(
        f'  <testsuite name={quoteattr(f"{suite_name}.{source}")} tests="{len(cases)}" '
        f'failures="{failures}" errors="{errors}" skipped="{skipped}" time="{total_time:.3f}" '
        f'timestamp={quoteattr(_iso(report.get("started_at")))}>'
    )
    for case in cases:
        lines.extend(_case_lines(case, source))
    lines.append("  </testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines) + "\n"


def _case_lines(case: dict[str, Any], source: str) -> list[str]:
    status = str(case.get("status", "passed"))
    duration = float(case.get("duration_ms", 0)) / 1000.0
    attrs = (
        f'classname={quoteattr(f"panguverse.{source}")} '
        f'name={quoteattr(str(case.get("case_id", "")))} '
        f'time="{duration:.3f}"'
    )
    out: list[str] = [f"    <testcase {attrs}>"]
    if status in ("failed", "error"):
        error = case.get("error") or {}
        message = str(error.get("message", "")) or status
        detail = _detail(case)
        tag = "failure" if status == "failed" else "error"
        out.append(
            f"      <{tag} message={quoteattr(message[:500])} "
            f"type={quoteattr(str(error.get('type', 'Failure')))}>{escape(detail)}</{tag}>"
        )
    if status == "skipped":
        out.append("      <skipped/>")
    if case.get("heal_events"):
        out.append(
            f"      <system-out>{escape('自愈事件: ' + str(len(case['heal_events'])) + ' 次')}</system-out>"
        )
    else:
        out.append(f"      <system-out>{escape(_steps_text(case))}</system-out>")
    out.append("    </testcase>")
    return out


def _detail(case: dict[str, Any]) -> str:
    lines = [_steps_text(case)]
    for step in case.get("steps", []):
        if step.get("status") in ("failed", "error"):
            error = step.get("error") or {}
            lines.append(
                f"[{step.get('step_id')}/{step.get('atom')}] "
                f"{error.get('type', '')}: {error.get('message', '')}"
            )
            if error.get("expected") is not None or error.get("actual") is not None:
                lines.append(f"  期望: {error.get('expected')!r}  实际: {error.get('actual')!r}")
    return "\n".join(lines)


def _steps_text(case: dict[str, Any]) -> str:
    rows = []
    for step in case.get("steps", []):
        rows.append(
            f"{step.get('step_id')}: {step.get('atom')} -> {step.get('status')} "
            f"({float(step.get('duration_ms', 0)):.0f}ms, retries={step.get('retries', 0)})"
        )
    return "\n".join(rows)


def _iso(value: Any) -> str:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def write_junit(report: dict[str, Any], path: str | Path, *, suite_name: str = "PanguVerse") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(report, suite_name=suite_name), encoding="utf-8")
    return target
