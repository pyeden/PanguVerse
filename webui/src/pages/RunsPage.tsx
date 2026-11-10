import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import { navigate } from '../shared/router'
import { PageHeader, SectionHeader, Button, Loading, ErrorState } from '../components/ui'
import { RunTable } from '../components/RunTable'
import { statusOf } from '../shared/utils'
import type { RunSummary } from '../shared/types'

type Filter = 'all' | 'failed' | 'active'

function isFailed(status: string): boolean {
  return status === 'failed' || status === 'error' || status === 'errored'
}

function isActive(status: string): boolean {
  return status === 'running' || status === 'queued' || status === 'cancelling'
}

export function RunsPage() {
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

  return (
    <div className="page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 运行</>}
        title="运行"
        description="全部运行记录；打开详情查看报告与产出物。"
        actions={<Button size="sm" variant="ghost" icon="refresh" onClick={() => runsQuery.refetch()}>刷新</Button>}
      />

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
    </div>
  )
}
