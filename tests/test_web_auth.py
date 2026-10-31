"""Web 登录认证（``tasks/web-auth-spec.md`` v2.1，A27–A31 + --no-auth）。

全部走真实 HTTP 服务（端口 0）+ 临时工作区；Cookie 由测试客户端手工携带，
便于构造「篡改签名」「过期」「logout 后重放」等场景。
"""

from __future__ import annotations

import json
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from panguverse.web import auth as auth_mod
from panguverse.web.api import WebAPI
from panguverse.web.auth import COOKIE_NAME, AuthManager, store_password
from panguverse.web.server import PanguverseHTTPServer, create_server

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASSWORD = "correct-horse-battery"


class Client:
    def __init__(self, base: str, workspace: Path, manager: AuthManager | None, logs: list[str]):
        self.base = base
        self.workspace = workspace
        self.manager = manager
        self.logs = logs
        self.cookie = ""

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        cookie: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = self.cookie if cookie is None else cookie
        if token:
            req.add_header("Cookie", f"{COOKIE_NAME}={token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with _DIRECT.open(req, timeout=30) as resp:
                status, response_headers, body = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            status, response_headers, body = exc.code, dict(exc.headers), exc.read()
        set_cookie = response_headers.get("Set-Cookie", "")
        if set_cookie.startswith(f"{COOKIE_NAME}="):
            value = set_cookie.split(";", 1)[0].split("=", 1)[1]
            self.cookie = "" if "Max-Age=0" in set_cookie else value
        return status, response_headers, body

    def json(self, method: str, path: str, payload: Any = None, **kwargs: Any) -> tuple[int, Any]:
        status, _headers, body = self.request(method, path, payload, **kwargs)
        return status, json.loads(body.decode("utf-8") or "null")

    def login(self, password: str, username: str = "admin", **kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        status, headers, body = self.request(
            "POST", "/api/auth/login", {"username": username, "password": password}, **kwargs
        )
        return status, json.loads(body.decode("utf-8")), headers


@pytest.fixture()
def web(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store_password(workspace, "admin", PASSWORD)
    logs: list[str] = []
    manager = AuthManager(workspace, logger=logs.append)
    api = WebAPI(workspace, auth=manager)
    server = PanguverseHTTPServer(("127.0.0.1", 0), api=api, quiet=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}", workspace, manager, logs)
    finally:
        server.shutdown()
        server.server_close()


def assert_error(body: Any, code: str | None = None) -> None:
    assert isinstance(body, dict) and set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}, error
    assert isinstance(error["message"], str) and error["message"]
    if code is not None:
        assert error["code"] == code, error


def logged_in(web: Client) -> None:
    status, body, _headers = web.login(PASSWORD)
    assert status == 200, body
    assert web.cookie


# --------------------------------------------------------------------------- #
# A27：未登录一律 401，前端入口仍 200
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/tasks", "/api/runs", "/api/status"])
def test_a27_protected_api_requires_login(web: Client, path: str) -> None:
    status, _headers, body = web.request("GET", path)
    assert status == 401
    assert_error(json.loads(body.decode("utf-8")), "unauthorized")


def test_a27_index_and_static_stay_public(web: Client) -> None:
    status, _headers, body = web.request("GET", "/")
    assert status == 200 and b"<html" in body.lower()
    status, _headers, _body = web.request("GET", "/static/app.js")
    assert status == 200
    status, data = web.json("GET", "/api/auth/status")
    assert status == 200 and data == {"authenticated": False}


# --------------------------------------------------------------------------- #
# A28：错误密码（不区分账号/密码、不泄漏明文）
# --------------------------------------------------------------------------- #
def test_a28_bad_password(web: Client) -> None:
    status, body, headers = web.login("definitely-wrong")
    assert status == 401
    assert_error(body, "bad_credentials")
    text = json.dumps(body, ensure_ascii=False)
    assert "definitely-wrong" not in text
    assert PASSWORD not in text
    assert "definitely-wrong" not in headers.get("Set-Cookie", "")

    # 不区分「用户不存在」与「密码错」
    status, body, _headers = web.login("definitely-wrong", username="nobody")
    assert status == 401
    assert_error(body, "bad_credentials")

    joined = "\n".join(web.logs)
    assert "definitely-wrong" not in joined
    assert PASSWORD not in joined
    assert "login failed" in joined and "ip=" in joined


# --------------------------------------------------------------------------- #
# A29：正确密码 → Cookie 属性 + 之后可访问
# --------------------------------------------------------------------------- #
def test_a29_login_sets_session_cookie(web: Client) -> None:
    status, body, headers = web.login(PASSWORD)
    assert status == 200
    assert body == {"ok": True, "user": "admin"}
    set_cookie = headers["Set-Cookie"]
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    for attribute in ("HttpOnly", "Path=/", "SameSite=Lax"):
        assert attribute in set_cookie, set_cookie
    assert "Secure" not in set_cookie  # 非 https 不加

    status, _headers, body = web.request("GET", "/api/status")
    assert status == 200
    assert json.loads(body.decode("utf-8"))["workspace"]

    status, data = web.json("GET", "/api/auth/status")
    assert status == 200 and data == {"authenticated": True, "user": "admin"}


def test_a29_secure_cookie_behind_https_proxy(web: Client) -> None:
    status, _body, headers = web.login(PASSWORD, headers={"X-Forwarded-Proto": "https"})
    assert status == 200
    assert "Secure" in headers["Set-Cookie"]


# --------------------------------------------------------------------------- #
# A30：会话安全
# --------------------------------------------------------------------------- #
def test_a30_tampered_signature_rejected(web: Client) -> None:
    logged_in(web)
    token = web.cookie
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    status, _headers, body = web.request("GET", "/api/status", cookie=tampered)
    assert status == 401
    assert_error(json.loads(body.decode("utf-8")), "unauthorized")


def test_a30_expired_session_rejected(web: Client) -> None:
    logged_in(web)
    assert web.manager is not None
    expired = web.manager.issue_session(ttl=-10)
    status, _headers, body = web.request("GET", "/api/status", cookie=expired)
    assert status == 401
    assert_error(json.loads(body.decode("utf-8")), "unauthorized")


def test_a30_logout_invalidates_old_cookie(web: Client) -> None:
    logged_in(web)
    old_cookie = web.cookie
    status, headers, body = web.request("POST", "/api/auth/logout")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"ok": True}
    assert "Max-Age=0" in headers["Set-Cookie"]
    assert web.cookie == ""

    status, _headers, body = web.request("GET", "/api/status", cookie=old_cookie)
    assert status == 401
    assert_error(json.loads(body.decode("utf-8")), "unauthorized")


def test_a30_status_is_idempotent_while_logged_out(web: Client) -> None:
    status, data = web.json("GET", "/api/auth/status")
    assert status == 200 and data == {"authenticated": False}
    status, data = web.json("POST", "/api/auth/logout")
    assert status == 200 and data == {"ok": True}


# --------------------------------------------------------------------------- #
# A31：失败限流
# --------------------------------------------------------------------------- #
def test_a31_rate_limit_after_five_failures(web: Client) -> None:
    for _ in range(5):
        status, body, _headers = web.login("wrong")
        assert status == 401 and body["error"]["code"] == "bad_credentials"
    status, body, headers = web.login("wrong")
    assert status == 429
    assert_error(body, "too_many_attempts")
    retry_after = headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) > 0

    # 被限流后，即使密码正确也不放行
    status, body, _headers = web.login(PASSWORD)
    assert status == 429


