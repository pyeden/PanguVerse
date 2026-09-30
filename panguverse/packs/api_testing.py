"""``api_testing`` 领域包：**HTTP / API 测试的全部领域知识都住在这里**。

内核（executor / healer / memory / loader）不再包含任何 HTTP 概念——
状态码语义、``Bearer`` 提示、URL 路径作为经验标识、``assert:`` 语法糖、
接口默认 headers，全部由本包通过 :class:`ApiTestingSemantics` 提供。

对照：``panguverse/packs/local_fs.py`` 是同样结构的文件系统领域包。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..atoms import assertions, extract, http
from ..core.atom import Atom
from ..core.contract import FailureClass, Recovery
from ..core.errors import SpecError
from ..core.molecule import Molecule, Step
from .core import GenericSemantics, enrich

# --------------------------------------------------------------------------- #
# HTTP 领域词汇（只有本包知道）
# --------------------------------------------------------------------------- #
_RETRYABLE_KINDS = {"server_error", "rate_limit", "timeout", "network"}
_AUTH_HINTS = ("login", "auth", "token", "signin", "sign_in", "授权", "登录", "认证")

_STATUS_MAP: dict[int, tuple[FailureClass, str]] = {
    400: (FailureClass.INVALID_INPUT, "client_error"),
    401: (FailureClass.CREDENTIALS, "auth"),
    403: (FailureClass.CREDENTIALS, "auth"),
    404: (FailureClass.MISSING_RESOURCE, "not_found"),
    405: (FailureClass.INVALID_INPUT, "method_not_allowed"),
    409: (FailureClass.CONFLICT, "conflict"),
    422: (FailureClass.INVALID_INPUT, "validation_error"),
    429: (FailureClass.TRANSIENT, "rate_limit"),
}

_HTTP_ADVICE: dict[str, str] = {
    "server_error": "服务端错误(5xx)：多为下游抖动，可退避重试；若稳定复现，需查服务端日志",
    "rate_limit": "被限流(429)：退避重试或降低并发",
    "auth": "凭证失效(401/403)：重新获取 token 后重试；若频繁失效，检查 token TTL 与登录步骤",
    "not_found": "资源不存在(404)：确认 id 的来源是否正确，或在前置步骤先创建资源再断言",
    "conflict": "状态冲突(409)：用例可能被重复执行；改用具唯一性的数据，或先回滚/清理状态",
    "client_error": "请求不合法(4xx)：核对必填字段、类型与格式（多看一次报告里的 request）",
    "validation_error": "参数校验失败(422)：核对字段类型/取值范围与 OpenAPI 定义",
    "method_not_allowed": "方法不被允许(405)：核对接口文档里的 Method",
    "network": "网络不可达：确认 BASE_URL、目标服务是否已启动、是否需要代理",
    "timeout": "接口过慢：放宽 timeout，或改为轮询 + sleep",
    "assertion": "断言与实现不一致：核对期望值来源；若为接口变更，请同步更新场景断言",
}

#: HTTP 响应对象里可以按名字直接寻址的顶层字段
_RESPONSE_ROOTS = {"body", "headers", "status_code", "text", "elapsed_ms", "request", "ok", "reason"}


def status_of(outputs: Mapping[str, Any] | None, exc: BaseException | None = None) -> int | None:
    """从原子输出里取出 HTTP 状态码。"""
    if isinstance(outputs, Mapping):
        status = outputs.get("status_code")
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


def _failure_status(atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None) -> int | None:
    """判定「这次失败的本质是不是某个 HTTP 状态」。

    只有**发起请求的原子**（声明了 ``network`` 能力）才用异常里带的状态码推断失败性质；
    像 ``assert_status`` 这种断言原子失败时，``exc.actual`` 只是被比较的值，
    其本质是「结果与预期不符」，不能当成服务端/凭证问题。
    """
    status = status_of(outputs, exc)
    if status is not None:
        return status
    capabilities = set(getattr(atom, "capabilities", ()) or ())
    if "network" not in capabilities:
        return None
    actual = getattr(exc, "actual", None)
    if isinstance(actual, int) and not isinstance(actual, bool):
        return actual
    return None


def classify_status(status_code: int) -> tuple[FailureClass, str]:
    """HTTP 状态码 → （中立类别, HTTP 领域标签）。"""
    if status_code in _STATUS_MAP:
        return _STATUS_MAP[status_code]
    if status_code >= 500:
        return FailureClass.TRANSIENT, "server_error"
    if status_code >= 400:
        return FailureClass.INVALID_INPUT, "client_error"
    return FailureClass.EXPECTATION, "unexpected_status"


def request_target(url: Any) -> str:
    """请求地址 → 接口标识（路径模板），用于把经验精确绑定到接口。

    - ``${env.BASE_URL}/api/flaky`` → ``/api/flaky``
    - ``http://127.0.0.1:8899/api/orders/${id}/pay`` → ``/api/orders/${id}/pay``
    """
    if not isinstance(url, str) or not url:
        return ""
    text = url.strip()
    if text.startswith("${"):
        closing = text.find("}")
        if closing >= 0:
            text = text[closing + 1 :]
    parsed = urlsplit(text)
    if not parsed.path and (parsed.scheme or parsed.netloc):
        return "/"
    return parsed.path or text


def find_auth_step(case: Molecule, *, before: str | None = None) -> Step | None:
    """找出用例里的认证步骤（tag=auth 优先，其次按命名启发式）。**HTTP 领域专属启发式。**"""
    candidates: list[Step] = []
    for step in case.steps:
        if before is not None and step.id == before:
            break
        if "auth" in step.tags:
            candidates.append(step)
            continue
        lowered = f"{step.id} {step.name} {step.use}".lower()
        if any(hint in lowered for hint in _AUTH_HINTS):
            candidates.append(step)
    if not candidates:
        return None
    tagged = [step for step in candidates if "auth" in step.tags]
    return (tagged or candidates)[-1]


# --------------------------------------------------------------------------- #
# 领域语义
# --------------------------------------------------------------------------- #
class ApiTestingSemantics(GenericSemantics):
    """API 测试领域语义。内核只通过基类接口调用它。"""

    name = "api_testing"

    # -- 失败语义 -------------------------------------------------------- #
    def failure_class(self, atom, outputs, exc) -> FailureClass:
        status = _failure_status(atom, outputs, exc)
        if status is not None:
            return classify_status(status)[0]
        return super().failure_class(atom, outputs, exc)

    def failure_kind(self, atom, outputs, exc) -> str:
        status = _failure_status(atom, outputs, exc)
        if status is not None:
            return classify_status(status)[1]
        return super().failure_kind(atom, outputs, exc)

    def is_retryable(self, atom, outputs, *, kind: str, failure_class: FailureClass) -> bool:
        if kind in _RETRYABLE_KINDS or failure_class is FailureClass.TRANSIENT:
            return True
        return super().is_retryable(atom, outputs, kind=kind, failure_class=failure_class)

    def result_status(self, atom, outputs) -> tuple[str, FailureClass] | None:
        """HTTP 特有：5xx / 429 是「成功返回但结果不可接受」，值得退避重试。"""
        status = status_of(outputs, None)
        if status is None:
            return None
        failure_class, kind = classify_status(status)
        if status >= 500 or status == 429:
            return kind, failure_class
        return None

    # -- 标识 / 恢复 / 建议 ---------------------------------------------- #
    def target(self, atom, inputs) -> str:
        if not isinstance(inputs, Mapping):
            return ""
        return request_target(inputs.get("url"))

    def recoveries(self, atom, *, kind, failure_class, case=None, step=None, inputs=None) -> list[Recovery]:
        out: list[Recovery] = []
        if failure_class is FailureClass.CREDENTIALS and case is not None and step is not None:
            auth_step = find_auth_step(case, before=step.id)
            if auth_step is not None:
                out.append(
                    Recovery(
                        action="reauth",
                        reason=f"凭证失效 → 重新执行认证步骤 {auth_step.id} 后重试",
                        auth_step_id=auth_step.id,
                        advice=_HTTP_ADVICE["auth"],
                        detail={"auth_step": auth_step.id, "strategy": "reauth-then-retry"},
                    )
                )
        if failure_class is FailureClass.TRANSIENT:
            out.append(
                Recovery(
                    action="retry",
                    reason=f"{kind} 属于瞬态故障 → 退避重试",
                    delay_ms=200,
                    retry_hint={"attempts": 2, "delay_ms": 200, "backoff": 2.0, "on": ["any"]},
                    advice=_HTTP_ADVICE.get(kind, ""),
                    detail={"strategy": "backoff-retry"},
                )
            )
        return out

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        if kind in _HTTP_ADVICE:
            return _HTTP_ADVICE[kind]
        return super().advice(kind=kind, failure_class=failure_class)

    # -- 步骤语法糖与默认值 ---------------------------------------------- #
    def assert_expanders(self) -> dict[str, Any]:
        table = dict(super().assert_expanders())
        table.update(
            {
                "status": self._expand_status,
                "latency_max_ms": self._expand_latency,
                "header": self._expand_header,
                "body_contains": self._expand_body,
            }
        )
        return table

    def assert_order(self) -> list[str]:
        # 状态码先行：报告里最要紧的信息排在最前
        return ["status", "json", "schema", "latency_max_ms", "header", "body_contains"]

    def _expand_status(self, step_id: str, status: Any, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        with_block: dict[str, Any] = {"expected": status}
        message = (step.get("assert") or {}).get("message")
        if message:
            with_block["message"] = message
        return [self._assert_step(step_id, "assert_status", "assert_status", with_block, f"{step_id} 状态码断言")]

    def _expand_latency(self, step_id: str, max_ms: Any, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [self._assert_step(step_id, "assert_latency", "assert_latency", {"max_ms": max_ms}, f"{step_id} 耗时断言")]

    def _expand_header(self, step_id: str, header: Any, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        if isinstance(header, str):
            header = {"name": header}
        if not isinstance(header, Mapping) or "name" not in header:
            raise SpecError(
                f"步骤 {step_id} 的 assert.header 需要 name 字段，或写成 {{name: ..., contains: ...}}"
            )
        return [self._assert_step(step_id, "assert_header", "assert_header", dict(header), f"{step_id} 响应头断言")]

    def _expand_body(self, step_id: str, text: Any, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            self._assert_step(step_id, "assert_body_contains", "assert_body_contains", {"text": text}, f"{step_id} 原文包含断言")
        ]

    def step_defaults(self, step: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """API 领域适用的套件级默认值：``headers`` / ``timeout``（只对 http_request 生效）。

        加载器负责「步骤自己的写法优先」的合并，领域包只声明**哪些默认值适用于本领域**。
        """
        defaults = defaults or {}
        if (step.get("use") or step.get("atom")) != "http_request":
            return {}
        out: dict[str, Any] = {}
        if defaults.get("headers"):
            out["headers"] = dict(defaults["headers"])
        if defaults.get("timeout") is not None:
            out["timeout"] = defaults["timeout"]
        return out

    def known_input_roots(self, atom) -> set[str]:
        if getattr(atom, "name", "") in {"json_get", "header_get"}:
            return set(_RESPONSE_ROOTS)
        return set()

    def planning_hints(self) -> tuple[str, ...]:
        """API 领域的规划惯例（注入 LLM 提示词，内核不需要知道这些）。"""
        return (
            "接口请求用 http_request；非预期状态码直接用 with.expect_status 断言，"
            "正常工作流的状态码断言用 assert: {status: ...}",
            "如果首个用例需要认证：先写一个登录步骤（tags 含 auth）并 capture token，"
            "后续请求用 headers.Authorization = \"Bearer ${token}\"",
            "响应字段断言用 assert_json（path/equals/contains/gt/length…），结构断言用 assert_schema",
            "接口返回是业务数据，不是异常：4xx/5xx 会原样返回，由断言决定成败",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": "api",
            "status_map": {str(code): kind for code, (_cls, kind) in sorted(_STATUS_MAP.items())},
            "retryable_kinds": sorted(_RETRYABLE_KINDS),
            "target": "URL 路径模板",
        }


# --------------------------------------------------------------------------- #
# 契约元数据：HTTP 方法决定副作用强度
# --------------------------------------------------------------------------- #
def _http_effects(inputs: Mapping[str, Any]) -> tuple[str, ...]:
    method = str((inputs or {}).get("method", "GET")).upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return ("read",)
    if method in {"PUT", "PATCH"}:
        return ("write",)
    if method == "DELETE":
        return ("destructive",)
    return ("mutate",)  # POST 等：非幂等


def build_pack() -> Any:
    """构建 ``api_testing`` 包。"""
    from ..core.pack import AtomPack
    from .api_testing_templates import api_intent_templates

    atoms: list[Atom] = [
        enrich(
            http.http_request,
            domain="api",
            capabilities=("network",),
            effects=("read",),
            effect_resolver=_http_effects,
        ),
        enrich(extract.header_get, domain="api", effects=("pure",)),
        enrich(assertions.assert_status, domain="api", effects=("pure",)),
        enrich(assertions.assert_latency, domain="api", effects=("pure",)),
        enrich(assertions.assert_header, domain="api", effects=("pure",)),
        enrich(assertions.assert_body_contains, domain="api", effects=("pure",)),
    ]
    return AtomPack(
        name="api_testing",
        domain="api",
        version="0.1.0",
        description="API 自动化测试领域包：HTTP 请求、状态码/响应断言、认证自愈语义",
        semantics=ApiTestingSemantics(),
        atoms=atoms,
        requires=("network",),
        tags=("builtin", "http", "testing"),
        license="Apache-2.0",
        intent_templates=api_intent_templates(),
    )
