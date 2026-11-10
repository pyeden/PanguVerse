import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api, studioApi } from '../shared/api'
import { href, navigate, withQuery } from '../shared/router'
import { copyText, formatDuration, formatTime, relativeUntil } from '../shared/utils'
import type {
  AnyRecord,
  Execution,
  TriggerKind,
  WorkflowTrigger,
  WorkflowTriggerCreate,
} from '../shared/types'
import { Icon } from './Icon'
import {
  Badge,
  Button,
  ConfirmDialog,
  Field,
  IconButton,
  Loading,
  Modal,
  SectionHeader,
  StatusBadge,
  useToast,
} from './ui'

const KIND_LABEL: Record<string, string> = {
  schedule: '定时',
  interval: '间隔',
  webhook: 'Webhook',
  manual: '手动',
}

const KIND_TONE: Record<string, 'cyan' | 'human' | 'ai' | 'neutral'> = {
  schedule: 'cyan',
  interval: 'human',
  webhook: 'ai',
  manual: 'neutral',
}

const DEFAULT_TZ = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'
  } catch {
    return 'Asia/Shanghai'
  }
})()

function kindLabel(trigger: WorkflowTrigger): string {
  // Backend kind 只有 schedule|webhook|manual；interval 是 schedule 的规则形态。
  if (trigger.kind === 'schedule' && trigger.interval) return '间隔'
  return KIND_LABEL[trigger.kind] || trigger.kind || '未知'
}

function triggerMs(value: unknown): number {
  if (value === null || value === undefined || value === '') return 0
  if (typeof value === 'number') return value > 100_000_000_000 ? value : value * 1000
  const parsed = Date.parse(String(value))
  return Number.isFinite(parsed) ? parsed : 0
}

function nextRunText(value: unknown): string {
  const ms = triggerMs(value)
  if (!ms) return '—'
  return ms > Date.now() ? `${relativeUntil(ms)} · ${formatTime(ms)}` : formatTime(ms)
}

function triggerRule(trigger: WorkflowTrigger): string {
  if (trigger.kind === 'schedule') {
    const cron = trigger.schedule?.cron || '未设置 cron'
    return trigger.schedule?.tz ? `${cron} · ${trigger.schedule.tz}` : cron
  }
  if (trigger.kind === 'interval') {
    const minutes = Number(trigger.interval?.minutes || 0)
    return minutes ? `每 ${minutes} 分钟` : '未设置间隔'
  }
  if (trigger.kind === 'webhook') {
    return trigger.webhook?.token_hint ? `Token ${trigger.webhook.token_hint}` : 'Token 已配置'
  }
  return '—'
}

export interface TriggerPanelProps {
  wid: string
  /** Published workflow ref. Required by the backend when creating triggers. */
  workflowRef?: string
  compact?: boolean
  /** Hide the built-in create buttons when the host page owns the primary actions. */
  showCreateActions?: boolean
  /** External request to open the create dialog, e.g. from the publish success card. */
  createSignal?: { kind: TriggerKind; seq: number } | null
}

