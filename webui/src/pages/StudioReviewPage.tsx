import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { studioApi, isStudioMock, onStudioMockChange, normalizeDiff } from '../shared/api'
import { navigate, href, useRoute } from '../shared/router'
import { formatTime, pretty, relativeTime } from '../shared/utils'
import type { AnyRecord, DiffItem, Risk, StudioDiff } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  ErrorState,
  Field,
  JsonBlock,
  Loading,
  Modal,
  SectionHeader,
  StatusBadge,
  useToast,
} from '../components/ui'

type Decision = 'accept' | 'reject'

export function StudioReviewPage({ wid }: { wid: string }) {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const [mock, setMock] = useState(isStudioMock())
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  const [acked, setAcked] = useState<Record<string, boolean>>({})
  const [note, setNote] = useState('')
  const [message, setMessage] = useState('')
  const [expandedItem, setExpandedItem] = useState<DiffItem | null>(null)
  const [localPublishedRef, setLocalPublishedRef] = useState('')
  const [proposal, setProposal] = useState<AnyRecord | null>(null)
  const workflowQuery = useQuery({ queryKey: ['studio-workflow', wid], queryFn: () => studioApi.getWorkflow(wid), enabled: !!wid })
  const workflow = workflowQuery.data
  const requestedBase = Number(route.query.get('base') || 0)
  const requestedRev = Number(route.query.get('rev') || 0)
  const baseRev = requestedBase || Math.max(0, (workflow?.current_rev || 0) - 1)
  const rev = requestedRev || workflow?.current_rev || 0
  const isProposal = route.query.get('proposed') === '1'
  const currentRev = workflow?.current_rev || 0
  const effectiveReviewRev = isProposal ? currentRev : rev
  const reviewQuery = useQuery({
    queryKey: ['studio-review', wid, isProposal ? currentRev : baseRev, effectiveReviewRev],
    queryFn: () => studioApi.review(wid, isProposal ? currentRev : baseRev, effectiveReviewRev),
    enabled: !!wid && !!workflow,
  })
  const validationQuery = useQuery({ queryKey: ['studio-validation', wid, effectiveReviewRev], queryFn: () => studioApi.validate(wid, effectiveReviewRev), enabled: !!wid })

  useEffect(() => onStudioMockChange(setMock), [])

  useEffect(() => {
    if (!workflow || !isProposal) return
    const proposalRaw = sessionStorage.getItem(`panguverse.proposal.${wid}`)
    if (!proposalRaw) return
    try {
      const parsed = JSON.parse(proposalRaw)
      if (Number(parsed.proposed_rev) === rev) {
        setProposal(parsed)
        setNote('审阅 AI 提议，逐条接受可用变更。')
      }
    } catch {
      /* ignore malformed local proposal */
    }
  }, [workflow, wid, rev, isProposal])

  const items = useMemo(() => {
    if (isProposal && proposal) return normalizeDiff(proposal).items || []
    return reviewQuery.data?.items || []
  }, [isProposal, proposal, reviewQuery.data])

  useEffect(() => {
    if (!items.length) return
    setDecisions((current) => {
      const next: Record<string, Decision> = { ...current }
      items.forEach((item) => { if (!next[item.id]) next[item.id] = 'accept' })
      return next
    })
  }, [items])
  const risks = reviewQuery.data?.risks || []
  const acceptedIds = items.filter((item) => decisions[item.id] !== 'reject').map((item) => item.id)
  const requiredRisks = risks.filter((risk) => risk.requires_ack !== false)
  const allRisksAcked = requiredRisks.every((risk, index) => acked[risk.id || `risk-${index}`])
  const blockingErrors = validationQuery.data?.errors || []
  const warnings = validationQuery.data?.warnings || []
  const state = workflow?.state || 'draft'
  const publishedRef = localPublishedRef || workflow?.published_ref || ''
  const displayState = publishedRef ? 'published' : state
  const reviewedRev = rev

  const setState = useMutation({
    mutationFn: ({ to, text }: { to: 'review' | 'approved' | 'rejected'; text: string }) => studioApi.setState(wid, to, text, requiredRisks.map((risk, index) => risk.id || `risk-${index}`)),
    onSuccess: (next) => {
      queryClient.setQueryData(['studio-workflow', wid], next)
      queryClient.invalidateQueries({ queryKey: ['studio-workflow', wid] })
      push(`状态已更新：${next.state}`, 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const applyDiff = useMutation({
    mutationFn: () => studioApi.applyDiff(wid, rev, acceptedIds),
    onSuccess: (next) => {
      queryClient.setQueryData(['studio-workflow', wid], next)
      sessionStorage.removeItem(`panguverse.proposal.${wid}`)
      push(`已接受 ${acceptedIds.length} 条变更，生成 rev ${next.current_rev}`, 'ok')
      navigate(`/studio/${wid}`)
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const publish = useMutation({
    mutationFn: () => studioApi.publish(wid, reviewedRev, message.trim()),
    onSuccess: (result) => {
      setLocalPublishedRef(result.workflow_ref)
      queryClient.invalidateQueries({ queryKey: ['studio-workflow', wid] })
      queryClient.invalidateQueries({ queryKey: ['studio-drafts'] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      push(`已发布：${result.workflow_ref}`, 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const approveNote = () => {
    const ackText = requiredRisks.map((risk, index) => risk.id || `risk-${index}`).join(', ')
    return `${note.trim()}${ackText ? `\n[风险确认] ${ackText}` : ''}`.trim()
  }

  const rejectAll = () => setDecisions(Object.fromEntries(items.map((item) => [item.id, 'reject' as Decision])))
  const acceptAll = () => setDecisions(Object.fromEntries(items.map((item) => [item.id, 'accept' as Decision])))
  const approvedByState = state === 'approved' || state === 'published' || !!publishedRef
  const publishGate = !approvedByState
    ? `当前状态为 ${state}，需先批准`
    : !message.trim()
      ? '请填写版本说明'
      : blockingErrors.length
        ? `存在 ${blockingErrors.length} 个校验错误`
        : ''
  const approveGate = !allRisksAcked
    ? `还有 ${requiredRisks.filter((risk, index) => !acked[risk.id || `risk-${index}`]).length} 项风险未确认`
    : blockingErrors.length
      ? `存在 ${blockingErrors.length} 个校验错误`
      : ''

  if (workflowQuery.isLoading) return <div className="page"><Loading label="正在读取流程…" /></div>
  if (workflowQuery.error || !workflow) return <div className="page"><ErrorState error={workflowQuery.error} onRetry={() => workflowQuery.refetch()} title="流程读取失败" /></div>

  return (
    <div className="studio-page studio-review-page">
      <div className="review-top">
        <div className="review-top-left">
          <a className="icon-btn" href={href(`/studio/${wid}`)} aria-label="返回画布"><Icon name="arrow" size={18} style={{ transform: 'rotate(180deg)' }} /></a>
          <div>
            <div className="eyebrow"><span className="live-dot" /> 审阅与发布 · {wid}</div>
            <div className="review-title-line"><h1>{workflow.name || '未命名流程'}</h1><StatusBadge status={displayState} />{mock ? <Badge tone="warn">演示</Badge> : null}</div>
            <p className="review-sub">{workflow.intent || '未记录意图'} · 比较 rev {baseRev} → rev {reviewedRev}</p>
          </div>
        </div>
        <div className="review-top-actions">
          <Button size="sm" variant="ghost" icon="flow" onClick={() => navigate(`/studio/${wid}`)}>回到画布</Button>
          {publishedRef ? <Button size="sm" variant="primary" icon="tasks" onClick={() => navigate(`/tasks?published=${encodeURIComponent(publishedRef)}`)}>前往任务</Button> : null}
        </div>
      </div>

      <div className="review-steps">
        {[
          { id: 'draft', label: '草案', done: state !== 'draft' },
          { id: 'review', label: '审阅', done: ['approved', 'published'].includes(state) },
          { id: 'approved', label: '批准', done: ['approved', 'published'].includes(state) },
          { id: 'published', label: '发布', done: state === 'published' || !!publishedRef },
        ].map((step, index) => (
          <div className={`review-step ${displayState === step.id ? 'is-active' : ''} ${step.done ? 'is-done' : ''}`} key={step.id}>
            <span className="review-step-dot">{step.done ? <Icon name="check" size={11} /> : index + 1}</span>
            <strong>{step.label}</strong>
            {index < 3 ? <span className="review-step-line" /> : null}
          </div>
        ))}
      </div>

      {publishedRef ? (
        <div className="publish-success">
          <span className="publish-success-icon"><Icon name="check" size={22} /></span>
          <div><strong>流程已发布为 {publishedRef}</strong><p>版本不可变。任务可以绑定项目环境后运行，报告会携带该 workflow_ref 回溯到本次审阅。</p></div>
          <Button size="sm" variant="primary" icon="tasks" onClick={() => navigate(`/tasks?published=${encodeURIComponent(publishedRef)}`)}>去任务</Button>
        </div>
      ) : null}

      <div className="review-layout">
        <section className="review-main panel">
          <SectionHeader
            title="变更清单"
            description={`${items.length} 条差异 · ${acceptedIds.length} 条接受 · ${items.length - acceptedIds.length} 条拒绝`}
            icon="sort"
            actions={<><Button size="sm" variant="ghost" onClick={acceptAll}>全部接受</Button><Button size="sm" variant="ghost" onClick={rejectAll}>全部拒绝</Button></>}
          />
          {reviewQuery.isLoading ? <Loading label="正在计算 diff…" /> : null}
          {reviewQuery.error ? <ErrorState error={reviewQuery.error} onRetry={() => reviewQuery.refetch()} /> : null}
          {!reviewQuery.isLoading && !items.length ? (
            <div className="diff-empty"><Icon name="check" size={20} /><strong>两个版本没有结构差异</strong><p>可以继续检查风险、校验，然后提交审阅或发布。</p></div>
          ) : null}
          <div className="diff-list">
            {items.map((item) => {
              const decision = decisions[item.id] || 'accept'
              const op = normalizeOp(item.op)
              return (
                <article className={`diff-item op-${op} ${decision === 'reject' ? 'is-rejected' : ''}`} key={item.id}>
                  <div className="diff-item-head">
                    <span className={`diff-op op-${op}`}>{op === 'add' ? '新增' : op === 'remove' ? '删除' : '改参'}</span>
                    <button type="button" className="diff-label" onClick={() => setExpandedItem(item)}>
                      <strong>{item.label || item.message || '未命名变更'}</strong>
                      <span className="mono">{item.kind} · {item.node_id || item.edge_id || '—'}</span>
                    </button>
                    <div className="diff-decision">
                      <button type="button" className={decision === 'accept' ? 'is-accept' : ''} onClick={() => setDecisions({ ...decisions, [item.id]: 'accept' })}><Icon name="check" size={12} />接受</button>
                      <button type="button" className={decision === 'reject' ? 'is-reject' : ''} onClick={() => setDecisions({ ...decisions, [item.id]: 'reject' })}><Icon name="close" size={12} />拒绝</button>
                    </div>
                  </div>
                  {item.message && item.message !== item.label ? <p className="diff-message">{item.message}</p> : null}
                </article>
              )
            })}
          </div>
          {isProposal || items.length ? (
            <div className="diff-actions">
              <div><strong>应用已接受变更</strong><span>拒绝项不会写入新版本；发布仍是独立步骤。</span></div>
              <Button variant="primary" icon="check" disabled={!acceptedIds.length || !isProposal} loading={applyDiff.isPending} onClick={() => applyDiff.mutate()}>
                {isProposal ? `应用 ${acceptedIds.length} 条` : '当前为版本比较'}
              </Button>
            </div>
          ) : null}
        </section>

        <aside className="review-side">
          <section className="panel gate-panel">
            <SectionHeader title="发布门禁" description="缺一项都不能进入下一状态" icon="shield" />
            <div className="gate-list">
              <GateRow ok={blockingErrors.length === 0} label="校验错误清零" detail={blockingErrors.length ? `${blockingErrors.length} 个错误待修复` : '静态校验通过'} />
              <GateRow ok={allRisksAcked} label="风险逐项确认" detail={`${requiredRisks.filter((risk) => acked[risk.id || '']).length}/${requiredRisks.length} 项已勾选`} />
              <GateRow ok={approvedByState} label="审阅已批准" detail={state === 'approved' || state === 'published' ? '已批准' : `当前 ${state}`} />
              <GateRow ok={!!message.trim()} label="版本说明非空" detail={message.trim() ? `${message.trim().length} 字` : '发布前必须填写'} />
            </div>
            <div className="gate-actions">
              {state === 'draft' || state === 'rejected' ? (
                <Button variant="primary" icon="git" loading={setState.isPending} onClick={() => setState.mutate({ to: 'review', text: note.trim() || '提交审阅' })}>提交审阅</Button>
              ) : state === 'review' ? (
                <>
                  <Button variant="ghost" icon="close" disabled={!note.trim()} onClick={() => setState.mutate({ to: 'rejected', text: note.trim() })}>拒绝</Button>
                  <Button variant="primary" icon="check" disabled={!!approveGate} title={approveGate || '批准审阅'} loading={setState.isPending} onClick={() => setState.mutate({ to: 'approved', text: approveNote() || '批准' })}>批准</Button>
                </>
              ) : state === 'approved' ? (
                <Badge tone="ok"><Icon name="check" size={12} />已批准，可发布</Badge>
              ) : state === 'published' ? (
                <Badge tone="ai"><Icon name="git" size={12} />{workflow.published_ref || '已发布'}</Badge>
              ) : null}
              {approveGate && state === 'review' ? <span className="gate-hint">{approveGate}</span> : null}
            </div>
          </section>

          <section className="panel risk-panel">
            <SectionHeader title="风险确认" description="破坏性 / 写副作用 / 密钥" icon="warning" actions={<Badge tone={requiredRisks.length ? 'warn' : 'ok'}>{requiredRisks.length} 项</Badge>} />
            {risks.length ? (
              <div className="risk-list">
                {risks.map((risk, index) => {
                  const id = risk.id || `risk-${index}`
                  const requires = risk.requires_ack !== false
                  return (
                    <label className={`risk-row ${acked[id] ? 'is-checked' : ''}`} key={id}>
                      <input type="checkbox" checked={!!acked[id]} onChange={(event) => setAcked({ ...acked, [id]: event.target.checked })} />
                      <span className={`risk-icon severity-${risk.severity || 'medium'}`}><Icon name={risk.severity === 'high' ? 'warning' : 'shield'} size={13} /></span>
                      <span className="risk-copy"><strong>{risk.message}</strong><small>{risk.kind || 'risk'} · {risk.severity || 'medium'}{requires ? ' · 必须确认' : ' · 提示'}</small></span>
                    </label>
                  )
                })}
              </div>
            ) : <div className="inspector-ok"><Icon name="check" size={14} />没有识别到风险</div>}
          </section>

          <section className="panel review-note-panel">
            <SectionHeader title="审阅意见" description="写入审计，不写版本说明" icon="edit" />
            <textarea className="textarea" rows={3} value={note} onChange={(event) => setNote(event.target.value)} placeholder="说明为什么批准、拒绝，或还需要什么验证。" />
            <div className="review-note-actions">
              <span>{note.length} 字</span>
              <Button size="sm" variant="ghost" icon="check" disabled={!note.trim()} onClick={() => push('审阅意见已暂存（提交状态时写入审计）', 'ok')}>暂存意见</Button>
            </div>
          </section>
        </aside>
      </div>

      <div className="review-bottom">
        <section className="panel validation-panel">
          <SectionHeader title="校验与审计" description="发布前最后一眼" icon="terminal" actions={<Badge tone={blockingErrors.length ? 'bad' : 'ok'}>{blockingErrors.length} 错误 · {warnings.length} 警告</Badge>} />
          <div className="validation-columns">
            <div>
              <h3 className="subsection-title">校验问题</h3>
              {validationQuery.data?.coverage ? (
                <div className={`coverage-line ${validationQuery.data.coverage.ratio < 0.6 ? 'bad' : 'ok'}`}>
                  <span>意图覆盖率</span>
                  <strong className="mono">{validationQuery.data.coverage.matched}/{validationQuery.data.coverage.total} = {validationQuery.data.coverage.ratio.toFixed(2)}</strong>
                </div>
              ) : null}
              {validationQuery.data?.missing_env?.length ? (
                <div className="digest-block"><span className="io-label">missing_env</span><div className="digest-chips">{validationQuery.data.missing_env.map((key) => <code key={key}>{key}</code>)}</div></div>
              ) : null}
              {validationQuery.data?.capability_gaps?.length ? (
                <div className="digest-block"><span className="io-label">capability_gaps</span>{validationQuery.data.capability_gaps.map((gap, index) => {
                  const label = typeof gap === 'string' ? gap : gap.atom || gap.name || gap.message || `gap-${index}`
                  return <div className="digest-gap" key={label}><code>{label}</code></div>
                })}</div>
              ) : null}
              {blockingErrors.length || warnings.length ? (
                <div className="issue-list">
                  {blockingErrors.map((issue, index) => (
                    <button type="button" className="issue-row bad" key={issue.id || index} disabled={!issue.node_id} onClick={() => issue.node_id && navigate(`/studio/${wid}?node=${encodeURIComponent(issue.node_id)}`)} title={issue.node_id ? `定位节点 ${issue.node_id}` : issue.message}>
                      <Icon name="warning" size={13} /><span>{issue.message}{issue.code ? <small className="issue-code">{issue.code}</small> : null}</span>{issue.node_id ? <Icon name="chevron" size={12} /> : null}
                    </button>
                  ))}
                  {warnings.map((issue, index) => (
                    <button type="button" className="issue-row warn" key={issue.id || `w-${index}`} disabled={!issue.node_id} onClick={() => issue.node_id && navigate(`/studio/${wid}?node=${encodeURIComponent(issue.node_id)}`)} title={issue.node_id ? `定位节点 ${issue.node_id}` : issue.message}>
                      <Icon name="bolt" size={13} /><span>{issue.message}{issue.code ? <small className="issue-code">{issue.code}</small> : null}</span>{issue.node_id ? <Icon name="chevron" size={12} /> : null}
                    </button>
                  ))}
                </div>
              ) : <div className="inspector-ok"><Icon name="check" size={14} />校验通过</div>}
            </div>
            <div>
              <h3 className="subsection-title">审计记录</h3>
              <div className="audit-list compact">
                {(reviewQuery.data?.audit_tail || []).slice(-5).map((entry: AnyRecord, index: number) => (
                  <div className="audit-item" key={index}><span className="audit-dot" /><div><strong>{entry.action || entry.to_state || '状态变更'}</strong><span>{formatTime(entry.ts)} · {entry.actor || 'system'} · rev {entry.rev || '—'}</span></div></div>
                ))}
                {!(reviewQuery.data?.audit_tail || []).length ? <p className="muted">暂无审计条目。</p> : null}
              </div>
            </div>
          </div>
        </section>

        <section className="panel publish-panel">
          <SectionHeader title="发布为流程" description="P0 只发布为流程；封存为原子在 P1。" icon="git" />
          <div className="publish-state">
            <span>目标版本</span><strong className="mono">{wid}@{reviewedRev}</strong>
          </div>
          <Field label="版本说明" hint="不可为空；会写入新版本的审计与任务元信息。">
            <textarea className="textarea" rows={3} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：补充订单断言与超时保护，批准上线。" />
          </Field>
          <Button variant="primary" size="lg" icon="git" className="full-width" disabled={!!publishGate} title={publishGate || '发布'} loading={publish.isPending} onClick={() => publish.mutate()}>发布流程 @{reviewedRev}</Button>
          {publishGate ? <p className="gate-hint center">{publishGate}</p> : <p className="publish-ready"><Icon name="shield" size={13} />门禁通过，发布后版本不可变</p>}
        </section>
      </div>

      <Modal
        open={!!expandedItem}
        onClose={() => setExpandedItem(null)}
        title={expandedItem?.label || '变更详情'}
        subtitle={expandedItem ? `${expandedItem.kind} · ${expandedItem.node_id || expandedItem.edge_id || ''}` : ''}
        size="lg"
        footer={<Button onClick={() => setExpandedItem(null)}>关闭</Button>}
      >
        {expandedItem ? (
          <div className="stack-lg">
            <div className="diff-detail-meta"><Badge tone={normalizeOp(expandedItem.op) === 'remove' ? 'bad' : normalizeOp(expandedItem.op) === 'add' ? 'cyan' : 'warn'}>{expandedItem.op}</Badge><span>{expandedItem.message || ''}</span></div>
            {expandedItem.before || expandedItem.after ? (
              <div className="before-after">
                {expandedItem.before ? <div><span className="io-label">before</span><JsonBlock data={expandedItem.before} /></div> : null}
                {expandedItem.after ? <div><span className="io-label">after</span><JsonBlock data={expandedItem.after} /></div> : null}
              </div>
            ) : <div className="diff-empty compact"><Icon name="sort" size={18} /><p>该变更没有附带完整前后数据；请回到画布检查节点。</p></div>}
          </div>
        ) : null}
      </Modal>
    </div>
  )
}

function GateRow({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div className={`gate-row ${ok ? 'is-ok' : ''}`}>
      <span className="gate-check">{ok ? <Icon name="check" size={12} /> : <Icon name="warning" size={12} />}</span>
      <div><strong>{label}</strong><small>{detail}</small></div>
    </div>
  )
}

function normalizeOp(op: string): 'add' | 'remove' | 'change' {
  if (op === 'add' || op === 'added' || op === 'create') return 'add'
  if (op === 'remove' || op === 'removed' || op === 'delete') return 'remove'
  return 'change'
}
