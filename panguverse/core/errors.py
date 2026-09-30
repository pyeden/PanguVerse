"""PanguVerse 异常体系。

设计要点：**必须**区分「测试失败（failed）」与「执行错误（error）」。
- ``failed``：被测系统按预期工作，但结果不满足断言（例如期望 200 实得 404）。
- ``error``：执行链本身出问题（网络不可达、原子内部报错、模板变量缺失）。

报告、退出码、自愈策略全部依赖这个区分，混在一起会让自动化测试失去意义。
"""

from __future__ import annotations

from typing import Any


class PanguError(Exception):
    """所有 PanguVerse 异常的基类。"""

    kind = "pangu"
    status = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "kind": self.kind,
            "message": str(self),
            "detail": getattr(self, "detail", None),
        }


# --------------------------------------------------------------------------- #
# 定义期错误：出现在「拆解 / 编排 / 校验」阶段，应该 Fail Fast
# --------------------------------------------------------------------------- #
class SpecError(PanguError):
    """场景规格（YAML/JSON）不合法。"""

    kind = "spec"


class PlanError(PanguError):
    """计划不合法：引用了不存在的原子、缺少必填输入等。"""

    kind = "plan"


class AtomNotFoundError(PlanError):
    """原子库中不存在该原子。"""

    kind = "atom_not_found"


class TemplateError(PanguError):
    """模板表达式无法解析（变量拼写错误、路径不存在）。"""

    kind = "template"


# --------------------------------------------------------------------------- #
# 运行期错误
# --------------------------------------------------------------------------- #
class TransportError(PanguError):
    """传输层错误：连接失败、DNS、拒绝连接。"""

    kind = "network"
    status = "error"


class NetworkError(TransportError):
    """连接失败 / DNS / 拒绝连接。"""

    kind = "network"


class RequestTimeout(TransportError):
    """请求超时。"""

    kind = "timeout"


class StepFailure(PanguError):
    """步骤失败（测试失败），由断言原子抛出。"""

    kind = "assertion"
    status = "failed"

    def __init__(
        self,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
        path: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual
        self.path = path
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "expected": _brief(self.expected),
                "actual": _brief(self.actual),
                "path": self.path,
            }
        )
        return data


class AssertionFailed(StepFailure):
    """断言不满足。"""

    kind = "assertion"


class StepError(PanguError):
    """步骤执行错误：原子内部异常或环境问题。"""

    kind = "error"
    status = "error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.detail = detail


class AuthExpired(StepError):
    """凭证失效（401/403），可被自愈器识别并重新认证。"""

    kind = "auth"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, detail={"status_code": status_code})
        self.status_code = status_code


def _brief(value: Any, limit: int = 400) -> Any:
    """把可能要进报告的值压成可读的短形式。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...(+{len(value) - limit} chars)"
    if isinstance(value, (list, dict)):
        text = repr(value)
        if len(text) > limit:
            return text[:limit] + f"...(+{len(text) - limit} chars)"
    return value
