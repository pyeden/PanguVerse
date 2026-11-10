import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, studioApi } from '../shared/api'
import { href, navigate, useRoute, withQuery } from '../shared/router'
import { relativeTime } from '../shared/utils'
import type { Workflow } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  Modal,
  PageHeader,
  SearchInput,
  SectionHeader,
  Select,
  useToast,
} from '../components/ui'

type StateFilter = 'all' | 'draft' | 'review' | 'published'

const STATE_META: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }> = {
  draft: { label: '草稿', tone: 'neutral' },
  review: { label: '待审', tone: 'warn' },
  approved: { label: '已批准', tone: 'warn' },
  published: { label: '已发布', tone: 'ok' },
  rejected: { label: '已拒绝', tone: 'bad' },
  deprecated: { label: '已归档', tone: 'neutral' },
}

function stateOf(workflow: Workflow): string {
  return String(workflow.state || 'draft')
}

function stateMeta(workflow: Workflow) {
  return STATE_META[stateOf(workflow)] || { label: stateOf(workflow), tone: 'neutral' as const }
}

function isPublished(workflow: Workflow): boolean {
  return stateOf(workflow) === 'published' || !!workflow.published_ref
}

function nodeCountOf(workflow: Workflow): number {
  return Number(workflow.node_count ?? workflow.graph?.nodes?.length ?? workflow.nodes?.length ?? 0)
}

function actionError(error: unknown, action: string): { message: string; unavailable: boolean } {
  if (error instanceof ApiError && error.status === 404) {
    return { message: `${action}接口尚未就绪（404）；后端升级后即可使用`, unavailable: true }
  }
  return { message: error instanceof Error ? error.message : `${action}失败`, unavailable: false }
}

