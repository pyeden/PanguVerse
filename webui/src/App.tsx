import { Component, useEffect, useMemo, useState, type ErrorInfo, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, UNAUTHORIZED_EVENT } from './shared/api'
import { navigate, useRoute, withQuery, type Route } from './shared/router'
import { Shell } from './components/Shell'
import { ToastProvider, Button, Loading } from './components/ui'
import { Icon } from './components/Icon'
import { LoginPage } from './pages/LoginPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ProjectHistoryPage, ProjectOverviewPage, ProjectRunPage, ProjectSettingsPage } from './pages/ProjectPages'
import { RunDetailPage } from './pages/RunDetailPage'
import { AtomsPage } from './pages/AtomsPage'
import { TasksPage } from './pages/TasksPage'
import { ExecutionsPage } from './pages/ExecutionsPage'
import { HistoryPage } from './pages/HistoryPage'
import { ReportsPage } from './pages/ReportsPage'
import { RunsPage } from './pages/RunsPage'
import { FlowsPage } from './pages/FlowsPage'
import { SettingsPage } from './pages/SettingsPage'
import { SchedulesPage } from './pages/SchedulesPage'
import { ArtifactsPage } from './pages/ArtifactsPage'
import { StudioNewPage } from './pages/StudioNewPage'
import { StudioExplorePage } from './pages/StudioExplorePage'
import { StudioCanvasPage } from './pages/StudioCanvasPage'
import { StudioReviewPage } from './pages/StudioReviewPage'
import { NextConsolePage } from './pages/next/NextConsolePage'
import { NextWorkbenchPage } from './pages/next/NextWorkbenchPage'

type AuthState = 'loading' | 'in' | 'out'

export default function App() {
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [user, setUser] = useState('')
  const [authDisabled, setAuthDisabled] = useState(false)
  const [expired, setExpired] = useState(false)
  const route = useRoute()
  const queryClient = useQueryClient()

  useEffect(() => {
    let alive = true
    api.authStatus()
      .then((result) => {
        if (!alive) return
        if (result.authenticated) {
          setUser(result.user || '')
          setAuthDisabled(!!result.auth_disabled)
          setAuthState('in')
        } else {
          setAuthState('out')
        }
      })
      .catch(() => {
        if (!alive) return
        setAuthState('out')
        setExpired(false)
      })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    const onUnauthorized = () => {
      setAuthState((current) => {
        if (current === 'in') setExpired(true)
        return 'out'
      })
      queryClient.clear()
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [queryClient])

  useEffect(() => {
    if (authState !== 'in') return
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior })
  }, [route.path, authState])

  const statusQuery = useQuery({ queryKey: ['status'], queryFn: api.status, enabled: authState === 'in', staleTime: 30_000 })
  const title = pageTitle(route)
  useEffect(() => { document.title = `${title} · PanguVerse` }, [title])

  const logout = async () => {
    try {
      await api.logout()
    } finally {
      queryClient.clear()
      setUser('')
      setAuthDisabled(false)
      setExpired(false)
      setAuthState('out')
      navigate('/projects', { replace: true })
    }
  }

  const loginSuccess = (nextUser: string) => {
    setUser(nextUser)
    setExpired(false)
    setAuthState('in')
    queryClient.invalidateQueries()
  }

  if (authState === 'loading') {
    return (
      <div className="boot-screen">
        <span className="boot-mark"><svg viewBox="0 0 32 32"><path d="M7 25 16 5l9 20z" /><path d="M11.4 19.5h9.2" /></svg></span>
        <span className="spinner" />
        <strong>正在校验会话…</strong>
        <span>PanguVerse 原子工作台</span>
      </div>
    )
  }

  if (authState === 'out') {
    return <LoginPage onSuccess={loginSuccess} expired={expired} />
  }

  return (
    <ToastProvider>
      <ErrorBoundary>
        <Shell route={route} user={user} authDisabled={authDisabled} status={statusQuery.data} onLogout={logout}>
          <RoutedPage route={route} />
        </Shell>
      </ErrorBoundary>
    </ToastProvider>
  )
}

