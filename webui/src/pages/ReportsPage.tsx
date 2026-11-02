import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import { useRoute, withQuery, href, navigate } from '../shared/router'
import { formatDuration, formatTime, rateOf, relativeTime, runCounts, runWhen, shortId, statusLabel, statusOf, classForStatus } from '../shared/utils'
import type { RunSummary } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  Drawer,
  EmptyState,
  ErrorState,
  Kpi,
  Loading,
  Markdown,
  PageHeader,
  Progress,
  SearchInput,
  SectionHeader,
  Select,
  SourceBadge,
  StatusBadge,
  useToast,
} from '../components/ui'

export function ReportsPage() {
  const route = useRoute()
  const { push } = useToast()
  const projectFilter = route.query.get('project') || 'all'
  const statusFilter = route.query.get('status') || 'all'
  const search = route.query.get('q') || ''
  const selectedId = route.query.get('run') || ''
  const [searchDraft, setSearchDraft] = useState(search)
  const projectsQuery = useQuery({ queryKey: ['projects'], queryFn: api.projects })
  const runsQuery = useQuery({ queryKey: ['reports-runs'], queryFn: () => api.runs(100) })
  const selectedRun = (runsQuery.data || []).find((run) => run.run_id === selectedId)
  const runDetailQuery = useQuery({ queryKey: ['run', selectedId], queryFn: () => api.run(selectedId), enabled: !!selectedId })
  const insightQuery = useQuery({ queryKey: ['insight', selectedId], queryFn: () => api.insight(selectedId), enabled: !!selectedId })
  const generate = useMutation({ mutationFn: () => api.generateInsight(selectedId, true) })
  const projects = projectsQuery.data || []
  const runs = runsQuery.data || []

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    return runs.filter((run) => {
      if (projectFilter !== 'all' && run.project_id !== projectFilter) return false
      if (statusFilter !== 'all' && statusOf(run) !== statusFilter) return false
      if (query && !`${run.run_id} ${run.suite_name || ''} ${run.meta?.suite || ''}`.toLowerCase().includes(query)) return false
      return true
    })
  }, [projectFilter, runs, search, statusFilter])

  const updateQuery = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(route.query)
    Object.entries(patch).forEach(([key, value]) => {
      if (!value || value === 'all') next.delete(key)
      else next.set(key, value)
    })
    navigate(withQuery('/reports', next), { replace: true })
  }

  const totalCases = filtered.reduce((acc, run) => {
    const counts = runCounts(run)
    acc.passed += counts.passed
    acc.total += counts.total
    return acc
  }, { passed: 0, total: 0 })
  const rate = rateOf(totalCases.passed, totalCases.total)
  const failed = filtered.filter((run) => ['failed', 'errored'].includes(statusOf(run))).length
  const selectedStatus = selectedRun ? statusOf(selectedRun) : 'none'

  return (
    <div className="page reports-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 账</>}
        title="报告"
        description="每次执行的可交付物。数字只从报告本体读取；AI 总结必须标注来源，人工结论才生效。"
        actions={<><Button variant="ghost" icon="history" onClick={() => navigate('/history')}>执行账本</Button><Button variant="ghost" icon="refresh" onClick={() => runsQuery.refetch()}>刷新</Button></>}
      />

      <div className="report-page-stats">
        <Kpi label="报告" value={filtered.length} sub="最近 100 次执行" icon="report" />
        <Kpi label="通过率" value={rate === null ? '—' : `${rate}%`} tone={rate !== null && rate < 80 ? 'bad' : 'ok'} sub={`${totalCases.passed}/${totalCases.total} 用例`} icon="check" />
        <Kpi label="需关注" value={failed} tone={failed ? 'bad' : 'ok'} sub="失败或异常执行" icon="warning" />
        <Kpi label="AI 来源" value="标注" sub="llm / rules 可追溯" icon="spark" />
      </div>

      <div className="catalog-toolbar panel">
        <SearchInput value={searchDraft} onChange={(value) => { setSearchDraft(value); updateQuery({ q: value }) }} placeholder="搜索报告、套件或 run_id" />
        <Select value={projectFilter} onChange={(event) => updateQuery({ project: event.target.value })} aria-label="按项目筛选">
          <option value="all">全部项目</option>
          {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
        </Select>
        <Select value={statusFilter} onChange={(event) => updateQuery({ status: event.target.value })} aria-label="按结论筛选">
          <option value="all">全部结论</option>
          <option value="passed">通过</option>
          <option value="failed">失败</option>
          <option value="errored">异常</option>
        </Select>
        <span className="toolbar-spacer" />
        <span className="toolbar-count">{filtered.length} / {runs.length}</span>
      </div>

      {runsQuery.isLoading ? <Loading label="正在读取报告…" /> : null}
      {runsQuery.error ? <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} /> : null}

      {!runsQuery.isLoading && !filtered.length ? (
        <EmptyState icon="report" title="没有匹配的报告" actions={<Button size="sm" onClick={() => navigate('/reports', { replace: true })}>清除筛选</Button>}>
          报告随执行自动生成；先发起一次运行。
        </EmptyState>
      ) : null}

      <div className="report-card-grid">
        {filtered.map((run) => <ReportCard key={run.run_id} run={run} projects={projects} onOpen={() => updateQuery({ run: run.run_id })} />)}
      </div>

      <section className="panel report-policy">
        <SectionHeader title="报告治理" description="可交付、可追溯、不可篡改。" icon="shield" />
        <div className="policy-grid">
          <div><Icon name="code" size={18} /><strong>数字来自报告本体</strong><p>AI 总结的 stats 字段逐字取自 report.summary，不采信模型数字。</p></div>
          <div><Icon name="spark" size={18} /><strong>来源必须标注</strong><p>LLM 与离线规则两种来源；降级原因在报告页明示。</p></div>
          <div><Icon name="lock" size={18} /><strong>签署后锁定</strong><p>人工结论签署依赖后端审计接口；当前页面不伪造签署状态。</p></div>
        </div>
      </section>

      <Drawer
        open={!!selectedId}
        onClose={() => updateQuery({ run: null })}
        title={selectedRun ? selectedRun.suite_name || selectedRun.meta?.suite || `报告 ${shortId(selectedId)}` : '报告预览'}
        subtitle={selectedRun ? `${shortId(selectedId, 12)} · ${formatTime(selectedRun.started_at)}` : '只读'}
        width="drawer-lg"
        footer={
          selectedId ? (
            <>
              <Button variant="ghost" onClick={() => updateQuery({ run: null })}>关闭</Button>
              <a className="btn btn-ghost btn-sm" href={`api/runs/${encodeURIComponent(selectedId)}/report.html`} target="_blank" rel="noopener noreferrer"><Icon name="external" size={14} />打开 HTML</a>
              {selectedRun?.project_id ? <a className="btn btn-primary btn-sm" href={href(`/p/${selectedRun.project_id}/runs/${selectedId}`)}>完整报告 <Icon name="arrow" size={14} /></a> : null}
            </>
          ) : null
        }
      >
        {selectedId ? (
          <div className="stack-lg">
            <div className="report-drawer-head">
              <StatusBadge status={selectedStatus} />
              <Badge tone="neutral">{selectedRun?.meta?.trigger || selectedRun?.meta?.mode || '手动'}</Badge>
              {selectedRun?.duration_ms ? <span className="mono">{formatDuration(selectedRun.duration_ms)}</span> : null}
            </div>
            <div className="job-summary">
              <div><span>用例</span><strong>{selectedRun?.summary?.passed || 0}/{selectedRun?.summary?.total || 0}</strong></div>
              <div><span>失败</span><strong className={selectedRun?.summary?.failed ? 'bad' : ''}>{selectedRun?.summary?.failed || 0}</strong></div>
              <div><span>异常</span><strong className={selectedRun?.summary?.errored ? 'bad' : ''}>{selectedRun?.summary?.errored || 0}</strong></div>
              <div><span>耗时</span><strong>{formatDuration(selectedRun?.duration_ms)}</strong></div>
            </div>
            <Progress value={selectedRun?.summary?.total ? ((selectedRun.summary.passed || 0) / selectedRun.summary.total) * 100 : 0} tone={selectedStatus === 'passed' ? 'ok' : 'bad'} />
            <div>
              <div className="subsection-head">
                <h3 className="subsection-title">AI 总结</h3>
                {insightQuery.data?.available ? <SourceBadge source={insightQuery.data.source} degradedReason={insightQuery.data.degraded_reason} /> : <Badge tone="neutral">未生成</Badge>}
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
                  <Button size="sm" variant="ghost" icon="spark" loading={generate.isPending} onClick={async () => { try { await generate.mutateAsync(); await insightQuery.refetch(); push('总结已生成', 'ok') } catch (error) { push(error instanceof Error ? error.message : '生成失败', 'error') } }}>生成总结</Button>
                </div>
              )}
            </div>
            <div>
              <h3 className="subsection-title">报告元信息</h3>
              <dl className="fact-list">
                <div><dt>Run ID</dt><dd className="mono">{selectedId}</dd></div>
                <div><dt>项目</dt><dd>{selectedRun?.project_id || '未归类'}</dd></div>
                <div><dt>套件</dt><dd>{selectedRun?.suite_name || selectedRun?.meta?.suite || '—'}</dd></div>
                <div><dt>结论</dt><dd>{selectedRun?.conclusion?.verdict || statusLabel(selectedStatus)}</dd></div>
                <div><dt>发布版本</dt><dd className="mono">{selectedRun?.meta?.workflow_ref || '—'}</dd></div>
                <div><dt>结束时间</dt><dd>{formatTime(selectedRun?.finished_at)}</dd></div>
              </dl>
            </div>
            {runDetailQuery.data ? (
              <div className="notice-inline">
                <Icon name="shield" size={15} />
                <div><strong>只读报告</strong><p>报告内容来自磁盘报告；签署结论需要后端审计接口，前端不会把本地操作伪装成签署。</p></div>
              </div>
            ) : null}
          </div>
        ) : null}
      </Drawer>
    </div>
  )
}

