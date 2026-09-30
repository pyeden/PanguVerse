"""端到端闭环测试：这是本项目要交付的「业务闭环」的验收测试。

覆盖：
1. 意图 → 规划 → 执行 → 断言 → 报告 → 自愈 → 沉淀 → 回归复用 全链路；
2. 仓库自带的场景文件（scenarios/）能直接跑通；
3. 两条自愈路径（步骤级重试、自愈器重新认证）在真实运行中生效；
4. 失败 → 经验沉淀 → 下次计划自动补强（进化）；
5. 一键演示命令（panguverse demo e2e）可跑通并产出报告。
"""

from __future__ import annotations

import json
from pathlib import Path

from panguverse.axe.axe import PanguAxe
from panguverse.axe.plan import PlanRequest
from panguverse.cli import main as cli_main
from panguverse.demo.e2e import collect_suites, merge_suites
from panguverse.demo.mock_api import MockAPI
from panguverse.spec.loader import load_suite, load_suite_text

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = REPO_ROOT / "scenarios"


def _run(axe: PanguAxe, suite, env, **kwargs):
    return axe.loop(PlanRequest(suite=suite, env=env), **kwargs)


# --------------------------------------------------------------------------- #
# 1) 全链路闭环
# --------------------------------------------------------------------------- #
def test_closed_loop_end_to_end(tmp_path, env):
    axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
    suite = load_suite_text(
        """
name: 闭环验收
env:
  API_USERNAME: demo
  API_PASSWORD: demo123
cases:
  - id: order_flow_mini
    name: 登录 → 下单 → 支付 → 回查
    tags: [smoke, order]
    secrets: [token]
    steps:
      - id: login
        use: http_request
        tags: [auth]
        with:
          method: POST
          url: ${env.BASE_URL}/api/login
          json: {username: "${env.API_USERNAME}", password: "${env.API_PASSWORD}"}
        capture: {token: body.token}
        assert:
          status: 200
      - id: create
        use: http_request
        with:
          method: POST
          url: ${env.BASE_URL}/api/orders
          headers: {Authorization: "Bearer ${token}"}
          json: {sku: SKU-E2E, quantity: 2, unit_price: 5}
        capture: {order_id: body.data.id}
        assert:
          status: 201
          json:
            - path: data.status
              equals: CREATED
      - id: pay
        use: http_request
        with:
          method: POST
          url: ${env.BASE_URL}/api/orders/${order_id}/pay
          headers: {Authorization: "Bearer ${token}"}
        assert:
          status: 200
          json:
            - path: data.status
              equals: PAID
      - id: verify
        use: http_request
        with:
          method: GET
          url: ${env.BASE_URL}/api/orders/${order_id}
          headers: {Authorization: "Bearer ${token}"}
        assert:
          status: 200
          json:
            - path: data.status
              equals: PAID
"""
    )
    result = _run(axe, suite, env, out_dir=tmp_path / "out", regression=True)

    # 执行与断言
    assert result.exit_code == 0
    assert result.summary["passed"] == 1 and result.summary["http_requests"] == 4
    assert result.run.cases[0].status == "passed"

    # 报告产物
    for name in ("report.json", "report.html", "junit.xml", "plan.json"):
        assert (tmp_path / "out" / name).exists()
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["conclusion"]["success"] is True
    assert report["summary"]["steps"] == 11  # 4 个请求 + 7 个展开的断言步骤

    # 闭环阶段齐全
    phases = {phase["name"]: phase for phase in result.phases}
    assert {"plan", "execute", "heal", "report", "evolve", "regression"} <= set(phases)

    # 沉淀：跑通的用例进入回归库
    assert result.evolve["sealed_count"] == 1
    assert result.evolve["sealed"][0]["name"] == "order_flow_mini"
    assert axe.sealed.get("order_flow_mini").version == 1

    # 回归复用：不再规划，直接跑沉淀的用例
    assert result.regression is not None
    assert result.regression.summary["passed"] == 1
    assert result.regression.plan["source"] == "memory"
    assert (tmp_path / "out" / "regression" / "report.json").exists()

    # 密钥没有泄漏
    blob = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    secret = result.run.cases[0].meta["secret_values"][0]
    assert secret not in blob


# --------------------------------------------------------------------------- #
# 2) 仓库自带场景
# --------------------------------------------------------------------------- #
def test_shipped_scenarios_pass(tmp_path, env):
    suites = collect_suites(SCENARIOS)
    assert len(suites) >= 3
    suite = merge_suites(suites)
    assert len(suite.cases) == 6

    axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
    result = _run(axe, suite, env, out_dir=tmp_path / "out")
    assert result.summary["failed"] == 0 and result.summary["errored"] == 0
    assert result.summary["passed"] == 6
    assert result.exit_code == 0


def test_shipped_self_healing_scenarios(tmp_path, env):
    suite = load_suite(SCENARIOS / "self_healing.yaml")
    axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
    result = _run(axe, suite, env, out_dir=tmp_path / "out")

    cases = {case.case_id: case for case in result.run.cases}
    assert set(cases) == {"flaky_retry_heal", "auth_reauth_heal"}
    assert result.exit_code == 0

    # 自愈第一层：服务端 5xx 抖动 → 步骤级重试
    flaky = cases["flaky_retry_heal"]
    flaky_step = flaky.steps[0]
    assert flaky_step.status == "passed" and flaky_step.retries >= 1
    assert flaky_step.attempts == flaky_step.retries + 1

    # 自愈第二层：token 失效 → 自愈器重新认证后重试成功
    auth = cases["auth_reauth_heal"]
    event = auth.heal_events[0]
    assert event.kind == "auth" and event.action == "reauth" and event.success is True
    assert auth.status == "passed"
    assert "重新执行认证步骤 login" in event.reason

    # 自愈过程要出现在报告里（可审计）
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["healed_cases"] == 1
    assert any(step.get("retries") for case in report["cases"] for step in case["steps"])


