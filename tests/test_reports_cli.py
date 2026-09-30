"""报告层与命令行测试。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from panguverse.axe.axe import PanguAxe
from panguverse.axe.plan import PlanRequest
from panguverse.cli import main as cli_main
from panguverse.report import build_report, html_report, junit, write_all
from panguverse.spec.loader import load_suite_text

SUITE_WITH_SECRET = """
name: 报告测试
env:
  BASE_URL: http://x
cases:
  - id: login_case
    name: 登录并检查
    tags: [smoke, auth]
    secrets: [token]
    steps:
      - id: login
        use: http_request
        tags: [auth]
        with: {method: POST, url: "${env.BASE_URL}/api/login",
               json: {username: demo, password: demo123}}
        capture: {token: body.token}
        assert:
          status: 200
      - id: me
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/me",
               headers: {Authorization: "Bearer ${token}"}}
        assert:
          status: 200
"""

SUITE_WITH_FAILURE = """
name: 失败测试
env:
  BASE_URL: http://x
cases:
  - id: failing_case
    name: 会失败的用例
    steps:
      - id: bad
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/orders/nope", expect_status: 200}
"""

TOKEN_ROTATION_SUITE = """
name: token 轮换
env:
  BASE_URL: http://x
cases:
  - id: rotated_token
    name: token 失效 → 自愈重新登录
    secrets: [token]
    steps:
      - id: login
        use: http_request
        tags: [auth]
        with: {method: POST, url: "${env.BASE_URL}/api/login",
               json: {username: demo, password: demo123}}
        capture: {token: body.token}
      - id: invalidate
        use: http_request
        with: {method: POST, url: "${env.BASE_URL}/api/debug/invalidate-tokens",
               headers: {Authorization: "Bearer ${token}"}, expect_status: 200}
      - id: me
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/me",
               headers: {Authorization: "Bearer ${token}"}, expect_status: 200}
"""


@pytest.fixture()
def run_with_secret(tmp_path, env):
    suite = load_suite_text(SUITE_WITH_SECRET)
    axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
    run = axe.run(axe.plan(PlanRequest(suite=suite, env=env)), env=env)
    return axe, run


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
class TestReports:
    def test_json_report_structure(self, run_with_secret):
        _axe, run = run_with_secret
        report = build_report(run)
        assert report["summary"]["total"] == 1
        assert report["generator"] == "panguverse"
        assert report["conclusion"]["verdict"] == "通过"
        assert report["conclusion"]["exit_code"] == 0
        assert report["cases"][0]["case_id"] == "login_case"
        assert report["plan"]["source"] == "spec"

    def test_secrets_are_masked_in_outputs(self, run_with_secret, tmp_path):
        _axe, run = run_with_secret
        raw_token = run.cases[0].meta["secret_values"]
        assert raw_token, "应当收集到密钥明文"
        secret = raw_token[0]
        artifacts = write_all(run, tmp_path / "out")
        blob = "".join(Path(path).read_text(encoding="utf-8") for path in artifacts.values())
        assert secret not in blob
        assert "Bearer ***" in blob or "***" in blob
        # 请求头里的 Authorization 也要打码
        assert not re.search(r"tok_[0-9a-f]{10,}", blob)

    def test_secrets_masked_across_token_rotation(self, tmp_path, env):
        """回归：自愈「重新认证」会产生**第二个** token，两个都必须从产物里消失。

        曾经的 bug：正常步骤路径漏了场景 `secrets:` 声明的检查，只有自愈路径登记密钥，
        导致第一次登录的 token 留在响应原文（`text`）里泄漏到报告。
        """
        suite = load_suite_text(TOKEN_ROTATION_SUITE)
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        run = axe.run(axe.plan(PlanRequest(suite=suite, env=env)), env=env)
        assert run.cases[0].status == "passed"
        assert run.cases[0].heal_events and run.cases[0].heal_events[0].action == "reauth"

        secrets = run.cases[0].meta["secret_values"]
        assert len(secrets) >= 2, "轮换前后的两个 token 都必须登记为密钥"

        artifacts = write_all(run, tmp_path / "out")
        blob = "".join(Path(path).read_text(encoding="utf-8") for path in artifacts.values())
        assert [s for s in secrets if s in blob] == []
        assert re.findall(r"tok_[0-9a-f]{16,}", blob) == []

    def test_html_report_contains_key_sections(self, run_with_secret, tmp_path):
        _axe, run = run_with_secret
        report = build_report(run)
        html = html_report.render(report)
        for marker in ("闭环阶段", "用例明细", "login_case", "登录并检查", "通过", "PanguVerse"):
            assert marker in html
        assert html.startswith("<!DOCTYPE html>")
        assert "<script src=" not in html  # 自包含，无外部资源
        path = html_report.write_html(report, tmp_path / "r.html")
        assert path.exists() and path.stat().st_size > 2000

    def test_html_escapes_untrusted_text(self, tmp_path):
        suite = load_suite_text(
            """
