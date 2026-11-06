import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, exploreApi } from '../shared/api'
import {
  mockArtifact,
  mockCrystallize,
  mockGetSession,
  mockHistory,
  mockStartExplore,
} from '../shared/exploreMock'
import { href, navigate } from '../shared/router'
import { relativeTime } from '../shared/utils'
import {
  atomContracts,
  contractText,
  effectsText,
  pendingAtoms,
  writeNewAtomCache,
} from '../shared/atomContracts'
import type {
  AnyRecord,
  ExploreArtifact,
  ExploreCheck,
  ExploreCheckSpec,
  ExploreCheckType,
  ExploreCrystallizeResult,
  ExploreEvent,
  ExploreHistoryItem,
  ExploreSession,
} from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  EmptyState,
  Loading,
  PageHeader,
  useToast,
} from '../components/ui'

/* -------------------------------------------------------------------------- */
/* 常量与小工具                                                               */
/* -------------------------------------------------------------------------- */

const CHECK_TYPES: { value: ExploreCheckType; label: string; hint: string }[] = [
  { value: 'text_contains', label: '文本包含', hint: '页面/输出文本包含指定内容' },
  { value: 'status_code', label: '状态码', hint: '某一步 HTTP 状态码等于期望值' },
  { value: 'regex', label: '正则匹配', hint: '文本匹配正则表达式' },
  { value: 'file_exists', label: '文件存在', hint: '确认产物/脚本输出文件已生成' },
]

const typeLabel: Record<string, string> = {
  thought: '思考',
  tool_call: '工具调用',
  tool_result: '工具结果',
  script: '脚本',
  verify: '确定性校验',
  end: '结束',
}

const typeTone: Record<string, 'violet' | 'run' | 'ok' | 'warn' | 'cyan' | 'neutral' | 'bad'> = {
  thought: 'violet',
  tool_call: 'run',
  tool_result: 'ok',
  script: 'warn',
  verify: 'cyan',
  end: 'neutral',
}

const checkTypeLabel: Record<string, string> = {
  text_contains: '文本包含',
  status_code: '状态码',
  regex: '正则',
  file_exists: '文件存在',
}

interface CriterionDraft {
  key: number
  type: ExploreCheckType
  value: string
  equals: string
  pattern: string
  path: string
}

interface ArtifactState {
  loading: boolean
  error: string
  text: string
  contentType: string
  image: boolean
  url: string
}

const emptyArtifact: ArtifactState = {
  loading: false,
  error: '',
  text: '',
  contentType: '',
  image: false,
  url: '',
}

function statusTone(status: string | undefined): 'ok' | 'bad' | 'warn' | 'run' | 'dim' {
  if (!status) return 'dim'
  if (status === 'succeeded' || status === 'passed') return 'ok'
  if (status === 'budget_exceeded') return 'warn'
  if (status === 'failed' || status === 'error') return 'bad'
  if (status === 'running') return 'run'
  return 'dim'
}

function statusText(status: string | undefined): string {
  const map: Record<string, string> = {
    running: '探索中',
    succeeded: '已成功',
    failed: '未通过',
    budget_exceeded: '预算超限',
    error: '异常中断',
    passed: '已通过',
  }
  return map[status || ''] || status || '未知'
}

function fmtTime(ts: number | undefined): string {
  if (!ts) return '—'
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleTimeString('zh-CN', { hour12: false })
}

function fmtDuration(ms: unknown): string {
  const value = Number(ms)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60_000) return `${(value / 1000).toFixed(1)}s`
  return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1000)}s`
}

function fmtBytes(bytes: unknown): string {
  const value = Number(bytes)
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function pretty(value: unknown, max = 12_000): string {
  if (value === undefined || value === null || value === '') return ''
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max)}\n…[truncated]` : text
}

function specSummary(check: ExploreCheck | ExploreCheckSpec): string {
  const spec = (check as ExploreCheck).spec || (check as ExploreCheckSpec)
  const id = String((check as ExploreCheck).id || spec.id || spec.type || '')
  if (spec.type === 'text_contains') return `${id} · 文本包含 ${JSON.stringify(spec.value || '')}`
  if (spec.type === 'status_code') return `${id} · 状态码 = ${spec.equals ?? 200}`
  if (spec.type === 'regex') return `${id} · 正则 ${spec.pattern || ''}`
  if (spec.type === 'file_exists') return `${id} · 文件 ${spec.path || ''}`
  return `${id} · ${spec.type || '检查'}`
}

