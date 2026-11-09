/* Mock data adapter for the /next prototype routes.
   No backend calls: everything here is deterministic demo data. */

export type NxStatus = 'done' | 'running' | 'fixing' | 'queued'
export type NxNeedKind = 'secret' | 'risk'

export interface NxPlanStep {
  id: string
  title: string
  atom: string
  kind: 'atom' | 'custom'
  risk: 'L0' | 'L1' | 'L2'
  status: NxStatus
  note: string
}

export interface NxTimelineEvent {
  id: string
  label: string
  detail: string
  status: NxStatus
  time: string
  duration?: string
}

export interface NxFeedItem {
  id: string
  at: string
  kind: 'observe' | 'fix' | 'decision' | 'success'
  text: string
  code?: string
}

export interface NxDebugRow {
  id: string
  at: string
  level: 'error' | 'fix' | 'pass'
  line: number
  nodeId: string
  message: string
}

export interface NxNeed {
  id: string
  kind: NxNeedKind
  title: string
  detail: string
  suggestion: string
  resolved: boolean
}

export interface NxSuggestion {
  id: string
  title: string
  text: string
  accepted: boolean
  approved: boolean
  alternative: boolean
}

export interface NxResult {
  title: string
  summary: string
  evidence: string[]
  humanCount: number
  aiActions: number
  artifacts: string[]
}

export const NX_INITIAL_INTENT = '每天 9 点抓取竞品价格，异常时发飞书提醒，并生成对比报告'

export const NX_TEMPLATES = [
  { id: 'tpl-price', title: '竞品价格监控', intent: '每天 9 点抓取竞品价格，异常时发飞书提醒，并生成对比报告', tag: 'HTTP · 报告' },
  { id: 'tpl-news', title: '每日资讯摘要', intent: '每小时抓取行业资讯，去重后生成摘要，重要新闻发到飞书群', tag: '抓取 · 摘要' },
  { id: 'tpl-form', title: '表单数据巡检', intent: '检查昨天的报名表单，字段缺失的记录写回表格并通知负责人', tag: '文件 · 通知' },
]

export const NX_INITIAL_PLAN: NxPlanStep[] = [
  { id: 'step-1', title: '识别意图与风险', atom: 'intent_route', kind: 'atom', risk: 'L0', status: 'done', note: '价格监控 · 每天 9:00' },
  { id: 'step-2', title: '抓取竞品价格页', atom: 'http_get_page', kind: 'atom', risk: 'L0', status: 'done', note: '3 个站点 · 并发 3' },
  { id: 'step-3', title: '提取 SKU 与价格', atom: 'extract_prices', kind: 'custom', risk: 'L0', status: 'fixing', note: 'AI 草稿 · 自修 2 次' },
  { id: 'step-4', title: '生成对比报告', atom: 'llm_summarize', kind: 'atom', risk: 'L1', status: 'running', note: '流式输出中' },
  { id: 'step-5', title: '推送飞书提醒', atom: 'notify_feishu', kind: 'atom', risk: 'L1', status: 'queued', note: '等待外部写批准' },
]

export const NX_INITIAL_TIMELINE: NxTimelineEvent[] = [
  { id: 'tl-1', label: '解析意图', detail: '匹配「价格监控」模板 · 置信度 0.92', status: 'done', time: '21:04:02', duration: '0.4s' },
  { id: 'tl-2', label: '抓取竞品价格页', detail: '3/3 页面成功 · 缓存未命中', status: 'done', time: '21:04:03', duration: '1.8s' },
  { id: 'tl-3', label: '提取 SKU 与价格', detail: '选择器失效 → 兜底正则 → 输出 12 条', status: 'fixing', time: '21:04:05', duration: '1.2s' },
  { id: 'tl-4', label: '生成对比报告', detail: '正在归纳价格差异与异常项', status: 'running', time: '21:04:06' },
  { id: 'tl-5', label: '推送飞书提醒', detail: '等待 L1 风险批准后执行外部写', status: 'queued', time: '—' },
]

