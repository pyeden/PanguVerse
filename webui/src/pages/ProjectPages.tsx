import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRoute, navigate, withQuery, href } from '../shared/router'
import { api, studioApi } from '../shared/api'
import {
  formatDuration,
  formatTime,
  rateOf,
  relativeTime,
  runCounts,
  runWhen,
  shortId,
  statusLabel,
  statusOf,
  pretty,
  classForStatus,
} from '../shared/utils'
import type { AnyRecord, Batch, Project, RunDetail as RunDetailType, RunSummary, Suite } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  EnvEditor,
  ErrorState,
  Field,
  JsonBlock,
  JsonTree,
  Kpi,
  Loading,
  Modal,
  PageHeader,
  Progress,
  SearchInput,
  SectionHeader,
  Select,
  StatusBadge,
  Tabs,
  useToast,
} from '../components/ui'
import { ProjectTabs } from '../components/Shell'
import { RunTable } from '../components/RunTable'

function ProjectHeader({ project, current, children }: { project: Project; current: string; children?: React.ReactNode }) {
  return (
    <>
      <div className="project-head">
        <div className="project-head-main">
          <div className="eyebrow"><span className="live-dot" /> 项目 · {project.project_id}</div>
          <div className="project-title-row">
            <h1>{project.name}</h1>
            <StatusBadge status={project.last_run ? statusOf(project.last_run) : 'none'} />
            {project.archived_at ? <Badge tone="warn">已归档</Badge> : null}
          </div>
          <div className="project-meta-line">
            <span className="mono">{project.base_url || '未配置站点地址'}</span>
            <span className="meta-divider" />
            <span>{project.suite_count ?? project.suites?.length ?? 0} 个套件</span>
            <span className="meta-divider" />
            <span>{project.last_run ? `上次运行 ${relativeTime(project.last_run.at || project.last_run.finished_at || project.last_run.started_at)}` : '还没有运行'}</span>
          </div>
        </div>
        <div className="project-head-actions">{children}</div>
      </div>
      <ProjectTabs pid={project.project_id} current={current} />
    </>
  )
}

