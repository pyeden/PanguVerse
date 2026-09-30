"""核心层测试：模板上下文、原子/原子库、YAML 子集解析、场景加载。"""

from __future__ import annotations

import pytest

from panguverse.core.atom import Atom, atom
from panguverse.core.context import RunContext
from panguverse.core.errors import AtomNotFoundError, PlanError, SpecError, TemplateError
from panguverse.core.molecule import Molecule, RetryPolicy, Step
from panguverse.core.registry import AtomRegistry
from panguverse.spec import miniyaml
from panguverse.spec.loader import load_suite_dict, load_suite_text


# --------------------------------------------------------------------------- #
# RunContext
# --------------------------------------------------------------------------- #
class TestRunContext:
    def test_full_template_keeps_type(self):
        ctx = RunContext(env={"PORT": "8899"}, variables={"n": 3, "items": ["a"]})
        assert ctx.resolve("${env.PORT}") == "8899"
        assert ctx.resolve("${n}") == 3
        assert ctx.resolve("${items}") == ["a"]

    def test_string_interpolation(self):
        ctx = RunContext(env={"BASE_URL": "http://x"}, variables={"token": "abc"})
        assert ctx.resolve("Bearer ${token}") == "Bearer abc"
        assert ctx.resolve("${env.BASE_URL}/api/login") == "http://x/api/login"

    def test_nested_and_index_paths(self):
        ctx = RunContext(variables={"login": {"body": {"data": {"items": [{"id": "o1"}]}}}})
        assert ctx.resolve("${login.body.data.items[0].id}") == "o1"
        assert ctx.resolve("${login.body.data.items.0.id}") == "o1"

    def test_filters(self):
        ctx = RunContext(env={"MS": "2000"}, variables={"s": " AbC ", "obj": '{"a": 1}'})
        assert ctx.resolve("${env.MS|int}") == 2000
        assert ctx.resolve("${s|strip|lower}") == "abc"
        assert ctx.resolve("${obj|json}") == {"a": 1}
        assert ctx.resolve("${missing|default:fallback}") == "fallback"
        assert ctx.resolve("${env.MS|default:1|int}") == 2000
        assert ctx.resolve("${s|len}") == 5

    def test_missing_variable_raises(self):
        ctx = RunContext()
        with pytest.raises(TemplateError):
            ctx.resolve("${nope}")

    def test_unknown_filter_raises(self):
        with pytest.raises(TemplateError):
            RunContext(variables={"a": 1}).resolve("${a|nope}")

    def test_recursive_resolution(self):
        ctx = RunContext(variables={"token": "t"})
        resolved = ctx.resolve({"a": ["${token}", 1], "b": {"c": "${token}"}})
        assert resolved == {"a": ["t", 1], "b": {"c": "t"}}

    def test_set_creates_paths(self):
        ctx = RunContext()
        ctx.set("a.b[0]", "x")
        assert ctx.vars["a"]["b"][0] == "x"
        assert ctx.resolve("${a.b[0]}") == "x"

    def test_child_shares_variables(self):
        ctx = RunContext(variables={"a": 1})
        child = ctx.child(item="i")
        child.set("written_by_child", True)
        assert ctx.vars["written_by_child"] is True  # 子作用域写入对外可见
        assert child.resolve("${a}") == 1
        assert child.resolve("${item}") == "i"

    def test_isolated_does_not_leak(self):
        ctx = RunContext(variables={"a": 1})
        scope = ctx.isolated(b=2)
        scope.set("c", 3)
        assert "c" not in ctx.vars and "b" not in ctx.vars

    def test_secrets_masking(self):
        ctx = RunContext()
        ctx.add_secret("token", "tok_abcdef123456")
        ctx.add_secret("token", "tok_second1234567")
        values = ctx.secret_values()
        assert "tok_abcdef123456" in values and "tok_second1234567" in values
        snapshot = ctx.snapshot()
        assert snapshot["secrets"] == {"token": "***", "token#2": "***"}


