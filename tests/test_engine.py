"""引擎层测试：HTTP 原子、断言/提取原子、执行器（变量/数据驱动/重试/自愈/组合）。"""

from __future__ import annotations

from typing import ClassVar

import pytest

from panguverse.atoms import assertions as A
from panguverse.atoms.schema import validate as validate_schema
from panguverse.axe.healer import Healer, find_auth_step
from panguverse.core.context import RunContext
from panguverse.core.contract import FailureClass
from panguverse.core.errors import (
    AssertionFailed,
    NetworkError,
    PlanError,
)
from panguverse.core.molecule import Molecule
from panguverse.engine.executor import (
    Executor,
    ExecutorSettings,
    classify_exception,
    classify_failure_class,
)
from panguverse.engine.result import ERROR, FAILED, PASSED, SKIPPED
from panguverse.http.client import HttpClient, HttpRequest
from panguverse.packs.api_testing import ApiTestingSemantics, classify_status


def run_case(registry, spec, env, **settings):
    executor = Executor(registry, settings=ExecutorSettings(**settings) if settings else None)
    return executor.run_case(Molecule.from_spec(spec), env=env)


def call(atom_obj, ctx, **kwargs):
    """调用被 ``@atom`` 装饰后的原子对象。"""
    return atom_obj.invoke(ctx, kwargs)


# --------------------------------------------------------------------------- #
# HTTP 原子
# --------------------------------------------------------------------------- #
class TestHttpAtom:
    def test_health_request_outputs(self, registry, env):
        result = run_case(
            registry,
            {
                "id": "h",
                "steps": [
                    {"id": "health", "use": "http_request",
                     "with": {"method": "GET", "url": "${env.BASE_URL}/api/health"}}
                ],
            },
            env,
        )
        assert result.status == PASSED
        out = result.steps[0].outputs
        assert out["status_code"] == 200
        assert out["body"]["status"] == "ok"
        assert out["ok"] is True and out["elapsed_ms"] >= 0
        assert result.steps[0].http[0]["response"]["status_code"] == 200

    def test_relative_url_and_base_url(self, registry, env):
        result = run_case(
            registry,
            {"id": "h", "steps": [{"id": "s", "use": "http_request", "with": {"url": "/api/health"}}]},
            env,
        )
        assert result.steps[0].outputs["status_code"] == 200

    def test_missing_base_url_is_error(self, registry):
        result = run_case(
            registry,
            {"id": "h", "steps": [{"id": "s", "use": "http_request", "with": {"url": "/api/health"}}]},
            {},
        )
        assert result.status == ERROR
        assert "BASE_URL" in result.steps[0].error["message"]

    def test_4xx_is_not_exception(self, registry, env):
        result = run_case(
            registry,
            {"id": "h", "steps": [{"id": "s", "use": "http_request",
                                   "with": {"url": "${env.BASE_URL}/api/users"}}]},
            env,
        )
        assert result.status == PASSED
        assert result.steps[0].outputs["status_code"] == 401

    def test_expect_status_mismatch(self, registry, env):
        result = run_case(
            registry,
            {"id": "h", "steps": [{"id": "s", "use": "http_request",
                                   "with": {"url": "${env.BASE_URL}/api/users", "expect_status": 200}}]},
            env,
        )
        assert result.status == FAILED
        error = result.steps[0].error
        assert error["kind"] == "assertion" and error["actual"] == 401 and error["expected"] == 200

    def test_connection_error_classified(self, registry):
        result = run_case(
            registry,
            {"id": "h", "steps": [{"id": "s", "use": "http_request",
                                   "with": {"url": "http://127.0.0.1:1/api"}}]},
            {},
        )
        assert result.status == ERROR
        assert result.steps[0].error["kind"] in {"network", "timeout"}

    def test_urllib_backend_works(self, base_url):
        """零依赖承诺：没有 requests 也能跑（这里强制走标准库后端）。"""
        client = HttpClient(backend="urllib", timeout=5)
        response = client.send(HttpRequest(method="GET", url=f"{base_url}/api/health"))
        assert response.status_code == 200 and response.json()["status"] == "ok"
        missing = client.send(HttpRequest(method="GET", url=f"{base_url}/api/users"))
        assert missing.status_code == 401
        client.close()


