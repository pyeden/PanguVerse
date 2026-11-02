import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Icon, type IconName } from './Icon'
import { classForStatus, copyText, markdown, pretty, statusLabel } from '../shared/utils'
import type { AnyRecord } from '../shared/types'

/* -------------------------------------------------------------------------- */
/* Buttons, badges and layout                                                 */
/* -------------------------------------------------------------------------- */

export function Button({
  children,
  variant = 'default',
  size = 'md',
  icon,
  loading = false,
  className = '',
  ...props
}: {
  children?: ReactNode
  variant?: 'default' | 'primary' | 'ghost' | 'danger' | 'soft'
  size?: 'sm' | 'md' | 'lg'
  icon?: IconName
  loading?: boolean
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`btn btn-${variant} btn-${size} ${className}`}
      {...props}
      disabled={props.disabled || loading}
    >
      {loading ? <span className="spinner spinner-sm" aria-hidden="true" /> : icon ? <Icon name={icon} size={size === 'sm' ? 15 : 17} /> : null}
      {children ? <span>{children}</span> : null}
    </button>
  )
}

export function IconButton({
  label,
  icon,
  className = '',
  ...props
}: { label: string; icon: IconName; className?: string } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button className={`icon-btn ${className}`} aria-label={label} title={label} {...props}>
      <Icon name={icon} size={18} />
    </button>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  dot = false,
  className = '',
}: {
  children: ReactNode
  tone?: 'neutral' | 'ok' | 'bad' | 'warn' | 'run' | 'ai' | 'human' | 'violet' | 'cyan'
  dot?: boolean
  className?: string
}) {
  return (
    <span className={`badge badge-${tone} ${className}`}>
      {dot ? <span className="badge-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  )
}

export function StatusBadge({ status, className = '' }: { status?: string; className?: string }) {
  const tone = classForStatus(status || 'none') as 'ok' | 'bad' | 'warn' | 'run' | 'dim'
  return (
    <span className={`badge status-badge tone-${tone} ${className}`}>
      <span className="status-dot" aria-hidden="true" />
      {statusLabel(status)}
    </span>
  )
}

export function OriginBadge({ origin }: { origin?: string }) {
  const ai = origin !== 'human'
  return (
    <span className={`origin-badge ${ai ? 'origin-ai' : 'origin-human'}`}>
      <Icon name={ai ? 'spark' : 'user'} size={11} />
      {ai ? 'AI' : '人工'}
    </span>
  )
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  className = '',
}: {
  eyebrow?: ReactNode
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  className?: string
}) {
  return (
    <header className={`page-header ${className}`}>
      <div className="page-header-copy">
        {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="page-header-actions">{actions}</div> : null}
    </header>
  )
}

export function SectionHeader({
  title,
  description,
  actions,
  icon,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  icon?: IconName
}) {
  return (
    <div className="section-header">
      <div className="section-title">
        {icon ? <span className="section-icon"><Icon name={icon} size={17} /></span> : null}
        <div>
          <h2>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
      </div>
      {actions ? <div className="section-actions">{actions}</div> : null}
    </div>
  )
}

export function Card({ children, className = '', ...props }: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLDivElement>) {
  return <div className={`card ${className}`} {...props}>{children}</div>
}

export function Panel({ children, className = '', ...props }: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLDivElement>) {
  return <section className={`panel ${className}`} {...props}>{children}</section>
}

export function EmptyState({
  icon = 'layers',
  title,
  children,
  actions,
}: {
  icon?: IconName
  title: string
  children?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon name={icon} size={26} /></div>
      <strong>{title}</strong>
      {children ? <p>{children}</p> : null}
      {actions ? <div className="empty-actions">{actions}</div> : null}
    </div>
  )
}

export function Loading({ label = '正在加载…' }: { label?: string }) {
  return (
    <div className="loading-block" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}

export function ErrorState({ error, onRetry, title = '读取失败' }: { error: unknown; onRetry?: () => void; title?: string }) {
  const message = error instanceof Error ? error.message : String(error || '未知错误')
  return (
    <div className="error-state" role="alert">
      <span className="error-icon"><Icon name="warning" size={20} /></span>
      <div>
        <strong>{title}</strong>
        <p>{message}</p>
      </div>
      {onRetry ? <Button size="sm" icon="refresh" onClick={onRetry}>重试</Button> : null}
    </div>
  )
}

export function Markdown({ value }: { value?: string | null }) {
  const html = useMemo(() => markdown(value), [value])
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
}

export function SourceBadge({ source, degradedReason }: { source?: string; degradedReason?: string }) {
  const isLlm = source === 'llm'
  return (
    <span className={`source-badge ${isLlm ? 'source-llm' : 'source-rules'}`} title={degradedReason || (isLlm ? '由模型生成，数字已由报告校正' : '离线规则生成，数字来自报告本体')}>
      <Icon name={isLlm ? 'spark' : 'code'} size={12} />
      {isLlm ? 'AI 总结 · LLM' : '规则总结'}
      {!isLlm && degradedReason ? <span className="source-note">降级</span> : null}
    </span>
  )
}

/* -------------------------------------------------------------------------- */
/* Search / tabs / stats                                                      */
/* -------------------------------------------------------------------------- */

export function SearchInput({
  value,
  onChange,
  placeholder = '搜索',
  ariaLabel,
}: {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  ariaLabel?: string
}) {
  return (
    <label className="search-input">
      <Icon name="search" size={16} />
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel || placeholder}
        autoComplete="off"
      />
      {value ? (
        <button type="button" className="search-clear" onClick={() => onChange('')} aria-label="清除搜索">
          <Icon name="close" size={13} />
        </button>
      ) : null}
    </label>
  )
}

export function Tabs({
  items,
  current,
}: {
  items: { id: string; label: string; href: string; count?: number }[]
  current: string
}) {
  return (
    <nav className="tabs" aria-label="页面分区">
      {items.map((item) => (
        <a key={item.id} href={item.href} className="tab" aria-current={current === item.id ? 'page' : undefined}>
          {item.label}
          {item.count !== undefined ? <span className="tab-count">{item.count}</span> : null}
        </a>
      ))}
    </nav>
  )
}

export function Kpi({
  label,
  value,
  sub,
  tone = '',
  icon,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: string
  icon?: IconName
}) {
  return (
    <div className="kpi">
      <div className="kpi-head">
        <span>{label}</span>
        {icon ? <Icon name={icon} size={16} /> : null}
      </div>
      <strong className={`kpi-value ${tone}`}>{value}</strong>
      {sub ? <div className="kpi-sub">{sub}</div> : null}
    </div>
  )
}

export function Progress({ value, tone = '' }: { value: number; tone?: string }) {
  return (
    <div className={`progress ${tone}`}>
      <span style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Modal and drawer                                                           */
/* -------------------------------------------------------------------------- */

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  size = 'md',
  dismissible = true,
}: {
  open: boolean
  onClose: () => void
  title?: ReactNode
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl'
  dismissible?: boolean
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && dismissible) onClose()
    }
    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [open, dismissible, onClose])

  if (!open) return null
  return createPortal(
    <div className="modal-layer" role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : '对话框'}>
      <button type="button" className="modal-backdrop" onClick={() => dismissible && onClose()} aria-label="关闭对话框" />
      <div className={`modal-card modal-${size}`}>
        {title ? (
          <div className="modal-head">
            <div>
              <h2>{title}</h2>
              {subtitle ? <p>{subtitle}</p> : null}
            </div>
            <IconButton label="关闭" icon="close" onClick={onClose} />
          </div>
        ) : null}
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>,
    document.body,
  )
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  tone = 'default',
  width = 'drawer-md',
}: {
  open: boolean
  onClose: () => void
  title?: ReactNode
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
  tone?: 'default' | 'debug'
  width?: string
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return createPortal(
    <div className={`drawer-layer drawer-${tone}`}>
      <button type="button" className="drawer-backdrop" aria-label="关闭面板" onClick={onClose} />
      <aside className={`drawer ${width}`} role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : '侧面板'}>
        <div className="drawer-head">
          <div>
            <h2>{title}</h2>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <IconButton label="关闭" icon="close" onClick={onClose} />
        </div>
        <div className="drawer-body">{children}</div>
        {footer ? <div className="drawer-foot">{footer}</div> : null}
      </aside>
    </div>,
    document.body,
  )
}

