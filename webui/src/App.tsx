import { Component, useEffect, useMemo, useState, type ErrorInfo, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, UNAUTHORIZED_EVENT } from './shared/api'
import { navigate, useRoute, withQuery, type Route } from './shared/router'
import { Shell } from './components/Shell'
import { ToastProvider, Button, Loading } from './components/ui'
import { Icon } from './components/Icon'
import { LoginPage } from './pages/LoginPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { AtomsPage } from './pages/AtomsPage'
import { TasksPage } from './pages/TasksPage'
import { RunsPage } from './pages/RunsPage'
import { FlowsPage } from './pages/FlowsPage'
import { OverviewPage } from './pages/OverviewPage'
import { SettingsPage } from './pages/SettingsPage'
import { StudioCanvasPage } from './pages/StudioCanvasPage'
import { StudioReviewPage } from './pages/StudioReviewPage'
import { NextConsolePage } from './pages/next/NextConsolePage'
import { NextWorkbenchPage } from './pages/next/NextWorkbenchPage'
import { clearConsoleContext } from './shared/nextConsoleContext'

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
      clearConsoleContext()
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
  // 未登录时必须显示登录标题；登录后按路由 title 显示（路由没变也要重算）
  useEffect(() => {
    document.title = authState === 'in' ? `${title} · PanguVerse` : '登录 · PanguVerse'
  }, [title, authState])

  const logout = async () => {
    try {
      await api.logout()
    } finally {
      clearConsoleContext()
      queryClient.clear()
      setUser('')
      setAuthDisabled(false)
      setExpired(false)
      setAuthState('out')
      navigate('/overview', { replace: true })
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
  const first = parts[0] || 'overview'

  if (first === 'platform') {
    const target = parts[1] === 'tasks' ? '/tasks' : parts[1] === 'packs' ? '/atoms' : '/atoms'
    return <Redirect to={target} />
  }
  if (first === 'run' && parts.length === 1) return <Redirect to="/overview" />
  if (first === 'packs') return <Redirect to="/atoms" />
  if (first === 'runs' && parts[1]) return <RunDetailPage key={parts[1]} />
  if (first === 'runs') return <RunsPage />
  if (first === 'overview') return <OverviewPage />
  if (first === 'flows') return <Redirect to={withQuery('/tasks', route.query)} />
  if (first === 'tasks') {
    if (parts[1] === 'config') return <TasksPage />
    if (route.query.get('focus') || route.query.get('published')) return <Redirect to={withQuery('/tasks/config', route.query)} />
    return <FlowsPage />
  }
  if (first === 'settings') return <SettingsPage />
  if (first === 'executions' || first === 'history') {
    if (first === 'history') {
      const focus = route.query.get('focus')
      if (focus) {
        const next = new URLSearchParams(route.query)
        next.delete('focus')
        return <Redirect to={withQuery(`/runs/${encodeURIComponent(focus)}`, next)} />
      }
    }
    return <Redirect to="/runs" />
  }
  if (first === 'reports') return <Redirect to={withQuery('/runs', { view: 'reports' })} />
  if (first === 'artifacts') {
    const run = route.query.get('run')
    return <Redirect to={withQuery('/runs', { view: 'artifacts', ...(run ? { run } : {}) })} />
  }
  if (first === 'projects') return <Redirect to="/overview" />
  if (first === 'atoms') return <AtomsPage />
  if (first === 'schedules') return <Redirect to="/tasks" />
  if (first === 'next') {
    if (parts[1] === 'console') return <NextConsolePage />
    if (parts[1] === 'workbench') return <NextWorkbenchPage wid={parts[2]} nodeId={parts[3]} key={`${parts[2] || ''}-${parts[3] || ''}`} />
    return <Redirect to="/next/console" />
  }
  if (first === 'studio' && (!parts[1] || parts[1] === 'new')) return <Redirect to="/next/console" />
  if (first === 'studio' && parts[1] === 'explore') return <Redirect to="/tasks" />
  if (first === 'studio') {
    if (parts[2] === 'review') return <StudioReviewPage wid={parts[1]} key={`${parts[1]}-review`} />
    return <StudioCanvasPage wid={parts[1]} key={parts[1]} />
  }
  if (first === 'p') return <Redirect to="/overview" />
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
      <p>没有匹配 <code>#{route.path}</code> 的路由。旧入口已迁移到概览、任务、运行与资产。</p>
      <div className="notfound-actions">
        <Button variant="primary" onClick={() => navigate('/overview')}>回到概览</Button>
        <Button variant="ghost" onClick={() => navigate('/next/console')}>新建任务</Button>
      </div>
    </div>
  )
}

function pageTitle(route: Route): string {
  const first = route.parts[0] || 'overview'
  if (first === 'overview') return '概览'
  if (first === 'next') return route.parts[1] === 'workbench' ? '节点工作台' : '新建任务'
  if (first === 'flows') return '任务'
  if (first === 'tasks') return route.parts[1] === 'config' ? '运行配置' : '任务'
  if (first === 'runs') return route.parts[1] ? '运行详情' : '运行'
  if (first === 'settings') return '设置'
  if (first === 'studio') return route.parts[2] === 'review' ? '审阅与发布' : route.parts[1] && route.parts[1] !== 'new' ? '任务画布' : '新建任务'
  const map: Record<string, string> = {
    overview: '概览',
    atoms: '原子库',
    tasks: '任务',
    runs: '运行',
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
            <Button variant="ghost" onClick={() => { this.setState({ error: null }); navigate('/overview') }}>回到概览</Button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
