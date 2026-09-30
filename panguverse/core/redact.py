"""密钥打码：报告、日志、HTML 里只出现明文的地方都要过这里。

策略是「按值打码」——只要某个字符串里出现了已知密钥明文，就替换成 ``***``。
这样无论密钥被写进 header、URL query、body 还是断言消息，都不会泄漏。
"""

from __future__ import annotations

from typing import Any

MASK = "***"
_REDACT_KEYS = {
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "cookie",
    "set-cookie",
    "x-api-key",
    "private_key",
}


def mask_obj(obj: Any, secret_values: list[str] | None = None) -> Any:
    """递归打码：敏感键名一律遮蔽，已知明文按值替换。"""
    return _mask(obj, [v for v in (secret_values or []) if v], None)


def _mask(obj: Any, values: list[str], key: str | None) -> Any:
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            k_str = str(k)
            if k_str.lower() in _REDACT_KEYS and isinstance(v, (str, int, float)):
                out[k] = MASK
            elif k_str.lower() == "headers" and isinstance(v, dict):
                out[k] = _mask(v, values, "headers")
            else:
                out[k] = _mask(v, values, k_str)
        return out
    if isinstance(obj, (list, tuple)):
        return [_mask(v, values, key) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj, values)
    return obj


def mask_text(text: str, secret_values: list[str]) -> str:
    for value in secret_values:
        if value and value in text:
            text = text.replace(value, MASK)
    return text
