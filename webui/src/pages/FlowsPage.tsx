import { useQuery } from '@tanstack/react-query'
import { studioApi } from '../shared/api'
import { href, navigate } from '../shared/router'
import { relativeTime } from '../shared/utils'
import { PageHeader, SectionHeader, Badge, Button, EmptyState, ErrorState, Loading } from '../components/ui'
import type { Workflow } from '../shared/types'

function stateTone(state?: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (state === 'approved' || state === 'published') return 'ok'
  if (state === 'review' || state === 'rejected') return 'warn'
  if (state === 'rejected') return 'bad'
  return 'neutral'
}

export function FlowsPage() {
  const draftsQuery = useQuery({ queryKey: ['studio-drafts-list'], queryFn: () => studioApi.listDrafts(), staleTime: 5_000 })
  const workflows: Workflow[] = draftsQuery.data || []

  return (
    <div className="page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 流程 · 管理</>}
        title="流程"
        description="这里管理已生成的流程；新建不再是一个重复的意图输入页，而是从首页一句话开始。"
        actions={<Button variant="primary" size="sm" icon="spark" onClick={() => navigate('/next/console')}>新建流程</Button>}
      />

      <SectionHeader
        icon="flow"
        title="流程列表"
        description={`${workflows.length} 个流程 · 草稿 / 审阅 / 已发布`}
        actions={<Button size="sm" variant="ghost" icon="refresh" onClick={() => draftsQuery.refetch()}>刷新</Button>}
      />

      {draftsQuery.isLoading ? <Loading label="正在读取流程…" /> : null}
      {draftsQuery.error ? <ErrorState error={draftsQuery.error} onRetry={() => draftsQuery.refetch()} title="流程读取失败" /> : null}
      {!draftsQuery.isLoading && !draftsQuery.error && !workflows.length ? (
        <EmptyState
          icon="flow"
          title="还没有流程"
          actions={<Button size="sm" variant="primary" icon="spark" onClick={() => navigate('/next/console')}>从首页一句话开始</Button>}
        >
          首页的 AI 控制台会先拆解意图，再生成流程草稿。
        </EmptyState>
      ) : null}

      {workflows.length ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
          {workflows.map((workflow) => {
            const nodes = workflow.graph?.nodes?.length || workflow.nodes?.length || 0
            const rev = workflow.current_rev ?? workflow.rev ?? 0
            return (
              <a
                key={workflow.wid}
                className="panel"
                href={href(`/studio/${workflow.wid}`)}
                style={{ padding: 14, textDecoration: 'none', color: 'inherit', display: 'flex', flexDirection: 'column', gap: 8 }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' }}>
                  <strong style={{ fontSize: 13 }}>{workflow.name || workflow.wid}</strong>
                  <Badge tone={stateTone(workflow.state)}>{workflow.state || 'draft'}</Badge>
                </div>
                <p className="muted" style={{ margin: 0, fontSize: 11, lineHeight: 1.45 }}>{workflow.intent || '无意图文本'}</p>
                <div className="small muted" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 'auto' }}>
                  <span className="mono">{workflow.wid}</span>
                  <span>rev {rev}</span>
                  <span>{nodes} 节点</span>
                  {workflow.updated_at ? <span>{relativeTime(workflow.updated_at)}</span> : null}
                </div>
              </a>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
