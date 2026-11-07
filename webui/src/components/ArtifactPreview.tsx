import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import type { Artifact } from '../shared/types'
import { formatBytes, formatTime } from '../shared/utils'
import { Badge, Button, ErrorState, Loading, Markdown, Modal } from './ui'
import { Icon } from './Icon'

const PREVIEW_LIMIT = 20_000

const typeLabel: Record<string, string> = {
  markdown: 'Markdown',
  image: '图片',
  json: 'JSON',
  xml: 'XML',
  text: '文本',
  file: '文件',
}

/**
 * 产出物就地预览：任务产出（deliverable）与执行报告（report）共用。
 * 大文件只显示前 20k 字符，完整内容通过下载获取。
 */
export function ArtifactPreviewModal({ artifactId, onClose }: { artifactId: string; onClose: () => void }) {
  const detailQuery = useQuery({
    queryKey: ['artifact-preview', artifactId],
    queryFn: () => api.artifact(artifactId),
    enabled: !!artifactId,
  })
  const artifact: Artifact | undefined = detailQuery.data
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
      open={!!artifactId}
      onClose={onClose}
      title={artifact?.name || '产出物预览'}
      subtitle={artifact ? `${artifact.artifact_id} · ${typeLabel[artifact.type] || artifact.type} · ${formatBytes(artifact.bytes)}` : undefined}
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
      <div className="artifact-preview" data-testid="artifact-preview-modal" data-artifact-id={artifactId}>
        {detailQuery.isLoading ? <Loading label="正在读取内容…" /> : null}
        {detailQuery.error ? <ErrorState error={detailQuery.error} title="产出物读取失败" /> : null}
        {artifact ? (
          <>
            <div className="artifact-preview-meta">
              <Badge tone="neutral">{typeLabel[artifact.type] || artifact.type}</Badge>
              <span>run <code>{artifact.run_id}</code></span>
              <span>节点 <code>{artifact.node_id || '—'}</code></span>
              {artifact.step_id ? <span>步骤 <code>{artifact.step_id}</code></span> : null}
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
          </>
        ) : null}
      </div>
    </Modal>
  )
}
