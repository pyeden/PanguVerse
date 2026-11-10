import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../shared/api'
import { navigate, useRoute, withQuery } from '../shared/router'
import { formatTime, rateOf, relativeTime, statusOf, statusLabel, classForStatus } from '../shared/utils'
import type { AnyRecord, Project } from '../shared/types'
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
  StatusBadge,
  useToast,
} from '../components/ui'

export function ProjectsPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const includeArchived = route.query.get('include_archived') === '1'
  const [archiveTarget, setArchiveTarget] = useState<Project | null>(null)
  const [archiveNext, setArchiveNext] = useState(true)
  const [search, setSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [env, setEnv] = useState<AnyRecord>({})
  const projectsQuery = useQuery({ queryKey: ['projects', includeArchived], queryFn: () => api.projects(includeArchived) })
  const projects = projectsQuery.data || []

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return projects
    return projects.filter((project) => `${project.name} ${project.base_url || ''} ${project.project_id}`.toLowerCase().includes(query))
  }, [projects, search])

  const createMutation = useMutation({
    mutationFn: () => api.createProject({ name: name.trim(), base_url: baseUrl.trim(), ...(Object.keys(env).length ? { env } : {}) }),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setCreateOpen(false)
      setName('')
      setBaseUrl('')
      setEnv({})
      push(`项目「${project.name}」已创建`, 'ok')
      navigate(`/p/${project.project_id}/settings`)
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const archiveMutation = useMutation({
    mutationFn: ({ project, archived }: { project: Project; archived: boolean }) => api.archiveProject(project.project_id, archived),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setArchiveTarget(null)
      push(variables.archived ? '项目已归档；历史运行与报告保留' : '项目已取消归档', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const totalRuns = projects.reduce((sum, project) => sum + (project.last_run ? 1 : 0), 0)
  const aggregate = projects.reduce(
    (acc, project) => {
      if (project.pass_rate) {
        acc.passed += project.pass_rate.passed || 0
        acc.total += project.pass_rate.total || 0
      }
      return acc
    },
    { passed: 0, total: 0 },
  )
  const aggregateRate = rateOf(aggregate.passed, aggregate.total)
  const archivedCount = projects.filter((project) => !!project.archived_at).length

  return (
    <div className="page projects-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 域</>}
        title="项目"
        description="每个项目是一组套件、共享环境与运行历史的归属。把被测系统放在这里，再绑定已发布任务。"
        actions={
          <>
            <Button variant="ghost" icon="history" onClick={() => navigate('/history')}>跨项目历史</Button>
            <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>新建项目</Button>
          </>
        }
      />

      <div className="overview-strip">
        <div className="overview-stat"><span>项目</span><strong>{projects.length}</strong><small>共 {projects.filter((project) => project.suite_count).length} 个已绑定套件</small></div>
        <div className="overview-stat"><span>累计通过率</span><strong className={aggregateRate !== null && aggregateRate < 80 ? 'bad' : 'ok'}>{aggregateRate === null ? '—' : `${aggregateRate}%`}</strong><small>{aggregate.passed}/{aggregate.total} 用例</small></div>
        <div className="overview-stat"><span>最近执行</span><strong>{totalRuns}</strong><small>{projects[0]?.last_run ? relativeTime(projects[0].last_run.at || projects[0].last_run.started_at) : '暂无运行'}</small></div>
        <div className="overview-stat signal"><span>AI 提议</span><strong>可追溯</strong><small>审阅门禁 · 锁定优先</small></div>
      </div>

      <div className="toolbar-row">
        <SearchInput value={search} onChange={setSearch} placeholder="搜索项目名称、站点或 ID" />
        <div className="toolbar-meta">
          <button type="button" className={`archive-toggle ${includeArchived ? 'is-on' : ''}`} onClick={() => {
            const next = new URLSearchParams(route.query)
            if (includeArchived) next.delete('include_archived')
            else next.set('include_archived', '1')
            navigate(withQuery('/projects', next), { replace: true })
          }} aria-pressed={includeArchived} data-testid="toggle-archived"><span className="toggle-track"><span /></span>显示已归档</button>
          <Badge tone="neutral">{filtered.length} / {projects.length} 个项目</Badge>
          {includeArchived ? <span className="muted">{archivedCount} 个已归档</span> : <span className="muted">按最近更新排序</span>}
        </div>
      </div>

      {projectsQuery.isLoading ? <Loading label="正在读取项目…" /> : null}
      {projectsQuery.error ? <ErrorState error={projectsQuery.error} onRetry={() => projectsQuery.refetch()} /> : null}

      {!projectsQuery.isLoading && !projectsQuery.error && !projects.length ? (
        <EmptyState
          icon="projects"
          title="先建一个项目"
          actions={<><Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>新建项目</Button><Button variant="ghost" onClick={() => navigate('/studio/new')}>先建任务</Button></>}
        >
          项目把站点地址、环境与套件放在一起；之后每次运行、报告和审计都会自动归属到这里。
        </EmptyState>
      ) : null}

      {filtered.length ? (
        <div className="project-grid">
          {filtered.map((project) => <ProjectCard key={project.project_id} project={project} onArchive={(archived) => { setArchiveTarget(project); setArchiveNext(archived) }} />)}
        </div>
      ) : projects.length ? (
        <EmptyState icon="search" title="没有匹配的项目" actions={<Button size="sm" onClick={() => setSearch('')}>清空搜索</Button>}>
          换个关键词，或检查站点地址。
        </EmptyState>
      ) : null}

      <SectionHeader
        title="项目与任务的关系"
        description="项目负责“在哪里跑”，任务画布负责“跑什么”。已发布任务绑定到项目后才会进入正式执行。"
        icon="git"
        actions={<Button size="sm" variant="ghost" icon="arrow" onClick={() => navigate('/studio/new')}>打开工作台</Button>}
      />
      <div className="relation-grid">
        <div className="relation-card">
          <span className="relation-index">01</span>
          <Icon name="spark" size={21} />
          <strong>工作台产出任务</strong>
          <p>意图 → 图 → 调试 → 审阅 → 不可变发布，得到 <code>wid@rev</code>。</p>
        </div>
        <div className="relation-arrow"><Icon name="arrow" size={18} /></div>
        <div className="relation-card">
          <span className="relation-index">02</span>
          <Icon name="link" size={21} />
          <strong>绑定项目环境</strong>
          <p>发布后的任务绑定项目 base_url 与已配置 env，禁止临时 URL 覆盖。</p>
        </div>
        <div className="relation-arrow"><Icon name="arrow" size={18} /></div>
        <div className="relation-card">
          <span className="relation-index">03</span>
          <Icon name="report" size={21} />
          <strong>运行与回溯</strong>
          <p>执行、历史、报告共用同一条审计链，随时回到发布版本与人工裁决点。</p>
        </div>
      </div>

      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="新建项目"
        subtitle="保存站点地址与默认环境；套件可在创建后继续添加。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)} disabled={createMutation.isPending}>取消</Button>
            <Button variant="primary" loading={createMutation.isPending} onClick={() => {
              if (!name.trim()) { push('请填写项目名称', 'error'); return }
              createMutation.mutate()
            }}>创建项目</Button>
          </>
        }
      >
        <Field label="项目名称" htmlFor="new-project-name" hint="例如 coding-log-web">
          <input id="new-project-name" className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="被测系统名称" maxLength={80} autoFocus />
        </Field>
        <Field label="站点地址 base_url" hint="可留空，之后在设置里补充。">
          <input className="input mono" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com" spellCheck={false} />
        </Field>
        <Field label="项目默认环境变量" hint="密钥以 *** 保存并回显；套件级 env 可以覆盖。">
          <EnvEditor value={env} onChange={setEnv} />
        </Field>
      </Modal>

      <ConfirmDialog
        open={!!archiveTarget}
        title={archiveNext ? '归档项目' : '取消归档项目'}
        message={archiveNext ? <>确定归档项目「{archiveTarget?.name}」？<br />只归档，不删历史运行、报告与套件配置；归档后默认列表不显示。</> : <>确定取消归档项目「{archiveTarget?.name}」？取消后会重新出现在默认列表。</>}
        confirmLabel={archiveNext ? '归档项目' : '取消归档'}
        confirmTestId="confirm-archive"
        danger={archiveNext}
        busy={archiveMutation.isPending}
        onClose={() => setArchiveTarget(null)}
        onConfirm={() => archiveTarget && archiveMutation.mutate({ project: archiveTarget, archived: archiveNext })}
      />
    </div>
  )
}

function ProjectCard({ project, onArchive }: { project: Project; onArchive: (archived: boolean) => void }) {
  const status = project.last_run ? statusOf(project.last_run) : 'none'
  const rate = project.pass_rate ? rateOf(project.pass_rate.passed, project.pass_rate.total) : null
  const lastAt = project.last_run?.at || project.last_run?.finished_at || project.last_run?.started_at
  const suites = project.suite_count ?? project.suites?.length ?? 0
  return (
    <article className={`project-card ${project.archived_at ? 'is-archived' : ''}`} data-testid={`project-card-${project.project_id}`}>
      <a className="project-card-link" href={`#/p/${project.project_id}/overview`}>
        <div className="project-card-top">
          <div className={`project-sigil tone-${classForStatus(status)}`}>
            <Icon name="projects" size={20} />
          </div>
          <div className="project-card-title">
            <strong>{project.name}</strong>
            <span className="mono">{project.base_url || '未配置站点地址'}</span>
          </div>
          <div className="project-card-statuses">
            {project.archived_at ? <Badge tone="warn">已归档</Badge> : null}
            <StatusBadge status={status} />
          </div>
        </div>

        <div className="project-card-stats">
          <div><strong>{rate === null ? '—' : `${rate}%`}</strong><span>累计通过率</span></div>
          <div><strong>{suites}</strong><span>套件</span></div>
          <div><strong>{project.last_run ? relativeTime(lastAt) : '—'}</strong><span>上次运行</span></div>
        </div>

        <div className="project-health">
          <div className="project-health-head">
            <span>健康度</span>
            <span>{project.last_run ? statusLabel(status) : '等待首次运行'}</span>
          </div>
          <div className={`progress ${rate !== null && rate < 80 ? 'bad' : 'ok'}`}>
            <span style={{ width: `${rate ?? 0}%` }} />
          </div>
        </div>

        <div className="project-card-foot">
          <span className="project-card-id mono">{project.project_id}</span>
          <span className="project-card-open">打开概览 <Icon name="arrow" size={14} /></span>
        </div>
      </a>
      <div className="project-card-admin">
        <span>{project.archived_at ? `归档于 ${relativeTime(project.archived_at)}` : '归档只影响列表，不删历史'}</span>
        <Button size="sm" variant="ghost" icon={project.archived_at ? 'refresh' : 'archive'} onClick={() => onArchive(!project.archived_at)} data-testid={`archive-project-${project.project_id}`}>{project.archived_at ? '取消归档' : '归档'}</Button>
      </div>
    </article>
  )
}
