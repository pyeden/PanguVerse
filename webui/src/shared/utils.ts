import type { AnyRecord, RunSummary } from './types'

export function cx(...values: (string | false | null | undefined)[]): string {
  return values.filter(Boolean).join(' ')
}

export function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

export function text(value: unknown): string {
  if (value === undefined || value === null) return ''
  return String(value)
}

export function escapeMarkdown(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/`/g, '&#96;')
}

/** Tiny, allow-list markdown renderer. Model output is escaped first. */
export function markdown(value: string | undefined | null): string {
  if (!value) return ''
  const escaped = escapeMarkdown(value)
  const lines = escaped.split(/\r?\n/)
  const out: string[] = []
  let list: 'ul' | 'ol' | null = null
  const closeList = () => {
    if (list) out.push(`</${list}>`)
    list = null
  }
  const inline = (line: string) =>
    line
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
  for (const line of lines) {
    if (!line.trim()) {
      closeList()
      continue
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      closeList()
      const level = Math.min(4, heading[1].length + 1)
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`)
      continue
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/)
    if (bullet) {
      if (list !== 'ul') {
        closeList()
        out.push('<ul>')
        list = 'ul'
      }
      out.push(`<li>${inline(bullet[1])}</li>`)
      continue
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (ordered) {
      if (list !== 'ol') {
        closeList()
        out.push('<ol>')
        list = 'ol'
      }
      out.push(`<li>${inline(ordered[1])}</li>`)
      continue
    }
    closeList()
    out.push(`<p>${inline(line)}</p>`)
  }
  closeList()
  return out.join('')
}

export function toMs(timestamp: unknown): number {
  const value = num(timestamp)
  if (!value) return 0
  return value > 100_000_000_000 ? value : value * 1000
}

export function formatTime(timestamp: unknown): string {
  const ms = toMs(timestamp)
  if (!ms) return '—'
  const date = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ${p(date.getHours())}:${p(date.getMinutes())}`
}

export function relativeTime(timestamp: unknown): string {
  const ms = toMs(timestamp)
  if (!ms) return '—'
  const delta = Date.now() - ms
  if (delta < 0) return formatTime(timestamp)
  if (delta < 60_000) return '刚刚'
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`
  if (delta < 604_800_000) return `${Math.floor(delta / 86_400_000)} 天前`
  return formatTime(timestamp)
}

export function formatDuration(ms: unknown): string {
  const value = num(ms, -1)
  if (value < 0) return '—'
  if (value < 1) return `${value.toFixed(2)} ms`
  if (value < 1000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(2)} s`
  const minutes = Math.floor(value / 60_000)
  const seconds = Math.round((value % 60_000) / 1000)
  return `${minutes} 分 ${seconds} 秒`
}

export function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export function isObject(value: unknown): value is AnyRecord {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

export function statusOf(run: RunSummary | null | undefined): string {
  if (!run) return 'none'
  const summary = run.summary || {}
  if (num(summary.errored) > 0) return 'errored'
  if (num(summary.failed) > 0) return 'failed'
  if (num(summary.total) > 0) return 'passed'
  if (run.conclusion && run.conclusion.success === false) return 'failed'
  if (run.conclusion && run.conclusion.success === true) return 'passed'
  return run.status || run.last_status || 'none'
}

export function rateOf(passed: unknown, total: unknown): number | null {
  const p = num(passed)
  const t = num(total)
  return t > 0 ? Math.round((p / t) * 1000) / 10 : null
}

export function runCounts(run: RunSummary | null | undefined): { passed: number; total: number } {
  const summary = run?.summary || {}
  return {
    passed: num(summary.passed, num(run?.passed)),
    total: num(summary.total, num(run?.total)),
  }
}

export function runWhen(run: RunSummary | null | undefined): number {
  return num(run?.at || run?.finished_at || run?.started_at)
}

export function shortId(value: string | undefined, length = 8): string {
  if (!value) return '—'
  return value.length > length ? value.slice(0, length) : value
}

export function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

export async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value)
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  document.execCommand('copy')
  textarea.remove()
}

export function classForStatus(status: string): string {
  const map: Record<string, string> = {
    passed: 'ok',
    ok: 'ok',
    done: 'ok',
    success: 'ok',
    failed: 'bad',
    error: 'bad',
    errored: 'bad',
    running: 'run',
    queued: 'dim',
    pending: 'dim',
    skipped: 'dim',
    none: 'dim',
    partial: 'warn',
  }
  return map[status] || 'dim'
}

export function statusLabel(status: string | undefined): string {
  const map: Record<string, string> = {
    passed: '通过',
    ok: '正常',
    done: '已完成',
    success: '成功',
    failed: '失败',
    error: '异常',
    errored: '异常',
    running: '运行中',
    queued: '排队中',
    pending: '待开始',
    skipped: '跳过',
    partial: '部分失败',
    none: '未运行',
    draft: '草案',
    review: '待审阅',
    approved: '已批准',
    rejected: '已拒绝',
    published: '已发布',
    deprecated: '已归档',
    cancelled: '已取消',
  }
  return map[status || 'none'] || status || '未知'
}

export function errorText(value: unknown): string {
  if (value === null || value === undefined || value === '') return ''
  if (typeof value === 'string') return value
  if (typeof value === 'object') {
    const row = value as AnyRecord
    return String(row.message || row.detail || row.error || row.type || row.failure_kind || pretty(value))
  }
  return String(value)
}
