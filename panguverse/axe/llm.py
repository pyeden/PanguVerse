"""LLM 客户端：AI 编排的「可选增强」，不是运行前提。

设计原则（对齐 README 的「AI 是盘古斧」但工程上必须可离线）：
- 没有密钥/网络时，规则兜底规划器照样能把闭环跑通；
- 有密钥时，用结构化 JSON 约束让 LLM 产出**可直接执行**的计划；
- 只依赖标准库（urllib），避免给项目加硬依赖。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any

from ..http.client import is_loopback_url

#: 回环目标必须直连（macOS 系统代理会把 127.0.0.1 也劫持走，本机 ollama 会 502）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 真实模型偶尔在 60s 附近抖动；默认放宽到 120s，并允许环境变量覆盖。
DEFAULT_TIMEOUT = 120.0
DEFAULT_RETRIES = 2
#: 决策类调用必须显式限输出；不传 max_tokens 时推理模型会按默认高 effort 长跑。
DEFAULT_MAX_TOKENS = 2048
#: 5xx/空响应重试退避基数（秒）；实际间隔按 2^n 递增并加抖动。
DEFAULT_RETRY_BACKOFF = 2.0

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、返回格式）。

    ``retryable`` 只表示传输层值得重试（超时/URLError/429/5xx）；
    鉴权失败、返回格式错误不重试。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.request_id = request_id


class LLMClient:
    """OpenAI 兼容 / Anthropic 两种后端的最小封装。"""

    def __init__(
        self,
        *,
        backend: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.backend = (backend or os.getenv("PANGUVERSE_AI_BACKEND") or "").strip().lower()
        self.api_key = api_key or os.getenv("PANGUVERSE_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        self.base_url = (
            base_url
            or os.getenv("PANGUVERSE_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or ("https://api.anthropic.com" if self.backend == "anthropic" else "https://api.openai.com")
        ).rstrip("/")
        self.model = model or os.getenv("PANGUVERSE_MODEL") or (
            "claude-3-5-sonnet-latest" if self.backend == "anthropic" else "gpt-4o-mini"
        )
        if not self.backend:
            self.backend = "anthropic" if "anthropic" in self.base_url else "openai"
        self.timeout = (
            float(timeout)
            if timeout is not None
            else _env_float("PANGUVERSE_LLM_TIMEOUT", DEFAULT_TIMEOUT)
        )
        self.retries = max(
            0,
            int(retries)
            if retries is not None
            else _env_int("PANGUVERSE_LLM_RETRIES", DEFAULT_RETRIES),
        )
        self.retry_backoff = max(0.0, float(retry_backoff))
        resolved_max_tokens = (
            max_tokens
            if max_tokens is not None
            else _env_int("PANGUVERSE_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS)
        )
        self.max_tokens = max(1, int(resolved_max_tokens))
        self.reasoning_effort = (
            (reasoning_effort or os.getenv("PANGUVERSE_LLM_REASONING_EFFORT") or "").strip()
            or None
        )
        self.calls = 0
        self.last_error = ""

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend,
            "model": self.model,
            "base_url": self.base_url,
            "reason": "" if self.available else "未配置 PANGUVERSE_API_KEY / OPENAI_API_KEY",
            "timeout": self.timeout,
            "retries": self.retries,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ #
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: int | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        observer: Any = None,
    ) -> str:
        if not self.available:
            raise LLMError("未配置 LLM 密钥（PANGUVERSE_API_KEY / OPENAI_API_KEY）")
        self.calls += 1
        attempts = self.retries + 1
        last_error: LLMError | None = None
        attempt_timeout = self.timeout if timeout is None else max(0.1, float(timeout))
        attempt_max_tokens = self.max_tokens if max_tokens is None else max(1, int(max_tokens))
        attempt_effort = reasoning_effort if reasoning_effort is not None else self.reasoning_effort
        prompt_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                if self.backend == "anthropic":
                    result = self._call_anthropic(
                        system,
                        user,
                        temperature,
                        json_mode=json_mode,
                        max_tokens=attempt_max_tokens,
                        timeout=attempt_timeout,
                    )
                else:
                    result = self._call_openai(
                        system,
                        user,
                        temperature,
                        json_mode=json_mode,
                        max_tokens=attempt_max_tokens,
                        timeout=attempt_timeout,
                        reasoning_effort=attempt_effort,
                    )
                self.last_error = ""
                _notify(
                    observer,
                    {
                        "attempt": attempt + 1,
                        "attempts": attempts,
                        "ok": True,
                        "status": None,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "prompt_bytes": prompt_bytes,
                        "model": self.model,
                        "max_tokens": attempt_max_tokens,
                        "reasoning_effort": attempt_effort,
                        "request_id": None,
                    },
                )
                return result
            except LLMError as exc:
                last_error = exc
                self.last_error = str(exc)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.warning(
                    "LLM attempt %d/%d failed in %dms: %s",
                    attempt + 1,
                    attempts,
                    elapsed_ms,
                    str(exc)[:300],
                )
                _notify(
                    observer,
                    {
                        "attempt": attempt + 1,
                        "attempts": attempts,
                        "ok": False,
                        "status": exc.status,
                        "elapsed_ms": elapsed_ms,
                        "prompt_bytes": prompt_bytes,
                        "model": self.model,
                        "max_tokens": attempt_max_tokens,
                        "reasoning_effort": attempt_effort,
                        "request_id": exc.request_id,
                        "error": str(exc)[:600],
                    },
                )
                if not exc.retryable or attempt + 1 >= attempts:
                    # 错误只在传输层包装一次；这里不再加 "LLM 调用失败" 前缀。
                    raise
                delay = self.retry_backoff * (2**attempt)
                if delay > 0:
                    time.sleep(delay + random.uniform(0, 0.25))
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                _notify(
                    observer,
                    {
                        "attempt": attempt + 1,
                        "attempts": attempts,
                        "ok": False,
                        "status": None,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "prompt_bytes": prompt_bytes,
                        "model": self.model,
                        "max_tokens": attempt_max_tokens,
                        "reasoning_effort": attempt_effort,
                        "request_id": None,
                        "error": self.last_error[:600],
                    },
                )
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        raise last_error or LLMError("未知错误")

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        timeout: float | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """要求返回 JSON 对象；解析失败会抛出 LLMError。

        结构化 JSON 不需要思维链：默认 ``reasoning_effort=none``，否则推理 token
        会吃掉 max_tokens，导致 content 为空（finish_reason=length）。
        """
        json_max_tokens = _env_int("PANGUVERSE_LLM_JSON_MAX_TOKENS", 4096)
        json_effort = (os.getenv("PANGUVERSE_LLM_JSON_REASONING_EFFORT") or "none").strip() or None
        text = self.complete(
            system,
            user,
            temperature=temperature,
            json_mode=True,
            max_tokens=json_max_tokens,
            timeout=timeout,
            reasoning_effort=json_effort,
        )
        if not text.strip():
            raise LLMError("LLM 返回空内容（可能被 max_tokens 截断）")
        data = _extract_json(text)
        if not isinstance(data, dict):
            raise LLMError(f"LLM 未返回 JSON 对象: {text[:200]}")
        return data

    # ------------------------------------------------------------------ #
    def _call_openai(
        self,
        system: str,
        user: str,
        temperature: float,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        data = self._send(request, timeout=timeout)
        try:
            choices = data["choices"]
            if not isinstance(choices, list) or not choices:
                raise KeyError("choices")
            choice = choices[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            finish = choice.get("finish_reason")
            if json_mode and finish == "length":
                raise LLMError(
                    "LLM JSON 输出被截断（finish_reason=length）：请提高 max_tokens 或关闭 reasoning",
                    retryable=False,
                )
            if not str(content).strip() and finish == "length":
                raise LLMError(
                    "LLM 输出被截断（finish_reason=length）：max_tokens 或 reasoning effort 限制过紧",
                    retryable=False,
                )
            return content
        except LLMError:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"LLM 响应无 choices: {str(data)[:200]}",
                retryable=True,
                request_id=_request_id(data),
            ) from exc

    def _call_anthropic(
        self,
        system: str,
        user: str,
        temperature: float,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "max_tokens": int(max_tokens or self.max_tokens),
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        data = self._send(request, timeout=timeout)
        try:
            blocks = data.get("content") or []
            return "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
        except (AttributeError, TypeError) as exc:
            raise LLMError(f"无法解析 Anthropic 响应: {str(data)[:200]}") from exc

    def _send(
        self,
        request: urllib.request.Request,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # 与 panguverse/http/client.py 同一套规则：回环直连，其余仍尊重系统/环境代理
        opener = _DIRECT_OPENER if is_loopback_url(request.full_url) else None
        request_timeout = self.timeout if timeout is None else max(0.1, float(timeout))
        try:
            with (
                opener.open(request, timeout=request_timeout)
                if opener
                else urllib.request.urlopen(request, timeout=request_timeout)
            ) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            self.last_error = f"HTTP {exc.code}: {detail[:300]}"
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise LLMError(
                f"LLM 接口返回 HTTP {exc.code}: {detail[:300]}",
                retryable=retryable,
                status=exc.code,
                request_id=_request_id_from_text(detail),
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            self.last_error = f"timeout/url error: {reason}"
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
                message = f"LLM 接口超时（{request_timeout:.0f}s）"
            else:
                message = f"LLM 接口不可达: {reason}"
            raise LLMError(message, retryable=True) from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            self.last_error = f"non-json: {body[:200]}"
            raise LLMError(f"LLM 返回非 JSON: {body[:200]}", retryable=True) from exc
        if not isinstance(data, dict):
            self.last_error = f"non-object: {body[:200]}"
            raise LLMError(f"LLM 返回非对象响应: {body[:200]}", retryable=True)
        return data


def _extract_json(text: str) -> Any:
    """从可能带 markdown 围栏的文本里提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start_positions = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
    if not start_positions:
        raise LLMError(f"响应中没有 JSON: {text[:200]}")
    start = min(start_positions)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        raise LLMError(f"响应中的 JSON 不完整: {text[:200]}")
    try:
        return json.loads(text[start : end + 1])
    except ValueError as exc:
        raise LLMError(f"JSON 解析失败: {exc}") from exc



def _notify(observer: Any, event: dict[str, Any]) -> None:
    """Best-effort observer; trace/log failures must never break the LLM call."""
    if observer is None:
        return
    try:
        observer(event)
    except Exception:  # pragma: no cover - 观测失败不影响主流程
        logger.debug("LLM observer failed", exc_info=True)


def _request_id(data: Any) -> str | None:
    if isinstance(data, dict):
        rid = data.get("request_id")
        if not rid and isinstance(data.get("error"), dict):
            rid = data["error"].get("request_id")
        return str(rid) if rid else None
    return None


def _request_id_from_text(text: str) -> str | None:
    try:
        return _request_id(json.loads(text))
    except (TypeError, ValueError):
        return None
