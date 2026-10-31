"""Web 登录认证：凭据解析、PBKDF2 校验、HMAC 会话、失败限流。

契约：``tasks/web-auth-spec.md`` v2.1。

- **默认由 ``serve`` 启用**；``--no-auth`` 才关闭（本地调试）。
- 凭据优先级：``PANGUVERSE_WEB_USER/PASSWORD`` → ``<ws>/.panguverse/web/auth.json``
  → 首次启动生成随机密码（只打印一次）。
- 会话 Cookie ``pv_session`` 用**独立密钥**签名（``session.key``），不复用任何 env 值；
  校验 HMAC、``exp``、``u``，任一项不过一律按未认证处理（不抛 500）。
- 密码/哈希绝不进日志、错误消息或任何 API 响应。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WEB_STATE_REL = Path(".panguverse") / "web"
AUTH_FILENAME = "auth.json"
SESSION_KEY_FILENAME = "session.key"
COOKIE_NAME = "pv_session"

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 32
SESSION_KEY_BYTES = 32
SESSION_TTL = 12 * 3600
RATE_WINDOW = 300.0
RATE_MAX_FAILURES = 5
DEFAULT_USER = "admin"
GEN_PASSWORD_LENGTH = 16
#: 去掉易混字符（0/O/o、1/l/I）后的字母数字表
_SAFE_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

DIR_MODE = 0o700
FILE_MODE = 0o600

Logger = Callable[[str], None]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def random_password(length: int = GEN_PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(_SAFE_ALPHABET) for _ in range(length))


def _ensure_private_dir(directory: Path) -> None:
    parent = directory.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            parent.chmod(DIR_MODE)
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(DIR_MODE)


def _write_private(path: Path, payload: bytes) -> None:
    """原子写 + ``0600``（与 last_env.json / auth.json 同策略）。"""
    _ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def auth_path(root: Path) -> Path:
    return Path(root) / WEB_STATE_REL / AUTH_FILENAME


def session_key_path(root: Path) -> Path:
    return Path(root) / WEB_STATE_REL / SESSION_KEY_FILENAME


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
@dataclass
class Credentials:
    user: str
    salt: bytes
    digest: bytes
    iterations: int = PBKDF2_ITERATIONS
    #: 首次启动生成时的一次性明文（只用于启动日志打印，绝不落盘/回显）
    generated_password: str = ""
    source: str = "file"

    def verify(self, password: Any) -> bool:
        if not isinstance(password, str) or not password:
            return False
        candidate = hash_password(password, self.salt, self.iterations)
        return hmac.compare_digest(candidate, self.digest)


def store_password(root: Path, user: str, password: str) -> Credentials:
    """把账号密码写成 PBKDF2 记录（``0600``）；供首启生成/运维重置/测试使用。"""
    salt = os.urandom(SALT_BYTES)
    digest = hash_password(password, salt)
    payload = {
        "algo": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": _b64encode(salt),
        "hash": _b64encode(digest),
        "user": user,
        "updated_at": time.time(),
    }
    _write_private(
        auth_path(root),
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return Credentials(user=user, salt=salt, digest=digest, source="file")


def _read_auth_file(root: Path) -> Credentials | None:
    path = auth_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        salt = _b64decode(str(raw["salt"]))
        digest = _b64decode(str(raw["hash"]))
        iterations = int(raw.get("iterations") or PBKDF2_ITERATIONS)
        user = str(raw.get("user") or DEFAULT_USER)
    except (KeyError, TypeError, ValueError):
        return None
    if not salt or not digest:
        return None
    return Credentials(user=user, salt=salt, digest=digest, iterations=iterations, source="file")


def resolve_credentials(root: Path, *, user_override: str | None = None) -> Credentials:
    """env > auth.json > 首启生成；``user_override``（``--auth-user``）只改账号名。"""
    env_user = (os.environ.get("PANGUVERSE_WEB_USER") or "").strip()
    env_password = os.environ.get("PANGUVERSE_WEB_PASSWORD") or ""
    user = (user_override or env_user).strip() or None

    if env_password:
        salt = os.urandom(SALT_BYTES)
        return Credentials(
            user=user or DEFAULT_USER,
            salt=salt,
            digest=hash_password(env_password, salt),
            source="env",
        )

    stored = _read_auth_file(root)
    if stored is not None:
        if user and user != stored.user:
            stored.user = user
        return stored

    password = random_password()
    generated = store_password(root, user or DEFAULT_USER, password)
    generated.generated_password = password
    generated.source = "generated"
    return generated


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def _rotate_session_key(root: Path) -> bytes:
    """每次进程启动都生成新密钥（v2.1.1，QA F2）。

    重启即令所有既有会话失效——比把登出黑名单落盘更简单，也不会出现
    「logout 后重启、旧 Cookie 复活」的窗口。
    """
    key = secrets.token_bytes(SESSION_KEY_BYTES)
    _write_private(session_key_path(root), key)
    return key


def _cookie_value(headers: dict[str, str], name: str) -> str:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "cookie":
            raw = value or ""
            break
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def build_cookie(value: str, *, secure: bool = False, max_age: int | None = SESSION_TTL) -> str:
    parts = [f"{COOKIE_NAME}={value}", "HttpOnly", "Path=/", "SameSite=Lax"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class AuthManager:
    """一次进程内的认证器：凭据、会话签名密钥、按 IP 的失败限流。"""

    def __init__(
        self,
        root: Path,
        *,
        username: str | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.root = Path(root)
        self.credentials = resolve_credentials(self.root, user_override=username)
        self._session_key = _rotate_session_key(self.root)
        self._logger = logger
        self._failures: dict[str, list[float]] = {}
        #: 已登出的会话 nonce → 过期时间（内存态；重启即全部失效，符合契约 §7）
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    @property
    def user(self) -> str:
        return self.credentials.user

    @property
    def generated_password(self) -> str:
        return self.credentials.generated_password

    def verify_credentials(self, username: Any, password: Any) -> bool:
        """账号与密码都走常量时间比较；任何异常输入都按失败处理。"""
        if not isinstance(username, str) or not isinstance(password, str):
            return False
        user_ok = hmac.compare_digest(username.strip(), self.user)
        password_ok = self.credentials.verify(password)
        return user_ok and password_ok

    # ------------------------------------------------------------------ #
    def _sign(self, body: bytes) -> bytes:
        return hmac.new(self._session_key, body, hashlib.sha256).digest()

    def issue_session(self, user: str | None = None, *, ttl: int = SESSION_TTL) -> str:
        payload = {
            "u": user or self.user,
            "exp": int(time.time()) + int(ttl),
            "nonce": secrets.token_urlsafe(9),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        return f"{_b64encode(body)}.{_b64encode(self._sign(body))}"

    def _verified_payload(self, token: str | None) -> dict[str, Any] | None:
        if not isinstance(token, str) or "." not in token:
            return None
        body_b64, _, sig_b64 = token.partition(".")
        try:
            body = _b64decode(body_b64)
            signature = _b64decode(sig_b64)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            expires = int(payload.get("exp"))
        except (TypeError, ValueError):
            return None
        if expires <= int(time.time()):
            return None
        user = payload.get("u")
        if not isinstance(user, str) or not hmac.compare_digest(user, self.user):
            return None
        return payload

    def verify_session(self, token: str | None) -> str | None:
        payload = self._verified_payload(token)
        if payload is None:
            return None
        nonce = payload.get("nonce")
        if isinstance(nonce, str):
            with self._lock:
                if nonce in self._revoked:
                    return None
        return payload["u"]

    def user_from_headers(self, headers: dict[str, str]) -> str | None:
        return self.verify_session(_cookie_value(headers, COOKIE_NAME))

    def revoke_session(self, token: str | None) -> None:
        """登出：把该会话的 nonce 记入内存黑名单直到它自然过期。"""
        payload = self._verified_payload(token)
        if payload is None:
            return
        nonce = payload.get("nonce")
        expires = payload.get("exp")
        if not isinstance(nonce, str) or not isinstance(expires, (int, float)):
            return
        now = time.time()
        with self._lock:
            self._revoked = {key: exp for key, exp in self._revoked.items() if exp > now}
            self._revoked[nonce] = float(expires)

    def revoke_headers(self, headers: dict[str, str]) -> None:
        self.revoke_session(_cookie_value(headers, COOKIE_NAME))

    # ------------------------------------------------------------------ #
    def rate_limit_retry(self, ip: str) -> int:
        """返回需要等待的秒数；0 表示放行。"""
        now = time.time()
        key = ip or "unknown"
        with self._lock:
            recent = [ts for ts in self._failures.get(key, []) if now - ts < RATE_WINDOW]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)
            if len(recent) >= RATE_MAX_FAILURES:
                return max(1, int(RATE_WINDOW - (now - recent[0])) + 1)
        return 0

    def record_failure(self, ip: str) -> None:
        now = time.time()
        key = ip or "unknown"
        with self._lock:
            recent = [ts for ts in self._failures.get(key, []) if now - ts < RATE_WINDOW]
            recent.append(now)
            self._failures[key] = recent

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip or "unknown", None)

    # ------------------------------------------------------------------ #
    def log_login_failure(self, username: str, ip: str) -> None:
        """只记录账号名与 IP，**绝不**记录密码。"""
        if self._logger is None:
            return
        safe_user = " ".join(str(username or "").split())[:64]
        self._logger(f"[auth] login failed user={safe_user} ip={ip or 'unknown'}")


__all__ = [
    "AUTH_FILENAME",
    "COOKIE_NAME",
    "DEFAULT_USER",
    "PBKDF2_ITERATIONS",
    "RATE_MAX_FAILURES",
    "RATE_WINDOW",
    "SESSION_KEY_FILENAME",
    "SESSION_TTL",
    "AuthManager",
    "Credentials",
    "auth_path",
    "build_cookie",
    "hash_password",
    "random_password",
    "resolve_credentials",
    "session_key_path",
    "store_password",
]
