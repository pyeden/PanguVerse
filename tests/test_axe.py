"""编排层测试：规划器、记忆/沉淀、进化、盘古斧闭环编排。"""

from __future__ import annotations

import copy
import json

import pytest

from panguverse.axe.axe import PanguAxe, apply_lessons_to_plan
from panguverse.axe.healer import Healer
from panguverse.axe.llm import LLMError
from panguverse.axe.memory import LessonStore, SealedStore, Workspace
from panguverse.axe.plan import PlanRequest, TestPlan
from panguverse.axe.planner import HybridPlanner, LLMPlanner, RuleBasedPlanner, SpecPlanner
from panguverse.core.errors import PlanError
from panguverse.core.molecule import Molecule
from panguverse.engine.executor import ExecutorSettings
from panguverse.spec.loader import load_suite_text


class FakeLLM:
    """可控的假 LLM：既能返回合法计划，也能模拟失败。"""

    available = True
    model = "fake-model"

    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    def complete_json(self, system, user):
        self.calls.append(user)
        if self.error:
            raise self.error
        return copy.deepcopy(self.payload)

    def complete(self, system, user):
        self.calls.append(user)
        return "建议：先创建资源再断言"

    def status(self):
        return {"available": True, "model": self.model}


VALID_PLAN = {
    "cases": [
        {
            "id": "llm_health",
            "name": "AI 生成的健康检查",
            "tags": ["smoke"],
            "steps": [
                {"id": "health", "use": "http_request",
                 "with": {"method": "GET", "url": "${env.BASE_URL}/api/health", "expect_status": 200}},
                {"id": "assert_ok", "use": "assert_json", "with": {"path": "status", "equals": "ok"}},
            ],
        }
    ]
}


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #
class TestRulePlanner:
    def test_keyword_matching(self, registry):
        planner = RuleBasedPlanner(registry)
        plan = planner.plan(PlanRequest(intent="检查订单支付闭环和健康状态"))
        ids = [case.case_id for case in plan.cases]
        assert "order_flow" in ids and "health_check" in ids
        assert plan.source == "rules" and plan.meta["llm_used"] is False

    def test_empty_intent_uses_defaults(self, registry):
        plan = RuleBasedPlanner(registry).plan(PlanRequest(intent=""))
        assert [case.case_id for case in plan.cases] == ["health_check", "login_flow", "order_flow"]
        assert plan.notes

    def test_unknown_intent_falls_back_with_note(self, registry):
        plan = RuleBasedPlanner(registry).plan(PlanRequest(intent="给我泡一杯咖啡"))
        assert plan.cases
        assert any("关键词" in note for note in plan.notes)

    def test_max_cases_limit(self, registry):
        plan = RuleBasedPlanner(registry, max_cases=1).plan(PlanRequest(intent="健康 登录 订单 用户 401 重试"))
        assert len(plan.cases) == 1

    def test_templates_link_molecules(self, registry):
        plan = RuleBasedPlanner(registry).plan(PlanRequest(intent="登录下单支付"))
        for case in plan.cases:
            assert case.steps and case.source == "rules"


class TestPlanValidation:
    def test_detects_unknown_atom_and_missing_input(self, registry):
        plan = TestPlan(
            cases=[
                Molecule.from_spec({"id": "c1", "steps": [{"id": "s", "use": "nope"}]}),
                Molecule.from_spec({"id": "c2", "steps": [{"id": "s", "use": "assert_status"}]}),
            ]
        )
        problems = plan.validate(registry)
        assert any("未知原子" in p for p in problems)
        assert any("缺少输入" in p for p in problems)

    def test_empty_plan_invalid(self, registry):
        assert TestPlan().validate(registry) == ["计划中没有任何用例"]


class TestSpecPlanner:
    def test_plans_from_suite(self, registry):
        suite = load_suite_text(
            """
name: s
env: {BASE_URL: http://x}
cases:
  - id: c1
    name: 用例
    steps: [{id: s, use: log, with: {message: hi}}]
"""
        )
        plan = SpecPlanner().plan(PlanRequest(suite=suite, env={"EXTRA": "1"}))
        assert plan.source == "spec" and plan.cases[0].case_id == "c1"
        assert plan.env == {"BASE_URL": "http://x", "EXTRA": "1"}

    def test_requires_suite(self):
        with pytest.raises(PlanError):
            SpecPlanner().plan(PlanRequest())