/* -------------------------------------------------------------------------- */
/* Toast                                                                      */
/* -------------------------------------------------------------------------- */

type ToastTone = 'info' | 'ok' | 'error' | 'warn'
interface ToastItem { id: number; message: string; tone: ToastTone }

const ToastContext = createContext<{ push: (message: string, tone?: ToastTone) => void }>({ push: () => undefined })

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const push = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = Date.now() + Math.random()
    setItems((current) => [...current, { id, message, tone }])
    window.setTimeout(() => setItems((current) => current.filter((item) => item.id !== id)), tone === 'error' ? 7000 : 4200)
  }, [])
  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="toast-stack" aria-live="polite" aria-atomic="false">
        {items.map((item) => (
          <div key={item.id} className={`toast toast-${item.tone}`}>
            <Icon name={item.tone === 'error' ? 'warning' : item.tone === 'ok' ? 'check' : 'bolt'} size={16} />
            <span>{item.message}</span>
            <button type="button" onClick={() => setItems((current) => current.filter((entry) => entry.id !== item.id))} aria-label="关闭提示">
              <Icon name="close" size={13} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  return useContext(ToastContext)
}

/* -------------------------------------------------------------------------- */
/* Confirm dialog helper                                                      */
/* -------------------------------------------------------------------------- */

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = '确认',
  danger = false,
  busy = false,
  onConfirm,
  onClose,
}: {
  open: boolean
  title: string
  message: ReactNode
  confirmLabel?: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>取消</Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm} loading={busy}>{confirmLabel}</Button>
        </>
      }
    >
      <div className="confirm-body">{message}</div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* JSON tree                                                                  */
