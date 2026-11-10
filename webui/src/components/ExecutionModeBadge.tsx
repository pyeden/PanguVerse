import { Badge } from './ui'
import { Icon } from './Icon'
import {
  EXECUTION_MODE_COPY,
  executionModeOf,
  frozenRefOf,
  type ExecutionMode,
  type ExecutionModeSource,
} from '../shared/executionMode'

/** 任务/运行共用：调试 / 已固化模式徽标；已固化显示 wid@rev。 */
export function ExecutionModeBadge({
  mode = 'debug',
  refText = '',
  run = false,
  className = '',
}: {
  mode?: ExecutionMode
  refText?: string
  run?: boolean
  className?: string
}) {
  const copy = EXECUTION_MODE_COPY[mode]
  if (mode === 'frozen') {
    return (
      <Badge tone="ok" dot className={`execution-mode-badge is-frozen ${className}`}>
        <Icon name="lock" size={10} />
        <span>{copy.label}</span>
        {refText ? <code className="execution-mode-ref" title={refText}>{refText}</code> : null}
        {run ? <em className="execution-mode-run-note">{copy.runHint}</em> : null}
      </Badge>
    )
  }
  return (
    <Badge tone="warn" dot className={`execution-mode-badge is-debug ${className}`}>
      <Icon name="bug" size={10} />
      <span>{copy.label}</span>
      {run ? <em className="execution-mode-run-note">{copy.runHint}</em> : null}
    </Badge>
  )
}

export function ExecutionModeBadgeForWorkflow({
  workflow,
  run = false,
  className = '',
}: {
  workflow?: ExecutionModeSource | null
  run?: boolean
  className?: string
}) {
  const mode = executionModeOf(workflow)
  return <ExecutionModeBadge mode={mode} refText={frozenRefOf(workflow)} run={run} className={className} />
}
