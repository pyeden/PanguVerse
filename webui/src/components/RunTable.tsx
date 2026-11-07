import { Icon } from './Icon'
import { Badge, EmptyState, StatusBadge } from './ui'
import { formatDuration, relativeTime, runCounts, runWhen, shortId, statusOf } from '../shared/utils'
import { href, withQuery } from '../shared/router'
import type { Project, RunSummary } from '../shared/types'

export function RunTable({
  runs,
  projects,
  showProject = true,
  selected = [],
  onToggle,
  emptyAction,
  emptyText = '没有匹配的执行记录',
}: {
  runs: RunSummary[]
  projects: Project[]
  showProject?: boolean
  selected?: string[]
  onToggle?: (runId: string) => void
  emptyAction?: React.ReactNode
  emptyText?: string
}) {
  if (!runs.length) {
    return <EmptyState icon="history" title={emptyText} actions={emptyAction}>调整筛选条件，或发起新的运行。</EmptyState>
  }
  return (
    <div className="run-table-wrap">
      <table className="run-table">
        <thead>
          <tr>
            {onToggle ? <th className="check-col" aria-label="选择对比" /> : null}
            <th>状态</th>
            {showProject ? <th>项目 / 套件</th> : <th>套件</th>}
            <th>执行</th>
            <th>结果</th>
            <th>耗时</th>
            <th>触发</th>
            <th>时间</th>
            <th aria-label="打开" />
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const status = statusOf(run)
            const counts = runCounts(run)
            const project = projects.find((item) => item.project_id === run.project_id)
            const suite = project?.suites?.find((item) => item.suite_id === run.suite_id)
            const pid = run.project_id || project?.project_id
            const runHref = pid ? href(`/p/${pid}/runs/${run.run_id}`) : href(`/history?focus=${run.run_id}`)
            const trigger = String(run.meta?.trigger || run.meta?.mode || (run.meta?.workflow_ref ? 'task' : 'manual'))
            return (
              <tr key={run.run_id} className={selected.includes(run.run_id) ? 'is-selected' : ''}>
                {onToggle ? (
                  <td className="check-col">
                    <input
                      type="checkbox"
                      checked={selected.includes(run.run_id)}
                      onChange={() => onToggle(run.run_id)}
                      aria-label={`选择执行 ${run.run_id} 进行对比`}
                    />
                  </td>
                ) : null}
                <td data-label="状态"><StatusBadge status={status} /></td>
                <td data-label="项目 / 套件">
                  <div className="run-cell-main">
                    <strong>{suite?.name || run.suite_name || run.meta?.suite || '未归类运行'}</strong>
                    <span>{project?.name || (run.project_id ? run.project_id : '未归类')}</span>
                  </div>
                </td>
                <td data-label="执行">
                  <a className="mono run-id-link" href={runHref}>{shortId(run.run_id, 12)}</a>
                  {run.run_id ? (
                    <a className="run-artifacts-mini" data-testid="run-artifacts-link" data-run-id={run.run_id} href={href(withQuery('/artifacts', { run: run.run_id }))} aria-label={`查看执行 ${run.run_id} 的产出物`}>
                      <Icon name="archive" size={11} />产出物
                    </a>
                  ) : null}
                </td>
                <td data-label="结果"><span className="mono">{counts.total ? `${counts.passed}/${counts.total}` : '—'}</span></td>
                <td data-label="耗时"><span className="mono nowrap">{formatDuration(run.duration_ms)}</span></td>
                <td data-label="触发"><Badge tone={trigger === 'debug' ? 'warn' : 'neutral'}>{triggerLabel(trigger)}</Badge></td>
                <td data-label="时间"><span className="nowrap muted">{relativeTime(runWhen(run))}</span></td>
                <td className="open-col"><a href={runHref} className="table-open" aria-label="打开运行详情"><Icon name="arrow" size={15} /></a></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function triggerLabel(value: string): string {
  const map: Record<string, string> = {
    task: '任务',
    studio: '工作台',
    debug: '调试',
    manual: '手动',
    scenario: '场景',
    intent: '意图',
    demo: '演示',
    regression: '回归',
  }
  return map[value] || value || '手动'
}
