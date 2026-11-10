import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../shared/api'
import { Icon } from '../components/Icon'
import { Button } from '../components/ui'

export function LoginPage({
  onSuccess,
  notice,
  expired,
}: {
  onSuccess: (user: string) => void
  notice?: string
  expired?: boolean
}) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const passwordRef = useRef<HTMLInputElement>(null)
  const userRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!username) userRef.current?.focus()
    else passwordRef.current?.focus()
  }, [username])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (!username.trim() || !password) {
      setError('请输入账号和密码。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await api.login(username.trim(), password)
      onSuccess(result.user || username.trim())
    } catch (err) {
      const apiError = err as ApiError
      setError(apiError.message || '登录失败')
      setPassword('')
      passwordRef.current?.focus()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-ambient login-ambient-a" aria-hidden="true" />
      <div className="login-ambient login-ambient-b" aria-hidden="true" />
      <div className="login-grid">
        <section className="login-story" aria-label="产品简介">
          <a className="login-brand" href="#/projects">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 32 32"><path d="M7 25 16 5l9 20z" /><path d="M11.4 19.5h9.2" /></svg>
            </span>
            <span><strong>PanguVerse</strong><small>原子工作台</small></span>
          </a>
          <div className="story-copy">
            <div className="eyebrow"><span className="live-dot" /> AI + 人工裁决的自动化闭环</div>
            <h1>把一句话<br />变成<span>可审阅的任务</span></h1>
            <p>意图识别、可视化编排、单步调试、逐条审阅、不可变发布。每一步都留下 AI 提议与人工裁决的痕迹。</p>
          </div>

          <div className="story-canvas" aria-hidden="true">
            <div className="story-node node-1">
              <span className="story-node-icon"><Icon name="spark" size={14} /></span>
              <div><strong>识别意图</strong><small>登录后查询订单</small></div>
              <span className="story-badge ai">AI</span>
            </div>
            <div className="story-line line-1"><span>order-only</span></div>
            <div className="story-node node-2">
              <span className="story-node-icon"><Icon name="link" size={14} /></span>
              <div><strong>http_request</strong><small>POST /auth/login</small></div>
              <span className="story-badge human">人工</span>
            </div>
            <div className="story-line line-2 data"><span>token</span></div>
            <div className="story-node node-3">
              <span className="story-node-icon"><Icon name="check" size={14} /></span>
              <div><strong>assert_status</strong><small>期望 200</small></div>
              <span className="story-lock"><Icon name="lock" size={10} /></span>
            </div>
            <div className="story-trace">
              <span className="trace-ok"><Icon name="check" size={11} /> 调试通过</span>
              <span>3 节点 · 318ms</span>
              <span className="trace-muted">不影响历史</span>
            </div>
          </div>

          <div className="story-foot">
            <span><Icon name="shield" size={14} /> 发布不可变</span>
            <span><Icon name="bug" size={14} /> 调试隔离</span>
            <span><Icon name="git" size={14} /> 审计可回溯</span>
          </div>
        </section>

        <section className="login-panel" aria-labelledby="login-title">
          <div className="login-card">
            <div className="login-card-head">
              <div className="login-mobile-brand">
                <span className="brand-mark"><svg viewBox="0 0 32 32"><path d="M7 25 16 5l9 20z" /><path d="M11.4 19.5h9.2" /></svg></span>
                <strong>PanguVerse</strong>
              </div>
              <span className="login-kicker">SECURE SESSION</span>
              <h2 id="login-title">登录工作台</h2>
              <p>继续你的自动化编排与审阅。</p>
            </div>

            {expired ? (
              <div className="login-notice" role="status"><Icon name="clock" size={15} /> 登录已过期，请重新登录。当前页面意图已保留。</div>
            ) : notice ? (
              <div className="login-notice" role="status"><Icon name="shield" size={15} /> {notice}</div>
            ) : null}

            {error ? <div className="login-error" role="alert"><Icon name="warning" size={15} />{error}</div> : null}

            <form onSubmit={submit} className="login-form" noValidate>
              <label className="login-field">
                <span>账号</span>
                <div className="login-input-wrap">
                  <Icon name="user" size={16} />
                  <input
                    ref={userRef}
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    autoComplete="username"
                    placeholder="admin"
                    spellCheck={false}
                  />
                </div>
              </label>
              <label className="login-field">
                <span>密码</span>
                <div className="login-input-wrap">
                  <Icon name="lock" size={16} />
                  <input
                    ref={passwordRef}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    placeholder="输入访问密码"
                    spellCheck={false}
                  />
                  <button type="button" className="password-toggle" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? '隐藏密码' : '显示密码'}>
                    <Icon name="eye" size={16} />
                  </button>
                </div>
              </label>
              <Button type="submit" variant="primary" size="lg" loading={busy} className="login-submit">
                {busy ? '正在验证…' : '进入工作台'}
                {!busy ? <Icon name="arrow" size={17} /> : null}
              </Button>
            </form>

            <div className="login-meta">
              <span>会话 12 小时有效，服务端校验</span>
              <span className="mono">PANGUVERSE / WEB</span>
            </div>
          </div>

          <div className="login-foot">
            <span><span className="status-dot tone-ok" /> 本机服务</span>
            <span>凭据不写入浏览器</span>
          </div>
        </section>
      </div>
    </div>
  )
}