def test_a31_success_resets_failure_counter(web: Client) -> None:
    for _ in range(4):
        assert web.login("wrong")[0] == 401
    status, _body, _headers = web.login(PASSWORD)
    assert status == 200
    # 计数已清零：再错一次仍是 401 而不是第 5 次触发前的封锁
    status, body, _headers = web.login("wrong")
    assert status == 401 and body["error"]["code"] == "bad_credentials"


# --------------------------------------------------------------------------- #
# 凭据来源 / 文件权限 / 首启生成
# --------------------------------------------------------------------------- #
def test_auth_files_are_private(web: Client) -> None:
    auth_file = auth_mod.auth_path(web.workspace)
    key_file = auth_mod.session_key_path(web.workspace)
    assert auth_file.is_file() and key_file.is_file()
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_file.parent.stat().st_mode) == 0o700
    assert PASSWORD not in auth_file.read_text(encoding="utf-8")
    assert web.manager is not None
    assert web.manager.issue_session()  # 密钥可用


def test_env_credentials_take_priority(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("PANGUVERSE_WEB_USER", "envuser")
    monkeypatch.setenv("PANGUVERSE_WEB_PASSWORD", "env-secret")
    manager = AuthManager(workspace)
    assert manager.user == "envuser"
    assert manager.verify_credentials("envuser", "env-secret")
    assert not manager.verify_credentials("admin", "env-secret")
    assert not auth_mod.auth_path(workspace).exists()  # env 凭据不落盘


def test_first_start_generates_password_once(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = AuthManager(workspace)
    password = first.generated_password
    assert len(password) == 16
    assert first.verify_credentials("admin", password)
    assert auth_mod.auth_path(workspace).is_file()

    # 第二次启动（同工作区）不再生成，也不会再拿到明文
    second = AuthManager(workspace)
    assert second.generated_password == ""
    assert second.verify_credentials("admin", password)


# --------------------------------------------------------------------------- #
# A33：--no-auth 行为与 v1.4 一致
# --------------------------------------------------------------------------- #
def test_no_auth_mode_is_open(tmp_path) -> None:
    workspace = tmp_path / "ws"
    server = create_server("127.0.0.1", 0, workspace, quiet=True, auth=None)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}", workspace, None, [])
    try:
        status, _headers, body = client.request("GET", "/api/status")
        assert status == 200 and json.loads(body.decode("utf-8"))["workspace"]
        status, data = client.json("GET", "/api/auth/status")
        assert status == 200
        assert data["authenticated"] is True and data["auth_disabled"] is True
        status, data = client.json("GET", "/api/tasks")
        assert status == 200 and data == {"tasks": []}
    finally:
        server.shutdown()
        server.server_close()