class TestLLMPlanner:
    def test_valid_plan_used(self, registry):
        client = FakeLLM(VALID_PLAN)
        plan = LLMPlanner(registry, client).plan(PlanRequest(intent="健康检查", env={"BASE_URL": "x"}))
        assert plan.source == "llm" and plan.planner == "llm"
        assert plan.meta["llm_used"] is True
        assert plan.cases[0].case_id == "llm_health"

    def test_invalid_atom_degrades_to_rules(self, registry):
        bad = {"cases": [{"id": "bad", "name": "坏计划",
                          "steps": [{"id": "s", "use": "invented_atom", "with": {}}]}]}
        fallback = RuleBasedPlanner(registry)
        plan = LLMPlanner(registry, FakeLLM(bad), fallback=fallback).plan(
            PlanRequest(intent="健康检查", env={"BASE_URL": "x"})
        )
        assert plan.source == "rules"
        assert any("降级" in w for w in plan.warnings)
        assert plan.meta["llm_used"] is False

    def test_llm_error_degrades(self, registry):
        plan = LLMPlanner(registry, FakeLLM(error=LLMError("boom")), fallback=RuleBasedPlanner(registry)).plan(
            PlanRequest(intent="登录", env={"BASE_URL": "x"})
        )
        assert plan.source == "rules" and "boom" in plan.meta["llm_error"]

    def test_unavailable_client_degrades(self, registry):
        class Offline:
            available = False
            model = "-"
            def complete_json(self, *a, **k):  # pragma: no cover - 不应被调用
                raise AssertionError("不应调用")
        plan = LLMPlanner(registry, Offline(), fallback=RuleBasedPlanner(registry)).plan(PlanRequest(intent="健康"))
        assert plan.source == "rules"

    def test_no_fallback_raises(self, registry):
        with pytest.raises(PlanError):
            LLMPlanner(registry, FakeLLM(error=LLMError("x"))).plan(PlanRequest(intent="健康"))


class TestHybridPlanner:
    def test_prefers_spec_then_llm_then_rules(self, registry):
        suite = load_suite_text(
            """
name: s
cases:
  - id: spec_case
    steps: [{id: s, use: log, with: {message: hi}}]
"""
        )
        hybrid = HybridPlanner(registry, client=FakeLLM(VALID_PLAN))
        assert hybrid.plan(PlanRequest(suite=suite)).source == "spec"
        assert hybrid.plan(PlanRequest(intent="健康检查", env={"BASE_URL": "x"})).source == "llm"

    def test_without_client_uses_rules_with_note(self, registry):
        plan = HybridPlanner(registry, client=None).plan(PlanRequest(intent="健康检查"))
        assert plan.source == "rules"
        assert any("未检测到 LLM 密钥" in note for note in plan.notes)

    def test_allow_llm_false(self, registry):
        plan = HybridPlanner(registry, client=FakeLLM(VALID_PLAN)).plan(
            PlanRequest(intent="健康检查", allow_llm=False)
        )
        assert plan.source == "rules"

    def test_status_reports_llm(self, registry):
        status = HybridPlanner(registry, client=FakeLLM(VALID_PLAN)).status()
        assert status["planner"] == "hybrid" and status["llm"]["available"] is True


