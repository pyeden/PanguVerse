"""通用节点模型：编排层身份 + 实现层契约 + 执行结果 + 证据日志。

节点（Node）是流程图上的稳定身份：位置、需求契约、implementation 绑定。
原子（Atom）是已注册的实现；草稿代码是未注册实现。
「自定义」只是 ``registration == "draft" and origin != "builtin"`` 的派生标签，
不参与执行、校验、日志或生命周期分支。
"""

from __future__ import annotations

import hashlib
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .atom import Atom
from .contract import Port

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class ImplementationKind(str, Enum):
    ATOM = "atom"
    CODE = "code"
    TEMPLATE = "template"
    SQL = "sql"
    HUMAN = "human"
    WORKFLOW = "workflow"


class Registration(str, Enum):
    REGISTERED = "registered"
    DRAFT = "draft"


class Lifecycle(str, Enum):
    DRAFT = "draft"
    DEBUGGED = "debugged"
    TASK_VERIFIED = "task_verified"
    CONFIRMED = "confirmed"
    INSTALLED = "installed"


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 1
    delay_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"attempts": self.attempts, "delay_ms": self.delay_ms}


@dataclass(frozen=True)
class NodeContract:
    """节点/实现的需求与供给契约。

    ``inputs/outputs`` 用现有 :class:`panguverse.core.contract.Port`，保持与原子一致。
    """

    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    capabilities: tuple[str, ...] = ()
    side_effect: str = "none"
    idempotency: str = "unknown"
    timeout_s: float | None = None
    retry: RetryPolicy | None = None
    risk: str = "L0"

    def validate_inputs(self, inputs: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        for port in self.inputs:
            if port.required and port.name not in inputs:
                errors.append(f"missing input: {port.name}")
                continue
            if port.name in inputs and not port.accepts(inputs[port.name]):
                errors.append(
                    f"input type mismatch: {port.name} expects {port.type}"
                )
        return errors

    def validate_outputs(self, outputs: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        if not isinstance(outputs, Mapping):
            return ["outputs must be a mapping"]
        for port in self.outputs:
            if port.required and port.name not in outputs:
                errors.append(f"missing output: {port.name}")
                continue
            if port.name in outputs and not port.accepts(outputs[port.name]):
                errors.append(
                    f"output type mismatch: {port.name} expects {port.type}"
                )
        return errors

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "inputs": [port.to_dict() for port in self.inputs],
            "outputs": [port.to_dict() for port in self.outputs],
            "capabilities": list(self.capabilities),
            "side_effect": self.side_effect,
            "idempotency": self.idempotency,
            "risk": self.risk,
        }
        if self.timeout_s is not None:
            data["timeout_s"] = self.timeout_s
        if self.retry is not None:
            data["retry"] = self.retry.to_dict()
        return data


# --------------------------------------------------------------------------- #
# 实现引用
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ImplementationRef:
    """实现的稳定引用；发布版本钉住它，运行时按它解析。"""

    kind: ImplementationKind
    name: str
    version: str = ""
    hash: str = ""

    @classmethod
    def parse(cls, text: str) -> "ImplementationRef":
        raw = (text or "").strip()
        head, sep, body = raw.partition(":")
        if not sep or not head or not body:
            raise ValueError(f"非法 implementation ref: {text!r}")
        try:
            kind = ImplementationKind(head)
        except ValueError as exc:
            raise ValueError(f"未知 implementation kind: {head!r}") from exc
        digest = ""
        if "#" in body:
            body, digest = body.split("#", 1)
        version = ""
        if "@" in body:
            body, version = body.split("@", 1)
        if not body:
            raise ValueError(f"implementation ref 缺少名称: {text!r}")
        return cls(kind=kind, name=body, version=version, hash=digest)

    def to_str(self) -> str:
        base = f"{self.kind.value}:{self.name}"
        if self.version:
            base += f"@{self.version}"
        if self.hash:
            base += f"#{self.hash}"
        return base

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "hash": self.hash,
            "ref": self.to_str(),
        }


