"""🪓 PanguVerse — 劈开混沌，万物自驱。

第一块落地业务：**API 自动化测试闭环**。

```python
from panguverse import PanguAxe, PlanRequest

axe = PanguAxe()
result = axe.loop(
    PlanRequest(intent="测试登录、下单、支付的业务闭环", env={"BASE_URL": "http://127.0.0.1:8899"}),
    regression=True,
)
print(result.text())
```
"""

from .axe import (
    Evolution,
    Healer,
    HybridPlanner,
    LLMClient,
    LoopResult,
    PanguAxe,
    PlanRequest,
    TestPlan,
    Workspace,
)
from .core import (
    AssertionFailed,
    Atom,
    AtomRegistry,
    Molecule,
    RunContext,
    Step,
    atom,
    default_registry,
)
from .engine import CaseResult, Executor, ExecutorSettings, RunResult, StepResult
from .spec import TestSuite, load_suite, load_suite_dict, load_suite_text

__version__ = "0.1.0"

__all__ = [
    "AssertionFailed",
    "Atom",
    "AtomRegistry",
    "CaseResult",
    "Evolution",
    "Executor",
    "ExecutorSettings",
    "Healer",
    "HybridPlanner",
    "LLMClient",
    "LoopResult",
    "Molecule",
    "PanguAxe",
    "PlanRequest",
    "RunContext",
    "RunResult",
    "Step",
    "StepResult",
    "TestPlan",
    "TestSuite",
    "Workspace",
    "__version__",
    "atom",
    "default_registry",
    "load_suite",
    "load_suite_dict",
    "load_suite_text",
]
