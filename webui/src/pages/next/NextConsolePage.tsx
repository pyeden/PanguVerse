import { useEffect, useMemo, useState } from 'react'
import { navigate } from '../../shared/router'
import {
  NX_DEBUG_ROWS,
  NX_INITIAL_FEED,
  NX_INITIAL_INTENT,
  NX_INITIAL_NEEDS,
  NX_INITIAL_PLAN,
  NX_INITIAL_RESULT,
  NX_INITIAL_SUGGESTION,
  NX_INITIAL_TIMELINE,
  NX_TEMPLATES,
  feedForPhase,
  planForPhase,
  timelineForPhase,
  type NxDebugRow,
  type NxFeedItem,
  type NxNeed,
  type NxPlanStep,
  type NxResult,
  type NxStatus,
  type NxSuggestion,
  type NxTimelineEvent,
} from '../../shared/nextMock'
import { Icon } from '../../components/Icon'
import { Badge, Button, PageHeader, Progress, SectionHeader, useToast } from '../../components/ui'
import './next.css'

const STATUS_LABEL: Record<NxStatus, string> = {
  done: '完成',
  running: '运行中',
  fixing: '自修',
  queued: '排队',
}

const STATUS_ICON = {
  done: 'check',
  running: 'play',
  fixing: 'refresh',
  queued: 'clock',
} as const

const STATUS_TONE = {
  done: 'ok',
  running: 'run',
  fixing: 'warn',
  queued: 'neutral',
} as const

type RunState = 'paused' | 'running' | 'complete'