export function ProjectOverviewPage() {
  const route = useRoute()
  const pid = route.parts[1]
  const queryClient = useQueryClient()
  const { push } = useToast()
  const projectQuery = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid), enabled: !!pid })
  const summaryQuery = useQuery({ queryKey: ['project-summary', pid], queryFn: () => api.projectSummary(pid, 5), enabled: !!pid })

  if (projectQuery.isLoading) return <Loading label="正在读取项目…" />
  if (projectQuery.error || !projectQuery.data) return <ErrorState error={projectQuery.error} onRetry={() => projectQuery.refetch()} title="项目读取失败" />
  const project = projectQuery.data
  const summary = summaryQuery.data || {}
  const recentRuns: RunSummary[] = Array.isArray(summary.recent_runs) ? summary.recent_runs : []
  const failures: AnyRecord[] = Array.isArray(summary.failure_top) ? summary.failure_top : []
  const rate = rateOf(summary.pass_rate?.passed ?? project.pass_rate?.passed, summary.pass_rate?.total ?? project.pass_rate?.total)
  const latest = recentRuns[0] || project.last_run
  const latestStatus = latest ? statusOf(latest) : 'none'
  const failureTotal = failures.reduce((sum, item) => sum + Number(item.count || 0), 0)
  const suites = project.suites || []
  const workflowSuites = suites.filter((suite) => !!suite.workflow_ref)
  const suiteHealth = suites.map((suite) => {
    const run = recentRuns.find((item) => item.suite_id === suite.suite_id)
    return { suite, run }
  })

  return (
    <div className="page project-page">
      <ProjectHeader project={project} current="overview">
        <Button variant="ghost" icon="settings" onClick={() => navigate(`/p/${pid}/settings`)}>设置</Button>
        <Button variant="primary" icon="play" onClick={() => navigate(`/p/${pid}/run`)}>运行</Button>
      </ProjectHeader>

      <div className="kpi-grid">
        <Kpi label="累计通过率" value={rate === null ? '—' : `${rate}%`} tone={rate !== null && rate < 80 ? 'bad' : 'ok'} sub={`${summary.pass_rate?.passed ?? 0}/${summary.pass_rate?.total ?? 0} 用例 · 全部历史`} icon="check" />
        <Kpi label="最近结果" value={statusLabel(latestStatus)} tone={classForStatus(latestStatus)} sub={latest ? relativeTime(runWhen(latest)) : '等待首次运行'} icon="play" />
        <Kpi label="失败 Top 3" value={failureTotal} tone={failureTotal ? 'bad' : 'ok'} sub="最近运行失败用例合计" icon="warning" />
        <Kpi label="套件" value={suites.length} sub={`${suiteHealth.filter((item) => item.run).length} 个已有运行记录`} icon="layers" />
      </div>

      <div className="project-overview-grid">
        <section className="panel recent-panel">
          <SectionHeader
            title="最近运行"
            description="点任意一行进入报告；报告保留 AI 总结来源与发布版本回溯。"
            icon="history"
            actions={<Button size="sm" variant="ghost" onClick={() => navigate(`/p/${pid}/history`)}>全部历史 <Icon name="arrow" size={14} /></Button>}
          />
          {summaryQuery.isLoading ? <Loading label="加载最近运行…" /> : null}
          {summaryQuery.error ? <ErrorState error={summaryQuery.error} onRetry={() => summaryQuery.refetch()} /> : null}
          {!summaryQuery.isLoading && !recentRuns.length ? (
            <EmptyState icon="play" title="还没有运行记录" actions={<Button size="sm" variant="primary" icon="play" onClick={() => navigate(`/p/${pid}/run`)}>发起第一次运行</Button>}>
              选择套件并运行后，结果会自动出现在这里。
            </EmptyState>
          ) : (
            <div className="run-list">
              {recentRuns.map((run) => <RunRow key={run.run_id} run={run} project={project} />)}
            </div>
          )}
        </section>

        <aside className="panel health-panel">
          <SectionHeader title="项目体检" description="基于最近 5 次运行自动聚合" icon="shield" />
          <div className="health-score">
            <div className={`health-ring ${rate !== null && rate < 80 ? 'bad' : 'ok'}`} style={{ '--score': `${rate ?? 0}%` } as React.CSSProperties}>
              <strong>{rate === null ? '—' : `${rate}%`}</strong>
              <span>通过率</span>
            </div>
            <div className="health-copy">
              <Badge tone={latestStatus === 'passed' ? 'ok' : latestStatus === 'failed' || latestStatus === 'errored' ? 'bad' : 'neutral'} dot>
                {latest ? statusLabel(latestStatus) : '暂无数据'}
              </Badge>
              <p>{healthNarrative(rate, failures, suites.length)}</p>
            </div>
          </div>
          <div className="health-findings">
            {failures.length ? failures.map((failure) => (
              <div className="health-finding" key={failure.kind || failure.label}>
                <span className="finding-icon bad"><Icon name="warning" size={14} /></span>
                <div>
                  <strong>{failure.kind || failure.label || '未归类失败'}</strong>
                  <span>{failure.count || 0} 个用例 · {Array.isArray(failure.cases) ? failure.cases.slice(0, 3).join(' · ') : ''}</span>
                </div>
              </div>
            )) : (
              <div className="health-finding">
                <span className="finding-icon ok"><Icon name="check" size={14} /></span>
                <div><strong>没有聚合失败</strong><span>最近 5 次运行没有失败用例。</span></div>
              </div>
            )}
            <div className="health-finding">
              <span className="finding-icon neutral"><Icon name="server" size={14} /></span>
              <div><strong>运行环境固定</strong><span>仅使用项目已配置 env；调试不进入正式历史。</span></div>
            </div>
          </div>
          <div className="health-foot">
            <span><Icon name="code" size={13} /> 规则回退</span>
            <span>数字来自报告本体</span>
          </div>
        </aside>
      </div>

      <section className="panel">
        <SectionHeader
          title="套件与绑定"
          description="套件决定运行目标；流程发布后在任务模块绑定到项目环境。"
          icon="layers"
          actions={<Button size="sm" variant="ghost" icon="plus" onClick={() => navigate(`/p/${pid}/settings`)}>管理套件</Button>}
        />
        {suiteHealth.length ? (
          <div className="suite-list">
            {suiteHealth.map(({ suite, run }) => (
              <div className="suite-row" key={suite.suite_id}>
                <span className={`suite-icon tone-${classForStatus(run ? statusOf(run) : 'none')}`}><Icon name="file" size={16} /></span>
                <div className="suite-copy">
                  <strong>{suite.name}</strong>
                  <span className="mono">{suite.scenario || suite.workflow_ref}</span>
                </div>
                <div className="suite-tags">
                  {(suite.tags || []).map((tag) => <Badge key={tag} tone="neutral">{tag}</Badge>)}
                </div>
                <div className="suite-last">
                  {run ? <a href={href(`/p/${pid}/runs/${run.run_id}`)} className="inline-link">{statusLabel(statusOf(run))} · {relativeTime(runWhen(run))}</a> : <span className="muted">尚未运行</span>}
                </div>
                <Button size="sm" variant="ghost" icon="play" onClick={() => navigate(withQuery(`/p/${pid}/run`, { suite: suite.suite_id }))}>运行</Button>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon="layers" title="还没有绑定套件" actions={<Button size="sm" variant="primary" icon="plus" onClick={() => navigate(`/p/${pid}/settings`)}>添加套件</Button>}>
            套件绑定一个场景文件；之后可用「一键全跑」串行执行。
          </EmptyState>
        )}
      </section>

      <section className="panel">
        <SectionHeader title="流程绑定" description="项目套件可以直接绑定已发布流程；执行只读发布版本。" icon="git" actions={<Button size="sm" variant="ghost" icon="settings" onClick={() => navigate(`/p/${pid}/settings`)}>绑定流程</Button>} />
        {workflowSuites.length ? (
          <div className="workflow-binding-list">
            {workflowSuites.map((suite) => (
              <div className="workflow-binding-row" key={suite.suite_id}>
                <span className="binding-node"><Icon name="git" size={15} /></span>
                <div><strong>{suite.name}</strong><span className="mono">{suite.workflow_ref}</span></div>
                <Badge tone="ai">发布版本</Badge>
                <a className="inline-link" href={href(`/studio/${String(suite.workflow_ref).split('@')[0]}/review`)}>查看审阅 <Icon name="arrow" size={12} /></a>
              </div>
            ))}
          </div>
        ) : (
          <div className="binding-empty">
            <div className="binding-visual">
              <span className="binding-node"><Icon name="spark" size={16} /></span>
              <span className="binding-line" />
              <span className="binding-node"><Icon name="link" size={16} /></span>
              <span className="binding-line" />
              <span className="binding-node"><Icon name="play" size={16} /></span>
            </div>
            <div>
              <strong>把发布流程绑定为项目套件</strong>
              <p>在项目设置中新增套件时选择「已发布流程」，无需场景文件；报告会携带 <code>workflow_ref</code> 回溯到发布版本。</p>
            </div>
            <Button variant="ghost" size="sm" icon="plus" onClick={() => navigate(`/p/${pid}/settings`)}>去绑定</Button>
          </div>
        )}
      </section>
    </div>
  )
}

function healthNarrative(rate: number | null, failures: AnyRecord[], suiteCount: number): string {
  if (rate === null) return '还没有足够的运行数据。先运行一个套件，体检会基于真实报告自动生成。'
  if (rate >= 95) return `当前 ${suiteCount} 个套件整体稳定，通过率处于高位。继续关注新增失败而不是重复全量跑。`
  if (rate >= 80) return '通过率可用，但已有失败聚集。建议先处理失败 Top 3，再执行回归。'
  if (failures.length) return '存在明显失败聚集。优先检查配置/认证类失败，避免把环境问题误判为断言失败。'
  return '通过率偏低，建议从失败用例的 inputs 与最近一次调试验证开始排查。'
}

function RunRow({ run, project }: { run: RunSummary; project: Project }) {
  const status = statusOf(run)
  const counts = runCounts(run)
  const suite = project.suites?.find((item) => item.suite_id === run.suite_id)
  return (
    <a className="run-row" href={href(`/p/${project.project_id}/runs/${run.run_id}`)}>
      <span className={`run-status tone-${classForStatus(status)}`}><span className="status-dot" />{statusLabel(status)}</span>
      <span className="run-main">
        <strong>{suite?.name || run.suite_name || run.meta?.suite || '未归类运行'}</strong>
        <span className="mono">{shortId(run.run_id, 12)}</span>
      </span>
      <span className="run-count">{counts.total ? `${counts.passed}/${counts.total}` : '—'}</span>
      <span className="run-duration">{formatDuration(run.duration_ms)}</span>
      <span className="run-when">{relativeTime(runWhen(run))}</span>
      <Icon name="chevron" size={16} className="run-chevron" />
    </a>
  )
}

export function ProjectRunPage() {
  const route = useRoute()
  const pid = route.parts[1]
  const batchId = route.query.get('batch') || ''
  const { push } = useToast()
  const projectQuery = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid), enabled: !!pid })
  const [selected, setSelected] = useState<string[]>([])
  const [env, setEnv] = useState<AnyRecord>({})
  const [model, setModel] = useState<'suites' | 'single'>('suites')
  const [initialized, setInitialized] = useState(false)

  useEffect(() => {
    if (!projectQuery.data || initialized) return
    const preselect = route.query.get('suite')
    setSelected(preselect ? [preselect] : (projectQuery.data.suites || []).map((suite) => suite.suite_id))
    setInitialized(true)
  }, [projectQuery.data, initialized, route.query])

  const batchQuery = useQuery({
    queryKey: ['batch', batchId],
    queryFn: () => api.batch(batchId),
    enabled: !!batchId,
    refetchInterval: (query) => {
      const data = query.state.data as Batch | undefined
      return data && ['done', 'failed'].includes(data.status) ? false : 1800
    },
  })

  const runMutation = useMutation({
    mutationFn: () => api.runProject(pid, {
      suite_ids: model === 'single' ? selected : (selected.length === (projectQuery.data?.suites?.length || 0) ? [] : selected),
      ...(Object.keys(env).length ? { env } : {}),
    }),
    onSuccess: (result) => {
      push(`批次已提交：${result.batch_id}`, 'ok')
      navigate(withQuery(`/p/${pid}/run`, { batch: result.batch_id }), { replace: true })
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  if (projectQuery.isLoading) return <Loading label="正在读取项目…" />
  if (projectQuery.error || !projectQuery.data) return <ErrorState error={projectQuery.error} onRetry={() => projectQuery.refetch()} />
  const project = projectQuery.data
  const suites = project.suites || []
  const batch = batchQuery.data

  const toggle = (suiteId: string) => {
    setSelected((current) => current.includes(suiteId) ? current.filter((id) => id !== suiteId) : [...current, suiteId])
  }

  return (
    <div className="page project-page">
      <ProjectHeader project={project} current="run">
        <Button variant="ghost" icon="history" onClick={() => navigate(`/p/${pid}/history`)}>历史</Button>
        <Button variant="primary" icon="play" loading={runMutation.isPending} disabled={!selected.length || runMutation.isPending} onClick={() => runMutation.mutate()}>
          运行所选套件
        </Button>
      </ProjectHeader>

      <div className="run-layout">
        <section className="panel run-config">
          <SectionHeader title="选择运行范围" description="同一批次内套件串行执行，避免争抢目标服务。" icon="layers" />
          {!suites.length ? (
            <EmptyState icon="layers" title="项目还没有套件" actions={<Button size="sm" variant="primary" onClick={() => navigate(`/p/${pid}/settings`)}>去设置添加</Button>}>
              套件绑定一个场景文件，运行会按套件生成独立执行记录。
            </EmptyState>
          ) : null}

          {suites.length ? (
            <>
              <div className="mode-switch" role="tablist" aria-label="运行范围">
                <button type="button" role="tab" aria-selected={model === 'suites'} className={model === 'suites' ? 'is-active' : ''} onClick={() => setModel('suites')}>
                  <Icon name="layers" size={16} /><span><strong>按套件选择</strong><small>可多选，串行执行</small></span>
                </button>
                <button type="button" role="tab" aria-selected={model === 'single'} className={model === 'single' ? 'is-active' : ''} onClick={() => setModel('single')}>
                  <Icon name="play" size={16} /><span><strong>单套件调试</strong><small>只跑一个套件</small></span>
                </button>
              </div>

              <div className="suite-picker">
                {suites.map((suite) => {
                  const checked = selected.includes(suite.suite_id)
                  return (
                    <button type="button" key={suite.suite_id} className={`suite-pick ${checked ? 'is-checked' : ''}`} onClick={() => {
                      if (model === 'single') setSelected([suite.suite_id])
                      else toggle(suite.suite_id)
                    }}>
                      <span className="suite-check">{checked ? <Icon name="check" size={12} /> : null}</span>
                      <span className="suite-pick-copy">
                        <strong>{suite.name}</strong>
                        <small className="mono">{suite.scenario || suite.workflow_ref}</small>
                      </span>
                      <span className="suite-pick-meta">{suite.workflow_ref ? 'workflow' : (suite.tags?.join(' · ') || 'scenario')}</span>
                    </button>
                  )
                })}
              </div>

              <div className="run-env">
                <div className="run-env-head">
                  <span><Icon name="shield" size={15} /> 本次环境覆盖</span>
                  <Badge tone="neutral">不写回项目</Badge>
                </div>
                <p>留空表示沿用项目/套件已保存的值。密钥显示为 <code>***</code>；本次运行不落新基线。</p>
                <EnvEditor value={env} onChange={setEnv} hint="仅用于本次批次；空值不覆盖。" />
              </div>

              <div className="run-submit-bar">
                <div>
                  <strong>{selected.length} 个套件待运行</strong>
                  <span>预计串行执行；可在下方实时查看阶段。</span>
                </div>
                <Button variant="primary" icon="play" loading={runMutation.isPending} disabled={!selected.length} onClick={() => runMutation.mutate()}>提交批次</Button>
              </div>
            </>
          ) : null}
        </section>

        <aside className="panel run-aside">
          <SectionHeader title="运行门禁" description="正式运行只使用已配置环境" icon="shield" />
          <ul className="check-list">
            <li className="is-done"><span><Icon name="check" size={12} /></span>项目已创建</li>
            <li className={suites.length ? 'is-done' : ''}><span><Icon name={suites.length ? 'check' : 'warning'} size={12} /></span>{suites.length ? `${suites.length} 个套件可用` : '至少绑定一个套件'}</li>
            <li className={selected.length ? 'is-done' : ''}><span><Icon name={selected.length ? 'check' : 'warning'} size={12} /></span>{selected.length ? '已选择运行范围' : '选择运行范围'}</li>
            <li className="is-done"><span><Icon name="check" size={12} /></span>无临时 URL 覆盖入口</li>
          </ul>
          <div className="aside-note">
            <Icon name="bug" size={15} />
            <div><strong>调试与运行分离</strong><p>画布里的单步调试写独立目录，不进入正式历史，也不会触发任务状态回写。</p></div>
          </div>
        </aside>
      </div>

      {batchId ? (
        <section className="panel batch-panel" aria-live="polite">
          <SectionHeader
            title="批次进度"
            description={`batch ${batchId}`}
            icon="terminal"
            actions={batch ? <Badge tone={batch.status === 'failed' ? 'bad' : batch.status === 'done' ? 'ok' : 'run'} dot>{statusLabel(batch.status)}</Badge> : <Loading label="读取中" />}
          />
          {batchQuery.error ? <ErrorState error={batchQuery.error} onRetry={() => batchQuery.refetch()} /> : null}
          {batch ? (
            <>
              <div className="batch-summary">
                <div><span>通过用例</span><strong>{batch.aggregate?.passed}/{batch.aggregate?.total}</strong></div>
                <div><span>套件通过</span><strong>{batch.aggregate?.suites_passed}/{batch.suites.length}</strong></div>
                <div><span>总耗时</span><strong>{formatDuration(batch.aggregate?.duration_ms)}</strong></div>
                <div><span>批次状态</span><strong className={`tone-${classForStatus(batch.status)}`}>{statusLabel(batch.status)}</strong></div>
              </div>
              <Progress value={batch.aggregate?.total ? (batch.aggregate.passed / batch.aggregate.total) * 100 : 0} tone={batch.status === 'failed' ? 'bad' : 'ok'} />
              <div className="batch-list">
                {batch.suites.map((item) => (
                  <div className="batch-row" key={item.suite_id}>
                    <span className={`run-status tone-${classForStatus(item.status)}`}><span className="status-dot" />{statusLabel(item.status)}</span>
                    <div className="batch-row-main">
                      <strong>{item.name}</strong>
                      <span className="mono">job {item.job_id}</span>
                    </div>
                    <span>{item.total ? `${item.passed}/${item.total}` : '—'}</span>
                    {item.run_id ? <a href={href(`/p/${pid}/runs/${item.run_id}`)} className="btn btn-ghost btn-sm">查看报告 <Icon name="arrow" size={13} /></a> : <span className="muted">{item.error || '等待执行'}</span>}
                  </div>
                ))}
              </div>
              {['done', 'failed'].includes(batch.status) ? (
                <div className="batch-done-actions">
                  <span><Icon name={batch.status === 'done' ? 'check' : 'warning'} size={15} /> 批次已结束，结果已写入历史。</span>
                  <Button size="sm" variant="ghost" icon="history" onClick={() => navigate(`/p/${pid}/history`)}>查看项目历史</Button>
                </div>
              ) : null}
            </>
          ) : <Loading label={batchQuery.isLoading ? '正在读取批次…' : '等待批次数据…'} />}
        </section>
      ) : null}
    </div>
  )
}

export function ProjectHistoryPage() {
  const route = useRoute()
  const pid = route.parts[1]
  const projectQuery = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid), enabled: !!pid })
  const suite = route.query.get('suite') || 'all'
  const status = route.query.get('status') || 'all'
  const page = Math.max(1, Number(route.query.get('page') || 1))
  const limit = 20
  const offset = (page - 1) * limit
  const [selected, setSelected] = useState<string[]>([])
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareData, setCompareData] = useState<{ left: RunSummary; right: RunSummary; leftDetail: RunDetailType; rightDetail: RunDetailType } | null>(null)
  const [compareError, setCompareError] = useState<string>('')
  const [compareBusy, setCompareBusy] = useState(false)

  const params = useMemo(() => {
    const next = new URLSearchParams({ limit: String(limit), offset: String(offset), status })
    if (suite !== 'all') next.set('suite', suite)
    return next
  }, [limit, offset, status, suite])

  const runsQuery = useQuery({
    queryKey: ['project-history', pid, suite, status, offset],
    queryFn: () => api.queryRuns(params),
    enabled: !!pid,
  })

  const setFilter = (key: 'suite' | 'status', value: string, resetPage = true) => {
    const next = new URLSearchParams(route.query)
    if (value && value !== 'all') next.set(key, value)
    else next.delete(key)
    if (resetPage) next.delete('page')
    navigate(withQuery(`/p/${pid}/history`, next), { replace: true })
  }

  const toggleCompare = (runId: string) => {
    setSelected((current) => current.includes(runId) ? current.filter((id) => id !== runId) : [...current, runId].slice(-2))
  }

  const openCompare = async () => {
    if (selected.length !== 2) return
    setCompareBusy(true)
    setCompareError('')
    try {
      const [leftDetail, rightDetail] = await Promise.all([api.run(selected[0]), api.run(selected[1])])
      setCompareData({
        left: { run_id: selected[0], summary: leftDetail.report?.summary, conclusion: leftDetail.report?.conclusion, started_at: leftDetail.report?.started_at, duration_ms: leftDetail.report?.duration_ms },
        right: { run_id: selected[1], summary: rightDetail.report?.summary, conclusion: rightDetail.report?.conclusion, started_at: rightDetail.report?.started_at, duration_ms: rightDetail.report?.duration_ms },
        leftDetail,
        rightDetail,
      })
      setCompareOpen(true)
    } catch (error) {
      setCompareError(error instanceof Error ? error.message : '读取运行详情失败')
    } finally {
      setCompareBusy(false)
    }
  }

  if (projectQuery.isLoading) return <Loading label="正在读取项目…" />
  if (projectQuery.error || !projectQuery.data) return <ErrorState error={projectQuery.error} onRetry={() => projectQuery.refetch()} />
  const project = projectQuery.data
  const runs = runsQuery.data?.items || []
  const total = runsQuery.data?.total || 0
  const pages = Math.max(1, Math.ceil(total / limit))

  return (
    <div className="page project-page">
      <ProjectHeader project={project} current="history">
        <Button variant="ghost" icon="play" onClick={() => navigate(`/p/${pid}/run`)}>新增运行</Button>
        <Button variant="primary" icon="sort" disabled={selected.length !== 2} loading={compareBusy} onClick={openCompare}>对比所选 ({selected.length}/2)</Button>
      </ProjectHeader>

      <div className="history-toolbar panel">
        <div className="filter-row">
          <label className="filter-field">
            <span>套件</span>
            <Select value={suite} onChange={(event) => setFilter('suite', event.target.value)}>
              <option value="all">全部套件</option>
              {(project.suites || []).map((item) => <option key={item.suite_id} value={item.suite_id}>{item.name}</option>)}
            </Select>
          </label>
          <label className="filter-field">
            <span>结果</span>
            <Select value={status} onChange={(event) => setFilter('status', event.target.value)}>
              <option value="all">全部结果</option>
              <option value="passed">通过</option>
              <option value="failed">失败</option>
              <option value="errored">异常</option>
              <option value="done">已结束</option>
            </Select>
          </label>
          <div className="filter-spacer" />
          <Badge tone="neutral">{total} 条记录</Badge>
          {(suite !== 'all' || status !== 'all') ? <Button size="sm" variant="ghost" icon="close" onClick={() => navigate(`/p/${pid}/history`, { replace: true })}>清除筛选</Button> : null}
        </div>
        <p className="filter-hint"><Icon name="link" size={13} /> 筛选与分页写入 URL，刷新或分享后可恢复。</p>
      </div>

      <section className="panel table-panel">
        {runsQuery.isLoading ? <Loading label="正在读取历史…" /> : runsQuery.error ? <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} /> : (
          <RunTable
            runs={runs}
            projects={[project]}
            showProject={false}
            selected={selected}
            onToggle={toggleCompare}
            emptyText="这个项目还没有匹配的执行"
            emptyAction={<Button size="sm" variant="primary" onClick={() => navigate(`/p/${pid}/run`)}>发起运行</Button>}
          />
        )}
        {pages > 1 ? (
          <div className="pagination">
            <span>第 {page} / {pages} 页 · 共 {total} 条</span>
            <div>
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => {
                const next = new URLSearchParams(route.query)
                if (page - 1 <= 1) next.delete('page')
                else next.set('page', String(page - 1))
                navigate(withQuery(`/p/${pid}/history`, next), { replace: true })
              }}>上一页</Button>
              <Button size="sm" variant="ghost" disabled={page >= pages} onClick={() => {
                const next = new URLSearchParams(route.query)
                next.set('page', String(page + 1))
                navigate(withQuery(`/p/${pid}/history`, next), { replace: true })
              }}>下一页</Button>
            </div>
          </div>
        ) : null}
      </section>

      {compareError ? <div className="inline-error"><Icon name="warning" size={15} />{compareError}</div> : null}

      <Modal
        open={compareOpen}
        onClose={() => setCompareOpen(false)}
        title="执行对比"
        subtitle={compareData ? `${shortId(compareData.left.run_id)} ↔ ${shortId(compareData.right.run_id)}` : ''}
        size="xl"
        footer={<Button variant="ghost" onClick={() => setCompareOpen(false)}>关闭</Button>}
      >
        {compareData ? <RunCompare data={compareData} /> : <Loading label="读取两份报告…" />}
      </Modal>
    </div>
  )
}

