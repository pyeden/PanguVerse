"""平台通用能力测试：原子契约、领域包、声明式原子、能力授权、静态校验、跨领域组合。

这一组测试守护的是「内核领域中立」这条架构红线：
新增一个行业只需要加一个领域包，**内核一行都不用改**。
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import ClassVar

import pytest

from panguverse.axe.axe import PanguAxe
from panguverse.axe.plan import PlanRequest, TestPlan
from panguverse.core.atom import Atom
from panguverse.core.contract import Effect, FailureClass, Port, retry_safe
from panguverse.core.errors import AtomFailure
from panguverse.core.molecule import Molecule
from panguverse.core.pack import (
    AtomPack,
    PackRegistry,
    builtin_pack_registry,
    load_pack_dir,
    load_pack_file,
)
from panguverse.core.registry import AtomRegistry, default_registry
from panguverse.core.validation import check_plan
from panguverse.engine.executor import Executor, ExecutorSettings
from panguverse.packs import pack_names
from panguverse.packs.api_testing import ApiTestingSemantics
from panguverse.spec.loader import load_suite_text

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE_PACK = REPO_ROOT / "examples" / "packs" / "ops_kit.pack.yaml"
SCENARIOS = REPO_ROOT / "scenarios"


# --------------------------------------------------------------------------- #
# 原子契约
# --------------------------------------------------------------------------- #
class TestAtomContract:
    def test_ports_from_type_hints(self, registry):
        atom = registry.get("http_request")
        ports = {port.name: port for port in atom.input_ports()}
        assert ports["url"].required and ports["url"].type.startswith("string")
        assert not ports["method"].secret
        assert all(isinstance(port, Port) for port in atom.output_ports())

    def test_effects_follow_http_method(self, registry):
        atom = registry.get("http_request")
        assert atom.effects_for({"method": "GET"}) == (Effect.READ,)
        assert atom.effects_for({"method": "POST"}) == (Effect.MUTATE,)
        assert atom.effects_for({"method": "DELETE"}) == (Effect.DESTRUCTIVE,)
        # 重试安全性由契约推导：只读安全、非幂等不安全
        assert atom.retry_safe_for({"method": "GET"}) is True
        assert atom.retry_safe_for({"method": "POST"}) is False

    def test_retry_safe_helper(self):
        assert retry_safe([Effect.PURE, Effect.READ, Effect.WRITE])
        assert not retry_safe([Effect.MUTATE])
        assert not retry_safe([])

    def test_secret_outputs_declaration(self):
        atom = Atom(
            name="login",
            fn=lambda: None,
            outputs={"token": "string"},
            secret_outputs=("token",),
        )
        assert atom.secrets_in_outputs({"token": "abc", "other": 1}) == [("token", "abc")]
        assert any(port.secret for port in atom.output_ports())

    def test_declares_variables(self):
        atom = Atom(name="set_x", fn=lambda: None, declares_variables=("name",))
        assert atom.spec()["declares_variables"] == ["name"]

    def test_atom_failure_carries_domain_semantics(self):
        from panguverse.core.contract import neutral_failure_class, neutral_failure_kind

        exc = AtomFailure("文件不存在", kind="file_not_found", failure_class="missing_resource")
        assert neutral_failure_kind(exc) == "file_not_found"
        assert neutral_failure_class(exc) is FailureClass.MISSING_RESOURCE

    def test_spec_exposes_contract(self, registry):
        spec = registry.get("fs_write_text").spec()
        assert spec["domain"] == "filesystem"
        assert "write" in spec["effects"]
        assert spec["capabilities"] == ["filesystem"]


# --------------------------------------------------------------------------- #
# 领域包体系
# --------------------------------------------------------------------------- #
class TestPackRegistry:
    def test_builtin_packs_and_domains(self):
        registry = builtin_pack_registry(reload=True)
        assert sorted(pack_names()) == ["api_testing", "core", "local_fs"]
        assert registry.domains() == ["api", "core", "filesystem"]
        assert len(registry.atoms()) == 24
        assert registry.capabilities() == ("network", "filesystem")

    def test_semantics_resolution_is_per_atom(self, registry):
        assert registry.semantics_for("http_request").name == "api_testing"
        assert registry.semantics_for("fs_write_text").name == "local_fs"
        assert registry.semantics_for("log").name == "generic"
        assert registry.pack_of("fs_copy").name == "local_fs"

    def test_manifest_is_reviewable(self):
        manifest = builtin_pack_registry(reload=True).manifest()
        assert manifest["atom_count"] == 24
        assert {pack["name"] for pack in manifest["packs"]} == set(pack_names())
        for pack in manifest["packs"]:
            assert pack["version"] and pack["description"]
            assert isinstance(pack["atoms"], list) and pack["atoms"]

    def test_validate_detects_conflicts(self):
        registry = PackRegistry()
        atom = Atom(name="dup", fn=lambda: None, description="x", inputs={"a": "string"})
        registry.register(AtomPack(name="p1", domain="d1", description="d", atoms=[atom]))
        registry.register(AtomPack(name="p2", domain="d2", description="d", atoms=[atom]))
        problems = registry.validate()
        assert any("原子重名" in problem for problem in problems)

    def test_validate_reports_incomplete_pack(self):
        pack = AtomPack(name="bad", atoms=[Atom(name="a", fn=lambda: None)])
        problems = pack.validate()
        assert any("description" in problem for problem in problems)
        assert any("输入/输出" in problem for problem in problems)

    def test_build_atom_registry_carries_semantics(self):
        registry = builtin_pack_registry(reload=True).build_atom_registry()
        assert isinstance(registry, AtomRegistry)
        assert registry.get("http_request").domain == "api"
        assert len(registry.packs()) == 3

    def test_semantics_are_replaceable_without_touching_core(self):
        class MySemantics(ApiTestingSemantics):
            name = "my_api"

            def failure_kind(self, atom, outputs, exc):  # 完全换一套领域词汇
                return "custom_kind"

        pack = AtomPack(
            name="my_pack",
            domain="custom",
            semantics=MySemantics(),
            atoms=[Atom(name="x", fn=lambda: None, description="d", inputs={"a": "string"})],
        )
        registry = PackRegistry()
        registry.register(pack)
        atom_registry = registry.build_atom_registry()
        assert atom_registry.semantics_for("x").failure_kind(None, None, None) == "custom_kind"


# --------------------------------------------------------------------------- #
# 声明式（YAML）原子：不写 Python 也能贡献原子
# --------------------------------------------------------------------------- #
class TestDeclarativePack:
    def test_example_pack_loads_and_validates(self):
        pack = load_pack_file(EXAMPLE_PACK)
        assert pack.name == "ops_kit" and pack.domain == "ops"
        assert pack.source == "local" and pack.path.endswith("ops_kit.pack.yaml")
        assert [atom.name for atom in pack.atoms] == ["snapshot_file", "collect_metrics"]
        assert pack.validate() == []
        manifest = pack.manifest()
        assert manifest["atoms"][0].get("source", True)

    def test_declarative_atom_metadata(self):
        pack = load_pack_file(EXAMPLE_PACK)
        atom = next(item for item in pack.atoms if item.name == "snapshot_file")
        assert atom.source == "declarative"
        assert atom.pack == "ops_kit"
        assert atom.domain == "ops"
        assert atom.required == ("source", "target")
        assert set(atom.outputs) == {"target", "exists"}

    def test_declarative_atom_runs_end_to_end(self, tmp_path):
        """声明式原子 = 用数据描述的原子，运行行为与 Python 原子完全一致。"""
        from panguverse.core.pack import load_pack_dir

        registry = default_registry(reload=True)
        for pack in load_pack_dir(EXAMPLE_PACK.parent):
            registry.register_pack(pack, replace=True)
        executor = Executor(registry)
        source = tmp_path / "src.txt"
        source.write_text("payload", encoding="utf-8")
        case = Molecule.from_spec(
            {
                "id": "decl",
                "steps": [
                    {
                        "id": "snap",
                        "use": "snapshot_file",
                        "with": {"source": str(source), "target": str(tmp_path / "backup" / "src.txt")},
                        "capture": {"snap_target": "target", "snap_exists": "exists"},
                    }
                ],
            }
        )
        result = executor.run_case(case, env={"FS_ROOT": str(tmp_path)})
        assert result.status == "passed", result.steps[0].error
        assert pathlib.Path(result.variables["vars"]["snap_target"]).exists()
        assert result.variables["vars"]["snap_exists"] is True

    def test_declarative_atom_requires_inputs(self, tmp_path):
        registry = default_registry(reload=True)
        registry.register_pack(load_pack_file(EXAMPLE_PACK), replace=True)
        executor = Executor(registry)
        case = Molecule.from_spec(
            {"id": "decl", "steps": [{"id": "snap", "use": "snapshot_file", "with": {"source": "a"}}]}
        )
        result = executor.run_case(case, env={"FS_ROOT": str(tmp_path)})
        assert result.status == "error"
        assert "target" in result.steps[0].error["message"]

    def test_pack_file_can_be_a_directory(self, tmp_path):
        copied = tmp_path / "packs"
        copied.mkdir()
        (copied / "x.pack.yaml").write_text(
            "pack: {name: dir_pack, domain: demo, version: 0.1.0, description: 目录加载}\n"
            "atoms:\n"
            "  - name: noop\n"
            "    description: 什么都不做\n"
            "    inputs: {}\n"
            "    outputs: {ok: boolean}\n"
            "    steps:\n"
            "      - id: log\n"
            "        use: log\n"
            "        with: {message: hi}\n"
            "    outputs_map: {ok: true}\n",
            encoding="utf-8",
        )
        packs = load_pack_dir(copied)
        assert [pack.name for pack in packs] == ["dir_pack"]
        assert packs[0].atoms[0].required == ()


# --------------------------------------------------------------------------- #
# 能力授权（平台化的安全边界）
# --------------------------------------------------------------------------- #
class TestCapabilities:
    def test_missing_capability_is_rejected(self, registry, base_url):
        executor = Executor(registry, settings=ExecutorSettings(capabilities=()))
        result = executor.run_case(
            Molecule.from_spec(
                {"id": "c", "steps": [{"id": "h", "use": "http_request",
                                       "with": {"url": f"{base_url}/api/health"}}]}
            ),
            env={"BASE_URL": base_url},
        )
        assert result.status == "error"
        assert "未授权的能力" in result.steps[0].error["message"]

    def test_granted_capability_allows_execution(self, registry, base_url, tmp_path):
        executor = Executor(registry, settings=ExecutorSettings(capabilities=("network", "filesystem")))
        result = executor.run_case(
            Molecule.from_spec(
                {"id": "c", "steps": [
                    {"id": "h", "use": "http_request", "with": {"url": f"{base_url}/api/health",
                                                                 "expect_status": 200}},
                    {"id": "w", "use": "fs_write_text",
                     "with": {"path": "cap.txt", "text": "ok"}},
                ]}
            ),
            env={"BASE_URL": base_url, "FS_ROOT": str(tmp_path)},
        )
        assert result.status == "passed", result.steps[-1].error

    def test_none_means_unrestricted(self, registry, base_url):
        executor = Executor(registry, settings=ExecutorSettings(capabilities=None))
        result = executor.run_case(
            Molecule.from_spec({"id": "c", "steps": [{"id": "h", "use": "http_request",
                                                      "with": {"url": f"{base_url}/api/health",
                                                               "expect_status": 200}}]}),
            env={"BASE_URL": base_url},
        )
        assert result.status == "passed"


# --------------------------------------------------------------------------- #
# 文件系统领域包（非 API 领域的通用性证据）
# --------------------------------------------------------------------------- #
class TestLocalFsPack:
    def run(self, registry, spec, fs_root):
        executor = Executor(registry)
        return executor.run_case(Molecule.from_spec(spec), env={"FS_ROOT": str(fs_root)})

    def test_write_read_list_exists(self, registry, tmp_path):
        result = self.run(
            registry,
            {"id": "fs", "steps": [
                {"id": "w", "use": "fs_write_text", "with": {"path": "a/b.txt", "text": "你好"}},
                {"id": "r", "use": "fs_read_text", "with": {"path": "a/b.txt"}},
                {"id": "l", "use": "fs_list", "with": {"path": "a", "pattern": "*.txt"}},
                {"id": "e", "use": "fs_exists", "with": {"path": "a/b.txt"}},
            ]},
            tmp_path,
        )
        assert result.status == "passed", result.steps[-1].error
        variables = result.variables["vars"]
        assert variables["r"]["text"] == "你好"
        assert variables["l"]["count"] == 1
        assert variables["e"]["exists"] is True and variables["e"]["is_file"] is True

    def test_copy_delete_and_file_assertions(self, registry, tmp_path):
        result = self.run(
            registry,
            {"id": "fs", "steps": [
                {"id": "w", "use": "fs_write_text", "with": {"path": "f.txt", "text": "hello"}},
                {"id": "c", "use": "fs_copy", "with": {"source": "f.txt", "target": "copy/f.txt"}},
                {"id": "a", "use": "assert_file_exists", "with": {"path": "copy/f.txt"}},
                {"id": "b", "use": "assert_file_contains", "with": {"path": "copy/f.txt", "text": "ell"}},
                {"id": "d", "use": "fs_delete", "with": {"path": "f.txt"}},
                {"id": "a2", "use": "assert_file_exists", "with": {"path": "f.txt", "exists": False}},
            ]},
            tmp_path,
        )
        assert result.status == "passed", result.steps[-1].error

    def test_missing_file_is_domain_failure_with_advice(self, registry, tmp_path):
        executor = Executor(registry)
        result = executor.run_case(
            Molecule.from_spec({"id": "fs", "secrets": [], "steps": [
                {"id": "r", "use": "fs_read_text", "with": {"path": "nope.txt"}}]}),
            env={"FS_ROOT": str(tmp_path)},
        )
        error = result.steps[0].error
        assert error["failure_kind"] == "file_not_found"
        assert error["failure_class"] == "missing_resource"
        # 领域包给出的建议（而不是 HTTP 的建议）
        semantics = registry.semantics_for("fs_read_text")
        advice = semantics.advice(kind="file_not_found", failure_class=FailureClass.MISSING_RESOURCE)
        assert "文件不存在" in advice

    def test_sandbox_rejects_path_escape(self, registry, tmp_path):
        outside = tmp_path.parent / "escaped.txt"
        result = self.run(
            registry,
            {"id": "fs", "steps": [
                {"id": "w", "use": "fs_write_text",
                 "with": {"path": str(outside), "text": "x"}}]},
            tmp_path,
        )
        assert result.status == "error"
        assert result.steps[0].error["failure_kind"] == "path_escape"
        assert result.steps[0].error["failure_class"] == "permission"
        assert not outside.exists()

    def test_session_syntax_is_domain_specific(self, registry, tmp_path):
        """文件领域认 expect:，不认 HTTP 的 assert:。"""
        suite = load_suite_text(
            """
