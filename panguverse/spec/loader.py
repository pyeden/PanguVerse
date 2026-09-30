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
    def case_specs(self, registry: Any = None) -> list[dict[str, Any]]:
        """产出可直接交给 ``Molecule.from_spec`` 的用例规格。

        默认值填充与语法糖展开都**委托给原子所属的领域包**：
        HTTP 原子认 ``assert:``，文件系统原子认 ``expect:``，
        第三方包可以定义自己的写法——加载器本身不知道任何领域。
        """
        from ..core.registry import default_registry

        reg = registry if registry is not None else default_registry()
        specs: list[dict[str, Any]] = []
        for index, raw in enumerate(self.cases):
            if not isinstance(raw, dict):
                raise SpecError(f"用例 #{index + 1} 必须是映射，实际 {type(raw).__name__}")
            spec = dict(raw)
            spec.setdefault("id", spec.get("name") or f"case_{index + 1}")
            spec["steps"] = self._build_steps(
                spec.get("steps") or [], case_id=str(spec["id"]), registry=reg
            )
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
    def _build_steps(
        self, raw_steps: list[Any], *, case_id: str, registry: Any = None
    ) -> list[dict[str, Any]]:
        from ..core.contract import NEUTRAL_SEMANTICS
        from ..core.registry import default_registry

        reg = registry if registry is not None else default_registry()
        steps: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                raise SpecError(f"[{case_id}] 第 {index + 1} 个步骤必须是映射")
            step = dict(raw)
            use = step.get("use") or step.get("atom")
            atom = reg.get(use) if (use and reg.has(use)) else None
            semantics = reg.semantics_for(use) if atom is not None else NEUTRAL_SEMANTICS

            # 1) 领域包声明的套件级默认值（例如 API 的 headers、文件系统的 encoding）
            extras = semantics.step_defaults(step, defaults=self.defaults) or {}
            with_block = dict(step.get("with") or {})
            for key, value in extras.items():
                if key not in with_block:
                    with_block[key] = value
                elif isinstance(value, dict) and isinstance(with_block[key], dict):
                    with_block[key] = {**value, **with_block[key]}  # 步骤自己的写法优先
            if with_block:
                step["with"] = with_block
            # 2) 重试默认值（领域中立的步骤级属性）
            if "retry" not in step and self.defaults.get("retry"):
                step["retry"] = self.defaults["retry"]

            steps.append(step)
            # 3) 领域专属语法糖：assert:（API）/ expect:（文件系统）/ 第三方自定义
            present = [key for key in semantics.sugar_keys() if key in step]
            if present:
                step_id = str(step.get("id") or step.get("name") or f"step_{index + 1}")
                produced: list[dict[str, Any]] = []
                for _key in present:
                    produced.extend(semantics.sugar(step_id, step, atom=atom))
                if not produced:
                    allowed = sorted(getattr(semantics, "assert_keys", lambda: set)())
                    raise SpecError(
                        f"步骤 {step_id} 的 {present[0]} 块没有可展开的断言"
                        f"（原子 {use} 的领域可用字段: {allowed}）"
                    )
                steps.extend(produced)
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


def expand_case_spec(
    spec: dict[str, Any],
    *,
    registry: Any = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对**单条**用例规格套用领域默认值与领域语法糖。

    规划器（例如领域包提供的意图模板）与场景文件走的是同一套展开逻辑，
    因此模板里也能直接写 ``assert:`` / ``expect:``，不会出现「模板里的糖没生效」这种隐蔽问题。
    """
    suite = TestSuite(name="<inline>", cases=[dict(spec)], defaults=dict(defaults or {}))
    return suite.case_specs(registry)[0]


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
