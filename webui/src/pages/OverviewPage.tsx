import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, studioApi } from '../shared/api'
import { href, navigate } from '../shared/router'
import { relativeTime, runCounts, runWhen, shortId, statusOf, toMs } from '../shared/utils'
import type { RunSummary, Workflow } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  SectionHeader,
  StatusBadge,
} from '../components/ui'

const PENDING_STATES = new Set(['draft', 'review'])

const STATE_LABEL: Record<string, string> = {
  draft: '草稿',
  review: '待审',
  approved: '已批准',
  published: '已发布',
  rejected: '已拒绝',
  deprecated: '已归档',
}

function stateTone(state?: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (state === 'published') return 'ok'
  if (state === 'review' || state === 'approved') return 'warn'
  if (state === 'rejected') return 'bad'
  return 'neutral'
}

function isFailed(status: string): boolean {
  return status === 'failed' || status === 'error' || status === 'errored'
}

function todayStartMs(): number {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
}

function runLabel(run: RunSummary): string {
  return String(run.suite_name || run.meta?.suite || run.meta?.task_name || run.meta?.workflow_ref || `运行 ${shortId(run.run_id)}`)
}

export function OverviewPage() {
  const runsQuery = useQuery({ queryKey: ['overview-runs'], queryFn: () => api.runs(100), staleTime: 15_000 })
  const draftsQuery = useQuery({ queryKey: ['overview-drafts'], queryFn: () => studioApi.listDrafts(), staleTime: 10_000 })

  const runs = runsQuery.data || []
  const workflows = draftsQuery.data || []
  const loading = runsQuery.isLoading || draftsQuery.isLoading
  const hasError = runsQuery.error || draftsQuery.error

  const todayRuns = useMemo(() => {
    const start = todayStartMs()
    return runs
      .filter((run) => {
        const at = toMs(runWhen(run))
        return at >= start && at <= Date.now() + 60_000
      })
      .sort((a, b) => toMs(runWhen(b)) - toMs(runWhen(a)))
  }, [runs])

  const failedRuns = useMemo(() => todayRuns.filter((run) => isFailed(statusOf(run))), [todayRuns])

  const caseStats = useMemo(() => {
    let passed = 0
    let total = 0
    todayRuns.forEach((run) => {
      const counts = runCounts(run)
      passed += counts.passed
      total += counts.total
    })
    return { passed, total, rate: total ? Math.round((passed / total) * 1000) / 10 : null }
  }, [todayRuns])

  const pendingTasks = useMemo(
    () => workflows
      .filter((workflow) => PENDING_STATES.has(String(workflow.state || 'draft')))
      .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0)),
    [workflows],
  )

  const recentRuns = useMemo(
    () => [...runs].sort((a, b) => toMs(runWhen(b)) - toMs(runWhen(a))).slice(0, 5),
    [runs],
  )

  const refresh = () => {
    void runsQuery.refetch()
    void draftsQuery.refetch()
  }

  return (
    <div className="page overview-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 首页 · 概览</>}
        title="概览"
        description="今日运行、待处理任务与最近运行一览。"
        actions={
          <>
            <Button size="sm" variant="ghost" icon="refresh" onClick={refresh}>刷新</Button>
            <Button size="sm" variant="primary" icon="spark" onClick={() => navigate('/next/console')}>新建任务</Button>
          </>
        }
      />

      <div className="kpi-grid" aria-label="关键指标">
        <div className="kpi">
          <div className="kpi-head"><span>今日运行数</span><Icon name="play" size={15} /></div>
          <strong className="kpi-value">{loading ? '—' : todayRuns.length}</strong>
          <span className="kpi-sub">{loading ? '正在统计' : todayRuns.length ? `最近一次 ${relativeTime(runWhen(todayRuns[0]))}` : '今天还没有运行'}</span>
        </div>
        <div className="kpi">
          <div className="kpi-head"><span>成功率</span><Icon name="check" size={15} /></div>
          <strong className={`kpi-value ${caseStats.rate !== null && caseStats.rate < 80 ? 'bad' : caseStats.rate !== null ? 'ok' : ''}`}>
            {loading ? '—' : caseStats.rate === null ? '—' : `${caseStats.rate}%`}
          </strong>
          <span className="kpi-sub">{loading ? '正在统计' : caseStats.total ? `${caseStats.passed}/${caseStats.total} 用例通过` : '今日暂无用例数据'}</span>
        </div>
        <div className="kpi">
          <div className="kpi-head"><span>待审任务数</span><Icon name="tasks" size={15} /></div>
          <strong className="kpi-value">{loading ? '—' : pendingTasks.length}</strong>
          <span className="kpi-sub">{loading ? '正在统计' : pendingTasks.length ? '草稿或待审，等你推进' : '没有待处理任务'}</span>
        </div>
        <div className="kpi">
          <div className="kpi-head"><span>失败数</span><Icon name="warning" size={15} /></div>
          <strong className={`kpi-value ${failedRuns.length ? 'bad' : 'ok'}`}>{loading ? '—' : failedRuns.length}</strong>
          <span className="kpi-sub">{loading ? '正在统计' : failedRuns.length ? '今日运行失败，请查看运行记录' : '今日没有失败运行'}</span>
        </div>
      </div>

      {loading ? <Loading label="正在读取概览…" /> : null}
      {hasError ? (
        <ErrorState
          error={runsQuery.error || draftsQuery.error}
          onRetry={refresh}
          title="概览读取失败"
        />
      ) : null}

      <div className="overview-grid">
        <section className="panel" aria-labelledby="overview-pending-title">
          <SectionHeader
            icon="tasks"
            title={<span id="overview-pending-title">待处理任务</span>}
            description={`${pendingTasks.length} 个草稿或待审任务`}
            actions={<a className="inline-link" href={href('/tasks')}>查看全部任务</a>}
          />
          {!loading && !pendingTasks.length ? (
            <EmptyState icon="tasks" title="没有待处理任务" actions={<Button size="sm" variant="primary" icon="spark" onClick={() => navigate('/next/console')}>新建任务</Button>} />
          ) : null}
          {pendingTasks.length ? (
            <div className="overview-list">
              {pendingTasks.slice(0, 5).map((workflow: Workflow) => (
                <a className="overview-row" key={workflow.wid} href={href(`/studio/${workflow.wid}`)}>
                  <span className={`task-icon tone-${stateTone(workflow.state) === 'ok' ? 'cyan' : stateTone(workflow.state) === 'warn' ? 'warn' : 'neutral'}`}>
                    <Icon name="flow" size={17} />
                  </span>
                  <span className="overview-row-main">
                    <strong className="overview-row-title">{workflow.name || workflow.wid}</strong>
                    <span className="overview-row-meta">{workflow.intent || '无意图文本'}</span>
                  </span>
                  <span className="overview-row-side">
                    <Badge tone={stateTone(workflow.state)}>{STATE_LABEL[workflow.state || 'draft'] || workflow.state}</Badge>
                    <span className="overview-row-time">{relativeTime(workflow.updated_at)}</span>
                  </span>
                </a>
              ))}
            </div>
          ) : null}
        </section>

        <section className="panel" aria-labelledby="overview-runs-title">
          <SectionHeader
            icon="play"
            title={<span id="overview-runs-title">最近运行</span>}
            description="最近 5 次运行，含状态与用例结果"
            actions={<a className="inline-link" href={href('/runs')}>全部运行</a>}
          />
          {!loading && !recentRuns.length ? (
            <EmptyState icon="history" title="还没有运行记录" actions={<Button size="sm" variant="primary" icon="spark" onClick={() => navigate('/next/console')}>新建任务</Button>} />
          ) : null}
          {recentRuns.length ? (
            <div className="overview-list">
              {recentRuns.map((run) => {
                const status = statusOf(run)
                const counts = runCounts(run)
                return (
                  <a className="overview-row" key={run.run_id} href={href(`/runs/${run.run_id}`)}>
                    <StatusBadge status={status} />
                    <span className="overview-row-main">
                      <strong className="overview-row-title">{runLabel(run)}</strong>
                      <span className="overview-row-meta mono">{shortId(run.run_id, 12)}{counts.total ? ` · ${counts.passed}/${counts.total} 用例` : ''}</span>
                    </span>
                    <span className="overview-row-side">
                      <span className="overview-row-time">{relativeTime(runWhen(run))}</span>
                    </span>
                  </a>
                )
              })}
            </div>
          ) : null}
        </section>
      </div>

      <section className="panel" aria-labelledby="overview-quick-title">
        <SectionHeader icon="bolt" title={<span id="overview-quick-title">快捷入口</span>} description="最常用的三个动作" />
        <div className="overview-quick-grid">
          <a className="overview-quick is-primary" href={href('/next/console')}>
            <span className="overview-quick-icon"><Icon name="spark" size={18} /></span>
            <span><strong>新建任务</strong><small>一句话生成可审阅任务</small></span>
            <Icon name="arrow" size={15} className="overview-quick-arrow" />
          </a>
          <a className="overview-quick" href={href('/tasks')}>
            <span className="overview-quick-icon"><Icon name="tasks" size={18} /></span>
            <span><strong>查看全部任务</strong><small>搜索、筛选与归档</small></span>
            <Icon name="arrow" size={15} className="overview-quick-arrow" />
          </a>
          <a className="overview-quick" href={href('/runs')}>
            <span className="overview-quick-icon"><Icon name="play" size={18} /></span>
            <span><strong>运行记录</strong><small>执行、报告与产出物</small></span>
            <Icon name="arrow" size={15} className="overview-quick-arrow" />
          </a>
        </div>
      </section>
    </div>
  )
}
