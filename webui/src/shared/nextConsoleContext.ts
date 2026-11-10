import type { NxPlanStep } from './nextMock'

/** 控制台草稿的会话级上下文；只存生成成功后的最小恢复信息。 */
export interface SavedConsoleContext {
  wid: string
  steps: NxPlanStep[]
  source: string
  intent: string
  at: number
}

const STORAGE_KEY = 'panguverse:next-console:context:v1'
const STEP_STATUSES = new Set<NxPlanStep['status']>(['done', 'running', 'fixing', 'queued'])

function sanitizeStep(value: unknown, index: number): NxPlanStep | null {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const id = typeof row.id === 'string' && row.id ? row.id : `step-${index + 1}`
  const title = typeof row.title === 'string' && row.title ? row.title : id
  const atom = typeof row.atom === 'string' ? row.atom : ''
  const kind: NxPlanStep['kind'] = row.kind === 'custom' ? 'custom' : 'atom'
  const risk: NxPlanStep['risk'] = row.risk === 'L1' || row.risk === 'L2' ? row.risk : 'L0'
  const status: NxPlanStep['status'] = STEP_STATUSES.has(row.status as NxPlanStep['status'])
    ? (row.status as NxPlanStep['status'])
    : 'queued'
  const note = typeof row.note === 'string' ? row.note : ''
  return { id, title, atom, kind, risk, status, note }
}

export function loadConsoleContext(): SavedConsoleContext | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const data = JSON.parse(raw) as Record<string, unknown>
    const wid = typeof data.wid === 'string' ? data.wid : ''
    const steps = Array.isArray(data.steps)
      ? data.steps.map(sanitizeStep).filter((step): step is NxPlanStep => !!step)
      : []
    if (!wid || !steps.length) return null
    return {
      wid,
      steps,
      source: typeof data.source === 'string' ? data.source : '',
      intent: typeof data.intent === 'string' ? data.intent : '',
      at: typeof data.at === 'number' && Number.isFinite(data.at) ? data.at : 0,
    }
  } catch {
    return null
  }
}

export function saveConsoleContext(context: Omit<SavedConsoleContext, 'at'> & { at?: number }): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
      wid: context.wid,
      steps: context.steps,
      source: context.source,
      intent: context.intent,
      at: context.at || Date.now(),
    }))
  } catch {
    // sessionStorage 不可用时不影响真实规划流程。
  }
}

export function clearConsoleContext(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}