export const NX_INITIAL_FEED: NxFeedItem[] = [
  { id: 'fd-1', at: '21:04:04', kind: 'observe', text: '3 个竞品页面结构不一致，AI 已按站点切换提取器。' },
  { id: 'fd-2', at: '21:04:06', kind: 'fix', text: '第 16 行选择器未命中，AI 在第 18 行加入 ¥ 兜底正则。', code: 'node-extract-prices · line 18' },
  { id: 'fd-3', at: '21:04:07', kind: 'observe', text: '识别到 JPY / USD 混用，已统一换算为 CNY。' },
  { id: 'fd-4', at: '21:04:08', kind: 'decision', text: '即将写入飞书外部群，已暂停并等待人工批准（L1）。' },
  { id: 'fd-5', at: '21:04:09', kind: 'success', text: '提取完成：12 个 SKU，2 个价格异常待复核。' },
]

export const NX_DEBUG_ROWS: NxDebugRow[] = [
  { id: 'dbg-1', at: '21:04:06', level: 'error', line: 16, nodeId: 'node-extract-prices', message: "KeyError: 'data-price' · 2/3 站点选择器未命中" },
  { id: 'dbg-2', at: '21:04:07', level: 'fix', line: 18, nodeId: 'node-extract-prices', message: 'AI 切换兜底正则 r"¥\\s*([\\d.]+)"，锁定段保持不变' },
  { id: 'dbg-3', at: '21:04:09', level: 'pass', line: 25, nodeId: 'node-extract-prices', message: '输出契约校验通过 · rows=12 / count=12' },
]

export const NX_INITIAL_NEEDS: NxNeed[] = [
  {
    id: 'need-secret',
    kind: 'secret',
    title: '缺密钥 · 竞品站点登录',
    detail: '站点 B 需要 COMP_PRICE_TOKEN 才能读取完整价格。',
    suggestion: '复用环境变量 COMP_PRICE_TOKEN（值已打码）',
    resolved: false,
  },
  {
    id: 'need-risk',
    kind: 'risk',
    title: 'L1 风险确认 · 外部写',
    detail: '推送飞书群「竞品价格监控」会向外部系统写消息。',
    suggestion: '批准一次，仅本次运行有效',
    resolved: false,
  },
]

export const NX_INITIAL_SUGGESTION: NxSuggestion = {
  id: 'sug-1',
  title: 'AI 建议 · 减少一次等待',
  text: '建议先完成报告生成，再把飞书推送与人工确认合并为一次外部写，可少一次批准。',
  accepted: false,
  approved: false,
  alternative: false,
}

export const NX_INITIAL_RESULT: NxResult = {
  title: '已提取 12 个 SKU 价格',
  summary: '3 个竞品站点已完成抓取与归一化；对比报告正在生成，最终报告会附证据与异常清单。',
  evidence: ['3 张页面截图', '12 条价格原始值', '2 次 AI 自修记录', 'run-9f2c14 · rev 4'],
  humanCount: 1,
  aiActions: 8,
  artifacts: ['price_compare.md', 'prices.csv'],
}

export function planForPhase(intent: string, phase: number): NxPlanStep[] {
  const next = NX_INITIAL_PLAN.map((step) => ({ ...step }))
  next[0] = { ...next[0], note: intent ? `意图：${intent.slice(0, 18)}${intent.length > 18 ? '…' : ''}` : next[0].note }
  const order: NxStatus[][] = [
    ['running', 'queued', 'queued', 'queued', 'queued'],
    ['done', 'running', 'queued', 'queued', 'queued'],
    ['done', 'done', 'fixing', 'queued', 'queued'],
    ['done', 'done', 'done', 'running', 'queued'],
    ['done', 'done', 'done', 'done', 'running'],
    ['done', 'done', 'done', 'done', 'done'],
  ]
  const statuses = order[Math.max(0, Math.min(order.length - 1, phase))]
  return next.map((step, index) => ({ ...step, status: statuses[index] }))
}

export function timelineForPhase(phase: number): NxTimelineEvent[] {
  const base = NX_INITIAL_TIMELINE.map((event) => ({ ...event }))
  const statuses: NxStatus[][] = [
    ['running', 'queued', 'queued', 'queued', 'queued'],
    ['done', 'running', 'queued', 'queued', 'queued'],
    ['done', 'done', 'fixing', 'queued', 'queued'],
    ['done', 'done', 'done', 'running', 'queued'],
    ['done', 'done', 'done', 'done', 'running'],
    ['done', 'done', 'done', 'done', 'done'],
  ]
  const current = statuses[Math.max(0, Math.min(statuses.length - 1, phase))]
  return base.map((event, index) => {
    const status = current[index]
    return {
      ...event,
      status,
      time: status === 'queued' ? '—' : event.time,
      detail: status === 'done' && event.id === 'tl-3' ? '选择器失效 → 兜底正则 → 输出 12 条' : event.detail,
      duration: status === 'running' || status === 'queued' ? undefined : event.duration,
    }
  })
}

