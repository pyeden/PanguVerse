"""通用 Node 模型测试：契约、实现解析、执行封装、指标与快照。"""

from __future__ import annotations

import pytest

from panguverse.core.atom import Atom
from panguverse.core.contract import Port
from panguverse.core.node import (
    CodeImplementation,
    ImplementationKind,
    ImplementationRef,
    Lifecycle,
    Node,
    NodeContract,
    Registration,
    build_node_from_atom,
    flow_metrics,
    run_snapshot,
)


def _echo_atom() -> Atom:
    return Atom(
        name="echo",
        fn=lambda text: {"out": text},
        inputs={"text": "string"},
        outputs={"out": "string"},
        effects=("read",),
    )


def _code_node(side_effect: str = "none", idempotency: str = "yes") -> Node:
    contract = NodeContract(
        inputs=(Port("a", "integer"),),
        outputs=(Port("b", "integer"),),
        side_effect=side_effect,
        idempotency=idempotency,
    )
    implementation = CodeImplementation(
        artifact_id="artifact-calc",
        code="def run(ctx):\n    return {'b': ctx['a'] + 1}\n",
        contract=contract,
        runner=lambda inputs: {"b": inputs["a"] + 1},
    )
    return Node(
        id="node-calc",
        name="计算",
        contract=contract,
        implementation=implementation,
        origin="ai_generated",
        registration=Registration.DRAFT,
    )


def test_implementation_ref_roundtrip() -> None:
    ref = ImplementationRef.parse("atom:http_request@2#abc123")
    assert ref.kind is ImplementationKind.ATOM
    assert ref.name == "http_request"
    assert ref.version == "2"
    assert ref.hash == "abc123"
    assert ref.to_str() == "atom:http_request@2#abc123"

    code = ImplementationRef.parse("code:artifact-x@3#deadbeef")
    assert code.kind is ImplementationKind.CODE
    assert code.version == "3"

    with pytest.raises(ValueError):
        ImplementationRef.parse("atom")
    with pytest.raises(ValueError):
        ImplementationRef.parse("unknown:thing@1")


def test_contract_validates_inputs_and_outputs() -> None:
    contract = NodeContract(
        inputs=(Port("name", "string"), Port("age", "integer")),
        outputs=(Port("ok", "boolean"),),
        idempotency="yes",
    )
    assert contract.validate_inputs({"name": "a", "age": 1}) == []
    assert "missing input: age" in contract.validate_inputs({"name": "a"})
    assert any("type mismatch" in item for item in contract.validate_inputs({"name": 1, "age": 1}))
    assert contract.validate_outputs({"ok": True}) == []
    assert "missing output: ok" in contract.validate_outputs({})


def test_atom_node_executes_through_same_envelope() -> None:
    node = build_node_from_atom(_echo_atom(), node_id="node-echo")
    result = node.execute(None, {"text": "hello"}, run_id="run-1")
    assert result.ok
    assert result.outputs == {"out": "hello"}
    assert result.duration_ms >= 0
    assert node.implementation.ref.to_str().startswith("atom:echo@")


def test_custom_is_only_a_derived_label() -> None:
    node = _code_node()
    assert node.is_custom is True
    node.registration = Registration.REGISTERED
    assert node.is_custom is False
    assert node.id == "node-calc"
    assert node.contract.inputs[0].name == "a"


def test_code_node_input_and_output_contract_errors() -> None:
    node = _code_node()
    missing = node.execute(None, {})
    assert missing.error is not None
    assert missing.error["code"] == "input_contract"

    bad_contract = NodeContract(
        inputs=(Port("a", "integer"),),
        outputs=(Port("b", "integer"),),
        side_effect="none",
        idempotency="yes",
    )
    bad = Node(
        id="node-bad",
        name="坏输出",
        contract=bad_contract,
        implementation=CodeImplementation(
            artifact_id="artifact-bad",
            code="pass",
            contract=bad_contract,
            runner=lambda inputs: {"b": "not-an-int"},
        ),
        origin="ai_generated",
        registration=Registration.DRAFT,
    )
    result = bad.execute(None, {"a": 1})
    assert result.error is not None
    assert result.error["code"] == "output_contract"


def test_flow_metrics_and_grade() -> None:
    atom_node = build_node_from_atom(_echo_atom(), node_id="node-echo")
    custom_node = _code_node(side_effect="none", idempotency="yes")
    metrics = flow_metrics([atom_node, custom_node])
    assert metrics["node_count"] == 2
    assert metrics["registered_atom_count"] == 1
    assert metrics["custom_node_count"] == 1
    assert metrics["atom_coverage"] == 0.5
    assert metrics["reuse_score"] == 50
    assert metrics["determinism_grade"] == "B"

    write_node = _code_node(side_effect="write", idempotency="unknown")
    assert flow_metrics([atom_node, write_node])["determinism_grade"] == "C"


def test_run_snapshot_contains_refs_and_metrics() -> None:
    node = build_node_from_atom(_echo_atom(), node_id="node-echo")
    snapshot = run_snapshot("run-1", [node])
    assert snapshot["run_id"] == "run-1"
    assert snapshot["nodes"][0]["id"] == "node-echo"
    assert snapshot["nodes"][0]["implementation"]["kind"] == "atom"
    assert snapshot["metrics"]["reuse_score"] == 100
    assert snapshot["metrics"]["determinism_grade"] == "A"
    assert snapshot["nodes"][0]["lifecycle"] == Lifecycle.DRAFT.value
