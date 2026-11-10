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
    document.title = '登录 · PanguVerse'
  }, [])

  // 只在首次挂载时决定焦点：跟随 username 会在每次输入后把焦点抢到密码框
  const focusedOnce = useRef(false)
  useEffect(() => {
    if (focusedOnce.current) return
    focusedOnce.current = true
    if (!username) userRef.current?.focus()
    else passwordRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
      if (apiError.status === 429) {
        const retrySeconds = Number(apiError.retryAfterSeconds || 0)
        setError(retrySeconds > 0
          ? `尝试过多，请 ${Math.max(1, Math.ceil(retrySeconds / 60))} 分钟后再试`
          : apiError.message || '尝试过多，请稍后再试')
      } else {
        setError(apiError.message || '登录失败')
      }
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

      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-card">
          <div className="login-card-head">
            <a className="login-brand" href="#/overview">
              <span className="brand-mark" aria-hidden="true">
                <svg viewBox="0 0 32 32"><path d="M7 25 16 5l9 20z" /><path d="M11.4 19.5h9.2" /></svg>
              </span>
              <span><strong>PanguVerse</strong><small>原子工作台</small></span>
            </a>
            <h2 id="login-title">登录工作台</h2>
          </div>

          {expired ? (
            <div className="login-notice" role="status"><Icon name="clock" size={15} /> 登录已过期，请重新登录。</div>
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
      </section>
    </div>
  )
}
