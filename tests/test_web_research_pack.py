"""``web_research`` 领域包契约测试（v6.0 §1）。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from panguverse.core.context import RunContext
from panguverse.core.errors import AtomFailure
from panguverse.core.pack import builtin_pack_registry
from panguverse.packs.web_research import (
    extract_structure,
    html_extract_structure,
    html_extract_text,
    http_get_page,
    llm_summarize,
)

HTML = """<!doctype html>
<html><head><title>示例 标题</title><style>.x{}</style></head>
<body>
  <header><h1>主标题</h1></header>
  <main>
    <p>第一段  正文。</p>
    <p>第二段。</p>
    <script>alert('bad')</script>
    <a href="/inside">内部链接</a>
    <a href="https://example.com/out">外部链接</a>
  </main>
  <footer>页脚</footer>
</body></html>"""


@pytest.fixture()
def page_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            if self.path == "/gbk":
                body = HTML.encode("gbk")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=gbk")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/notfound":
                body = b"<html><title>404</title><body>missing</body></html>"
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_pack_is_registered_and_read_only() -> None:
    registry = builtin_pack_registry(reload=True)
    assert registry.has("web_research")
    pack = registry.get("web_research")
    assert {atom.name for atom in pack.atoms} == {
        "http_get_page",
        "html_extract_text",
        "html_extract_structure",
        "llm_summarize",
    }
    for atom in pack.atoms:
        assert "mutate" not in atom.effects
        assert "destructive" not in atom.effects
    llm = next(atom for atom in pack.atoms if atom.name == "llm_summarize")
    assert "needs_llm" in llm.capabilities
    assert pack.semantics.name == "web_research"


def test_http_get_page_decodes_declared_charset(page_server: str) -> None:
    ctx = RunContext()
    out = http_get_page.fn(ctx, f"{page_server}/gbk")
    assert out["status"] == 200
    assert out["charset"].lower() == "gbk"
    assert "示例 标题" in out["html"]
    assert "第一段 正文。" in out["text"]
    assert out["final_url"].endswith("/gbk")


def test_http_get_page_defaults_to_utf8_and_keeps_4xx(page_server: str) -> None:
    ctx = RunContext()
    ok = http_get_page.fn(ctx, f"{page_server}/plain")
    assert ok["status"] == 200 and ok["charset"] == "utf-8"
    assert "主标题" in ok["text"]

    missing = http_get_page.fn(ctx, f"{page_server}/notfound")
    assert missing["status"] == 404
    assert "missing" in missing["text"]


def test_http_get_page_connection_failure_is_network() -> None:
    # 先占用再释放一个端口，确保连接被拒绝
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(AtomFailure) as exc:
        http_get_page.fn(RunContext(), f"http://127.0.0.1:{port}/")
    assert exc.value.kind in {"network", "timeout"}


def test_html_extract_text_strips_scripts_and_keeps_paragraphs() -> None:
    out = html_extract_text.fn(RunContext(), HTML)
    assert "alert" not in out["text"]
    assert ".x{}" not in out["text"]
    assert "第一段 正文。" in out["text"]
    assert "\n" in out["text"]
    assert out["chars"] == len(out["text"])
    assert html_extract_text.fn(RunContext(), "") == {"text": "", "chars": 0}


def test_html_extract_structure_title_headings_links_sections() -> None:
    out = html_extract_structure.fn(RunContext(), HTML)
    assert out["title"] == "示例 标题"
    assert {"level": 1, "text": "主标题"} in out["headings"]
    links = {link["href"]: link for link in out["links"]}
    assert links["/inside"]["internal"] is True
    assert links["https://example.com/out"]["internal"] is False
    assert out["sections"]
    assert out["counts"]["headings"] == 1
    assert out["counts"]["links"] == 2


def test_llm_summarize_without_key_hard_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PANGUVERSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AtomFailure) as exc:
        llm_summarize.fn(RunContext(), "一些页面文本", "给布局报告")
    assert exc.value.kind == "missing_llm"
    assert "未配置 LLM" in str(exc.value)


def test_llm_summarize_uses_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        available = True
        model = "fake-model"

        def complete(self, system: str, user: str, *, temperature: float = 0.0) -> str:
            assert "页面" in user
            return "# 布局报告\n\n- 顶部导航\n- 主内容"

    monkeypatch.setattr("panguverse.axe.llm.LLMClient", lambda *a, **k: FakeClient())
    out = llm_summarize.fn(RunContext(), "页面内容", "给布局报告")
    assert out["model"] == "fake-model"
    assert out["summary_md"].startswith("# 布局报告")


def test_extract_helpers_are_pure() -> None:
    assert extract_structure(HTML)["title"] == "示例 标题"