function ReportCard({ run, projects, onOpen }: { run: RunSummary; projects: any[]; onOpen: () => void }) {
  const status = statusOf(run)
  const counts = runCounts(run)
  const rate = rateOf(counts.passed, counts.total)
  const project = projects.find((item) => item.project_id === run.project_id)
  return (
    <article className="report-card">
      <button type="button" className="report-card-body" onClick={onOpen}>
        <div className="report-card-head">
          <StatusBadge status={status} />
          <span className="mono">{shortId(run.run_id, 10)}</span>
        </div>
        <h3>{run.suite_name || run.meta?.suite || project?.name || '未归类报告'}</h3>
        <p>{project?.name || run.project_id || '未归类项目'} · {relativeTime(runWhen(run))}</p>
        <div className="report-card-rate">
          <strong className={rate !== null && rate < 80 ? 'bad' : 'ok'}>{rate === null ? '—' : `${rate}%`}</strong>
          <span>{counts.total ? `${counts.passed}/${counts.total} 用例` : '无摘要'}</span>
        </div>
        <Progress value={rate ?? 0} tone={rate !== null && rate < 80 ? 'bad' : 'ok'} />
        <div className="report-card-meta">
          <span>{formatDuration(run.duration_ms)}</span>
          <span>{run.summary?.steps || 0} 步</span>
          <span>{run.meta?.trigger || '手动'}</span>
        </div>
      </button>
      <div className="report-card-foot">
        <span className="badge badge-neutral"><span className={`status-dot tone-${classForStatus(status)}`} />{statusLabel(status)}</span>
        <button type="button" className="inline-link" onClick={onOpen}>预览 <Icon name="arrow" size={12} /></button>
      </div>
    </article>
  )
}
