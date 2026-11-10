import { Fragment, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../shared/api'
import { href, navigate, useRoute, withQuery } from '../shared/router'
import { errorText, formatDuration, formatTime, relativeTime, shortId, statusLabel, classForStatus, toMs } from '../shared/utils'
import type { Batch, Execution } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  Drawer,
  EmptyState,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  SectionHeader,
  Select,
  StatusBadge,
  useToast,
} from '../components/ui'

const pageSize = 50

export function ExecutionsPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const projectFilter = route.query.get('project') || 'all'
  const statusFilter = route.query.get('status') || 'all'
  const page = Math.max(1, Number(route.query.get('page') || 1))
  const offset = (page - 1) * pageSize
  const selectedId = route.query.get('eid') || route.query.get('job') || route.query.get('run') || ''
  const [cancelTarget, setCancelTarget] = useState<Execution | null>(null)
  const [cancelError, setCancelError] = useState('')
  const [triggerOpen, setTriggerOpen] = useState(false)
  const [taskId, setTaskId] = useState('')
  const [watchedBatches, setWatchedBatches] = useState<string[]>([])
  const [cancelBatchTarget, setCancelBatchTarget] = useState('')
  const [cancelBatchError, setCancelBatchError] = useState('')
  const [cancelRequestedAt, setCancelRequestedAt] = useState<Record<string, number>>({})
  const [now, setNow] = useState(Date.now())

  const params = useMemo(() => {
    const next = new URLSearchParams({ offset: String(offset), limit: String(pageSize), status: statusFilter })
    if (projectFilter !== 'all') next.set('project', projectFilter)
    return next
  }, [offset, projectFilter, statusFilter])

  const executionsQuery = useQuery({
    queryKey: ['executions', projectFilter, statusFilter, offset],
    queryFn: () => api.executions(params),
    refetchInterval: (query) => {
      const data = query.state.data
      return data?.executions?.some((item) => ['queued', 'running', 'cancelling'].includes(item.status)) ? 1800 : false
    },
  })
  const projectsQuery = useQuery({ queryKey: ['projects', 'with-archived'], queryFn: () => api.projects(true) })
  const tasksQuery = useQuery({ queryKey: ['tasks'], queryFn: () => api.tasks() })
  const detailQuery = useQuery({
    queryKey: ['execution', selectedId],
    queryFn: () => api.execution(selectedId),
    enabled: !!selectedId,
    refetchInterval: (query) => {
      const data = query.state.data as Execution | undefined
      return data && ['queued', 'running', 'cancelling'].includes(data.status) ? 1500 : false
    },
  })
  const currentBatchIds = useMemo(() => Array.from(new Set(executionsQuery.data?.executions?.map((item) => item.batch_id).filter((id): id is string => !!id) || [])).sort(), [executionsQuery.data])
  const allBatchIds = useMemo(() => Array.from(new Set([...currentBatchIds, ...watchedBatches])).sort(), [currentBatchIds, watchedBatches])
  const batchQuery = useQuery({
    queryKey: ['execution-batches', allBatchIds.join(',')],
    queryFn: async () => {
      const rows = await Promise.all(allBatchIds.map((batchId) => api.batch(batchId).catch(() => null)))
      return rows.filter((batch): batch is Batch => !!batch)
    },
    enabled: allBatchIds.length > 0,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 1000
      return data.some((batch) => ['queued', 'running', 'cancelling'].includes(batch.status)) ? 1200 : false
    },
  })
  const batchById = useMemo(() => Object.fromEntries((batchQuery.data || []).map((batch) => [batch.batch_id, batch])), [batchQuery.data])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!watchedBatches.length || !batchQuery.data) return
    const watched = watchedBatches.map((batchId) => batchById[batchId]).filter((batch): batch is Batch => !!batch)
    if (watched.length !== watchedBatches.length) return
    if (!watched.every((batch) => ['done', 'failed', 'cancelled'].includes(batch.status))) return
    const summary = watched.map((batch) => `${batch.batch_id}:${batch.status}`).join(' · ')
    setWatchedBatches([])
    queryClient.invalidateQueries({ queryKey: ['executions'] })
    push(`批次已到终态（${summary}）。已完成 run 保留，未开始套件已标记 cancelled。`, 'ok')
  }, [batchQuery.data, batchById, watchedBatches, queryClient, push])

  const executions = executionsQuery.data?.executions || []
  const total = executionsQuery.data?.total || 0
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const projects = projectsQuery.data || []
  const tasks = tasksQuery.data || []
  const runningCount = executions.filter((item) => ['queued', 'running', 'cancelling'].includes(item.status)).length
  const failedCount = executions.filter((item) => ['failed', 'error', 'errored'].includes(item.status)).length
  const cancellableCount = executions.filter((item) => item.cancellable).length

  const updateQuery = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(route.query)
    Object.entries(patch).forEach(([key, value]) => {
      if (!value || value === 'all') next.delete(key)
      else next.set(key, value)
    })
    next.delete('page')
    navigate(withQuery('/executions', next), { replace: true })
  }

  const cancel = useMutation({
    mutationFn: (execution: Execution) => api.cancelExecution(execution.eid || execution.run_id || execution.job_id, '从执行中心取消'),
    onSuccess: (result, execution) => {
      setCancelTarget(null)
      setCancelError('')
      queryClient.invalidateQueries({ queryKey: ['executions'] })
      queryClient.invalidateQueries({ queryKey: ['execution', execution.eid] })
      queryClient.invalidateQueries({ queryKey: ['execution', execution.run_id] })
      if (result.batch_id) {
        const batchId = result.batch_id
        setWatchedBatches((current) => current.includes(batchId) ? current : [...current, batchId])
        setCancelRequestedAt((current) => ({ ...current, [batchId]: Date.now() }))
      }
      const suiteText = result.cancelled_suites?.length ? `；未开始套件 ${result.cancelled_suites.length} 个已标记 cancelled` : ''
      push(`取消请求已提交（${statusLabel(result.status)}）。当前步骤结束后停止；已完成 run 保留${suiteText}。正在等待批处理终态…`, 'ok')
      if (statusFilter !== 'all') {
        const next = new URLSearchParams(route.query)
        next.delete('status')
        next.delete('page')
        navigate(withQuery('/executions', next), { replace: true })
      }
    },
    onError: (error: Error) => {
      setCancelError(error.message)
      push(error.message, 'error')
    },
  })

  const cancelBatch = useMutation({
    mutationFn: (batchId: string) => api.cancelExecution(batchId, '从执行中心取消整批'),
    onSuccess: (result) => {
      const batchId = result.batch_id || result.eid
      setCancelBatchTarget('')
      setCancelBatchError('')
      setWatchedBatches((current) => current.includes(batchId) ? current : [...current, batchId])
      setCancelRequestedAt((current) => ({ ...current, [batchId]: Date.now() }))
      queryClient.invalidateQueries({ queryKey: ['executions'] })
      const suiteText = result.cancelled_suites?.length ? `；未开始套件 ${result.cancelled_suites.length} 个已标记 cancelled` : ''
      push(`批次取消请求已提交（${statusLabel(result.status)}）。已完成 run 保留${suiteText}；正在等待批处理终态…`, 'ok')
      if (statusFilter !== 'all') {
        const next = new URLSearchParams(route.query)
        next.delete('status')
        next.delete('page')
        navigate(withQuery('/executions', next), { replace: true })
      }
    },
    onError: (error: Error) => {
      setCancelBatchError(error.message)
      push(error.message, 'error')
    },
  })

  const trigger = useMutation({
    mutationFn: () => api.runTask(taskId, {}),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['executions'] })
      setTriggerOpen(false)
      navigate(withQuery('/executions', { eid: result.job_id }), { replace: true })
      push('执行已提交', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const requestCancel = (execution: Execution) => {
    setCancelError('')
    setCancelTarget(execution)
  }

  const batchGroups = useMemo(() => {
    const groups = new Map<string, Execution[]>()
    const ungrouped: Execution[] = []
    executions.forEach((execution) => {
      if (!execution.batch_id) { ungrouped.push(execution); return }
      const list = groups.get(execution.batch_id) || []
      list.push(execution)
      groups.set(execution.batch_id, list)
    })
    return { groups: [...groups.entries()].map(([batchId, rows]) => ({ batchId, rows, batch: batchById[batchId] })), ungrouped }
  }, [executions, batchById])

  return (
    <div className="page executions-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 活</>}
        title="执行"
        description="磁盘历史与内存 Job 的统一执行视图。取消只停止后续步骤，已产生的报告和已完成的 run 永久保留。"
        actions={<><Button variant="ghost" icon="history" onClick={() => navigate('/history')}>历史账本</Button><Button variant="primary" icon="play" onClick={() => setTriggerOpen(true)}>发起执行</Button></>}
      />

      <div className="execution-stats">
        <div className="execution-stat live"><span><Icon name="play" size={17} /> 当前页进行中</span><strong>{runningCount}</strong><small>{cancellableCount} 个可取消</small></div>
        <div><span>执行总数</span><strong>{total}</strong><small>符合当前筛选</small></div>
        <div><span>当前页失败</span><strong className={failedCount ? 'bad' : 'ok'}>{failedCount}</strong><small>失败 / 异常</small></div>
        <div><span>取消语义</span><strong>协作式</strong><small>当前步骤结束后停止</small></div>
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
            <span>状态</span>
            <Select value={statusFilter} onChange={(event) => updateQuery({ status: event.target.value })}>
              <option value="all">全部状态</option>
              <option value="queued">排队中</option>
              <option value="running">运行中</option>
              <option value="done">已完成</option>
              <option value="passed">通过</option>
              <option value="failed">失败</option>
              <option value="errored">异常</option>
              <option value="cancelled">已取消</option>
            </Select>
          </label>
          <div className="filter-spacer" />
          <Badge tone="neutral">{total} 条</Badge>
          {(projectFilter !== 'all' || statusFilter !== 'all') ? <Button size="sm" variant="ghost" icon="close" onClick={() => navigate('/executions', { replace: true })}>清除筛选</Button> : null}
        </div>
        <p className="filter-hint"><Icon name="link" size={13} /> project / status / page 写入 URL；执行列表自动轮询进行中的任务。</p>
      </section>

      <section className="panel execution-list-panel">
        {executionsQuery.isLoading ? <Loading label="正在读取执行列表…" /> : null}
        {executionsQuery.error ? <ErrorState error={executionsQuery.error} onRetry={() => executionsQuery.refetch()} /> : null}
        {!executionsQuery.isLoading && !executionsQuery.error && !executions.length ? (
          <EmptyState icon="play" title="没有匹配的执行" actions={<Button size="sm" onClick={() => navigate('/executions', { replace: true })}>清除筛选</Button>}>
            提交任务或项目批次后，这里会合并展示内存 Job 与磁盘报告。
          </EmptyState>
        ) : null}

        {executions.length ? (
          <div className="execution-table-wrap">
            <table className="execution-table">
              <thead>
                <tr><th>状态</th><th>执行 / 触发</th><th>项目 / 套件</th><th>结果</th><th>耗时</th><th>开始时间</th><th className="execution-actions-col">操作</th></tr>
              </thead>
              <tbody>
                {batchGroups.groups.map((group) => (
                  <Fragment key={group.batchId}>
                    <BatchSummaryRow
                      batchId={group.batchId}
                      rows={group.rows}
                      batch={group.batch}
                      now={now}
                      cancelRequestedAt={cancelRequestedAt[group.batchId]}
                      onCancel={() => { setCancelBatchError(''); setCancelBatchTarget(group.batchId) }}
                    />
                    {group.rows.map((execution) => <ExecutionRow key={`${execution.eid}-${execution.job_id}`} execution={execution} onOpen={() => navigate(withQuery('/executions', { eid: execution.eid || execution.run_id || execution.job_id }), { replace: true })} onCancel={() => requestCancel(execution)} />)}
                  </Fragment>
                ))}
                {batchGroups.ungrouped.map((execution) => <ExecutionRow key={`${execution.eid}-${execution.job_id}`} execution={execution} onOpen={() => navigate(withQuery('/executions', { eid: execution.eid || execution.run_id || execution.job_id }), { replace: true })} onCancel={() => requestCancel(execution)} />)}
              </tbody>
            </table>
          </div>
        ) : null}

        {pages > 1 ? (
          <div className="pagination">
            <span>第 {page} / {pages} 页 · 共 {total} 条</span>
            <div>
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => {
                const next = new URLSearchParams(route.query)
                if (page - 1 <= 1) next.delete('page')
                else next.set('page', String(page - 1))
                navigate(withQuery('/executions', next), { replace: true })
              }}>上一页</Button>
              <Button size="sm" variant="ghost" disabled={page >= pages} onClick={() => {
                const next = new URLSearchParams(route.query)
                next.set('page', String(page + 1))
                navigate(withQuery('/executions', next), { replace: true })
              }}>下一页</Button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel execution-policy">
        <SectionHeader title="取消语义" description="协作式停止，不伪造中断。" icon="shield" />
        <div className="policy-grid">
          <div><Icon name="clock" size={18} /><strong>当前步骤结束后停止</strong><p>正在执行的原子不会被强制 kill；后续步骤记为 skipped，已产生的证据保留。</p></div>
          <div><Icon name="database" size={18} /><strong>已完成 run 保留</strong><p>batch 取消时，已经落盘的报告不动；未开始的套件标 cancelled 且不产生 run。</p></div>
          <div><Icon name="bug" size={18} /><strong>调试仍隔离</strong><p>Studio 单步调试不进入执行中心，也不产生正式报告。</p></div>
        </div>
      </section>

      <Drawer
        open={!!selectedId}
        onClose={() => {
          const next = new URLSearchParams(route.query)
          next.delete('eid'); next.delete('job'); next.delete('run')
          navigate(withQuery('/executions', next), { replace: true })
        }}
        title={detailQuery.data ? detailQuery.data.suite_id || detailQuery.data.run_id || detailQuery.data.job_id : '执行详情'}
        subtitle={detailQuery.data ? `${detailQuery.data.mode || 'run'} · ${detailQuery.data.trigger || 'manual'}` : '读取中'}
        width="drawer-md"
        footer={detailQuery.data ? (
          <>
            <Button variant="ghost" onClick={() => {
              const next = new URLSearchParams(route.query)
              next.delete('eid'); next.delete('job'); next.delete('run')
              navigate(withQuery('/executions', next), { replace: true })
            }}>关闭</Button>
            {detailQuery.data.cancellable ? <Button variant="danger" icon="close" onClick={() => requestCancel(detailQuery.data!)}>取消执行</Button> : null}
            {detailQuery.data.run_id ? <a className="btn btn-primary btn-sm" href={href(detailQuery.data.project_id ? `/p/${detailQuery.data.project_id}/runs/${detailQuery.data.run_id}` : `/runs/${detailQuery.data.run_id}`)}>打开报告 <Icon name="arrow" size={14} /></a> : null}
          </>
        ) : null}
      >
        {detailQuery.isLoading ? <Loading label="正在读取执行详情…" /> : detailQuery.error ? <ErrorState error={detailQuery.error} onRetry={() => detailQuery.refetch()} /> : detailQuery.data ? <ExecutionDetail execution={detailQuery.data} onCancel={() => requestCancel(detailQuery.data!)} /> : null}
      </Drawer>

      <Modal
        open={!!cancelTarget}
        onClose={() => { if (!cancel.isPending) { setCancelTarget(null); setCancelError('') } }}
        title="取消这次执行？"
        subtitle={cancelTarget ? `${cancelTarget.run_id || cancelTarget.job_id}${cancelTarget.batch_id ? ` · batch ${cancelTarget.batch_id}` : ''}` : ''}
        size="sm"
        dismissible={!cancel.isPending}
        footer={
          <>
            <Button variant="ghost" disabled={cancel.isPending} onClick={() => { setCancelTarget(null); setCancelError('') }}>保留运行</Button>
            <Button variant="danger" icon="close" loading={cancel.isPending} onClick={() => cancelTarget && cancel.mutate(cancelTarget)} data-testid="confirm-cancel">确认取消</Button>
          </>
        }
      >
        <div className="confirm-body">
          <p><strong>当前步骤结束后停止</strong>，剩余步骤标记为 skipped；不会强制中断正在执行的原子。</p>
          <p style={{ marginTop: 8 }}>如果这是 batch：<b>已完成的 run 保留</b>，未开始的套件标记为 <code>cancelled</code> 且不产生新 run。</p>
          <p style={{ marginTop: 8 }}>取消请求会写入审计。已结束的执行不能取消。</p>
          {cancelError ? <div className="step-error" style={{ marginTop: 10 }}><Icon name="warning" size={14} />{cancelError}</div> : null}
        </div>
      </Modal>

      <Modal
        open={!!cancelBatchTarget}
        onClose={() => { if (!cancelBatch.isPending) { setCancelBatchTarget(''); setCancelBatchError('') } }}
        title="取消整个批次？"
        subtitle={cancelBatchTarget ? `batch ${cancelBatchTarget}` : ''}
        size="sm"
        dismissible={!cancelBatch.isPending}
        footer={
          <>
            <Button variant="ghost" disabled={cancelBatch.isPending} onClick={() => { setCancelBatchTarget(''); setCancelBatchError('') }}>保留批次</Button>
            <Button variant="danger" icon="close" loading={cancelBatch.isPending} onClick={() => cancelBatch.mutate(cancelBatchTarget)} data-testid="confirm-cancel-batch">确认取消整批</Button>
          </>
        }
      >
        <div className="confirm-body">
          <p><strong>当前步骤结束后停止</strong>，正在执行的套件按协作式取消；不会强制 kill 原子。</p>
          <p style={{ marginTop: 8 }}><b>已完成 run 保留</b>；<b>未开始的套件标记 cancelled</b>，不会产生新 run。</p>
          <p style={{ marginTop: 8 }}>批次取消后会自动轮询到 batch 终态，批次行会显示最终结果。</p>
          {cancelBatchError ? <div className="step-error" style={{ marginTop: 10 }}><Icon name="warning" size={14} />{cancelBatchError}</div> : null}
        </div>
      </Modal>

      <Modal
        open={triggerOpen}
        onClose={() => setTriggerOpen(false)}
        title="发起执行"
        subtitle="从已定义任务触发；调试请在画布内进行。"
        size="sm"
        footer={<><Button variant="ghost" onClick={() => setTriggerOpen(false)}>取消</Button><Button variant="primary" icon="play" disabled={!taskId} loading={trigger.isPending} onClick={() => trigger.mutate()}>运行任务</Button></>}
      >
        <label className="filter-field">
          <span>选择任务</span>
          <Select value={taskId} onChange={(event) => setTaskId(event.target.value)}>
            <option value="">选择一个任务</option>
            {tasks.map((task) => <option key={task.task_id} value={task.task_id}>{task.name} · {task.task_id}</option>)}
          </Select>
        </label>
        <p className="muted">没有任务？先到 <a className="inline-link" href={href('/tasks')}>任务页</a> 新建，或从工作台发布。</p>
      </Modal>
    </div>
  )
}

function BatchSummaryRow({ batchId, rows, batch, now, cancelRequestedAt, onCancel }: { batchId: string; rows: Execution[]; batch?: Batch; now: number; cancelRequestedAt?: number; onCancel: () => void }) {
  const active = batch ? ['queued', 'running', 'cancelling'].includes(batch.status) : rows.some((row) => ['queued', 'running', 'cancelling'].includes(row.status))
  const suiteCount = batch?.suites.length ?? rows.length
  const passed = batch?.aggregate?.passed ?? rows.reduce((sum, row) => sum + (row.passed || 0), 0)
  const total = batch?.aggregate?.total ?? rows.reduce((sum, row) => sum + (row.total || 0), 0)
  const currentSuite = batch?.suites.find((suite) => suite.status === 'running') || batch?.suites.find((suite) => suite.status === 'queued')
  const currentExecution = rows.find((row) => row.status === 'running' && (!currentSuite || row.suite_id === currentSuite.suite_id)) || rows.find((row) => row.status === 'running')
  const phase = currentExecution?.status === 'running' ? { name: 'execute' } : currentExecution?.phases?.slice(-1)[0]
  const firstStarted = rows.map((row) => toMs(row.started_at || row.created_at)).filter((value) => value > 0).sort((a, b) => a - b)[0]
  const waitFrom = cancelRequestedAt || firstStarted || now
  const waited = Math.max(0, Math.floor((now - waitFrom) / 1000))
  const status = batch?.status || (rows.some((row) => ['queued', 'running', 'cancelling'].includes(row.status)) ? 'running' : rows.every((row) => row.status === 'cancelled') ? 'cancelled' : 'done')
  const cancelledCount = batch?.suites.filter((suite) => suite.status === 'cancelled').length ?? rows.filter((row) => row.status === 'cancelled').length
  const doneCount = batch?.suites.filter((suite) => suite.status === 'done').length ?? rows.filter((row) => row.status === 'done').length
  const failedCount = batch?.suites.filter((suite) => suite.status === 'failed').length ?? rows.filter((row) => row.status === 'failed').length
  return (
    <tr className={`batch-summary-row ${active ? 'is-active' : 'is-terminal'}`} data-testid={`batch-row-${batchId}`} data-batch-id={batchId}>
      <td colSpan={7}>
        <div className="batch-summary-cell">
          <div className="batch-summary-main">
            <span className="batch-icon"><Icon name="layers" size={15} /></span>
            <div>
              <div className="batch-title-line"><strong className="mono">batch {batchId}</strong><StatusBadge status={status} /><Badge tone="neutral">套件 {suiteCount}</Badge><Badge tone="neutral">用例 {passed}/{total}</Badge></div>
              <span className="batch-current">{active ? (
                currentSuite ? <>当前：<b>{currentSuite.name}</b>{phase ? <> · 阶段 <b>{phase.name}</b></> : null} · 等待当前步骤结束后停止{currentExecution?.error ? ` · ${errorText(currentExecution.error)}` : ''}</> : '等待批次启动…'
              ) : (
                <>
                  {status === 'cancelled' ? `已取消：完成 ${doneCount} / 已取消 ${cancelledCount}${failedCount ? ` / 失败 ${failedCount}` : ''}` : status === 'failed' ? `批次失败：完成 ${doneCount} / 失败 ${failedCount}` : `批次完成：通过 ${doneCount} / 共 ${suiteCount} 个套件`}
                  {' · '}已完成的 run 保留
                </>
              )}</span>
            </div>
          </div>
          <div className="batch-wait">{active ? <><span className="batch-pulse" />{cancelRequestedAt ? '已等待' : '已运行'} {waited}s</> : <span className="batch-final">{status === 'cancelled' ? 'cancelled' : status === 'failed' ? 'failed' : 'done'}</span>}</div>
          {active && !cancelRequestedAt ? <Button size="sm" variant="danger" icon="close" onClick={onCancel} data-testid={`cancel-batch-${batchId}`}>取消批次</Button> : active ? <Badge tone="warn">取消中 · 等待步骤边界</Badge> : null}
        </div>
      </td>
    </tr>
  )
}

function ExecutionRow({ execution, onOpen, onCancel }: { execution: Execution; onOpen: () => void; onCancel: () => void }) {
  const status = execution.status || execution.run_status || 'done'
  const suiteName = execution.suite_id || execution.mode || '未归类执行'
  return (
    <tr data-testid={`execution-row-${execution.eid || execution.run_id || execution.job_id}`} data-batch-id={execution.batch_id || undefined}>
      <td data-label="状态"><StatusBadge status={status} />{execution.cancellable ? <span className="cancellable-mark">可取消</span> : null}</td>
      <td data-label="执行 / 触发">
        <button type="button" className="execution-link" onClick={onOpen}>
          <strong className="mono">{shortId(execution.run_id || execution.job_id, 12)}</strong>
          <span>{execution.trigger || 'manual'}{execution.batch_id ? ` · batch ${shortId(execution.batch_id, 8)}` : ''}</span>
        </button>
      </td>
      <td data-label="项目 / 套件">
        <div className="run-cell-main"><strong>{suiteName}</strong><span>{execution.project_id || '未归类'}</span></div>
        {execution.workflow_ref ? <Badge tone="ai" className="workflow-mini"><Icon name="git" size={10} />{execution.workflow_ref}</Badge> : null}
      </td>
      <td data-label="结果"><span className="mono">{execution.total ? `${execution.passed}/${execution.total}` : '—'}</span></td>
      <td data-label="耗时"><span className="mono nowrap">{formatDuration(execution.duration_ms)}</span></td>
      <td data-label="开始时间"><span className="nowrap muted">{relativeTime(execution.started_at || execution.created_at)}</span></td>
      <td className="execution-actions-col">
        <div className="row-actions">
          {execution.run_id ? <a className="btn btn-ghost btn-sm" data-testid="execution-artifacts-link" data-run-id={execution.run_id} href={href(withQuery('/artifacts', { run: execution.run_id }))}><Icon name="archive" size={13} />产出物</a> : null}
          {execution.run_id ? <a className="btn btn-ghost btn-sm" href={href(execution.project_id ? `/p/${execution.project_id}/runs/${execution.run_id}` : `/runs/${execution.run_id}`)}>报告</a> : null}
          <Button size="sm" variant="ghost" onClick={onOpen}>详情</Button>
          {execution.cancellable ? <Button size="sm" variant="danger" icon="close" onClick={onCancel} data-testid={`cancel-execution-${execution.eid || execution.run_id || execution.job_id}`}>取消</Button> : null}
        </div>
      </td>
    </tr>
  )
}

function ExecutionDetail({ execution, onCancel }: { execution: Execution; onCancel: () => void }) {
  return (
    <div className="stack-lg" data-testid="execution-detail">
      <div className="detail-status-row">
        <StatusBadge status={execution.status || 'done'} />
        <Badge tone="neutral">{execution.mode || 'run'}</Badge>
        <Badge tone="neutral">{execution.trigger || 'manual'}</Badge>
        {execution.cancellable ? <Badge tone="warn">可取消</Badge> : null}
      </div>
      <div className="job-summary">
        <div><span>结果</span><strong>{execution.total ? `${execution.passed}/${execution.total}` : '—'}</strong></div>
        <div><span>耗时</span><strong>{formatDuration(execution.duration_ms)}</strong></div>
        <div><span>开始</span><strong>{formatTime(execution.started_at)}</strong></div>
        <div><span>结束</span><strong>{formatTime(execution.finished_at)}</strong></div>
      </div>
      {execution.error ? <div className="step-error"><Icon name="warning" size={14} />{errorText(execution.error)}</div> : null}
      <dl className="fact-list two-col">
        <div><dt>EID</dt><dd className="mono">{execution.eid}</dd></div>
        <div><dt>Run ID</dt><dd className="mono">{execution.run_id || '—'}</dd></div>
        <div><dt>Job ID</dt><dd className="mono">{execution.job_id || '—'}</dd></div>
        <div><dt>Batch</dt><dd className="mono">{execution.batch_id || '—'}</dd></div>
        <div><dt>项目</dt><dd>{execution.project_id || '未归类'}</dd></div>
        <div><dt>套件</dt><dd>{execution.suite_id || '—'}</dd></div>
        <div><dt>发布版本</dt><dd className="mono">{execution.workflow_ref || '—'}</dd></div>
        <div><dt>结论</dt><dd>{execution.conclusion?.verdict || statusLabel(execution.status)}</dd></div>
      </dl>
      <div>
        <h3 className="subsection-title">执行阶段</h3>
        <div className="timeline">
          {(execution.phases || []).map((phase, index) => (
            <div className="timeline-row" key={`${phase.name}-${index}`}>
              <span className={`timeline-dot tone-${classForStatus(phase.status)}`} />
              <div>
                <strong>{phase.name}</strong>
                {phase.status === 'warning' ? <Badge tone="warn" className="phase-warn-badge"><Icon name="warning" size={10} />警告</Badge> : null}
                <span>{phase.detail || statusLabel(phase.status)}</span>
              </div>
            </div>
          ))}
          {!execution.phases?.length ? <p className="muted">没有阶段详情（已完成的历史 run 可能未持久化阶段）。</p> : null}
        </div>
      </div>
      {execution.cancellable ? (
        <div className="detail-danger">
          <div><strong>取消执行</strong><span>当前步骤结束后停止；已完成的 run 与报告保留。</span></div>
          <Button size="sm" variant="danger" icon="close" onClick={onCancel} data-testid={`cancel-execution-${execution.eid || execution.run_id || execution.job_id}-detail`}>取消</Button>
        </div>
      ) : null}
      {execution.run_id ? <a className="btn btn-primary btn-sm" href={href(execution.project_id ? `/p/${execution.project_id}/runs/${execution.run_id}` : `/runs/${execution.run_id}`)}>打开运行报告 <Icon name="arrow" size={14} /></a> : null}
    </div>
  )
}
