"""LLM 客户端：AI 编排的「可选增强」，不是运行前提。

设计原则（对齐 README 的「AI 是盘古斧」但工程上必须可离线）：
- 没有密钥/网络时，规则兜底规划器照样能把闭环跑通；
- 有密钥时，用结构化 JSON 约束让 LLM 产出**可直接执行**的计划；
- 只依赖标准库（urllib），避免给项目加硬依赖。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、返回格式）。"""


class LLMClient:
    """OpenAI 兼容 / Anthropic 两种后端的最小封装。"""

    def __init__(
        self,
        *,
        backend: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
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
        self.timeout = timeout
        self.max_tokens = max_tokens
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
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ #
    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> str:
        if not self.available:
            raise LLMError("未配置 LLM 密钥（PANGUVERSE_API_KEY / OPENAI_API_KEY）")
        self.calls += 1
        try:
            if self.backend == "anthropic":
                return self._call_anthropic(system, user, temperature)
            return self._call_openai(system, user, temperature)
        except LLMError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            raise LLMError(f"LLM 调用失败: {exc}") from exc

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """要求返回 JSON 对象；解析失败会抛出 LLMError。"""
        text = self.complete(system, user)
        data = _extract_json(text)
        if not isinstance(data, dict):
            raise LLMError(f"LLM 未返回 JSON 对象: {text[:200]}")
        return data

    # ------------------------------------------------------------------ #
    def _call_openai(self, system: str, user: str, temperature: float) -> str:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        data = self._send(request)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"无法解析 OpenAI 响应: {str(data)[:200]}") from exc

    def _call_anthropic(self, system: str, user: str, temperature: float) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
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
        data = self._send(request)
        try:
            blocks = data.get("content") or []
            return "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
        except (AttributeError, TypeError) as exc:
            raise LLMError(f"无法解析 Anthropic 响应: {str(data)[:200]}") from exc

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            self.last_error = f"HTTP {exc.code}: {detail[:300]}"
            raise LLMError(f"LLM 接口返回 HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            self.last_error = str(exc.reason)
            raise LLMError(f"LLM 接口不可达: {exc.reason}") from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise LLMError(f"LLM 返回非 JSON: {body[:200]}") from exc


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