# --------------------------------------------------------------------------- #
# Atom / AtomRegistry
# --------------------------------------------------------------------------- #
class TestAtom:
    def test_ctx_injection_and_outputs(self):
        @atom("echo", inputs={"value": "any"}, outputs={"value": "any"}, required=("value",))
        def echo(ctx, value):
            ctx.emit("echo", value=value)
            return {"value": value}

        ctx = RunContext()
        assert echo.invoke(ctx, {"value": 7}) == {"value": 7}
        assert echo.wants_ctx is True
        assert ctx.trace[-1]["event"] == "echo"

    def test_keyword_collision_alias(self):
        @atom("kw", inputs={"in": "string"}, required=())
        def kw(ctx, in_="body"):
            return {"got": in_}

        assert kw.invoke(RunContext(), {"in": "response"}) == {"got": "response"}

    def test_required_defaults_from_inputs(self):
        item = Atom(name="x", fn=lambda a: None, inputs={"a": "int", "b?": "int"})
        assert item.required == ("a",)
        explicit = Atom(name="y", fn=lambda: None, inputs={"a": "int"}, required=())
        assert explicit.required == ()

    def test_registry_alias_and_errors(self):
        reg = AtomRegistry()
        item = Atom(name="base", fn=lambda: {}, aliases=("b",))
        reg.register(item)
        assert reg.get("b") is item
        assert reg.has("b") and "base" in reg
        with pytest.raises(ValueError):
            reg.register(item)
        with pytest.raises(AtomNotFoundError):
            reg.get("nope")

    def test_builtin_catalog(self, registry):
        names = registry.names()
        for expected in ("http_request", "assert_status", "assert_json", "json_get", "set_var", "sleep"):
            assert expected in names
        catalog = registry.catalog()
        assert all("name" in entry and "inputs" in entry for entry in catalog)


# --------------------------------------------------------------------------- #
# Molecule / Step
# --------------------------------------------------------------------------- #
class TestMolecule:
    def test_from_spec_and_retry(self):
        molecule = Molecule.from_spec(
            {
                "id": "case1",
                "name": "用例",
                "steps": [
                    {"id": "a", "use": "set_var", "with": {"name": "x", "value": 1}},
                    {"id": "b", "use": "sleep", "with": {"seconds": 0}, "retry": {"attempts": 3}},
                ],
            }
        )
        assert molecule.case_id == "case1"
        assert molecule.steps[1].retry.attempts == 3
        # 默认重试判据是**中立类别**（跨领域通用）：transient 命中、expectation 不命中
        assert molecule.steps[1].retry.should_retry("whatever", "transient")
        assert molecule.steps[1].retry.should_retry("timeout")
        assert not molecule.steps[1].retry.should_retry("assertion", "expectation")
        # 领域标签需要显式写进 on 才命中（保持「谁声明谁负责」）
        explicit = RetryPolicy(attempts=3, on=("server_error",))
        assert explicit.should_retry("server_error")
        assert not explicit.should_retry("whatever", "transient")

    def test_duplicate_step_ids_rejected(self):
        with pytest.raises(PlanError):
            Molecule.from_spec(
                {"id": "c", "steps": [{"id": "s", "use": "log", "with": {"message": "1"}},
                                      {"id": "s", "use": "log", "with": {"message": "2"}}]}
            )

    def test_missing_steps_rejected(self):
        with pytest.raises(PlanError):
            Molecule.from_spec({"id": "c", "steps": []})

    def test_retry_policy_forms(self):
        assert RetryPolicy.from_spec(None) is None
        assert RetryPolicy.from_spec(True).attempts == 3
        assert RetryPolicy.from_spec(2).attempts == 2
        policy = RetryPolicy.from_spec({"attempts": 5, "delay_ms": 10, "on": ["timeout"]})
        assert (policy.attempts, policy.delay_ms, policy.on) == (5, 10, ("timeout",))
        assert policy.delay_for(3) == pytest.approx(0.04)  # backoff 默认 2.0 → 0.01 * 2**2

    def test_step_from_spec_keeps_extra(self):
        step = Step.from_spec({"id": "s", "use": "log", "with": {"message": "m"}, "custom": 1})
        assert step.extra == {"custom": 1}


