import type {
  AnyRecord,
  ExploreArtifact,
  ExploreCheck,
  ExploreCheckSpec,
  ExploreCrystallizeResult,
  ExploreEvent,
  ExploreHistoryItem,
  ExploreSession,
  ExploreStartResult,
} from './types'

/**
 * 前端演示用探索数据：仅在后端 /api/studio/explore 尚未就绪（404）时启用。
 * 真实后端就绪后页面会自动切回真实接口，不会污染任何持久化数据。
 */

interface MockExploreState {
  eid: string
  created: number
  intent: string
  criteria: ExploreCheckSpec[]
  artifacts: ExploreArtifact[]
}

const store = new Map<string, MockExploreState>()
let counter = 0

const DEMO_HTML = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>百度热搜（演示）</title></head>
<body style="font-family:system-ui;margin:0;background:#0b1220;color:#dbe7ff">
  <main style="padding:28px">
    <h1 style="font-size:22px;margin:0 0 14px">百度热搜 · 演示产物</h1>
    <ol style="line-height:2">
      <li>今日热点：国产大模型发布新版本</li>
      <li>多地迎来降温天气</li>
      <li>年度科技大会开幕</li>
      <li>新能源汽车销量创新高</li>
      <li>城市马拉松周末开跑</li>
    </ol>
    <p style="color:#8fa3c4;font-size:12px">此文件为 mock 产物，用于验证 iframe 沙箱预览。</p>
  </main>