export function feedForPhase(phase: number): NxFeedItem[] {
  const extra: NxFeedItem[] = [
    { id: 'live-1', at: '刚刚', kind: 'observe', text: 'AI 已拆解意图并生成 5 步计划，开始执行。' },
    { id: 'live-2', at: '刚刚', kind: 'observe', text: '页面抓取完成，正在比对 12 个 SKU 的价格字段。' },
    { id: 'live-3', at: '刚刚', kind: 'fix', text: '检测到选择器漂移，AI 在不改锁定段的前提下加入兜底分支。', code: 'node-extract-prices · line 18' },
    { id: 'live-4', at: '刚刚', kind: 'success', text: '提取与归一化完成，进入报告生成。' },
    { id: 'live-5', at: '刚刚', kind: 'observe', text: '报告已生成；飞书推送需要 L1 批准，AI 已挂起该步骤。' },
    { id: 'live-6', at: '刚刚', kind: 'success', text: '本轮执行完成，证据与产出物已落盘。' },
  ]
  if (phase <= 0) return [extra[0]]
  return extra.slice(0, Math.min(extra.length, phase + 1)).filter((item) => item.id !== 'live-5' || phase >= 4)
}

/* ------------------------------------------------------------------ */
/* Workbench                                                           */
/* ------------------------------------------------------------------ */

export interface NxPort {
  name: string
  type: string
  required?: boolean
  source?: string
  note?: string
}

export interface NxNodeMeta {
  id: string
  name: string
  atom: string
  custom: boolean
  locked: boolean
  rev: number
  autoFixes: number
  tests: { passed: number; total: number }
  contract: {
    inputs: NxPort[]
    outputs: NxPort[]
    capabilities: string[]
    sideEffect: string
    idempotency: string
    risk: string
    timeout: string
  }
  code: string
  logs: NxLogEvent[]
  note: string
}

export interface NxLogEvent {
  id: string
  at: string
  level: 'info' | 'warn' | 'error' | 'pass'
  line: number
  message: string
}

export const EXTRACT_CODE_V7 = `# AI 草稿 · 提取价格（draft）
# rev 7 · 锁定段 3-7 · 自动修复 2 次
import re
from panguverse.sdk import NodeContext

PRICE_RE = re.compile(r'data-price="([\\d.]+)"')
CURRENCY_RE = re.compile(r'data-currency="(\\w+)"')

def run(ctx: NodeContext) -> dict:
    html = ctx.inputs["html"]
    default_currency = ctx.inputs.get("currency", "CNY")
    rows = []
    for block in html.split("data-sku=")[1:]:
        sku = block.split('"')[0]
        match = PRICE_RE.search(block)
        if not match:
            ctx.log("价格选择器未命中，切换兜底正则")
            match = re.search(r"¥\\s*([\\d.]+)", block)
        if not match:
            continue
        price = float(match.group(1))
        currency = CURRENCY_RE.search(block)
        rows.append({"sku": sku, "price": price, "currency": currency.group(1) if currency else default_currency})
    ctx.log(f"共提取 {len(rows)} 条价格")
    return {"rows": rows, "count": len(rows)}`

export const EXTRACT_LOGS: NxLogEvent[] = [
  { id: 'lg-1', at: '21:04:05.102', level: 'info', line: 10, message: 'input.resolved · html=42.8 KB · currency=CNY' },
  { id: 'lg-2', at: '21:04:05.681', level: 'warn', line: 16, message: 'selector.miss · data-price 未命中，切换到兜底正则' },
  { id: 'lg-3', at: '21:04:05.704', level: 'warn', line: 18, message: 'self_fix.applied · 锁定段 3-7 未改动，仅追加兜底分支' },
  { id: 'lg-4', at: '21:04:06.118', level: 'info', line: 24, message: '共提取 12 条价格 · 2 条异常已标记' },
  { id: 'lg-5', at: '21:04:06.129', level: 'pass', line: 25, message: 'output.valid · rows=12 · count=12' },
]

