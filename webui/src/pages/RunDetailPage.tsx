import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../shared/api'
import { href, navigate, useRoute, withQuery } from '../shared/router'
import {
  errorText,
  formatBytes,
  formatDuration,
  formatTime,
  pretty,
  rateOf,
  relativeTime,
  runCounts,
  shortId,
  statusLabel,
  statusOf,
  classForStatus,
} from '../shared/utils'
import type { AnyRecord, Artifact } from '../shared/types'
import { artifactHint, splitArtifacts } from '../shared/artifacts'
import { ArtifactPreviewModal } from '../components/ArtifactPreview'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  JsonBlock,
  JsonTree,
  Kpi,
  Loading,
  Markdown,
  Modal,
  Progress,
  SearchInput,
  SectionHeader,
  SourceBadge,
  StatusBadge,
  useToast,
} from '../components/ui'

export function RunDetailPage() {
  const route = useRoute()
  const pid = route.parts[0] === 'p' ? route.parts[1] : route.query.get('project') || ''
  const rid = route.parts[0] === 'p' ? route.parts[3] : route.parts[1]
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [caseSearch, setCaseSearch] = useState('')
  const [caseFilter, setCaseFilter] = useState('all')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [rawOpen, setRawOpen] = useState(false)
  const [saveOpen, setSaveOpen] = useState(false)
  const [taskName, setTaskName] = useState('')
  const [rerunOpen, setRerunOpen] = useState(false)
  const [previewArtifactId, setPreviewArtifactId] = useState('')

  const runQuery = useQuery({ queryKey: ['run', rid], queryFn: () => api.run(rid), enabled: !!rid })
  const insightQuery = useQuery({ queryKey: ['insight', rid], queryFn: () => api.insight(rid), enabled: !!rid })
  const projectQuery = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid), enabled: !!pid })
  const runArtifactsQuery = useQuery({
    queryKey: ['run-artifacts', rid],
    queryFn: () => api.artifacts(new URLSearchParams({ run: rid, limit: '100' })),
    enabled: !!rid,
  })
  // 阶段明细由执行记录提供（GET /api/executions/{run_id} 已合并内存 job.phases）；
  // 旧报告可能没有 phases，此时回退到 report.phases，不臆造接口。
  const executionQuery = useQuery({
    queryKey: ['run-execution', rid],
    queryFn: () => api.execution(rid),
    enabled: !!rid,
    retry: false,
  })

  const generateInsight = useMutation({
    mutationFn: (refresh: boolean) => api.generateInsight(rid, refresh),
    onSuccess: (data) => {
      queryClient.setQueryData(['insight', rid], data)
      push('AI 总结已生成', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const saveTask = useMutation({
    mutationFn: () => api.saveAsTask(rid, taskName.trim() ? { name: taskName.trim() } : {}),
    onSuccess: (task) => {
      setSaveOpen(false)
      push(`已保存为任务：${task.name}`, 'ok')
      navigate(`/tasks?focus=${task.task_id}`)
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const rerun = useMutation({
    mutationFn: () => api.rerun(rid, {}),
    onSuccess: (result) => {
      setRerunOpen(false)
      push('重跑已提交', 'ok')
      navigate(`/executions?job=${result.job_id}`)
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  if (runQuery.isLoading) return <Loading label="正在读取运行报告…" />
  if (runQuery.error || !runQuery.data) return <ErrorState error={runQuery.error} onRetry={() => runQuery.refetch()} title="运行报告读取失败" />
  const report = runQuery.data.report || {}
  const plan = runQuery.data.plan || report.plan || {}
  const summary = report.summary || {}
  const cases: AnyRecord[] = Array.isArray(report.cases) ? report.cases : []
  const status = statusOf({ run_id: rid, summary, conclusion: report.conclusion })
  const rate = rateOf(summary.passed, summary.total)
  const insight = insightQuery.data
  const project = projectQuery.data
  const suiteName = report.meta?.suite || plan.meta?.suite || report.meta?.suite_name || project?.suites?.find((suite) => suite.suite_id === report.meta?.suite_id)?.name || '未归类运行'
  const workflowRef = report.workflow_ref || report.meta?.workflow_ref || plan.workflow_ref || report.meta?.published_ref || ''
  const audit: AnyRecord[] = Array.isArray(report.audit_tail) ? report.audit_tail : Array.isArray(plan.audit_tail) ? plan.audit_tail : []
  const executionPhases: { name: string; status: string; detail?: string }[] = executionQuery.data?.phases?.length
    ? executionQuery.data.phases
    : Array.isArray(report.phases)
      ? (report.phases as { name: string; status: string; detail?: string }[])
      : []
  const warningPhases = executionPhases.filter((phase) => phase.status === 'warning')
  const artifactPhase = executionPhases.find((phase) => phase.name === 'artifacts')
  const artifactWarning = artifactPhase?.status === 'warning'
  // 后端 warning detail 可能仍以「本次运行没有产出物」开头；收集失败不是「没有声明产出」，
  // 报告页统一去掉这个误导前缀，只保留真正的失败原因。
  const cleanPhaseDetail = (detail?: string) => (detail || '').replace(/^本次运行没有产出物[；;]\s*/, '')
  const runArtifacts: Artifact[] = runArtifactsQuery.data?.items || []
  const { reports: reportArtifacts, deliverables: taskOutputs } = splitArtifacts(runArtifacts)
  const artifactsSettled = !runArtifactsQuery.isLoading && !runArtifactsQuery.error

  const renderArtifactRow = (artifact: Artifact, kind: 'report' | 'deliverable') => (
    <div
      className={`run-artifact-row is-${kind}`}
      key={artifact.artifact_id}
      data-testid={`run-artifact-item-${artifact.artifact_id}`}
      data-run-id={rid}
      data-artifact-kind={kind}
    >
      <button
        type="button"
        className="run-artifact-main"
        onClick={() => setPreviewArtifactId(artifact.artifact_id)}
        aria-label={`预览 ${artifact.name}`}
        data-testid={`run-artifact-preview-${artifact.artifact_id}`}
      >
        <span className="run-artifact-type"><Icon name={artifact.type === 'image' ? 'eye' : artifact.type === 'markdown' ? 'report' : 'file'} size={15} /></span>
        <span className="run-artifact-copy">
          <strong>{artifact.name}</strong>
          <small><Badge tone="neutral">{artifact.type}</Badge> {formatBytes(artifact.bytes)} · {artifact.node_id || 'unknown'} · {formatTime(artifact.created_at)}</small>
          <span className="run-artifact-hint">{artifactHint(artifact.name)}</span>
        </span>
        <span
          className="run-artifact-kind"
          data-testid={kind === 'report' ? 'artifact-kind-report' : 'artifact-kind-deliverable'}
        >
          <Badge tone={kind === 'report' ? 'ai' : 'cyan'}>{kind === 'report' ? '执行报告' : '任务产出'}</Badge>
        </span>
        <Icon name="eye" size={13} />
      </button>
      <a
        className="btn btn-ghost btn-sm run-artifact-download"
        href={api.artifactDownloadUrl(artifact.artifact_id)}
        download={artifact.name}
        aria-label={`下载 ${artifact.name}`}
        data-testid={`run-artifact-download-${artifact.artifact_id}`}
      >
        <Icon name="download" size={14} /><span>下载</span>
      </a>
    </div>
  )

  const caseQuery = caseSearch.trim().toLowerCase()
  const filteredCases = cases.filter((item) => {
    if (caseFilter !== 'all' && item.status !== caseFilter) return false
    if (caseQuery && !`${item.name || ''} ${item.case_id || ''}`.toLowerCase().includes(caseQuery)) return false
    return true
  })

  return (
    <div className="page run-detail-page">
      <div className="report-hero">
        <div className="report-hero-main">
          <div className="report-back">
            {pid ? <a href={href(`/p/${pid}/history`)}><Icon name="arrow" size={14} /> 项目历史</a> : <a href={href('/history')}><Icon name="arrow" size={14} /> 全部历史</a>}
            <span>/</span>
            <span className="mono">{rid}</span>
          </div>
          <div className="report-title-row">
            <h1>{suiteName}</h1>
            <StatusBadge status={status} />
            {report.conclusion?.verdict ? <Badge tone={report.conclusion?.success ? 'ok' : 'bad'}>{report.conclusion.verdict}</Badge> : null}
          </div>
          <div className="report-meta-line">
            <span>{project?.name || report.meta?.project_id || '未归类项目'}</span>
            <span className="meta-divider" />
            <span>{formatTime(report.started_at)}</span>
            <span className="meta-divider" />
            <span>{formatDuration(report.duration_ms)}</span>
            {workflowRef ? <><span className="meta-divider" /><span className="mono workflow-ref"><Icon name="git" size={12} />{workflowRef}</span></> : null}
          </div>
        </div>
        <div className="report-actions">
          <a className="btn btn-ghost btn-sm" href={`api/runs/${encodeURIComponent(rid)}/report.html`} target="_blank" rel="noopener noreferrer"><Icon name="external" size={15} />HTML 报告</a>
          <a className="btn btn-ghost btn-sm" href={`api/runs/${encodeURIComponent(rid)}/junit.xml`}><Icon name="download" size={15} />JUnit</a>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => setRerunOpen(true)}>重跑</Button>
          <Button size="sm" variant="primary" icon="tasks" onClick={() => { setTaskName(`重跑 ${shortId(rid)}`); setSaveOpen(true) }}>存为任务</Button>
        </div>
      </div>

      <div className="kpi-grid report-kpis">
        <Kpi label="通过率" value={rate === null ? '—' : `${rate}%`} tone={rate !== null && rate < 80 ? 'bad' : 'ok'} sub={`${summary.passed || 0}/${summary.total || 0} 用例`} icon="check" />
        <Kpi label="失败 / 异常" value={`${summary.failed || 0} / ${summary.errored || 0}`} tone={summary.failed || summary.errored ? 'bad' : 'ok'} sub={`${summary.skipped || 0} 跳过 · ${summary.healed_cases || 0} 自愈`} icon="warning" />
        <Kpi label="HTTP 请求" value={summary.http_requests ?? '—'} sub={`${summary.steps ?? 0} 个步骤`} icon="server" />
        <Kpi label="总耗时" value={formatDuration(report.duration_ms)} sub={`结论：${report.conclusion?.verdict || '—'}`} icon="clock" />
      </div>

      {warningPhases.length ? (
        <div className="notice-inline warn-note run-warning-phases" data-testid="run-warning-phases">
          <Icon name="warning" size={15} />
          <div>
            <strong>执行阶段有 {warningPhases.length} 项警告</strong>
            {warningPhases.map((phase, index) => (
              <p key={`${phase.name}-${index}`}><span className="mono">{phase.name}</span> · {cleanPhaseDetail(phase.detail) || statusLabel(phase.status)}</p>
            ))}
          </div>
        </div>
      ) : null}

      <div className="report-grid">
        <div className="report-main">
          <section className="panel insight-panel">
            <SectionHeader
              title="AI 总结"
              description="数字来自报告本体；模型只负责措辞。"
              icon="spark"
              actions={
                <>
                  {insight?.available ? <SourceBadge source={insight.source} degradedReason={insight.degraded_reason} /> : null}
                  <Button size="sm" variant="ghost" icon="refresh" loading={generateInsight.isPending} onClick={() => generateInsight.mutate(!!insight?.available)}>
                    {insight?.available ? '重新生成' : '生成总结'}
                  </Button>
                </>
              }
            />
            {insightQuery.isLoading ? <Loading label="正在读取总结缓存…" /> : null}
            {!insightQuery.isLoading && !insight?.available ? (
              <div className="insight-empty">
                <span className="insight-mark"><Icon name="spark" size={22} /></span>
                <div><strong>还没有生成总结</strong><p>不会自动调用模型；点击「生成总结」后写入缓存。没有 LLM 密钥时会回退离线规则，并明确标注来源。</p></div>
              </div>
            ) : null}
            {insight?.available ? (
              <>
                {insight.degraded_reason ? <div className="insight-degraded"><Icon name="warning" size={14} />{insight.degraded_reason}</div> : null}
                <Markdown value={insight.summary_md} />
                {Array.isArray(insight.corrections) && insight.corrections.length ? <div className="insight-corrections"><Badge tone="neutral">确定性校正 {insight.corrections.length} 项</Badge></div> : null}
              </>
            ) : null}
          </section>

          <section className="panel run-artifacts-panel" data-testid="run-artifacts-panel">
            <SectionHeader
              title="产出物"
              description="执行报告回答「怎么跑的」，任务产出回答「跑出了什么」；均可就地预览或下载。"
              icon="archive"
              actions={<a className="inline-link" data-testid="run-artifacts-link" href={href(withQuery('/artifacts', { run: rid }))}>全部产出物 <Icon name="arrow" size={12} /></a>}
            />
            {runArtifactsQuery.isLoading ? <Loading label="正在读取产出物…" /> : null}
            {runArtifactsQuery.error ? (
              <div className="notice-inline warn-note">
                <Icon name="warning" size={14} />
                <div><strong>产出物接口未就绪或读取失败</strong><p>接口上线后，本 run 的报告与任务产出会自动列在这里。</p></div>
              </div>
            ) : null}
            {artifactWarning ? (
              <div className="notice-inline warn-note run-artifact-warning" data-testid="run-artifact-warning">
                <Icon name="warning" size={14} />
                <div><strong>产物收集失败（见警告明细）</strong><p>{cleanPhaseDetail(artifactPhase?.detail) || '产物收集阶段返回 warning，运行结论不受影响。'}</p></div>
              </div>
            ) : null}

            <div className="run-artifact-groups">
              <div className="run-artifact-group is-report" data-testid="run-execution-artifacts">
                <div className="run-artifact-group-head">
                  <div>
                    <strong><Icon name="report" size={14} />执行报告</strong>
                    <p>怎么跑的 · 运行报告、计划与 JUnit 结果</p>
                  </div>
                  <div className="run-artifact-group-actions">
                    <Badge tone="ai">{reportArtifacts.length} 项</Badge>
                    <a
                      className="inline-link"
                      href="#run-cases"
                      data-testid="run-report-steps-entry"
                      onClick={(event) => {
                        event.preventDefault()
                        document.getElementById('run-cases')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                      }}
                    >节点/步骤明细 <Icon name="arrow" size={12} /></a>
                  </div>
                </div>
                {artifactsSettled && !reportArtifacts.length ? (
                  <div className="run-artifact-empty" data-testid="run-execution-artifacts-empty">
                    <Icon name="report" size={18} />
                    <span data-testid="run-artifact-empty">无执行报告 · 正常情况下 report.html / report.json / junit.xml / plan.json 会列在这里</span>
                  </div>
                ) : null}
                {reportArtifacts.length ? (
                  <div className="run-artifact-list">
                    {reportArtifacts.map((artifact) => renderArtifactRow(artifact, 'report'))}
                  </div>
                ) : null}
              </div>

              <div className="run-artifact-group is-deliverable" data-testid="run-task-outputs">
                <div className="run-artifact-group-head">
                  <div>
                    <strong><Icon name="archive" size={14} />任务产出</strong>
                    <p>跑出了什么 · 本次流程声明的交付文件</p>
                  </div>
                  <Badge tone="cyan">{taskOutputs.length} 项</Badge>
                </div>
                {artifactsSettled && !taskOutputs.length ? (
                  <div className="run-artifact-empty" data-testid="run-task-outputs-empty">
                    <Icon name="archive" size={18} />
                    <span>本流程没有声明任务产出</span>
                  </div>
                ) : null}
                {taskOutputs.length ? (
                  <div className="run-artifact-list">
                    {taskOutputs.map((artifact) => renderArtifactRow(artifact, 'deliverable'))}
                  </div>
                ) : null}
              </div>
            </div>
          </section>

          <section className="panel cases-panel" id="run-cases">
            <SectionHeader
              title={`用例明细`}
              description={`${cases.length} 个用例 · ${summary.steps || 0} 个步骤`}
              icon="terminal"
              actions={<Badge tone="neutral">{filteredCases.length} / {cases.length}</Badge>}
            />
            <div className="case-toolbar">
              <SearchInput value={caseSearch} onChange={setCaseSearch} placeholder="搜索用例名称或 case_id" />
              <div className="segmented">
                {[
                  ['all', '全部'],
                  ['passed', '通过'],
                  ['failed', '失败'],
                  ['error', '异常'],
                ].map(([value, label]) => (
                  <button key={value} type="button" className={caseFilter === value ? 'is-active' : ''} onClick={() => setCaseFilter(value)}>{label}</button>
                ))}
              </div>
            </div>
            {!cases.length ? <EmptyState icon="report" title="报告中没有用例">可能是一次规划失败或空场景。</EmptyState> : (
              <div className="case-list">
                {filteredCases.map((item, index) => {
                  const key = `${item.case_id || item.name}-${index}`
                  const isOpen = !!expanded[key]
                  return (
                    <article className={`case-card ${isOpen ? 'is-open' : ''}`} key={key}>
                      <button type="button" className="case-head" onClick={() => setExpanded((current) => ({ ...current, [key]: !isOpen }))} aria-expanded={isOpen}>
                        <StatusBadge status={item.status} />
                        <span className="case-name"><strong>{item.name || item.case_id}</strong><small className="mono">{item.case_id || 'case'}</small></span>
                        <span className="case-duration mono">{formatDuration(item.duration_ms)}</span>
                        <span className="case-step-count">{Array.isArray(item.steps) ? item.steps.length : 0} 步</span>
                        <Icon name="chevron" size={16} className="case-chevron" />
                      </button>
                      {isOpen ? (
                        <div className="case-body">
                          {Array.isArray(item.steps) ? item.steps.map((step: AnyRecord, stepIndex: number) => (
                            <details className="step-card" key={`${step.step_id || stepIndex}`}>
                              <summary>
                                <span className={`step-index tone-${classForStatus(step.status)}`}>{stepIndex + 1}</span>
                                <span className="step-name"><strong>{step.step_id || step.atom}</strong><small className="mono">{step.atom}</small></span>
                                <StatusBadge status={step.status} />
                                <span className="mono step-duration">{formatDuration(step.duration_ms)}</span>
                              </summary>
                              <div className="step-detail">
                                {step.error ? <div className="step-error"><Icon name="warning" size={14} />{errorText(step.error)}</div> : null}
                                <div className="step-io"><div><span className="io-label">inputs</span><JsonTree data={step.inputs || {}} /></div><div><span className="io-label">outputs</span><JsonTree data={step.outputs || {}} /></div></div>
                              </div>
                            </details>
                          )) : null}
                          {item.error ? <div className="step-error"><Icon name="warning" size={14} />{errorText(item.error)}</div> : null}
                        </div>
                      ) : null}
                    </article>
                  )
                })}
              </div>
            )}
          </section>
        </div>

        <aside className="report-side">
          <section className="panel">
            <SectionHeader title="运行信息" description="来自报告与 plan.json" icon="database" />
            <dl className="fact-list">
              <div><dt>Run ID</dt><dd className="mono">{rid}</dd></div>
              <div><dt>项目</dt><dd>{project?.name || report.meta?.project_id || '未归类'}</dd></div>
              <div><dt>套件</dt><dd>{suiteName}</dd></div>
              <div><dt>计划来源</dt><dd>{plan.source || report.meta?.plan_source || '—'}</dd></div>
              <div><dt>传输后端</dt><dd>{report.meta?.backend || '—'}</dd></div>
              <div><dt>触发方式</dt><dd>{report.meta?.trigger || report.meta?.mode || '手动'}</dd></div>
              <div><dt>开始</dt><dd>{formatTime(report.started_at)}</dd></div>
              <div><dt>结束</dt><dd>{formatTime(report.finished_at)}</dd></div>
              <div><dt>发布版本</dt><dd className="mono">{workflowRef || '—'}</dd></div>
            </dl>
            <Button size="sm" variant="ghost" icon="code" className="full-width" onClick={() => setRawOpen(true)}>查看原始报告</Button>
          </section>

          <section className="panel">
            <SectionHeader title="审计链" description="发布版本与人工裁决" icon="git" />
            {audit.length ? (
              <ol className="audit-list">
                {audit.map((item, index) => (
                  <li key={index}>
                    <span className="audit-dot" />
                    <div><strong>{item.action || '状态变更'}</strong><p>{item.note || item.to_state || '—'}</p><span>{formatTime(item.ts)} · {item.actor || 'system'}{item.rev ? ` · rev ${item.rev}` : ''}</span></div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="audit-empty">
                <Icon name="git" size={19} />
                <strong>{workflowRef ? '已定位发布版本' : '没有内嵌审计记录'}</strong>
                <p>{workflowRef ? `报告指向 ${workflowRef}。工作台审阅页可查看完整 audit_tail。` : '当前报告未携带 workflow_ref；仍可通过项目与执行 ID 回溯。'}</p>
                {workflowRef ? <a className="inline-link" href={href(`/studio/${workflowRef.split('@')[0]}/review`)}>打开发布版本 <Icon name="arrow" size={12} /></a> : null}
              </div>
            )}
          </section>

          <section className="panel compact-panel">
            <div className="report-integrity">
              <Icon name="shield" size={17} />
              <div><strong>报告完整性</strong><p>AI 总结数字经确定性校正；报告出口统一打码 env 敏感值。</p></div>
            </div>
          </section>
        </aside>
      </div>

      <Modal open={rawOpen} onClose={() => setRawOpen(false)} title="原始报告" subtitle="JSON 原文" size="xl" footer={<Button variant="ghost" onClick={() => setRawOpen(false)}>关闭</Button>}>
        <JsonBlock data={report} />
      </Modal>

      <Modal
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        title="存为任务"
        subtitle="会从报告复制场景/意图与真实环境值；不产生新运行。"
        size="sm"
        footer={<><Button variant="ghost" onClick={() => setSaveOpen(false)}>取消</Button><Button variant="primary" loading={saveTask.isPending} onClick={() => saveTask.mutate()}>保存任务</Button></>}
      >
        <Field label="任务名称"><input className="input" value={taskName} onChange={(event) => setTaskName(event.target.value)} autoFocus /></Field>
        {report.meta?.mode ? <p className="muted">模式：<code>{report.meta.mode}</code> · 来源执行 {shortId(rid)}</p> : null}
      </Modal>

      <Modal
        open={rerunOpen}
        onClose={() => setRerunOpen(false)}
        title="重跑这次执行"
        subtitle="会生成新的执行与报告，原记录保持不变。"
        size="sm"
        footer={<><Button variant="ghost" onClick={() => setRerunOpen(false)}>取消</Button><Button variant="primary" icon="refresh" loading={rerun.isPending} onClick={() => rerun.mutate()}>确认重跑</Button></>}
      >
        <div className="confirm-body">将用本次报告还原的配置重新提交运行。环境值从磁盘真实配置读取，不使用页面上的打码值。</div>
      </Modal>

      <ArtifactPreviewModal artifactId={previewArtifactId} onClose={() => setPreviewArtifactId('')} />
    </div>
  )
}
