import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../shared/api'
import { href, navigate, useRoute, withQuery } from '../shared/router'
import { errorText, formatDuration, formatTime, relativeTime, runWhen, shortId, statusLabel, statusOf, classForStatus } from '../shared/utils'
import type { Job, RunSummary } from '../shared/types'
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
  SearchInput,
  SectionHeader,
  Select,
  StatusBadge,
  useToast,
} from '../components/ui'

export function ExecutionsPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const [tab, setTab] = useState<'live' | 'runs'>('live')
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [triggerOpen, setTriggerOpen] = useState(false)
  const [taskId, setTaskId] = useState('')
  const jobsQuery = useQuery({ queryKey: ['jobs'], queryFn: api.jobs, refetchInterval: 3000 })
  const runsQuery = useQuery({ queryKey: ['runs', 50], queryFn: () => api.runs(50) })
  const tasksQuery = useQuery({ queryKey: ['tasks'], queryFn: api.tasks })
  const jobs = jobsQuery.data || []
  const runs = runsQuery.data || []
  const tasks = tasksQuery.data || []
  const activeJobId = route.query.get('job')

  const currentJobQuery = useQuery({
    queryKey: ['job', activeJobId],
    queryFn: () => api.job(activeJobId!),
    enabled: !!activeJobId,
    refetchInterval: (query) => {
      const data = query.state.data as Job | undefined
      return data && ['done', 'failed'].includes(data.status) ? false : 1500
    },
  })

  const trigger = useMutation({
    mutationFn: () => api.runTask(taskId, {}),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      setTriggerOpen(false)
      navigate(withQuery('/executions', { job: result.job_id }), { replace: true })
      push('执行已提交', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const filteredJobs = useMemo(() => {
    const query = search.trim().toLowerCase()
    return jobs.filter((job) => {
      if (statusFilter !== 'all' && job.status !== statusFilter) return false
      if (query && !`${job.job_id} ${job.mode} ${job.run_id || ''}`.toLowerCase().includes(query)) return false
      return true
    })
  }, [jobs, search, statusFilter])

  const filteredRuns = useMemo(() => {
    const query = search.trim().toLowerCase()
    return runs.filter((run) => {
      const status = statusOf(run)
      if (statusFilter !== 'all' && status !== statusFilter) return false
      if (query && !`${run.run_id} ${run.meta?.suite || ''} ${run.project_id || ''}`.toLowerCase().includes(query)) return false
      return true
    })
  }, [runs, search, statusFilter])

  const runningCount = jobs.filter((job) => ['queued', 'running'].includes(job.status)).length

  return (
    <div className="page executions-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 活</>}
        title="执行"
        description="运行中与已结束的执行。调试执行单独隔离，不进入正式历史；已发生的执行不可改写。"
        actions={<><Button variant="ghost" icon="history" onClick={() => navigate('/history')}>历史账本</Button><Button variant="primary" icon="play" onClick={() => setTriggerOpen(true)}>发起执行</Button></>}
      />

      <div className="execution-stats">
        <div className="execution-stat live"><span><Icon name="play" size={17} /> 进行中</span><strong>{runningCount}</strong><small>内存态 job，自动刷新</small></div>
        <div><span>最近 Job</span><strong>{jobs.length}</strong><small>服务重启后清空</small></div>
        <div><span>落盘执行</span><strong>{runs.length}</strong><small>runs/ 目录，可回溯</small></div>
        <div><span>失败执行</span><strong className={runs.some((run) => ['failed', 'errored'].includes(statusOf(run))) ? 'bad' : 'ok'}>{runs.filter((run) => ['failed', 'errored'].includes(statusOf(run))).length}</strong><small>最近 50 条</small></div>
      </div>

      <div className="segmented large">
        <button type="button" className={tab === 'live' ? 'is-active' : ''} onClick={() => setTab('live')}><Icon name="terminal" size={15} /> 实时 Job <Badge tone={runningCount ? 'run' : 'neutral'}>{jobs.length}</Badge></button>
        <button type="button" className={tab === 'runs' ? 'is-active' : ''} onClick={() => setTab('runs')}><Icon name="database" size={15} /> 落盘执行 <Badge tone="neutral">{runs.length}</Badge></button>
      </div>

      <div className="catalog-toolbar panel">
        <SearchInput value={search} onChange={setSearch} placeholder={tab === 'live' ? '搜索 job / 模式 / run' : '搜索 run / 套件 / 项目'} />
        <Select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="按状态筛选">
          <option value="all">全部状态</option>
          {tab === 'live' ? <><option value="queued">排队中</option><option value="running">运行中</option><option value="done">已完成</option><option value="failed">失败</option></> : <><option value="passed">通过</option><option value="failed">失败</option><option value="errored">异常</option></>}
        </Select>
        <span className="toolbar-spacer" />
        <span className="toolbar-count">{tab === 'live' ? filteredJobs.length : filteredRuns.length} 条</span>
      </div>

      {tab === 'live' ? (
        jobsQuery.isLoading ? <Loading label="正在读取实时 job…" /> : jobsQuery.error ? <ErrorState error={jobsQuery.error} onRetry={() => jobsQuery.refetch()} /> : !filteredJobs.length ? (
          <EmptyState icon="play" title="没有进行中的 Job">提交任务或项目批次后，实时状态会显示在这里；历史结果在「落盘执行」与「历史」中。</EmptyState>
        ) : (
          <div className="job-list">
            {filteredJobs.map((job) => <JobRow key={job.job_id} job={job} onOpen={() => setSelectedJob(job)} />)}
          </div>
        )
      ) : runsQuery.isLoading ? <Loading label="正在读取执行…" /> : runsQuery.error ? <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} /> : !filteredRuns.length ? (
        <EmptyState icon="history" title="还没有执行记录" />
      ) : (
        <div className="execution-run-grid">
          {filteredRuns.map((run) => <ExecutionCard key={run.run_id} run={run} />)}
        </div>
      )}

      <section className="panel execution-policy">
        <SectionHeader title="执行边界" description="产品上的审计约束，而不是前端禁用按钮。" icon="shield" />
        <div className="policy-grid">
          <div><Icon name="bug" size={18} /><strong>调试不入历史</strong><p>工作台单步调试写 studio/debug/ 独立目录，不触发任务状态，不产生报告。</p></div>
          <div><Icon name="clock" size={18} /><strong>已发生不可改</strong><p>执行记录只允许重跑生成新执行，parent_execution_id 建立关联。</p></div>
          <div><Icon name="warning" size={18} /><strong>失败可归因</strong><p>失败与异常分开统计；基础设施不可达不会被伪装成测试失败。</p></div>
        </div>
      </section>

      <Drawer
        open={!!selectedJob || !!currentJobQuery.data}
        onClose={() => { setSelectedJob(null); if (activeJobId) navigate('/executions', { replace: true }) }}
        title={selectedJob?.job_id || currentJobQuery.data?.job_id || '执行详情'}
        subtitle={selectedJob?.mode || currentJobQuery.data?.mode ? `模式 ${selectedJob?.mode || currentJobQuery.data?.mode}` : 'runtime job'}
        width="drawer-md"
        footer={<Button variant="ghost" onClick={() => { setSelectedJob(null); if (activeJobId) navigate('/executions', { replace: true }) }}>关闭</Button>}
      >
        <JobDetail job={selectedJob || currentJobQuery.data || null} loading={currentJobQuery.isLoading} />
      </Drawer>

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

function JobRow({ job, onOpen }: { job: Job; onOpen: () => void }) {
  const done = ['done', 'failed'].includes(job.status)
  return (
    <button type="button" className="job-row" onClick={onOpen}>
      <span className={`job-state tone-${classForStatus(job.status)}`}><span className="status-dot" />{statusLabel(job.status)}</span>
      <span className="job-row-main"><strong className="mono">{job.job_id}</strong><small>{job.mode || 'run'} · {job.phases?.length || 0} 个阶段</small></span>
      <span className="job-row-result">{job.summary?.total ? `${job.summary.passed || 0}/${job.summary.total}` : '—'}</span>
      <span className="job-row-time mono">{formatDuration(job.elapsed_ms)}</span>
      <span className="job-row-when">{relativeTime(job.started_at || job.created_at)}</span>
      {done && job.run_id ? <span className="job-run-link">run {shortId(job.run_id)}</span> : <Icon name="chevron" size={16} />}
    </button>
  )
}

function JobDetail({ job, loading }: { job: Job | null; loading: boolean }) {
  if (loading) return <Loading label="正在读取 job…" />
  if (!job) return <EmptyState icon="terminal" title="没有选择执行">从列表选择一条 job。</EmptyState>
  return (
    <div className="stack-lg">
      <div className="detail-status-row"><StatusBadge status={job.status} /><Badge tone="neutral">{job.mode || 'run'}</Badge>{job.run_id ? <Badge tone="cyan" className="mono">run {shortId(job.run_id)}</Badge> : null}</div>
      <div className="job-summary">
        <div><span>创建</span><strong>{formatTime(job.created_at)}</strong></div>
        <div><span>耗时</span><strong>{formatDuration(job.elapsed_ms)}</strong></div>
        <div><span>用例</span><strong>{job.summary?.total ? `${job.summary.passed || 0}/${job.summary.total}` : '—'}</strong></div>
        <div><span>结论</span><strong>{job.conclusion?.verdict || statusLabel(job.status)}</strong></div>
      </div>
      {job.error ? <div className="step-error"><Icon name="warning" size={14} />{errorText(job.error)}</div> : null}
      <div>
        <h3 className="subsection-title">执行阶段</h3>
        <div className="timeline">
          {(job.phases || []).map((phase, index) => (
            <div className="timeline-row" key={`${phase.name}-${index}`}>
              <span className={`timeline-dot tone-${classForStatus(phase.status)}`} />
              <div><strong>{phase.name}</strong><span>{phase.detail || statusLabel(phase.status)}</span></div>
            </div>
          ))}
          {!job.phases?.length ? <p className="muted">阶段尚未写入。</p> : null}
        </div>
      </div>
      {job.status === 'done' && job.run_id ? <a className="btn btn-primary btn-sm" href={href(`/history?focus=${job.run_id}`)}>打开运行详情 <Icon name="arrow" size={14} /></a> : null}
      {job.status === 'failed' ? <div className="notice-inline"><Icon name="warning" size={15} /><div><strong>失败原因已打码</strong><p>详细堆栈见服务端日志；不要在公网环境暴露调试信息。</p></div></div> : null}
    </div>
  )
}

function ExecutionCard({ run }: { run: RunSummary }) {
  const status = statusOf(run)
  return (
    <a className="execution-card" href={href(run.project_id ? `/p/${run.project_id}/runs/${run.run_id}` : `/history?focus=${run.run_id}`)}>
      <div className="execution-card-head"><StatusBadge status={status} /><span className="mono">{shortId(run.run_id, 12)}</span></div>
      <strong>{run.suite_name || run.meta?.suite || '未归类执行'}</strong>
      <p className="mono">{run.project_id || 'unclassified'} · {formatDuration(run.duration_ms)}</p>
      <div className="execution-card-foot"><span>{run.summary?.total ? `${run.summary.passed || 0}/${run.summary.total} 用例` : '无摘要'}</span><span>{relativeTime(runWhen(run))}</span></div>
    </a>
  )
}