export function NextConsolePage() {
  const { push } = useToast()
  const [intent, setIntent] = useState(NX_INITIAL_INTENT)
  const [showTemplates, setShowTemplates] = useState(false)
  const [runState, setRunState] = useState<RunState>('paused')
  const [phase, setPhase] = useState(3)
  const [plan, setPlan] = useState<NxPlanStep[]>(NX_INITIAL_PLAN)
  const [timeline, setTimeline] = useState<NxTimelineEvent[]>(NX_INITIAL_TIMELINE)
  const [feed, setFeed] = useState<NxFeedItem[]>(NX_INITIAL_FEED)
  const [debugRows] = useState<NxDebugRow[]>(NX_DEBUG_ROWS)
  const [needs, setNeeds] = useState<NxNeed[]>(NX_INITIAL_NEEDS)
  const [suggestion, setSuggestion] = useState<NxSuggestion>(NX_INITIAL_SUGGESTION)
  const [result, setResult] = useState<NxResult>(NX_INITIAL_RESULT)
  const [humanCount, setHumanCount] = useState(1)
  const [aiActions, setAiActions] = useState(8)

  const unresolved = needs.filter((need) => !need.resolved).length
  const planSummary = useMemo(() => {
    const atoms = plan.filter((step) => step.kind === 'atom').length
    const custom = plan.filter((step) => step.kind === 'custom').length
    return `${plan.length} 步 · ${atoms} 原子 · ${custom} 自定义`
  }, [plan])

  useEffect(() => {
    if (runState !== 'running') return
    if (phase >= 5) {
      setRunState('complete')
      setResult({
        ...NX_INITIAL_RESULT,
        title: '对比报告已生成 · 12 个 SKU',
        summary: '3 个竞品站点已完成抓取、提取与归一化；报告、异常清单和原始价格证据已落盘。飞书推送仍在等待 L1 批准。',
        evidence: ['3 张页面截图', '12 条价格原始值', '2 次 AI 自修记录', 'run-9f2c14 · rev 4'],
      })
      return
    }
    const timer = window.setTimeout(() => setPhase((value) => value + 1), 950)
    return () => window.clearTimeout(timer)
  }, [runState, phase])

  useEffect(() => {
    if (runState === 'paused') return
    setPlan(planForPhase(intent, phase))
    setTimeline(timelineForPhase(phase))
    const additions = feedForPhase(phase)
    setFeed((current) => {
      const ids = new Set(current.map((item) => item.id))
      return [...current, ...additions.filter((item) => !ids.has(item.id))]
    })
    if (phase >= 4) {
      setResult((current) => ({
        ...current,
        title: '对比报告已生成 · 12 个 SKU',
        summary: '报告已生成并通过输出校验；飞书推送等待 L1 风险批准。',
        evidence: ['3 张页面截图', '12 条价格原始值', '2 次 AI 自修记录', 'run-9f2c14 · rev 4'],
      }))
    }
  }, [phase, runState, intent])

  const start = () => {
    if (runState === 'running') return
    const text = intent.trim() || NX_TEMPLATES[0].intent
    if (!intent.trim()) setIntent(text)
    setRunState('running')
    setPhase(0)
    setPlan(planForPhase(text, 0))
    setTimeline(timelineForPhase(0))
    setFeed([])
    setNeeds(NX_INITIAL_NEEDS)
    setSuggestion(NX_INITIAL_SUGGESTION)
    setHumanCount(1)
    setAiActions(8)
    setResult({
      ...NX_INITIAL_RESULT,
      title: 'AI 正在执行…',
      summary: '已拆解意图并开始执行；无需填写项目、套件或模式。',
      evidence: ['正在生成证据…'],
      artifacts: [],
    })
    setShowTemplates(false)
    push('AI 开始执行：先拆解，再逐步落到原子或自定义实现', 'ok')
  }

  const applyTemplate = (template: (typeof NX_TEMPLATES)[number]) => {
    setIntent(template.intent)
    setShowTemplates(false)
    push(`已套用模板「${template.title}」`, 'ok')
  }

  const resolveNeed = (need: NxNeed, mode: 'approve' | 'alternative') => {
    setHumanCount((value) => value + 1)
    if (mode === 'alternative') {
      setAiActions((value) => value + 2)
      setNeeds((current) => current.map((item) => item.id === need.id
        ? { ...item, suggestion: 'AI 将改为先导出 CSV 与报告，再由你批准外部推送。' }
        : item))
      setFeed((current) => [...current, {
        id: `need-alt-${Date.now()}`,
        at: '刚刚',
        kind: 'decision',
        text: `你要求换方案：${need.kind === 'secret' ? '跳过站点 B，保留其余站点' : '先导出 CSV，飞书推送延后'}。AI 已重规划。`,
      }])
      push('AI 已按你的要求切换方案', 'ok')
      return
    }
    setNeeds((current) => current.map((item) => item.id === need.id ? { ...item, resolved: true } : item))
    setAiActions((value) => value + 1)
    if (need.kind === 'risk') {
      setPlan((current) => current.map((step) => step.id === 'step-5' ? { ...step, status: 'running', note: 'L1 已批准 · 正在推送' } : step))
      setTimeline((current) => current.map((event) => event.id === 'tl-5' ? { ...event, status: 'running', time: '21:04:10', detail: 'L1 已批准 · 推送飞书卡片' } : event))
    } else {
      setPlan((current) => current.map((step) => step.id === 'step-2' ? { ...step, note: '3 站点 · 密钥已注入' } : step))
    }
    setFeed((current) => [...current, {
      id: `need-ok-${Date.now()}`,
      at: '刚刚',
      kind: 'decision',
      text: need.kind === 'secret'
        ? '密钥已注入（值打码），AI 继续读取站点 B。'
        : 'L1 风险已批准，AI 继续执行外部写；本次批准只对当前运行有效。',
    }])
    push(need.kind === 'secret' ? '密钥已注入，AI 继续执行' : 'L1 风险已批准，AI 继续执行', 'ok')
  }

  const actSuggestion = (mode: 'agree' | 'approve' | 'alternative') => {
    setHumanCount((value) => value + 1)
    setAiActions((value) => value + 2)
    if (mode === 'alternative') {
      setSuggestion((current) => ({
        ...current,
        accepted: false,
        approved: false,
        alternative: true,
        text: 'AI 已重新规划：保留报告生成；飞书推送改为一次批准后批量发送，并附异常清单。',
      }))
      setFeed((current) => [...current, {
        id: `sug-alt-${Date.now()}`,
        at: '刚刚',
        kind: 'fix',
        text: 'AI 按「换方案」重排：合并外部写，减少一次等待。',
      }])
      push('AI 已重新生成方案', 'ok')
      return
    }
    setSuggestion((current) => ({
      ...current,
      accepted: true,
      approved: mode === 'approve',
      alternative: false,
      text: mode === 'approve'
        ? '已批准并执行：报告生成后，飞书推送与人工确认合并为一次外部写。'
        : '已采纳建议：飞书推送与人工确认合并为一次外部写。',
    }))
    setFeed((current) => [...current, {
      id: `sug-ok-${Date.now()}`,
      at: '刚刚',
      kind: 'success',
      text: mode === 'approve' ? 'AI 建议已批准并执行。' : 'AI 建议已采纳，等待下一次运行生效。',
    }])
    push(mode === 'approve' ? '建议已批准并执行' : '建议已采纳', 'ok')
  }

  const budgetPercent = Math.min(100, Math.round((humanCount / Math.max(1, aiActions / 3)) * 100))
  const budgetTone = humanCount <= 1 ? 'ok' : humanCount <= 3 ? 'run' : 'warn'

  return (
    <div className="page nx-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> AI 控制台 · 默认 AI 执行</>}
        title={<>说出目标，<span className="text-gradient">AI 开始干活</span></>}
        description="不用配置项目、套件或模式。AI 负责拆解、执行、观察与自修；你只在缺密钥、权限和风险点出现。"
        actions={
          <>
            <Badge tone="warn">演示数据 · 不连后端</Badge>
            <Button variant="ghost" size="sm" icon="flow" onClick={() => navigate('/next/workbench/wf-console/node-extract-prices')}>节点工作台</Button>
          </>
        }
      />

      <section className="nx-hero panel" aria-label="意图输入">
        <div className="nx-hero-glow" aria-hidden="true" />
        <div className="nx-intent-row">
          <label className="nx-intent-box">
            <span className="nx-intent-label">
              <Icon name="spark" size={14} />
              一句话说明目标
              <span className="nx-intent-optional">无需填表</span>
            </span>
            <textarea
              className="nx-intent-input"
              rows={2}
              value={intent}
              onChange={(event) => setIntent(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') start()
              }}
              placeholder="例如：每天 9 点抓取竞品价格，异常时发飞书提醒，并生成对比报告"
              aria-label="一句话说明目标"
            />
            <span className="nx-intent-hint">⌘ / Ctrl + ↵ 直接开始 · 空着也行，AI 会用示例意图</span>
          </label>
          <div className="nx-intent-actions">
            <Button variant="primary" size="lg" icon={runState === 'running' ? 'refresh' : 'spark'} disabled={runState === 'running'} onClick={start}>
              {runState === 'running' ? 'AI 正在执行…' : runState === 'complete' ? '让 AI 再跑一次' : '让 AI 开始'}
            </Button>
            <Button variant="ghost" size="lg" icon="layers" onClick={() => setShowTemplates((value) => !value)} aria-expanded={showTemplates}>
              用模板
            </Button>
          </div>
        </div>

        {showTemplates ? (
          <div className="nx-templates" aria-label="模板">
            {NX_TEMPLATES.map((template) => (
              <button key={template.id} type="button" className="nx-template" onClick={() => applyTemplate(template)}>
                <span className="nx-template-icon"><Icon name="layers" size={14} /></span>
                <span className="nx-template-copy"><strong>{template.title}</strong><small>{template.intent}</small></span>
                <Badge tone="neutral">{template.tag}</Badge>
              </button>
            ))}
          </div>
        ) : (
          <div className="nx-suggestions">
            <span>试试：</span>
            {['把 3 个竞品站点的价格做成日报', '表单缺字段自动补全并通知', '网页内容变化时发飞书'].map((text) => (
              <button key={text} type="button" onClick={() => setIntent(text)}>{text}</button>
            ))}
          </div>
        )}
      </section>

      <div className="nx-layout">
        <div className="nx-main">
          <section className="panel nx-panel" aria-labelledby="nx-plan-title">
            <SectionHeader
              icon="flow"
              title={<span id="nx-plan-title">AI 计划</span>}
              description={planSummary}
              actions={<Badge tone={runState === 'running' ? 'run' : runState === 'complete' ? 'ok' : 'warn'} dot>{runState === 'running' ? '执行中' : runState === 'complete' ? '本轮完成' : '等待人工'}</Badge>}
            />
            <div className="nx-plan-flow" role="list" aria-label="AI 计划步骤">
              {plan.map((step, index) => (
                <div key={step.id} className={`nx-plan-step is-${step.status}`} role="listitem">
                  <div className="nx-plan-top">
                    <span className="nx-step-index">{String(index + 1).padStart(2, '0')}</span>
                    <Badge tone={step.kind === 'custom' ? 'warn' : 'cyan'}>{step.kind === 'custom' ? '自定义' : '原子'}</Badge>
                    <Badge tone={step.risk === 'L0' ? 'neutral' : step.risk === 'L1' ? 'warn' : 'bad'}>{step.risk}</Badge>
                  </div>
                  <strong className="nx-step-title">{step.title}</strong>
                  <code className="nx-step-atom">{step.atom}</code>
                  <div className="nx-step-foot">
                    <span className={`nx-status nx-status-${step.status}`}>
                      <Icon name={STATUS_ICON[step.status]} size={11} />
                      {STATUS_LABEL[step.status]}
                      {step.status === 'running' || step.status === 'fixing' ? <i className="nx-pulse" aria-hidden="true" /> : null}
                    </span>
                    <span className="nx-step-note">{step.note}</span>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <div className="nx-split">
            <section className="panel nx-panel" aria-labelledby="nx-timeline-title">
              <SectionHeader icon="clock" title={<span id="nx-timeline-title">执行时间线</span>} description="完成 / 运行中 / 自修 / 排队" actions={<span className="mono nx-run-id">run-9f2c14</span>} />
              <ol className="nx-timeline">
                {timeline.map((event) => (
                  <li key={event.id} className={`nx-timeline-item is-${event.status}`}>
                    <span className="nx-timeline-dot"><Icon name={STATUS_ICON[event.status]} size={12} /></span>
                    <div className="nx-timeline-copy">
                      <div className="nx-timeline-head">
                        <strong>{event.label}</strong>
                        <Badge tone={STATUS_TONE[event.status]} dot>{STATUS_LABEL[event.status]}</Badge>
                        {event.status === 'running' || event.status === 'fixing' ? <span className="nx-typing" aria-hidden="true"><i /><i /><i /></span> : null}
                      </div>
                      <p>{event.detail}</p>
                    </div>
                    <div className="nx-timeline-meta"><span>{event.time}</span>{event.duration ? <small>{event.duration}</small> : null}</div>
                  </li>
                ))}
              </ol>
            </section>

            <section className="panel nx-panel" aria-labelledby="nx-feed-title">
              <SectionHeader icon="eye" title={<span id="nx-feed-title">AI 观察与自修</span>} description="每次观察、修复和决策都可回看" actions={<Badge tone="ai">AI feed</Badge>} />
              <ol className="nx-feed">
                {feed.map((item) => (
                  <li key={item.id} className={`nx-feed-item is-${item.kind}`}>
                    <span className={`nx-feed-icon tone-${item.kind === 'fix' ? 'warn' : item.kind === 'decision' ? 'violet' : item.kind === 'success' ? 'ok' : 'run'}`}>
                      <Icon name={item.kind === 'fix' ? 'refresh' : item.kind === 'decision' ? 'shield' : item.kind === 'success' ? 'check' : 'eye'} size={13} />
                    </span>
                    <div className="nx-feed-copy">
                      <p>{item.text}</p>
                      <div className="nx-feed-meta">
                        <span>{item.at}</span>
                        {item.code ? <code>{item.code}</code> : null}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          </div>

          <section className="panel nx-panel nx-result" aria-labelledby="nx-result-title">
            <SectionHeader
              icon="archive"
              title={<span id="nx-result-title">当前结果</span>}
              description="结果、证据和人工干预一起看"
              actions={
                <>
                  <Button size="sm" variant="ghost" icon="download" onClick={() => push('演示模式：产出物已准备好（mock）', 'info')}>下载产出物</Button>
                  <Button size="sm" variant="ghost" icon="external" onClick={() => push('演示模式：运行详情将在批次 2 接入真实 API', 'info')}>查看运行</Button>
                </>
              }
            />
            <div className="nx-result-grid">
              <div className="nx-result-main">
                <span className="nx-result-kicker">RESULT · 中间结果</span>
                <h3>{result.title}</h3>
                <p>{result.summary}</p>
                <div className="nx-artifacts">
                  {result.artifacts.length ? result.artifacts.map((artifact) => (
                    <span key={artifact} className="nx-artifact"><Icon name="archive" size={12} />{artifact}</span>
                  )) : <span className="nx-artifact pending"><Icon name="clock" size={12} />等待报告生成</span>}
                </div>
              </div>
              <div className="nx-result-side">
                <div className="nx-result-stat">
                  <span>证据</span>
                  <strong>{result.evidence.length}<small>组</small></strong>
                  <ul>
                    {result.evidence.map((item) => <li key={item}><Icon name="check" size={11} />{item}</li>)}
                  </ul>
                </div>
                <div className="nx-result-stat">
                  <span>人工干预计数</span>
                  <strong>{humanCount}<small>次</small><i>/</i>{aiActions}<small>个 AI 动作</small></strong>
                  <div className="nx-budget-bar"><span style={{ width: `${budgetPercent}%` }} /></div>
                  <small className="muted">只在缺密钥、权限和风险点暂停</small>
                </div>
              </div>
            </div>
          </section>

          <section className="panel nx-panel nx-debug" aria-labelledby="nx-debug-title">
            <SectionHeader
              icon="bug"
              title={<span id="nx-debug-title">AI 调试记录</span>}
              description="错误 → 修复 → 通过；点击行号跳到节点工作台对应代码行"
              actions={<Badge tone="ok">已自修 2 次</Badge>}
            />
            <div className="nx-debug-table" role="table" aria-label="AI 调试记录">
              <div className="nx-debug-row nx-debug-head" role="row">
                <span role="columnheader">时间</span>
                <span role="columnheader">结果</span>
                <span role="columnheader">行号</span>
                <span role="columnheader">消息</span>
              </div>
              {debugRows.map((row) => (
                <div key={row.id} className={`nx-debug-row is-${row.level}`} role="row">
                  <span className="mono" role="cell">{row.at}</span>
                  <span role="cell"><Badge tone={row.level === 'error' ? 'bad' : row.level === 'fix' ? 'warn' : 'ok'}>{row.level === 'error' ? '错误' : row.level === 'fix' ? '修复' : '通过'}</Badge></span>
                  <span role="cell">
                    <button
                      type="button"
                      className="nx-line-jump"
                      onClick={() => navigate(`/next/workbench/wf-console/${row.nodeId}?line=${row.line}`)}
                      aria-label={`跳到第 ${row.line} 行`}
                    >
                      L{row.line}
                    </button>
                  </span>
                  <span className="nx-debug-message" role="cell">{row.message}</span>
                </div>
              ))}
            </div>
          </section>
        </div>

        <aside className="nx-side" aria-label="需要你">
          <section className="panel nx-side-card nx-budget-card">
            <SectionHeader icon="user" title="人工干预预算" description="少干预可度量" actions={<Badge tone={budgetTone === 'ok' ? 'ok' : 'warn'}>{budgetTone === 'ok' ? '预算内' : '接近阈值'}</Badge>} />
            <div className="nx-budget-main">
              <div><strong>{humanCount}</strong><span>次人工干预</span></div>
              <i>/</i>
              <div><strong>{aiActions}</strong><span>个 AI 动作</span></div>
            </div>
            <Progress value={budgetPercent} tone={budgetPercent > 70 ? 'warm' : ''} />
            <p className="nx-budget-note">AI 默认执行；只在缺密钥、权限或 L1/L2 风险时停下来问你。</p>
          </section>

          <section className="panel nx-side-card nx-needs-card" aria-labelledby="nx-needs-title">
            <SectionHeader
              icon="shield"
              title={<span id="nx-needs-title">需要你</span>}
              description="只出现决策点，不出现表单"
              actions={<Badge tone={unresolved ? 'warn' : 'ok'} dot>{unresolved ? `${unresolved} 项待处理` : '已清空'}</Badge>}
            />
            <div className="nx-need-list">
              {needs.map((need) => (
                <article key={need.id} className={`nx-need ${need.resolved ? 'is-resolved' : ''}`}>
                  <div className="nx-need-head">
                    <span className={`nx-need-icon ${need.kind}`}><Icon name={need.kind === 'secret' ? 'lock' : 'shield'} size={13} /></span>
                    <div>
                      <strong>{need.title}</strong>
                      <p>{need.detail}</p>
                    </div>
                    {need.resolved ? <Badge tone="ok">已处理</Badge> : <Badge tone={need.kind === 'risk' ? 'warn' : 'bad'}>{need.kind === 'risk' ? 'L1' : '缺输入'}</Badge>}
                  </div>
                  <div className="nx-need-suggest"><Icon name="spark" size={11} /><span>AI 建议：{need.suggestion}</span></div>
                  {!need.resolved ? (
                    <div className="nx-need-actions">
                      <Button size="sm" variant="soft" icon={need.kind === 'risk' ? 'check' : 'lock'} onClick={() => resolveNeed(need, 'approve')}>
                        {need.kind === 'risk' ? '批准' : '同意'}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => resolveNeed(need, 'alternative')}>让 AI 换方案</Button>
                    </div>
                  ) : (
                    <div className="nx-need-done"><Icon name="check" size={12} />已记录人工决策 · 继续执行</div>
                  )}
                </article>
              ))}
            </div>
          </section>

          <section className="panel nx-side-card nx-suggestion-card" aria-labelledby="nx-suggestion-title">
            <SectionHeader icon="spark" title={<span id="nx-suggestion-title">AI 建议</span>} description="采纳、批准或让 AI 换方案" actions={<Badge tone={suggestion.accepted ? 'ok' : suggestion.alternative ? 'warn' : 'ai'}>{suggestion.accepted ? '已采纳' : suggestion.alternative ? '已换方案' : '待决策'}</Badge>} />
            <div className="nx-suggestion-body">
              <strong>{suggestion.title}</strong>
              <p>{suggestion.text}</p>
              <div className="nx-suggestion-code">
                <span>计划变化</span>
                <code>4 steps → 3 steps · 1 次外部写</code>
              </div>
              <div className="nx-suggestion-actions">
                <Button size="sm" variant="primary" icon="check" disabled={suggestion.accepted} onClick={() => actSuggestion('agree')}>同意</Button>
                <Button size="sm" variant="soft" icon="shield" disabled={suggestion.accepted} onClick={() => actSuggestion('approve')}>批准</Button>
                <Button size="sm" variant="ghost" onClick={() => actSuggestion('alternative')}>让 AI 换方案</Button>
              </div>
            </div>
          </section>

          <section className="panel nx-side-card nx-side-note">
            <Icon name="lock" size={15} />
            <div>
              <strong>证据不会被省略</strong>
              <p>AI 自修次数、改动行、实现版本都会写进证据；批准只减审批步骤，不减审计字段。</p>
            </div>
          </section>
        </aside>
      </div>
    </div>
  )
}
