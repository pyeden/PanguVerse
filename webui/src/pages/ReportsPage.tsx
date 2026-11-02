import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../shared/api'
import { href, navigate, useRoute, withQuery } from '../shared/router'
import { formatDuration, formatTime, rateOf, relativeTime, shortId, statusLabel, statusOf, classForStatus } from '../shared/utils'
import type { ReportSummary } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  Drawer,
  EmptyState,
  ErrorState,
  Field,
  Kpi,
  Loading,
  Markdown,
  Modal,
  PageHeader,
  Progress,
  SectionHeader,
  Select,
  SourceBadge,
  StatusBadge,
  useToast,
} from '../components/ui'

const pageSize = 30

const verdictMeta: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }> = {
  approved: { label: '通过', tone: 'ok' },
  conditional: { label: '有条件通过', tone: 'warn' },
  rejected: { label: '不通过', tone: 'bad' },
  passed: { label: '通过', tone: 'ok' },
  failed: { label: '不通过', tone: 'bad' },
  '通过': { label: '通过', tone: 'ok' },
  '有条件通过': { label: '有条件通过', tone: 'warn' },
  '不通过': { label: '不通过', tone: 'bad' },
}

function verdictMetaOf(verdict?: string) {
  return verdictMeta[verdict || ''] || { label: verdict || '已签署', tone: 'neutral' as const }
}

