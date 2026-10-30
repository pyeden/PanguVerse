"""HTTP 传输层：优先 ``requests``，缺失时回退标准库 ``urllib``（零依赖可跑）。

这一层只做「发出去、收回来」，**不对 4xx/5xx 抛异常**——
在 API 测试里，404 是一种合法的被测结果，应该交给断言原子判断。
只有连接失败、超时、TLS 失败才是 :class:`TransportError`。
"""

from __future__ import annotations

import json as _json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.errors import NetworkError, RequestTimeout

try:  # pragma: no cover - 取决于环境
    import requests as _requests
except Exception:  # noqa: BLE001 - 任何导入失败都退化为 urllib
    _requests = None

try:  # pragma: no cover
    import urllib.error
    import urllib.request
except Exception:  # noqa: BLE001
    urllib = None  # type: ignore[assignment]


DEFAULT_TIMEOUT = 10.0


@dataclass
class HttpRequest:
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None
    json: Any = None
    data: Any = None
    form: dict[str, Any] | None = None
    timeout: float | None = None
    allow_redirects: bool = True
    verify: bool = True
    expect_status: int | list[int] | None = None

    def full_url(self) -> str:
        if not self.params:
            return self.url
        parts = urlsplit(self.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key, value in self.params.items():
            if isinstance(value, (list, tuple)):
                query[key] = ",".join(str(v) for v in value)
            else:
                query[key] = value
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)
        )

    def redacted(self) -> dict[str, Any]:
        """给报告的请求记录（headers 由报告层统一打码）。"""
        out: dict[str, Any] = {
            "method": self.method.upper(),
            "url": self.full_url(),
            "headers": dict(self.headers),
        }
        if self.json is not None:
            out["json"] = self.json
        if self.data is not None:
            out["data"] = self.data if isinstance(self.data, str) else repr(self.data)
        if self.form is not None:
            out["form"] = self.form
        if self.timeout:
            out["timeout"] = self.timeout
        return out


@dataclass
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    text: str
    url: str
    elapsed_ms: float
    body: Any = None
    content_type: str = ""
    reason: str = ""
    size_bytes: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self, default: Any = None) -> Any:
        if self.body is not None:
            return self.body
        if not self.text:
            return default
        try:
            return _json.loads(self.text)
        except (ValueError, TypeError):
            return default

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "ok": self.ok,
            "reason": self.reason,
            "headers": self.headers,
            "content_type": self.content_type,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "size_bytes": self.size_bytes,
            "url": self.url,
            "body": self.body if self.body is not None else _truncate(self.text),
            "text": _truncate(self.text) if self.body is None else None,
        }


