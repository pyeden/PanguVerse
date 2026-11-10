import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, onStudioMockChange, isStudioMock } from '../shared/api'
import { href, navigate, type Route } from '../shared/router'
import { relativeTime, statusOf, classForStatus, shortId } from '../shared/utils'
import type { Project, StatusPayload } from '../shared/types'
import { Icon, type IconName } from './Icon'
import { Badge, IconButton, StatusBadge } from './ui'

const navItems: { id: string; label: string; href: string; icon: IconName; hint?: string }[] = [
  { id: 'home', label: '首页', href: '/overview', icon: 'home', hint: '览' },
  { id: 'tasks', label: '任务', href: '/tasks', icon: 'tasks', hint: '管' },
  { id: 'runs', label: '运行', href: '/runs', icon: 'play', hint: '跑' },
  { id: 'assets', label: '资产', href: '/atoms', icon: 'atoms', hint: '料' },
  { id: 'settings', label: '设置', href: '/settings', icon: 'settings', hint: '配' },
]

function isCanvasRoute(route: Route): boolean {
  return route.parts[0] === 'studio' && !!route.parts[1] && route.parts[1] !== 'new' && route.parts[1] !== 'explore' && route.parts[2] !== 'review'
}

function navActive(route: Route): string {
  const first = route.parts[0] || 'overview'
  if (first === 'overview' || first === 'projects') return 'home'
  if (first === 'next' || first === 'flows' || first === 'studio' || first === 'tasks' || first === 'schedules' || first === 'explore') return 'tasks'
  if (first === 'runs' || first === 'executions' || first === 'history' || first === 'reports' || first === 'artifacts' || first === 'p') return 'runs'
  if (first === 'atoms' || first === 'packs' || first === 'platform') return 'assets'
  if (first === 'settings') return 'settings'
  return first
}

function crumbs(route: Route, projects: Project[]): { label: string; href?: string }[] {
  const first = route.parts[0] || 'overview'
  if (first === 'overview') return [{ label: '概览' }]
  if (first === 'next') {
    return [{ label: '任务', href: '/tasks' }, ...(route.parts[1] === 'workbench' ? [{ label: '节点工作台' }] : [{ label: '新建任务' }])]
  }
  if (first === 'runs') return [{ label: '运行' }, ...(route.parts[1] ? [{ label: '运行详情' }] : [])]
  if (first === 'flows' || first === 'tasks') {
    if (route.parts[1] === 'config') return [{ label: '任务', href: '/tasks' }, { label: '运行配置' }]
    return [{ label: '任务' }]
  }
  if (first === 'settings') return [{ label: '设置' }]
  if (first === 'atoms') return [{ label: '资产' }, { label: '原子库' }]
  if (first === 'packs') return [{ label: '资产' }, { label: '领域包' }]
  if (first === 'executions' || first === 'history') return [{ label: '运行' }]
  if (first === 'reports') return [{ label: '运行' }, { label: '报告' }]
  if (first === 'artifacts') return [{ label: '运行' }, { label: '产出物' }]
  if (first === 'schedules') return [{ label: '任务', href: '/tasks' }, { label: '调度' }]
  if (first === 'projects') return [{ label: '首页', href: '/overview' }, { label: '项目' }]
  if (first === 'studio') {
    if (route.parts[1] === 'new') return [{ label: '任务', href: '/tasks' }, { label: '新建任务' }]
    if (route.parts[1] === 'explore') return [{ label: '任务', href: '/tasks' }, { label: '探索' }, ...(route.parts[2] ? [{ label: '回看' }] : [])]
    if (route.parts[2] === 'review') return [{ label: '任务', href: '/tasks' }, { label: '画布', href: `/studio/${route.parts[1]}` }, { label: '审阅' }]
    return [{ label: '任务', href: '/tasks' }, { label: '画布' }]
  }
  if (first === 'p') {
    const pid = route.parts[1]
    const project = projects.find((item) => item.project_id === pid)
    return [
      { label: '运行', href: '/runs' },
      { label: project?.name || pid },
      ...(route.parts[2] === 'runs' ? [{ label: '运行详情' }] : []),
    ]
  }
  return [{ label: 'PanguVerse' }]
}

