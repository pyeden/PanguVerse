import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import { navigate, useRoute } from '../shared/router'
import { PageHeader, SectionHeader, Badge, Button, Loading, ErrorState } from '../components/ui'
import { RunTable } from '../components/RunTable'
import { ReportsPage } from './ReportsPage'
import { ArtifactsPage } from './ArtifactsPage'
import { statusOf } from '../shared/utils'
import type { RunSummary } from '../shared/types'

type View = 'runs' | 'reports' | 'artifacts'
type Filter = 'all' | 'failed' | 'active'

function isFailed(status: string): boolean {
  return status === 'failed' || status === 'error' || status === 'errored'
}

function isActive(status: string): boolean {
  return status === 'running' || status === 'queued' || status === 'cancelling'
}

export function RunsPage() {
  const route = useRoute()
  const view = (route.query.get('view') as View) || 'runs'
  const [filter, setFilter] = useState<Filter>('all')
  const runsQuery = useQuery({ queryKey: ['runs-unified'], queryFn: () => api.runs(100), staleTime: 15_000 })
  const projectsQuery = useQuery({ queryKey: ['projects'], queryFn: () => api.projects(), staleTime: 20_000 })
  const runs = runsQuery.data || []
  const projects = projectsQuery.data || []

  const filtered = useMemo<RunSummary[]>(() => runs.filter((run) => {
    const status = statusOf(run)
    if (filter === 'failed') return isFailed(status)
    if (filter === 'active') return isActive(status)
    return true
  }), [runs, filter])

  const failedCount = runs.filter((run) => isFailed(statusOf(run))).length
  const activeCount = runs.filter((run) => isActive(statusOf(run))).length

  const go = (next: View) => navigate(`/runs?view=${next}`, { replace: true })

  return (
    <div className="page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 运行 · 统一入口</>}
        title="运行"
        description="执行、报告、产出物统一在这里；不再分执行 / 历史 / 报告 / 产出物四个入口。"
        actions={<Button size="sm" variant="ghost" icon="refresh" onClick={() => runsQuery.refetch()}>刷新</Button>}
      />

      <div className="segmented" role="tablist" aria-label="运行视图">
        <Button size="sm" variant={view === 'runs' ? 'soft' : 'ghost'} onClick={() => go('runs')}>运行</Button>
        <Button size="sm" variant={view === 'reports' ? 'soft' : 'ghost'} onClick={() => go('reports')}>报告</Button>
        <Button size="sm" variant={view === 'artifacts' ? 'soft' : 'ghost'} onClick={() => go('artifacts')}>产出物</Button>
      </div>

      {view === 'runs' ? (
        <>
          <SectionHeader
            icon="play"
            title="执行记录"
            description={`${runs.length} 次运行 · 失败 ${failedCount} · 进行中 ${activeCount}`}
            actions={
              <div className="segmented" role="group" aria-label="运行筛选">
                <Button size="sm" variant={filter === 'all' ? 'soft' : 'ghost'} onClick={() => setFilter('all')}>全部 {runs.length}</Button>
                <Button size="sm" variant={filter === 'failed' ? 'soft' : 'ghost'} onClick={() => setFilter('failed')}>失败 {failedCount}</Button>
                <Button size="sm" variant={filter === 'active' ? 'soft' : 'ghost'} onClick={() => setFilter('active')}>进行中 {activeCount}</Button>
              </div>
            }
          />
          {runsQuery.isLoading ? <Loading label="正在读取运行…" /> : null}
          {runsQuery.error ? <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} title="运行读取失败" /> : null}
          {!runsQuery.isLoading && !runsQuery.error ? (
            <RunTable
              runs={filtered}
              projects={projects}
              emptyAction={<Button size="sm" variant="primary" icon="spark" onClick={() => navigate('/next/console')}>去新建任务</Button>}
              emptyText={filter === 'all' ? '还没有运行记录' : '当前筛选下没有运行'}
            />
          ) : null}
          <div className="panel" style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <Badge tone="neutral">统一 Run 对象</Badge>
            <span className="muted" style={{ fontSize: 11 }}>
              一次运行就是一个 Run：执行、报告、产出物都在详情页内查看；旧入口会自动跳到对应视图。
            </span>
          </div>
        </>
      ) : null}

      {view === 'reports' ? <ReportsPage /> : null}
      {view === 'artifacts' ? <ArtifactsPage /> : null}
    </div>
  )
}