class HttpClient:
    """会话级 HTTP 客户端：保持 cookie，可跨步骤复用连接。"""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, backend: str | None = None) -> None:
        self.timeout = timeout
        if backend is None:
            backend = "requests" if _requests is not None else "urllib"
        if backend == "requests" and _requests is None:
            backend = "urllib"
        self.backend = backend
        self._session = _requests.Session() if self.backend == "requests" else None
        self._opener = None
        self._direct_opener = None
        if self.backend == "urllib":
            import http.cookiejar
            import urllib.request as _urlreq

            jar = http.cookiejar.CookieJar()
            # 默认 opener 尊重系统/环境代理；direct opener 用空 ProxyHandler 彻底关闭代理，
            # 专供回环地址（127.0.0.1 / localhost / ::1）——macOS 系统代理会把本机请求打到 502。
            self._opener = _urlreq.build_opener(_urlreq.HTTPCookieProcessor(jar))
            self._direct_opener = _urlreq.build_opener(
                _urlreq.ProxyHandler({}), _urlreq.HTTPCookieProcessor(jar)
            )

    # ------------------------------------------------------------------ #
    def send(self, request: HttpRequest) -> HttpResponse:
        timeout = float(request.timeout or self.timeout)
        url = request.full_url()
        if self.backend == "requests":
            return self._send_requests(request, url, timeout)
        return self._send_urllib(request, url, timeout)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    # ------------------------------------------------------------------ #
    def _send_requests(self, request: HttpRequest, url: str, timeout: float) -> HttpResponse:
        import requests

        kwargs: dict[str, Any] = {
            "method": request.method.upper(),
            "url": url,
            "headers": dict(request.headers),
            "timeout": timeout,
            "allow_redirects": request.allow_redirects,
            "verify": request.verify,
        }
        if is_loopback_url(url):
            # 逐请求关闭代理（显式 None 覆盖 env 代理）：不改 session.trust_env，
            # 因为那是会话级状态，会顺带影响同一会话里后续的非回环请求。
            kwargs["proxies"] = {"http": None, "https": None}
        if request.json is not None:
            kwargs["json"] = request.json
        elif request.form is not None:
            kwargs["data"] = request.form
        elif request.data is not None:
            kwargs["data"] = request.data
        started = time.perf_counter()
        try:
            resp = self._session.request(**kwargs)
        except requests.exceptions.Timeout as exc:
            raise RequestTimeout(f"请求超时(>{timeout}s): {request.method.upper()} {url}") from exc
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"请求失败: {request.method.upper()} {url} -> {exc}") from exc
        elapsed = (time.perf_counter() - started) * 1000
        content = resp.content or b""
        return _build_response(
            status=resp.status_code,
            headers=dict(resp.headers),
            content=content,
            url=str(resp.url),
            elapsed_ms=elapsed,
            reason=resp.reason or "",
            encoding=resp.encoding,
        )

    def _send_urllib(self, request: HttpRequest, url: str, timeout: float) -> HttpResponse:
        import socket
        import urllib.error
        import urllib.request as _urlreq

        body: bytes | None = None
        headers = dict(request.headers)
        if request.json is not None:
            body = _json.dumps(request.json, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif request.form is not None:
            body = urlencode(request.form, doseq=True).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif request.data is not None:
            body = request.data.encode("utf-8") if isinstance(request.data, str) else bytes(request.data)

        req = _urlreq.Request(url, data=body, method=request.method.upper())
        for key, value in headers.items():
            req.add_header(key, str(value))

        started = time.perf_counter()
        if is_loopback_url(url):
            opener = self._direct_opener or _urlreq.build_opener(_urlreq.ProxyHandler({}))
        else:
            opener = self._opener or _urlreq.build_opener()
        try:
            with opener.open(req, timeout=timeout) as resp:
                content = resp.read()
                elapsed = (time.perf_counter() - started) * 1000
                return _build_response(
                    status=getattr(resp, "status", resp.getcode()),
                    headers=dict(resp.headers),
                    content=content,
                    url=resp.geturl(),
                    elapsed_ms=elapsed,
                    reason=getattr(resp, "reason", "") or "",
                    encoding=resp.headers.get_content_charset(),
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是合法响应
            content = exc.read() if hasattr(exc, "read") else b""
            elapsed = (time.perf_counter() - started) * 1000
            return _build_response(
                status=exc.code,
                headers=dict(exc.headers or {}),
                content=content,
                url=url,
                elapsed_ms=elapsed,
                reason=str(exc.reason or ""),
                encoding=(exc.headers.get_content_charset() if exc.headers else None),
            )
        except TimeoutError as exc:
            raise RequestTimeout(f"请求超时(>{timeout}s): {request.method.upper()} {url}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise RequestTimeout(
                    f"请求超时(>{timeout}s): {request.method.upper()} {url}"
                ) from exc
            raise NetworkError(f"请求失败: {request.method.upper()} {url} -> {reason}") from exc
        except OSError as exc:
            raise NetworkError(f"请求失败: {request.method.upper()} {url} -> {exc}") from exc


def _build_response(
    *,
    status: int,
    headers: dict[str, str],
    content: bytes,
    url: str,
    elapsed_ms: float,
    reason: str,
    encoding: str | None,
) -> HttpResponse:
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = str(value)
            break
    charset = encoding or _charset_of(content_type) or "utf-8"
    try:
        text = content.decode(charset, errors="replace")
    except LookupError:  # pragma: no cover - 非法 charset
        text = content.decode("utf-8", errors="replace")
    body: Any = None
    stripped = text.strip()
    if stripped[:1] in {"{", "["} or "json" in content_type.lower():
        try:
            body = _json.loads(stripped)
        except (ValueError, TypeError):
            body = None
    return HttpResponse(
        status_code=int(status),
        headers=headers,
        text=text,
        body=body,
        url=url,
        elapsed_ms=elapsed_ms,
        content_type=content_type,
        reason=reason,
        size_bytes=len(content),
    )


def _charset_of(content_type: str) -> str | None:
    for chunk in content_type.split(";"):
        chunk = chunk.strip()
        if chunk.lower().startswith("charset="):
            return chunk.split("=", 1)[1].strip().strip('"')
    return None


def _truncate(text: str, limit: int = 4000) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"


def backend_name() -> str:
    """当前使用的传输后端（报告里会展示，便于排查环境差异）。"""
    return "requests" if _requests is not None else "urllib"


_LOOPBACK_HOSTS = {"localhost", "::1", "0.0.0.0", "127.0.0.1"}


def is_loopback_url(url: str) -> bool:
    """判断 URL 是否指向本机（回环）。

    覆盖 ``127.0.0.0/8``、``::1``、``localhost`` 与 ``*.localhost``、``0.0.0.0``。
    这类请求必须直连：macOS 的系统代理会把它们也劫持走，导致 502。
    """
    if not url:
        return False
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return True
    try:
        import ipaddress

        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or str(address) == "0.0.0.0"
