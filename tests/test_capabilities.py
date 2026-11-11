"""G2：执行前能力闸门。

- 服务层默认能力集合：放行注册表能力，禁用 shell，可用环境变量覆盖；
- 计划校验：未授权能力是 error（P203），不是 warning。
"""

from __future__ import annotations

import pytest

from panguverse.axe.plan import TestPlan
from panguverse.core.atom import Atom
from panguverse.core.molecule import Molecule, Step
from panguverse.core.registry import AtomRegistry
from panguverse.core.validation import check_plan
from panguverse.web import service


class _Pack:
    def __init__(self, caps: tuple[str, ...]) -> None:
        self._caps = caps

    def capabilities(self) -> tuple[str, ...]:
        return self._caps


class _Registry:
    def __init__(self, caps: tuple[str, ...]) -> None:
        self._pack = _Pack(caps)

    def packs(self) -> list[_Pack]:
        return [self._pack]


def test_default_allowed_capabilities_denies_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PANGUVERSE_ALLOWED_CAPABILITIES", raising=False)
    monkeypatch.delenv("PANGUVERSE_DENIED_CAPABILITIES", raising=False)
    allowed = service.allowed_capabilities(_Registry(("network", "browser", "shell")))
    assert "network" in allowed and "browser" in allowed
    assert "shell" not in allowed


def test_allowed_capabilities_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANGUVERSE_ALLOWED_CAPABILITIES", "shell, network")
    allowed = service.allowed_capabilities(_Registry(("network", "shell")))
    assert allowed == frozenset({"shell", "network"})


def _registry_with(atom: Atom) -> AtomRegistry:
    registry = AtomRegistry("test")
    registry.register(atom)
    return registry


def _plan(atom_name: str) -> TestPlan:
    step = Step.from_spec({"id": "s1", "use": atom_name})
    molecule = Molecule(case_id="c1", name="case", steps=[step])
    return TestPlan(cases=[molecule], source="test", planner="test")


def test_unauthorized_capability_is_error() -> None:
    atom = Atom(name="shell_run", capabilities=("shell",), fn=lambda: None)
    result = check_plan(_plan("shell_run"), _registry_with(atom), capabilities={"network"})
    p203 = [issue for issue in result.issues if issue.code == "P203"]
    assert p203 and p203[0].level == "error"
    assert not result.ok


def test_authorized_capability_passes() -> None:
    atom = Atom(name="shell_run", capabilities=("shell",), fn=lambda: None)
    result = check_plan(_plan("shell_run"), _registry_with(atom), capabilities={"shell"})
    assert not [issue for issue in result.issues if issue.code == "P203"]
    assert result.ok