# --------------------------------------------------------------------------- #
# 3) 失败 → 经验沉淀 → 进化
# --------------------------------------------------------------------------- #
def test_failure_creates_lesson_and_next_run_uses_it(tmp_path, env):
    """失败 → 沉淀「修复假设」→ 下次规划自动套用 → 通过：这就是进化闭环。"""
    from panguverse.engine.executor import ExecutorSettings

    axe = PanguAxe(
        workspace=tmp_path / "ws",
        enable_llm=False,
        settings=ExecutorSettings(heal=False),  # 关掉自愈，先制造一次真实失败
    )
    failing = load_suite_text(
        """
name: 失败与进化
cases:
  - id: retry_needed
    steps:
      - id: flaky
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/flaky", expect_status: 200}
"""
    )
    # 第一次：没有重试策略 → 5xx 抖动直接失败
    first = _run(axe, failing, env, out_dir=tmp_path / "out1")
    assert first.exit_code == 1
    assert first.run.cases[0].steps[0].error["actual"] == 500
    lesson = axe.lessons.all()[-1]
    assert lesson.kind == "server_error"
    assert lesson.fix["retry"], "瞬态故障应当沉淀出可验证的重试策略"
    assert "重试" in lesson.suggestion

    # 第二次：同样的场景，规划阶段自动补上经验里的重试策略 → 抖动被吃掉，通过
    second = _run(axe, failing, env, out_dir=tmp_path / "out2")
    assert any("历史经验" in note for note in second.plan.notes)
    assert second.plan.meta["lessons_applied"]
    assert second.exit_code == 0
    step = second.run.cases[0].steps[0]
    assert step.status == "passed" and step.retries >= 1

    # 经验库与回归库都在工作区里持久化
    assert axe.workspace.lessons_path.exists()
    assert axe.sealed.get("retry_needed") is not None


def test_lesson_applied_after_retry_success(tmp_path, env):
    """重试后成功的经验会沉淀，并自动补强后续没有重试策略的计划。"""
    axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
    learning = load_suite_text(
        """
name: 学习重试
cases:
  - id: learn
    steps:
      - id: flaky
        use: http_request
        retry: {attempts: 5, delay_ms: 5, on: [server_error]}
        with: {method: GET, url: "${env.BASE_URL}/api/flaky", expect_status: 200}
"""
    )
    result = _run(axe, learning, env, out_dir=tmp_path / "out")
    assert result.exit_code == 0
    lessons = [item for item in axe.lessons.all() if item.fix.get("retry")]
    assert lessons and lessons[0].atom == "http_request"

    # 新计划（同一个接口，未写 retry）在 plan 阶段被自动补强
    fresh = load_suite_text(
        """
name: 复用经验
cases:
  - id: needs_retry
    steps:
      - id: req
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/flaky", expect_status: 200}
"""
    )
    plan = axe.plan(PlanRequest(suite=fresh, env=env))
    step = plan.cases[0].steps[0]
    assert step.retry is not None and step.retry.attempts >= 2
    assert plan.meta["lessons_applied"]
    assert "/api/flaky" in plan.meta["lessons_applied"][0]

    # 另一个接口不会被套用别人的经验（经验按「原子 + 接口」精确定位）
    unrelated = load_suite_text(
        """
name: 不相关接口
cases:
  - id: other
    steps:
      - id: req
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/health", expect_status: 200}
"""
    )
    other_plan = axe.plan(PlanRequest(suite=unrelated, env=env))
    assert other_plan.cases[0].steps[0].retry is None
    assert "lessons_applied" not in other_plan.meta


# --------------------------------------------------------------------------- #
# 4) 一键演示命令
# --------------------------------------------------------------------------- #
def test_demo_e2e_command(tmp_path, capsys, monkeypatch):
    result = cli_main([
        "demo", "e2e",
        "--workspace", str(tmp_path / "ws"),
        "--report-dir", str(tmp_path / "out"),
        "--scenarios", str(SCENARIOS),
    ])
    assert result == 0
    out = capsys.readouterr().out
    assert "闭环完成" in out and "回归复用" in out
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["success"] is True
    assert report["summary"]["total"] == 6
    assert (tmp_path / "out" / "regression" / "report.html").exists()


def test_demo_e2e_intent_only_offline(tmp_path, capsys):
    """无 LLM 密钥、只有自然语言意图时，闭环仍要跑通（规则规划兜底）。"""
    axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
    with MockAPI(flaky_failures=2) as api:
        env = {"BASE_URL": api.base_url}
        result = axe.loop(
            PlanRequest(intent="检查服务健康状态并验证登录链路", env=env),
            out_dir=tmp_path / "out",
        )
        assert result.plan.source == "rules"
        assert result.plan.meta["llm_used"] is False
        assert any("未检测到 LLM 密钥" in note for note in result.plan.notes)
        assert result.exit_code == 0
        assert result.summary["passed"] >= 2


def test_llm_planner_path_with_fake_client(tmp_path):
    from tests.test_axe import VALID_PLAN, FakeLLM

    axe = PanguAxe(workspace=tmp_path / "ws", llm=FakeLLM(VALID_PLAN))
    with MockAPI() as api:
        result = axe.loop(
            PlanRequest(intent="AI 生成健康检查", env={"BASE_URL": api.base_url}),
            out_dir=tmp_path / "out",
        )
    assert result.plan.source == "llm" and result.plan.meta["llm_used"] is True
    assert result.exit_code == 0
    assert result.run.summary["passed"] == 1