export function TriggerPanel({ wid, workflowRef = '', compact = false, showCreateActions = true, createSignal = null }: TriggerPanelProps) {
  const queryClient = useQueryClient()
  const { push } = useToast()
  const [createOpen, setCreateOpen] = useState(false)
  const [createKind, setCreateKind] = useState<'schedule' | 'webhook'>('schedule')
  const [scheduleMode, setScheduleMode] = useState<'cron' | 'interval'>('cron')
  const [cron, setCron] = useState('0 9 * * 1-5')
  const [tz, setTz] = useState(DEFAULT_TZ)
  const [minutes, setMinutes] = useState('60')
  const [envText, setEnvText] = useState('')
  const [formError, setFormError] = useState('')
  const [tokenReveal, setTokenReveal] = useState<{ token: string; kind: string; triggerId: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<WorkflowTrigger | null>(null)
  const [resetTarget, setResetTarget] = useState<WorkflowTrigger | null>(null)
  const [lastRun, setLastRun] = useState<{ triggerId: string; jobId: string } | null>(null)

  const triggersQuery = useQuery({
    queryKey: ['workflow-triggers', wid],
    queryFn: () => studioApi.listTriggers(wid),
    enabled: !!wid,
    retry: false,
    refetchInterval: 15000,
  })
  const triggers = triggersQuery.data || []
  const unsupported = triggersQuery.error instanceof ApiError && triggersQuery.error.status === 404
  const canCreate = !!workflowRef

  useEffect(() => {
    if (!createSignal) return
    setCreateKind(createSignal.kind === 'webhook' ? 'webhook' : 'schedule')
    setScheduleMode(createSignal.kind === 'interval' ? 'interval' : 'cron')
    setCreateOpen(true)
    setFormError('')
  }, [createSignal?.seq])

  const resetForm = () => {
    setFormError('')
    setEnvText('')
    setCron('0 9 * * 1-5')
    setTz(DEFAULT_TZ)
    setMinutes('60')
    setScheduleMode('cron')
  }

  const openCreate = (kind: TriggerKind) => {
    resetForm()
    setCreateKind(kind === 'webhook' ? 'webhook' : 'schedule')
    setScheduleMode(kind === 'interval' ? 'interval' : 'cron')
    setCreateOpen(true)
  }

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['workflow-triggers', wid] })
  }

  const createTrigger = useMutation({
    mutationFn: () => {
      if (!canCreate) throw new Error('任务尚未发布，无法创建触发器')
      const body: WorkflowTriggerCreate = { workflow_ref: workflowRef, kind: createKind }
      if (createKind === 'schedule') {
        if (scheduleMode === 'cron') {
          if (!cron.trim()) throw new Error('请填写 cron 表达式')
          body.schedule = { cron: cron.trim(), tz: tz.trim() || undefined }
        } else {
          const value = Number(minutes)
          if (!Number.isFinite(value) || value < 1) throw new Error('间隔至少为 1 分钟')
          body.interval = { minutes: Math.floor(value) }
        }
      }
      const raw = envText.trim()
      if (raw) {
        try {
          const parsed = JSON.parse(raw)
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not-an-object')
          body.env_override = parsed as AnyRecord
        } catch {
          throw new Error('环境变量覆盖必须是 JSON 对象，例如 {"REGION":"cn"}')
        }
      }
      return studioApi.createTrigger(wid, body)
    },
    onSuccess: (result) => {
      invalidate()
      setCreateOpen(false)
      resetForm()
      if (result.token) {
        setTokenReveal({ token: result.token, kind: createKind, triggerId: result.trigger.trigger_id })
      }
      push(createKind === 'webhook' ? 'Webhook 触发器已创建' : '定时触发器已创建', 'ok')
    },
    onError: (error: Error) => setFormError(error.message),
  })

  const toggleTrigger = useMutation({
    mutationFn: (trigger: WorkflowTrigger) =>
      studioApi.updateTrigger(trigger.trigger_id, { enabled: !trigger.enabled }),
    onSuccess: (_result, trigger) => {
      invalidate()
      push(trigger.enabled ? '触发器已停用' : '触发器已启用', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const runTrigger = useMutation({
    mutationFn: (trigger: WorkflowTrigger) => studioApi.runTrigger(trigger.trigger_id),
    onSuccess: (result, trigger) => {
      invalidate()
      if (result?.status === 'skipped_overlap') {
        push('上一次运行仍在进行，本次已跳过', 'warn')
      } else if (result?.job_id) {
        setLastRun({ triggerId: trigger.trigger_id, jobId: result.job_id })
        push(`已开始运行 · ${result.job_id}`, 'ok')
      } else {
        push('已提交运行', 'ok')
      }
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const deleteTrigger = useMutation({
    mutationFn: (trigger: WorkflowTrigger) => studioApi.deleteTrigger(trigger.trigger_id),
    onSuccess: () => {
      invalidate()
      setDeleteTarget(null)
      push('触发器已删除', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const regenerateToken = useMutation({
    mutationFn: (trigger: WorkflowTrigger) => studioApi.regenerateTriggerToken(trigger.trigger_id),
    onSuccess: (result, trigger) => {
      invalidate()
      setResetTarget(null)
      if (result.token) {
        setTokenReveal({ token: result.token, kind: 'webhook', triggerId: trigger.trigger_id })
      }
      push('Webhook Token 已重置，旧 URL 立即失效', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const webhookUrl = tokenReveal ? `${window.location.origin}/api/hooks/workflow/${tokenReveal.token}` : ''

  const copyWebhook = async (value: string, label: string) => {
    try {
      await copyText(value)
      push(`${label}已复制`, 'ok')
    } catch {
      push('复制失败，请手动选择文本', 'error')
    }
  }

  const headActions = (
    <div className="trigger-panel-actions">
      {showCreateActions ? (
        <>
          <Button
            size="sm"
            variant="ghost"
            icon="clock"
            disabled={!canCreate || unsupported}
            title={!canCreate ? '任务发布后才能创建触发器' : unsupported ? '当前服务未启用触发器接口' : '创建定时触发器'}
            onClick={() => openCreate('schedule')}
          >创建定时</Button>
          <Button
            size="sm"
            variant="ghost"
            icon="link"
            disabled={!canCreate || unsupported}
            title={!canCreate ? '任务发布后才能创建触发器' : unsupported ? '当前服务未启用触发器接口' : '创建 Webhook 触发器'}
            onClick={() => openCreate('webhook')}
          >创建 Webhook</Button>
        </>
      ) : null}
      <IconButton label="刷新触发器" icon="refresh" onClick={() => triggersQuery.refetch()} />
    </div>
  )

  const body = (
    <>
      {!canCreate && !unsupported ? (
        <div className="trigger-hint"><Icon name="warning" size={13} />该任务尚未发布。发布后可创建定时或 Webhook 触发器。</div>
      ) : null}

      {tokenReveal ? (
        <div className="trigger-token" role="status" data-testid="trigger-token-reveal">
          <span className="trigger-token-icon"><Icon name="warning" size={15} /></span>
          <div className="trigger-token-copy">
            <strong>Webhook Token 只显示一次，请立即保存</strong>
            <code className="trigger-token-value">{tokenReveal.token}</code>
            <span className="mono trigger-token-url">{webhookUrl}</span>
            <small>关闭后无法再次查看；如需更换，可在触发器行内「重置 Token」。</small>
          </div>
          <div className="trigger-token-actions">
            <Button size="sm" variant="ghost" icon="copy" onClick={() => void copyWebhook(webhookUrl, 'Webhook URL')}>复制 URL</Button>
            <Button size="sm" variant="ghost" icon="copy" onClick={() => void copyWebhook(tokenReveal.token, 'Token')}>复制 Token</Button>
            <IconButton label="我已保存" icon="close" onClick={() => setTokenReveal(null)} />
          </div>
        </div>
      ) : null}

      {lastRun ? (
        <div className="trigger-run-note" data-testid="trigger-run-note">
          <Icon name="play" size={13} />
          <span>已触发运行 · <code>{lastRun.jobId}</code></span>
          <button type="button" onClick={() => navigate(withQuery('/tasks/config', { job: lastRun.jobId }))}>查看运行 →</button>
          <IconButton label="关闭提示" icon="close" onClick={() => setLastRun(null)} />
        </div>
      ) : null}

      {unsupported ? (
        <div className="trigger-empty" data-testid="trigger-unsupported">
          <Icon name="warning" size={20} />
          <strong>当前服务未启用触发器接口（404）</strong>
          <p>发布与画布能力不受影响；可继续使用原有「任务」入口运行，或升级后端后刷新本页。</p>
        </div>
      ) : triggersQuery.isLoading ? (
        <Loading label="正在读取触发器…" />
      ) : triggersQuery.error ? (
        <div className="trigger-empty">
          <Icon name="warning" size={20} />
          <strong>触发器读取失败</strong>
          <p>{triggersQuery.error.message}</p>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => triggersQuery.refetch()}>重试</Button>
        </div>
      ) : !triggers.length ? (
        <div className="trigger-empty">
          <Icon name="clock" size={20} />
          <strong>还没有触发器</strong>
          <p>创建定时或 Webhook 后，可在本页直接启用、停用、立即运行。</p>
        </div>
      ) : (
        <div className="trigger-list">
          {triggers.map((trigger) => {
            const toggling = toggleTrigger.isPending && toggleTrigger.variables?.trigger_id === trigger.trigger_id
            const running = runTrigger.isPending && runTrigger.variables?.trigger_id === trigger.trigger_id
            return (
              <article className={`trigger-item ${trigger.enabled ? '' : 'is-disabled'}`} key={trigger.trigger_id} data-testid={`trigger-${trigger.trigger_id}`}>
                <div className="trigger-item-main">
                  <div className="trigger-item-head">
                    <Badge tone={KIND_TONE[trigger.kind] || 'neutral'}>{kindLabel(trigger)}</Badge>
                    <code className="trigger-id">{trigger.trigger_id}</code>
                    <StatusBadge status={trigger.last_status || 'none'} />
                    {!trigger.enabled ? <Badge tone="neutral">已停用</Badge> : null}
                  </div>
                  <div className="trigger-item-meta">
                    <span className="trigger-rule"><Icon name="clock" size={12} />{triggerRule(trigger)}</span>
                    <span>下次运行 <strong>{nextRunText(trigger.next_run_at)}</strong></span>
                    {trigger.last_job_id ? <span className="mono">上次 {trigger.last_job_id}</span> : null}
                  </div>
                </div>
                <div className="trigger-item-actions">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={trigger.enabled}
                    className={`trigger-switch ${trigger.enabled ? 'is-on' : ''}`}
                    disabled={toggling}
                    title={trigger.enabled ? '停用触发器' : '启用触发器'}
                    onClick={() => toggleTrigger.mutate(trigger)}
                  >
                    <span className="trigger-switch-knob" />
                    {trigger.enabled ? '已启用' : '已停用'}
                  </button>
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="play"
                    loading={running}
                    onClick={() => runTrigger.mutate(trigger)}
                  >立即运行</Button>
                  {trigger.kind === 'webhook' ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon="refresh"
                      loading={regenerateToken.isPending && regenerateToken.variables?.trigger_id === trigger.trigger_id}
                      onClick={() => setResetTarget(trigger)}
                    >重置 Token</Button>
                  ) : null}
                  <Button size="sm" variant="ghost" icon="trash" onClick={() => setDeleteTarget(trigger)}>删除</Button>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </>
  )

  return (
    <section className={`panel trigger-panel ${compact ? 'is-compact' : ''}`} data-testid="trigger-panel">
      {compact ? (
        <div className="trigger-panel-head">
          <div>
            <span className="trigger-kicker">触发器</span>
            <h3>{triggers.length ? `${triggers.length} 个触发器` : '发布后可直接运行'}</h3>
          </div>
          {headActions}
        </div>
      ) : (
        <SectionHeader
          title="触发器"
          description="定时 / 间隔 / Webhook；运行记录见「运行」页。"
          icon="clock"
          actions={headActions}
        />
      )}
      {body}

      <Modal
        open={createOpen}
        onClose={() => { if (!createTrigger.isPending) setCreateOpen(false) }}
        title={createKind === 'webhook' ? '创建 Webhook 触发器' : '创建定时触发器'}
        subtitle={workflowRef ? `绑定 ${workflowRef}` : '任务尚未发布'}
        size="md"
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)} disabled={createTrigger.isPending}>取消</Button>
            <Button
              variant="primary"
              icon="plus"
              loading={createTrigger.isPending}
              disabled={!canCreate || unsupported}
              onClick={() => createTrigger.mutate()}
            >创建触发器</Button>
          </>
        }
      >
        <div className="trigger-form">
          <div className="segmented trigger-kind-tabs" role="tablist" aria-label="触发器类型">
            <button
              type="button"
              role="tab"
              aria-selected={createKind !== 'webhook'}
              className={createKind !== 'webhook' ? 'is-active' : ''}
              onClick={() => { setCreateKind('schedule'); setScheduleMode('cron'); setFormError('') }}
            >定时</button>
            <button
              type="button"
              role="tab"
              aria-selected={createKind === 'webhook'}
              className={createKind === 'webhook' ? 'is-active' : ''}
              onClick={() => { setCreateKind('webhook'); setFormError('') }}
            >Webhook</button>
          </div>

          {createKind === 'webhook' ? (
            <div className="trigger-hint">
              <Icon name="link" size={13} />
              创建后会生成一次性展示的 Token，URL 形如 <code>/api/hooks/workflow/&lt;token&gt;</code>。关闭弹窗后将无法再次查看，只能重置。
            </div>
          ) : (
            <>
              <div className="segmented trigger-mode-tabs" role="tablist" aria-label="定时方式">
                <button type="button" role="tab" aria-selected={scheduleMode === 'cron'} className={scheduleMode === 'cron' ? 'is-active' : ''} onClick={() => setScheduleMode('cron')}>Cron</button>
                <button type="button" role="tab" aria-selected={scheduleMode === 'interval'} className={scheduleMode === 'interval' ? 'is-active' : ''} onClick={() => setScheduleMode('interval')}>固定间隔</button>
              </div>
              {scheduleMode === 'cron' ? (
                <div className="trigger-form-grid">
                  <Field label="cron 表达式" hint="分 时 日 月 周，例如 0 9 * * 1-5 表示工作日 09:00。">
                    <input className="input mono" value={cron} onChange={(event) => setCron(event.target.value)} placeholder="0 9 * * 1-5" spellCheck={false} />
                  </Field>
                  <Field label="时区" hint="IANA 时区名。">
                    <input className="input mono" value={tz} onChange={(event) => setTz(event.target.value)} placeholder="Asia/Shanghai" spellCheck={false} />
                  </Field>
                </div>
              ) : (
                <Field label="间隔（分钟）" hint="至少 1 分钟。">
                  <input className="input mono" type="number" min={1} value={minutes} onChange={(event) => setMinutes(event.target.value)} />
                </Field>
              )}
            </>
          )}

          <Field label="环境变量覆盖（可选）" hint='JSON 对象，仅覆盖本次触发器运行，例如 {"REGION":"cn"}。'>
            <textarea className="textarea mono" rows={3} value={envText} onChange={(event) => setEnvText(event.target.value)} placeholder='{"REGION":"cn"}' spellCheck={false} />
          </Field>

          {formError ? <div className="trigger-form-error"><Icon name="warning" size={13} />{formError}</div> : null}
        </div>
      </Modal>

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除触发器"
        message={<>确定删除 <code>{deleteTarget?.trigger_id}</code>？删除后不再按规则运行，历史运行记录会保留。</>}
        confirmLabel="删除"
        danger
        busy={deleteTrigger.isPending}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && deleteTrigger.mutate(deleteTarget)}
      />

      <ConfirmDialog
        open={!!resetTarget}
        title="重置 Webhook Token"
        message={<>重置后旧 Webhook URL 立即失效。新的 Token 只展示一次，确定继续？</>}
        confirmLabel="重置 Token"
        danger
        busy={regenerateToken.isPending}
        onClose={() => setResetTarget(null)}
        onConfirm={() => resetTarget && regenerateToken.mutate(resetTarget)}
      />
    </section>
  )
}

/* -------------------------------------------------------------------------- */
/* Runs                                                                       */
/* -------------------------------------------------------------------------- */

export function WorkflowRunsPanel({ workflowRef = '', wid = '' }: { workflowRef?: string; wid?: string }) {
  const queryClient = useQueryClient()
  const pageSize = 100
  const runsQuery = useQuery({
    queryKey: ['workflow-runs', workflowRef, pageSize],
    queryFn: async () => {
      const data = await api.executions(new URLSearchParams({ offset: '0', limit: String(pageSize) }))
      return (data.executions || []).filter((run) => run.workflow_ref === workflowRef)
    },
    enabled: !!workflowRef,
    refetchInterval: (query) =>
      (query.state.data || []).some((run) => ['queued', 'running', 'cancelling'].includes(run.status)) ? 2500 : false,
  })
  const runs = runsQuery.data || []
  const latest = useMemo(() => runs.slice(0, pageSize), [runs, pageSize])

  if (!workflowRef) {
    return (
      <section className="panel workflow-runs-panel" data-testid="workflow-runs-panel">
        <SectionHeader title="最近运行" description="按发布版本过滤" icon="history" />
        <div className="trigger-empty">
          <Icon name="git" size={20} />
          <strong>该任务尚未发布</strong>
          <p>发布后产生的运行会按 workflow_ref 汇总在这里。</p>
          {wid ? <Button size="sm" variant="ghost" icon="git" onClick={() => navigate(`/studio/${wid}/review`)}>去发布</Button> : null}
        </div>
      </section>
    )
  }

  return (
    <section className="panel workflow-runs-panel" data-testid="workflow-runs-panel" data-workflow-ref={workflowRef}>
      <SectionHeader
        title="最近运行"
        description={`${workflowRef} · 最近 ${latest.length} 条`}
        icon="history"
        actions={<Button size="sm" variant="ghost" icon="refresh" onClick={() => { void queryClient.invalidateQueries({ queryKey: ['workflow-runs', workflowRef] }) }}>刷新</Button>}
      />
      {runsQuery.isLoading ? <Loading label="正在读取运行…" /> : null}
      {runsQuery.error ? (
        <div className="trigger-empty">
          <Icon name="warning" size={20} />
          <strong>运行读取失败</strong>
          <p>{(runsQuery.error as Error).message}</p>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => runsQuery.refetch()}>重试</Button>
        </div>
      ) : null}
      {!runsQuery.isLoading && !runsQuery.error && !latest.length ? (
        <div className="trigger-empty">
          <Icon name="history" size={20} />
          <strong>还没有运行记录</strong>
          <p>使用触发器「立即运行」或发布页的「立即运行」后，记录会出现在这里。</p>
        </div>
      ) : null}
      {latest.length ? (
        <div className="workflow-run-list">
          {latest.map((run: Execution) => (
            <a className="workflow-run" key={run.eid} href={href(run.run_id ? `/runs/${run.run_id}` : '/runs')} data-testid={`workflow-run-${run.eid}`}>
              <StatusBadge status={run.status || run.run_status} />
              <div className="workflow-run-main">
                <strong>{run.trigger || '手动运行'}</strong>
                <span className="mono">{run.eid}{run.job_id ? ` · ${run.job_id}` : ''}</span>
              </div>
              <span className="workflow-run-time">{formatTime(run.started_at || run.created_at)}</span>
              <span className="workflow-run-duration">{run.duration_ms ? formatDuration(run.duration_ms) : '—'}</span>
              <span className="workflow-run-count mono">{run.passed ?? 0}/{run.total ?? 0}</span>
              <Icon name="chevron" size={13} />
            </a>
          ))}
        </div>
      ) : null}
      {runsQuery.data && runsQuery.data.length !== latest.length ? (
        <p className="muted trigger-runs-note">仅显示最近 {pageSize} 条内匹配该 workflow_ref 的运行。</p>
      ) : null}
    </section>
  )
}
