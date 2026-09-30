"""自包含 HTML 报告：单文件、无外部资源、可离线打开与归档。

报告只渲染**已打码**的数据（来自 ``run.to_dict(mask=True)``），
密码、token 不会出现在产物里。
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

_STATUS_CLASS = {
    "passed": "ok",
    "failed": "bad",
    "error": "err",
    "skipped": "skip",
    "ok": "ok",
}
_STATUS_TEXT = {
    "passed": "通过",
    "failed": "失败",
    "error": "异常",
    "skipped": "跳过",
    "ok": "完成",
}


def render(report: dict[str, Any], *, title: str = "PanguVerse API 自动化测试报告") -> str:
    summary = report.get("summary", {})
    plan = report.get("plan", {})
    cases = report.get("cases", [])
    phases = report.get("phases", [])
    conclusion = report.get("conclusion", {})

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>{_e(title)}</title>",
        "<style>",
        _CSS,
        "</style></head><body>",
        '<div class="wrap">',
        _header(report, title, conclusion),
        _summary_cards(summary),
        _phases(phases),
        _plan_block(plan, report),
        _cases_block(cases),
        _footer(report),
        "</div>",
        "<script>",
        _JS,
        "</script>",
        "</body></html>",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 区块
# --------------------------------------------------------------------------- #
def _header(report: dict[str, Any], title: str, conclusion: dict[str, Any]) -> str:
    success = bool(conclusion.get("success"))
    badge = "pass" if success else "fail"
    started = _ts(report.get("started_at"))
    duration = report.get("duration_ms", 0) / 1000.0
    return f"""
<header>
  <div class="title">
    <h1>🪓 {_e(title)}</h1>
    <p class="sub">run <code>{_e(str(report.get('run_id', '')))}</code>
       · 开始 {_e(started)} · 耗时 {duration:.2f}s
       · 传输后端 {_e(str((report.get('meta') or {}).get('backend', '?')))}</p>
  </div>
  <div class="verdict {badge}">{_e(str(conclusion.get('verdict', '')))}</div>
</header>"""


def _summary_cards(summary: dict[str, Any]) -> str:
    cards = [
        ("用例总数", summary.get("total", 0), ""),
        ("通过", summary.get("passed", 0), "ok"),
        ("失败", summary.get("failed", 0), "bad" if summary.get("failed") else ""),
        ("异常", summary.get("errored", 0), "err" if summary.get("errored") else ""),
        ("跳过", summary.get("skipped", 0), "skip" if summary.get("skipped") else ""),
        ("自愈用例", summary.get("healed_cases", 0), "heal" if summary.get("healed_cases") else ""),
        ("步骤数", summary.get("steps", 0), ""),
        ("HTTP 调用", summary.get("http_requests", 0), ""),
    ]
    cells = "".join(
        f'<div class="card {cls}"><div class="num">{_e(str(value))}</div>'
        f'<div class="label">{_e(str(label))}</div></div>'
        for label, value, cls in cards
    )
    return f'<section class="cards">{cells}</section>'


def _phases(phases: list[dict[str, Any]]) -> str:
    if not phases:
        return ""
    items = []
    for index, phase in enumerate(phases, 1):
        status = str(phase.get("status", ""))
        cls = _STATUS_CLASS.get(status, "")
        items.append(
            f'<li class="{cls}"><span class="idx">{index}</span>'
            f'<span class="pname">{_e(str(phase.get("name", "")))}</span>'
            f'<span class="pdetail">{_e(str(phase.get("detail", "")))}</span></li>'
        )
    return f'<section class="phases"><h2>闭环阶段</h2><ol>{"".join(items)}</ol></section>'


def _plan_block(plan: dict[str, Any], report: dict[str, Any]) -> str:
    notes = plan.get("notes") or []
    warnings = plan.get("warnings") or []
    items = "".join(f"<li>{_e(str(note))}</li>" for note in notes)
    warn = "".join(f"<li>{_e(str(w))}</li>" for w in warnings)
    meta = report.get("meta") or {}
    return f"""
<section class="plan">
  <h2>计划与来源</h2>
  <p>来源 <code>{_e(str(plan.get('source', '-')))}</code>
     · 规划器 <code>{_e(str(plan.get('planner', '-')))}</code>
     · 用例 {_e(str(plan.get('case_count', 0)))} · 步骤 {_e(str(plan.get('step_count', 0)))}</p>
  {f'<ul class="notes">{items}</ul>' if items else ''}
  {f'<ul class="warnings">{warn}</ul>' if warn else ''}
  <details><summary>运行元数据</summary><pre>{_e(json.dumps(meta, ensure_ascii=False, indent=2))}</pre></details>
