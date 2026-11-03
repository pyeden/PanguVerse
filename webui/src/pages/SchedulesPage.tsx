import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../shared/api'
import { href, withQuery } from '../shared/router'
import { formatTime, relativeTime, relativeUntil, shortId } from '../shared/utils'
import type { AnyRecord, Schedule } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  EnvEditor,
  ErrorState,
  Field,
  Loading,
  Modal,
  PageHeader,
  SectionHeader,
  Select,
  StatusBadge,
  useToast,
} from '../components/ui'

const CRON_PRESETS: { label: string; expr: string }[] = [
  { label: '每小时整点', expr: '0 * * * *' },
  { label: '每天 09:00', expr: '0 9 * * *' },
  { label: '工作日 09:30', expr: '30 9 * * 1-5' },
  { label: '每周一 08:00', expr: '0 8 * * 1' },
  { label: '每月 1 日 09:00', expr: '0 9 1 * *' },
]

function cronFieldIssue(field: string, min: number, max: number): string | null {
  if (field === '*') return null
  for (const part of field.split(',')) {
    if (!part) return '存在空的字段段'
    const [base, stepText, extra] = part.split('/')
    if (extra !== undefined) return `不支持 ${part}`
    let step = 1
    if (stepText !== undefined) {
      step = Number(stepText)
      if (!Number.isInteger(step) || step <= 0) return `步长 ${stepText} 非法`
    }
    if (base === '*') continue
    if (base.includes('-')) {
      const [fromText, toText, extraRange] = base.split('-')
      if (extraRange !== undefined) return `范围 ${base} 非法`
      const from = Number(fromText)
      const to = Number(toText)
      if (!Number.isInteger(from) || !Number.isInteger(to)) return `范围 ${base} 非法`
      if (from < min || to > max || from > to) return `${base} 超出 ${min}-${max}`
      continue
    }
    const value = Number(base)
    if (!Number.isInteger(value) || value < min || value > max) return `${base} 超出 ${min}-${max}`
    if (step === 0) return '步长必须大于 0'
  }
  return null
}

export function validateCron(expr: string): string {
  const text = expr.trim()
  if (!text) return '请填写 cron 表达式'
  const fields = text.split(/\s+/)
  if (fields.length !== 5) return `需要 5 段（分 时 日 月 周），当前 ${fields.length} 段`
  const ranges: [number, number, string][] = [
    [0, 59, '分钟'],
    [0, 23, '小时'],
    [1, 31, '日'],
    [1, 12, '月'],
    [0, 7, '星期'],
  ]
  for (let index = 0; index < fields.length; index += 1) {
    const [min, max, label] = ranges[index]
    const issue = cronFieldIssue(fields[index], min, max)
    if (issue) return `${label}字段：${issue}`
  }
  return ''
}

interface ScheduleFormState {
  name: string
  project_id: string
  suite_ids: string[]
  trigger: 'cron' | 'interval' | 'webhook'
  cron: string
  tz: string
  minutes: string
  env: AnyRecord
  enabled: boolean
}

const emptyForm = (projectId = ''): ScheduleFormState => ({
  name: '',
  project_id: projectId,
  suite_ids: [],
  trigger: 'cron',
  cron: '0 9 * * *',
  tz: 'Asia/Shanghai',
  minutes: '30',
  env: {},
  enabled: true,
})

function triggerLabel(schedule: Schedule): string {
  const trigger = schedule.trigger || { kind: '' }
  if (trigger.kind === 'cron') return `cron ${trigger.expr || '—'}${trigger.tz ? ` · ${trigger.tz}` : ''}`
  if (trigger.kind === 'interval') return `每 ${trigger.minutes ?? '—'} 分钟`
  if (trigger.kind === 'webhook') return 'Webhook（token 已隐藏）'
  return trigger.kind || '—'
}

