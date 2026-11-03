import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, studioApi } from '../shared/api'
import { href, navigate, useRoute, withQuery } from '../shared/router'
import { errorText, formatDuration, formatTime, relativeTime, shortId, statusLabel, statusOf, classForStatus } from '../shared/utils'
import type { AnyRecord, Job, Scenario, Task, Workflow } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  EnvEditor,
  ErrorState,
  Field,
  Loading,
  Modal,
  PageHeader,
  SearchInput,
  SectionHeader,
  Select,
  StatusBadge,
  useToast,
} from '../components/ui'

const modeMeta: Record<string, { label: string; tone: 'cyan' | 'violet' | 'neutral' | 'warn'; hint: string }> = {
  scenario: { label: '场景', tone: 'cyan', hint: '固定 YAML 场景' },
  intent: { label: '意图', tone: 'violet', hint: '自然语言规划' },
  demo: { label: '演示', tone: 'neutral', hint: '内置演示闭环' },
  regression: { label: '回归', tone: 'warn', hint: '复用沉淀库' },
  workflow: { label: '流程', tone: 'violet', hint: '发布版本 wid@rev' },
}

export function TasksPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const includeArchived = route.query.get('include_archived') === '1'
  const tasksQuery = useQuery({ queryKey: ['tasks', includeArchived], queryFn: () => api.tasks(includeArchived) })
  const scenariosQuery = useQuery({ queryKey: ['scenarios'], queryFn: api.scenarios, staleTime: 60_000 })
  const draftsQuery = useQuery({ queryKey: ['studio-drafts'], queryFn: studioApi.listDrafts, staleTime: 15_000 })
  const [search, setSearch] = useState('')
  const [modeFilter, setModeFilter] = useState('all')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<Task | null>(null)
  const [detail, setDetail] = useState<Task | null>(null)
  const [archiveTarget, setArchiveTarget] = useState<Task | null>(null)
  const [archiveNext, setArchiveNext] = useState(true)
  const [activeJob, setActiveJob] = useState<string>(route.query.get('job') || '')
  const [publishedHandled, setPublishedHandled] = useState(false)
  const [form, setForm] = useState({ name: '', mode: 'scenario', scenario: '', intent: '', workflow_ref: '', regression: false, no_llm: false })
  const [env, setEnv] = useState<AnyRecord>({})

  const tasks = tasksQuery.data || []
  const archivedCount = tasks.filter((task) => !!task.archived_at).length
  const scenarios = scenariosQuery.data || []
  const publishedWorkflows = (draftsQuery.data || []).filter((workflow) => !!workflow.published_ref)
  const published = route.query.get('published') || ''
  const focus = route.query.get('focus')
  const openedFocusRef = useRef('')
  const focusRetriedRef = useRef('')

  useEffect(() => {
    if (!focus) return
    if (openedFocusRef.current === focus) return
    if (tasksQuery.isLoading || tasksQuery.isFetching) return
    const target = tasks.find((task) => task.task_id === focus)
    if (target) {
      openedFocusRef.current = focus
      setDetail(target)
      return
    }
    // 发布后后端可能刚写入任务；首次未命中时补拉一次，避免缓存里没有新任务。
    if (focusRetriedRef.current !== focus) {
      focusRetriedRef.current = focus
      void tasksQuery.refetch()
    }
  }, [focus, tasks, tasksQuery.isLoading, tasksQuery.isFetching, tasksQuery.refetch])

  useEffect(() => {
    setActiveJob(route.query.get('job') || '')
  }, [route.query])

  const jobQuery = useQuery({
    queryKey: ['job', activeJob],
    queryFn: () => api.job(activeJob),
    enabled: !!activeJob,
    refetchInterval: (query) => {
      const data = query.state.data as Job | undefined
      return data && ['done', 'failed'].includes(data.status) ? false : 1500
    },
  })

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    return tasks.filter((task) => {
      if (modeFilter !== 'all' && task.mode !== modeFilter) return false
      if (query && !`${task.name} ${task.task_id} ${task.scenario || ''} ${task.intent || ''}`.toLowerCase().includes(query)) return false
      return true
    })
  }, [modeFilter, search, tasks])

  const openCreate = () => {
    setEditing(null)
    setForm({ name: '', mode: 'scenario', scenario: scenarios[0]?.id || '', intent: '', workflow_ref: '', regression: false, no_llm: false })
    setEnv({})
    setEditorOpen(true)
  }

  const openEdit = (task: Task) => {
    setEditing(task)
    setForm({
      name: task.name,
      mode: task.mode,
      scenario: task.scenario || '',
      intent: task.intent || '',
      workflow_ref: task.workflow_ref || '',
      regression: !!task.regression,
      no_llm: !!task.no_llm,
    })
    setEnv(task.env || {})
    setEditorOpen(true)
  }

  const save = useMutation({
    mutationFn: async () => {
      if (!form.name.trim()) throw new Error('请填写任务名称')
      if (form.mode === 'scenario' && !form.scenario) throw new Error('请选择场景文件')
      if (form.mode === 'intent' && !form.intent.trim()) throw new Error('请填写意图')
      if (form.mode === 'workflow' && !form.workflow_ref.trim()) throw new Error('请选择已发布流程')
      const body: AnyRecord = {
        name: form.name.trim(),
        mode: form.mode,
        regression: form.regression,
        no_llm: form.no_llm,
        env,
      }
      if (form.mode === 'scenario') body.scenario = form.scenario
      if (form.mode === 'intent') body.intent = form.intent.trim()
      if (form.mode === 'workflow') body.workflow_ref = form.workflow_ref.trim()
      return editing ? api.updateTask(editing.task_id, body) : api.createTask(body)
    },
    onSuccess: (task) => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      setEditorOpen(false)
      push(editing ? '任务已更新' : '任务已创建', 'ok')
      if (!editing) setDetail(task)
      if (route.query.get('published')) navigate('/tasks', { replace: true })
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const archiveMutation = useMutation({
    mutationFn: ({ task, archived }: { task: Task; archived: boolean }) => api.archiveTask(task.task_id, archived),
    onSuccess: (next, variables) => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      setArchiveTarget(null)
      setDetail((current) => current?.task_id === variables.task.task_id ? { ...current, ...next } : current)
      push(variables.archived ? '任务已归档；配置保留，历史运行与报告不变' : '任务已取消归档', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const runTask = useMutation({
    mutationFn: (taskId: string) => api.runTask(taskId, {}),
    onSuccess: (result) => {
      setActiveJob(result.job_id)
      setDetail(null)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      push('任务已提交运行', 'ok')
      navigate(withQuery('/tasks', { job: result.job_id }), { replace: true })
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  useEffect(() => {
    if (!published || publishedHandled || !publishedWorkflows.length) return
    const target = publishedWorkflows.find((workflow) => workflow.published_ref === published)
    setEditing(null)
    setForm({ name: target?.name ? `${target.name} 任务` : '发布流程任务', mode: 'workflow', scenario: '', intent: '', workflow_ref: published, regression: false, no_llm: false })
    setEnv({})
    setEditorOpen(true)
    setPublishedHandled(true)
  }, [published, publishedWorkflows.length, publishedHandled])

  return (
    <div className="page tasks-page">
      {published ? <div className="publish-success"><span className="publish-success-icon"><Icon name="git" size={20} /></span><div><strong>已发布流程 {published}</strong><p>创建一个任务绑定这个不可变版本，或到项目设置把它绑定为套件。</p></div><Button size="sm" variant="ghost" onClick={() => openCreate()}>换个方式新建</Button></div> : null}
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 活</>}
        title="任务"
        description="任务 = 已发布流程或可复用的运行入口。图只能在工坊出新版本，已发布任务不可就地改图。"
        actions={<><Button variant="ghost" icon="spark" onClick={() => navigate('/studio/new')}>从工作台发布</Button><Button variant="primary" icon="plus" onClick={openCreate}>新建任务</Button></>}
      />

      <div className="task-stats">
        <div><span>任务总数</span><strong>{tasks.length}</strong><small>{tasks.filter((task) => task.workflow_ref).length} 个来自发布流程</small></div>
        <div><span>最近运行失败</span><strong className={tasks.some((task) => task.last_status === 'failed') ? 'bad' : 'ok'}>{tasks.filter((task) => task.last_status === 'failed').length}</strong><small>失败定义不等于报告失败</small></div>
        <div><span>固定场景</span><strong>{tasks.filter((task) => task.mode === 'scenario').length}</strong><small>可绑定项目 / 套件</small></div>
        <div><span>流程任务</span><strong>{tasks.filter((task) => task.mode === 'workflow').length}</strong><small>绑定 wid@rev</small></div>
        {includeArchived ? <div><span>已归档</span><strong>{archivedCount}</strong><small>仅显示，不参与运行</small></div> : null}
      </div>

      <div className="catalog-toolbar panel">
        <SearchInput value={search} onChange={setSearch} placeholder="搜索任务名称、场景或意图" />
        <Select value={modeFilter} onChange={(event) => setModeFilter(event.target.value)} aria-label="按模式筛选">
          <option value="all">全部模式</option>
          <option value="scenario">场景</option>
          <option value="intent">意图</option>
          <option value="demo">演示</option>
          <option value="regression">回归</option>
        </Select>
        <span className="toolbar-spacer" />
        <button type="button" className={`archive-toggle ${includeArchived ? 'is-on' : ''}`} onClick={() => {
          const next = new URLSearchParams(route.query)
          if (includeArchived) next.delete('include_archived')
          else next.set('include_archived', '1')
          navigate(withQuery('/tasks', next), { replace: true })
        }} aria-pressed={includeArchived} data-testid="toggle-archived">
          <span className="toggle-track"><span /></span>
          显示已归档
        </button>
        <span className="toolbar-count">{filtered.length} / {tasks.length}</span>
      </div>

      {tasksQuery.isLoading ? <Loading label="正在读取任务…" /> : null}
      {tasksQuery.error ? <ErrorState error={tasksQuery.error} onRetry={() => tasksQuery.refetch()} /> : null}

      {!tasksQuery.isLoading && !filtered.length ? (
        <EmptyState icon="tasks" title={tasks.length ? '没有匹配的任务' : '还没有任务'} actions={<Button size="sm" variant="primary" icon="plus" onClick={openCreate}>新建任务</Button>}>
          {tasks.length ? '换个关键词或模式筛选。' : '从工作台发布流程，或创建固定场景任务。'}
        </EmptyState>
      ) : null}

      {filtered.length ? (
        <div className="task-list">
          {filtered.map((task) => {
            const meta = modeMeta[task.mode] || { label: task.mode, tone: 'neutral' as const, hint: '' }
            const lastStatus = task.last_status ? statusOf({ run_id: task.last_run_id || '', summary: { total: 1, failed: task.last_status === 'failed' ? 1 : 0, passed: task.last_status === 'passed' ? 1 : 0 } }) : 'none'
            return (
              <article className={`task-card ${task.archived_at ? 'is-archived' : ''}`} key={task.task_id} data-testid={`task-row-${task.task_id}`}>
                <div className="task-card-main">
                  <span className={`task-icon tone-${meta.tone}`}><Icon name={task.mode === 'intent' ? 'spark' : task.mode === 'regression' ? 'history' : 'file'} size={18} /></span>
                  <div className="task-copy">
                    <div className="task-title-row">
                      <strong>{task.name}</strong>
                      <Badge tone={meta.tone}>{meta.label}</Badge>
                      {task.archived_at ? <Badge tone="warn">已归档</Badge> : null}
                      {task.workflow_ref ? <Badge tone="ai"><Icon name="git" size={11} />{task.workflow_ref}</Badge> : null}
                    </div>
                    <p className="task-target mono">{task.mode === 'scenario' ? task.scenario : task.mode === 'intent' ? task.intent : task.mode === 'workflow' ? task.workflow_ref : meta.hint || '—'}</p>
                    <div className="task-meta">
                      <span className="mono">{task.task_id}</span>
                      <span>·</span>
                      <span>{task.env_keys?.length ? `${task.env_keys.length} 个环境键` : '无环境覆盖'}</span>
                      <span>·</span>
                      <span>更新于 {relativeTime(task.updated_at)}</span>
                    </div>
                  </div>
                </div>
                <div className="task-last">
                  {task.last_run_id ? (
                    <a href={href(task.workflow_ref ? `/tasks?focus=${task.task_id}` : `/history?focus=${task.last_run_id}`)} className="task-last-link">
                      <StatusBadge status={task.last_status || 'done'} />
                      <span>{task.last_status === 'failed' ? '最近失败' : task.last_status === 'passed' || task.last_status === 'done' ? '最近成功' : statusLabel(task.last_status)}</span>
                    </a>
                  ) : <Badge tone="neutral">未运行</Badge>}
                </div>
                <div className="task-actions">
                  <Button size="sm" variant="ghost" icon="eye" onClick={() => setDetail(task)}>详情</Button>
                  {task.archived_at ? (
                    <Button size="sm" variant="ghost" icon="refresh" onClick={() => { setArchiveTarget(task); setArchiveNext(false) }} data-testid={`archive-task-${task.task_id}`}>取消归档</Button>
                  ) : (
                    <>
                      <Button size="sm" variant="ghost" icon="edit" onClick={() => openEdit(task)}>编辑</Button>
                      <Button size="sm" variant="primary" icon="play" loading={runTask.isPending && runTask.variables === task.task_id} onClick={() => runTask.mutate(task.task_id)}>运行</Button>
                      <Button size="sm" variant="ghost" icon="archive" onClick={() => { setArchiveTarget(task); setArchiveNext(true) }} data-testid={`archive-task-${task.task_id}`}>归档</Button>
                    </>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      ) : null}

      {activeJob ? (
        <section className="panel job-panel">
          <SectionHeader
            title="任务执行"
            description={`job ${activeJob}`}
            icon="terminal"
            actions={
              <>
                {jobQuery.data ? <StatusBadge status={jobQuery.data.status} /> : null}
                <Button size="sm" variant="ghost" icon="close" onClick={() => { setActiveJob(''); navigate('/tasks', { replace: true }) }}>关闭</Button>
              </>
            }
          />
          {jobQuery.isLoading ? <Loading label="正在读取 job…" /> : null}
          {jobQuery.error ? <ErrorState error={jobQuery.error} onRetry={() => jobQuery.refetch()} /> : null}
          {jobQuery.data ? <JobPanel job={jobQuery.data} /> : null}
        </section>
      ) : null}

      <section className="panel task-lifecycle">
        <SectionHeader title="任务生命周期" description="变更必须回到工作台出新版本，才能保持可追溯。" icon="git" />
        <div className="lifecycle-track">
          {['草案', '审阅', '已批准', '已发布', '运行', '报告'].map((step, index) => (
            <div className="lifecycle-step" key={step}>
              <span className={`lifecycle-dot ${index < 3 ? 'is-done' : index === 3 ? 'is-active' : ''}`}>{index < 3 ? <Icon name="check" size={11} /> : index + 1}</span>
              <strong>{step}</strong>
              {index < 5 ? <span className="lifecycle-line" /> : null}
            </div>
          ))}
        </div>
      </section>

      <Modal
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        title={editing ? '编辑任务' : '新建任务'}
        subtitle="已发布流程的图不可在此修改；本表单只维护运行入口与环境。"
        size="lg"
        footer={<><Button variant="ghost" onClick={() => setEditorOpen(false)}>取消</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{editing ? '保存任务' : '创建任务'}</Button></>}
      >
        <Field label="任务名称" htmlFor="task-name"><input id="task-name" className="input" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如 订单闭环冒烟" autoFocus /></Field>
        <Field label="运行模式">
          <Select value={form.mode} onChange={(event) => setForm({ ...form, mode: event.target.value })} disabled={editing?.mode === 'demo' || editing?.mode === 'regression'}>
            <option value="scenario">场景（固定 YAML）</option>
            <option value="intent">意图（自然语言）</option>
            <option value="demo">演示</option>
            <option value="regression">回归</option>
            <option value="workflow">已发布流程</option>
          </Select>
        </Field>
        {form.mode === 'scenario' ? (
          <Field label="场景文件">
            {scenarios.length ? (
              <Select value={form.scenario} onChange={(event) => setForm({ ...form, scenario: event.target.value })}>
                <option value="">选择场景</option>
                {scenarios.map((scenario: Scenario) => <option key={scenario.id} value={scenario.id}>{scenario.name} · {scenario.cases ?? 0} 用例</option>)}
              </Select>
            ) : <input className="input mono" value={form.scenario} onChange={(event) => setForm({ ...form, scenario: event.target.value })} placeholder="scenarios/api_smoke.yaml" />}
          </Field>
        ) : null}
        {form.mode === 'intent' ? (
          <Field label="意图"><textarea className="textarea" rows={4} value={form.intent} onChange={(event) => setForm({ ...form, intent: event.target.value })} placeholder="用一句自然语言描述要验证什么" /></Field>
        ) : null}
        {form.mode === 'workflow' ? (
          <Field label="已发布流程" hint="只执行发布时的不可变版本；图变更需回工作台出新版本。">
            {publishedWorkflows.length ? (
              <Select value={form.workflow_ref} onChange={(event) => setForm({ ...form, workflow_ref: event.target.value })}>
                <option value="">选择发布版本</option>
                {publishedWorkflows.map((workflow: Workflow) => <option key={workflow.published_ref} value={workflow.published_ref}>{workflow.name || workflow.wid} · {workflow.published_ref}</option>)}
              </Select>
            ) : <input className="input mono" value={form.workflow_ref} onChange={(event) => setForm({ ...form, workflow_ref: event.target.value })} placeholder="w-xxxxxxxx@1" />}
          </Field>
        ) : null}
        <Field label="环境变量" hint="密钥以 *** 回显；空白沿用旧值。"><EnvEditor value={env} onChange={setEnv} /></Field>
        <div className="checkbox-row">
          <label><input type="checkbox" checked={form.regression} onChange={(event) => setForm({ ...form, regression: event.target.checked })} /> 复用回归库</label>
          <label><input type="checkbox" checked={form.no_llm} onChange={(event) => setForm({ ...form, no_llm: event.target.checked })} /> 不调用模型</label>
        </div>
      </Modal>

      <Modal
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail?.name}
        subtitle={detail ? `${detail.task_id} · ${modeMeta[detail.mode]?.label || detail.mode}` : ''}
        size="lg"
        footer={<><Button variant="ghost" onClick={() => setDetail(null)}>关闭</Button>{detail?.archived_at ? <Button variant="soft" icon="refresh" onClick={() => { setArchiveTarget(detail); setArchiveNext(false) }} data-testid={`archive-task-${detail?.task_id || ''}-detail`}>取消归档</Button> : <><Button variant="ghost" icon="archive" onClick={() => { setArchiveTarget(detail); setArchiveNext(true) }} data-testid={`archive-task-${detail?.task_id || ''}-detail`}>归档</Button><Button variant="ghost" icon="edit" onClick={() => detail && openEdit(detail)}>编辑</Button><Button variant="primary" icon="play" loading={runTask.isPending} onClick={() => detail && runTask.mutate(detail.task_id)}>运行任务</Button></>}</>}
      >
        {detail ? <TaskDetail task={detail} onArchive={(archived) => { setArchiveTarget(detail); setArchiveNext(archived) }} /> : null}
      </Modal>

      <ConfirmDialog
        open={!!archiveTarget}
        title={archiveNext ? '归档任务' : '取消归档任务'}
        message={archiveNext ? <>确定归档任务「{archiveTarget?.name}」？<br />只归档，不删除配置与历史运行；归档后默认列表不再显示。</> : <>确定取消归档任务「{archiveTarget?.name}」？取消后任务会重新出现在默认列表。</>}
        confirmLabel={archiveNext ? '归档任务' : '取消归档'}
        confirmTestId="confirm-archive"
        danger={archiveNext}
        busy={archiveMutation.isPending}
        onClose={() => setArchiveTarget(null)}
        onConfirm={() => archiveTarget && archiveMutation.mutate({ task: archiveTarget, archived: archiveNext })}
      />
    </div>
  )
}

function TaskDetail({ task, onArchive }: { task: Task; onArchive: (archived: boolean) => void }) {
  const status = task.last_status || 'none'
  return (
    <div className="stack-lg">
      <div className="detail-status-row">
        <StatusBadge status={status} />
        <Badge tone={modeMeta[task.mode]?.tone || 'neutral'}>{modeMeta[task.mode]?.label || task.mode}</Badge>
        {task.workflow_ref ? <Badge tone="ai"><Icon name="git" size={11} />{task.workflow_ref}</Badge> : null}
        {task.archived_at ? <Badge tone="warn">已归档 · {formatTime(task.archived_at)}</Badge> : null}
      </div>
      <dl className="fact-list two-col">
        <div><dt>任务 ID</dt><dd className="mono">{task.task_id}</dd></div>
        <div><dt>更新时间</dt><dd>{formatTime(task.updated_at)}</dd></div>
        <div><dt>运行目标</dt><dd className="mono word-break">{task.mode === 'scenario' ? task.scenario : task.mode === 'intent' ? task.intent : task.mode === 'workflow' ? task.workflow_ref : modeMeta[task.mode]?.hint}</dd></div>
        <div><dt>最近运行</dt><dd className="mono">{task.last_run_id ? shortId(task.last_run_id) : '—'}</dd></div>
        <div><dt>环境键</dt><dd>{task.env_keys?.length ? task.env_keys.join(', ') : '无'}</dd></div>
        <div><dt>回归复用</dt><dd>{task.regression ? '开启' : '关闭'} · {task.no_llm ? '不调用模型' : '允许模型'}</dd></div>
      </dl>
      {task.workflow_ref ? (
        <div className="notice-inline">
          <Icon name="git" size={15} />
          <div><strong>可回溯到发布版本</strong><p>图变更请回到工作台；从 <code>{task.workflow_ref}</code> 出新的 rev。</p></div>
        </div>
      ) : null}
      <div className="detail-danger">
        <div><strong>{task.archived_at ? '取消归档' : '归档任务'}</strong><span>{task.archived_at ? '恢复后任务重新出现在默认列表。' : '只归档，不删除历史运行与报告。'}</span></div>
        <Button size="sm" variant={task.archived_at ? 'soft' : 'danger'} icon={task.archived_at ? 'refresh' : 'archive'} onClick={() => onArchive(!task.archived_at)}>{task.archived_at ? '取消归档' : '归档'}</Button>
      </div>
    </div>
  )
}

function JobPanel({ job }: { job: Job }) {
  return (
    <div className="job-view">
      <div className="job-summary">
        <div><span>状态</span><strong className={`tone-${classForStatus(job.status)}`}>{statusLabel(job.status)}</strong></div>
        <div><span>耗时</span><strong>{formatDuration(job.elapsed_ms)}</strong></div>
        <div><span>模式</span><strong>{job.mode || '—'}</strong></div>
        <div><span>结果</span><strong>{job.summary?.total ? `${job.summary.passed || 0}/${job.summary.total}` : '—'}</strong></div>
      </div>
      {job.error ? <div className="step-error"><Icon name="warning" size={14} />{errorText(job.error)}</div> : null}
      <div className="phase-list">
        {(job.phases || []).map((phase, index) => (
          <div className="phase-row" key={`${phase.name}-${index}`}>
            <span className={`phase-dot tone-${classForStatus(phase.status)}`} />
            <strong>{phase.name}</strong>
            <span>{phase.detail || phase.status}</span>
          </div>
        ))}
      </div>
      {job.status === 'done' && job.run_id ? (
        <a className="btn btn-primary btn-sm" href={href(job.run_id ? `/history?focus=${job.run_id}` : '/history')}>查看运行报告 <Icon name="arrow" size={14} /></a>
      ) : job.status === 'failed' ? <Badge tone="bad">执行失败，请检查阶段详情</Badge> : <Loading label="执行中，自动刷新…" />}
    </div>
  )
}
