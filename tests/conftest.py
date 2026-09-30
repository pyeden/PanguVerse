"""pytest 共享夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from panguverse.axe.axe import PanguAxe  # noqa: E402
from panguverse.core.registry import default_registry  # noqa: E402
from panguverse.demo.mock_api import MockAPI  # noqa: E402


@pytest.fixture()
def mock_api() -> MockAPI:
    """每个测试一个干净的被测量（flaky 计数器独立）。"""
    api = MockAPI(flaky_failures=2)
    api.start()
    try:
        yield api
    finally:
        api.stop()


@pytest.fixture()
def base_url(mock_api: MockAPI) -> str:
    return mock_api.base_url


@pytest.fixture()
def env(base_url: str) -> dict[str, str]:
    return {"BASE_URL": base_url, "API_USERNAME": "demo", "API_PASSWORD": "demo123"}


@pytest.fixture()
def registry():
    return default_registry(reload=True)


@pytest.fixture()
def axe(tmp_path) -> PanguAxe:
    """无 LLM 的盘古斧（走规则规划 + 自愈）。"""
    return PanguAxe(workspace=tmp_path / ".panguverse", enable_llm=False)


def write_scenario(tmp_path: Path, text: str, name: str = "scenario.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path
