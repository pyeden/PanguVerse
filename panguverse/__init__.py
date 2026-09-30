"""🪓 PanguVerse — 劈开混沌，万物自驱。

一个**通用原子编排平台**：把业务劈成原子步骤（Atom），组合成脚本（Molecule），
再由 AI 规划器编排执行，并支持自愈、沉淀与回归复用。
领域知识放在**领域包**里，内核保持中立——换行业 = 加一个包。

```python
from panguverse import PanguAxe, PlanRequest

axe = PanguAxe()
result = axe.loop(
    PlanRequest(intent="测试登录、下单、支付的业务闭环", env={"BASE_URL": "http://127.0.0.1:8899"}),
    regression=True,
)
print(result.text())
```

查看平台能力：``python -m panguverse packs list`` / ``panguverse atoms --domain filesystem``。
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
from .core.contract import Effect, FailureClass, Port, Recovery, RuleTemplate
from .core.errors import AtomFailure
from .core.pack import AtomPack, PackRegistry, builtin_pack_registry, load_pack_dir, load_pack_file
from .core.validation import Issue, check_plan
from .engine import CaseResult, Executor, ExecutorSettings, RunResult, StepResult
from .packs import builtin_packs
from .spec import TestSuite, load_suite, load_suite_dict, load_suite_text

__version__ = "0.1.0"

__all__ = [
    "AssertionFailed",
    "Atom",
    "AtomFailure",
    "AtomPack",
    "AtomRegistry",
    "CaseResult",
    "Effect",
    "Evolution",
    "Executor",
    "ExecutorSettings",
    "FailureClass",
    "Healer",
    "HybridPlanner",
    "Issue",
    "LLMClient",
    "LoopResult",
    "Molecule",
    "PackRegistry",
    "PanguAxe",
    "PlanRequest",
    "Port",
    "Recovery",
    "RuleTemplate",
    "RunContext",
    "RunResult",
    "Step",
    "StepResult",
    "TestPlan",
    "TestSuite",
    "Workspace",
    "__version__",
    "atom",
    "builtin_pack_registry",
    "builtin_packs",
    "check_plan",
    "default_registry",
    "load_pack_dir",
    "load_pack_file",
    "load_suite",
    "load_suite_dict",
    "load_suite_text",
]
