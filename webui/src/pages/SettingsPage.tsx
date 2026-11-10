import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import { href } from '../shared/router'
import { PageHeader, SectionHeader, Loading } from '../components/ui'

export function SettingsPage() {
  const statusQuery = useQuery({ queryKey: ['status'], queryFn: api.status, staleTime: 30_000 })
  const authQuery = useQuery({ queryKey: ['auth-status'], queryFn: api.authStatus, staleTime: 30_000 })
  const status = statusQuery.data
  const auth = authQuery.data

  return (
    <div className="page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 设置 · 工作区</>}
        title="设置"
        description="工作区、账号与会话状态。"
      />

      <div className="settings-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 14 }}>
        <section className="panel">
          <SectionHeader icon="server" title="工作区" description="当前运行环境" />
          {statusQuery.isLoading ? <Loading label="正在读取工作区…" /> : (
            <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <Row label="路径" value={status?.workspace || status?.root || '—'} mono />
              <Row label="版本" value={status?.version || '—'} />
              <Row label="原子 / 包" value={`${status?.atoms ?? '—'} / ${status?.packs ?? '—'}`} />
              <Row label="规划方式" value={status?.llm_enabled ? 'LLM 可用' : '规则兜底'} />
            </div>
          )}
        </section>

        <section className="panel">
          <SectionHeader icon="user" title="账号" description="认证与会话" />
          {authQuery.isLoading ? <Loading label="正在读取账号…" /> : (
            <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <Row label="当前用户" value={auth?.user || (auth?.auth_disabled ? '未启用认证' : '—')} />
              <Row label="认证状态" value={auth?.auth_disabled ? '已关闭（本机调试）' : auth?.authenticated ? '已登录' : '未登录'} />
              <Row label="会话" value={auth?.authenticated ? '有效' : '无'} />
            </div>
          )}
        </section>

        <section className="panel">
          <SectionHeader icon="flow" title="快捷入口" description="常用模块" />
          <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <a className="inline-link" href={href('/overview')}>概览</a>
            <a className="inline-link" href={href('/tasks')}>任务</a>
            <a className="inline-link" href={href('/runs')}>运行</a>
            <a className="inline-link" href={href('/atoms')}>资产 · 原子库</a>
            <a className="inline-link" href={href('/next/console')}>新建任务</a>
          </div>
        </section>
      </div>
    </div>
  )
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, fontSize: 11.5, borderTop: '1px dashed var(--line)', paddingTop: 5 }}>
      <span className="muted">{label}</span>
      <span className={mono ? 'mono' : ''} style={{ textAlign: 'right', wordBreak: 'break-all' }}>{value}</span>
    </div>
  )
}