function RoutedPage({ route }: { route: Route }) {
  const parts = route.parts
  const first = parts[0] || 'projects'

  if (first === 'platform') {
    const target = parts[1] === 'tasks' ? '/tasks' : parts[1] === 'packs' ? '/atoms' : '/atoms'
    return <Redirect to={target} />
  }
  if (['overview', 'run'].includes(first) && parts.length === 1) return <Redirect to="/projects" />
  if (first === 'packs') return <Redirect to="/atoms" />
  if (first === 'runs' && parts[1]) return <RunDetailPage key={parts[1]} />
  if (first === 'runs') return <RunsPage />
  if (first === 'flows') return <FlowsPage />
  if (first === 'settings') return <SettingsPage />
  if (first === 'executions' || first === 'history') return <Redirect to="/runs" />
  if (first === 'reports') return <Redirect to={withQuery('/runs', { view: 'reports' })} />
  if (first === 'artifacts') {
    const run = route.query.get('run')
    return <Redirect to={withQuery('/runs', { view: 'artifacts', ...(run ? { run } : {}) })} />
  }
  if (first === 'projects') return <ProjectsPage />
  if (first === 'atoms') return <AtomsPage />
  if (first === 'tasks') return <TasksPage />
  if (first === 'executions') return <ExecutionsPage />
  if (first === 'history') return <HistoryPage />
  if (first === 'reports') return <ReportsPage />
  if (first === 'schedules') return <SchedulesPage />
  if (first === 'artifacts') return <ArtifactsPage />
  if (first === 'next') {
    if (parts[1] === 'console') return <NextConsolePage />
    if (parts[1] === 'workbench') return <NextWorkbenchPage wid={parts[2]} nodeId={parts[3]} key={`${parts[2] || ''}-${parts[3] || ''}`} />
    return <Redirect to="/next/console" />
  }
  if (first === 'studio' && (!parts[1] || parts[1] === 'new')) return <Redirect to="/next/console" />
  if (first === 'studio') {
    if (!parts[1] || parts[1] === 'new') return <StudioNewPage />
    if (parts[1] === 'explore') return <StudioExplorePage eid={parts[2]} key={parts[2] || 'explore-new'} />
    if (parts[2] === 'review') return <StudioReviewPage wid={parts[1]} key={`${parts[1]}-review`} />
    return <StudioCanvasPage wid={parts[1]} key={parts[1]} />
  }
  if (first === 'p' && parts[1]) {
    const page = parts[2] || 'overview'
    if (parts[2] === 'runs' && parts[3]) return <RunDetailPage key={`${parts[3]}-${parts[1]}`} />
    if (page === 'overview') return <ProjectOverviewPage key={parts[1]} />
    if (page === 'run') return <ProjectRunPage key={parts[1]} />
    if (page === 'history') return <ProjectHistoryPage key={parts[1]} />
    if (page === 'settings') return <ProjectSettingsPage key={parts[1]} />
  }
  return <NotFound route={route} />
}

function Redirect({ to }: { to: string }) {
  useEffect(() => {
    navigate(to, { replace: true })
  }, [to])
  return <Loading label="正在重定向…" />
}

function NotFound({ route }: { route: Route }) {
  return (
    <div className="page notfound-page">
      <div className="notfound-mark"><Icon name="flow" size={30} /></div>
      <h1>页面不存在</h1>
      <p>没有匹配 <code>#{route.path}</code> 的路由。旧入口已经迁移到新的六模块结构。</p>
      <div className="notfound-actions">
        <Button variant="primary" onClick={() => navigate('/projects')}>回到项目</Button>
        <Button variant="ghost" onClick={() => navigate('/studio/new')}>打开工作台</Button>
      </div>
    </div>
  )
}

function pageTitle(route: Route): string {
  const first = route.parts[0] || 'projects'
  if (first === 'next') return route.parts[1] === 'workbench' ? '节点工作台' : 'AI 控制台'
  if (first === 'flows') return '流程'
  if (first === 'runs') return route.parts[1] ? '运行详情' : '运行'
  if (first === 'settings') return '设置'
  if (first === 'studio') return route.parts[1] === 'explore' ? (route.parts[2] ? '探索回看' : '探索') : route.parts[2] === 'review' ? '审阅与发布' : route.parts[1] && route.parts[1] !== 'new' ? '流程画布' : '新建流程'
  if (first === 'p') {
    const page = route.parts[2]
    return page === 'run' ? '运行' : page === 'history' ? '项目历史' : page === 'settings' ? '项目设置' : route.parts[2] === 'runs' ? '运行报告' : '项目概览'
  }
  const map: Record<string, string> = {
    projects: '项目',
    atoms: '原子库',
    tasks: '任务',
    executions: '执行',
    history: '历史',
    reports: '报告',
    schedules: '调度',
    artifacts: '产出物',
    runs: '运行报告',
  }
  return map[first] || 'PanguVerse'
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('PanguVerse UI error boundary', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="page notfound-page">
          <div className="notfound-mark bad"><Icon name="warning" size={30} /></div>
          <h1>界面遇到未预期错误</h1>
          <p>{this.state.error.message}</p>
          <div className="notfound-actions">
            <Button variant="primary" onClick={() => window.location.reload()}>重新加载</Button>
            <Button variant="ghost" onClick={() => { this.setState({ error: null }); navigate('/projects') }}>回到项目</Button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