name: fs
env: {FS_ROOT: .}
cases:
  - id: c
    steps:
      - id: w
        use: fs_write_text
        with: {path: out.txt, text: hello}
        expect:
          exists: true
          contains: hello
"""
        )
        specs = suite.case_specs(registry)
        step_ids = [step["id"] for step in specs[0]["steps"]]
        assert step_ids == ["w", "w__assert_file_exists", "w__assert_file_contains"]

    def test_wrong_domain_sugar_is_reported_by_validation(self, registry, tmp_path):
        suite = load_suite_text(
            """
name: fs
env: {FS_ROOT: .}
cases:
  - id: c
    steps:
      - id: w
        use: fs_write_text
        with: {path: out.txt, text: hello}
        assert:
          status: 200
"""
        )
        # 加载阶段不报错（assert 不是文件领域的糖），但静态校验会明确指出领域用错
        specs = suite.case_specs(registry)
        plan = TestPlan(cases=[Molecule.from_spec(specs[0])])
        result = plan.check(registry)
        assert not result.ok
        assert any(issue.code == "P102" for issue in result.errors)


# --------------------------------------------------------------------------- #
# 静态计划校验
# --------------------------------------------------------------------------- #
class TestStaticValidation:
    def plan(self, text: str, registry):
        suite = load_suite_text(text)
        return TestPlan(cases=[Molecule.from_spec(spec, source="spec") for spec in suite.case_specs(registry)])

    def test_unknown_variable_typo_gets_hint(self, registry):
        plan = self.plan(
            """