function RunCompare({ data }: { data: { left: RunSummary; right: RunSummary; leftDetail: RunDetailType; rightDetail: RunDetailType } }) {
  const leftCases = Array.isArray(data.leftDetail.report?.cases) ? data.leftDetail.report.cases : []
  const rightCases = Array.isArray(data.rightDetail.report?.cases) ? data.rightDetail.report.cases : []
  const keyOf = (item: AnyRecord) => String(item.case_id || item.name || '')
  const leftMap = new Map(leftCases.map((item: AnyRecord) => [keyOf(item), item]))
  const rightMap = new Map(rightCases.map((item: AnyRecord) => [keyOf(item), item]))
  const keys = Array.from(new Set([...leftMap.keys(), ...rightMap.keys()]))
  const rows = keys.map((key) => {
    const left = leftMap.get(key)
    const right = rightMap.get(key)
    const leftStatus = left?.status || 'missing'
    const rightStatus = right?.status || 'missing'
    let change = 'same'
    if (!left) change = 'added'
    else if (!right) change = 'removed'
    else if (leftStatus !== rightStatus) change = 'changed'
    return { key, left, right, change }
  })
  const regressions = rows.filter((row) => row.change === 'changed' && row.right?.status !== 'passed').length
  return (
    <div className="compare-view">
      <div className="compare-summary">
        <div><span>左</span><strong>{shortId(data.left.run_id)}</strong><small>{statusLabel(statusOf(data.left))} · {formatDuration(data.left.duration_ms)}</small></div>
        <div><span>右</span><strong>{shortId(data.right.run_id)}</strong><small>{statusLabel(statusOf(data.right))} · {formatDuration(data.right.duration_ms)}</small></div>
        <div className={regressions ? 'bad' : 'ok'}><span>回归风险</span><strong>{regressions ? `${regressions} 个` : '低'}</strong><small>状态变化且未通过</small></div>
      </div>
      <div className="compare-table-wrap">
        <table className="compare-table">
          <thead><tr><th>用例</th><th>左</th><th>右</th><th>变化</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td><strong>{row.right?.name || row.left?.name || row.key}</strong><span className="mono">{row.key}</span></td>
                <td><StatusBadge status={row.left?.status || 'none'} /></td>
                <td><StatusBadge status={row.right?.status || 'none'} /></td>
                <td><Badge tone={row.change === 'same' ? 'neutral' : row.change === 'added' ? 'cyan' : row.change === 'removed' ? 'bad' : 'warn'}>{row.change === 'same' ? '无变化' : row.change === 'added' ? '新增' : row.change === 'removed' ? '移除' : '状态变化'}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function ProjectSettingsPage() {
  const route = useRoute()
  const pid = route.parts[1]
  const queryClient = useQueryClient()
  const { push } = useToast()
  const projectQuery = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid), enabled: !!pid })
  const scenariosQuery = useQuery({ queryKey: ['scenarios'], queryFn: api.scenarios, staleTime: 60_000 })
  const draftsQuery = useQuery({ queryKey: ['studio-drafts'], queryFn: studioApi.listDrafts, staleTime: 15_000 })
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [env, setEnv] = useState<AnyRecord>({})
  const [ready, setReady] = useState(false)
  const [suiteOpen, setSuiteOpen] = useState(false)
  const [editingSuite, setEditingSuite] = useState<Suite | null>(null)
  const [suiteName, setSuiteName] = useState('')
  const [suiteScenario, setSuiteScenario] = useState('')
  const [suiteSource, setSuiteSource] = useState<'scenario' | 'workflow'>('scenario')
  const [suiteWorkflow, setSuiteWorkflow] = useState('')
  const [suiteTags, setSuiteTags] = useState('')
  const [suiteEnv, setSuiteEnv] = useState<AnyRecord>({})
  const [archiveProjectOpen, setArchiveProjectOpen] = useState(false)
  const [archiveProjectNext, setArchiveProjectNext] = useState(true)
  const [deletingSuite, setDeletingSuite] = useState<Suite | null>(null)

  useEffect(() => {
    if (!projectQuery.data || ready) return
    setName(projectQuery.data.name || '')
    setBaseUrl(projectQuery.data.base_url || '')
    setEnv(projectQuery.data.env || {})
    setReady(true)
  }, [projectQuery.data, ready])

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['project', pid] })
    queryClient.invalidateQueries({ queryKey: ['projects'] })
  }

  const saveProject = useMutation({
    mutationFn: () => api.updateProject(pid, { name: name.trim(), base_url: baseUrl.trim(), env }),
    onSuccess: () => { refresh(); push('项目设置已保存', 'ok') },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const saveSuite = useMutation({
    mutationFn: async () => {
      if (!suiteName.trim()) throw new Error('请填写套件名称')
      if (suiteSource === 'scenario' && !suiteScenario) throw new Error('请选择场景文件')
      if (suiteSource === 'workflow' && !suiteWorkflow) throw new Error('请选择已发布流程')
      const tags = suiteTags.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean)
      const body = { name: suiteName.trim(), ...(suiteSource === 'scenario' ? { scenario: suiteScenario } : { workflow_ref: suiteWorkflow }), tags, ...(Object.keys(suiteEnv).length ? { env: suiteEnv } : {}) }
      if (editingSuite) return api.updateSuite(pid, editingSuite.suite_id, body)
      return api.addSuite(pid, body)
    },
    onSuccess: () => {
      refresh()
      setSuiteOpen(false)
      push(editingSuite ? '套件已更新' : '套件已添加', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const deleteSuiteMutation = useMutation({
    mutationFn: (sid: string) => api.deleteSuite(pid, sid),
    onSuccess: () => { refresh(); setDeletingSuite(null); push('套件已删除', 'ok') },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const archiveProjectMutation = useMutation({
    mutationFn: (archived: boolean) => api.archiveProject(pid, archived),
    onSuccess: (next, archived) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      queryClient.setQueryData(['project', pid], next)
      setArchiveProjectOpen(false)
      push(archived ? '项目已归档；历史运行与报告保留' : '项目已取消归档', 'ok')
      if (archived) navigate('/projects?include_archived=1', { replace: true })
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  if (projectQuery.isLoading) return <Loading label="正在读取项目…" />
  if (projectQuery.error || !projectQuery.data) return <ErrorState error={projectQuery.error} onRetry={() => projectQuery.refetch()} />
  const project = projectQuery.data
  const scenarios = scenariosQuery.data || []
  const publishedWorkflows = (draftsQuery.data || []).filter((workflow) => !!workflow.published_ref)

  const openAddSuite = () => {
    setEditingSuite(null)
    setSuiteName('')
    setSuiteScenario(scenarios[0]?.id || '')
    setSuiteSource('scenario')
    setSuiteWorkflow('')
    setSuiteTags('')
    setSuiteEnv({})
    setSuiteOpen(true)
  }
  const openEditSuite = (suite: Suite) => {
    setEditingSuite(suite)
    setSuiteName(suite.name)
    setSuiteScenario(suite.scenario || '')
    setSuiteSource(suite.workflow_ref ? 'workflow' : 'scenario')
    setSuiteWorkflow(suite.workflow_ref || '')
    setSuiteTags((suite.tags || []).join(', '))
    setSuiteEnv(suite.env || {})
    setSuiteOpen(true)
  }

  return (
    <div className="page project-page">
      <ProjectHeader project={project} current="settings">
        <Button variant="ghost" icon="play" onClick={() => navigate(`/p/${pid}/run`)}>运行</Button>
        <Button variant="primary" icon="check" loading={saveProject.isPending} onClick={() => saveProject.mutate()}>保存设置</Button>
      </ProjectHeader>

      <div className="settings-grid">
        <section className="panel settings-panel">
          <SectionHeader title="基础信息" description="项目名称与站点地址用于运行环境组装和报表展示。" icon="settings" />
          <Field label="项目名称" htmlFor="project-name"><input id="project-name" className="input" value={name} onChange={(event) => setName(event.target.value)} maxLength={80} /></Field>
          <Field label="站点地址 base_url" hint="项目 env 中的 BASE_URL 会自动对齐该地址；套件环境可以覆盖。">
            <input className="input mono" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com" spellCheck={false} />
          </Field>
          <Field label="项目默认环境变量" hint="密钥以 *** 回显；空值保持原值，删除键使用删除按钮。">
            <EnvEditor value={env} onChange={setEnv} />
          </Field>
          <div className="form-actions">
            <Button variant="primary" icon="check" loading={saveProject.isPending} onClick={() => saveProject.mutate()}>保存基础信息</Button>
            <span className="muted">修改不会影响已产生的历史报告。</span>
          </div>
        </section>

        <aside className="panel danger-panel">
          <SectionHeader title="归档项目" description="只归档，不删历史运行、报告与套件配置。" icon="shield" />
          <div className="danger-row">
            <div><strong>{project.archived_at ? '项目已归档' : '归档项目'}</strong><p>{project.archived_at ? `归档于 ${formatTime(project.archived_at)}；取消归档后恢复默认列表可见。` : '归档后默认项目列表不再显示；直接 URL 仍可访问，历史全部保留。'}</p></div>
            <Button variant={project.archived_at ? 'soft' : 'danger'} size="sm" icon={project.archived_at ? 'refresh' : 'archive'} onClick={() => { setArchiveProjectNext(!project.archived_at); setArchiveProjectOpen(true) }} data-testid={`archive-project-${project.project_id}-detail`}>{project.archived_at ? '取消归档' : '归档'}</Button>
          </div>
          <div className="aside-note">
            <Icon name="shield" size={15} />
            <div><strong>审计优先</strong><p>归档与取消归档都会写审计；历史 run、report.json 与套件绑定不变。</p></div>
          </div>
        </aside>
      </div>

      <section className="panel">
        <SectionHeader
          title="套件"
          description={`${project.suites?.length || 0} 个套件绑定到本项目；每个套件对应场景文件或已发布流程。`}
          icon="layers"
          actions={<Button size="sm" variant="primary" icon="plus" onClick={openAddSuite}>添加套件</Button>}
        />
        {project.suites?.length ? (
          <div className="suite-settings-list">
            {project.suites.map((suite) => (
              <div className="suite-settings-row" key={suite.suite_id}>
                <span className="suite-icon"><Icon name="file" size={16} /></span>
                <div className="suite-copy">
                  <strong>{suite.name}</strong>
                  <span className="mono">{suite.scenario || suite.workflow_ref}</span>
                </div>
                <div className="suite-tags">{(suite.tags || []).map((tag) => <Badge key={tag}>{tag}</Badge>)}</div>
                <span className="mono muted">{suite.suite_id}</span>
                <div className="row-actions">
                  <Button size="sm" variant="ghost" icon="edit" onClick={() => openEditSuite(suite)}>编辑</Button>
                  <Button size="sm" variant="ghost" icon="trash" onClick={() => setDeletingSuite(suite)}>删除</Button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon="layers" title="还没有套件" actions={<Button size="sm" variant="primary" icon="plus" onClick={openAddSuite}>添加第一个套件</Button>}>
            套件把项目环境与场景文件或已发布流程绑定，是批量运行的执行单元。
          </EmptyState>
        )}
      </section>

      <Modal
        open={suiteOpen}
        onClose={() => setSuiteOpen(false)}
        title={editingSuite ? '编辑套件' : '添加套件'}
        subtitle="场景文件必须位于工作区或内置 scenarios/ 下。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setSuiteOpen(false)}>取消</Button>
            <Button variant="primary" loading={saveSuite.isPending} onClick={() => saveSuite.mutate()}>{editingSuite ? '保存套件' : '添加套件'}</Button>
          </>
        }
      >
        <Field label="套件名称" htmlFor="suite-name"><input id="suite-name" className="input" value={suiteName} onChange={(event) => setSuiteName(event.target.value)} placeholder="例如 登录与订单冒烟" autoFocus /></Field>
        <Field label="执行目标">
          <div className="segmented" style={{ alignSelf: 'flex-start' }}>
            <button type="button" className={suiteSource === 'scenario' ? 'is-active' : ''} onClick={() => setSuiteSource('scenario')}>场景文件</button>
            <button type="button" className={suiteSource === 'workflow' ? 'is-active' : ''} onClick={() => setSuiteSource('workflow')}>已发布流程</button>
          </div>
        </Field>
        {suiteSource === 'scenario' ? (
          <Field label="场景文件" hint={scenariosQuery.error ? '读取场景失败，可手动输入路径。' : '来自 /api/scenarios'}>
            {scenarios.length ? (
              <Select value={suiteScenario} onChange={(event) => setSuiteScenario(event.target.value)}>
                <option value="">选择场景</option>
                {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.name} · {scenario.cases ?? 0} 用例</option>)}
              </Select>
            ) : (
              <input className="input mono" value={suiteScenario} onChange={(event) => setSuiteScenario(event.target.value)} placeholder="scenarios/api_smoke.yaml" spellCheck={false} />
            )}
          </Field>
        ) : (
          <Field label="发布版本" hint="只执行不可变版本；图变更需回工作台出新版本并重新绑定。">
            {publishedWorkflows.length ? (
              <Select value={suiteWorkflow} onChange={(event) => setSuiteWorkflow(event.target.value)}>
                <option value="">选择发布流程</option>
                {publishedWorkflows.map((workflow) => <option key={workflow.published_ref} value={workflow.published_ref}>{workflow.name || workflow.wid} · {workflow.published_ref}</option>)}
              </Select>
            ) : <input className="input mono" value={suiteWorkflow} onChange={(event) => setSuiteWorkflow(event.target.value)} placeholder="w-xxxxxxxx@0" />}
          </Field>
        )}
        <Field label="标签" hint="用逗号或空格分隔。"><input className="input" value={suiteTags} onChange={(event) => setSuiteTags(event.target.value)} placeholder="smoke, auth" /></Field>
        <Field label="套件环境覆盖" hint="优先级高于项目 env；留空沿用。"><EnvEditor value={suiteEnv} onChange={setSuiteEnv} /></Field>
      </Modal>

      <ConfirmDialog
        open={!!deletingSuite}
        title="删除套件"
        message={<>确定删除套件「{deletingSuite?.name}」？历史运行不会被删除。</>}
        confirmLabel="删除套件"
        danger
        busy={deleteSuiteMutation.isPending}
        onClose={() => setDeletingSuite(null)}
        onConfirm={() => deletingSuite && deleteSuiteMutation.mutate(deletingSuite.suite_id)}
      />
      <ConfirmDialog
        open={archiveProjectOpen}
        title={archiveProjectNext ? '归档项目' : '取消归档项目'}
        message={archiveProjectNext ? <>确定归档项目「{project.name}」？<br />只归档，不删历史运行、报告与套件配置；默认列表将不再显示。</> : <>确定取消归档项目「{project.name}」？项目会重新出现在默认列表。</>}
        confirmLabel={archiveProjectNext ? '归档项目' : '取消归档'}
        confirmTestId="confirm-archive"
        danger={archiveProjectNext}
        busy={archiveProjectMutation.isPending}
        onClose={() => setArchiveProjectOpen(false)}
        onConfirm={() => archiveProjectMutation.mutate(archiveProjectNext)}
      />
    </div>
  )
}