export function Shell({
  route,
  user,
  authDisabled,
  status,
  onLogout,
  children,
}: {
  route: Route
  user: string
  authDisabled?: boolean
  status?: StatusPayload
  onLogout: () => void
  children: ReactNode
}) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [mock, setMock] = useState(isStudioMock())
  const active = navActive(route)
  const projectsQuery = useQuery({ queryKey: ['projects'], queryFn: () => api.projects(), staleTime: 20_000 })
  const projects = projectsQuery.data || []
  const breadcrumb = useMemo(() => crumbs(route, projects), [route, projects])
  const currentProjectId = route.parts[0] === 'p' ? route.parts[1] : ''

  useEffect(() => {
    setMobileOpen(false)
  }, [route.path])

  useEffect(() => onStudioMockChange(setMock), [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="app-shell">
      <div className="ambient ambient-a" aria-hidden="true" />
      <div className="ambient ambient-b" aria-hidden="true" />
      <a className="skip-link" href="#main-content">跳到主内容</a>

      {mobileOpen ? <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setMobileOpen(false)} /> : null}
      <aside className={`sidebar ${mobileOpen ? 'is-open' : ''}`} aria-label="主导航">
        <div className="brand-row">
          <a className="brand" href={href('/overview')} aria-label="PanguVerse 首页">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 32 32"><path d="M7 25 16 5l9 20z" /><path d="M11.4 19.5h9.2" /></svg>
            </span>
            <span className="brand-copy">
              <strong>PanguVerse</strong>
              <small>原子工作台</small>
            </span>
          </a>
          <IconButton label="关闭导航" icon="close" className="mobile-only" onClick={() => setMobileOpen(false)} />
        </div>

        <button className="create-flow" type="button" onClick={() => navigate('/next/console')}>
          <span className="create-flow-icon"><Icon name="plus" size={17} /></span>
          <span>
            <strong>新建任务</strong>
            <small>从一句话开始</small>
          </span>
          <span className="key-hint">N</span>
        </button>

        <nav className="primary-nav" aria-label="模块">
          <div className="nav-caption">工作区</div>
          {navItems.map((item) => (
            <a key={item.id} href={href(item.href)} className={`nav-item ${active === item.id ? 'is-active' : ''}`} aria-current={active === item.id ? 'page' : undefined}>
              <span className="nav-icon"><Icon name={item.icon} size={18} /></span>
              <span className="nav-label">{item.label}</span>
              {item.hint ? <span className="nav-hint" aria-hidden="true">{item.hint}</span> : null}
              {active === item.id ? <span className="nav-active-mark" aria-hidden="true" /> : null}
            </a>
          ))}
        </nav>

        <div className="sidebar-projects">
          <div className="nav-caption">
            <span>最近项目</span>
            <a href={href('/projects')} className="mini-link">全部</a>
          </div>
          {projectsQuery.isLoading ? <div className="side-loading"><span className="spinner spinner-sm" />读取中</div> : null}
          {!projectsQuery.isLoading && !projects.length ? (
            <a href={href('/projects')} className="side-empty">还没有项目，先创建一个</a>
          ) : null}
          <div className="side-project-list">
            {projects.slice(0, 5).map((project) => {
              const statusValue = statusOf(project.last_run)
              return (
                <a
                  key={project.project_id}
                  href={href(`/p/${project.project_id}/overview`)}
                  className={`side-project ${currentProjectId === project.project_id ? 'is-active' : ''}`}
                  title={project.name}
                >
                  <span className={`side-project-dot tone-${classForStatus(statusValue)}`} />
                  <span className="side-project-copy">
                    <strong>{project.name}</strong>
                    <small>{project.last_run ? `${project.last_run.status ? '' : ''}${relativeTime(project.last_run.at || project.last_run.started_at)}` : '尚未运行'}</small>
                  </span>
                </a>
              )
            })}
          </div>
        </div>

        <div className="sidebar-foot">
          <div className="connection">
            <span className={`connection-dot ${status ? 'is-online' : 'is-offline'}`} />
            <span>{status ? '服务已连接' : '状态读取中'}</span>
            {mock ? <Badge tone="warn" className="mock-badge">演示</Badge> : null}
          </div>
          <div className="account-row">
            <span className="avatar">{authDisabled ? <Icon name="server" size={15} /> : (user || '?').slice(0, 1).toUpperCase()}</span>
            <span className="account-copy">
              <strong>{authDisabled ? '本地模式' : user || '已登录'}</strong>
              <small>{status?.version ? `v${status.version}` : 'PanguVerse'}</small>
            </span>
            {!authDisabled ? <IconButton label="退出登录" icon="logout" onClick={onLogout} /> : null}
          </div>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-left">
            <IconButton label="打开导航" icon="menu" className="mobile-only" onClick={() => setMobileOpen(true)} />
            <nav className="breadcrumbs" aria-label="面包屑">
              {breadcrumb.map((item, index) => (
                <span key={`${item.label}-${index}`} className="crumb-item">
                  {index > 0 ? <Icon name="chevron" size={12} className="crumb-sep" /> : null}
                  {item.href ? <a href={href(item.href)}>{item.label}</a> : <span aria-current={index === breadcrumb.length - 1 ? 'page' : undefined}>{item.label}</span>}
                </span>
              ))}
            </nav>
          </div>
          <div className="topbar-right">
            {route.parts[0] === 'p' && route.parts[1] ? (
              <span className="topbar-context mono">{shortId(route.parts[1], 12)}</span>
            ) : null}
            <span className="model-chip" title={status?.llm_enabled ? '模型规划已启用' : '离线规则模式'}>
              <span className={`model-dot ${status?.llm_enabled ? 'on' : ''}`} />
              {status?.llm_enabled ? 'AI 在线' : '规则模式'}
            </span>
            <a href={href('/next/console')} className="topbar-create"><Icon name="spark" size={15} />新建任务</a>
          </div>
        </header>
        <main id="main-content" className={`content ${isCanvasRoute(route) ? 'content-canvas' : ''}`} tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  )
}

export function ProjectTabs({ pid, current }: { pid: string; current: string }) {
  const items = [
    { id: 'overview', label: '概览', href: href(`/p/${pid}/overview`) },
    { id: 'run', label: '运行', href: href(`/p/${pid}/run`) },
    { id: 'history', label: '历史', href: href(`/p/${pid}/history`) },
    { id: 'settings', label: '设置', href: href(`/p/${pid}/settings`) },
  ]
  return (
    <nav className="project-tabs" aria-label="项目导航">
      {items.map((item) => (
        <a key={item.id} className="project-tab" href={item.href} aria-current={current === item.id ? 'page' : undefined}>
          {item.label}
        </a>
      ))}
    </nav>
  )
}

export function RunStatus({ status, label }: { status?: string; label?: string }) {
  return <span className={`run-status tone-${classForStatus(status || 'none')}`}><span className="status-dot" />{label || (status ? status : '未运行')}</span>
}
