"""LLM 客户端超时/重试契约：默认 120s、2 次指数退避、错误只包一层。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

from panguverse.axe import llm as llm_mod
from panguverse.axe.llm import LLMClient, LLMError


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


class _Opener:
    def __init__(self, steps: list[object]) -> None:
        self.steps = list(steps)

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _client(monkeypatch, steps: list[object], **kwargs) -> LLMClient:
    monkeypatch.setattr(llm_mod, "_DIRECT_OPENER", _Opener(steps))
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _seconds: None)
    return LLMClient(
        api_key="test-key",
        backend="openai",
        base_url="http://127.0.0.1:1",
        retry_backoff=0,
        **kwargs,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:1/v1/chat/completions",
        code,
        "boom",
        {},
        io.BytesIO(b""),
    )


def test_default_timeout_and_retries_with_env_overrides(monkeypatch) -> None:
    monkeypatch.delenv("PANGUVERSE_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("PANGUVERSE_LLM_RETRIES", raising=False)
    default = LLMClient(api_key="k")
    assert default.timeout == 120.0
    assert default.retries == 2

    monkeypatch.setenv("PANGUVERSE_LLM_TIMEOUT", "30")
    monkeypatch.setenv("PANGUVERSE_LLM_RETRIES", "0")
    overridden = LLMClient(api_key="k")
    assert overridden.timeout == 30.0
    assert overridden.retries == 0


def test_timeout_is_retried_then_succeeds(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        [
            TimeoutError("timed out"),
            _Response(b'{"choices":[{"message":{"content":"ok"}}]}'),
        ],
    )
    assert client.complete("system", "user") == "ok"
    assert client.last_error == ""


def test_http_500_is_retried_then_succeeds(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        [
            _http_error(500),
            _Response(b'{"choices":[{"message":{"content":"ok"}}]}'),
        ],
    )
    assert client.complete("system", "user") == "ok"


def test_http_429_retry_limit_reports_single_error(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        [_http_error(429), _http_error(429), _http_error(429)],
    )
    try:
        client.complete("system", "user")
    except LLMError as exc:
        assert "HTTP 429" in str(exc)
        assert str(exc).count("LLM 调用失败") == 0
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("429 连续失败应当抛 LLMError")


def test_non_retryable_error_fails_fast(monkeypatch) -> None:
    calls: list[int] = []

    def call_openai(system: str, user: str, temperature: float, **kwargs: object) -> str:
        calls.append(1)
        raise LLMError("鉴权失败", retryable=False)

    client = LLMClient(api_key="k", backend="openai", base_url="http://127.0.0.1:1")
    monkeypatch.setattr(client, "_call_openai", call_openai)
    try:
        client.complete("system", "user")
    except LLMError as exc:
        assert "鉴权失败" in str(exc)
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("不可重试错误应当直接抛出")
    assert len(calls) == 1


class _CaptureOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[tuple[dict[str, object], float]] = []

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        self.requests.append((json.loads(request.data or b"{}"), timeout))
        return _Response(self.body)


def test_null_body_is_retried_then_succeeds(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        [
            _Response(b"null"),
            _Response(b'{"choices":[{"message":{"content":"ok"}}]}'),
        ],
    )
    assert client.complete("system", "user") == "ok"


def test_empty_choices_are_retried(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        [
            _Response(b'{"choices":[]}'),
            _Response(b'{"choices":[{"message":{"content":"ok"}}]}'),
        ],
    )
    assert client.complete("system", "user") == "ok"


def test_openai_payload_has_output_limits_and_effort(monkeypatch) -> None:
    capture = _CaptureOpener(b'{"choices":[{"message":{"content":"ok"}}]}')
    monkeypatch.setattr(llm_mod, "_DIRECT_OPENER", capture)
    client = LLMClient(
        api_key="test-key",
        backend="openai",
        base_url="http://127.0.0.1:1",
        retry_backoff=0,
    )
    assert (
        client.complete(
            "system",
            "user",
            json_mode=True,
            max_tokens=64,
            timeout=7,
            reasoning_effort="none",
        )
        == "ok"
    )
    payload, timeout = capture.requests[0]
    assert payload["max_tokens"] == 64
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["reasoning_effort"] == "none"
    assert timeout == 7

def test_http_error_request_id_is_preserved(monkeypatch) -> None:
    body = io.BytesIO(b'{"error":{"request_id":"rid-123"}}')
    errors = [
        urllib.error.HTTPError(
            "http://127.0.0.1:1/v1/chat/completions", 500, "boom", {}, io.BytesIO(body.getvalue())
        )
        for _ in range(3)
    ]
    client = _client(monkeypatch, errors)
    try:
        client.complete("system", "user")
    except LLMError as exc:
        assert exc.request_id == "rid-123"
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("连续 500 应当抛 LLMError")