# --------------------------------------------------------------------------- #
# 记忆 / 沉淀 / 进化
# --------------------------------------------------------------------------- #
class TestMemory:
    def test_sealed_store_versions_and_listing(self, tmp_path):
        store = SealedStore(Workspace(tmp_path))
        molecule = Molecule.from_spec({"id": "c1", "steps": [{"id": "s", "use": "log", "with": {"message": "x"}}]},
                                      source="spec")
        first = store.save(molecule, tags=["regression"], source_run="run1")
        second = store.save(molecule, tags=["smoke"], source_run="run2")
        assert (first.version, second.version) == (1, 2)
        assert set(second.tags) == {"regression", "smoke"}
        assert [r.name for r in store.list()] == ["c1"]
        assert store.list(tag="smoke")[0].name == "c1"
        molecules = store.to_molecules(tag="regression")
        assert molecules[0].case_id == "c1" and molecules[0].source == "memory"
        store.mark_run(["c1", "nope"])
        assert store.get("c1").run_count == 1

    def test_lesson_store_record_and_recall(self, tmp_path):
        store = LessonStore(Workspace(tmp_path))
        store.record(kind="not_found", atom="http_request", target="/api/users/1",
                     message="404", suggestion="先创建资源")
        store.record(kind="server_error", atom="http_request", target="/api/flaky", message="500",
                     fix={"retry": {"attempts": 3}})
        assert store.count() == 2
        # 精确命中「原子 + 接口」
        recalled = store.recall(kind="server_error", atom="http_request", target="/api/flaky")
        assert recalled.fix["retry"]["attempts"] == 3
        # 接口不同 → 不套用别人的经验
        assert store.recall(kind="server_error", atom="http_request", target="/api/orders") is None
        # 未指定接口 → 退回同原子的经验
        assert store.recall(kind="server_error", atom="http_request") is not None
        # 不指定原子 → 按失败类型取最新
        assert store.recall(kind="not_found").target == "/api/users/1"
        assert store.summary() == {"not_found": 1, "server_error": 1}
        # 落盘可跨进程复用
        assert len(LessonStore(Workspace(tmp_path)).all()) == 2

    def test_evolution_seals_and_learns(self, tmp_path, registry, env):
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        suite = load_suite_text(
            """
name: s
cases:
  - id: ok_case
    steps:
      - id: health
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/health", expect_status: 200}
  - id: bad_case
    secrets: [token]
    steps:
      - id: login
        use: http_request
        tags: [auth]
        with: {method: POST, url: "${env.BASE_URL}/api/login",
               json: {username: demo, password: demo123}}
        capture: {token: body.token}
      - id: miss
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/orders/nope",
               headers: {Authorization: "Bearer ${token}"}, expect_status: 200}
"""
        )
        plan = axe.plan(PlanRequest(suite=suite, env=env))
        run = axe.run(plan, env=env)
        summary = axe.evolution.evolve(run)
        assert summary["sealed_count"] == 1 and summary["sealed"][0]["name"] == "ok_case"
        assert summary["lesson_count"] == 1
        lesson = axe.lessons.all()[-1]
        assert lesson.case_id == "bad_case" and lesson.kind == "not_found"
        assert "资源" in lesson.suggestion

    def test_evolution_learns_retry_success(self, tmp_path, env):
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        suite = load_suite_text(
            """
name: s
cases:
  - id: flaky_case
    steps:
      - id: flaky
        use: http_request
        retry: {attempts: 5, delay_ms: 5, on: [server_error]}
        with: {method: GET, url: "${env.BASE_URL}/api/flaky", expect_status: 200}
"""
        )
        run = axe.run(axe.plan(PlanRequest(suite=suite, env=env)), env=env)
        summary = axe.evolution.evolve(run)
        retry_lessons = [item for item in summary["lessons"] if item["fix"].get("retry")]
        assert retry_lessons and retry_lessons[0]["kind"] == "server_error"

    def test_apply_lessons_to_plan(self, tmp_path, registry):
        workspace = Workspace(tmp_path)
        lessons = LessonStore(workspace)
        lessons.record(kind="server_error", atom="http_request", target="/",
                       fix={"retry": {"attempts": 4, "delay_ms": 10, "on": ["server_error"]}})
        plan = TestPlan(
            cases=[Molecule.from_spec({"id": "c", "steps": [
                {"id": "req", "use": "http_request", "with": {"url": "http://x"}}]})]
        )
        applied = apply_lessons_to_plan(plan, lessons)
        assert applied and plan.cases[0].steps[0].retry.attempts == 4
        assert plan.meta["lessons_applied"] == applied
        # 接口不同 → 不套用别人的经验
        other = TestPlan(cases=[Molecule.from_spec({"id": "c", "steps": [
            {"id": "req", "use": "http_request", "with": {"url": "http://x/api/other"}}]})])
        assert apply_lessons_to_plan(other, lessons) == []
        assert other.cases[0].steps[0].retry is None
        # 已有 retry 的步骤不会被覆盖
        plan2 = TestPlan(cases=[Molecule.from_spec({"id": "c", "steps": [
            {"id": "req", "use": "http_request", "with": {"url": "http://x"},
             "retry": {"attempts": 2}}]})])
        assert apply_lessons_to_plan(plan2, lessons) == []
        assert plan2.cases[0].steps[0].retry.attempts == 2

    def test_workspace_reports_are_isolated(self, tmp_path):
        workspace = Workspace(tmp_path / ".panguverse").ensure()
        assert workspace.registry_path.parent == workspace.root
        assert workspace.run_dir("abc").exists()
        path = workspace.write_json("runs/abc/x.json", {"a": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}


# --------------------------------------------------------------------------- #
# PanguAxe
# --------------------------------------------------------------------------- #
class TestPanguAxe:
    def test_loop_full_cycle(self, tmp_path, env):
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        suite = load_suite_text(
            """
name: demo
cases:
  - id: health
    tags: [smoke]
    steps:
      - id: health
        use: http_request
        with: {method: GET, url: "${env.BASE_URL}/api/health"}
        assert:
          status: 200
          json:
            - path: status
              equals: ok
"""
        )
        result = axe.loop(PlanRequest(suite=suite, env=env), out_dir=tmp_path / "out", regression=True)
        assert result.exit_code == 0
        assert result.summary["passed"] == 1
        assert result.evolve["sealed_count"] == 1
        assert result.regression is not None and result.regression.summary["passed"] == 1
        names = {phase["name"] for phase in result.phases}
        assert {"plan", "execute", "heal", "report", "evolve", "regression"} <= names
        assert (tmp_path / "out" / "report.json").exists()
        assert (tmp_path / "out" / "report.html").exists()
        assert (tmp_path / "out" / "junit.xml").exists()
        assert (tmp_path / "out" / "plan.json").exists()
        assert "闭环完成" in result.text()

    def test_loop_validate_failure_raises(self, tmp_path, env):
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        plan = TestPlan(cases=[Molecule.from_spec({"id": "c", "steps": [{"id": "s", "use": "nope"}]})])
        with pytest.raises(PlanError):
            axe.run(plan, env=env)

    def test_compose_and_seal(self, tmp_path, env, registry):
        axe = PanguAxe(workspace=tmp_path / "ws", registry=registry, enable_llm=False)
        molecule = axe.compose(
            "登录并检查",
            steps=[
                {"id": "login", "use": "http_request", "tags": ["auth"],
                 "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                          "json": {"username": "demo", "password": "demo123"}, "expect_status": 200},
                 "capture": {"token": "body.token"}},
                {"id": "me", "use": "http_request",
                 "with": {"url": "${env.BASE_URL}/api/me",
                          "headers": {"Authorization": "Bearer ${token}"}, "expect_status": 200}},
            ],
            tags=["composed"],
        )
        assert registry.has("登录并检查")
        assert molecule.owner is axe
        result = axe.executor.run_case(molecule, env=env)
        assert result.status == "passed"

    def test_compose_atoms_shortcut(self, tmp_path, registry, env):
        axe = PanguAxe(workspace=tmp_path / "ws", registry=registry, enable_llm=False)
        log_atom = registry.get("log")
        molecule = axe.compose("只记日志", atoms=[log_atom])
        assert [step.use for step in molecule.steps] == ["log"]
        result = axe.executor.run_case(molecule, env=env)
        # log 需要 message，未提供 → 计划错误（用于说明 atoms= 只是便捷写法）
        assert result.status == "error"

    def test_regression_plan_from_memory(self, tmp_path, env):
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        plan = axe.regression_plan(env=env)
        assert plan.cases == [] and plan.source == "memory"

    def test_status(self, tmp_path):
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        status = axe.status()
        assert status["atoms"] > 10
        assert status["planner"]["llm"]["available"] is False
        assert status["http_backend"] in {"requests", "urllib"}

    def test_custom_healer_and_settings(self, tmp_path, env, registry):
        healer = Healer()
        axe = PanguAxe(
            workspace=tmp_path / "ws",
            registry=registry,
            healer=healer,
            settings=ExecutorSettings(heal=True, heal_budget=1),
            enable_llm=False,
        )
        assert axe.executor.healer is healer
        suite = load_suite_text(
            """
name: s
cases:
  - id: c
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
        with: {url: "${env.BASE_URL}/api/me",
               headers: {Authorization: "Bearer ${token}"}, expect_status: 200}
"""
        )
        result = axe.loop(PlanRequest(suite=suite, env=env), out_dir=tmp_path / "out", evolve=False)
        assert result.exit_code == 0
        assert result.run.summary["healed_cases"] == 1
        assert healer.stats["reauth"] == 1