# --------------------------------------------------------------------------- #
# 断言原子
# --------------------------------------------------------------------------- #
class TestAssertions:
    def ctx(self):
        return RunContext(variables={"last_response": {"status_code": 201, "elapsed_ms": 12.5,
                                                      "headers": {"Content-Type": "application/json"},
                                                      "text": '{"data": {"id": "o1", "items": [1, 2], "ok": true}}',
                                                      "body": {"data": {"id": "o1", "items": [1, 2], "ok": True}}}})

    def test_status_matches_forms(self):
        assert A.status_matches(200, 200)
        assert A.status_matches("2xx", 204)
        assert A.status_matches([200, 201], 201)
        assert A.status_matches("200,201", 201)
        assert not A.status_matches(200, 404)

    def test_assert_status_and_json(self):
        ctx = self.ctx()
        assert call(A.assert_status, ctx, expected=201)["status_code"] == 201
        call(A.assert_status, ctx, expected="2xx")
        with pytest.raises(AssertionFailed) as excinfo:
            call(A.assert_status, ctx, expected=200)
        assert excinfo.value.expected == 200 and excinfo.value.actual == 201
        call(A.assert_json, ctx, path="data.id", equals="o1")
        call(A.assert_json, ctx, path="data.items", length=2, contains=1, type="array")
        call(A.assert_json, ctx, path="data.missing", exists=False)
        with pytest.raises(AssertionFailed):
            call(A.assert_json, ctx, path="data.missing")
        with pytest.raises(AssertionFailed) as excinfo:
            call(A.assert_json, ctx, path="data.id", equals="o2")
        assert excinfo.value.actual == "o1"

    def test_assert_json_root_and_headers(self):
        ctx = self.ctx()
        call(A.assert_json, ctx, path="status_code", equals=201, **{"in": "response"})
        call(A.assert_header, ctx, name="content-type", contains="json")
        with pytest.raises(AssertionFailed):
            call(A.assert_header, ctx, name="x-missing", exists=True)
        call(A.assert_latency, ctx, max_ms=100)
        with pytest.raises(AssertionFailed):
            call(A.assert_latency, ctx, max_ms=10)
        call(A.assert_body_contains, ctx, text='"id": "o1"')

    def test_assert_json_collects_multiple_failures(self):
        ctx = self.ctx()
        with pytest.raises(AssertionFailed) as excinfo:
            call(A.assert_json, ctx, path="data.id", equals="x", type="integer")
        detail = excinfo.value.detail
        assert len(detail["checks"]) == 2

    def test_assert_schema(self):
        ctx = self.ctx()
        call(A.assert_schema, ctx, schema={"type": "object", "required": ["data"],
                                           "properties": {"data": {"type": "object",
                                                                   "required": ["id", "items"]}}})
        with pytest.raises(AssertionFailed) as excinfo:
            call(A.assert_schema, ctx, schema={"type": "object", "required": ["nope"]})
        assert "nope" in str(excinfo.value) or "nope" in str(excinfo.value.detail)

    def test_schema_validator_subset(self):
        schema = {
            "type": "object",
            "required": ["a"],
            "properties": {
                "a": {"type": "integer", "minimum": 1},
                "b": {"type": "string", "maxLength": 3, "pattern": "^x"},
                "c": {"enum": ["p", "q"]},
                "d": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "additionalProperties": False,
        }
        assert validate_schema({"a": 2}, schema) == []
        errors = validate_schema({"a": 0, "b": "yyyy", "c": "z", "extra": 1}, schema)
        assert any("应 >= 1" in e for e in errors)
        assert any("长度应 <= 3" in e for e in errors)
        assert any("枚举" in e for e in errors)
        assert any("未声明的字段" in e for e in errors)
        assert validate_schema({"a": 1, "d": "s"}, schema) == []
        assert validate_schema({"a": 1, "d": 3.5}, schema)  # 3.5 不满足 anyOf(string|integer)

    def test_ref_resolution(self):
        schema = {"$ref": "#/$defs/t", "$defs": {"t": {"type": "object", "required": ["x"]}}}
        assert validate_schema({"x": 1}, schema) == []
        assert validate_schema({"y": 1}, schema)
        assert validate_schema({}, {"$ref": "#/nope"})


# --------------------------------------------------------------------------- #
# 提取原子
# --------------------------------------------------------------------------- #
class TestExtract:
    def test_json_get_and_save_as(self, registry, env):
        result = run_case(
            registry,
            {
                "id": "e",
                "secrets": ["token"],
                "steps": [
                    {"id": "login", "use": "http_request",
                     "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                              "json": {"username": "demo", "password": "demo123"}},
                     "capture": {"token": "body.token"}},
                    {"id": "token_value", "use": "json_get",
                     "with": {"path": "body.token", "source": "${login}", "in": "response", "save_as": "token2"}},
                    {"id": "header", "use": "header_get",
                     "with": {"name": "Content-Type", "source": "${login}", "save_as": "ctype"}},
                ],
            },
            env,
        )
        assert result.status == PASSED
        variables = result.variables["vars"]
        # 声明为密钥的变量在快照里被打码；未声明的同值变量仍是明文
        assert variables["token"] == "***"
        assert variables["token2"] == result.meta["secret_values"][0]
        assert "json" in variables["ctype"].lower()

    def test_missing_path_required_raises(self, registry, env):
        result = run_case(
            registry,
            {"id": "e", "steps": [
                {"id": "req", "use": "http_request", "with": {"url": "${env.BASE_URL}/api/health"}},
                {"id": "get", "use": "json_get", "with": {"path": "body.nope", "required": True}},
            ]},
            env,
        )
        assert result.status == ERROR
        assert "body.nope" in result.steps[1].error["message"]

    def test_regex_extract(self, registry, env):
        result = run_case(
            registry,
            {"id": "e", "steps": [
                {"id": "req", "use": "http_request", "with": {"url": "${env.BASE_URL}/api/health"}},
                {"id": "rx", "use": "regex_extract", "with": {"pattern": '"status"\\s*:\\s*"(\\w+)"', "save_as": "st"}},
            ]},
            env,
        )
        assert result.variables["vars"]["st"] == "ok"

    def test_set_var_and_templates(self, registry, env):
        result = run_case(
            registry,
            {"id": "e", "steps": [
                {"id": "skus", "use": "set_var", "with": {"name": "skus", "value": ["A", "B"]}},
                {"id": "count", "use": "set_var", "with": {"name": "n", "value": "${skus|len}"}},
            ]},
            env,
        )
        assert result.variables["vars"]["n"] == 2
        assert result.variables["vars"]["skus"] == ["A", "B"]


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class TestExecutor:
    def test_capture_chain_and_last_response(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "secrets": ["token"], "steps": [
                {"id": "login", "use": "http_request", "tags": ["auth"],
                 "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                          "json": {"username": "demo", "password": "demo123"}},
                 "capture": {"token": "body.token", "user": "body.user.name"}},
                {"id": "me", "use": "http_request",
                 "with": {"url": "${env.BASE_URL}/api/me", "headers": {"Authorization": "Bearer ${token}"},
                          "expect_status": 200}},
            ]},
            env,
        )
        assert result.status == PASSED
        assert result.variables["vars"]["user"] == "demo"
        assert result.variables["secrets"] == {"token": "***"}
        assert len(result.meta["secret_values"]) == 1

    def test_foreach_accumulates_and_merges_last(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "skus", "use": "set_var", "with": {"name": "skus", "value": ["A", "B", "C"]}},
                {"id": "each", "use": "json_get", "foreach": "${skus}", "as": "sku",
                 "with": {"path": "index", "source": "${loop}", "in": "response"},
                 "capture": {"seen": "value"}},
                {"id": "tag", "use": "set_var", "foreach": "${skus}", "as": "sku",
                 "with": {"name": "tag_${index}", "value": "${sku}"}},
            ]},
            env,
        )
        assert result.status == PASSED, result.steps[-1].error
        variables = result.variables["vars"]
        assert result.steps[1].iterations == 3
        assert variables["seen"] == [0, 1, 2]
        assert variables["tag_0"] == "A" and variables["tag_2"] == "C"

    def test_foreach_merges_last_outputs(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "secrets": ["token"], "steps": [
                {"id": "login", "use": "http_request", "tags": ["auth"],
                 "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                          "json": {"username": "demo", "password": "demo123"}},
                 "capture": {"token": "body.token"}},
                {"id": "skus", "use": "set_var", "with": {"name": "skus", "value": ["A", "B"]}},
                {"id": "create", "use": "http_request", "foreach": "${skus}", "as": "sku",
                 "with": {"method": "POST", "url": "${env.BASE_URL}/api/orders",
                          "headers": {"Authorization": "Bearer ${token}"},
                          "json": {"sku": "${sku}", "quantity": 1},
                          "expect_status": 201},
                 "capture": {"ids": "body.data.id"}},
            ]},
            env,
        )
        assert result.status == PASSED, result.steps[-1].error
        aggregated = result.variables["vars"]["create"]
        assert aggregated["count"] == 2
        assert aggregated["status_code"] == 201            # 最后一次迭代的响应直接可用
        assert aggregated["body"]["data"]["sku"] == "B"
        assert len(aggregated["items"]) == 2
        assert len(result.variables["vars"]["ids"]) == 2   # foreach 的 capture 累积成列表

    def test_when_skips_step(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "yes", "use": "log", "when": "${flag|default:true}", "with": {"message": "run"}},
                {"id": "no", "use": "log", "when": "false", "with": {"message": "skip"}},
            ]},
            env,
        )
        assert result.steps[0].status == PASSED
        assert result.steps[1].status == SKIPPED and "when" in result.steps[1].skipped_reason

    def test_failure_skips_remaining_steps(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "bad", "use": "assert_status", "with": {"expected": 200, "actual": 500}},
                {"id": "after", "use": "log", "with": {"message": "never"}},
            ]},
            env,
        )
        assert result.status == FAILED
        assert result.steps[1].status == SKIPPED

    def test_continue_on_failure(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "bad", "use": "assert_status", "continue_on_failure": True,
                 "with": {"expected": 200, "actual": 500}},
                {"id": "after", "use": "log", "with": {"message": "runs"}},
            ]},
            env,
        )
        assert result.status == FAILED and result.steps[1].status == PASSED

    def test_optional_step_does_not_abort(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "opt", "use": "assert_status", "optional": True,
                 "with": {"expected": 200, "actual": 500}},
                {"id": "after", "use": "log", "with": {"message": "runs"}},
            ]},
            env,
        )
        assert result.steps[0].status == FAILED and result.steps[1].status == PASSED

    def test_retry_heals_server_error(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "flaky", "use": "http_request",
                 "retry": {"attempts": 5, "delay_ms": 10, "on": ["server_error"]},
                 "with": {"url": "${env.BASE_URL}/api/flaky", "expect_status": 200}},
            ]},
            env,
        )
        assert result.status == PASSED
        assert result.steps[0].retries >= 1 and result.steps[0].attempts >= 2

    def test_retry_exhausted_reports_failure(self, registry, mock_api):
        mock_api.state.flaky_failures = 99
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "flaky", "use": "http_request",
                 "retry": {"attempts": 3, "delay_ms": 5, "on": ["server_error"]},
                 "with": {"url": "${env.BASE_URL}/api/flaky", "expect_status": 200}},
            ]},
            {"BASE_URL": mock_api.base_url},
        )
        assert result.status == FAILED
        assert result.steps[0].retries == 2  # 3 次尝试 = 2 次重试
        assert result.steps[0].error["actual"] == 500

    def test_unknown_atom_is_error(self, registry, env):
        result = run_case(registry, {"id": "c", "steps": [{"id": "x", "use": "nope_atom"}]}, env)
        assert result.status == ERROR and result.steps[0].error["kind"] == "atom_not_found"

    def test_missing_required_input(self, registry, env):
        result = run_case(registry, {"id": "c", "steps": [{"id": "s", "use": "assert_status"}]}, env)
        assert result.status == ERROR and "expected" in result.steps[0].error["message"]

    def test_capture_path_missing_is_error(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "req", "use": "http_request", "with": {"url": "${env.BASE_URL}/api/health"},
                 "capture": {"x": "body.nope"}},
            ]},
            env,
        )
        assert result.status == ERROR and "capture" in result.steps[0].error["message"]

    def test_seal_molecule_becomes_atom(self, registry, env):
        executor = Executor(registry)
        inner = Molecule.from_spec(
            {"id": "login_molecule", "name": "登录分子", "steps": [
                {"id": "login", "use": "http_request", "tags": ["auth"],
                 "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                          "json": {"username": "${env.API_USERNAME}", "password": "${env.API_PASSWORD}"},
                          "expect_status": 200},
                 "capture": {"token": "body.token", "who": "body.user.name"}},
            ]},
            source="composed",
        )
        inner.meta["outputs"] = {"token": "${token}", "who": "${who}"}
        atom = executor.seal_molecule(inner)
        assert atom.name == "login_molecule" and registry.get("login_molecule") is atom

        outer = Molecule.from_spec(
            {"id": "outer", "steps": [
                {"id": "auth", "use": "login_molecule"},
                {"id": "me", "use": "http_request",
                 "with": {"url": "${env.BASE_URL}/api/me",
                          "headers": {"Authorization": "Bearer ${auth.token}"},
                          "expect_status": 200}},
            ]}
        )
        result = executor.run_case(outer, env=env)
        assert result.status == PASSED, result.steps[0].error
        assert result.variables["vars"]["auth"]["who"] == "demo"

    def test_composite_molecule_failure_propagates(self, registry, env):
        executor = Executor(registry)
        inner = Molecule.from_spec(
            {"id": "bad_molecule", "steps": [
                {"id": "check", "use": "assert_status", "with": {"expected": 599, "actual": 200}}]},
            source="composed",
        )
        executor.seal_molecule(inner)
        outer = Molecule.from_spec({"id": "outer", "steps": [{"id": "call", "use": "bad_molecule"}]})
        result = executor.run_case(outer, env=env)
        assert result.status == FAILED

    def test_fail_and_sleep_atoms(self, registry, env):
        result = run_case(
            registry,
            {"id": "c", "steps": [
                {"id": "s", "use": "sleep", "with": {"seconds": 0}},
                {"id": "f", "use": "fail", "with": {"message": "不该走到这里"}},
            ]},
            env,
        )
        assert result.status == FAILED and "不该走到这里" in result.steps[1].error["message"]