export function SchedulesPage() {
  const queryClient = useQueryClient()
  const { push } = useToast()
  const [includeDisabled, setIncludeDisabled] = useState(false)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<Schedule | null>(null)
  const [form, setForm] = useState<ScheduleFormState>(emptyForm())
  const [formError, setFormError] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Schedule | null>(null)
  const [tokenInfo, setTokenInfo] = useState<{ name: string; token: string } | null>(null)
  const [lastRun, setLastRun] = useState<{ name: string; job_id: string; batch_id: string } | null>(null)

  const schedulesQuery = useQuery({
    queryKey: ['schedules', includeDisabled],
    queryFn: () => api.schedules(includeDisabled),
    refetchInterval: 30_000,
  })
  const projectsQuery = useQuery({ queryKey: ['projects', 'with-archived'], queryFn: () => api.projects(true), staleTime: 30_000 })
  const projects = projectsQuery.data || []
  const schedules = schedulesQuery.data || []
  const projectName = useMemo(() => {
    const map = new Map(projects.map((project) => [project.project_id, project.name]))
    return (pid?: string) => (pid ? map.get(pid) || pid : '—')
  }, [projects])

  const suitesQuery = useQuery({
    queryKey: ['project', form.project_id],
    queryFn: () => api.project(form.project_id),
    enabled: editorOpen && !!form.project_id,
  })
  const suites = suitesQuery.data?.suites || []

  useEffect(() => {
    if (!editorOpen || form.project_id || !projects.length) return
    setForm((current) => ({ ...current, project_id: projects[0].project_id }))
  }, [editorOpen, form.project_id, projects])

  const openCreate = () => {
    setEditing(null)
    setForm(emptyForm(projects[0]?.project_id || ''))
    setFormError('')
    setEditorOpen(true)
  }

  const openEdit = (schedule: Schedule) => {
    setEditing(schedule)
    setForm({
      name: schedule.name,
      project_id: schedule.target?.project_id || projects[0]?.project_id || '',
      suite_ids: schedule.target?.suite_ids || [],
      trigger: (schedule.trigger?.kind as ScheduleFormState['trigger']) || 'cron',
      cron: schedule.trigger?.expr || '0 9 * * *',
      tz: schedule.trigger?.tz || 'Asia/Shanghai',
      minutes: String(schedule.trigger?.minutes ?? 30),
      env: schedule.env_override || {},
      enabled: schedule.enabled !== false,
    })
    setFormError('')
    setEditorOpen(true)
  }

  const save = useMutation({
    mutationFn: async () => {
      if (!form.name.trim()) throw new Error('请填写调度名称')
      if (!form.project_id) throw new Error('请选择目标项目')
      let trigger: AnyRecord
      if (form.trigger === 'cron') {
        const issue = validateCron(form.cron)
        if (issue) throw new Error(issue)
        trigger = { kind: 'cron', expr: form.cron.trim(), tz: form.tz || 'Asia/Shanghai' }
      } else if (form.trigger === 'interval') {
        const minutes = Number(form.minutes)
        if (!Number.isFinite(minutes) || minutes < 1) throw new Error('间隔至少为 1 分钟')
        trigger = { kind: 'interval', minutes: Math.floor(minutes) }
      } else {
        trigger = { kind: 'webhook' }
      }
      const body: AnyRecord = {
        name: form.name.trim(),
        target: { kind: 'project', project_id: form.project_id, suite_ids: form.suite_ids },
        trigger,
        env_override: form.env,
        enabled: form.enabled,
      }
      return editing ? api.updateSchedule(editing.sid, body) : api.createSchedule(body)
    },
    onSuccess: (schedule) => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      setEditorOpen(false)
      push(editing ? '调度已更新' : '调度已创建', 'ok')
      const token = schedule?.trigger?.token || (schedule as AnyRecord | undefined)?.token
      if (!editing && form.trigger === 'webhook') {
        setTokenInfo({ name: schedule?.name || form.name, token: token ? String(token) : '' })
      }
    },
    onError: (error: Error) => setFormError(error.message),
  })

  const toggle = useMutation({
    mutationFn: ({ schedule, enabled }: { schedule: Schedule; enabled: boolean }) =>
      api.updateSchedule(schedule.sid, { enabled }),
    onSuccess: (schedule, variables) => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      const enabled = schedule?.enabled ?? variables.enabled
      if (enabled === false && !includeDisabled) {
        // 默认列表只列启用项；停用后自动展开停用项，避免行消失、无法再启用。
        setIncludeDisabled(true)
        push('调度已停用；已自动显示停用项', 'ok')
        return
      }
      push(enabled === false ? '调度已停用' : '调度已启用', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const runNow = useMutation({
    mutationFn: (schedule: Schedule) => api.runScheduleNow(schedule.sid),
    onSuccess: (result, schedule) => {
      setLastRun({ name: schedule.name, job_id: result.job_id, batch_id: result.batch_id })
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      push(`已触发「${schedule.name}」· batch ${shortId(result.batch_id, 10)}`, 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const remove = useMutation({
    mutationFn: (schedule: Schedule) => api.deleteSchedule(schedule.sid),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      setDeleteTarget(null)
      push('调度已删除；历史运行保留', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const resetToken = useMutation({
    mutationFn: (schedule: Schedule) =>
      api.updateSchedule(schedule.sid, { trigger: { kind: 'webhook', regenerate_token: true } }),
    onSuccess: (schedule) => {
      const token = schedule?.trigger?.token || (schedule as AnyRecord | undefined)?.token
      setTokenInfo({ name: schedule?.name || editing?.name || 'Webhook', token: token ? String(token) : '' })
      push(token ? 'Webhook token 已重置' : '后端未返回新 token', token ? 'ok' : 'warn')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const apiMissing = schedulesQuery.error instanceof ApiError && schedulesQuery.error.status === 404
  const cronIssue = form.trigger === 'cron' ? validateCron(form.cron) : ''

  return (
    <div className="page schedules-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 自动 · 调度</>}
        title="调度"
        description="让流程按 cron、固定间隔或 webhook 自己跑起来。重叠触发会被跳过，不会堆叠 batch。"
        actions={
          <>
            <Button variant="ghost" icon="refresh" onClick={() => queryClient.invalidateQueries({ queryKey: ['schedules'] })}>刷新</Button>
            <Button variant="primary" icon="plus" onClick={openCreate}>新建调度</Button>
          </>
        }
      />

      {apiMissing ? (
        <div className="notice-inline warn-note">
          <Icon name="warning" size={15} />
          <div>
            <strong>后端接口未就绪（未联调）</strong>
            <p>当前前端已按契约 v8.0 §1.2 接入 <code>api/schedules</code>，但本机后端仍返回 404。接口上线后本页无需改动即可工作。</p>
          </div>
        </div>
      ) : null}

      {lastRun ? (
        <div className="notice-inline">
          <Icon name="play" size={15} />
          <div>
            <strong>「{lastRun.name}」已立即运行</strong>
            <p>
              batch <code>{lastRun.batch_id}</code> · job <code>{lastRun.job_id}</code>
              <a className="inline-link" href={href(withQuery('/executions', { job: lastRun.job_id }))}>查看执行 <Icon name="arrow" size={12} /></a>
            </p>
          </div>
          <button type="button" className="notice-close" aria-label="关闭" onClick={() => setLastRun(null)}><Icon name="close" size={13} /></button>
        </div>
      ) : null}

      <div className="catalog-toolbar panel">
        <span className="toolbar-label"><Icon name="clock" size={15} />{schedules.length} 个调度</span>
        <span className="toolbar-spacer" />
        <button
          type="button"
          className={`archive-toggle ${includeDisabled ? 'is-on' : ''}`}
          onClick={() => setIncludeDisabled((value) => !value)}
          aria-pressed={includeDisabled}
          data-testid="toggle-disabled-schedules"
        >
          <span className="toggle-track"><span /></span>
          显示已停用
        </button>
      </div>

      {schedulesQuery.isLoading ? <Loading label="正在读取调度…" /> : null}
      {schedulesQuery.error && !apiMissing ? <ErrorState error={schedulesQuery.error} onRetry={() => schedulesQuery.refetch()} /> : null}

      {!schedulesQuery.isLoading && !schedulesQuery.error && !schedules.length ? (
        <EmptyState
          icon="clock"
          title="还没有调度"
          actions={<Button size="sm" variant="primary" icon="plus" onClick={openCreate}>新建调度</Button>}
        >
          先建一个 interval 调度（例如每 30 分钟），保存后列表会显示后端计算的下次运行时间。
        </EmptyState>
      ) : null}

      {schedules.length ? (
        <div className="schedule-list">
          {schedules.map((schedule) => (
            <article className={`schedule-card ${schedule.enabled ? '' : 'is-disabled'}`} key={schedule.sid} data-testid={`schedule-row-${schedule.sid}`}>
              <div className="schedule-main">
                <span className={`schedule-icon ${schedule.enabled ? 'is-on' : ''}`}><Icon name="clock" size={18} /></span>
                <div className="schedule-copy">
                  <div className="schedule-title">
                    <strong>{schedule.name}</strong>
                    <Badge tone={schedule.enabled ? 'ok' : 'neutral'}>{schedule.enabled ? '启用' : '停用'}</Badge>
                    {schedule.last_status ? <StatusBadge status={schedule.last_status} /> : <Badge tone="neutral">未运行</Badge>}
                  </div>
                  <div className="schedule-meta">
                    <span><Icon name="projects" size={13} />{projectName(schedule.target?.project_id)}</span>
                    <span>·</span>
                    <span>{(schedule.target?.suite_ids || []).length ? `${schedule.target?.suite_ids?.length} 个套件` : '全部套件'}</span>
                    <span>·</span>
                    <span className="mono">{schedule.sid}</span>
                  </div>
                  <div className="schedule-trigger"><Icon name="bolt" size={13} />{triggerLabel(schedule)}</div>
                </div>
              </div>
              <div className="schedule-next">
                <span className="schedule-next-label">下次运行</span>
                {schedule.enabled ? (
                  <>
                    <strong>{relativeUntil(schedule.next_run_at)}</strong>
                    <small>{formatTime(schedule.next_run_at)}</small>
                  </>
                ) : <strong className="muted">已停用</strong>}
                <small className="muted">{schedule.last_run_at ? `上次 ${relativeTime(schedule.last_run_at)}` : '尚未触发'}</small>
              </div>
              <div className="schedule-actions">
                <Button
                  size="sm"
                  variant="primary"
                  icon="play"
                  loading={runNow.isPending && runNow.variables?.sid === schedule.sid}
                  onClick={() => runNow.mutate(schedule)}
                >立即运行</Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={schedule.enabled ? 'archive' : 'refresh'}
                  loading={toggle.isPending && toggle.variables?.schedule.sid === schedule.sid}
                  onClick={() => toggle.mutate({ schedule, enabled: !schedule.enabled })}
                >{schedule.enabled ? '停用' : '启用'}</Button>
                <Button size="sm" variant="ghost" icon="edit" onClick={() => openEdit(schedule)}>编辑</Button>
                <Button size="sm" variant="ghost" icon="trash" onClick={() => setDeleteTarget(schedule)}>删除</Button>
              </div>
            </article>
          ))}
        </div>
      ) : null}

      <Modal
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        title={editing ? '编辑调度' : '新建调度'}
        subtitle="目标项目与套件复用项目批量运行；套件留空表示全部。next_run_at 由后端统一计算。"
        size="lg"
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditorOpen(false)}>取消</Button>
            {editing?.trigger?.kind === 'webhook' ? (
              <Button variant="ghost" icon="refresh" loading={resetToken.isPending} onClick={() => resetToken.mutate(editing)}>重置 Webhook token</Button>
            ) : null}
            <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>{editing ? '保存调度' : '创建调度'}</Button>
          </>
        }
      >
        <Field label="调度名称" htmlFor="schedule-name">
          <input id="schedule-name" className="input" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如 每日站点巡检" autoFocus />
        </Field>
        <div className="form-grid two">
          <Field label="目标项目">
            <Select
              value={form.project_id}
              onChange={(event) => setForm({ ...form, project_id: event.target.value, suite_ids: [] })}
            >
              <option value="">选择项目</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
            </Select>
          </Field>
          <Field label="时区" hint="cron 默认 Asia/Shanghai">
            <input className="input mono" value={form.tz} onChange={(event) => setForm({ ...form, tz: event.target.value })} disabled={form.trigger !== 'cron'} />
          </Field>
        </div>
        <Field label="套件" hint={form.suite_ids.length ? `已选 ${form.suite_ids.length} 个` : '不选 = 全部套件'}>
          {suitesQuery.isLoading ? <span className="muted">正在读取套件…</span> : null}
          {!suitesQuery.isLoading && !suites.length ? <span className="muted">该项目还没有套件，将按项目全部套件运行。</span> : null}
          {suites.length ? (
            <div className="suite-choice-grid">
              {suites.map((suite) => (
                <label className={`suite-choice ${form.suite_ids.includes(suite.suite_id) ? 'is-checked' : ''}`} key={suite.suite_id}>
                  <input
                    type="checkbox"
                    checked={form.suite_ids.includes(suite.suite_id)}
                    onChange={(event) => setForm({
                      ...form,
                      suite_ids: event.target.checked
                        ? [...form.suite_ids, suite.suite_id]
                        : form.suite_ids.filter((id) => id !== suite.suite_id),
                    })}
                  />
                  <span><strong>{suite.name}</strong><small className="mono">{suite.suite_id}</small></span>
                </label>
              ))}
            </div>
          ) : null}
        </Field>

        <Field label="触发方式">
          <div className="segmented large">
            {(['cron', 'interval', 'webhook'] as const).map((kind) => (
              <button key={kind} type="button" className={form.trigger === kind ? 'is-active' : ''} onClick={() => setForm({ ...form, trigger: kind })}>
                {kind === 'cron' ? 'cron 表达式' : kind === 'interval' ? '固定间隔' : 'Webhook'}
              </button>
            ))}
          </div>
        </Field>

        {form.trigger === 'cron' ? (
          <div className="trigger-config">
            <Field label="cron 表达式" error={form.cron ? cronIssue : undefined} hint="5 段：分 时 日 月 周；后端仍会再次校验。">
              <input className={`input mono ${cronIssue ? 'input-invalid' : ''}`} value={form.cron} onChange={(event) => setForm({ ...form, cron: event.target.value })} spellCheck={false} />
            </Field>
            <div className="cron-presets">
              {CRON_PRESETS.map((preset) => (
                <button key={preset.expr} type="button" className={`chip ${form.cron === preset.expr ? 'is-active' : ''}`} onClick={() => setForm({ ...form, cron: preset.expr })}>
                  {preset.label}<code>{preset.expr}</code>
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {form.trigger === 'interval' ? (
          <Field label="间隔（分钟）" error={Number(form.minutes) < 1 ? '最小 1 分钟' : undefined}>
            <div className="inline-input">
              <input className="input mono" type="number" min={1} step={1} value={form.minutes} onChange={(event) => setForm({ ...form, minutes: event.target.value })} />
              <span className="muted">分钟触发一次</span>
            </div>
          </Field>
        ) : null}

        {form.trigger === 'webhook' ? (
          <div className="notice-inline">
            <Icon name="link" size={15} />
            <div>
              <strong>Webhook 触发</strong>
              <p>{editing ? '保存不会自动更换 token；需要新 token 请点下方「重置 Webhook token」。' : '创建成功后仅展示一次 token，请立即复制。触发地址为 /api/hooks/{token}。'}</p>
            </div>
          </div>
        ) : null}

        <Field label="环境变量覆盖" hint="仅覆盖本次调度运行；密钥以 *** 回显，留空沿用。">
          <EnvEditor value={form.env} onChange={(env) => setForm({ ...form, env })} />
        </Field>
        <label className="checkbox-row single">
          <input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />
          启用调度（停用后 cron/webhook 均不触发，但仍可「立即运行」）
        </label>
        {formError ? <div className="form-error"><Icon name="warning" size={14} />{formError}</div> : null}
      </Modal>

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除调度"
        message={<>确定删除「{deleteTarget?.name}」？<br />只删除调度定义，不删除历史运行与 batch。</>}
        confirmLabel="删除调度"
        confirmTestId="confirm-delete-schedule"
        danger
        busy={remove.isPending}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && remove.mutate(deleteTarget)}
      />

      <Modal
        open={!!tokenInfo}
        onClose={() => setTokenInfo(null)}
        title="Webhook token（仅显示一次）"
        subtitle="关闭后列表不再展示 token；丢失可重置。"
        size="sm"
        footer={<Button variant="primary" onClick={() => setTokenInfo(null)}>我已保存</Button>}
      >
        {tokenInfo?.token ? (
          <WebhookToken name={tokenInfo.name} token={tokenInfo.token} />
        ) : (
          <div className="notice-inline warn-note">
            <Icon name="warning" size={15} />
            <div><strong>后端未返回 token</strong><p>创建成功但响应中没有 trigger.token；接口联调后请在此展示。</p></div>
          </div>
        )}
      </Modal>
    </div>
  )
}

function WebhookToken({ name, token }: { name: string; token: string }) {
  const { push } = useToast()
  const url = `${window.location.origin}/api/hooks/${token}`
  return (
    <div className="token-box">
      <div className="token-row">
        <span>调度</span>
        <strong>{name}</strong>
      </div>
      <div className="token-row">
        <span>Token</span>
        <code>{token}</code>
      </div>
      <div className="token-row">
        <span>触发地址</span>
        <code className="word-break">{url}</code>
      </div>
      <Button
        size="sm"
        variant="soft"
        icon="copy"
        onClick={() => navigator.clipboard.writeText(url).then(() => push('触发地址已复制', 'ok')).catch(() => push('复制失败', 'error'))}
      >复制触发地址</Button>
      <p className="muted">示例：<code>{`curl -X POST ${url} -H 'Content-Type: application/json' -d '{"env":{"KEY":"value"}}'`}</code></p>
    </div>
  )
}