</section>"""


def _cases_block(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return ""
    blocks = "".join(_case_block(case) for case in cases)
    return f'<section class="cases"><h2>用例明细</h2>{blocks}</section>'


def _case_block(case: dict[str, Any]) -> str:
    status = str(case.get("status", ""))
    cls = _STATUS_CLASS.get(status, "")
    tags = "".join(f'<span class="tag">{_e(str(t))}</span>' for t in (case.get("tags") or []))
    healed = ""
    if case.get("heal_events"):
        healed = f'<span class="badge heal">自愈 {len(case["heal_events"])} 次</span>'
    steps = "".join(_step_row(step, case_id=str(case.get("case_id", ""))) for step in case.get("steps", []))
    error = ""
    if case.get("error"):
        error = f'<div class="case-error">{_e(str(case["error"].get("message", "")))}</div>'
    variables = ""
    if case.get("variables"):
        variables = (
            "<details><summary>变量快照（已打码）</summary>"
            f"<pre>{_e(json.dumps(case['variables'], ensure_ascii=False, indent=2))}</pre></details>"
        )
    return f"""
<article class="case {cls}">
  <div class="case-head">
    <span class="status {cls}">{_e(_STATUS_TEXT.get(status, status))}</span>
    <span class="case-id">{_e(str(case.get('case_id', '')))}</span>
    <span class="case-name">{_e(str(case.get('name', '')))}</span>
    <span class="case-meta">{float(case.get('duration_ms', 0)):.0f}ms · {case.get('http_requests', 0)} 次请求</span>
    {healed}
    <span class="tags">{tags}</span>
  </div>
  {error}
  <table class="steps">
    <thead><tr><th>步骤</th><th>原子</th><th>状态</th><th>耗时</th><th>重试</th><th>HTTP</th></tr></thead>
    <tbody>{steps}</tbody>
  </table>
  {variables}
</article>"""


def _step_row(step: dict[str, Any], *, case_id: str) -> str:
    status = str(step.get("status", ""))
    cls = _STATUS_CLASS.get(status, "")
    http_codes = " ".join(
        str(entry.get("response", {}).get("status_code", "?")) for entry in step.get("http", [])
    )
    detail_id = f"detail-{case_id}-{step.get('step_id')}"
    detail = _step_detail(step, detail_id)
    name = step.get("name") or step.get("step_id", "")
    return f"""
<tr class="{cls}">
  <td><code>{_e(str(step.get('step_id', '')))}</code><div class="step-name">{_e(str(name))}</div></td>
  <td>{_e(str(step.get('atom', '')))}</td>
  <td><span class="status {cls}">{_e(_STATUS_TEXT.get(status, status))}</span></td>
  <td>{float(step.get('duration_ms', 0)):.0f}ms</td>
  <td>{_e(str(step.get('retries', 0)))}</td>
  <td>{_e(http_codes or '-')}</td>
</tr>
{detail}"""


def _step_detail(step: dict[str, Any], detail_id: str) -> str:
    chunks: list[str] = []
    error = step.get("error") or {}
    if error:
        rows = [
            ("类型", f"{error.get('type', '')} / {error.get('kind', '')}"),
            ("信息", error.get("message", "")),
        ]
        if error.get("path"):
            rows.append(("位置", str(error["path"])))
        if error.get("expected") is not None or error.get("actual") is not None:
            rows.append(("期望", json.dumps(error.get("expected"), ensure_ascii=False)))
            rows.append(("实际", json.dumps(error.get("actual"), ensure_ascii=False)))
        body = "".join(
            f"<tr><th>{_e(k)}</th><td><pre>{_e(str(v))}</pre></td></tr>" for k, v in rows
        )
        chunks.append(f'<table class="kv">{body}</table>')
    for event in step.get("heal", []) or []:
        chunks.append(
            f'<div class="heal-event">自愈[{_e(str(event.get("kind")))}] '
            f'动作 {_e(str(event.get("action")))} · 成功 {event.get("success")} · {_e(str(event.get("reason", "")))}'
            f'{(" · 建议: " + _e(str(event.get("suggestion")))) if event.get("suggestion") else ""}</div>'
        )
    for entry in step.get("http", []) or []:
        request = entry.get("request", {})
        response = entry.get("response", {})
        chunks.append(
            "<details><summary>HTTP 往返 "
            f"{_e(str(request.get('method', '')))} {_e(str(request.get('url', '')))} → "
            f"{_e(str(response.get('status_code', '')))}</summary>"
            f"<pre>请求: {_e(json.dumps(request, ensure_ascii=False, indent=2))}\n\n"
            f"响应: {_e(json.dumps(response, ensure_ascii=False, indent=2))}</pre></details>"
        )
    if step.get("inputs"):
        chunks.append(
            f"<details><summary>步骤输入</summary><pre>{_e(json.dumps(step['inputs'], ensure_ascii=False, indent=2))}</pre></details>"
        )
    if step.get("skipped_reason"):
        chunks.append(f'<div class="skip-reason">跳过原因：{_e(str(step["skipped_reason"]))}</div>')
    if not chunks:
        return ""
    return f'<tr class="detail-row"><td colspan="6"><div class="detail" id="{_e(detail_id)}">{"".join(chunks)}</div></td></tr>'


def _footer(report: dict[str, Any]) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    artifacts = report.get("artifacts") or {}
    links = "".join(
        f'<li><code>{_e(str(name))}</code> → {_e(str(path))}</li>' for name, path in artifacts.items()
    )
    return f"""