function eventSummary(event: ExploreEvent): string {
  if (event.type === 'thought') return String(event.text || '')
  if (event.type === 'tool_call') {
    const params = event.params && typeof event.params === 'object' ? event.params : {}
    const url = (params as AnyRecord).url || (params as AnyRecord).path
    return `${event.tool || 'tool'}${url ? ` · ${String(url)}` : ''}`
  }
  if (event.type === 'tool_result') {
    const digest = (event.digest || {}) as AnyRecord
    const bits = [
      event.ok ? '成功' : '失败',
      digest.title ? String(digest.title) : '',
      digest.status !== undefined ? `status ${digest.status}` : '',
      digest.text_chars ? `${digest.text_chars} 字符` : '',
      event.error ? String(event.error) : '',
    ].filter(Boolean)
    return `${event.tool || 'tool'} · ${bits.join(' · ')}`
  }
  if (event.type === 'script') {
    return `${event.path || 'script'} · exit ${event.exit_code ?? '?'} · ${fmtDuration(event.duration_ms)}`
  }
  if (event.type === 'verify') {
    const checks = Array.isArray(event.checks) ? event.checks : []
    return `${checks.filter((item: ExploreCheck) => item.ok).length}/${checks.length} 通过 · ${event.verdict === 'passed' ? '可固化' : '未通过'}`
  }
  if (event.type === 'end') {
    const summary = (event.summary || {}) as AnyRecord
    return `${statusText(String(event.status || ''))}${summary.reason ? ` · ${String(summary.reason)}` : ''}`
  }
  return String(event.text || event.type)
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                       */
/* -------------------------------------------------------------------------- */

export function StudioExplorePage({ eid: routeEid }: { eid?: string }) {
  const { push } = useToast()
  const [intent, setIntent] = useState('')
  const [criteria, setCriteria] = useState<CriterionDraft[]>([])
  const [maxSteps, setMaxSteps] = useState('12')
  const [allowScripts, setAllowScripts] = useState(true)
  const [formError, setFormError] = useState('')
  const [llmMissing, setLlmMissing] = useState('')
  const [starting, setStarting] = useState(false)

  const [mockMode, setMockMode] = useState(false)
  const [session, setSession] = useState<ExploreSession | null>(null)
  const [events, setEvents] = useState<ExploreEvent[]>([])
  const [pollError, setPollError] = useState('')
  const [polling, setPolling] = useState(false)
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null)
  const [follow, setFollow] = useState(true)
  const [selectedArtifact, setSelectedArtifact] = useState('')
  const [artifact, setArtifact] = useState<ArtifactState>(emptyArtifact)
  const [showHtmlSource, setShowHtmlSource] = useState(false)
  const [crystallizing, setCrystallizing] = useState(false)
  const [crystallizeResult, setCrystallizeResult] = useState<ExploreCrystallizeResult | null>(null)
  const [crystallizeError, setCrystallizeError] = useState('')
  const [history, setHistory] = useState<ExploreHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState('')
  const [historyTick, setHistoryTick] = useState(0)

  const cursorRef = useRef(0)
  const nextKeyRef = useRef(1)
  const autoNavRef = useRef(0)
  const eid = routeEid || ''
  const newAtoms = useMemo(() => atomContracts(crystallizeResult?.atoms_created), [crystallizeResult])
  const pending = useMemo(() => pendingAtoms(crystallizeResult?.pending_atoms), [crystallizeResult])

  const newCriterion = (type: ExploreCheckType = 'text_contains'): CriterionDraft => {
    const key = nextKeyRef.current
    nextKeyRef.current += 1
    return { key, type, value: '', equals: '200', pattern: '', path: '' }
  }

  /* ---------------------------------- 生命周期 --------------------------- */

  useEffect(() => {
    setSession(null)
    setEvents([])
    setSelectedSeq(null)
    setFollow(true)
    setSelectedArtifact('')
    setArtifact(emptyArtifact)
    setCrystallizeResult(null)
    setCrystallizeError('')
    setPollError('')
    cursorRef.current = 0
  }, [eid])

  useEffect(() => () => window.clearTimeout(autoNavRef.current), [])

  /* ---------------------------------- 历史 ------------------------------- */

  useEffect(() => {
    let cancelled = false
    setHistoryLoading(true)
    setHistoryError('')
    const load = async () => {
      try {
        const list = mockMode ? mockHistory() : await exploreApi.list(30)
        if (!cancelled) setHistory(list)
      } catch (error) {
        if (cancelled) return
        if (error instanceof ApiError && error.status === 404) {
          setMockMode(true)
          setHistory(mockHistory())
        } else {
          setHistoryError(error instanceof Error ? error.message : '读取历史探索失败')
        }
      } finally {
        if (!cancelled) setHistoryLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [mockMode, historyTick])

  /* ---------------------------------- 轮询 ------------------------------- */

  useEffect(() => {
    if (!eid) return
    let cancelled = false
    let timer = 0
    let failures = 0
    setPolling(true)
    setPollError('')

    const tick = async () => {
      try {
        const next = mockMode
          ? mockGetSession(eid, cursorRef.current)
          : await exploreApi.get(eid, cursorRef.current)
        if (cancelled) return
        failures = 0
        cursorRef.current = Number(next.cursor ?? cursorRef.current)
        setSession((previous) => ({ ...(previous || {}), ...next, events: [] }))
        const incoming = Array.isArray(next.events) ? next.events : []
        if (incoming.length) {
          setEvents((previous) => {
            const seen = new Set(previous.map((item) => item.seq))
            const merged = [...previous, ...incoming.filter((item) => !seen.has(item.seq))]
            merged.sort((a, b) => a.seq - b.seq)
            return merged
          })
        }
        if (next.done) {
          setPolling(false)
          setHistoryTick((value) => value + 1)
          return
        }
        timer = window.setTimeout(tick, 900)
      } catch (error) {
        if (cancelled) return
        failures += 1
        if (failures >= 3) {
          setPolling(false)
          setPollError(error instanceof Error ? error.message : '读取探索状态失败')
          return
        }
        timer = window.setTimeout(tick, 1600)
      }
    }
    tick()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [eid, mockMode])

  /* ------------------------------- 选中与产物 ----------------------------- */

  useEffect(() => {
    if (!events.length) return
    if (follow) setSelectedSeq(events[events.length - 1].seq)
  }, [events, follow])

  const artifacts = session?.artifacts || []
  useEffect(() => {
    if (!artifacts.length) {
      if (!selectedArtifact) return
      setSelectedArtifact('')
      return
    }
    if (!selectedArtifact || !artifacts.some((item) => item.path === selectedArtifact)) {
      setSelectedArtifact(artifacts[0].path)
    }
  }, [artifacts, selectedArtifact])

  const activeArtifact = useMemo(
    () => artifacts.find((item) => item.path === selectedArtifact) || null,
    [artifacts, selectedArtifact],
  )
  const activeArtifactPath = activeArtifact?.path || ''
  const activeArtifactType = String(activeArtifact?.content_type || '')

  useEffect(() => {
    setShowHtmlSource(false)
    if (!eid || !selectedArtifact || !activeArtifactPath) {
      setArtifact(emptyArtifact)
      return
    }
    const contentType = activeArtifactType
    const isImage = contentType.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(selectedArtifact)
    if (isImage) {
      setArtifact({
        loading: false,
        error: '',
        text: '',
        contentType,
        image: true,
        url: exploreApi.artifactUrl(eid, selectedArtifact),
      })
      return
    }
    let cancelled = false
    setArtifact({ ...emptyArtifact, loading: true, contentType })
    const load = async () => {
      try {
        let text = ''
        let ct = contentType
        if (mockMode) {
          const result = mockArtifact(eid, selectedArtifact)
          text = result.body
          ct = result.contentType
        } else {
          const response = await fetch(exploreApi.artifactUrl(eid, selectedArtifact), { credentials: 'same-origin' })
          if (!response.ok) throw new Error(`读取产物失败（HTTP ${response.status}）`)
          ct = response.headers.get('content-type') || contentType
          text = await response.text()
        }
        if (cancelled) return
        setArtifact({ loading: false, error: '', text, contentType: ct, image: false, url: '' })
      } catch (error) {
        if (cancelled) return
        setArtifact({
          loading: false,
          error: error instanceof Error ? error.message : '读取产物失败',
          text: '',
          contentType,
          image: false,
          url: '',
        })
      }
    }
    load()
    return () => { cancelled = true }
  }, [eid, selectedArtifact, activeArtifactPath, activeArtifactType, mockMode])

  /* ------------------------------- 操作 ---------------------------------- */

  const buildSpecs = useCallback((): { specs: ExploreCheckSpec[]; error: string } => {
    const specs: ExploreCheckSpec[] = []
    for (let index = 0; index < criteria.length; index += 1) {
      const row = criteria[index]
      const id = `${row.type}-${index + 1}`
      if (row.type === 'text_contains') {
        if (!row.value.trim()) return { specs: [], error: `检查 #${index + 1}：请填写要包含的文本` }
        specs.push({ id, type: row.type, value: row.value.trim() })
      } else if (row.type === 'status_code') {
        const equals = Number(row.equals)
        if (!Number.isFinite(equals)) return { specs: [], error: `检查 #${index + 1}：状态码必须是整数` }
        specs.push({ id, type: row.type, equals })
      } else if (row.type === 'regex') {
        if (!row.pattern.trim()) return { specs: [], error: `检查 #${index + 1}：请填写正则表达式` }
        specs.push({ id, type: row.type, pattern: row.pattern.trim() })
      } else if (row.type === 'file_exists') {
        if (!row.path.trim()) return { specs: [], error: `检查 #${index + 1}：请填写文件相对路径` }
        specs.push({ id, type: row.type, path: row.path.trim() })
      }
    }
    return { specs, error: '' }
  }, [criteria])

  const submit = async () => {
    const text = intent.trim()
    if (!text) {
      setFormError('先写一句要探索的意图，例如：打开百度热搜并确认至少有三条标题。')
      return
    }
    const { specs, error } = buildSpecs()
    if (error) {
      setFormError(error)
      return
    }
    const steps = Number(maxSteps)
    if (!Number.isFinite(steps) || steps < 1 || steps > 50) {
      setFormError('最大步数必须是 1–50 的整数')
      return
    }
    setFormError('')
    setLlmMissing('')
    setStarting(true)
    try {
      const body = {
        intent: text,
        success_criteria: specs.length ? specs : undefined,
        max_steps: Math.round(steps),
        allow_scripts: allowScripts,
      }
      let started: { eid: string; status: string; cursor?: number }
      try {
        started = await exploreApi.start(body)
      } catch (error_) {
        if (error_ instanceof ApiError && error_.status === 404) {
          setMockMode(true)
          started = mockStartExplore({ intent: text, success_criteria: specs })
        } else {
          throw error_
        }
      }
      setSession({
        eid: started.eid,
        status: 'running',
        done: false,
        intent: text,
        criteria: specs,
        max_steps: Math.round(steps),
        allow_scripts: allowScripts,
        summary: {},
        checks: [],
        artifacts: [],
        events: [],
      })
      setEvents([])
      cursorRef.current = 0
      setHistoryTick((value) => value + 1)
      push(`探索已启动 · ${started.eid}`, 'ok')
      navigate(`/studio/explore/${started.eid}`)
    } catch (error_) {
      if (error_ instanceof ApiError && (error_.status === 503 || error_.code === 'llm_required')) {
        setLlmMissing(error_.message || '探索需要配置 LLM。')
      } else {
        setFormError(error_ instanceof Error ? error_.message : '启动探索失败')
      }
    } finally {
      setStarting(false)
    }
  }

  const crystallize = async () => {
    if (!eid) return
    setCrystallizing(true)
    setCrystallizeError('')
    try {
      const result = mockMode
        ? mockCrystallize(eid)
        : await exploreApi.crystallize(eid, { include_scripts: true })
      setCrystallizeResult(result)
      writeNewAtomCache(result.wid, result.rev, atomContracts(result.atoms_created))
      setHistoryTick((value) => value + 1)
      push(`已固化草稿 ${result.wid}`, 'ok')
      window.clearTimeout(autoNavRef.current)
      autoNavRef.current = window.setTimeout(() => navigate(`/studio/${result.wid}`), 2600)
    } catch (error) {
      setCrystallizeError(error instanceof Error ? error.message : '固化失败')
    } finally {
      setCrystallizing(false)
    }
  }

  const openCanvas = () => {
    if (!crystallizeResult) return
    window.clearTimeout(autoNavRef.current)
    navigate(`/studio/${crystallizeResult.wid}`)
  }

  /* ------------------------------- 派生数据 ------------------------------- */

  const checks = useMemo<ExploreCheck[]>(() => {
    if (session?.checks?.length) return session.checks
    const verify = [...events].reverse().find((item) => item.type === 'verify')
    return Array.isArray(verify?.checks) ? (verify.checks as ExploreCheck[]) : []
  }, [session, events])

  const verdict = String(session?.verdict || [...events].reverse().find((item) => item.type === 'verify')?.verdict || '')
  const status = String(session?.status || (eid ? 'running' : ''))

  const summary = useMemo(() => {
    const raw = (session?.summary || {}) as AnyRecord
    const toolCalls = Number(raw.tool_calls ?? events.filter((item) => item.type === 'tool_call').length)
    const scripts = Number(raw.scripts ?? events.filter((item) => item.type === 'script').length)
    const steps = Number(raw.steps ?? events.filter((item) => item.type === 'tool_call' || item.type === 'script').length)
    let duration = raw.duration_ms
    if (duration === undefined && session?.finished_at && session?.created_at) {
      duration = (Number(session.finished_at) - Number(session.created_at)) * 1000
    }
    return { steps, toolCalls, scripts, duration, reason: String(raw.reason || '') }
  }, [session, events])

  const failureReason = String(
    (session?.error && (session.error as AnyRecord).message) || summary.reason || '',
  )

  const selected = useMemo(
    () => events.find((item) => item.seq === selectedSeq) || null,
    [events, selectedSeq],
  )

  const relatedCall = useMemo(() => {
    if (!selected || selected.type !== 'tool_result') return null
    return [...events]
      .reverse()
      .find((item) => item.seq < selected.seq && item.type === 'tool_call' && item.tool === selected.tool) || null
  }, [events, selected])

  const runtimeEvent = selected?.type === 'tool_result' && relatedCall ? relatedCall : selected

  /* ---------------------------------- 渲染 -------------------------------- */

  return (
    <div className="page explore-page" data-testid="explore-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 工作台 · 先跑通再固化</>}
        title={<>探索模式：<span className="text-gradient">让 AI 真的跑一遍</span></>}
        description="AI 在沙箱里调用原子或写脚本，直到确定性校验通过；成功轨迹再固化为可重放的流程。LLM 自述不算成功。"
        actions={
          <>
            <Button size="sm" variant="ghost" icon="refresh" onClick={() => setHistoryTick((value) => value + 1)}>刷新历史</Button>
            {eid ? <Button size="sm" variant="soft" icon="plus" onClick={() => navigate('/studio/explore')}>新建探索</Button> : null}
          </>
        }
      />

      {mockMode ? (
        <div className="demo-banner" data-testid="explore-mock-banner">
          <span className="demo-icon"><Icon name="terminal" size={16} /></span>
          <div>
            <strong>探索演示模式</strong>
            <p><code>/api/studio/explore</code> 未就绪（404）。当前时间线与产物由前端 mock 生成，用于验证交互；后端就绪后刷新即自动切回真实接口。</p>
          </div>
          <Badge tone="warn">未联调</Badge>
        </div>
      ) : null}

      {llmMissing ? (
        <div className="explore-alert bad" data-testid="explore-llm-required">
          <span className="explore-alert-icon"><Icon name="warning" size={17} /></span>
          <div>
            <strong>需要配置 LLM</strong>
            <p>{llmMissing}</p>
            <p className="explore-alert-hint">
              在运行 <code>panguverse serve</code> 的环境中设置 <code>PANGUVERSE_API_KEY</code> 或 <code>OPENAI_API_KEY</code>，重启服务后重试。未配置时后端不会伪造执行结果。
            </p>
          </div>
          <Button size="sm" variant="ghost" icon="refresh" onClick={submit} loading={starting}>重试</Button>
        </div>
      ) : null}

      {formError ? (
        <div className="explore-alert warn" data-testid="explore-form-error">
          <span className="explore-alert-icon"><Icon name="warning" size={16} /></span>
          <div><strong>无法开始探索</strong><p>{formError}</p></div>
          <button type="button" className="explore-alert-close" aria-label="关闭" onClick={() => setFormError('')}><Icon name="close" size={13} /></button>
        </div>
      ) : null}

      {!eid ? (
        <section className="panel explore-start" data-testid="explore-start-panel">
          <div className="explore-start-head">
            <div>
              <span className="explore-kicker">01 · 意图</span>
              <h2>要 AI 真的做成什么？</h2>
              <p>描述目标与验收标准。探索会优先使用已有原子，能力不足时在沙箱里写脚本。</p>
            </div>
            <Badge tone="cyan">LLM 驱动 · 确定性校验</Badge>
          </div>

          <textarea
            className="textarea explore-intent"
            value={intent}
            rows={4}
            maxLength={500}
            placeholder="例如：打开百度热搜页，提取今天的热点标题，确认至少包含三条标题。"
            onChange={(event) => setIntent(event.target.value)}
            aria-label="探索意图"
          />

          <div className="explore-criteria-head">
            <div>
              <strong>成功标准</strong>
              <span>必须是可执行检查；留空则由 AI 提议，但仍需落成检查才能通过。</span>
            </div>
            <div className="explore-criteria-add">
              {CHECK_TYPES.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className="explore-add-chip"
                  onClick={() => setCriteria((current) => [...current, newCriterion(item.value)])}
                  title={item.hint}
                >
                  <Icon name="plus" size={11} />{item.label}
                </button>
              ))}
            </div>
          </div>

          <div className="explore-criteria-list">
            {criteria.length ? criteria.map((row, index) => (
              <div className="explore-criterion" key={row.key}>
                <span className="explore-criterion-index">{String(index + 1).padStart(2, '0')}</span>
                <select
                  className="select"
                  value={row.type}
                  onChange={(event) => {
                    const type = event.target.value as ExploreCheckType
                    setCriteria((current) => current.map((item) => item.key === row.key ? { ...item, type } : item))
                  }}
                  aria-label={`检查 ${index + 1} 类型`}
                >
                  {CHECK_TYPES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </select>
                {row.type === 'text_contains' ? (
                  <input
                    className="input"
                    value={row.value}
                    placeholder="要包含的文本，例如：热搜"
                    onChange={(event) => setCriteria((current) => current.map((item) => item.key === row.key ? { ...item, value: event.target.value } : item))}
                  />
                ) : null}
                {row.type === 'status_code' ? (
                  <input
                    className="input"
                    value={row.equals}
                    inputMode="numeric"
                    placeholder="200"
                    onChange={(event) => setCriteria((current) => current.map((item) => item.key === row.key ? { ...item, equals: event.target.value } : item))}
                  />
                ) : null}
                {row.type === 'regex' ? (
                  <input
                    className="input mono"
                    value={row.pattern}
                    placeholder="例如：\\d+\\s*条"
                    onChange={(event) => setCriteria((current) => current.map((item) => item.key === row.key ? { ...item, pattern: event.target.value } : item))}
                  />
                ) : null}
                {row.type === 'file_exists' ? (
                  <input
                    className="input mono"
                    value={row.path}
                    placeholder="artifacts/open.html"
                    onChange={(event) => setCriteria((current) => current.map((item) => item.key === row.key ? { ...item, path: event.target.value } : item))}
                  />
                ) : null}
                <button type="button" className="icon-btn" aria-label={`删除检查 ${index + 1}`} onClick={() => setCriteria((current) => current.filter((item) => item.key !== row.key))}>
                  <Icon name="trash" size={14} />
                </button>
              </div>
            )) : (
              <div className="explore-criteria-empty">
                <Icon name="filter" size={15} />
                还没有成功标准。可以留空（由 AI 提议），或从右上角添加四类可执行检查。
              </div>
            )}
          </div>

          <div className="explore-start-foot">
            <label className="explore-field">
              <span>最大步数</span>
              <input
                className="input"
                value={maxSteps}
                inputMode="numeric"
                onChange={(event) => setMaxSteps(event.target.value)}
                aria-label="最大步数"
              />
            </label>
            <label className="explore-check-toggle">
              <input type="checkbox" checked={allowScripts} onChange={(event) => setAllowScripts(event.target.checked)} />
              <span>允许写脚本（沙箱内 Python）</span>
            </label>
            <div className="explore-start-note">
              <Icon name="shield" size={14} />
              <span>脚本只写入探索工作区；校验在沙箱执行，LLM 不能自证成功。</span>
            </div>
            <Button
              variant="primary"
              size="lg"
              icon="bolt"
              loading={starting}
              onClick={submit}
              data-testid="explore-start"
              disabled={!intent.trim()}
            >
              {starting ? '正在启动…' : '开始探索'}
            </Button>
          </div>
        </section>
      ) : (
        <section className="panel explore-runhead">
          <div className="explore-runhead-main">
            <div className="explore-runhead-line">
              <span className={`badge status-badge tone-${statusTone(status)}`}>
                <span className="status-dot" />
                {statusText(status)}
              </span>
              {polling ? <span className="explore-live"><span className="live-dot" />实时增量</span> : null}
              <span className="eid-chip mono">{eid}</span>
              {session?.model ? <Badge tone="violet">{session.model}</Badge> : null}
            </div>
            <h2>{session?.intent || '正在读取探索会话…'}</h2>
            <p>
              {session?.max_steps ? `最多 ${session.max_steps} 步` : '步数预算未知'}
              <span>·</span>
              {session?.allow_scripts === false ? '禁用脚本' : '允许脚本'}
              <span>·</span>
              {session?.created_at ? `开始于 ${relativeTime(session.created_at)}` : '等待会话元数据'}
              {session?.finished_at ? <><span>·</span>结束于 {relativeTime(session.finished_at)}</> : null}
            </p>
          </div>
          <div className="explore-runhead-actions">
            {verdict === 'passed' ? (
              <Button
                variant="primary"
                icon="git"
                loading={crystallizing}
                onClick={crystallize}
                data-testid="explore-crystallize"
              >
                {crystallizing ? '正在固化…' : '固化为流程'}
              </Button>
            ) : (
              <span className="explore-runhead-hint">
                <Icon name="shield" size={14} />
                {status === 'running' ? '校验通过后才能固化' : '失败/预算超限不能固化'}
              </span>
            )}
          </div>
        </section>
      )}

      {eid ? (
        <>
          {pollError ? (
            <div className="explore-alert bad" data-testid="explore-poll-error">
              <span className="explore-alert-icon"><Icon name="warning" size={16} /></span>
              <div><strong>增量读取中断</strong><p>{pollError}</p></div>
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => setHistoryTick((value) => value + 1)}>重新加载</Button>
            </div>
          ) : null}

          <div className="explore-grid">
            {/* 左栏：事件时间线 */}
            <section className="panel explore-col explore-timeline-panel" aria-label="探索事件时间线">
              <div className="explore-col-head">
                <div><h3>事件时间线</h3><span>{events.length} 条事件</span></div>
                <label className="explore-follow">
                  <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
                  跟随最新
                </label>
              </div>
              <div className="explore-timeline">
                {!events.length ? <Loading label="等待第一步事件…" /> : null}
                {events.map((event) => {
                  const rawTone: any = event.type === 'tool_result'
                    ? (event.ok ? 'ok' : 'bad')
                    : event.type === 'end'
                      ? statusTone(String(event.status || ''))
                      : typeTone[event.type] || 'neutral'
                  const tone: any = rawTone === 'dim' ? 'neutral' : rawTone
                  return (
                    <div
                      key={event.seq}
                      className={`explore-tl-item tone-${tone} ${selectedSeq === event.seq ? 'is-active' : ''}`}
                      data-testid="explore-timeline-item"
                      data-seq={event.seq}
                      data-type={event.type}
                    >
                      <button type="button" className="explore-tl-head" onClick={() => { setFollow(false); setSelectedSeq(event.seq) }}>
                        <span className="explore-tl-dot" aria-hidden="true" />
                        <span className="explore-tl-body">
                          <span className="explore-tl-top">
                            <Badge tone={tone}>{typeLabel[event.type] || event.type}</Badge>
                            <span className="mono">#{event.seq}</span>
                            <time>{fmtTime(event.ts)}</time>
                          </span>
                          <span className="explore-tl-text">{eventSummary(event)}</span>
                        </span>
                        {event.type === 'tool_call' || event.type === 'script' ? <Icon name="chevron" size={13} /> : null}
                      </button>
                      {event.type === 'tool_call' && event.params ? (
                        <details className="explore-tl-more">
                          <summary>入参</summary>
                          <pre>{pretty(event.params)}</pre>
                        </details>
                      ) : null}
                      {event.type === 'tool_result' && event.digest ? (
                        <details className="explore-tl-more">
                          <summary>结果摘要</summary>
                          <pre>{pretty(event.digest)}</pre>
                        </details>
                      ) : null}
                      {event.type === 'script' && event.stdout ? (
                        <details className="explore-tl-more">
                          <summary>stdout</summary>
                          <pre>{pretty(event.stdout)}</pre>
                        </details>
                      ) : null}
                    </div>
                  )
                })}
              </div>
            </section>

            {/* 中栏：当前步骤详情 */}
            <section className="panel explore-col explore-detail-panel" aria-label="当前步骤详情">
              <div className="explore-col-head">
                <div><h3>步骤详情</h3><span>{selected ? `#${selected.seq} · ${typeLabel[selected.type] || selected.type}` : '未选择'}</span></div>
                {selected ? <Badge tone={selected.type === 'tool_result' ? (selected.ok ? 'ok' : 'bad') : typeTone[selected.type] || 'neutral'}>{selected.type}</Badge> : null}
              </div>
              {!selected ? (
                <EmptyState icon="terminal" title="选择一个事件">点击左侧时间线，查看工具入参、输出摘要、错误与耗时。</EmptyState>
              ) : (
                <div className="explore-detail">
                  <div className="explore-detail-title">
                    <strong>{runtimeEvent?.tool || runtimeEvent?.path || typeLabel[selected.type] || selected.type}</strong>
                    <span className="mono">#{selected.seq} · {fmtTime(selected.ts)}</span>
                  </div>

                  {selected.type === 'thought' ? (
                    <p className="explore-thought">{String(selected.text || '')}</p>
                  ) : null}

                  {runtimeEvent?.tool ? (
                    <div className="explore-facts">
                      <div><span>工具</span><strong>{runtimeEvent.tool}</strong></div>
                      <div><span>耗时</span><strong>{fmtDuration(selected.duration_ms ?? runtimeEvent.duration_ms)}</strong></div>
                      <div><span>状态</span><strong className={`tone-${selected.type === 'tool_result' ? (selected.ok ? 'ok' : 'bad') : 'neutral'}`}>{selected.type === 'tool_result' ? (selected.ok ? '成功' : '失败') : '调用'}</strong></div>
                    </div>
                  ) : null}

                  {runtimeEvent?.params ? (
                    <div className="explore-detail-block">
                      <h4>入参</h4>
                      <pre className="explore-pre">{pretty(runtimeEvent.params)}</pre>
                    </div>
                  ) : null}

                  {selected.digest ? (
                    <div className="explore-detail-block">
                      <h4>输出摘要</h4>
                      <pre className="explore-pre">{pretty(selected.digest)}</pre>
                    </div>
                  ) : null}

                  {selected.error ? (
                    <div className="explore-detail-block bad">
                      <h4>错误</h4>
                      <pre className="explore-pre">{String(selected.error)}</pre>
                    </div>
                  ) : null}

                  {selected.type === 'script' ? (
                    <>
                      <div className="explore-facts">
                        <div><span>路径</span><strong className="mono">{selected.path}</strong></div>
                        <div><span>退出码</span><strong className={Number(selected.exit_code) === 0 ? 'tone-ok' : 'tone-bad'}>{String(selected.exit_code)}</strong></div>
                        <div><span>耗时</span><strong>{fmtDuration(selected.duration_ms)}</strong></div>
                      </div>
                      {selected.stdout ? <div className="explore-detail-block"><h4>stdout</h4><pre className="explore-pre">{String(selected.stdout)}</pre></div> : null}
                      {selected.stderr ? <div className="explore-detail-block bad"><h4>stderr</h4><pre className="explore-pre">{String(selected.stderr)}</pre></div> : null}
                      {selected.code_sha256 ? <p className="explore-sha mono">code sha256 · {String(selected.code_sha256).slice(0, 16)}…</p> : null}
                    </>
                  ) : null}

                  {selected.type === 'verify' && Array.isArray(selected.checks) ? (
                    <div className="explore-detail-block">
                      <h4>校验结果 · {selected.verdict}</h4>
                      <div className="explore-verify-list">
                        {(selected.checks as ExploreCheck[]).map((check) => (
                          <div key={check.id} className={`explore-verify-row ${check.ok ? 'ok' : 'bad'}`}>
                            <span>{check.ok ? '✅' : '❌'}</span>
                            <div><strong>{specSummary(check)}</strong><p>{check.evidence || '无证据'}</p></div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {selected.type === 'end' ? (
                    <div className="explore-detail-block">
                      <h4>结束摘要</h4>
                      <pre className="explore-pre">{pretty({ status: selected.status, verdict: selected.verdict, summary: selected.summary })}</pre>
                    </div>
                  ) : null}

                  {Array.isArray(selected.artifacts) && selected.artifacts.length ? (
                    <div className="explore-detail-block">
                      <h4>产物</h4>
                      <div className="explore-inline-artifacts">
                        {(selected.artifacts as string[]).map((path) => (
                          <button key={path} type="button" className="explore-inline-artifact mono" onClick={() => setSelectedArtifact(path)}>
                            <Icon name="file" size={12} />{path}
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>
              )}
            </section>

            {/* 右栏：产物查看器 */}
            <section className="panel explore-col explore-artifact-panel" aria-label="产物查看器">
              <div className="explore-col-head">
                <div><h3>产物</h3><span>{artifacts.length} 个文件</span></div>
                {activeArtifact ? (
                  <a className="icon-btn" href={exploreApi.artifactUrl(eid, activeArtifact.path)} target="_blank" rel="noreferrer" aria-label="在新窗口打开产物">
                    <Icon name="external" size={15} />
                  </a>
                ) : null}
              </div>
              {!artifacts.length ? (
                <EmptyState icon="archive" title="暂无产物">工具调用或脚本执行后，截图 / DOM / 文本会出现在这里。</EmptyState>
              ) : (
                <div className="explore-artifact-layout">
                  <div className="explore-artifact-list">
                    {artifacts.map((item) => (
                      <button
                        key={item.path}
                        type="button"
                        className={`explore-artifact-item ${selectedArtifact === item.path ? 'is-active' : ''}`}
                        data-testid="explore-artifact"
                        data-path={item.path}
                        onClick={() => setSelectedArtifact(item.path)}
                      >
                        <span className="explore-artifact-icon">
                          <Icon name={String(item.content_type || '').startsWith('image/') ? 'eye' : String(item.content_type || '').includes('html') ? 'code' : 'file'} size={13} />
                        </span>
                        <span className="explore-artifact-copy">
                          <strong>{item.path.split('/').pop()}</strong>
                          <small>{fmtBytes(item.bytes)} · {item.content_type || 'unknown'}</small>
                        </span>
                      </button>
                    ))}
                  </div>
                  <div className="explore-artifact-view">
                    {artifact.loading ? <Loading label="读取产物…" /> : null}
                    {artifact.error ? (
                      <div className="explore-artifact-error"><Icon name="warning" size={15} /><span>{artifact.error}</span></div>
                    ) : null}
                    {!artifact.loading && !artifact.error && activeArtifact ? (
                      artifact.image ? (
                        <img className="explore-artifact-image" src={artifact.url} alt={activeArtifact.path} />
                      ) : artifact.contentType.includes('html') ? (
                        <>
                          <div className="explore-artifact-view-head">
                            <span><Icon name="shield" size={12} />沙箱预览（脚本已禁用）</span>
                            <button type="button" onClick={() => setShowHtmlSource((value) => !value)}>{showHtmlSource ? '预览' : '源码'}</button>
                          </div>
                          {showHtmlSource ? (
                            <pre className="explore-artifact-pre">{artifact.text}</pre>
                          ) : (
                            <iframe
                              className="explore-artifact-frame"
                              title={activeArtifact.path}
                              sandbox=""
                              referrerPolicy="no-referrer"
                              srcDoc={artifact.text}
                            />
                          )}
                        </>
                      ) : artifact.contentType.includes('json') ? (
                        <pre className="explore-artifact-pre">{pretty(safeJson(artifact.text))}</pre>
                      ) : (
                        <pre className="explore-artifact-pre">{artifact.text}</pre>
                      )
                    ) : null}
                  </div>
                </div>
              )}
            </section>
          </div>

          {/* 底部：成功检查清单 */}
          <section className="panel explore-checks-panel" data-testid="explore-checks">
            <div className="explore-col-head">
              <div><h3>成功检查清单</h3><span>确定性校验 · LLM 自述不作为通过依据</span></div>
              <div className="explore-verdict">
                <span className={`badge status-badge tone-${verdict === 'passed' ? 'ok' : verdict ? 'bad' : statusTone(status)}`}>
                  <span className="status-dot" />{verdict ? (verdict === 'passed' ? '整体通过' : '整体未通过') : statusText(status)}
                </span>
              </div>
            </div>

            {checks.length ? (
              <div className="explore-check-grid">
                {checks.map((check) => (
                  <div key={check.id} className={`explore-check ${check.ok ? 'ok' : 'bad'}`} data-testid="explore-check-item">
                    <span className="explore-check-mark" aria-label={check.ok ? '通过' : '未通过'}>{check.ok ? '✅' : '❌'}</span>
                    <div className="explore-check-body">
                      <strong>{specSummary(check)}</strong>
                      <p>{check.evidence || '无证据'}</p>
                      <span className="mono">{check.type}</span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="explore-checks-empty">
                {status === 'running' ? <><span className="spinner spinner-sm" /> 正在收集可执行证据…</> : '本次探索没有产出可执行检查。'}
              </div>
            )}

            <div className="explore-summary">
              <div><span>步骤</span><strong>{Number.isFinite(summary.steps) ? summary.steps : '—'}</strong></div>
              <div><span>工具调用</span><strong>{Number.isFinite(summary.toolCalls) ? summary.toolCalls : '—'}</strong></div>
              <div><span>脚本</span><strong>{Number.isFinite(summary.scripts) ? summary.scripts : '—'}</strong></div>
              <div><span>耗时</span><strong>{fmtDuration(summary.duration)}</strong></div>
              <div><span>结论</span><strong>{verdict ? (verdict === 'passed' ? '可固化' : '不可固化') : '—'}</strong></div>
            </div>

            {status !== 'running' && verdict !== 'passed' ? (
              <div className="explore-failure" data-testid="explore-failure-reason">
                <Icon name="warning" size={15} />
                <div>
                  <strong>{status === 'budget_exceeded' ? '预算超限，探索终止' : '探索未通过确定性校验'}</strong>
                  <p>{failureReason || '没有拿到可验证的成功证据；可调整成功标准或提高步数预算后重试。'}</p>
                </div>
              </div>
            ) : null}
          </section>

          {crystallizeError ? (
            <div className="explore-alert bad" data-testid="explore-crystallize-error">
              <span className="explore-alert-icon"><Icon name="warning" size={16} /></span>
              <div><strong>固化失败</strong><p>{crystallizeError}</p></div>
            </div>
          ) : null}

          {crystallizeResult ? (
            <section className="panel explore-crystallize-result" data-testid="explore-crystallize-result">
              <div className="explore-col-head">
                <div><h3>已固化为流程草稿</h3><span>参数来自真实调用值；重放不依赖 LLM。</span></div>
                <Badge tone={crystallizeResult.replay?.status === 'passed' ? 'ok' : 'bad'}>
                  重放 {crystallizeResult.replay?.status === 'passed' ? '通过' : '未通过'}
                </Badge>
              </div>
              <div className="explore-crystal-grid">
                <div><span>wid</span><strong className="mono">{crystallizeResult.wid}</strong></div>
                <div><span>rev</span><strong>{crystallizeResult.rev ?? 0}</strong></div>
                <div><span>重放状态</span><strong className={crystallizeResult.replay?.status === 'passed' ? 'tone-ok' : 'tone-bad'}>{crystallizeResult.replay?.status || '未执行'}</strong></div>
                <div><span>新原子</span><strong data-testid="explore-new-atom-count">{newAtoms.length ? `${newAtoms.length} 个` : '无新原子'}</strong></div>
                <div><span>待封装脚本</span><strong data-testid="explore-pending-atom-count">{pending.length ? `${pending.length} 个` : '无'}</strong></div>
              </div>
              <div className="explore-new-atoms" data-testid="explore-new-atoms">
                <div className="explore-crystal-subhead">
                  <span>新原子契约</span>
                  <span>{newAtoms.length ? `${newAtoms.length} 个` : '无新原子'}</span>
                </div>
                {newAtoms.length ? newAtoms.map((atom) => (
                  <details className="explore-new-atom" data-testid="explore-new-atom" data-atom={atom.name} key={atom.name}>
                    <summary>
                      <strong className="mono explore-new-atom-name">{atom.name}</strong>
                      {atom.pack ? <Badge tone="violet">{atom.pack}</Badge> : null}
                      <span className="explore-new-atom-hint">{effectsText(atom.effects)}</span>
                    </summary>
                    <div className="explore-new-atom-body">
                      <div><span>inputs</span><pre>{contractText(atom.inputs)}</pre></div>
                      <div><span>outputs</span><pre>{contractText(atom.outputs)}</pre></div>
                      <div><span>output_paths</span><pre>{contractText(atom.output_paths)}</pre></div>
                      <div><span>effects</span><pre>{contractText(atom.effects)}</pre></div>
                      <div><span>source_script</span><pre className="mono">{contractText(atom.source_script)}</pre></div>
                      <div><span>code_sha256</span><pre className="mono">{contractText(atom.code_sha256)}</pre></div>
                    </div>
                  </details>
                )) : (
                  <p className="explore-contract-empty" data-testid="explore-new-atoms-empty">
                    无新原子：本次固化没有把脚本封装成可复用原子。
                  </p>
                )}
              </div>
              {pending.length ? (
                <div className="explore-pending-atoms" data-testid="explore-pending-atoms">
                  <div className="explore-crystal-subhead">
                    <span>待封装脚本</span>
                    <span>{pending.length} 个</span>
                  </div>
                  {pending.map((item, index) => (
                    <div
                      className="explore-pending-atom"
                      data-testid="explore-pending-atom"
                      data-status={item.status || 'pending'}
                      key={`${item.name || item.path || 'pending'}-${index}`}
                    >
                      <div className="explore-pending-atom-head">
                        <strong className="mono">{item.name || `待封装脚本 ${index + 1}`}</strong>
                        <Badge tone="warn">{item.status || 'pending'}</Badge>
                      </div>
                      <dl className="explore-pending-atom-meta">
                        <div><dt>path</dt><dd className="mono">{contractText(item.path)}</dd></div>
                        <div><dt>reason</dt><dd>{contractText(item.reason)}</dd></div>
                      </dl>
                    </div>
                  ))}
                </div>
              ) : null}
              {crystallizeResult.replay?.errors?.length ? (
                <div className="explore-failure">
                  <Icon name="warning" size={15} />
                  <div><strong>重放错误</strong><p>{crystallizeResult.replay.errors.map((item) => String((item as AnyRecord).error || JSON.stringify(item))).join('；')}</p></div>
                </div>
              ) : null}
              <div className="explore-crystal-actions">
                <span>将在 2.6 秒后打开画布；也可立即前往。</span>
                <Button variant="primary" size="sm" icon="arrow" onClick={openCanvas}>立即打开画布</Button>
              </div>
            </section>
          ) : null}
        </>
      ) : null}

      {/* 历史探索 */}
      <section className="explore-history" aria-label="历史探索">
        <div className="explore-history-head">
          <div>
            <h2>历史探索</h2>
            <p>失败与预算超限也会保留，便于回看证据与调整标准。</p>
          </div>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => setHistoryTick((value) => value + 1)}>刷新</Button>
        </div>
        {historyLoading ? <Loading label="读取历史探索…" /> : null}
        {historyError ? <div className="explore-history-error"><Icon name="warning" size={15} />{historyError}</div> : null}
        {!historyLoading && !historyError && !history.length ? (
          <EmptyState icon="history" title="还没有探索记录">在上方启动第一次探索；跑通后即可固化为画布流程。</EmptyState>
        ) : (
          <div className="explore-history-list">
            {history.map((item) => (
              <a
                key={item.eid}
                className={`explore-history-item ${item.eid === eid ? 'is-active' : ''}`}
                href={href(`/studio/explore/${item.eid}`)}
                data-testid="explore-history-item"
              >
                <span className={`explore-history-dot tone-${statusTone(item.status)}`} />
                <span className="explore-history-copy">
                  <strong>{item.intent || '未记录意图'}</strong>
                  <small>
                    <span className="mono">{item.eid}</span>
                    <span>·</span>
                    <span>{relativeTime(item.created_at)}</span>
                    {item.summary?.tool_calls !== undefined ? <><span>·</span><span>{item.summary.tool_calls} 次工具调用</span></> : null}
                  </small>
                </span>
                <span className="explore-history-status">
                  <span className={`badge status-badge tone-${statusTone(item.status)}`}><span className="status-dot" />{statusText(item.status)}</span>
                  <Icon name="chevron" size={13} />
                </span>
              </a>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}
