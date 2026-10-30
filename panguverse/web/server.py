"""HTTP 服务：静态资源 + API 路由。

- ``ThreadingHTTPServer`` + 自定义 ``BaseHTTPRequestHandler``，``allow_reuse_address``；
- 默认只服务本机单人使用：**没有认证**，不可暴露到公网；
- 静态资源只认白名单（``index.html``/``app.js``/``style.css``/``favicon.svg``），
  并用 ``realpath`` 前缀校验兜底，杜绝目录穿越；
- 前端资源缺失时 ``GET /`` 返回 503 + 明确文案，而不是让服务崩溃。
"""

from __future__ import annotations

import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .api import Response as ApiResponse
from .api import WebAPI, error_response

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_MIME = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
# 前端用的是 hash 路由（#/overview），只有深链才会带这些前缀；其余未知路径一律 JSON 404。
SPA_PREFIXES = {"overview", "run", "history", "runs", "atoms", "packs"}
MISSING_STATIC_MESSAGE = (
    "前端资源未就绪：未找到 panguverse/web/static/index.html。"
    "请等待前端资源部署后再访问（API 已可用）。"
)
MAX_BODY_BYTES = 8 * 1024 * 1024


class PanguverseRequestHandler(BaseHTTPRequestHandler):
    server_version = "PanguVerseWeb/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if _is_api_path(path):
            self._emit(self.server.api.handle("GET", path, parsed.query))
            return
        if path == "/":
            self._serve_index()
            return
        if path.startswith("/static/"):
            self._serve_static(unquote(path[len("/static/"):]))
            return
        if _is_spa_route(path):
            self._serve_index()
            return
        self._emit(error_response("not_found", "路径不存在"))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if not _is_api_path(parsed.path):
            self._emit(error_response("not_found", "路径不存在"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._emit(error_response("bad_request", "请求体过大"), close=True)
            return
        raw = self.rfile.read(length) if length else b""
        self._emit(self.server.api.handle("POST", parsed.path, parsed.query, raw))

    # ------------------------------------------------------------------ #
    def _serve_static(self, name: str) -> None:
        if name not in STATIC_MIME or "/" in name or "\\" in name or name.startswith("."):
            self._emit(error_response("not_found", "静态资源不存在"))
            return
        path = (STATIC_DIR / name).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._emit(error_response("not_found", "静态资源不存在"))
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._emit(error_response("not_found", "静态资源不存在"))
            return
        self._send(200, data, STATIC_MIME[name], {"Cache-Control": "no-cache"})

    def _serve_index(self) -> None:
        index = STATIC_DIR / "index.html"
        try:
            data = index.read_bytes()
        except OSError:
            self._send(
                503,
                MISSING_STATIC_MESSAGE.encode("utf-8"),
                "text/plain; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        self._send(200, data, STATIC_MIME["index.html"], {"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ #
    def _emit(self, response: ApiResponse, *, close: bool = False) -> None:
        headers = dict(response.headers)
        if close:
            self.close_connection = True
        self._send(response.status, response.body, response.content_type, headers)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)


class PanguverseHTTPServer(ThreadingHTTPServer):
    """带 API 实例的线程化 HTTP 服务。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler] = PanguverseRequestHandler,
        *,
        api: WebAPI,
        quiet: bool = False,
    ) -> None:
        self.api = api
        self.quiet = quiet
        super().__init__(server_address, handler_cls)


def create_server(
    host: str, port: int, workspace: str | Path, *, quiet: bool = False
) -> PanguverseHTTPServer:
    api = WebAPI(workspace)
    return PanguverseHTTPServer((host, port), api=api, quiet=quiet)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 7788,
    workspace: str | Path = ".",
    open_browser: bool = False,
    quiet: bool = False,
) -> int:
    root = Path(workspace).expanduser().resolve()
    try:
        httpd = create_server(host, port, root, quiet=quiet)
    except OSError as exc:
        print(f"❌ 无法监听 {host}:{port} -> {exc}（端口被占用或地址不可用）")
        return 2

    bound_port = int(httpd.server_address[1])
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    url = f"http://{display_host}:{bound_port}/"
    print(f"🪓 PanguVerse Web 已启动: {url}")
    print(f"   工作区: {root}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("⚠️  已监听非本机地址：本服务无认证，仅限可信网络使用，切勿暴露公网")
    if not (STATIC_DIR / "index.html").is_file():
        print("⚠️  前端资源未就绪（panguverse/web/static/index.html 缺失）：GET / 将返回 503")

    if open_browser and host in {"127.0.0.1", "localhost", "::1"}:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _is_spa_route(path: str) -> bool:
    head = path.strip("/").split("/", 1)[0]
    return bool(head) and head in SPA_PREFIXES


__all__ = [
    "MISSING_STATIC_MESSAGE",
    "STATIC_DIR",
    "PanguverseHTTPServer",
    "PanguverseRequestHandler",
    "create_server",
    "serve",
]