<footer>
  <p>生成时间 {_e(generated)} · 退出码 {_e(str(report.get('conclusion', {}).get('exit_code', 0)))}</p>
  {f'<ul>{links}</ul>' if links else ''}
  <p class="sub">PanguVerse · 执行 → 观察 → 沉淀 → 进化</p>
</footer>"""


# --------------------------------------------------------------------------- #
def write_html(report: dict[str, Any], path: str | Path, *, title: str = "PanguVerse API 自动化测试报告") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(report, title=title), encoding="utf-8")
    return target


def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--fg:#e8eaed;--dim:#9aa3b2;
--ok:#2ecc71;--bad:#ff5f56;--err:#ffa726;--skip:#7f8c9b;--heal:#4dd0e1;--line:#2a2f3a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;border-bottom:1px solid var(--line);padding-bottom:16px}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:16px;margin:26px 0 10px;color:var(--fg)}
.sub{color:var(--dim);margin:0;font-size:13px}
code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.verdict{padding:8px 16px;border-radius:999px;font-weight:600;white-space:nowrap}
.verdict.pass{background:rgba(46,204,113,.15);color:var(--ok);border:1px solid rgba(46,204,113,.4)}
.verdict.fail{background:rgba(255,95,86,.15);color:var(--bad);border:1px solid rgba(255,95,86,.4)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:20px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center}
.card .num{font-size:22px;font-weight:700}
.card .label{color:var(--dim);font-size:12px}
.card.ok .num{color:var(--ok)}.card.bad .num{color:var(--bad)}.card.err .num{color:var(--err)}
.card.skip .num{color:var(--skip)}.card.heal .num{color:var(--heal)}
.phases ol{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:6px}
.phases li{display:flex;gap:10px;align-items:baseline;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:8px;padding:8px 12px}
.phases li.failed{border-left-color:var(--bad)}
.phases li .idx{color:var(--dim);font-size:12px;min-width:16px}
.phases li .pname{font-weight:600;min-width:110px}
.phases li .pdetail{color:var(--dim)}
.plan p{color:var(--dim)}
.notes,.warnings{padding-left:20px;margin:6px 0}
.warnings li{color:var(--err)}
.case{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}
.case.failed{border-color:rgba(255,95,86,.45)}.case.error{border-color:rgba(255,167,38,.45)}
.case-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.case-id{font-weight:700;font-family:ui-monospace,Consolas,monospace}
.case-name{color:var(--dim)}
.case-meta{color:var(--dim);font-size:12.5px;margin-left:auto}
.status{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.status.ok{background:rgba(46,204,113,.15);color:var(--ok)}
.status.bad{background:rgba(255,95,86,.15);color:var(--bad)}
.status.err{background:rgba(255,167,38,.15);color:var(--err)}
.status.skip{background:rgba(127,140,155,.2);color:var(--skip)}
.badge.heal{background:rgba(77,208,225,.15);color:var(--heal);padding:2px 9px;border-radius:999px;font-size:12px}
.tag{background:var(--panel2);color:var(--dim);border-radius:6px;padding:1px 7px;font-size:12px;margin-left:4px}
table.steps{width:100%;border-collapse:collapse;margin-top:12px}
table.steps th{text-align:left;color:var(--dim);font-weight:500;font-size:12.5px;border-bottom:1px solid var(--line);padding:6px 8px}
table.steps td{padding:7px 8px;border-bottom:1px solid rgba(42,47,58,.6);vertical-align:top}
.step-name{color:var(--dim);font-size:12px}
tr.detail-row td{border-bottom:1px solid var(--line);background:rgba(0,0,0,.15)}
.detail{padding:6px 4px}
.case-error{color:var(--bad);margin-top:10px;white-space:pre-wrap}
.heal-event{color:var(--heal);margin:6px 0;font-size:13px}
.skip-reason{color:var(--skip);font-size:13px}
table.kv{width:100%;border-collapse:collapse;margin:4px 0}
table.kv th{text-align:left;color:var(--dim);font-weight:500;width:70px;vertical-align:top}
table.kv td{color:var(--fg)}
pre{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:420px;font-size:12.5px}
details{margin:6px 0}
summary{cursor:pointer;color:var(--dim)}
footer{margin-top:36px;border-top:1px solid var(--line);padding-top:14px;color:var(--dim)}
footer ul{padding-left:20px}
"""

_JS = """
document.querySelectorAll('details.http').forEach(function(el){el.open=false;});
"""
