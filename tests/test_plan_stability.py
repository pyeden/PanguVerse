"""G6：规划必须显式 temperature=0，稳定性由度量脚本负责。"""

from __future__ import annotations

import pytest

from panguverse.axe.planner import LLMPlanner, PlanRequest
from panguverse.core.errors import PlanError
from panguverse.core.registry import AtomRegistry


class _RecordingClient:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, system: str, user: str, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("stop after recording")


def test_llm_planner_passes_temperature_zero() -> None:
    client = _RecordingClient()
    planner = LLMPlanner(AtomRegistry("test"), client)
    with pytest.raises(PlanError):
        planner.plan(PlanRequest(intent="打开百度提取热点新闻"))
    assert client.calls, "planner 应该调用一次 LLM"
    assert client.calls[0].get("temperature") == 0.0
