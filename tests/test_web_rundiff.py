"""G4：运行对比（基线 diff）。"""

from __future__ import annotations

import json
from pathlib import Path

from panguverse.web import rundiff


def _write_run(root: Path, run_id: str, *, status: str, duration: float, value: int, started: float) -> None:
    report = {
        "run_id": run_id,
        "started_at": started,
        "summary": {"total": 1, "passed": 1 if status == "passed" else 0, "failed": 0 if status == "passed" else 1, "success": status == "passed", "duration_ms": duration},
        "meta": {"workflow_ref": "w-test@0"},
        "cases": [{
            "case_id": "c1",
            "variables": {"items": value},
            "steps": [{"step_id": "s1", "atom": "http_request", "status": status, "duration_ms": duration, "outputs": {}}],
        }],
        "artifacts": {"execution": {"report.json": "report.json"}, "task_outputs": {}},
    }
    target = root / "runs" / run_id
    target.mkdir(parents=True)
    (target / "report.json").write_text(json.dumps(report), encoding="utf-8")


def test_diff_detects_status_duration_and_variable_changes(tmp_path: Path) -> None:
    _write_run(tmp_path, "r-old", status="passed", duration=10.0, value=3, started=100.0)
    _write_run(tmp_path, "r-new", status="failed", duration=20.0, value=5, started=200.0)

    diff = rundiff.diff_runs(tmp_path, "r-new")
    assert diff["available"] is True
    assert diff["base_id"] == "r-old"
    changed = [item for item in diff["steps"] if item["step"] == "c1:s1"][0]
    assert changed["change"] == "status_changed"
    assert set(changed["changed"]) == {"status", "duration"}
    assert changed["duration_delta_ms"] == 10.0
    assert any(item["name"] == "items" for item in diff["variables"])
    assert any(item["change"] == "content_changed" for item in diff["artifacts"])


def test_diff_without_baseline_is_explicit(tmp_path: Path) -> None:
    _write_run(tmp_path, "r-only", status="passed", duration=1.0, value=1, started=100.0)
    diff = rundiff.diff_runs(tmp_path, "r-only")
    assert diff["available"] is False
    assert "没有可对比" in diff["reason"]
