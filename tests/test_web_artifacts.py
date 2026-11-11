"""任务产出物契约测试（v8.0 §2，验收 A1–A7）。

真实 HTTP + 临时工作区；收集器用真实 :class:`RunResult` 形状的步骤输出驱动，
运行链路（A1 的 links、A7 的失败注入）都走 ``POST /api/runs`` 真跑一次。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from panguverse.web import artifacts
from panguverse.web.api import WebAPI
from panguverse.web.auth import AuthManager
from panguverse.web.server import PanguverseHTTPServer

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000154a24f6d0000000049454e44ae426082"
)


class WebClient:
    def __init__(self, base: str, api: WebAPI) -> None:
        self.base = base
        self.api = api
        self.workspace = api.root

    def request(self, method: str, path: str, payload: Any = None) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _DIRECT.open(req, timeout=60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def get_json(self, path: str) -> tuple[int, Any]:
        status, _headers, body = self.request("GET", path)
        return status, json.loads(body.decode("utf-8"))

    def post_json(self, path: str, payload: Any) -> tuple[int, Any]:
        status, _headers, body = self.request("POST", path, payload)
        return status, json.loads(body.decode("utf-8"))

    def delete(self, path: str) -> int:
        status, _headers, _body = self.request("DELETE", path)
        return status

    def wait_job(self, job_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, data = self.get_json(f"/api/jobs/{job_id}")
            assert status == 200, data
            if data["status"] in ("done", "failed", "cancelled"):
                return data
            time.sleep(0.03)
        raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


@pytest.fixture()
def web(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    api = WebAPI(workspace)
    server = PanguverseHTTPServer(("127.0.0.1", 0), api=api, quiet=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    client = WebClient(f"http://127.0.0.1:{server.server_address[1]}", api)
    try:
        yield client
    finally:
        api.stop_scheduler()
        server.shutdown()
        server.server_close()


def _step(atom: str, outputs: dict[str, Any], status: str = "passed") -> Any:
    return SimpleNamespace(atom=atom, outputs=outputs, status=status, step_id=f"step-{atom}")


def _run(run_id: str, steps: list[Any], *, project: str | None = None) -> Any:
    meta = {"project_id": project} if project else {}
    return SimpleNamespace(
        run_id=run_id,
        meta=meta,
        cases=[SimpleNamespace(case_id="case-1", steps=steps)],
    )


def _collect(web: WebClient, run: Any) -> dict[str, Any]:
    return artifacts.collect_run(web.workspace, run)


def _items(web: WebClient, query: str = "") -> tuple[int, Any]:
    return web.get_json("/api/artifacts" + query)


def _scenario_with_links(web: WebClient) -> None:
    (web.workspace / "art.yaml").write_text(
        "name: artifact-links\n"
        "cases:\n"
        "  - id: c1\n"
        "    name: links\n"
        "    steps:\n"
        "      - id: links\n"
        "        use: web_extract_links\n"
        "        with:\n"
        "          html: \"<html><body><a href='/a'>A</a><a href='https://example.com/b'>B</a></body></html>\"\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# A1 收集：llm_summarize → summary_md；报告级 AI 总结自动成为产出物
# --------------------------------------------------------------------------- #
def test_a1_collect_summary_and_insight(web: WebClient) -> None:
    run = _run(
        "abcdef123456",
        [_step("llm_summarize", {"summary_md": "# 总结\n\n- 通过\n- 完成", "model": "fake"})],
    )
    result = _collect(web, run)
    assert result["count"] == 1
    item = result["artifacts"][0]
    assert item["name"] == "summary_md" and item["type"] == "markdown"
    assert item["node_id"] == "llm_summarize"
    assert item["bytes"] == len("# 总结\n\n- 通过\n- 完成".encode())
    assert item["sha256"] == hashlib.sha256(
        "# 总结\n\n- 通过\n- 完成".encode()
    ).hexdigest()
    manifest = json.loads(
        (web.workspace / ".panguverse" / "artifacts" / "unclassified" / "abcdef123456" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["artifacts"][0]["sha256"] == item["sha256"]
    assert manifest["artifacts"][0]["bytes"] == item["bytes"]

    # 真实运行：web_extract_links 产出 links；随后 AI 总结自动成为 ai_summary
    _scenario_with_links(web)
    status, queued = web.post_json("/api/runs", {"mode": "scenario", "scenario": "art.yaml"})
    assert status == 202
    job = web.wait_job(queued["job_id"])
    assert job["status"] == "done", job
    status, listing = _items(web, f"?run={job['run_id']}")
    assert status == 200
    names = {item["name"]: item for item in listing["items"]}
    assert "links" in names and names["links"]["type"] == "json"
    assert names["links"]["node_id"] == "web_extract_links"

    status, insight = web.post_json(f"/api/runs/{job['run_id']}/insight", {})
    assert status == 200 and insight["summary_md"]
    status, listing = _items(web, f"?run={job['run_id']}")
    names = {item["name"]: item for item in listing["items"]}
    assert "ai_summary" in names and names["ai_summary"]["type"] == "markdown"


# --------------------------------------------------------------------------- #
# A2 类型识别：markdown / image / json / xml（声明优先 + 内容嗅探）
# --------------------------------------------------------------------------- #
def test_a2_artifact_types(web: WebClient) -> None:
    shot = web.workspace / ".panguverse" / "web" / "shot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(PNG)

    run = _run(
        "aabbccddeeff",
        [
            _step("llm_summarize", {"summary_md": "## 报告"}),
            _step("web_screenshot", {"path": ".panguverse/web/shot.png", "bytes": len(PNG)}),
            _step("android_ui_dump", {"xml": "<hierarchy><node text='ok'/></hierarchy>"}),
            _step("web_extract_links", {"links": [{"href": "/a", "text": "A"}]}),
        ],
    )
    result = _collect(web, run)
    kinds = {item["name"]: item["type"] for item in result["artifacts"]}
    assert kinds == {
        "summary_md": "markdown",
        "screenshot": "image",
        "ui_tree": "xml",
        "links": "json",
    }
    # 内容嗅探（未声明时）
    assert artifacts.infer_type("x", b"# title") == "markdown"
    assert artifacts.infer_type("x", b"<root/>") == "xml"
    assert artifacts.infer_type("x", b'{"a": 1}') == "json"
    assert artifacts.infer_type("x", PNG) == "image"
    assert artifacts.infer_type("x", b"plain words") == "text"

    # 单产物 20MB 上限：只记录元信息 + truncated=true，不落文件，下载 409
    oversized = _collect(
        web,
        _run(
            "999999999999",
            [_step("llm_summarize", {"summary_md": "x" * (artifacts.MAX_ARTIFACT_BYTES + 1)})],
        ),
    )["artifacts"][0]
    assert oversized["truncated"] is True
    assert oversized["bytes"] > artifacts.MAX_ARTIFACT_BYTES
    assert oversized["file"] == ""
    assert not (
        web.workspace
        / ".panguverse"
        / "artifacts"
        / "unclassified"
        / "999999999999"
        / "summary_md.md"
    ).exists()
    status, _headers, body = web.request("GET", f"/api/artifacts/{oversized['artifact_id']}/download")
    assert status == 409 and json.loads(body)["error"]["code"] == "conflict"


# --------------------------------------------------------------------------- #
# A3 列表/过滤/分页：不重不漏
# --------------------------------------------------------------------------- #
def test_a3_list_filters_and_pagination(web: WebClient) -> None:
    _collect(
        web,
        _run(
            "111111111111",
            [
                _step("llm_summarize", {"summary_md": "# one"}),
                _step("web_extract_links", {"links": [{"href": "/1"}]}),
            ],
            project="p-aaa111",
        ),
    )
    _collect(
        web,
        _run(
            "222222222222",
            [
                _step("llm_summarize", {"summary_md": "# two"}),
                _step("android_ui_dump", {"xml": "<a/>"}),
            ],
            project="p-aaa111",
        ),
    )
    _collect(
        web,
        _run(
            "333333333333",
            [
                _step("llm_summarize", {"summary_md": "# three"}),
                _step("web_extract_links", {"links": []}),
            ],
            project="p-bbb222",
        ),
    )

    status, data = _items(web, "?project=p-aaa111")
    assert status == 200 and data["total"] == 4
    assert {item["run_id"] for item in data["items"]} == {"111111111111", "222222222222"}

    status, data = _items(web, "?project=p-bbb222&run=333333333333")
    assert status == 200 and data["total"] == 2

    status, data = _items(web, "?type=markdown")
    assert status == 200 and data["total"] == 3

    pagination: list[str] = []
    offset = 0
    while True:
        status, page = _items(web, f"?project=p-aaa111&offset={offset}&limit=2")
        assert status == 200
        ids = [item["artifact_id"] for item in page["items"]]
        assert len(ids) == min(2, page["total"] - offset)
        pagination.extend(ids)
        offset += 2
        if offset >= page["total"]:
            break
    status, full = _items(web, "?project=p-aaa111")
    assert len(pagination) == len(set(pagination)) == full["total"]
    assert set(pagination) == {item["artifact_id"] for item in full["items"]}


# --------------------------------------------------------------------------- #
# A4 下载：字节与 sha256 一致，Content-Disposition 带文件名
# --------------------------------------------------------------------------- #
def test_a4_download(web: WebClient) -> None:
    shot = web.workspace / "shot.png"
    shot.write_bytes(PNG)
    result = _collect(web, _run("ddddeeeeffff", [_step("web_screenshot", {"path": "shot.png"})]))
    item = result["artifacts"][0]
    assert item["type"] == "image"

    status, headers, body = web.request("GET", f"/api/artifacts/{item['artifact_id']}/download")
    assert status == 200
    assert body == PNG
    assert hashlib.sha256(body).hexdigest() == item["sha256"]
    disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    assert "attachment" in disposition and "screenshot.png" in disposition
    assert (headers.get("Content-Type") or headers.get("content-type")) == "image/png"


# --------------------------------------------------------------------------- #
# A5 分享：未登录可访问；过期 410；撤销 404
# --------------------------------------------------------------------------- #
def test_a5_share_lifecycle(web: WebClient) -> None:
    result = _collect(
        web, _run("555555555555", [_step("llm_summarize", {"summary_md": "# 分享\n\n- 只读"})])
    )
    aid = result["artifacts"][0]["artifact_id"]

    status, share = web.post_json(f"/api/artifacts/{aid}/share", {"ttl": "1h"})
    assert status == 201 and share["expires_at"] > time.time() + 3500
    token = share["url_token"]

    revoked_status, revoked = web.post_json(f"/api/artifacts/{aid}/share", {"expires_in": "1h"})
    assert revoked_status == 201
    assert web.delete(f"/api/artifacts/{aid}/share/{revoked['url_token']}") == 204

    # UI 的撤销按钮不带 token：DELETE /share 撤销该产物全部分享
    other = _collect(
        web, _run("777777777777", [_step("llm_summarize", {"summary_md": "# other"})])
    )
    other_aid = other["artifacts"][0]["artifact_id"]
    status, other_share = web.post_json(f"/api/artifacts/{other_aid}/share", {"ttl": "1d"})
    assert status == 201
    assert web.delete(f"/api/artifacts/{other_aid}/share") == 204
    status, _headers, _body = web.request("GET", f"/api/share/{other_share['url_token']}")
    assert status == 404

    expiring_status, expiring = web.post_json(
        f"/api/artifacts/{aid}/share", {"expires_in": 1}
    )
    assert expiring_status == 201

    # 开启登录后：分享页仍匿名可访问，普通 API 需要登录
    web.api.auth = AuthManager(web.workspace, logger=None)
    status, _headers, body = web.request("GET", f"/api/share/{token}")
    assert status == 200
    assert b"<html" in body.lower() and b"<h1>\xe5\x88\x86\xe4\xba\xab</h1>" in body
    status, _ = web.get_json("/api/artifacts")
    assert status == 401

    status, _headers, body = web.request("GET", f"/api/share/{revoked['url_token']}")
    assert status == 404
    status, _headers, body = web.request("GET", f"/api/share/{expiring['url_token']}")
    assert status == 200
    time.sleep(1.2)
    status, _headers, _body = web.request("GET", f"/api/share/{expiring['url_token']}")
    assert status == 410


# --------------------------------------------------------------------------- #
# A6 隔离：路径穿越 / 跨项目 / manifest 不可直读
# --------------------------------------------------------------------------- #
def test_a6_isolation(web: WebClient) -> None:
    result = _collect(
        web,
        _run("666666666666", [_step("llm_summarize", {"summary_md": "# secret"})], project="p-aaa111"),
    )
    aid = result["artifacts"][0]["artifact_id"]

    status, body = web.get_json(f"/api/artifacts/{aid}?project=p-bbb222")
    assert status == 404 and body["error"]["code"] == "not_found"

    status, body = web.get_json("/api/artifacts/ar-..%2F..%2Fetc")
    assert status == 404

    status, body = web.get_json("/api/artifacts?project=../../etc")
    assert status == 404
    status, body = web.get_json("/api/artifacts?run=../../etc/passwd")
    assert status == 404

    status, _headers, _body = web.request("GET", "/static/manifest.json")
    assert status == 404
    status, _headers, _body = web.request("GET", "/static/../.panguverse/artifacts/p-aaa111/666666666666/manifest.json")
    assert status == 404
    # manifest 没有 artifact_id，不能经产物接口读取
    status, body = web.get_json("/api/artifacts/ar-manifest")
    assert status == 404

    # 正常读取仍然可用（证明上面的 404 不是全局失效）
    status, data = web.get_json(f"/api/artifacts/{aid}?project=p-aaa111")
    assert status == 200 and data["content"].startswith("# secret")


# --------------------------------------------------------------------------- #
# A7 不阻断：产出物目录不可写 → 运行仍 done，只告警
# --------------------------------------------------------------------------- #
def test_a7_collection_failure_does_not_fail_run(web: WebClient) -> None:
    if os.geteuid() == 0:
        pytest.skip("root 无视目录权限，无法注入写失败")
    _scenario_with_links(web)
    directory = web.workspace / ".panguverse" / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    original = stat.S_IMODE(directory.stat().st_mode)
    os.chmod(directory, 0o500)
    try:
        status, queued = web.post_json("/api/runs", {"mode": "scenario", "scenario": "art.yaml"})
        assert status == 202
        job = web.wait_job(queued["job_id"])
    finally:
        os.chmod(directory, original)
    assert job["status"] == "done", job
    phases = [phase for phase in job["phases"] if phase["name"] == "artifacts"]
    assert phases and phases[-1]["status"] == "warning"
    status, run = web.get_json(f"/api/runs/{job['run_id']}")
    assert status == 200 and run["report"]["summary"]["success"] is True
    assert any("收集" in phase["detail"] or "失败" in phase["detail"] for phase in phases)


# --------------------------------------------------------------------------- #
# A8 图片产物：路径 → 真实文件字节；类型以魔数为准
# --------------------------------------------------------------------------- #
def _download(web: WebClient, aid: str, query: str = "") -> tuple[int, dict[str, str], bytes]:
    return web.request("GET", f"/api/artifacts/{aid}/download{query}")


def test_a8_screenshot_reads_workspace_file(web: WebClient) -> None:
    relative = "artifacts/web/shot.png"
    target = web.workspace / ".panguverse" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)

    result = _collect(
        web,
        _run(
            "777777777777",
            [
                _step(
                    "web_screenshot",
                    {"path": relative, "bytes": len(PNG), "width": 1, "height": 1},
                )
            ],
        ),
    )
    assert result["count"] == 1, result
    item = result["artifacts"][0]
    assert item["type"] == "image"
    assert item["bytes"] == len(PNG)
    assert item["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert item["file"].endswith(".png")

    status, headers, body = _download(web, item["artifact_id"])
    assert status == 200
    assert body == PNG
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert headers.get("Content-Type", "").startswith("image/png")


def test_a8_image_type_uses_magic_not_suffix(web: WebClient) -> None:
    relative = "artifacts/web/not-really.jpg"
    target = web.workspace / ".panguverse" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)

    result = _collect(
        web,
        _run("777777777778", [_step("web_screenshot", {"path": relative})]),
    )
    item = result["artifacts"][0]
    assert item["type"] == "image"
    # PNG 魔数优先于 .jpg 文件名后缀
    assert item["file"].endswith(".png")
    status, headers, body = _download(web, item["artifact_id"])
    assert status == 200 and headers["Content-Type"].startswith("image/png")
    assert body == PNG


def test_a8_missing_image_source_warns_without_artifact(web: WebClient) -> None:
    result = _collect(
        web,
        _run(
            "777777777779",
            [_step("web_screenshot", {"path": "artifacts/web/missing.png"})],
        ),
    )
    assert result["count"] == 0
    assert any("artifact_source_missing" in warning for warning in result["warnings"])
    status, listing = _items(web, "?run=777777777779")
    assert status == 200 and listing["total"] == 0


# --------------------------------------------------------------------------- #
# A9 ui_tree：落完整 XML（不是截断回显）；只有 nodes 时重建
# --------------------------------------------------------------------------- #
def test_a9_ui_tree_uses_full_xml(web: WebClient) -> None:
    full = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<hierarchy>"
        + "".join(
            f'<node text="节点{i}" resource-id="id/{i}" bounds="[0,{i}][10,{i + 1}]"/>'
            for i in range(400)
        )
        + "</hierarchy>"
    )
    clipped = full[:3000] + "…（已截断）"
    result = _collect(
        web,
        _run(
            "888888888888",
            [
                _step(
                    "android_ui_dump",
                    {
                        "xml": clipped,
                        "xml_full": full,
                        "xml_chars": len(full),
                        "nodes": [{"text": "节点0"}],
                        "count": 1,
                    },
                )
            ],
        ),
    )
    assert result["count"] == 1, result
    item = result["artifacts"][0]
    assert item["name"] == "ui_tree" and item["type"] == "xml"
    assert item["bytes"] == len(full.encode())
    path = (
        web.workspace
        / ".panguverse"
        / "artifacts"
        / "unclassified"
        / "888888888888"
        / "ui_tree.xml"
    )
    assert path.read_bytes() == full.encode()
    status, headers, body = _download(web, item["artifact_id"])
    assert status == 200 and body == full.encode()
    assert headers["Content-Type"].startswith("application/xml")


def test_a9_ui_tree_rebuilds_from_nodes(web: WebClient) -> None:
    nodes = [{"text": "深层节点", "bounds": "[0,690][1080,699]"}]
    result = _collect(
        web,
        _run(
            "888888888889",
            [_step("android_ui_dump", {"xml_chars": 0, "nodes": nodes, "count": 1})],
        ),
    )
    assert result["count"] == 1, result
    item = result["artifacts"][0]
    assert item["type"] == "xml"
    path = (
        web.workspace
        / ".panguverse"
        / "artifacts"
        / "unclassified"
        / "888888888889"
        / "ui_tree.xml"
    )
    body = path.read_text(encoding="utf-8")
    assert body.startswith("<?xml")
    assert 'text="深层节点"' in body


def test_a6_cross_project_download_returns_404(web: WebClient) -> None:
    result = _collect(
        web,
        _run(
            "666666666667",
            [_step("llm_summarize", {"summary_md": "# secret"})],
            project="p-aaa111",
        ),
    )
    aid = result["artifacts"][0]["artifact_id"]

    for query in ("?project=p-other", "?project=p-bbb222"):
        status, headers, body = web.request(
            "GET", f"/api/artifacts/{aid}/download{query}"
        )
        assert status == 404, (query, status, body)
        assert json.loads(body.decode("utf-8"))["error"]["code"] == "not_found"
        assert headers.get("Content-Type", "").startswith("application/json")

    # 列表过滤也按「不存在」处理，不是 400 探针
    status, body = web.get_json("/api/artifacts?project=p-other")
    assert status == 404 and body["error"]["code"] == "not_found"
    status, body = web.get_json("/api/artifacts?run=not-a-hex-run")
    assert status == 404 and body["error"]["code"] == "not_found"

    status, headers, body = web.request(
        "GET", f"/api/artifacts/{aid}/download?project=p-aaa111"
    )
    assert status == 200 and body == b"# secret"


# --------------------------------------------------------------------------- #
# A10 每次运行自动登记报告类产出物 + 历史 run 惰性回填
# --------------------------------------------------------------------------- #
_REPORT_HTML = "<!doctype html><html><body><h1>run report</h1></body></html>"
_JUNIT_XML = '<testsuite name="PanguVerse" tests="1"><testcase name="c1"/></testsuite>'
_PLAN_JSON = '{"source": "studio", "cases": [{"id": "c1"}]}'


def _write_run_dir(
    root: Path,
    run_id: str,
    *,
    project: str | None = None,
    report_html: str = _REPORT_HTML,
    with_noise: bool = False,
) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    report = {"run_id": run_id, "meta": {"project_id": project} if project else {}}
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "report.html").write_text(report_html, encoding="utf-8")
    (run_dir / "junit.xml").write_text(_JUNIT_XML, encoding="utf-8")
    (run_dir / "plan.json").write_text(_PLAN_JSON, encoding="utf-8")
    if with_noise:
        # 非白名单文件/目录：不应登记
        (run_dir / "trace.json").write_text('{"steps": []}', encoding="utf-8")
        (run_dir / "steps.jsonl").write_text("not an artifact\n", encoding="utf-8")
        (run_dir / "report.json.tmp").write_text("tmp", encoding="utf-8")
        (run_dir / "regression").mkdir()
        (run_dir / "regression" / "report.json").write_text("{}", encoding="utf-8")
    return run_dir


def _manifest_for(root: Path, project: str, run_id: str) -> dict[str, Any]:
    path = root / ".panguverse" / "artifacts" / project / run_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a10_record_run_outputs_registers_four_reports(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    run_dir = _write_run_dir(root, "4f265f46cbae", project="p-abcdef12", with_noise=True)

    result = artifacts.record_run_outputs(root, "4f265f46cbae")
    assert result["count"] == 4, result
    by_name = {item["name"]: item for item in result["artifacts"]}
    assert set(by_name) == {"report.html", "report.json", "junit.xml", "plan.json"}
    assert by_name["report.html"]["type"] == "text"
    assert by_name["report.html"]["file"] == "report.html"
    assert by_name["report.json"]["type"] == "json"
    assert by_name["junit.xml"]["type"] == "xml"
    assert by_name["plan.json"]["type"] == "json"
    assert all(item["project_id"] == "p-abcdef12" for item in by_name.values())
    assert by_name["report.html"]["bytes"] == len(_REPORT_HTML.encode())
    assert by_name["report.html"]["sha256"] == hashlib.sha256(
        _REPORT_HTML.encode()
    ).hexdigest()

    manifest = _manifest_for(root, "p-abcdef12", "4f265f46cbae")
    assert len(manifest["artifacts"]) == 4
    stored = {item["file"] for item in manifest["artifacts"]}
    assert stored == {"report.html", "report.json", "junit.xml", "plan.json"}
    # 临时/无关文件不登记
    assert not any("trace" in item["file"] for item in manifest["artifacts"])
    assert (run_dir / "report.html").exists()

    # 下载/预览可用（html 按 text 存储，仍可原样下载与内联预览）
    aid = by_name["report.html"]["artifact_id"]
    data, filename, mime = artifacts.download_artifact(root, aid)
    assert data == _REPORT_HTML.encode() and filename == "report.html"
    assert mime == "text/plain; charset=utf-8"
    detail = artifacts.get_artifact(root, aid)
    assert detail["content"] == _REPORT_HTML
    assert detail["download_url"].endswith("/download")


def test_a10_record_run_outputs_is_idempotent_by_name(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_run_dir(root, "aaaabbbbcccc")

    first = artifacts.record_run_outputs(root, "aaaabbbbcccc")
    first_ids = {item["name"]: item["artifact_id"] for item in first["artifacts"]}
    # 重复调用：同名覆盖，不堆叠、不换 id
    second = artifacts.record_run_outputs(root, "aaaabbbbcccc")
    second_ids = {item["name"]: item["artifact_id"] for item in second["artifacts"]}
    assert first_ids == second_ids
    manifest = _manifest_for(root, "unclassified", "aaaabbbbcccc")
    assert len(manifest["artifacts"]) == 4
    assert sorted(item["name"] for item in manifest["artifacts"]) == [
        "junit.xml",
        "plan.json",
        "report.html",
        "report.json",
    ]

    # 内容更新后仍是同一条记录（sha256/bytes 刷新，文件覆盖）
    (root / "runs" / "aaaabbbbcccc" / "report.html").write_text("<html>v2</html>", encoding="utf-8")
    third = artifacts.record_run_outputs(root, "aaaabbbbcccc")
    by_name = {item["name"]: item for item in third["artifacts"]}
    assert by_name["report.html"]["artifact_id"] == first_ids["report.html"]
    assert by_name["report.html"]["sha256"] == hashlib.sha256(b"<html>v2</html>").hexdigest()
    manifest = _manifest_for(root, "unclassified", "aaaabbbbcccc")
    assert len(manifest["artifacts"]) == 4


def test_a10_legacy_run_is_backfilled_lazily_via_api(web: WebClient) -> None:
    _write_run_dir(web.workspace, "4f265f46cbae", with_noise=True)

    status, data = web.get_json("/api/artifacts?run=4f265f46cbae")
    assert status == 200, data
    assert data["total"] == 4, data
    names = {item["name"]: item for item in data["items"]}
    assert set(names) == {"report.html", "report.json", "junit.xml", "plan.json"}
    ids = {item["artifact_id"] for item in data["items"]}

    # 第二次查询不再重复登记
    status, again = web.get_json("/api/artifacts?run=4f265f46cbae")
    assert status == 200 and again["total"] == 4
    assert {item["artifact_id"] for item in again["items"]} == ids

    # 下载与预览可用
    aid = names["report.html"]["artifact_id"]
    status, headers, body = web.request("GET", f"/api/artifacts/{aid}/download")
    assert status == 200 and body == _REPORT_HTML.encode()
    assert "report.html" in (headers.get("Content-Disposition") or "")
    status, detail = web.get_json(f"/api/artifacts/{aid}")
    assert status == 200 and detail["content"] == _REPORT_HTML
    status, share = web.post_json(f"/api/artifacts/{aid}/share", {"ttl": "1h"})
    assert status == 201
    status, _headers, page = web.request("GET", f"/api/share/{share['url_token']}")
    assert status == 200 and b"run report" in page

    # run 详情接口仍可读
    status, detail = web.get_json("/api/runs/4f265f46cbae")
    assert status == 200 and detail["report"]["run_id"] == "4f265f46cbae"


def test_a10_ensure_does_not_fake_missing_run(web: WebClient) -> None:
    result = artifacts.ensure_run_artifacts(web.workspace, "deadbeef1234")
    assert result["count"] == 0 and result["created"] is False
    assert not (web.workspace / "runs" / "deadbeef1234").exists()
    assert not (web.workspace / ".panguverse" / "artifacts").exists()

    status, data = web.get_json("/api/artifacts?run=deadbeef1234")
    assert status == 200 and data["total"] == 0
    assert not (web.workspace / ".panguverse" / "artifacts").exists()


def test_a10_missing_optional_reports_are_skipped(web: WebClient) -> None:
    run_dir = web.workspace / "runs" / "abcdabcdabcd"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text('{"meta": {}}', encoding="utf-8")
    (run_dir / "plan.json").write_text('{"cases": []}', encoding="utf-8")
    result = artifacts.record_run_outputs(web.workspace, "abcdabcdabcd")
    assert result["count"] == 2
    assert {item["name"] for item in result["artifacts"]} == {"report.json", "plan.json"}
    assert result["warnings"] == []


def test_a10_run_registers_report_outputs_end_to_end(web: WebClient) -> None:
    _scenario_with_links(web)
    status, queued = web.post_json("/api/runs", {"mode": "scenario", "scenario": "art.yaml"})
    assert status == 202, queued
    job = web.wait_job(queued["job_id"])
    assert job["status"] == "done", job
    run_id = job["run_id"]

    status, listing = web.get_json(f"/api/artifacts?run={run_id}")
    assert status == 200, listing
    names = {item["name"] for item in listing["items"]}
    assert {"report.html", "report.json", "junit.xml", "plan.json"} <= names
    by_name = {item["name"]: item for item in listing["items"]}
    assert by_name["report.html"]["type"] == "text"
    assert by_name["junit.xml"]["type"] == "xml"

    phases = [phase for phase in job["phases"] if phase["name"] == "artifacts"]
    assert phases and phases[-1]["status"] == "ok", phases
    assert "已登记" in phases[-1]["detail"]

    # 再查一次不重复登记
    status, again = web.get_json(f"/api/artifacts?run={run_id}")
    assert status == 200 and again["total"] == listing["total"]


def test_locate_absolute_artifact_inside_workspace(tmp_path) -> None:
    """G5：绝对产物路径只要在受信根目录内就应被登记，而不是误报源缺失。"""
    from panguverse.web import artifacts

    workspace = tmp_path / "runs" / "r1" / "workspace"
    workspace.mkdir(parents=True)
    produced = workspace / "report.html"
    produced.write_text("ok", encoding="utf-8")

    assert artifacts._locate_absolute(tmp_path, workspace, str(produced)) == produced.resolve()

    outside = tmp_path.parent / "outside-artifact.txt"
    outside.write_text("nope", encoding="utf-8")
    assert artifacts._locate_absolute(tmp_path, workspace, str(outside)) is None