# --------------------------------------------------------------------------- #
# mini YAML
# --------------------------------------------------------------------------- #
class TestMiniYaml:
    def test_nested_mapping_list_and_scalars(self):
        data = miniyaml.load(
            """
name: 场景  # 注释
enabled: true
count: 3
ratio: 0.5
empty: null
tags: [a, b]
nested:
  key:
    - id: one
      name: 第一
      tags: [x, y]
      child:
        deep: 1
    - id: two
      name: 第二
text: "带冒号: 的值"
"""
        )
        assert data["name"] == "场景"
        assert data["enabled"] is True and data["count"] == 3 and data["ratio"] == 0.5
        assert data["empty"] is None and data["tags"] == ["a", "b"]
        items = data["nested"]["key"]
        assert items[0]["id"] == "one" and items[0]["tags"] == ["x", "y"]
        assert items[0]["child"]["deep"] == 1
        assert items[1]["name"] == "第二"
        assert data["text"] == "带冒号: 的值"

    def test_flow_map_and_quotes(self):
        data = miniyaml.load("a: {x: 1, y: 'two', z: true}\nb: [1, 2, 3]\n")
        assert data["a"] == {"x": 1, "y": "two", "z": True}
        assert data["b"] == [1, 2, 3]

    def test_block_scalar(self):
        data = miniyaml.load("desc: |\n  第一行\n  第二行\nother: 1\n")
        assert "第一行" in data["desc"] and "第二行" in data["desc"]
        assert data["other"] == 1

    def test_templates_and_urls_not_split(self):
        data = miniyaml.load("url: ${env.BASE_URL}/api/login\nraw: http://x:8080/p?q=1\n")
        assert data["url"] == "${env.BASE_URL}/api/login"
        assert data["raw"] == "http://x:8080/p?q=1"

    def test_comment_inside_quotes_kept(self):
        data = miniyaml.load('a: "value # not comment"\n')
        assert data["a"] == "value # not comment"

    def test_unsupported_syntax_raises(self):
        with pytest.raises(SpecError):
            miniyaml.load("---\na: 1\n")
        with pytest.raises(SpecError):
            miniyaml.load("a: &anchor 1\n")

    def test_matches_pyyaml_when_available(self):
        yaml = pytest.importorskip("yaml")
        text = """
name: cross-check
env:
  BASE_URL: http://127.0.0.1:8899
  MS: 2000
defaults:
  headers:
    Content-Type: application/json
cases:
  - id: c1
    name: 用例一
    tags: [a, b]
    steps:
      - id: s1
        use: http_request
        with:
          method: POST
          url: ${env.BASE_URL}/api/login
          json:
            username: demo
            quantity: 3
        assert:
          status: 200
          json:
            - path: data.id
              equals: x
            - path: ok
              equals: true
"""
        assert miniyaml.load(text) == yaml.safe_load(text)


# --------------------------------------------------------------------------- #
# 场景加载
# --------------------------------------------------------------------------- #
class TestLoader:
    def test_suite_defaults_and_assert_sugar(self):
        suite = load_suite_text(
            """
name: t
env:
  BASE_URL: http://x
defaults:
  headers:
    Content-Type: application/json
  retry: { attempts: 2 }
cases:
  - id: c1
    name: 用例
    secrets: [token]
    steps:
      - id: login
        use: http_request
        with:
          url: /api/login
        capture: { token: body.token }
        assert:
          status: 200
          json:
            - path: token
              exists: true
"""
        )
        specs = suite.case_specs()
        steps = specs[0]["steps"]
        assert [s["id"] for s in steps] == ["login", "login__assert_status", "login__assert_json"]
        assert steps[0]["with"]["headers"] == {"Content-Type": "application/json"}
        assert steps[0]["retry"] == {"attempts": 2}
        assert steps[1]["with"]["source"] == "${login}"
        assert steps[1]["with"]["expected"] == 200
        assert specs[0]["secrets"] == ["token"]

    def test_json_suite(self):
        suite = load_suite_text(
            '{"name": "j", "cases": [{"id": "c", "steps": [{"id": "s", "use": "log", "with": {"message": "x"}}]}]}'
        )
        assert suite.name == "j" and len(suite.cases) == 1

    def test_validation_errors(self):
        with pytest.raises(SpecError, match="cases"):
            load_suite_dict({"name": "x"})
        with pytest.raises(SpecError, match="缺少 use"):
            load_suite_dict({"name": "x", "cases": [{"id": "c", "steps": [{"id": "s"}]}]})
        with pytest.raises(SpecError, match="重复"):
            load_suite_dict(
                {
                    "name": "x",
                    "cases": [
                        {"id": "c", "steps": [{"id": "s", "use": "log", "with": {"message": "1"}}]},
                        {"id": "c", "steps": [{"id": "s", "use": "log", "with": {"message": "2"}}]},
                    ],
                }
            )

    def test_filter_by_tag(self):
        suite = load_suite_text(
            """
name: t
cases:
  - id: c1
    tags: [smoke]
    steps: [{id: s, use: log, with: {message: a}}]
  - id: c2
    tags: [order]
    steps: [{id: s, use: log, with: {message: b}}]
"""
        )
        filtered = suite.filter(tags=["order"])
        assert [c["id"] for c in filtered.cases] == ["c2"]

    def test_unknown_assert_field(self):
        with pytest.raises(SpecError, match="未知字段"):
            load_suite_text(
                """
name: t
cases:
  - id: c
    steps:
      - id: s
        use: http_request
        with: {url: /x}
        assert:
          nope: 1
"""
            )
