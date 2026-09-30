"""场景规格加载：YAML/JSON → TestSuite。

场景文件是团队协作与 CI 的正式入口（比自然语言意图更精确）。格式示例::

    name: 订单业务闭环
    env:
      BASE_URL: http://127.0.0.1:8899
    defaults:
      headers:
        Content-Type: application/json
      retry: { attempts: 3, delay_ms: 100 }
    cases:
      - id: order_flow
        name: 登录 → 下单 → 支付
        tags: [smoke, order]
        secrets: [token]
        steps:
          - id: login
            use: http_request
            tags: [auth]
            with:
              method: POST
              url: ${env.BASE_URL}/api/login
              json: { username: demo, password: demo123 }
            capture: { token: body.token }
            assert:
              status: 200

``assert:`` 是便捷糖，会被展开成紧随其后的 ``assert_*`` 原子步骤，
让场景更短，同时保持执行模型统一（一切皆原子步骤）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import SpecError
from . import miniyaml


@dataclass
class TestSuite:
    """一个场景文件（可含多个用例）。"""

    name: str
    cases: list[dict[str, Any]] = field(default_factory=list)
    env: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    tags: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    path: str | None = None

    # ------------------------------------------------------------------ #
    def case_specs(self) -> list[dict[str, Any]]:
        """产出可直接交给 ``Molecule.from_spec`` 的用例规格（已套用默认值与断言糖）。"""
        specs: list[dict[str, Any]] = []
        for index, raw in enumerate(self.cases):
            if not isinstance(raw, dict):
                raise SpecError(f"用例 #{index + 1} 必须是映射，实际 {type(raw).__name__}")
            spec = dict(raw)
            spec.setdefault("id", spec.get("name") or f"case_{index + 1}")
            spec["steps"] = self._build_steps(spec.get("steps") or [], case_id=str(spec["id"]))
            specs.append(spec)
        return specs

    def filter(self, *, tags: list[str] | None = None, case_ids: list[str] | None = None) -> TestSuite:
        cases = self.cases
        if tags:
            wanted = {t.strip() for t in tags if t.strip()}
            cases = [
                c for c in cases if wanted & {str(t) for t in (c.get("tags") or [])}
            ]
        if case_ids:
            wanted_ids = {c.strip() for c in case_ids if c.strip()}
            cases = [c for c in cases if str(c.get("id") or c.get("name")) in wanted_ids]
        return TestSuite(
            name=self.name,
            cases=list(cases),
            env=dict(self.env),
            defaults=dict(self.defaults),
            description=self.description,
            tags=self.tags,
            meta=dict(self.meta),
            notes=list(self.notes),
            path=self.path,
        )

    def case_ids(self) -> list[str]:
        return [str(case.get("id") or case.get("name")) for case in self.cases]

    # ------------------------------------------------------------------ #
    def _build_steps(self, raw_steps: list[Any], *, case_id: str) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                raise SpecError(f"[{case_id}] 第 {index + 1} 个步骤必须是映射")
            step = _apply_defaults(raw, self.defaults)
            steps.append(step)
            steps.extend(_expand_asserts(step))
        return steps


# --------------------------------------------------------------------------- #
# 加载入口
# --------------------------------------------------------------------------- #
def load_suite(source: str | Path, *, prefer_pyyaml: bool = False) -> TestSuite:
    """从文件加载场景；按扩展名选择解析器（``.json`` 用 JSON，其余用 YAML 子集）。"""
    path = Path(source)
    if not path.exists():
        raise SpecError(f"场景文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    return load_suite_text(text, path=str(path), prefer_pyyaml=prefer_pyyaml)


def load_suite_text(
    text: str, *, path: str | None = None, prefer_pyyaml: bool = False
) -> TestSuite:
    stripped = text.lstrip()
    if path and path.lower().endswith(".json"):
        data = json.loads(text)
    elif stripped.startswith("{"):
        data = json.loads(text)
    else:
        data = miniyaml.load(text, prefer_pyyaml=prefer_pyyaml)
    if not isinstance(data, dict):
        raise SpecError(f"场景根节点必须是映射，实际 {type(data).__name__}")
    return load_suite_dict(data, path=path)


def load_suite_dict(data: dict[str, Any], *, path: str | None = None) -> TestSuite:
    name = data.get("name") or data.get("suite") or (Path(path).stem if path else "scenario")
    cases = data.get("cases") or data.get("scenarios")
    if not isinstance(cases, list) or not cases:
        raise SpecError("场景必须包含非空的 cases 列表")
    env = data.get("env") or {}
    defaults = data.get("defaults") or {}
    if not isinstance(env, dict):
        raise SpecError("env 必须是映射")
    if not isinstance(defaults, dict):
        raise SpecError("defaults 必须是映射")
    suite = TestSuite(
        name=str(name),
        cases=cases,
        env={str(k): v for k, v in env.items()},
        defaults=defaults,
        description=str(data.get("description") or ""),
        tags=tuple(data.get("tags") or ()),
        meta=dict(data.get("meta") or {}),
        notes=list(data.get("notes") or []),
        path=path,
    )
    _validate_suite(suite)
    return suite


def _validate_suite(suite: TestSuite) -> None:
    seen: set[str] = set()
    for index, case in enumerate(suite.cases):
        if not isinstance(case, dict):
            raise SpecError(f"用例 #{index + 1} 必须是映射")
        case_id = str(case.get("id") or case.get("name") or "")
        if not case_id:
            raise SpecError(f"用例 #{index + 1} 缺少 id/name")
        if case_id in seen:
            raise SpecError(f"用例 id 重复: {case_id}")
        seen.add(case_id)
        if not case.get("steps"):
            raise SpecError(f"用例 {case_id} 没有任何步骤")
        step_ids: list[str] = []
        for j, step in enumerate(case["steps"]):
            if not isinstance(step, dict):
                raise SpecError(f"[{case_id}] 第 {j + 1} 个步骤必须是映射")
            if not (step.get("use") or step.get("atom")):
                raise SpecError(f"[{case_id}] 第 {j + 1} 个步骤缺少 use/atom")
            step_ids.append(str(step.get("id") or f"step_{j + 1}"))
        dupes = {sid for sid in step_ids if step_ids.count(sid) > 1}
        if dupes:
            raise SpecError(f"用例 {case_id} 存在重复步骤 id: {sorted(dupes)}")
    # 断言糖展开也在加载阶段完成，让配置错误尽早暴露（fail fast）
    suite.case_specs()


# --------------------------------------------------------------------------- #
# 默认值与断言糖
# --------------------------------------------------------------------------- #
def _apply_defaults(step: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    step = dict(step)
    use = step.get("use") or step.get("atom")
    with_block = dict(step.get("with") or {})
    if use == "http_request":
        headers = dict(defaults.get("headers") or {})
        headers.update(with_block.get("headers") or {})
        if headers:
            with_block["headers"] = headers
        if "timeout" not in with_block and defaults.get("timeout") is not None:
            with_block["timeout"] = defaults["timeout"]
        if "retry" not in step and defaults.get("retry"):
            step["retry"] = defaults["retry"]
    step["with"] = with_block
    return step


_ASSERT_KEYS = {"status", "json", "schema", "latency_max_ms", "header", "body_contains", "message"}


def _expand_asserts(step: dict[str, Any]) -> list[dict[str, Any]]:
    """展开 ``assert:`` 糖为 assert_* 原子步骤。"""
    block = step.get("assert")
    if block is None:
        return []
    if not isinstance(block, dict):
        raise SpecError(f"步骤 {step.get('id')} 的 assert 必须是映射（或列表）")
    unknown = set(block) - _ASSERT_KEYS
    if unknown:
        raise SpecError(
            f"步骤 {step.get('id')} 的 assert 含未知字段: {sorted(unknown)}；"
            f"可用: {sorted(_ASSERT_KEYS)}"
        )
    step_id = str(step.get("id") or "step")
    source = f"${{{step_id}}}"
    expanded: list[dict[str, Any]] = []

    def add(suffix: str, atom: str, with_block: dict[str, Any], name: str) -> None:
        expanded.append(
            {
                "id": f"{step_id}__{suffix}",
                "use": atom,
                "name": name,
                "with": {"source": source, **with_block},
                "tags": ["assert"],
            }
        )

    if "status" in block:
        with_block = {"expected": block["status"]}
        if block.get("message"):
            with_block["message"] = block["message"]
        add("assert_status", "assert_status", with_block, f"{step_id} 状态码断言")
    json_checks = block.get("json")
    if json_checks:
        if isinstance(json_checks, dict):
            json_checks = [json_checks]
        for index, check in enumerate(json_checks):
            if not isinstance(check, dict):
                raise SpecError(f"步骤 {step_id} 的 assert.json 第 {index + 1} 项必须是映射")
            suffix = "assert_json" if len(json_checks) == 1 else f"assert_json_{index + 1}"
            add(suffix, "assert_json", dict(check), f"{step_id} 字段断言 {check.get('path', '<root>')}")
    if "schema" in block:
        add("assert_schema", "assert_schema", {"schema": block["schema"]}, f"{step_id} 结构断言")
    if "latency_max_ms" in block:
        add("assert_latency", "assert_latency", {"max_ms": block["latency_max_ms"]}, f"{step_id} 耗时断言")
    if "header" in block:
        header = block["header"]
        if isinstance(header, str):
            header = {"name": header}
        if not isinstance(header, dict) or "name" not in header:
            raise SpecError(f"步骤 {step_id} 的 assert.header 需要 name 字段，或写成 {{name: ..., contains: ...}}")
        add("assert_header", "assert_header", dict(header), f"{step_id} 响应头断言")
    if "body_contains" in block:
        add(
            "assert_body_contains",
            "assert_body_contains",
            {"text": block["body_contains"]},
            f"{step_id} 原文包含断言",
        )
    if not expanded:
        raise SpecError(f"步骤 {step_id} 的 assert 块没有可展开的断言")
    return expanded