const FETCH_CODE = `# atom: http_get_page@2 · registered
# 只读实现；运行时由 registry 按 pinned ref 解析
import re
from panguverse.atoms import http_get_page

def run(ctx):
    resp = http_get_page(url=ctx.inputs["url"], timeout_s=20)
    ctx.log(f"HTTP {resp.status_code}")
    return {"html": resp.text, "status": resp.status_code}`

const FETCH_LOGS: NxLogEvent[] = [
  { id: 'fl-1', at: '21:04:03.004', level: 'info', line: 9, message: 'input.resolved · url=https://shop-b.example/prices' },
  { id: 'fl-2', at: '21:04:03.622', level: 'pass', line: 10, message: 'HTTP 200 · 42.8 KB · 312 ms' },
]

const REPORT_CODE = `# atom: llm_summarize@1 · registered
# 只读实现；提示词与输出契约由原子版本固定
from panguverse.atoms import llm_summarize

def run(ctx):
    report = llm_summarize(items=ctx.inputs["rows"], template="price_compare")
    ctx.log("report.generated")
    return {"report": report.text, "summary": report.summary}`

const REPORT_LOGS: NxLogEvent[] = [
  { id: 'rl-1', at: '21:04:07.010', level: 'info', line: 8, message: 'input.resolved · rows=12 · template=price_compare' },
  { id: 'rl-2', at: '21:04:08.442', level: 'pass', line: 10, message: 'report.generated · 1.4 KB · 2 个异常项' },
]

const NOTIFY_CODE = `# AI 草稿 · 飞书通知（draft）
# rev 3 · 锁定段 3-5 · 自动修复 1 次
from panguverse.sdk import NodeContext

def run(ctx: NodeContext) -> dict:
    webhook = ctx.inputs["webhook"]
    text = ctx.inputs["text"]
    ctx.log("准备推送飞书卡片")
    result = ctx.call_atom("http_request", {"method": "POST", "url": webhook, "json": {"text": text}})
    return {"ok": result.get("status") == 200}`

const NOTIFY_LOGS: NxLogEvent[] = [
  { id: 'nl-1', at: '21:04:09.001', level: 'warn', line: 9, message: 'capability.network · 外部写需要 L1 批准' },
  { id: 'nl-2', at: '21:04:09.004', level: 'info', line: 11, message: '等待人工确认后继续' },
]

export const WORKBENCH_NODES: NxNodeMeta[] = [
  {
    id: 'node-fetch',
    name: '抓取竞品价格页',
    atom: 'http_get_page@2',
    custom: false,
    locked: true,
    rev: 4,
    autoFixes: 0,
    tests: { passed: 8, total: 8 },
    contract: {
      inputs: [{ name: 'url', type: 'string', required: true, source: 'step-1.url' }, { name: 'timeout_s', type: 'int', source: 'default 20' }],
      outputs: [{ name: 'html', type: 'string' }, { name: 'status', type: 'int' }],
      capabilities: ['network'],
      sideEffect: 'read',
      idempotency: 'yes',
      risk: 'L0',
      timeout: '20s',
    },
    code: FETCH_CODE,
    logs: FETCH_LOGS,
    note: '已注册原子 · 实现只读',
  },
  {
    id: 'node-extract-prices',
    name: '提取 SKU 与价格',
    atom: 'extract_prices',
    custom: true,
    locked: false,
    rev: 7,
    autoFixes: 2,
    tests: { passed: 12, total: 12 },
    contract: {
      inputs: [{ name: 'html', type: 'string', required: true, source: 'node-fetch.html' }, { name: 'currency', type: 'enum', source: 'default CNY' }],
      outputs: [{ name: 'rows', type: 'array<object>' }, { name: 'count', type: 'int' }],
      capabilities: [],
      sideEffect: 'none',
      idempotency: 'yes',
      risk: 'L0',
      timeout: '15s',
    },
    code: EXTRACT_CODE_V7,
    logs: EXTRACT_LOGS,
    note: '自定义实现 · 可编辑 · 尚未注册',
  },
  {
    id: 'node-report',
    name: '生成对比报告',
    atom: 'llm_summarize@1',
    custom: false,
    locked: true,
    rev: 4,
    autoFixes: 0,
    tests: { passed: 10, total: 10 },
    contract: {
      inputs: [{ name: 'rows', type: 'array<object>', required: true, source: 'node-extract-prices.rows' }, { name: 'template', type: 'string', source: 'price_compare' }],
      outputs: [{ name: 'report', type: 'string' }, { name: 'summary', type: 'object' }],
      capabilities: ['llm'],
      sideEffect: 'none',
      idempotency: 'yes',
      risk: 'L0',
      timeout: '45s',
    },
    code: REPORT_CODE,
    logs: REPORT_LOGS,
    note: '已注册原子 · 实现只读',
  },
  {
    id: 'node-notify',
    name: '推送飞书提醒',
    atom: 'notify_feishu',
    custom: true,
    locked: false,
    rev: 3,
    autoFixes: 1,
    tests: { passed: 6, total: 6 },
    contract: {
      inputs: [{ name: 'webhook', type: 'string', required: true, source: 'env.FEISHU_WEBHOOK' }, { name: 'text', type: 'string', required: true, source: 'node-report.report' }],
      outputs: [{ name: 'ok', type: 'bool' }],
      capabilities: ['network'],
      sideEffect: 'write',
      idempotency: 'no',
      risk: 'L1',
      timeout: '20s',
    },
    code: NOTIFY_CODE,
    logs: NOTIFY_LOGS,
    note: '自定义实现 · 可编辑 · 外部写',
  },
]

