"""执行引擎（领域中立：不含任何 HTTP / 文件系统知识）。"""

from .executor import (
    Executor,
    ExecutorSettings,
    classify_exception,
    classify_failure_class,
)
from .result import ERROR, FAILED, PASSED, SKIPPED, CaseResult, HealEvent, RunResult, StepResult

__all__ = [
    "ERROR",
    "FAILED",
    "PASSED",
    "SKIPPED",
    "CaseResult",
    "Executor",
    "ExecutorSettings",
    "HealEvent",
    "RunResult",
    "StepResult",
    "classify_exception",
    "classify_failure_class",
]