# --------------------------------------------------------------------------- #
# 日志与结果
# --------------------------------------------------------------------------- #


@dataclass
class LogEvent:
    ts: float = field(default_factory=time.time)
    run_id: str = ""
    node_id: str = ""
    attempt: int = 1
    seq: int = 0
    level: str = "info"
    event: str = ""
    message: str = ""
    data: Mapping[str, Any] | None = None
    code_ref: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ts": self.ts,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "seq": self.seq,
            "level": self.level,
            "event": self.event,
            "message": self.message,
        }
        if self.data:
            data["data"] = dict(self.data)
        if self.code_ref:
            data["code_ref"] = dict(self.code_ref)
        return data


@dataclass
class NodeResult:
    outputs: dict[str, Any] = field(default_factory=dict)
    logs: list[LogEvent] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    duration_ms: float = 0.0
    attempt: int = 1

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "outputs": self.outputs,
            "logs": [item.to_dict() for item in self.logs],
            "artifacts": list(self.artifacts),
            "duration_ms": round(self.duration_ms, 3),
            "attempt": self.attempt,
        }
        if self.error is not None:
            data["error"] = dict(self.error)
        return data


# --------------------------------------------------------------------------- #
# 实现
# --------------------------------------------------------------------------- #


class Implementation:
    """实现后端统一接口；Node 只认它，不认来源。"""

    ref: ImplementationRef
    contract: NodeContract

    def execute(self, ctx: Any, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None


@dataclass
class AtomImplementation:
    """把已注册原子包装成统一实现。"""

    atom: Atom
    origin: str = "builtin"

    @property
    def ref(self) -> ImplementationRef:
        return ImplementationRef(ImplementationKind.ATOM, self.atom.name, self.atom.version)

    @property
    def contract(self) -> NodeContract:
        return NodeContract(
            inputs=self.atom.input_ports(),
            outputs=self.atom.output_ports(),
            capabilities=self.atom.capabilities,
            side_effect=_side_effect_of(self.atom),
            idempotency="yes" if self.atom.idempotent else "unknown",
            risk=_risk_of(self.atom),
        )

    def execute(self, ctx: Any, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.atom.invoke(ctx, inputs)


@dataclass
class CodeImplementation:
    """草稿代码实现；执行器由沙箱层注入，模型层不直接 exec。"""

    artifact_id: str
    code: str
    contract: NodeContract
    language: str = "python"
    revision: int = 1
    runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    origin: str = "ai_generated"

    @property
    def ref(self) -> ImplementationRef:
        digest = hashlib.sha256(self.code.encode("utf-8")).hexdigest()[:12]
        return ImplementationRef(
            ImplementationKind.CODE, self.artifact_id, str(self.revision), digest
        )

    def execute(self, ctx: Any, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.runner is None:
            raise RuntimeError("草稿代码尚未注入沙箱执行器")
        result = self.runner(inputs)
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise TypeError("代码实现必须返回 Mapping")
        return dict(result)


def _side_effect_of(atom: Atom) -> str:
    effects = {str(item) for item in atom.effects_for({})}
    if "destructive" in effects:
        return "destructive"
    if "mutate" in effects or "write" in effects:
        return "write"
    if "read" in effects:
        return "read"
    return "none"


def _risk_of(atom: Atom) -> str:
    if atom.is_destructive({}):
        return "L2"
    side_effect = _side_effect_of(atom)
    if side_effect in {"write", "external"} or atom.secret_inputs:
        return "L1"
    return "L0"


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    id: str
    name: str
    contract: NodeContract
    implementation: Implementation
    origin: str = "builtin"
    registration: Registration = Registration.REGISTERED
    revision: int = 1
    lifecycle: Lifecycle = Lifecycle.DRAFT
    locked_ranges: tuple[tuple[int, int], ...] = ()

    @property
    def is_custom(self) -> bool:
        return self.registration is Registration.DRAFT and self.origin != "builtin"

    def validate(self, inputs: Mapping[str, Any]) -> list[str]:
        return list(self.contract.validate_inputs(inputs))

    def invoke(self, ctx: Any, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """执行实现并返回原始输出；异常原样抛出，交给 executor 分类/重试。"""
        outputs = self.implementation.execute(ctx, inputs)
        return dict(outputs or {})

    def execute(
        self,
        ctx: Any,
        inputs: Mapping[str, Any],
        *,
        run_id: str = "",
        attempt: int = 1,
    ) -> NodeResult:
        started = time.perf_counter()
        errors = self.validate(inputs)
        if errors:
            return NodeResult(
                error={
                    "code": "input_contract",
                    "message": "; ".join(errors),
                    "node_id": self.id,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
                attempt=attempt,
            )
        try:
            outputs = self.implementation.execute(ctx, inputs)
        except Exception as exc:  # noqa: BLE001 - 统一落成 NodeResult
            return NodeResult(
                error={
                    "code": "execution_error",
                    "message": str(exc),
                    "node_id": self.id,
                    "code_ref": _code_ref_from_exception(exc),
                },
                duration_ms=(time.perf_counter() - started) * 1000,
                attempt=attempt,
            )
        output_errors = self.contract.validate_outputs(outputs)
        if output_errors:
            return NodeResult(
                outputs=dict(outputs),
                error={
                    "code": "output_contract",
                    "message": "; ".join(output_errors),
                    "node_id": self.id,
                },
                duration_ms=(time.perf_counter() - started) * 1000,
                attempt=attempt,
            )
        return NodeResult(
            outputs=dict(outputs),
            duration_ms=(time.perf_counter() - started) * 1000,
            attempt=attempt,
        )

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "contract": self.contract.to_dict(),
            "implementation": self.implementation.ref.to_dict(),
            "origin": self.origin,
            "registration": self.registration.value,
            "revision": self.revision,
            "lifecycle": self.lifecycle.value,
            "locked_ranges": [list(item) for item in self.locked_ranges],
            "is_custom": self.is_custom,
        }


def _code_ref_from_exception(exc: BaseException) -> dict[str, Any]:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return {}
    last = frames[-1]
    return {
        "language": "python",
        "file": last.filename,
        "line": last.lineno,
        "column": last.colno,
    }


def build_node_from_atom(
    atom: Atom,
    *,
    node_id: str | None = None,
    name: str | None = None,
    origin: str = "builtin",
    registration: Registration = Registration.REGISTERED,
    revision: int = 1,
) -> Node:
    implementation = AtomImplementation(atom, origin=origin)
    return Node(
        id=node_id or f"node-{atom.name}",
        name=name or atom.name,
        contract=implementation.contract,
        implementation=implementation,
        origin=origin,
        registration=registration,
        revision=revision,
    )


# --------------------------------------------------------------------------- #
# 流程指标与运行快照
# --------------------------------------------------------------------------- #


def flow_metrics(nodes: Sequence[Node]) -> dict[str, Any]:
    total = len(nodes)
    registered_atoms = sum(
        1
        for node in nodes
        if node.registration is Registration.REGISTERED
        and node.implementation.ref.kind is ImplementationKind.ATOM
    )
    custom = sum(1 for node in nodes if node.is_custom)
    atom_coverage = (registered_atoms / total) if total else 0.0
    reuse_score = round(atom_coverage * 100)
    if total == 0:
        grade = "A"
    elif custom == 0 and registered_atoms == total:
        grade = "A"
    elif all(
        node.is_custom
        and node.contract.idempotency == "yes"
        and node.contract.side_effect in {"none", "read"}
        for node in nodes
        if node.is_custom
    ):
        grade = "B"
    else:
        grade = "C"
    return {
        "node_count": total,
        "registered_atom_count": registered_atoms,
        "custom_node_count": custom,
        "atom_coverage": round(atom_coverage, 4),
        "reuse_score": reuse_score,
        "determinism_grade": grade,
        "custom_node_ids": [node.id for node in nodes if node.is_custom],
    }


def run_snapshot(run_id: str, nodes: Sequence[Node]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "ts": time.time(),
        "nodes": [node.to_snapshot() for node in nodes],
        "metrics": flow_metrics(nodes),
    }