name: s
env: {BASE_URL: http://x}
cases:
  - id: c
    steps:
      - id: login
        use: http_request
        with: {url: "${env.BASE_URL}/api/login"}
        capture: {token: body.token}
      - id: me
        use: http_request
        with: {url: "${env.BASE_URL}/api/me", headers: {Authorization: "Bearer ${toke}"}}
""",
            registry,
        )
        result = plan.check(registry, known_variables={"BASE_URL"})
        codes = [issue.code for issue in result.errors]
        assert "P101" in codes
        hint = next(issue.hint for issue in result.errors if issue.code == "P101")
        assert "token" in hint and "想写" in hint

    def test_dynamic_and_loop_variables_are_recognized(self, registry):
        plan = self.plan(
            """
name: s
env: {BASE_URL: http://x}
cases:
  - id: c
    steps:
      - id: prepare
        use: set_var
        with: {name: skus, value: [A, B]}
      - id: each
        use: http_request
        foreach: ${skus}
        as: sku
        with: {url: "${env.BASE_URL}/api/orders", json: {sku: "${sku}"}}
""",
            registry,
        )
        result = plan.check(registry, known_variables={"BASE_URL"})
        assert result.ok, result.messages()

    def test_type_and_retry_warnings(self, registry):
        plan = self.plan(
            """
name: s
env: {BASE_URL: http://x}
cases:
  - id: c
    steps:
      - id: pay
        use: http_request
        retry: {attempts: 3}
        with: {method: POST, url: "${env.BASE_URL}/pay", url_weight: 1}
""",
            registry,
        )
        result = plan.check(registry, known_variables={"BASE_URL"})
        codes = [issue.code for issue in result.warnings]
        assert "P202" in codes  # 非幂等 + 重试

    def test_capability_warning(self, registry):
        plan = self.plan(
            """
name: s
env: {BASE_URL: http://x}
cases:
  - id: c
    steps:
      - id: h
        use: http_request
        with: {url: "${env.BASE_URL}/api/health"}
""",
            registry,
        )
        result = plan.check(registry, known_variables={"BASE_URL"}, capabilities=())
        assert any(issue.code == "P203" for issue in result.warnings)

    def test_empty_plan(self, registry):
        result = check_plan(TestPlan(), registry)
        assert not result.ok and "没有任何用例" in result.errors[0].message


# --------------------------------------------------------------------------- #
# 跨领域组合（同一个用例里混用 API 与文件系统原子）
# --------------------------------------------------------------------------- #
class TestCrossDomainComposition:
    def test_shipped_cross_domain_scenario(self, tmp_path, env):
        from panguverse.spec.loader import load_suite

        suite = load_suite(SCENARIOS / "ops_snapshot.yaml")
        axe = PanguAxe(workspace=tmp_path / "ws", enable_llm=False)
        fs_root = tmp_path / "fs"
        result = axe.loop(
            PlanRequest(suite=suite, env={**env, "FS_ROOT": str(fs_root)}),
            out_dir=tmp_path / "out",
            regression=True,
        )
        assert result.exit_code == 0, result.run.cases[0].error
        assert result.summary["passed"] == 1

        # 快照真的落在沙箱里，并且被归档（回归复用会再跑一遍，因此至少各 1 个）
        snapshots = list((fs_root / "snapshots").glob("*.json"))
        archives = list((fs_root / "archive").glob("*.json"))
        assert len(snapshots) >= 1 and len(archives) >= 1
        for path in snapshots:
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["status"] == "CREATED" and payload["sku"] == "SNAPSHOT-1"

        # 回归复用同样能跨领域重跑
        assert result.regression is not None and result.regression.summary["passed"] == 1

    def test_generic_assertion_reused_across_domains(self, registry, tmp_path):
        """同一个 assert_json 原子，既可断言 HTTP 响应体，也可断言文件里的 JSON。"""
        executor = Executor(registry)
        result = executor.run_case(
            Molecule.from_spec(
                {"id": "c", "steps": [
                    {"id": "w", "use": "fs_write_json",
                     "with": {"path": "d.json", "data": {"state": "READY", "n": 3}}},
                    {"id": "r", "use": "fs_read_json", "with": {"path": "d.json"}},
                    {"id": "a1", "use": "assert_json",
                     "with": {"source": "${r.data}", "path": "state", "equals": "READY"}},
                    {"id": "a2", "use": "assert_json",
                     "with": {"source": "${r.data}", "path": "n", "gte": 1}},
                ]}
            ),
            env={"FS_ROOT": str(tmp_path)},
        )
        assert result.status == "passed", result.steps[-1].error


# --------------------------------------------------------------------------- #
# 意图模板按领域包分发（新行业加模板 = 加包，不改内核）
# --------------------------------------------------------------------------- #
class TestIntentTemplates:
    def test_templates_collected_from_all_packs(self, registry):
        from panguverse.axe.planner import RuleBasedPlanner

        templates = RuleBasedPlanner(registry).templates()
        packs = {template.pack for template in templates}
        assert packs == {"api_testing", "local_fs"}
        assert {"health_check", "fs_config_backup"} <= {template.name for template in templates}

    def test_non_api_intent_plans_and_runs(self, registry, tmp_path):
        """非 API 领域的自然语言意图：只靠领域包模板就能产出可执行、可校验的计划。"""
        from panguverse.axe.plan import PlanRequest
        from panguverse.axe.planner import RuleBasedPlanner

        plan = RuleBasedPlanner(registry).plan(
            PlanRequest(intent="备份配置文件并校验备份存在", env={"FS_ROOT": str(tmp_path)})
        )
        assert plan.source == "rules"
        assert plan.meta["template_packs"] == ["local_fs"]
        assert "fs_config_backup" in plan.meta["templates"]
        assert plan.validate(registry, known_variables={"FS_ROOT"}) == []

        # 模板里的领域语法糖（expect:）也被正确展开
        case = next(item for item in plan.cases if item.case_id == "config_backup")
        uses = [step.use for step in case.steps]
        assert "fs_copy" in uses and "assert_file_exists" in uses

        # 而且真的能跑通（纯文件系统闭环，无任何外部依赖）
        from panguverse.axe.axe import PanguAxe

        axe = PanguAxe(workspace=tmp_path / "ws", registry=registry, enable_llm=False)
        run = axe.run(plan, env={"FS_ROOT": str(tmp_path)})
        assert run.summary["success"] is True, run.cases[0].error
        assert (tmp_path / "backups" / "app.conf.bak").exists()

    def test_manifest_lists_templates(self):
        manifest = builtin_pack_registry(reload=True).manifest()
        per_pack = {pack["name"]: pack["intent_templates"] for pack in manifest["packs"]}
        assert len(per_pack["api_testing"]) == 7
        assert len(per_pack["local_fs"]) == 3
        assert per_pack["core"] == []


# --------------------------------------------------------------------------- #
# 架构红线：内核不得含领域知识
# --------------------------------------------------------------------------- #
class TestKernelStaysDomainNeutral:
    """把「内核领域中立」变成 CI 能拦住的规则，而不是口头约定。"""

    NEUTRAL_FILES: ClassVar[list[str]] = [
        "panguverse/engine/executor.py",
        "panguverse/axe/healer.py",
        "panguverse/axe/memory.py",
        "panguverse/axe/planner.py",
        "panguverse/core/molecule.py",
        "panguverse/core/validation.py",
        "panguverse/spec/loader.py",
    ]
    FORBIDDEN = ("status_code", "Bearer", "server_error", "rate_limit", "not_found", "BASE_URL")

    @staticmethod
    def _code_without_docstrings(path: pathlib.Path) -> str:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        drop: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    start = body[0].lineno - 1
                    end = getattr(body[0], "end_lineno", start + 1)
                    drop.update(range(start, end))
        return "\n".join(
            line for index, line in enumerate(lines)
            if index not in drop and not line.strip().startswith("#")
        )

    @pytest.mark.parametrize("relative", NEUTRAL_FILES)
    def test_no_domain_tokens_in_kernel_code(self, relative):
        path = REPO_ROOT / relative
        code = self._code_without_docstrings(path)
        hits = [token for token in self.FORBIDDEN if token in code]
        assert not hits, (
            f"{relative} 出现了领域专属词汇 {hits}——领域知识必须放进 panguverse/packs/*"
        )

    def test_healer_and_memory_use_semantics_hooks(self):
        healer = (REPO_ROOT / "panguverse/axe/healer.py").read_text(encoding="utf-8")
        assert ".recoveries(" in healer          # 恢复策略来自领域包
        assert "retry_safe_for" in healer        # 重试安全来自原子契约
        memory = (REPO_ROOT / "panguverse/axe/memory.py").read_text(encoding="utf-8")
        assert "failure_class" in memory         # 经验用中立类别检索，跨领域复用
