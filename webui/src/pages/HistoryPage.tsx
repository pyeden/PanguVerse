import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import { navigate, useRoute, withQuery, href } from '../shared/router'
import { rateOf, statusOf, runCounts, formatDuration, shortId, statusLabel } from '../shared/utils'
import type { RunSummary } from '../shared/types'
import { Icon } from '../components/Icon'
import { RunTable } from '../components/RunTable'
import {
  Badge,
  Button,
  Drawer,
  EmptyState,
  ErrorState,
  Kpi,
  Loading,
  PageHeader,
  SectionHeader,
  Select,
  StatusBadge,
} from '../components/ui'

export function HistoryPage() {
  const route = useRoute()
  const projectFilter = route.query.get('project') || 'all'
  const suiteFilter = route.query.get('suite') || 'all'
  const statusFilter = route.query.get('status') || 'all'
  const page = Math.max(1, Number(route.query.get('page') || 1))
  const focus = route.query.get('focus') || ''
  const limit = 20
  const offset = (page - 1) * limit
  const projectsQuery = useQuery({ queryKey: ['projects'], queryFn: api.projects })
  const projects = projectsQuery.data || []
  const selectedProject = projects.find((project) => project.project_id === projectFilter)

  const params = useMemo(() => {
    const next = new URLSearchParams({ limit: String(limit), offset: String(offset), status: statusFilter })
    if (projectFilter !== 'all') next.set('project', projectFilter)
    if (suiteFilter !== 'all') next.set('suite', suiteFilter)
    return next
  }, [limit, offset, projectFilter, statusFilter, suiteFilter])

  const runsQuery = useQuery({ queryKey: ['global-history', projectFilter, suiteFilter, statusFilter, offset], queryFn: () => api.queryRuns(params) })
  const focusQuery = useQuery({ queryKey: ['run', focus], queryFn: () => api.run(focus), enabled: !!focus })
  const runs = runsQuery.data?.items || []
  const total = runsQuery.data?.total || 0
  const pages = Math.max(1, Math.ceil(total / limit))
  const pagePassed = runs.reduce((sum, run) => sum + runCounts(run).passed, 0)
  const pageTotal = runs.reduce((sum, run) => sum + runCounts(run).total, 0)
  const pageRate = rateOf(pagePassed, pageTotal)

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(route.query)
    if (!value || value === 'all') next.delete(key)
    else next.set(key, value)
    next.delete('page')
    navigate(withQuery('/history', next), { replace: true })
  }

  const exportCsv = () => {
    const header = ['run_id', 'project_id', 'suite_id', 'status', 'passed', 'total', 'duration_ms', 'started_at']
    const rows = runs.map((run) => {
      const counts = runCounts(run)
      return [run.run_id, run.project_id || '', run.suite_id || '', statusOf(run), counts.passed, counts.total, run.duration_ms || 0, run.started_at || ''].map((value) => `"${String(value).replace(/"/g, '""')}"`).join(',')
    })
    const blob = new Blob([[header.join(','), ...rows].join('\n')], { type: 'text/csv;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `panguverse-history-${new Date().toISOString().slice(0, 10)}.csv`
    link.click()
    URL.revokeObjectURL(link.href)
  }

  return (
    <div className="page history-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 账</>}
        title="历史"
        description="跨项目、任务的执行账本。筛选与分页写入 URL；执行不可删除，只能重跑或追加审计注记。"
        actions={<><Button variant="ghost" icon="download" onClick={exportCsv} disabled={!runs.length}>导出当前页</Button><Button variant="ghost" icon="report" onClick={() => navigate('/reports')}>报告</Button></>}
      />

      <div className="history-kpis">
        <Kpi label="筛选结果" value={total} sub="条落盘执行" icon="history" />
        <Kpi label="本页通过率" value={pageRate === null ? '—' : `${pageRate}%`} tone={pageRate !== null && pageRate < 80 ? 'bad' : 'ok'} sub={`${pagePassed}/${pageTotal} 用例`} icon="check" />
        <Kpi label="本页失败" value={runs.filter((run) => ['failed', 'errored'].includes(statusOf(run))).length} tone="bad" sub="失败与异常分开归类" icon="warning" />
        <Kpi label="覆盖项目" value={new Set(runs.map((run) => run.project_id).filter(Boolean)).size} sub="当前页去重" icon="projects" />
      </div>

      <section className="panel filter-panel">
        <div className="filter-row">
          <label className="filter-field"><span>项目</span>
            <Select value={projectFilter} onChange={(event) => { update('project', event.target.value); const next = new URLSearchParams(route.query); next.delete('suite'); next.delete('project'); if (event.target.value !== 'all') next.set('project', event.target.value); next.delete('page'); navigate(withQuery('/history', next), { replace: true }) }}>
              <option value="all">全部项目</option>
              <option value="unclassified">未归类</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
            </Select>
          </label>
          <label className="filter-field"><span>套件</span>
            <Select value={suiteFilter} onChange={(event) => update('suite', event.target.value)} disabled={!selectedProject}>
              <option value="all">全部套件</option>
              {(selectedProject?.suites || []).map((suite) => <option key={suite.suite_id} value={suite.suite_id}>{suite.name}</option>)}
            </Select>
          </label>
          <label className="filter-field"><span>结果</span>
            <Select value={statusFilter} onChange={(event) => update('status', event.target.value)}>
              <option value="all">全部结果</option>
              <option value="passed">通过</option>
              <option value="failed">失败</option>
              <option value="errored">异常</option>
              <option value="done">已结束</option>
            </Select>
          </label>
          <div className="filter-spacer" />
          {(projectFilter !== 'all' || suiteFilter !== 'all' || statusFilter !== 'all') ? <Button size="sm" variant="ghost" icon="close" onClick={() => navigate('/history', { replace: true })}>清除筛选</Button> : null}
        </div>
        <p className="filter-hint"><Icon name="link" size={13} /> URL 状态：project={projectFilter} · suite={suiteFilter} · status={statusFilter} · page={page}</p>
      </section>

      <section className="panel table-panel">
        {runsQuery.isLoading ? <Loading label="正在读取执行账本…" /> : runsQuery.error ? <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} /> : (
          <RunTable runs={runs} projects={projects} emptyText="没有匹配的执行记录" emptyAction={<Button size="sm" onClick={() => navigate('/history')}>清除筛选</Button>} />
        )}
        {pages > 1 ? (
          <div className="pagination">
            <span>第 {page} / {pages} 页 · 共 {total} 条</span>
            <div>
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => {
                const next = new URLSearchParams(route.query)
                if (page - 1 <= 1) next.delete('page')
                else next.set('page', String(page - 1))
                navigate(withQuery('/history', next), { replace: true })
              }}>上一页</Button>
              <Button size="sm" variant="ghost" disabled={page >= pages} onClick={() => {
                const next = new URLSearchParams(route.query)
                next.set('page', String(page + 1))
                navigate(withQuery('/history', next), { replace: true })
              }}>下一页</Button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel ledger-note">
        <SectionHeader title="账本不可删" description="历史是审计链的一部分。" icon="shield" />
        <div className="ledger-row">
          <div><Icon name="database" size={18} /><strong>磁盘报告是唯一真值</strong><p>列表与数字从 report.json 还原；服务重启不会丢失。</p></div>
          <div><Icon name="git" size={18} /><strong>发布版本可回溯</strong><p>报告 meta 携带 workflow_ref 时，可直接跳到 wid@rev 的审阅记录。</p></div>
          <div><Icon name="refresh" size={18} /><strong>重跑生成新执行</strong><p>旧记录保持不变，parent_execution_id 建立关联。</p></div>
        </div>
      </section>

      <Drawer
        open={!!focus}
        onClose={() => {
          const next = new URLSearchParams(route.query)
          next.delete('focus')
          navigate(withQuery('/history', next), { replace: true })
        }}
        title={focus ? `执行 ${shortId(focus)}` : '执行详情'}
        subtitle="只读预览"
        width="drawer-md"
        footer={focus ? <Button variant="primary" icon="external" onClick={() => navigate(`/history?focus=${focus}`)}>返回列表</Button> : null}
      >
        {focusQuery.isLoading ? <Loading label="正在读取报告…" /> : focusQuery.error ? <ErrorState error={focusQuery.error} onRetry={() => focusQuery.refetch()} /> : focusQuery.data ? (
          <div className="stack-lg">
            <div className="detail-status-row"><StatusBadge status={statusOf({ run_id: focus, summary: focusQuery.data.report.summary, conclusion: focusQuery.data.report.conclusion })} /><Badge tone="neutral">{focusQuery.data.report.meta?.suite || '未归类'}</Badge></div>
            <div className="job-summary">
              <div><span>用例</span><strong>{focusQuery.data.report.summary?.passed || 0}/{focusQuery.data.report.summary?.total || 0}</strong></div>
              <div><span>耗时</span><strong>{formatDuration(focusQuery.data.report.duration_ms)}</strong></div>
              <div><span>结论</span><strong>{focusQuery.data.report.conclusion?.verdict || statusLabel(statusOf({ run_id: focus, summary: focusQuery.data.report.summary }))}</strong></div>
              <div><span>项目</span><strong>{focusQuery.data.report.meta?.project_id || '—'}</strong></div>
            </div>
            {focusQuery.data.report.meta?.project_id ? <a className="btn btn-primary btn-sm" href={href(`/p/${focusQuery.data.report.meta.project_id}/runs/${focus}`)}>打开完整报告 <Icon name="arrow" size={14} /></a> : <p className="muted">该运行没有项目归属，完整报告请通过 run_id 访问。</p>}
          </div>
        ) : null}
      </Drawer>
    </div>
  )
}
