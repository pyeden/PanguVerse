"""流程与控制原子：暂停、记录、构造失败、变量搬运。"""

from __future__ import annotations

import time
from typing import Any

from ..core.atom import atom
from ..core.context import RunContext
from ..core.errors import StepFailure


@atom(
    "sleep",
    description="等待若干秒（限流、异步任务轮询）",
    category="flow",
    tags=("flow",),
    inputs={"seconds": "number", "reason": "string"},
    outputs={},
    required=("seconds",),
)
def sleep(ctx: RunContext, seconds: Any, reason: str = "") -> dict[str, Any]:
    duration = float(seconds)
    if duration > 0:
        if reason:
            ctx.emit("sleep", seconds=duration, reason=reason)
        time.sleep(duration)
    return {"slept_seconds": duration}


@atom(
    "log",
    description="往执行轨迹里写一条记录（便于排查链路）",
    category="flow",
    tags=("flow",),
    aliases=("note",),
    inputs={"message": "string", "level": "string=info"},
    outputs={},
    required=("message",),
)
def log(ctx: RunContext, message: str, level: str = "info") -> dict[str, Any]:
    ctx.emit("log", level=str(level), message=str(message))
    return {}


@atom(
    "fail",
    description="主动让当前步骤失败（用于「不该走到这里」的负向用例）",
    category="flow",
    tags=("flow", "assert"),
    inputs={"message": "string"},
    outputs={},
    required=("message",),
)
def fail(ctx: RunContext, message: str) -> dict[str, Any]:
    raise StepFailure(str(message), path="<flow>")