# --------------------------------------------------------------------------- #
# 失败分类 & 自愈决策
# --------------------------------------------------------------------------- #
class TestClassification:
    def test_http_status_taxonomy_lives_in_the_api_pack(self):
        """HTTP 状态码语义属于领域包（内核不再知道 401/500 是什么意思）。"""
        assert classify_status(401) == (FailureClass.CREDENTIALS, "auth")
        assert classify_status(403) == (FailureClass.CREDENTIALS, "auth")
        assert classify_status(404) == (FailureClass.MISSING_RESOURCE, "not_found")
        assert classify_status(409) == (FailureClass.CONFLICT, "conflict")
        assert classify_status(429) == (FailureClass.TRANSIENT, "rate_limit")
        assert classify_status(500) == (FailureClass.TRANSIENT, "server_error")
        assert classify_status(400) == (FailureClass.INVALID_INPUT, "client_error")
        assert classify_status(200) == (FailureClass.EXPECTATION, "unexpected_status")

    def test_neutral_classification_in_engine(self):
        """内核只做中立分类：与领域无关。"""
        assert classify_exception(NetworkError("x")) == "network"
        assert classify_exception(AssertionFailed("x")) == "assertion"
        assert classify_exception(PlanError("x")) == "plan"
        assert classify_exception(ValueError("x")) == "atom_error"
        assert classify_failure_class(NetworkError("x")) is FailureClass.TRANSIENT
        assert classify_failure_class(PlanError("x")) is FailureClass.INVALID_INPUT
        assert classify_failure_class(AssertionFailed("x")) is FailureClass.EXPECTATION

    def test_api_semantics_maps_status_to_class(self):
        semantics = ApiTestingSemantics()
        assert semantics.failure_class(None, {"status_code": 503}, None) is FailureClass.TRANSIENT
        assert semantics.failure_kind(None, {"status_code": 503}, None) == "server_error"
        assert semantics.failure_class(None, {"status_code": 404}, None) is FailureClass.MISSING_RESOURCE
        assert semantics.failure_kind(None, {"status_code": 401}, None) == "auth"
        # 成功结果里藏着可重试的失败（仅 5xx / 429）
        assert semantics.result_status(None, {"status_code": 500})[1] is FailureClass.TRANSIENT
        assert semantics.result_status(None, {"status_code": 404}) is None
        assert semantics.result_status(None, {"status_code": 200}) is None