name: 转义
env: {BASE_URL: http://x}
cases:
  - id: esc
    name: "<script>alert(1)</script>"
    steps:
      - id: s
        use: http_request
        with: {method: GET, url: "http://127.0.0.1:1/x"}
"""
        )
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        run = axe.run(axe.plan(PlanRequest(suite=suite, env={"BASE_URL": "http://127.0.0.1:1"})),
                      env={"BASE_URL": "http://127.0.0.1:1"})
        html = html_report.render(build_report(run))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_junit_xml_for_ci(self, tmp_path, env):
        suite = load_suite_text(SUITE_WITH_SECRET)
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        run = axe.run(axe.plan(PlanRequest(suite=suite, env=env)), env=env)
        xml_text = junit.render(build_report(run))
        root = ET.fromstring(xml_text)
        assert root.tag == "testsuites"
        assert root.attrib["tests"] == "1" and root.attrib["failures"] == "0"
        case = root.find("./testsuite/testcase")
        assert case.attrib["name"] == "login_case"

    def test_junit_reports_failures_and_errors(self, tmp_path):
        suite = load_suite_text(SUITE_WITH_FAILURE)
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        run = axe.run(axe.plan(PlanRequest(suite=suite, env={})), env={"BASE_URL": "http://127.0.0.1:1"})
        root = ET.fromstring(junit.render(build_report(run)))
        assert root.attrib["errors"] == "1"  # 连不上 → error 而不是 failure
        assert root.find(".//error") is not None

    def test_write_all_updates_run_artifacts(self, run_with_secret, tmp_path):
        _axe, run = run_with_secret
        artifacts = write_all(run, tmp_path / "out", formats=["json", "html"])
        assert set(artifacts) == {"json", "html"}
        for path in artifacts.values():
            assert Path(path).exists()
        assert run.artifacts["json"] == artifacts["json"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class TestCli:
    def test_atoms_listing(self, capsys):
        assert cli_main(["atoms"]) == 0
        out = capsys.readouterr().out
        assert "http_request" in out and "assert_json" in out

    def test_atoms_json(self, capsys):
        assert cli_main(["atoms", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert any(item["name"] == "http_request" for item in data)

    def test_plan_from_intent_json(self, capsys, tmp_path):
        code = cli_main(["plan", "-i", "健康检查与登录", "--json", "--workspace", str(tmp_path / "ws")])
        assert code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["source"] == "rules" and data["case_count"] >= 1
        assert data["problems"] == []

    def test_plan_reports_problems(self, capsys, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            "name: bad\ncases:\n  - id: c\n    steps:\n      - id: s\n        use: nope_atom\n",
            encoding="utf-8",
        )
        code = cli_main(["plan", "-f", str(path), "--workspace", str(tmp_path / "ws")])
        assert code == 2
        assert "nope_atom" in capsys.readouterr().out

    def test_loop_with_scenario_and_regression(self, capsys, tmp_path, env):
        path = tmp_path / "s.yaml"
        path.write_text(SUITE_WITH_SECRET, encoding="utf-8")
        args = [
            "loop", "-f", str(path),
            "--env", f"BASE_URL={env['BASE_URL']}",
            "--workspace", str(tmp_path / "ws"),
            "--report-dir", str(tmp_path / "out"),
            "--regression",
            "--json",
        ]
        assert cli_main(args) == 0
        out = capsys.readouterr().out
        assert "闭环完成" in out and "回归复用" in out
        assert (tmp_path / "out" / "report.html").exists()
        assert (tmp_path / "out" / "regression" / "report.json").exists()
        summary = json.loads(out[out.index("{\n"):])
        assert summary["summary"]["success"] is True

    def test_loop_failure_exit_code(self, capsys, tmp_path, env):
        path = tmp_path / "f.yaml"
        path.write_text(SUITE_WITH_FAILURE, encoding="utf-8")
        code = cli_main([
            "loop", "-f", str(path),
            "--env", f"BASE_URL={env['BASE_URL']}",
            "--workspace", str(tmp_path / "ws"),
            "--report-dir", str(tmp_path / "out"),
        ])
        assert code == 1
        assert "❌" in capsys.readouterr().out

    def test_run_then_regression(self, capsys, tmp_path, env):
        path = tmp_path / "s.yaml"
        path.write_text(SUITE_WITH_SECRET, encoding="utf-8")
        workspace = str(tmp_path / "ws")
        assert cli_main([
            "run", str(path), "--env", f"BASE_URL={env['BASE_URL']}",
            "--workspace", workspace, "--report-dir", str(tmp_path / "out1"),
        ]) == 0
        capsys.readouterr()
        assert cli_main(["regression", "--workspace", workspace,
                         "--env", f"BASE_URL={env['BASE_URL']}",
                         "--report-dir", str(tmp_path / "out2")]) == 0
        out = capsys.readouterr().out
        assert "回归" in out or "✅" in out

    def test_regression_empty_is_ok(self, capsys, tmp_path):
        assert cli_main(["regression", "--workspace", str(tmp_path / "empty")]) == 0
        assert "回归库为空" in capsys.readouterr().out

    def test_status_command(self, capsys, tmp_path):
        assert cli_main(["status", "--workspace", str(tmp_path / "ws"), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["atoms"] > 10 and data["sealed"] == 0

    def test_report_regeneration(self, capsys, tmp_path, env):
        path = tmp_path / "s.yaml"
        path.write_text(SUITE_WITH_SECRET, encoding="utf-8")
        out_dir = tmp_path / "out"
        cli_main(["run", str(path), "--env", f"BASE_URL={env['BASE_URL']}",
                  "--workspace", str(tmp_path / "ws"), "--report-dir", str(out_dir),
                  "--format", "json"])
        capsys.readouterr()
        (out_dir / "report.html").unlink(missing_ok=True)
        assert cli_main(["report", str(out_dir)]) == 0
        assert (out_dir / "report.html").exists()
        assert "report.html" in capsys.readouterr().out

    def test_init_creates_workspace_and_sample(self, capsys, tmp_path):
        assert cli_main(["init", "--dir", str(tmp_path)]) == 0
        assert (tmp_path / ".panguverse").is_dir()
        sample = tmp_path / "scenarios" / "sample_health.yaml"
        assert sample.exists()
        assert "health" in sample.read_text(encoding="utf-8")

    def test_missing_target_is_error(self, capsys, tmp_path):
        assert cli_main(["plan", "--workspace", str(tmp_path / "ws")]) == 2
        assert "需要 --intent" in capsys.readouterr().err

    def test_bad_env_pair(self, capsys, tmp_path):
        code = cli_main(["loop", "-i", "健康检查", "--env", "NOPE", "--workspace", str(tmp_path / "ws")])
        assert code == 2
        assert "K=V" in capsys.readouterr().err