</body></html>`

const DEMO_JSON = JSON.stringify(
  {
    seq: 1,
    tool: 'web_open',
    params: { url: 'https://top.baidu.com/board?tab=realtime' },
    result: { status: 200, title: '百度热搜', html_bytes: 163747, final_url: 'https://top.baidu.com/board?tab=realtime' },
    note: 'mock artifact',
  },
  null,
  2,
)

const DEMO_TEXT = `热点条目: 5
1. 今日热点：国产大模型发布新版本
2. 多地迎来降温天气
3. 年度科技大会开幕
4. 新能源汽车销量创新高
5. 城市马拉松周末开跑
`

function defaultCriteria(): ExploreCheckSpec[] {
  return [
    { id: 'titles>=3', type: 'text_contains', value: '热搜' },
    { id: 'status-200', type: 'status_code', equals: 200 },
    { id: 'page-saved', type: 'file_exists', path: 'artifacts/001_web_open_html.html' },
  ]
}

function makeChecks(criteria: ExploreCheckSpec[]): ExploreCheck[] {
  const specs = criteria.length ? criteria : defaultCriteria()
  return specs.map((spec, index) => {
    const id = String(spec.id || `${spec.type}-${index + 1}`)
    if (spec.type === 'status_code') {
      const expected = Number(spec.equals ?? 200)
      return { id, type: spec.type, spec, ok: expected === 200, evidence: `status=200，期望 ${expected}` }
    }
    if (spec.type === 'regex') {
      return { id, type: spec.type, spec, ok: true, evidence: `正则命中：${spec.pattern || '(空)'}` }
    }
    if (spec.type === 'file_exists') {
      return { id, type: spec.type, spec, ok: true, evidence: `${spec.path || 'artifacts/001_web_open.html'} 存在（163747 bytes）` }
    }
    return {
      id,
      type: spec.type,
      spec,
      ok: true,
      evidence: `文本包含 ${JSON.stringify(spec.value || '热搜')}：…今日热点：国产大模型发布新版本…`,
    }
  })
}

function event(seq: number, offset: number, type: string, payload: AnyRecord, created: number): ExploreEvent {
  return { seq, ts: created + offset / 1000, type, ...payload }
}

function timeline(state: MockExploreState): ExploreEvent[] {
  const checks = makeChecks(state.criteria)
  return [
    event(1, 200, 'thought', { text: '先打开百度热搜页，确认目标页面真实可访问，再提取正文。' }, state.created),
    event(2, 500, 'tool_call', { tool: 'web_open', step: 1, params: { url: 'https://top.baidu.com/board?tab=realtime' } }, state.created),
    event(
      3,
      1400,
      'tool_result',
      {
        tool: 'web_open',
        ok: true,
        digest: { keys: ['html', 'status', 'title', 'url'], status: 200, title: '百度热搜', html_bytes: 163747, text_chars: 0 },
        artifacts: ['artifacts/001_web_open.json', 'artifacts/001_web_open_html.html'],
      },
      state.created,
    ),
    event(4, 1700, 'thought', { text: '页面可访问。提取正文文本，并统计热搜标题数量。' }, state.created),
    event(5, 2000, 'tool_call', { tool: 'web_get_text', step: 2, params: { url: 'https://top.baidu.com/board?tab=realtime', max_chars: 4000 } }, state.created),
    event(
      6,
      2600,
      'tool_result',
      {
        tool: 'web_get_text',
        ok: true,
        digest: { keys: ['text', 'title'], text_chars: 1832, text_head: '今日热点：国产大模型发布新版本 …', title: '百度热搜' },
        artifacts: ['artifacts/002_web_get_text.json'],
      },
      state.created,
    ),
    event(
      7,
      3200,
      'script',
      {
        step: 3,
        path: 'scripts/parse_hot.py',
        code_sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
        exit_code: 0,
        ok: true,
        duration_ms: 420,
        stdout: DEMO_TEXT,
        stderr: '',
        artifacts: ['artifacts/parse_hot_stdout.txt'],
      },
      state.created,
    ),
    event(8, 4000, 'verify', { checks, verdict: 'passed' }, state.created),
    event(
      9,
      4200,
      'end',
      {
        status: 'succeeded',
        verdict: 'passed',
        checks,
        summary: { steps: 4, tool_calls: 2, scripts: 1, duration_ms: 4200, verdict: 'passed' },
      },
      state.created,
    ),
  ]
}

function baseArtifacts(): ExploreArtifact[] {
  return [
    { path: 'artifacts/001_web_open.json', bytes: DEMO_JSON.length, content_type: 'application/json' },
    { path: 'artifacts/001_web_open_html.html', bytes: DEMO_HTML.length, content_type: 'text/html' },
    { path: 'artifacts/002_web_get_text.json', bytes: 512, content_type: 'application/json' },
    { path: 'artifacts/parse_hot_stdout.txt', bytes: DEMO_TEXT.length, content_type: 'text/plain' },
  ]
}

export function mockStartExplore(body: {
  intent: string
  success_criteria?: ExploreCheckSpec[]
}): ExploreStartResult {
  counter += 1
  const eid = `e-mock${String(counter).padStart(4, '0')}`
  const state: MockExploreState = {
    eid,
    created: Math.floor(Date.now() / 1000),
    intent: body.intent,
    criteria: Array.isArray(body.success_criteria) ? body.success_criteria : [],
    artifacts: baseArtifacts(),
  }
  store.set(eid, state)
  return { eid, status: 'running', cursor: 0 }
}

export function mockGetSession(eid: string, cursor = 0): ExploreSession {
  const state = store.get(eid)
  if (!state) throw new Error(`演示探索不存在: ${eid}`)
  const now = Date.now() / 1000
  const all = timeline(state)
  const visible = all.filter((item) => item.ts <= now)
  const pending = visible.filter((item) => item.seq > cursor)
  const end = visible.find((item) => item.type === 'end')
  const verify = visible.find((item) => item.type === 'verify')
  const checks = (verify?.checks as ExploreCheck[]) || []
  const done = Boolean(end)
  const artifacts = state.artifacts.filter((artifact) => {
    const seq = Number(artifact.path.slice('artifacts/'.length, 'artifacts/'.length + 3))
    const seen = visible.some((item) => Array.isArray(item.artifacts) && item.artifacts.includes(artifact.path))
    return Number.isFinite(seq) ? seen : done
  })
  return {
    eid,
    cursor: pending.length ? Math.max(...pending.map((item) => item.seq)) : cursor,
    status: done ? 'succeeded' : 'running',
    done,
    intent: state.intent,
    criteria: state.criteria,
    summary: (end?.summary as ExploreSession['summary']) || {},
    checks,
    verdict: (end?.verdict as string) || '',
    artifacts,
    events: pending,
    created_at: state.created,
    finished_at: done ? state.created + 4.2 : undefined,
  }
}

export function mockArtifact(eid: string, path: string): { contentType: string; body: string } {
  if (!store.has(eid)) throw new Error(`演示探索不存在: ${eid}`)
  if (path.endsWith('.html')) return { contentType: 'text/html; charset=utf-8', body: DEMO_HTML }
  if (path.endsWith('.txt')) return { contentType: 'text/plain; charset=utf-8', body: DEMO_TEXT }
  if (path.endsWith('002_web_get_text.json')) {
    return {
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ tool: 'web_get_text', text: DEMO_TEXT, text_chars: DEMO_TEXT.length }, null, 2),
    }
  }
  return { contentType: 'application/json; charset=utf-8', body: DEMO_JSON }
}

export function mockCrystallize(eid: string): ExploreCrystallizeResult {
  const state = store.get(eid)
  const checks = makeChecks(state?.criteria || [])
  return {
    wid: `wf-demo-${eid.slice(-4)}`,
    rev: 1,
    explore_id: eid,
    atoms_created: [],
    pending_atoms: ['parse_hot'],
    replay: { status: 'passed', checks, errors: [], duration_ms: 1860 },
    replay_status: 'replay_passed',
    validation: {},
    state: 'draft',
  }
}

export function mockHistory(): ExploreHistoryItem[] {
  return Array.from(store.values())
    .map((state) => {
      const session = mockGetSession(state.eid, 9999)
      return {
        eid: state.eid,
        intent: state.intent,
        status: session.status,
        verdict: session.verdict,
        created_at: state.created,
        finished_at: session.finished_at,
        summary: session.summary,
      }
    })
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0))
}