class TestHealer:
    def case_with_auth(self):
        return Molecule.from_spec(
            {"id": "c", "steps": [
                {"id": "login", "use": "http_request", "tags": ["auth"],
                 "with": {"url": "http://x/login"}},
                {"id": "me", "use": "http_request", "with": {"url": "http://x/me"}},
            ]}
        )

    def test_find_auth_step(self):
        case = self.case_with_auth()
        assert find_auth_step(case, before="me").id == "login"
        assert find_auth_step(case, before="login") is None

    def api_semantics(self, registry):
        """自愈器的决策完全由领域语义驱动，因此测试要显式把语义传进去。"""
        return ApiTestingSemantics(), registry.get("http_request")

    def test_reauth_decision(self, registry):
        semantics, atom = self.api_semantics(registry)
        healer = Healer()
        decision = healer.propose(
            case=self.case_with_auth(), step=self.case_with_auth().steps[1],
            exc=AssertionFailed("401", actual=401),
            semantics=semantics, atom=atom, resolved_inputs={"url": "http://x/me"},
        )
        assert decision.kind == "reauth" and decision.auth_step_id == "login"
        assert decision.retry_override.attempts == 1
        assert decision.failure_kind == "auth" and decision.failure_class == "credentials"

    def test_transient_retry_decision(self, registry):
        semantics, atom = self.api_semantics(registry)
        healer = Healer()
        decision = healer.propose(
            case=self.case_with_auth(), step=self.case_with_auth().steps[1],
            exc=NetworkError("boom"), semantics=semantics, atom=atom,
            resolved_inputs={"url": "http://x/me"},
        )
        assert decision.kind == "retry" and decision.retry_override.attempts == 2

    def test_non_idempotent_atom_is_not_auto_retried(self, registry):
        """POST（mutate）自动重试可能重复执行 → 只给建议，除非计划显式写 retry。"""
        semantics, atom = self.api_semantics(registry)
        case = Molecule.from_spec(
            {"id": "c", "steps": [
                {"id": "login", "use": "http_request", "tags": ["auth"], "with": {"url": "http://x/login"}},
                {"id": "pay", "use": "http_request", "with": {"url": "http://x/pay", "method": "POST"}},
            ]}
        )
        healer = Healer()
        decision = healer.propose(
            case=case, step=case.steps[1], exc=NetworkError("boom"),
            semantics=semantics, atom=atom, resolved_inputs={"url": "http://x/pay", "method": "POST"},
        )
        assert decision.kind == "none"
        assert "重复执行" in decision.suggestion

    def test_assertion_gives_advice_only(self, registry):
        semantics, atom = self.api_semantics(registry)
        healer = Healer()
        decision = healer.propose(
            case=self.case_with_auth(), step=self.case_with_auth().steps[1],
            exc=AssertionFailed("期望 200 实际 404", actual=404),
            semantics=semantics, atom=atom, resolved_inputs={"url": "http://x/me"},
        )
        assert decision.kind == "none"
        assert "资源" in decision.suggestion and decision.failure_kind == "not_found"

    def test_lesson_driven_patch_retry(self, registry):
        class FakeLesson:
            fix: ClassVar[dict] = {"retry": {"attempts": 3, "delay_ms": 1}}
            suggestion = "历史验证过"
            signature = "http_request:server_error"

            def describe(self):
                return "server_error@http_request 需重试"

        class FakeLessons:
            def recall(self, *, kind=None, failure_class=None, atom="", target=None):
                return FakeLesson()

        healer = Healer(lessons=FakeLessons())
        semantics, atom = self.api_semantics(registry)
        decision = healer.propose(
            case=self.case_with_auth(), step=self.case_with_auth().steps[1],
            exc=AssertionFailed("服务端错误 500", actual=500),
            semantics=semantics, atom=atom, resolved_inputs={"url": "http://x/me", "method": "GET"},
        )
        assert decision.kind in {"patch_retry", "retry"}
        assert decision.retry_override.attempts == 3
        assert healer.stats["patch_retry"] == 1

    def test_healer_used_by_executor_for_reauth(self, registry, env):
        executor = Executor(
            registry,
            settings=ExecutorSettings(heal=True, heal_budget=3),
            healer=Healer(),
        )
        result = executor.run_case(
            Molecule.from_spec(
                {"id": "c", "secrets": ["token"], "steps": [
                    {"id": "login", "use": "http_request", "tags": ["auth"],
                     "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                              "json": {"username": "demo", "password": "demo123"}},
                     "capture": {"token": "body.token"}},
                    {"id": "invalidate", "use": "http_request",
                     "with": {"method": "POST", "url": "${env.BASE_URL}/api/debug/invalidate-tokens",
                              "headers": {"Authorization": "Bearer ${token}"}, "expect_status": 200}},
                    {"id": "me", "use": "http_request",
                     "with": {"url": "${env.BASE_URL}/api/me",
                              "headers": {"Authorization": "Bearer ${token}"}, "expect_status": 200}},
                ]}
            ),
            env=env,
        )
        assert result.status == PASSED
        events = result.heal_events
        assert events and events[0].kind == "auth" and events[0].success is True
        assert events[0].action == "reauth"

    def test_executor_without_healer_fails(self, registry, env):
        executor = Executor(registry, settings=ExecutorSettings(heal=False))
        result = executor.run_case(
            Molecule.from_spec(
                {"id": "c", "steps": [
                    {"id": "login", "use": "http_request", "tags": ["auth"],
                     "with": {"method": "POST", "url": "${env.BASE_URL}/api/login",
                              "json": {"username": "demo", "password": "demo123"}},
                     "capture": {"token": "body.token"}},
                    {"id": "invalidate", "use": "http_request",
                     "with": {"method": "POST", "url": "${env.BASE_URL}/api/debug/invalidate-tokens",
                              "headers": {"Authorization": "Bearer ${token}"}, "expect_status": 200}},
                    {"id": "me", "use": "http_request",
                     "with": {"url": "${env.BASE_URL}/api/me",
                              "headers": {"Authorization": "Bearer ${token}"}, "expect_status": 200}},
                ]}
            ),
            env=env,
        )
        assert result.status == FAILED and not result.heal_events