export function ReportsPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const projectFilter = route.query.get('project') || 'all'
  const statusFilter = route.query.get('status') || 'all'
  const page = Math.max(1, Number(route.query.get('page') || 1))
  const offset = (page - 1) * pageSize
  const selectedId = route.query.get('run') || ''
  const [signTarget, setSignTarget] = useState<ReportSummary | null>(null)
  const [voidTarget, setVoidTarget] = useState<ReportSummary | null>(null)
  const [verdict, setVerdict] = useState('approved')
  const [signNote, setSignNote] = useState('')
  const [force, setForce] = useState(false)
  const [voidReason, setVoidReason] = useState('')
  const [actionError, setActionError] = useState('')

  const params = useMemo(() => {
    const next = new URLSearchParams({ offset: String(offset), limit: String(pageSize), status: statusFilter })
    if (projectFilter !== 'all') next.set('project', projectFilter)
    return next
  }, [offset, projectFilter, statusFilter])

  const reportsQuery = useQuery({
    queryKey: ['reports', projectFilter, statusFilter, offset],
    queryFn: () => api.reports(params),
  })
  const projectsQuery = useQuery({ queryKey: ['projects', 'with-archived'], queryFn: () => api.projects(true) })
  const selectedReport = (reportsQuery.data?.reports || []).find((item) => item.run_id === selectedId)
  const insightQuery = useQuery({ queryKey: ['insight', selectedId], queryFn: () => api.insight(selectedId), enabled: !!selectedId })
  const generate = useMutation({
    mutationFn: () => api.generateInsight(selectedId, true),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['insight', selectedId] })
      await reportsQuery.refetch()
      push('AI 总结已生成', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const reports = reportsQuery.data?.reports || []
  const total = reportsQuery.data?.total || 0
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const projects = projectsQuery.data || []
  const totalCases = reports.reduce((acc, report) => {
    acc.passed += report.passed || 0
    acc.total += report.total || 0
    return acc
  }, { passed: 0, total: 0 })
  const rate = rateOf(totalCases.passed, totalCases.total)
  const signedCount = reports.filter((report) => report.signed && !report.voided).length
  const voidedCount = reports.filter((report) => report.voided).length
  const selectedStatus = selectedReport ? statusOf({ run_id: selectedReport.run_id, summary: selectedReport.summary, conclusion: selectedReport.conclusion }) : 'none'

  const updateQuery = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(route.query)
    Object.entries(patch).forEach(([key, value]) => {
      if (!value || value === 'all') next.delete(key)
      else next.set(key, value)
    })
    next.delete('page')
    navigate(withQuery('/reports', next), { replace: true })
  }

  const openSign = (report: ReportSummary) => {
    setSignTarget(report)
    setSignNote(report.note || '')
    setVerdict(report.verdict && verdictMeta[report.verdict] ? report.verdict : 'approved')
    setForce(false)
    setActionError('')
  }

  const openVoid = (report: ReportSummary) => {
    setVoidTarget(report)
    setVoidReason('')
    setActionError('')
  }

  const sign = useMutation({
    mutationFn: () => {
      if (!signTarget) throw new Error('没有选中报告')
      if (signTarget.signed && !force) throw new Error('请先确认覆盖已签署结论')
      return api.signReport(signTarget.run_id, { verdict, note: signNote.trim(), force })
    },
    onSuccess: async () => {
      setSignTarget(null)
      await queryClient.invalidateQueries({ queryKey: ['reports'] })
      push(force ? '报告已重新签署；历史签名已留存' : '报告已签署并锁定', 'ok')
    },
    onError: (error: Error) => { setActionError(error.message); push(error.message, 'error') },
  })

  const voidReport = useMutation({
    mutationFn: () => {
      if (!voidTarget) throw new Error('没有选中报告')
      if (!voidReason.trim()) throw new Error('请填写作废理由')
      return api.voidReport(voidTarget.run_id, voidReason.trim())
    },
    onSuccess: async () => {
      setVoidTarget(null)
      await queryClient.invalidateQueries({ queryKey: ['reports'] })
      push('报告已作废；不可撤销，且不可再签署', 'ok')
    },
    onError: (error: Error) => { setActionError(error.message); push(error.message, 'error') },
  })

  useEffect(() => {
    setActionError('')
  }, [signTarget, voidTarget])

  return (
    <div className="page reports-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 账</>}
        title="报告"
        description="每次执行的可交付物。数字只从报告本体读取；人工结论签署后锁定，作废不可撤销。"
        actions={<><Button variant="ghost" icon="history" onClick={() => navigate('/history')}>执行账本</Button><Button variant="ghost" icon="refresh" onClick={() => reportsQuery.refetch()}>刷新</Button></>}
      />

      <div className="report-page-stats">
        <Kpi label="报告" value={total} sub="符合当前筛选" icon="report" />
        <Kpi label="当前页通过率" value={rate === null ? '—' : `${rate}%`} tone={rate !== null && rate < 80 ? 'bad' : 'ok'} sub={`${totalCases.passed}/${totalCases.total} 用例`} icon="check" />
        <Kpi label="当前页已签署" value={signedCount} tone="ok" sub="锁定后需 force 重签" icon="lock" />
        <Kpi label="当前页已作废" value={voidedCount} tone={voidedCount ? 'bad' : ''} sub="作废后不可再签" icon="warning" />
      </div>

      <section className="panel filter-panel">
        <div className="filter-row">
          <label className="filter-field">
            <span>项目</span>
            <Select value={projectFilter} onChange={(event) => updateQuery({ project: event.target.value })}>
              <option value="all">全部项目</option>
              <option value="unclassified">未归类</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}{project.archived_at ? '（已归档）' : ''}</option>)}
            </Select>
          </label>
          <label className="filter-field">
            <span>签署 / 结论</span>
            <Select value={statusFilter} onChange={(event) => updateQuery({ status: event.target.value })}>
              <option value="all">全部</option>
              <option value="unsigned">未签署</option>
              <option value="signed">已签署</option>
              <option value="voided">已作废</option>
              <option value="passed">运行通过</option>
              <option value="failed">运行失败</option>
              <option value="errored">运行异常</option>
            </Select>
          </label>
          <div className="filter-spacer" />
          <Badge tone="neutral">{total} 份报告</Badge>
          {(projectFilter !== 'all' || statusFilter !== 'all') ? <Button size="sm" variant="ghost" icon="close" onClick={() => navigate('/reports', { replace: true })}>清除筛选</Button> : null}
        </div>
        <p className="filter-hint"><Icon name="link" size={13} /> 列表来自 <code>/api/reports</code>；project / status / page 写入 URL。</p>
      </section>

      {reportsQuery.isLoading ? <Loading label="正在读取报告中心…" /> : null}
      {reportsQuery.error ? <ErrorState error={reportsQuery.error} onRetry={() => reportsQuery.refetch()} /> : null}

      {!reportsQuery.isLoading && !reportsQuery.error && !reports.length ? (
        <EmptyState icon="report" title="没有匹配的报告" actions={<Button size="sm" onClick={() => navigate('/reports', { replace: true })}>清除筛选</Button>}>
          报告随执行自动生成；先发起一次运行。
        </EmptyState>
      ) : null}

      <div className="report-card-grid">
        {reports.map((report) => (
          <ReportCard
            key={report.run_id}
            report={report}
            projects={projects}
            onOpen={() => updateQuery({ run: report.run_id })}
            onSign={() => openSign(report)}
            onVoid={() => openVoid(report)}
          />
        ))}
      </div>

      {pages > 1 ? (
        <div className="pagination panel">
          <span>第 {page} / {pages} 页 · 共 {total} 份</span>
          <div>
            <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => {
              const next = new URLSearchParams(route.query)
              if (page - 1 <= 1) next.delete('page')
              else next.set('page', String(page - 1))
              navigate(withQuery('/reports', next), { replace: true })
            }}>上一页</Button>
            <Button size="sm" variant="ghost" disabled={page >= pages} onClick={() => {
              const next = new URLSearchParams(route.query)
              next.set('page', String(page + 1))
              navigate(withQuery('/reports', next), { replace: true })
            }}>下一页</Button>
          </div>
        </div>
      ) : null}

      <section className="panel report-policy">
        <SectionHeader title="报告治理" description="可交付、可追溯、不可篡改。" icon="shield" />
        <div className="policy-grid">
          <div><Icon name="code" size={18} /><strong>数字来自报告本体</strong><p>AI 总结的 stats 字段逐字取自 report.summary，不采信模型数字。</p></div>
          <div><Icon name="spark" size={18} /><strong>来源必须标注</strong><p>LLM 与离线规则两种来源；降级原因在报告页明示。</p></div>
          <div><Icon name="lock" size={18} /><strong>签署后锁定</strong><p>已签署再签需 force，并保留历史签名；作废后不可再签。</p></div>
        </div>
      </section>

      <Drawer
        open={!!selectedId}
        onClose={() => updateQuery({ run: null })}
        title={selectedReport ? selectedReport.suite_name || `报告 ${shortId(selectedId)}` : '报告预览'}
        subtitle={selectedReport ? `${shortId(selectedId, 12)} · ${formatTime(selectedReport.started_at)}` : '读取中'}
        width="drawer-lg"
        footer={
          selectedId ? (
            <>
              <Button variant="ghost" onClick={() => updateQuery({ run: null })}>关闭</Button>
              <a className="btn btn-ghost btn-sm" href={`api/runs/${encodeURIComponent(selectedId)}/report.html`} target="_blank" rel="noopener noreferrer"><Icon name="external" size={14} />HTML</a>
              {selectedReport && !selectedReport.voided ? <Button variant="ghost" size="sm" icon="edit" onClick={() => openSign(selectedReport)}>{selectedReport.signed ? '重新签署' : '签署'}</Button> : null}
              {selectedReport && !selectedReport.voided ? <Button variant="ghost" size="sm" icon="trash" onClick={() => openVoid(selectedReport)}>作废</Button> : null}
              {selectedReport?.project_id ? <a className="btn btn-primary btn-sm" href={href(`/p/${selectedReport.project_id}/runs/${selectedId}`)}>完整报告 <Icon name="arrow" size={14} /></a> : null}
            </>
          ) : null
        }
      >
        {!selectedId ? null : !selectedReport ? <Loading label="正在读取报告元信息…" /> : (
          <div className="stack-lg">
            <div className="report-drawer-head">
              <StatusBadge status={selectedStatus} />
              <Badge tone="neutral">{selectedReport.trigger || 'manual'}</Badge>
              <SignBadge report={selectedReport} />
              {selectedReport.workflow_ref ? <Badge tone="ai"><Icon name="git" size={10} />{selectedReport.workflow_ref}</Badge> : null}
            </div>
            <div className="job-summary">
              <div><span>用例</span><strong>{selectedReport.passed || 0}/{selectedReport.total || 0}</strong></div>
              <div><span>结论</span><strong>{selectedReport.conclusion?.verdict || statusLabel(selectedStatus)}</strong></div>
              <div><span>耗时</span><strong>{formatDuration(selectedReport.duration_ms)}</strong></div>
              <div><span>AI 总结</span><strong>{selectedReport.has_insight ? (selectedReport.insight_source || '缓存') : '未生成'}</strong></div>
            </div>
            <Progress value={selectedReport.total ? ((selectedReport.passed || 0) / selectedReport.total) * 100 : 0} tone={selectedStatus === 'passed' ? 'ok' : 'bad'} />
            <SignatureBlock report={selectedReport} />
            <div>
              <div className="subsection-head">
                <h3 className="subsection-title">AI 总结</h3>
                {insightQuery.data?.available ? <SourceBadge source={insightQuery.data.source} degradedReason={insightQuery.data.degraded_reason} /> : <Badge tone="neutral">{selectedReport.has_insight ? '缓存可用' : '未生成'}</Badge>}
              </div>
              {insightQuery.isLoading ? <Loading label="读取总结缓存…" /> : insightQuery.data?.available ? (
                <>
                  {insightQuery.data.degraded_reason ? <div className="insight-degraded"><Icon name="warning" size={14} />{insightQuery.data.degraded_reason}</div> : null}
                  <Markdown value={insightQuery.data.summary_md} />
                </>
              ) : (
                <div className="insight-empty compact">
                  <Icon name="spark" size={18} />
                  <p>没有缓存的 AI 总结。生成前不会自动调用模型。</p>
                  <Button size="sm" variant="ghost" icon="spark" loading={generate.isPending} onClick={() => generate.mutate()}>生成总结</Button>
                </div>
              )}
            </div>
            <div>
              <h3 className="subsection-title">报告元信息</h3>
              <dl className="fact-list">
                <div><dt>Run ID</dt><dd className="mono">{selectedId}</dd></div>
                <div><dt>项目</dt><dd>{selectedReport.project_id || '未归类'}</dd></div>
                <div><dt>套件</dt><dd>{selectedReport.suite_name || '—'}</dd></div>
                <div><dt>发布版本</dt><dd className="mono">{selectedReport.workflow_ref || '—'}</dd></div>
                <div><dt>结束时间</dt><dd>{formatTime(selectedReport.finished_at)}</dd></div>
              </dl>
            </div>
            {selectedReport.voided ? (
              <div className="step-error"><Icon name="warning" size={14} />报告已作废，不可再签署；作废不可撤销。</div>
            ) : selectedReport.signed ? (
              <div className="notice-inline"><Icon name="lock" size={15} /><div><strong>已签署锁定</strong><p>再次签署会覆盖当前人工结论并要求 force；历史签名保留在审计中。</p></div></div>
            ) : (
              <div className="notice-inline"><Icon name="edit" size={15} /><div><strong>等待人工签署</strong><p>人工结论不会自动生成；签署后锁定，报告本体永久不变。</p></div></div>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={!!signTarget}
        onClose={() => { if (!sign.isPending) setSignTarget(null) }}
        title={signTarget?.signed ? '重新签署报告' : '签署报告'}
        subtitle={signTarget ? `${signTarget.run_id} · ${signTarget.suite_name || '报告'}` : ''}
        size="md"
        dismissible={!sign.isPending}
        footer={
          <>
            <Button variant="ghost" disabled={sign.isPending} onClick={() => setSignTarget(null)}>取消</Button>
            <Button
              variant="primary"
              icon="edit"
              loading={sign.isPending}
              disabled={signTarget?.voided || (signTarget?.signed && !force)}
              onClick={() => sign.mutate()}
            >{signTarget?.signed ? '覆盖并签署' : '确认签署'}</Button>
          </>
        }
      >
        {signTarget ? (
          <div className="stack-lg">
            {signTarget.voided ? <div className="step-error"><Icon name="warning" size={14} />报告已作废，不可再签。</div> : null}
            {signTarget.signed ? (
              <div className="sign-current">
                <span>当前结论</span><Badge tone={verdictMetaOf(signTarget.verdict).tone}>{verdictMetaOf(signTarget.verdict).label}</Badge>
                {signTarget.note ? <p>{signTarget.note}</p> : null}
                <small>{signTarget.signed_by || '—'} · {formatTime(signTarget.signed_at)}</small>
              </div>
            ) : null}
            <Field label="人工结论">
              <div className="verdict-grid">
                {[['approved', '通过', '可交付'], ['conditional', '有条件通过', '附带条件'], ['rejected', '不通过', '不可交付']].map(([value, label, hint]) => (
                  <button type="button" key={value} className={`verdict-option ${verdict === value ? 'is-active' : ''}`} onClick={() => setVerdict(value)}>
                    <strong>{label}</strong><span>{hint}</span>
                  </button>
                ))}
              </div>
            </Field>
            <Field label="签署意见" hint="可选；会随签署记录进入审计。">
              <textarea className="textarea" rows={4} value={signNote} onChange={(event) => setSignNote(event.target.value)} placeholder="写明依据、遗留项或附加条件。" />
            </Field>
            {signTarget.signed ? (
              <label className="force-check">
                <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
                <span><strong>我确认覆盖当前签署结论</strong><small>旧签名将留存历史；此操作会写入审计。</small></span>
              </label>
            ) : null}
            {actionError ? <div className="step-error"><Icon name="warning" size={14} />{actionError}</div> : null}
          </div>
        ) : null}
      </Modal>

      <Modal
        open={!!voidTarget}
        onClose={() => { if (!voidReport.isPending) setVoidTarget(null) }}
        title="作废报告"
        subtitle={voidTarget ? `${voidTarget.run_id} · 不可撤销` : ''}
        size="sm"
        dismissible={!voidReport.isPending}
        footer={<><Button variant="ghost" disabled={voidReport.isPending} onClick={() => setVoidTarget(null)}>取消</Button><Button variant="danger" icon="trash" loading={voidReport.isPending} disabled={!voidReason.trim()} onClick={() => voidReport.mutate()}>确认作废</Button></>}
      >
        <div className="stack-lg">
          <div className="step-error"><Icon name="warning" size={14} />作废后报告不可再签署，且不可撤销；报告本体与历史执行仍保留。</div>
          <Field label="作废理由" hint="必填；会写入审计。">
            <textarea className="textarea" rows={4} value={voidReason} onChange={(event) => setVoidReason(event.target.value)} placeholder="例如：被测目标错误，本次结果不可采信。" autoFocus />
          </Field>
          {actionError ? <div className="step-error"><Icon name="warning" size={14} />{actionError}</div> : null}
        </div>
      </Modal>
    </div>
  )
}

function SignBadge({ report }: { report: ReportSummary }) {
  if (report.voided) return <Badge tone="bad">已作废</Badge>
  if (report.signed) return <Badge tone="ok"><Icon name="lock" size={10} />已签署 · {verdictMetaOf(report.verdict).label}</Badge>
  return <Badge tone="warn">未签署</Badge>
}

function SignatureBlock({ report }: { report: ReportSummary }) {
  if (report.voided) {
    return (
      <div className="signature-block is-voided">
        <div className="signature-head"><Badge tone="bad">已作废</Badge><strong>不可再签署</strong></div>
        <p>{report.void_reason || '未记录理由'}</p>
        <small>{report.voided_by || '—'} · {formatTime(report.voided_at)}</small>
      </div>
    )
  }
  if (report.signed) {
    const meta = verdictMetaOf(report.verdict)
    return (
      <div className="signature-block is-signed">
        <div className="signature-head"><Badge tone={meta.tone}>{meta.label}</Badge><span className="signature-lock"><Icon name="lock" size={12} />人工结论已锁定</span></div>
        {report.note ? <p>{report.note}</p> : <p className="muted">无签署意见</p>}
        <small>{report.signed_by || '—'} · {formatTime(report.signed_at)}</small>
      </div>
    )
  }
  return <div className="signature-block"><div className="signature-head"><Badge tone="warn">未签署</Badge><span>报告尚未形成人工结论</span></div><p className="muted">AI 总结只是建议；人工签署后才生效。</p></div>
}

function ReportCard({ report, projects, onOpen, onSign, onVoid }: { report: ReportSummary; projects: any[]; onOpen: () => void; onSign: () => void; onVoid: () => void }) {
  const status = report.status ? statusOf({ run_id: report.run_id, summary: report.summary, conclusion: report.conclusion }) : 'done'
  const counts = { passed: report.passed || 0, total: report.total || 0 }
  const rate = rateOf(counts.passed, counts.total)
  const project = projects.find((item) => item.project_id === report.project_id)
  return (
    <article className={`report-card ${report.voided ? 'is-voided' : report.signed ? 'is-signed' : ''}`}>
      <button type="button" className="report-card-body" onClick={onOpen}>
        <div className="report-card-head">
          <StatusBadge status={status} />
          <span className="mono">{shortId(report.run_id, 10)}</span>
        </div>
        <h3>{report.suite_name || project?.name || '未归类报告'}</h3>
        <p>{project?.name || report.project_id || '未归类项目'} · {relativeTime(report.started_at)}</p>
        <div className="report-card-rate">
          <strong className={rate !== null && rate < 80 ? 'bad' : 'ok'}>{rate === null ? '—' : `${rate}%`}</strong>
          <span>{counts.total ? `${counts.passed}/${counts.total} 用例` : '无摘要'}</span>
        </div>
        <Progress value={rate ?? 0} tone={rate !== null && rate < 80 ? 'bad' : 'ok'} />
        <div className="report-card-flags">
          <SignBadge report={report} />
          {report.workflow_ref ? <Badge tone="ai"><Icon name="git" size={10} />{report.workflow_ref}</Badge> : null}
          {report.has_insight ? <Badge tone="neutral"><Icon name="spark" size={10} />AI 总结</Badge> : <Badge tone="neutral">无总结</Badge>}
        </div>
        <div className="report-card-meta">
          <span>{formatDuration(report.duration_ms)}</span>
          <span>{report.conclusion?.verdict || statusLabel(status)}</span>
          <span>{report.trigger || 'manual'}</span>
        </div>
      </button>
      <div className="report-card-foot">
        <span className="muted">{report.voided_at ? `作废于 ${relativeTime(report.voided_at)}` : report.signed_at ? `签署于 ${relativeTime(report.signed_at)}` : '等待人工签署'}</span>
        <div className="row-actions">
          <Button size="sm" variant="ghost" icon="eye" onClick={onOpen}>预览</Button>
          {!report.voided ? <Button size="sm" variant="ghost" icon={report.signed ? 'refresh' : 'edit'} onClick={onSign}>{report.signed ? '重签' : '签署'}</Button> : null}
          {!report.voided ? <Button size="sm" variant="ghost" icon="trash" onClick={onVoid}>作废</Button> : null}
        </div>
      </div>
    </article>
  )
}
