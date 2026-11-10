/**
 * 任务执行态：调试（debug）/ 固化（frozen）。
 *
 * 旧任务与旧报告没有 execution_mode 字段时按 debug 处理；前端只读展示，
 * 真正的门禁（LLM 不改图）由后端运行守卫负责。
 */
export type ExecutionMode = 'debug' | 'frozen'

export interface ExecutionModeSource {
  execution_mode?: unknown
  frozen_ref?: unknown
  current_rev?: number
  rev?: number
  wid?: string
}

export interface RunModeSource {
  status?: string
  run_status?: string
  conclusion?: unknown
}

export const EXECUTION_MODE_COPY = {
  debug: {
    label: '调试',
    title: '调试模式',
    hint: 'LLM 可自定义节点；跑通后固化',
    runHint: 'LLM 可改图',
  },
  frozen: {
    label: '已固化',
    title: '生产模式 · 已固化',
    hint: '确定性执行，不调 LLM 改图',
    runHint: '确定性执行',
  },
} as const

export function executionModeOf(value?: ExecutionModeSource | null): ExecutionMode {
  return String(value?.execution_mode || '').trim().toLowerCase() === 'frozen' ? 'frozen' : 'debug'
}

export function frozenRefOf(workflow?: ExecutionModeSource | null): string {
  if (!workflow) return ''
  const explicit = typeof workflow.frozen_ref === 'string' ? workflow.frozen_ref.trim() : ''
  if (explicit) return explicit
  if (executionModeOf(workflow) !== 'frozen') return ''
  const wid = String(workflow.wid || '')
  const rev = Number(workflow.current_rev ?? workflow.rev ?? 0)
  return wid ? `${wid}@${rev}` : ''
}

const SUCCESS_STATUSES = new Set(['done', 'passed', 'success', 'succeeded', 'ok', 'completed'])

export function runSucceeded(run?: RunModeSource | null): boolean {
  if (!run) return false
  const status = String(run.status || run.run_status || '').trim().toLowerCase()
  if (SUCCESS_STATUSES.has(status)) return true
  const conclusion = run.conclusion
  return !!conclusion && typeof conclusion === 'object' && (conclusion as { success?: unknown }).success === true
}

export function runStatusLabel(run?: RunModeSource | null): string {
  if (!run) return '未运行'
  const status = String(run.status || run.run_status || '').trim().toLowerCase()
  if (runSucceeded(run)) return '成功'
  if (status === 'failed') return '失败'
  if (status === 'cancelled') return '已取消'
  if (status === 'running') return '运行中'
  if (status === 'queued') return '排队中'
  return status || '未知'
}