export function FlowsPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const includeArchived = route.query.get('include_archived') === '1'
  const draftsQuery = useQuery({
    queryKey: ['studio-drafts-list', includeArchived],
    queryFn: () => studioApi.listDrafts(includeArchived),
    staleTime: 5_000,
  })
  const [search, setSearch] = useState('')
  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const [renameTarget, setRenameTarget] = useState<Workflow | null>(null)
  const [renameName, setRenameName] = useState('')
  const [renameError, setRenameError] = useState('')
  const [archiveTarget, setArchiveTarget] = useState<{ workflow: Workflow; archived: boolean } | null>(null)

  const workflows = draftsQuery.data || []

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    return workflows
      .filter((workflow) => {
        if (stateFilter !== 'all') {
          if (stateFilter === 'published') return isPublished(workflow)
          return stateOf(workflow) === stateFilter
        }
        return true
      })
      .filter((workflow) => {
        if (!query) return true
        return `${workflow.name || ''} ${workflow.intent || ''} ${workflow.wid}`.toLowerCase().includes(query)
      })
      .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
  }, [search, stateFilter, workflows])

  const refreshLists = () => {
    void queryClient.invalidateQueries({ queryKey: ['studio-drafts-list'] })
    void queryClient.invalidateQueries({ queryKey: ['studio-drafts'] })
    void queryClient.invalidateQueries({ queryKey: ['overview-drafts'] })
  }

  const renameMutation = useMutation({
    mutationFn: ({ wid, name }: { wid: string; name: string }) => studioApi.renameWorkflow(wid, name),
    onSuccess: (result, variables) => {
      refreshLists()
      setRenameTarget(null)
      setRenameError('')
      push(`任务已重命名为「${result.name || variables.name}」`, 'ok')
    },
    onError: (error) => {
      const info = actionError(error, '重命名')
      push(info.message, info.unavailable ? 'warn' : 'error')
    },
  })

  const archiveMutation = useMutation({
    mutationFn: ({ workflow, archived }: { workflow: Workflow; archived: boolean }) => studioApi.archiveWorkflow(workflow.wid, archived),
    onSuccess: (_result, variables) => {
      refreshLists()
      setArchiveTarget(null)
      push(variables.archived ? '任务已归档；历史运行与报告保留' : '任务已取消归档', 'ok')
    },
    onError: (error) => {
      const info = actionError(error, '归档')
      push(info.message, info.unavailable ? 'warn' : 'error')
    },
  })

  const openRename = (workflow: Workflow) => {
    setRenameTarget(workflow)
    setRenameName(workflow.name || '')
    setRenameError('')
  }

  const submitRename = () => {
    if (!renameTarget) return
    const name = renameName.trim()
    if (!name) {
      setRenameError('任务名称不能为空')
      return
    }
    if (name.length > 200) {
      setRenameError('任务名称不能超过 200 字')
      return
    }
    setRenameError('')
    renameMutation.mutate({ wid: renameTarget.wid, name })
  }

  const toggleArchived = () => {
    const next = new URLSearchParams(route.query)
    if (includeArchived) next.delete('include_archived')
    else next.set('include_archived', '1')
    navigate(withQuery('/tasks', next), { replace: true })
  }

  return (
    <div className="page tasks-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 任务 · 管理</>}
        title="任务"
        description="任务由一句话生成，可搜索、筛选、重命名与归档；打开画布继续编辑任务图与版本。"
        actions={
          <>
            <Button variant="ghost" icon="settings" onClick={() => navigate('/tasks/config')}>运行配置</Button>
            <Button variant="primary" icon="spark" onClick={() => navigate('/next/console')}>新建任务</Button>
          </>
        }
      />

      <SectionHeader
        icon="tasks"
        title="任务列表"
        description={`${filtered.length} / ${workflows.length} 个任务 · 草稿 / 待审 / 已发布`}
        actions={<Button size="sm" variant="ghost" icon="refresh" onClick={() => draftsQuery.refetch()}>刷新</Button>}
      />

      <div className="catalog-toolbar panel">
        <SearchInput value={search} onChange={setSearch} placeholder="搜索任务名称、意图或 wid" />
        <Select value={stateFilter} onChange={(event) => setStateFilter(event.target.value as StateFilter)} aria-label="按状态筛选">
          <option value="all">全部状态</option>
          <option value="draft">草稿</option>
          <option value="review">待审</option>
          <option value="published">已发布</option>
        </Select>
        <span className="toolbar-spacer" />
        <button
          type="button"
          className={`archive-toggle ${includeArchived ? 'is-on' : ''}`}
          onClick={toggleArchived}
          aria-pressed={includeArchived}
          data-testid="toggle-archived"
        >
          <span className="toggle-track"><span /></span>
          显示已归档
        </button>
        <span className="toolbar-count">{filtered.length} / {workflows.length}</span>
      </div>

      {draftsQuery.isLoading ? <Loading label="正在读取任务…" /> : null}
      {draftsQuery.error ? <ErrorState error={draftsQuery.error} onRetry={() => draftsQuery.refetch()} title="任务读取失败" /> : null}

      {!draftsQuery.isLoading && !draftsQuery.error && !filtered.length ? (
        <EmptyState
          icon="tasks"
          title={workflows.length ? '没有匹配的任务' : '还没有任务'}
          actions={<Button size="sm" variant="primary" icon="spark" onClick={() => navigate('/next/console')}>新建任务</Button>}
        >
          {workflows.length ? '换个关键词或状态筛选。' : '从一句话开始，AI 会拆解成可审阅任务草稿。'}
        </EmptyState>
      ) : null}

      {filtered.length ? (
        <div className="task-list">
          {filtered.map((workflow) => {
            const meta = stateMeta(workflow)
            const nodes = nodeCountOf(workflow)
            const rev = workflow.current_rev ?? workflow.rev ?? 0
            const archived = !!workflow.archived_at
            return (
              <article className={`task-card workflow-card ${archived ? 'is-archived' : ''}`} key={workflow.wid} data-testid={`workflow-row-${workflow.wid}`}>
                <span className={`task-icon tone-${meta.tone === 'ok' ? 'cyan' : meta.tone === 'warn' ? 'warn' : 'neutral'}`}>
                  <Icon name="flow" size={18} />
                </span>
                <div className="task-copy">
                  <div className="task-title-row">
                    <a className="task-name-link" href={href(`/studio/${workflow.wid}`)}>{workflow.name || workflow.wid}</a>
                    <Badge tone={meta.tone}>{meta.label}</Badge>
                    {archived ? <Badge tone="warn">已归档</Badge> : null}
                  </div>
                  <p className="task-target">{workflow.intent || '无意图文本'}</p>
                  <div className="task-meta">
                    <span className="mono">{workflow.wid}</span>
                    <span>·</span>
                    <span>{nodes} 个节点</span>
                    <span>·</span>
                    <span>rev {rev}</span>
                    <span>·</span>
                    <span>更新于 {relativeTime(workflow.updated_at)}</span>
                  </div>
                </div>
                <div className="task-actions">
                  <a className="btn btn-ghost btn-sm" href={href(`/studio/${workflow.wid}`)}>
                    <Icon name="flow" size={15} /><span>打开</span>
                  </a>
                  <Button size="sm" variant="ghost" icon="edit" onClick={() => openRename(workflow)} data-testid={`rename-workflow-${workflow.wid}`}>重命名</Button>
                  <Button
                    size="sm"
                    variant={archived ? 'soft' : 'ghost'}
                    icon={archived ? 'refresh' : 'archive'}
                    onClick={() => setArchiveTarget({ workflow, archived: !archived })}
                    data-testid={`archive-workflow-${workflow.wid}`}
                  >
                    {archived ? '取消归档' : '归档'}
                  </Button>
                </div>
              </article>
            )
          })}
        </div>
      ) : null}

      <Modal
        open={!!renameTarget}
        onClose={() => setRenameTarget(null)}
        title="重命名任务"
        subtitle={renameTarget ? `${renameTarget.wid} · 只改名称，不改图与版本` : ''}
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenameTarget(null)} disabled={renameMutation.isPending}>取消</Button>
            <Button variant="primary" loading={renameMutation.isPending} onClick={submitRename} data-testid="confirm-rename">保存名称</Button>
          </>
        }
      >
        <Field label="任务名称" htmlFor="workflow-rename" error={renameError} hint="最多 200 字；保存后列表立即更新。">
          <input
            id="workflow-rename"
            className="input"
            value={renameName}
            maxLength={200}
            onChange={(event) => setRenameName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') submitRename()
            }}
            placeholder="例如 竞品价格日报"
            autoFocus
          />
        </Field>
      </Modal>

      <ConfirmDialog
        open={!!archiveTarget}
        title={archiveTarget?.archived ? '归档任务' : '取消归档任务'}
        message={archiveTarget?.archived
          ? <>确定归档任务「{archiveTarget?.workflow.name || archiveTarget?.workflow.wid}」？<br />只归档，不删除图、配置与历史运行；归档后默认列表不再显示。</>
          : <>确定取消归档任务「{archiveTarget?.workflow.name || archiveTarget?.workflow.wid}」？取消后任务会重新出现在默认列表。</>}
        confirmLabel={archiveTarget?.archived ? '归档任务' : '取消归档'}
        confirmTestId="confirm-archive-workflow"
        danger={!!archiveTarget?.archived}
        busy={archiveMutation.isPending}
        onClose={() => setArchiveTarget(null)}
        onConfirm={() => archiveTarget && archiveMutation.mutate(archiveTarget)}
      />
    </div>
  )
}
