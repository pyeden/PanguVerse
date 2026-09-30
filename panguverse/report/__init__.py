"""报告层：JSON（机器可读）、HTML（人看）、JUnit XML（CI 消费）。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import html_report, json_report, junit

DEFAULT_FORMATS = ("json", "html", "junit")
_FILENAMES = {"json": "report.json", "html": "report.html", "junit": "junit.xml"}


def write_all(
    run: Any,
    out_dir: str | Path,
    *,
    formats: Iterable[str] = DEFAULT_FORMATS,
    title: str = "PanguVerse API 自动化测试报告",
    suite_name: str = "PanguVerse",
) -> dict[str, str]:
    """写出全部报告，返回 ``{格式: 绝对路径}``，同时回填到 ``run.artifacts``。"""
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    report = json_report.build_report(run, mask=True)
    artifacts: dict[str, str] = {}
    for fmt in formats:
        fmt = fmt.lower()
        path = target_dir / _FILENAMES.get(fmt, f"report.{fmt}")
        if fmt == "json":
            json_report.write_json(report, path)
        elif fmt == "html":
            html_report.write_html(report, path, title=title)
        elif fmt in {"junit", "xml"}:
            junit.write_junit(report, path, suite_name=suite_name)
        else:
            raise ValueError(f"未知报告格式: {fmt}")
        artifacts[fmt] = str(path)
    try:
        run.artifacts.update(artifacts)
        # 报告里也要带上产物路径，方便互相引用
        json_report.write_json(json_report.build_report(run, mask=True), artifacts.get("json", target_dir / "report.json"))
    except Exception:  # noqa: BLE001 - 报告回填失败不影响主流程
        pass
    return artifacts


__all__ = [
    "DEFAULT_FORMATS",
    "build_report",
    "html_report",
    "json_report",
    "junit",
    "write_all",
]

build_report = json_report.build_report