/* -------------------------------------------------------------------------- */

export function JsonTree({ data, name = 'root', depth = 0 }: { data: unknown; name?: string; depth?: number }) {
  const [open, setOpen] = useState(depth < 2)
  if (data === null || data === undefined) {
    return <div className="json-leaf"><span className="json-key">{name}</span><span className="json-null">null</span></div>
  }
  if (typeof data !== 'object') {
    const type = typeof data
    return (
      <div className="json-leaf">
        <span className="json-key">{name}</span>
        <span className={`json-value json-${type}`}>{type === 'string' ? JSON.stringify(data) : String(data)}</span>
      </div>
    )
  }
  const isArray = Array.isArray(data)
  const entries = Object.entries(data as AnyRecord)
  return (
    <div className={`json-node ${open ? 'is-open' : ''}`}>
      <button type="button" className="json-toggle" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <Icon name="chevron" size={12} />
        <span className="json-key">{name}</span>
        <span className="json-bracket">{isArray ? '[' : '{'}</span>
        {!open ? <span className="json-summary">{entries.length} {isArray ? '项' : '字段'}</span> : null}
        {!open ? <span className="json-bracket">{isArray ? ']' : '}'}</span> : null}
      </button>
      {open ? (
        <div className="json-children">
          {entries.map(([key, value]) => (
            <JsonTree key={key} data={value} name={isArray ? `[${key}]` : key} depth={depth + 1} />
          ))}
          {!entries.length ? <span className="json-empty">空</span> : null}
        </div>
      ) : null}
    </div>
  )
}

export function JsonBlock({ data, maxHeight }: { data: unknown; maxHeight?: number }) {
  const { push } = useToast()
  return (
    <div className="json-block" style={maxHeight ? { maxHeight } : undefined}>
      <button
        type="button"
        className="copy-float"
        onClick={() => copyText(pretty(data)).then(() => push('已复制 JSON', 'ok')).catch(() => push('复制失败', 'error'))}
      >
        <Icon name="copy" size={13} /> 复制
      </button>
      <JsonTree data={data} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Environment editor                                                         */
/* -------------------------------------------------------------------------- */

interface EnvRow { key: string; value: string; existing: boolean }

export function EnvEditor({
  value,
  onChange,
  hint,
}: {
  value: AnyRecord
  onChange: (value: AnyRecord) => void
  hint?: string
}) {
  const initial = useMemo<EnvRow[]>(
    () => Object.entries(value || {}).map(([key, entry]) => ({
      key,
      value: entry === '***' ? '' : String(entry ?? ''),
      existing: true,
    })),
    [value],
  )
  const [rows, setRows] = useState<EnvRow[]>(initial)
  useEffect(() => setRows(initial), [initial])

  const commit = (next: EnvRow[]) => {
    setRows(next)
    const output: AnyRecord = {}
    next.forEach((row) => {
      const key = row.key.trim()
      if (!key) return
      if (row.value !== '') output[key] = row.value
      else if (row.existing) output[key] = '***'
    })
    onChange(output)
  }

  return (
    <div className="env-editor">
      {rows.map((row, index) => (
        <div className="env-row" key={`${row.key}-${index}`}>
          <input
            className="input mono"
            value={row.key}
            placeholder="变量名"
            spellCheck={false}
            readOnly={row.existing}
            onChange={(event) => commit(rows.map((item, i) => i === index ? { ...item, key: event.target.value } : item))}
          />
          <input
            className="input mono"
            type="password"
            value={row.value}
            placeholder={row.existing ? '***（留空沿用）' : '值'}
            autoComplete="new-password"
            onChange={(event) => commit(rows.map((item, i) => i === index ? { ...item, value: event.target.value } : item))}
          />
          <IconButton
            label="删除变量"
            icon="close"
            onClick={() => commit(rows.filter((_, i) => i !== index))}
          />
        </div>
      ))}
      <div className="env-actions">
        <Button size="sm" variant="ghost" icon="plus" onClick={() => setRows((current) => [...current, { key: '', value: '', existing: false }])}>
          添加变量
        </Button>
        {hint ? <span>{hint}</span> : null}
      </div>
    </div>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  children: ReactNode
  htmlFor?: string
}) {
  return (
    <div className={`field ${error ? 'field-error' : ''}`}>
      <label htmlFor={htmlFor} className="field-label">{label}</label>
      {children}
      {error ? <span className="field-message">{error}</span> : hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  )
}

export function Select({
  children,
  className = '',
  ...props
}: { children: ReactNode } & React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`select ${className}`} {...props}>{children}</select>
}
