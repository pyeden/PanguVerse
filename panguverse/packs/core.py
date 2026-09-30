"""``core`` 领域包：**领域中立的**通用原子与默认语义。

这里放的是任何行业都会用到的东西：等待、记录、主动失败、变量赋值、JSON 路径取值、
正则提取、JSON 结构断言。它们不依赖 HTTP、文件系统或任何具体协议。

:class:`GenericSemantics` 也是所有领域包的基类默认行为来源：
**不猜测领域信号、不做魔法修补**，只给出中立类别与可执行建议。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..atoms import extract, flow
from ..core.atom import Atom
from ..core.contract import NEUTRAL_ADVICE, AtomSemantics, FailureClass, neutral_failure_class, neutral_failure_kind
from ..core.errors import SpecError

#: 通用断言糖支持的字段（领域包在此基础上扩展；实际以 :meth:`GenericSemantics.assert_expanders` 为准）
GENERIC_ASSERT_KEYS = {"json", "schema", "message"}


def enrich(atom: Atom, **fields: Any) -> Atom:
    """把契约元数据补到**既有原子对象**上（保持对象身份不变，避免出现两份同义原子）。"""
    for key, value in fields.items():
        if value is None:
            continue
        setattr(atom, key, value)
    return atom


class GenericSemantics(AtomSemantics):
    """通用语义：只使用中立词汇，适合「没有领域假设」的原子。"""

    name = "generic"

    def failure_class(self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None) -> FailureClass:
        return neutral_failure_class(exc)

    def failure_kind(self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None) -> str:
        return neutral_failure_kind(exc)

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        return NEUTRAL_ADVICE.get(failure_class, NEUTRAL_ADVICE[FailureClass.UNKNOWN])

    def planning_hints(self) -> tuple[str, ...]:
        return (
            "步骤之间用 ${步骤id.输出字段} 传递数据；整串为单个模板时保留原始类型",
            "变量取名用 capture: {变量名: 输出路径}；临时变量用 set_var",
            "foreach 会对列表逐项执行，循环变量默认叫 item，可用 as 改名（index/loop 也可用）",
        )

    # -- 断言糖：``assert: {json: [...], schema: {...}}`` ------------------ #
    def sugar_keys(self) -> set[str]:
        return {"assert"}

    def assert_expanders(self) -> dict[str, Any]:
        """断言糖字段 → 展开函数表（领域包可以往表里加自己的字段）。"""
        return {
            "json": self._expand_json,
            "schema": self._expand_schema,
        }

    def assert_order(self) -> list[str]:
        """展开顺序（决定报告里断言步骤的先后）。"""
        return list(self.assert_expanders())

    def assert_keys(self) -> set[str]:
        return set(self.assert_expanders())

    def sugar(self, step_id: str, step: Mapping[str, Any], *, atom: Any = None) -> list[dict[str, Any]]:
        block = step.get("assert")
        if block is None:
            return []
        if not isinstance(block, Mapping):
            raise SpecError(f"步骤 {step_id} 的 assert 必须是映射")
        table = self.assert_expanders()
        unknown = set(block) - set(table)
        if unknown:
            raise SpecError(
                f"步骤 {step_id} 的 assert 含未知字段: {sorted(unknown)}；"
                f"该原子所属领域可用: {sorted(table)}"
            )
        expanded: list[dict[str, Any]] = []
        for key in self.assert_order():
            if key not in block or key not in table:
                continue
            expanded.extend(table[key](step_id, block[key], step))
        return expanded

    # -- 通用字段的展开实现 ---------------------------------------------- #
    def _assert_step(self, step_id: str, suffix: str, use: str, with_block: dict[str, Any], name: str) -> dict[str, Any]:
        return {
            "id": f"{step_id}__{suffix}",
            "use": use,
            "name": name,
            "with": {"source": f"${{{step_id}}}", **with_block},
            "tags": ["assert"],
        }

    def _expand_json(self, step_id: str, checks: Any, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        if isinstance(checks, Mapping):
            checks = [checks]
        if not isinstance(checks, list):
            raise SpecError(f"步骤 {step_id} 的 assert.json 必须是映射或列表")
        out: list[dict[str, Any]] = []
        for index, check in enumerate(checks):
            if not isinstance(check, Mapping):
                raise SpecError(f"步骤 {step_id} 的 assert.json 第 {index + 1} 项必须是映射")
            suffix = "assert_json" if len(checks) == 1 else f"assert_json_{index + 1}"
            out.append(
                self._assert_step(
                    step_id, suffix, "assert_json", dict(check),
                    f"{step_id} 字段断言 {check.get('path', '<root>')}",
                )
            )
        return out

    def _expand_schema(self, step_id: str, schema: Any, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [self._assert_step(step_id, "assert_schema", "assert_schema", {"schema": schema}, f"{step_id} 结构断言")]


def build_pack() -> Any:
    """构建 ``core`` 包。"""
    from ..core.pack import AtomPack

    atoms = [
        flow.sleep,
        flow.log,
        flow.fail,
        extract.set_var,
        extract.json_get,
        extract.regex_extract,
    ]
    # 这些原子的「某些输入的名字」本身就是新建变量名（静态校验需要知道）
    enrich(extract.set_var, declares_variables=("name",))
    enrich(extract.json_get, declares_variables=("save_as",))
    enrich(extract.regex_extract, declares_variables=("save_as",))
    from ..atoms import assertions

    atoms.extend([assertions.assert_json, assertions.assert_schema])
    for atom in atoms:
        atom.domain = "core"
    for atom in (flow.sleep, flow.log, flow.fail, extract.set_var, extract.json_get, extract.regex_extract):
        if not atom.effects:
            atom.effects = ("pure",)
    if not assertions.assert_json.effects:
        assertions.assert_json.effects = ("pure",)
    if not assertions.assert_schema.effects:
        assertions.assert_schema.effects = ("pure",)
    return AtomPack(
        name="core",
        domain="core",
        version="0.1.0",
        description="领域中立的通用原子：等待/记录/失败/变量/JSON 取值与结构断言",
        semantics=GenericSemantics(),
        atoms=atoms,
        tags=("builtin", "generic"),
    )
