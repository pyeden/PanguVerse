"""HTTP 原子：把「发请求」变成最小操作单元。

设计取舍：``http_request`` **不把 4xx/5xx 当异常**。
在 API 测试里，``404`` / ``401`` 往往正是要被断言的预期结果；
异常只保留给真正的执行故障（连不上、超时）。
"""

from __future__ import annotations

import time
from typing import Any

from ..core.atom import atom
from ..core.context import RunContext
from ..core.errors import AssertionFailed
from ..http.client import DEFAULT_TIMEOUT, HttpClient, HttpRequest


def _client(ctx: RunContext, timeout: float | None) -> HttpClient:
    client = ctx.meta.get("_http_client")
    if client is None:
        env_timeout = str(ctx.env.get("PANGUVERSE_HTTP_TIMEOUT", DEFAULT_TIMEOUT))
        try:
            default = float(env_timeout)
        except ValueError:
            default = DEFAULT_TIMEOUT
        client = HttpClient(timeout=timeout or default)
        ctx.meta["_http_client"] = client
    return client


def _join_url(url: str, base_url: str | None, ctx: RunContext) -> str:
    if not url:
        raise ValueError("http_request 需要 url")
    if url.startswith(("http://", "https://")):
        return url
    base = base_url or ctx.env.get("BASE_URL") or ctx.env.get("base_url")
    if not base:
        raise ValueError(
            f"相对地址 {url!r} 需要 BASE_URL：请在场景 env 里声明，或传入 base_url/绝对 URL"
        )
    return str(base).rstrip("/") + (url if url.startswith("/") else "/" + url)


@atom(
    "http_request",
    description="发起一次 HTTP 请求，返回状态码/响应头/响应体；4xx/5xx 不抛异常",
    category="http",
    tags=("http", "core"),
    aliases=("request", "http"),
    inputs={
        "url": "string（绝对地址，或以 / 开头并用 BASE_URL 拼接）",
        "method": "string=GET",
        "headers": "object",
        "params": "object（query 参数）",
        "json": "any（JSON 请求体）",
        "data": "string|bytes（原始请求体）",
        "form": "object（表单请求体）",
        "timeout": "number（秒）",
        "base_url": "string",
        "expect_status": "int|int[]|'2xx'（可选，等价于内联断言）",
        "allow_redirects": "bool=true",
        "verify": "bool=true",
    },
    outputs={
        "status_code": "int",
        "ok": "bool",
        "reason": "string",
        "headers": "object",
        "body": "any（JSON 自动解析）",
        "text": "string",
        "elapsed_ms": "float",
        "url": "string",
        "request": "object",
    },
    required=("url",),
)
def http_request(
    ctx: RunContext,
    url: str,
    method: str = "GET",
    headers: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    data: Any = None,
    form: dict[str, Any] | None = None,
    timeout: float | None = None,
    base_url: str | None = None,
    expect_status: Any = None,
    allow_redirects: bool = True,
    verify: bool = True,
) -> dict[str, Any]:
    request = HttpRequest(
        method=str(method or "GET").upper(),
        url=_join_url(str(url), base_url, ctx),
        headers={str(k): str(v) for k, v in (headers or {}).items()},
        params=params,
        json=json,
        data=data,
        form=form,
        timeout=float(timeout) if timeout is not None else None,
        allow_redirects=bool(allow_redirects),
        verify=bool(verify),
    )
    client = _client(ctx, request.timeout)
    response = client.send(request)
    payload: dict[str, Any] = {
        "status_code": response.status_code,
        "ok": response.ok,
        "reason": response.reason,
        "headers": response.headers,
        "body": response.body,
        "text": response.text,
        "content_type": response.content_type,
        "elapsed_ms": round(response.elapsed_ms, 2),
        "url": response.url,
        "size_bytes": response.size_bytes,
        "method": request.method,
        # 供断言/提取原子直接引用，省去每步重复写 source
        "request": request.redacted(),
    }
    ctx.record_http({"at": time.time(), "request": request.redacted(), "response": response.to_dict()})
    ctx.vars["last_response"] = payload
    ctx.emit(
        "http",
        method=request.method,
        url=request.full_url(),
        status=response.status_code,
        elapsed_ms=payload["elapsed_ms"],
    )
    if expect_status is not None:
        _check_expect_status(expect_status, response.status_code, request.full_url(), ctx)
    return payload


def _check_expect_status(expected: Any, actual: int, url: str, ctx: RunContext) -> None:
    from .assertions import status_matches

    if not status_matches(expected, actual):
        raise AssertionFailed(
            f"期望状态码 {expected}，实际 {actual}（{url}）",
            expected=expected,
            actual=actual,
            path="response.status_code",
        )