export const WORKBENCH_TESTS = [
  { id: 'test-contract', name: '输出契约测试', detail: 'rows 为数组，count 等于 len(rows)', status: 'pass' as const, duration: '0.8s' },
  { id: 'test-replay', name: '确定性重放', detail: '同输入 + 同 rev → 输出哈希一致', status: 'pass' as const, duration: '1.1s' },
  { id: 'test-task', name: '整任务调试', detail: '从上游节点运行到当前节点，5/5 通过', status: 'pass' as const, duration: '0.4s' },
  { id: 'test-empty', name: '边界：空 HTML', detail: '返回 rows=[] / count=0，不抛异常', status: 'pass' as const, duration: '0.1s' },
]

export interface NxDiff {
  id: string
  rev: number
  at: string
  author: 'AI' | '人工'
  summary: string
  lines: string
}

export const EXTRACT_DIFFS: NxDiff[] = [
  { id: 'diff-7', rev: 7, at: '今天 20:58', author: 'AI', summary: '加入 ¥ 兜底正则，保持锁定段 3-7 不变', lines: '16-18' },
  { id: 'diff-6', rev: 6, at: '今天 20:54', author: 'AI', summary: '按站点拆分 SKU 提取，过滤空价格', lines: '12-14' },
  { id: 'diff-5', rev: 5, at: '今天 20:47', author: '人工', summary: '补充 CURRENCY_RE，保留默认币种 CNY', lines: '7, 22-23' },
]

export const EVIDENCE_BY_NODE: Record<string, { run: string; rev: number; duration: string; inputHash: string; artifacts: string[]; checks: string[] }> = {
  'node-fetch': { run: 'run-9f2c14', rev: 4, duration: '312 ms', inputHash: 'sha256:4a91…c02f', artifacts: ['page-b.html'], checks: ['HTTP 200', 'html 非空', '能力 network 已授权'] },
  'node-extract-prices': { run: 'run-9f2c14', rev: 7, duration: '1.03 s', inputHash: 'sha256:9d2e…77ab', artifacts: ['prices.csv', 'extract.trace.json'], checks: ['rows=12', 'count=12', '2 个异常已标黄', '输出哈希可重放'] },
  'node-report': { run: 'run-9f2c14', rev: 4, duration: '1.42 s', inputHash: 'sha256:10be…8cc1', artifacts: ['price_compare.md'], checks: ['模板 price_compare', '摘要字段完整', '无密钥泄漏'] },
  'node-notify': { run: 'run-9f2c14', rev: 3, duration: '等待批准', inputHash: 'pending', artifacts: [], checks: ['L1 待批准', 'webhook 已打码'] },
}

export function codeWithHeader(code: string, nodeName: string, rev: number, autoFixes: number): string {
  const lines = code.split('\n')
  const isDraft = lines[0]?.startsWith('# AI 草稿')
  if (!isDraft) return code
  lines[0] = `# AI 草稿 · ${nodeName}（draft）`
  lines[1] = `# rev ${rev} · 锁定段 3-7 · 自动修复 ${autoFixes} 次`
  return lines.join('\n')
}
