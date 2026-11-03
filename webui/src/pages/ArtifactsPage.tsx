import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../shared/api'
import { navigate, useRoute, withQuery } from '../shared/router'
import { copyText, formatBytes, formatTime, relativeTime, relativeUntil, shortId } from '../shared/utils'
import type { Artifact, ArtifactShare } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  Markdown,
  Modal,
  PageHeader,
  Select,
  useToast,
} from '../components/ui'

const PAGE_SIZE = 36
const PREVIEW_LIMIT = 20_000

const typeMeta: Record<string, { label: string; tone: 'violet' | 'cyan' | 'warn' | 'neutral' | 'ai' }> = {
  markdown: { label: 'Markdown', tone: 'violet' },
  image: { label: '图片', tone: 'cyan' },
  json: { label: 'JSON', tone: 'warn' },
  xml: { label: 'XML', tone: 'ai' },
  text: { label: '文本', tone: 'neutral' },
  file: { label: '文件', tone: 'neutral' },
}

const TTL_OPTIONS = [
  { value: '1h', label: '1 小时' },
  { value: '1d', label: '1 天' },
  { value: '7d', label: '7 天（默认）' },
  { value: '30d', label: '30 天' },
]

export function ArtifactsPage() {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const [project, setProject] = useState(route.query.get('project') || 'all')
  const [run, setRun] = useState(route.query.get('run') || '')
  const [type, setType] = useState(route.query.get('type') || 'all')
  const [offset, setOffset] = useState(0)
  const [previewId, setPreviewId] = useState(route.query.get('focus') || '')
  const [ttl, setTtl] = useState('7d')
  const [shares, setShares] = useState<Record<string, ArtifactShare>>({})

  useEffect(() => {
    setProject(route.query.get('project') || 'all')
    setRun(route.query.get('run') || '')
    setType(route.query.get('type') || 'all')
    setOffset(0)
    setPreviewId(route.query.get('focus') || '')
  }, [route.query])

  const params = useMemo(() => {
    const next = new URLSearchParams({ offset: String(offset), limit: String(PAGE_SIZE) })
    if (project !== 'all') next.set('project', project)
    if (run.trim()) next.set('run', run.trim())
    if (type !== 'all') next.set('type', type)
    return next
  }, [offset, project, run, type])

  const artifactsQuery = useQuery({
    queryKey: ['artifacts', params.toString()],
    queryFn: () => api.artifacts(params),
  })
  const projectsQuery = useQuery({ queryKey: ['projects', 'with-archived'], queryFn: () => api.projects(true), staleTime: 30_000 })
  const detailQuery = useQuery({
    queryKey: ['artifact', previewId],
    queryFn: () => api.artifact(previewId),
    enabled: !!previewId,
  })

  const projects = projectsQuery.data || []
  const projectName = useMemo(() => {
    const map = new Map(projects.map((item) => [item.project_id, item.name]))
    return (pid?: string) => (pid ? map.get(pid) || pid : '未归类')
  }, [projects])
  const data = artifactsQuery.data
  const items = data?.items || []
  const total = data?.total ?? items.length
  const apiMissing = artifactsQuery.error instanceof ApiError && artifactsQuery.error.status === 404

  const shareMutation = useMutation({
    mutationFn: ({ aid, ttlValue }: { aid: string; ttlValue: string }) => api.shareArtifact(aid, ttlValue),
    onSuccess: (share, variables) => {
      setShares((current) => ({ ...current, [variables.aid]: share }))
      queryClient.invalidateQueries({ queryKey: ['artifact', variables.aid] })
      push('分享链接已生成', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const revokeMutation = useMutation({
    mutationFn: (aid: string) => api.revokeArtifactShare(aid),
    onSuccess: (_, aid) => {
      setShares((current) => {
        const next = { ...current }
        delete next[aid]
        return next
      })
      queryClient.invalidateQueries({ queryKey: ['artifact', aid] })
      queryClient.invalidateQueries({ queryKey: ['artifacts'] })
      push('分享已撤销', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const reset = () => {
    setProject('all')
    setRun('')
    setType('all')
    setOffset(0)
    if (route.query.toString()) navigate('/artifacts', { replace: true })
  }

  return (
    <div className="page artifacts-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 交付 · 产出物</>}
        title="产出物"
        description="一次运行中值得交付的结果：summary_md、截图、JSON/XML 导出。列表按项目 / 运行 / 类型筛选，分享链接只读且可撤销。"
        actions={<Button variant="ghost" icon="refresh" onClick={() => queryClient.invalidateQueries({ queryKey: ['artifacts'] })}>刷新</Button>}
      />

      {apiMissing ? (
        <div className="notice-inline warn-note">
          <Icon name="warning" size={15} />
          <div>
            <strong>后端接口未就绪（未联调）</strong>
            <p>当前前端已按契约 v8.0 §2.2 接入 <code>api/artifacts</code> 与 <code>api/share</code>，但本机后端仍返回 404。接口上线后本页无需改动即可工作。</p>
          </div>
        </div>
      ) : null}

      <div className="catalog-toolbar panel artifact-filters">
        <Select value={project} onChange={(event) => { setProject(event.target.value); setOffset(0) }} aria-label="按项目筛选">
          <option value="all">全部项目</option>
          {projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
        </Select>
        <label className="search-input artifact-run-filter">
          <Icon name="terminal" size={16} />
          <input
            type="search"
            value={run}
            onChange={(event) => { setRun(event.target.value); setOffset(0) }}
            placeholder="按运行 ID 筛选"
            aria-label="按运行 ID 筛选"
            autoComplete="off"
          />
          {run ? <button type="button" className="search-clear" onClick={() => { setRun(''); setOffset(0) }} aria-label="清除运行 ID"><Icon name="close" size={13} /></button> : null}
        </label>
        <Select value={type} onChange={(event) => { setType(event.target.value); setOffset(0) }} aria-label="按类型筛选">
          <option value="all">全部类型</option>
          <option value="markdown">Markdown</option>
          <option value="image">图片</option>
          <option value="json">JSON</option>
          <option value="xml">XML</option>
          <option value="text">文本</option>
          <option value="file">文件</option>
        </Select>
        <span className="toolbar-spacer" />
        {project !== 'all' || run || type !== 'all' ? <Button size="sm" variant="ghost" icon="close" onClick={reset}>清除筛选</Button> : null}
        <span className="toolbar-count">{total} 项</span>
      </div>

      {artifactsQuery.isLoading ? <Loading label="正在读取产出物…" /> : null}
      {artifactsQuery.error && !apiMissing ? <ErrorState error={artifactsQuery.error} onRetry={() => artifactsQuery.refetch()} /> : null}

      {!artifactsQuery.isLoading && !artifactsQuery.error && !items.length ? (
        <EmptyState icon="archive" title="没有匹配的产出物">
          跑一次包含 <code>llm_summarize</code> 的运行后，<code>summary_md</code> 会出现在这里；也可以放宽项目 / 运行 / 类型筛选。
        </EmptyState>
      ) : null}

      {items.length ? (
        <div className="artifact-grid">
          {items.map((artifact) => {
            const meta = typeMeta[artifact.type] || { label: artifact.type, tone: 'neutral' as const }
            return (
              <article className="artifact-card" key={artifact.artifact_id} data-testid={`artifact-card-${artifact.artifact_id}`}>
                <button type="button" className="artifact-card-main" onClick={() => setPreviewId(artifact.artifact_id)}>
                  <span className={`artifact-type tone-${meta.tone}`}><Icon name={artifact.type === 'image' ? 'eye' : artifact.type === 'markdown' ? 'report' : artifact.type === 'json' || artifact.type === 'xml' ? 'code' : 'file'} size={19} /></span>
                  <span className="artifact-copy">
                    <span className="artifact-title"><strong>{artifact.name}</strong><Badge tone={meta.tone}>{meta.label}</Badge></span>
                    <span className="artifact-meta">
                      <span>{formatBytes(artifact.bytes)}</span>
                      <span>·</span>
                      <span>{projectName(artifact.project_id)}</span>
                      <span>·</span>
                      <span className="mono">{artifact.node_id || 'unknown'}</span>
                    </span>
                    <span className="artifact-meta">
                      <span>run <code>{shortId(artifact.run_id, 12)}</code></span>
                      <span>·</span>
                      <span>{relativeTime(artifact.created_at)}</span>
                      <span>·</span>
                      <span>{formatTime(artifact.created_at)}</span>
                    </span>
                  </span>
                  {artifact.truncated ? <Badge tone="warn">已截断</Badge> : null}
                </button>
                <div className="artifact-card-actions">
                  <a className="btn btn-ghost btn-sm" href={api.artifactDownloadUrl(artifact.artifact_id)} download={artifact.name}>
                    <Icon name="download" size={15} /><span>下载</span>
                  </a>
                  <Button size="sm" variant="ghost" icon="link" onClick={() => setPreviewId(artifact.artifact_id)}>分享</Button>
                </div>
              </article>
            )
          })}
        </div>
      ) : null}

      {total > PAGE_SIZE ? (
        <div className="pagination">
          <span>第 {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} 项 · 共 {total} 项</span>
          <div className="pagination-actions">
            <Button size="sm" variant="ghost" icon="arrow" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
            <Button size="sm" variant="ghost" icon="arrow" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
          </div>
        </div>
      ) : null}

      <ArtifactPreview
        artifact={detailQuery.data}
        loading={detailQuery.isLoading}
        error={detailQuery.error}
        aid={previewId}
        share={shares[previewId]}
        ttl={ttl}
        onTtl={setTtl}
        sharing={shareMutation.isPending}
        revoking={revokeMutation.isPending}
        onShare={() => previewId && shareMutation.mutate({ aid: previewId, ttlValue: ttl })}
        onRevoke={() => previewId && revokeMutation.mutate(previewId)}
        onClose={() => {
          setPreviewId('')
          if (route.query.get('focus')) {
            const next = new URLSearchParams(route.query)
            next.delete('focus')
            navigate(withQuery('/artifacts', next), { replace: true })
          }
        }}
      />
    </div>
  )
}

function ArtifactPreview({
  artifact,
  loading,
  error,
  aid,
  share,
  ttl,
  onTtl,
  sharing,
  revoking,
  onShare,
  onRevoke,
  onClose,
}: {
  artifact?: Artifact
  loading: boolean
  error: unknown
  aid: string
  share?: ArtifactShare
  ttl: string
  onTtl: (value: string) => void
  sharing: boolean
  revoking: boolean
  onShare: () => void
  onRevoke: () => void
  onClose: () => void
}) {
  const { push } = useToast()
  const [copied, setCopied] = useState(false)
  const activeShare = share || (artifact?.share_token ? { url_token: artifact.share_token, expires_at: artifact.share_expires_at || 0 } : undefined)
  const shareUrl = activeShare
    ? activeShare.url || `${window.location.origin}/api/share/${activeShare.url_token}`
    : ''

  useEffect(() => { setCopied(false) }, [aid])

  const raw = artifact?.content || ''
  const truncated = raw.length > PREVIEW_LIMIT
  const shown = truncated ? raw.slice(0, PREVIEW_LIMIT) : raw
  const prettyJson = useMemo(() => {
    if (artifact?.type !== 'json' || !shown) return shown
    try {
      return JSON.stringify(JSON.parse(shown), null, 2).slice(0, PREVIEW_LIMIT)
    } catch {
      return shown
    }
  }, [artifact?.type, shown])

  return (
    <Modal
      open={!!aid}
      onClose={onClose}
      title={artifact?.name || '产出物预览'}
      subtitle={artifact ? `${artifact.artifact_id} · ${artifact.type} · ${formatBytes(artifact.bytes)}` : undefined}
      size="xl"
      footer={
        <>
          {artifact ? (
            <a className="btn btn-ghost btn-sm" href={api.artifactDownloadUrl(artifact.artifact_id)} download={artifact.name}>
              <Icon name="download" size={15} /><span>下载</span>
            </a>
          ) : null}
          <Button variant="ghost" onClick={onClose}>关闭</Button>
        </>
      }
    >
      {loading ? <Loading label="正在读取内容…" /> : null}
      {error ? <ErrorState error={error} title="产出物读取失败" /> : null}
      {artifact ? (
        <div className="artifact-preview">
          <div className="artifact-preview-meta">
            <Badge tone={typeMeta[artifact.type]?.tone || 'neutral'}>{typeMeta[artifact.type]?.label || artifact.type}</Badge>
            <span>run <code>{artifact.run_id}</code></span>
            <span>节点 <code>{artifact.node_id || '—'}</code></span>
            <span>{formatTime(artifact.created_at)}</span>
            {artifact.sha256 ? <span className="mono">sha256 {artifact.sha256.slice(0, 12)}…</span> : null}
          </div>

          <div className={`artifact-content type-${artifact.type}`}>
            {artifact.type === 'markdown' ? (
              raw ? <Markdown value={shown} /> : <p className="muted">没有可预览的文本内容，请下载查看。</p>
            ) : null}
            {artifact.type === 'image' ? (
              <img
                className="artifact-image"
                src={artifact.download_url || api.artifactDownloadUrl(artifact.artifact_id)}
                alt={artifact.name}
                loading="lazy"
              />
            ) : null}
            {artifact.type === 'json' || artifact.type === 'xml' || artifact.type === 'text' || artifact.type === 'file' ? (
              shown
                ? <pre className="artifact-code">{artifact.type === 'json' ? prettyJson : shown}</pre>
                : <p className="muted">文件过大或为二进制，预览不可用；请下载查看。</p>
            ) : null}
          </div>
          {truncated ? (
            <div className="artifact-truncated"><Icon name="warning" size={13} />仅展示前 {PREVIEW_LIMIT.toLocaleString()} 字符；完整内容请下载。</div>
          ) : null}
          {artifact.truncated ? <div className="artifact-truncated"><Icon name="warning" size={13} />后端标记该产出物超过 20MB，仅保留元信息。</div> : null}

          <section className="artifact-share">
            <div className="artifact-share-head">
              <div><strong>分享</strong><p>生成只读链接，未登录也可打开预览页；过期或撤销后立即失效。</p></div>
              {artifact.share_expires_at ? <Badge tone="warn">已分享至 {formatTime(artifact.share_expires_at)}</Badge> : null}
            </div>
            <div className="artifact-share-actions">
              <Select value={ttl} onChange={(event) => onTtl(event.target.value)} aria-label="分享有效期">
                {TTL_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </Select>
              <Button size="sm" variant="primary" icon="link" loading={sharing} onClick={onShare}>生成分享链接</Button>
              {activeShare ? <Button size="sm" variant="ghost" icon="close" loading={revoking} onClick={onRevoke}>撤销分享</Button> : null}
            </div>
            {shareUrl ? (
              <div className="share-url-row">
                <code className="word-break">{shareUrl}</code>
                <Button
                  size="sm"
                  variant="soft"
                  icon="copy"
                  onClick={() => copyText(shareUrl).then(() => { setCopied(true); push('分享链接已复制', 'ok') }).catch(() => push('复制失败', 'error'))}
                >{copied ? '已复制' : '复制'}</Button>
              </div>
            ) : null}
            {activeShare?.expires_at ? <p className="muted">有效期至 {formatTime(activeShare.expires_at)} · {relativeUntil(activeShare.expires_at)}</p> : null}
          </section>
        </div>
      ) : null}
    </Modal>
  )
}
