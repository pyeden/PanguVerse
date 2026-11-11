import type {
  AnyRecord,
  Atom,
  Batch,
  Execution,
  ExecutionPageData,
  ExecutionCancelResult,
  Insight,
  Job,
  Pack,
  Project,
  ReportPageData,
  RunDetail,
  RunSummary,
  Scenario,
  StatusPayload,
  StudioDiff,
  StudioEdge,
  StudioGraph,
  StudioNode,
  Task,
  Validation,
  ValidationIssue,
  Workflow,
  DebugSession,
  DebugNodeState,
  DebugResult,
  CapabilityGap,
  DiffItem,
  Risk,
  Schedule,
  ScheduleRunResult,
  Artifact,
  ArtifactPageData,
  ArtifactShare,
  WorkflowTrigger,
  WorkflowTriggerCreate,
  WorkflowTriggerPatch,
  WorkflowTriggerResult,
  ExploreSession,
  ExploreHistoryItem,
  ExploreStartResult,
  ExploreCrystallizeResult,
} from './types'

export class ApiError extends Error {
  status: number
  code: string
  data?: AnyRecord
  retryAfterSeconds?: number

  constructor(message: string, status = 0, code = '', data?: AnyRecord, retryAfterSeconds?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.data = data
    this.retryAfterSeconds = retryAfterSeconds
  }
}

export const UNAUTHORIZED_EVENT = 'panguverse:unauthorized'

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: options.method || 'GET',
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    credentials: 'same-origin',
    signal: options.signal,
  }
  if (options.body !== undefined) {
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json; charset=utf-8'
    init.body = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(path, init)
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    throw new ApiError(
      '网络请求失败：请确认 panguverse serve 仍在运行，端口与当前页面一致。',
      0,
      'network',
    )
  }

  const type = (response.headers.get('content-type') || '').toLowerCase()
  const isJson = type.includes('json')
  const noContent = [204, 205, 304].includes(response.status)
  let payload: unknown = null
  if (!noContent) {
    if (isJson) {
      payload = await response.json().catch(() => null)
    } else {
      payload = await response.text().catch(() => '')
    }
  }

  if (response.status === 401 && !path.includes('api/auth/')) {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
  }

  if (!response.ok) {
    const body = payload as AnyRecord | null
    const message =
      (body?.error && typeof body.error === 'object' && body.error.message) ||
      (typeof payload === 'string' && payload.trim().slice(0, 300)) ||
      `请求失败（HTTP ${response.status}）`
    const code = (body?.error && typeof body.error === 'object' && body.error.code) || `http_${response.status}`
    const data = body && typeof body === 'object' ? body : undefined
    const errorBody = body?.error && typeof body.error === 'object' ? body.error as AnyRecord : null
    const retryRaw = [
      body?.retry_after_seconds,
      errorBody?.retry_after_seconds,
      response.headers.get('Retry-After'),
    ].find((value) => value !== undefined && value !== null && value !== '')
    const retryAfterSeconds = retryRaw === undefined ? 0 : Number(retryRaw)
    throw new ApiError(
      String(message),
      response.status,
      String(code),
      data,
      Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0 ? retryAfterSeconds : undefined,
    )
  }
  return (noContent ? null : payload) as T
}

export const api = {
  authStatus: () =>
    request<{ authenticated: boolean; user?: string; auth_disabled?: boolean }>('api/auth/status'),
  login: (username: string, password: string) =>
    request<{ ok: boolean; user?: string }>('api/auth/login', {
      method: 'POST',
      body: { username, password },
    }),
  logout: () => request<{ ok: boolean }>('api/auth/logout', { method: 'POST' }),
  status: () => request<StatusPayload>('api/status'),

  projects: async (includeArchived = false) => {
    const suffix = includeArchived ? '?include_archived=1' : ''
    const payload = await request<{ projects?: Project[] }>(`api/projects${suffix}`)
    return payload.projects || []
  },
  project: (pid: string) => request<Project>(`api/projects/${encodeURIComponent(pid)}`),
  createProject: (body: AnyRecord) => request<Project>('api/projects', { method: 'POST', body }),
  updateProject: (pid: string, body: AnyRecord) =>
    request<Project>(`api/projects/${encodeURIComponent(pid)}`, { method: 'PATCH', body }),
  deleteProject: (pid: string) =>
    request<null>(`api/projects/${encodeURIComponent(pid)}`, { method: 'DELETE' }),
  archiveProject: (pid: string, archived = true) =>
    request<Project>(`api/projects/${encodeURIComponent(pid)}/${archived ? 'archive' : 'unarchive'}`, { method: 'POST', body: {} }),
  addSuite: (pid: string, body: AnyRecord) =>
    request<AnyRecord>(`api/projects/${encodeURIComponent(pid)}/suites`, { method: 'POST', body }),
  updateSuite: (pid: string, sid: string, body: AnyRecord) =>
    request<AnyRecord>(`api/projects/${encodeURIComponent(pid)}/suites/${encodeURIComponent(sid)}`, {
      method: 'PATCH',
      body,
    }),
  deleteSuite: (pid: string, sid: string) =>
    request<null>(`api/projects/${encodeURIComponent(pid)}/suites/${encodeURIComponent(sid)}`, {
      method: 'DELETE',
    }),
  projectSummary: (pid: string, limit = 5) =>
    request<AnyRecord>(`api/projects/${encodeURIComponent(pid)}/summary?limit=${limit}`),
  runProject: (pid: string, body: AnyRecord) =>
    request<{ batch_id: string; jobs: { job_id: string; suite_id: string }[] }>(
      `api/projects/${encodeURIComponent(pid)}/run`,
      { method: 'POST', body },
    ),
  batch: (batchId: string) => request<Batch>(`api/batches/${encodeURIComponent(batchId)}`),

  atoms: () => request<Atom[]>('api/atoms'),
  packs: async () => {
    const payload = await request<Pack[]>('api/packs')
    return Array.isArray(payload) ? payload : []
  },
  pack: (name: string) => request<Pack>(`api/packs/${encodeURIComponent(name)}`),
  scenarios: () => request<Scenario[]>('api/scenarios'),

  runs: (limit = 50) => request<RunSummary[]>(`api/runs?limit=${limit}`),
  queryRuns: (params: URLSearchParams) => request<{ total: number; items: RunSummary[] }>(`api/runs/query?${params.toString()}`),
  run: (rid: string) => request<RunDetail>(`api/runs/${encodeURIComponent(rid)}`),
  runDiff: (rid: string, base?: string) =>
    request<AnyRecord>(`api/runs/${encodeURIComponent(rid)}/diff${base ? `?base=${encodeURIComponent(base)}` : ''}`),
  insight: (rid: string) => request<Insight>(`api/runs/${encodeURIComponent(rid)}/insight`),
  generateInsight: (rid: string, refresh = false) =>
    request<Insight>(`api/runs/${encodeURIComponent(rid)}/insight`, { method: 'POST', body: { refresh } }),
  rerun: (rid: string, body: AnyRecord) =>
    request<{ job_id: string; status: string }>(`api/runs/${encodeURIComponent(rid)}/rerun`, {
      method: 'POST',
      body,
    }),
  saveAsTask: (rid: string, body: AnyRecord = {}) =>
    request<Task>(`api/runs/${encodeURIComponent(rid)}/save_as_task`, { method: 'POST', body }),
  runConfig: (rid: string) => request<AnyRecord>(`api/runs/${encodeURIComponent(rid)}/config`),

  jobs: () => request<Job[]>('api/jobs'),
  startDemoRun: () => request<{ job_id: string; status: string }>('api/runs', { method: 'POST', body: { mode: 'demo' } }),

  job: (jobId: string) => request<Job>(`api/jobs/${encodeURIComponent(jobId)}`),

  executions: (params: URLSearchParams) =>
    request<ExecutionPageData>(`api/executions?${params.toString()}`),
  execution: (eid: string) => request<Execution>(`api/executions/${encodeURIComponent(eid)}`),
  cancelExecution: (eid: string, note = '') =>
    request<ExecutionCancelResult>(`api/executions/${encodeURIComponent(eid)}/cancel`, { method: 'POST', body: { note } }),

  reports: (params: URLSearchParams) =>
    request<ReportPageData>(`api/reports?${params.toString()}`),
  signReport: (runId: string, body: { verdict: string; note?: string; force?: boolean }) =>
    request<AnyRecord>(`api/reports/${encodeURIComponent(runId)}/sign`, { method: 'POST', body }),
  voidReport: (runId: string, reason: string) =>
    request<AnyRecord>(`api/reports/${encodeURIComponent(runId)}/void`, { method: 'POST', body: { reason } }),

  tasks: async (includeArchived = false) => {
    const suffix = includeArchived ? '?include_archived=1' : ''
    const payload = await request<{ tasks?: Task[] }>(`api/tasks${suffix}`)
    return payload.tasks || []
  },
  task: (taskId: string) => request<Task>(`api/tasks/${encodeURIComponent(taskId)}`),
  createTask: (body: AnyRecord) => request<Task>('api/tasks', { method: 'POST', body }),
  updateTask: (taskId: string, body: AnyRecord) =>
    request<Task>(`api/tasks/${encodeURIComponent(taskId)}`, { method: 'PATCH', body }),
  deleteTask: (taskId: string) =>
    request<null>(`api/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
  archiveTask: (taskId: string, archived = true) =>
    request<Task>(`api/tasks/${encodeURIComponent(taskId)}/${archived ? 'archive' : 'unarchive'}`, { method: 'POST', body: {} }),
  runTask: (taskId: string, body: AnyRecord = {}) =>
    request<{ job_id: string; status: string }>(`api/tasks/${encodeURIComponent(taskId)}/run`, {
      method: 'POST',
      body,
    }),

  /* Schedules ------------------------------------------------------------- */

  schedules: async (includeDisabled = false) => {
    const suffix = includeDisabled ? '?include_disabled=1' : ''
    const payload = await request<AnyRecord>(`api/schedules${suffix}`)
    const items = Array.isArray(payload) ? payload : payload.schedules || payload.items || []
    return items as Schedule[]
  },
  schedule: async (sid: string) => unwrapSchedule(await request<AnyRecord>(`api/schedules/${encodeURIComponent(sid)}`)),
  createSchedule: async (body: AnyRecord) => unwrapSchedule(await request<AnyRecord>('api/schedules', { method: 'POST', body })),
  updateSchedule: async (sid: string, body: AnyRecord) =>
    unwrapSchedule(await request<AnyRecord>(`api/schedules/${encodeURIComponent(sid)}`, { method: 'PATCH', body })),
  deleteSchedule: (sid: string) =>
    request<null>(`api/schedules/${encodeURIComponent(sid)}`, { method: 'DELETE' }),
  runScheduleNow: (sid: string) =>
    request<ScheduleRunResult>(`api/schedules/${encodeURIComponent(sid)}/run-now`, { method: 'POST', body: {} }),

  /* Artifacts ------------------------------------------------------------- */

  artifacts: async (params: URLSearchParams) => {
    const payload = await request<AnyRecord>(`api/artifacts?${params.toString()}`)
    if (Array.isArray(payload)) return { total: payload.length, items: payload as Artifact[] }
    const items = (payload.items || payload.artifacts || []) as Artifact[]
    return { total: Number(payload.total ?? items.length), items }
  },
  artifact: async (aid: string) => {
    const payload = await request<AnyRecord>(`api/artifacts/${encodeURIComponent(aid)}`)
    const artifact = (payload.artifact || payload) as Artifact
    if (artifact && payload.content !== undefined) artifact.content = payload.content
    return artifact
  },
  artifactDownloadUrl: (aid: string) => `api/artifacts/${encodeURIComponent(aid)}/download`,
  shareArtifact: async (aid: string, ttl: string) => {
    const path = `api/artifacts/${encodeURIComponent(aid)}/share`
    try {
      const payload = await request<AnyRecord>(path, { method: 'POST', body: { ttl } })
      return (payload.share || payload) as ArtifactShare
    } catch (error) {
      if (!(error instanceof ApiError) || (error.status !== 400 && error.status !== 422)) throw error
      const payload = await request<AnyRecord>(path, { method: 'POST', body: { expires_in: ttl } })
      return (payload.share || payload) as ArtifactShare
    }
  },
  revokeArtifactShare: async (aid: string) => {
    const path = `api/artifacts/${encodeURIComponent(aid)}/share`
    try {
      return await request<null>(path, { method: 'DELETE' })
    } catch (error) {
      if (!(error instanceof ApiError) || (error.status !== 404 && error.status !== 405)) throw error
      return request<null>(`${path}/revoke`, { method: 'POST', body: {} })
    }
  },
}

/* -------------------------------------------------------------------------- */
/* Studio API                                                                 */
/* -------------------------------------------------------------------------- */

let studioMockMode = false
const mockListeners = new Set<(value: boolean) => void>()

export function isStudioMock(): boolean {
  return studioMockMode
}

export function onStudioMockChange(listener: (value: boolean) => void): () => void {
  mockListeners.add(listener)
  return () => mockListeners.delete(listener)
}

function setStudioMock(value: boolean): void {
  if (studioMockMode === value) return
  studioMockMode = value
  mockListeners.forEach((listener) => listener(value))
}

/* Planner provenance ------------------------------------------------------- */

export interface StudioProvenance {
  planner?: string
  plan_source?: string
  confidence?: number
  assumptions?: string[]
  capability_gaps?: Array<CapabilityGap | string>
}

const PROVENANCE_PREFIX = 'panguverse.studio.provenance.'

export function rememberStudioProvenance(wid: string, payload: AnyRecord): void {
  if (!wid) return
  const source = (payload.provenance && typeof payload.provenance === 'object' ? payload.provenance : payload) as AnyRecord
  if (source.planner === undefined && source.plan_source === undefined && source.confidence === undefined) return
  try {
    sessionStorage.setItem(PROVENANCE_PREFIX + wid, JSON.stringify({
      planner: source.planner,
      plan_source: source.plan_source,
      confidence: source.confidence,
      assumptions: source.assumptions,
      capability_gaps: source.capability_gaps,
    }))
  } catch {
    /* session storage is best-effort */
  }
}

export function readStudioProvenance(wid: string): StudioProvenance | null {
  try {
    const raw = sessionStorage.getItem(PROVENANCE_PREFIX + wid)
    return raw ? JSON.parse(raw) as StudioProvenance : null
  } catch {
    return null
  }
}

export function normalizeGraph(workflow: Workflow | null | undefined): StudioGraph {
  if (!workflow) return { nodes: [], edges: [] }
  const graph = workflow.graph || workflow
  const ui = (workflow.ui || (workflow as AnyRecord).ui || {}) as Record<string, { x?: number; y?: number }>
  const nodes = ((graph.nodes || workflow.nodes || []) as AnyRecord[]).map((node) => {
    const id = String(node.node_id || node.id || '')
    const suppliedUi = node.ui && typeof node.ui === 'object' ? node.ui : ui[id]
    return {
      ...node,
      node_id: id,
      atom: String(node.atom || node.use || node.node_id || node.id || ''),
      name: node.name || id,
      with: (node.with || node.params || {}) as AnyRecord,
      capture: (node.capture || {}) as AnyRecord,
      ui: suppliedUi || node.ui,
    } as StudioNode
  })
  const edges = ((graph.edges || workflow.edges || []) as AnyRecord[]).map((edge, index) => ({
    ...edge,
    edge_id: String(edge.edge_id || edge.id || `edge-${index}`),
    from: String(edge.from || ''),
    to: String(edge.to || ''),
    via: edge.via == null ? '' : String(edge.via),
    kind: String(edge.kind || 'order'),
  })) as StudioEdge[]
  return { ...graph, nodes, edges }
}

function normalizeWorkflow(payload: AnyRecord, fallback: Workflow = {} as Workflow): Workflow {
  const rawRev = payload.rev ?? payload.current_rev ?? fallback.current_rev ?? fallback.rev
  const rev = Number(rawRev ?? 0)
  const current = Number(payload.current_rev ?? fallback.current_rev ?? rev ?? 0)
  const graph = normalizeGraph(payload as Workflow)
  return {
    ...fallback,
    ...payload,
    wid: String(payload.wid || payload.workflow_id || fallback.wid || ''),
    current_rev: current,
    rev,
    graph,
  }
}

async function ensureStudioMode(): Promise<boolean> {
  if (studioMockMode) return true
  try {
    await request<AnyRecord>('api/studio/drafts')
    return false
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      setStudioMock(true)
      return true
    }
    throw error
  }
}

async function realOrMock<T>(real: () => Promise<T>, mock: () => Promise<T>): Promise<T> {
  const mockMode = await ensureStudioMode()
  if (mockMode) return mock()
  return real()
}

function encode(value: string): string {
  return encodeURIComponent(value)
}

function normalizeTrigger(payload: AnyRecord): WorkflowTrigger {
  const record = (payload?.trigger && typeof payload.trigger === 'object' ? payload.trigger : payload) as AnyRecord
  const rawSchedule = record?.schedule && typeof record.schedule === 'object' ? record.schedule as AnyRecord : null
  const rawInterval = record?.interval && typeof record.interval === 'object' ? record.interval as AnyRecord : null
  const rawWebhook = record?.webhook && typeof record.webhook === 'object' ? record.webhook as AnyRecord : null
  return {
    ...record,
    trigger_id: String(record?.trigger_id || record?.id || record?.tid || ''),
    workflow_ref: String(record?.workflow_ref || record?.ref || ''),
    kind: String(record?.kind || record?.type || ''),
    enabled: record?.enabled !== false,
    schedule: rawSchedule
      ? { cron: String(rawSchedule.cron ?? rawSchedule.expr ?? ''), tz: rawSchedule.tz ? String(rawSchedule.tz) : undefined }
      : undefined,
    interval: rawInterval
      ? { minutes: Number(rawInterval.minutes ?? rawInterval.min ?? 0) }
      : undefined,
    webhook: rawWebhook
      ? { token_hint: rawWebhook.token_hint ? String(rawWebhook.token_hint) : undefined }
      : undefined,
    next_run_at: record?.next_run_at ?? null,
    last_status: record?.last_status ?? null,
    last_job_id: record?.last_job_id ?? null,
  } as WorkflowTrigger
}

function triggerResult(payload: AnyRecord): WorkflowTriggerResult {
  const record = (payload?.trigger && typeof payload.trigger === 'object' ? payload.trigger : payload) as AnyRecord
  const rawWebhook = record?.webhook && typeof record.webhook === 'object' ? record.webhook as AnyRecord : null
  const payloadWebhook = payload?.webhook && typeof payload.webhook === 'object' ? payload.webhook as AnyRecord : null
  const token = rawWebhook?.token || payloadWebhook?.token || payload?.token
  return {
    trigger: normalizeTrigger(record || {}),
    token: token ? String(token) : undefined,
  }
}

export const studioApi = {
  async listDrafts(includeArchived = false): Promise<Workflow[]> {
    const mode = await ensureStudioMode()
    const { mockListWorkflows } = await import('./studioMock')
    if (mode) return mockListWorkflows(includeArchived)
    const suffix = includeArchived ? '?include_archived=1' : ''
    const payload = await request<AnyRecord>(`api/studio/drafts${suffix}`)
    const items = Array.isArray(payload) ? payload : payload.drafts || payload.workflows || []
    return (items as AnyRecord[]).map((item) => normalizeWorkflow(item))
  },

  /**
   * POST api/studio/{wid}/run —— 从任务卡直接运行当前发布版本。
   * 未发布时后端返回 409 not_published；旧服务没有该路由时返回 404。
   */
  async runWorkflow(
    wid: string,
    body: AnyRecord = {},
  ): Promise<{ job_id: string; task_id?: string; workflow_ref?: string; run_id?: string; status?: string }> {
    return realOrMock(
      () => request<{ job_id: string; task_id?: string; workflow_ref?: string; run_id?: string; status?: string }>(
        `api/studio/${encode(wid)}/run`,
        { method: 'POST', body },
      ),
      async () => {
        const { mockRunWorkflow } = await import('./studioMock')
        return mockRunWorkflow(wid, body)
      },
    )
  },

  async renameWorkflow(wid: string, name: string): Promise<{ wid: string; name: string; current_rev?: number }> {
    return realOrMock(
      () => request<{ wid: string; name: string; current_rev?: number }>(`api/studio/${encode(wid)}`, {
        method: 'PATCH',
        body: { name },
      }),
      async () => {
        const { mockRenameWorkflow } = await import('./studioMock')
        return mockRenameWorkflow(wid, name)
      },
    )
  },

  async archiveWorkflow(wid: string, archived = true): Promise<{ wid: string; archived_at: number | null }> {
    return realOrMock(
      () => request<{ wid: string; archived_at: number | null }>(`api/studio/${encode(wid)}/archive`, {
        method: 'POST',
        body: { archived },
      }),
      async () => {
        const { mockArchiveWorkflow } = await import('./studioMock')
        return mockArchiveWorkflow(wid, archived)
      },
    )
  },

  /**
   * POST api/studio/{wid}/freeze —— 把当前 rev 固化为生产任务。
   * 后端要求该 rev 最近一次运行成功；未就绪时由调用方按 404 给友好提示。
   */
  async freezeWorkflow(
    wid: string,
    body: { rev?: number; message: string; checks?: AnyRecord[] },
  ): Promise<{ wid: string; execution_mode: 'frozen'; frozen_ref: string; goal_checks?: AnyRecord[] }> {
    return realOrMock(
      () => request<{ wid: string; execution_mode: 'frozen'; frozen_ref: string; goal_checks?: AnyRecord[] }>(
        `api/studio/${encode(wid)}/freeze`,
        { method: 'POST', body },
      ),
      async () => {
        const { mockFreezeWorkflow } = await import('./studioMock')
        return mockFreezeWorkflow(wid, body)
      },
    )
  },

  /** POST api/studio/{wid}/unfreeze —— 回到调试；已固化 ref 作为历史保留。 */
  async unfreezeWorkflow(
    wid: string,
    body: { message: string },
  ): Promise<{ wid: string; execution_mode: 'debug'; previous_frozen_ref?: string }> {
    return realOrMock(
      () => request<{ wid: string; execution_mode: 'debug'; previous_frozen_ref?: string }>(
        `api/studio/${encode(wid)}/unfreeze`,
        { method: 'POST', body },
      ),
      async () => {
        const { mockUnfreezeWorkflow } = await import('./studioMock')
        return mockUnfreezeWorkflow(wid, body)
      },
    )
  },

  async createDraft(body: { intent: string; domain_pack?: string; exclude_atoms?: string[]; allow_llm?: boolean }): Promise<Workflow> {
    return realOrMock(
      async () => {
        const payload = await request<AnyRecord>('api/studio/drafts', { method: 'POST', body })
        const wid = String(payload.wid || payload.workflow_id)
        rememberStudioProvenance(wid, payload)
        const normalized = normalizeWorkflow(payload)
        normalized.wid = wid
        if (!normalized.graph?.nodes?.length) {
          const full = await request<AnyRecord>(`api/studio/${encode(wid)}`)
          return normalizeWorkflow(full, normalized)
        }
        return normalized
      },
      async () => {
        const { mockCreateDraft } = await import('./studioMock')
        return mockCreateDraft(body)
      },
    )
  },

  async registerAtom(wid: string, nodeId: string, body: AnyRecord): Promise<AnyRecord> {
    return request<AnyRecord>('api/atoms/register', {
      method: 'POST',
      body: { ...body, wid, node_id: nodeId },
    })
  },

  async getWorkflow(wid: string, rev?: number): Promise<Workflow> {
    return realOrMock(
      async () => {
        const suffix = rev ? `?rev=${rev}` : ''
        const payload = await request<AnyRecord>(`api/studio/${encode(wid)}${suffix}`)
        return normalizeWorkflow(payload, { wid })
      },
      async () => {
        const { mockGetWorkflow } = await import('./studioMock')
        return mockGetWorkflow(wid, rev)
      },
    )
  },

  async createDraftNode(wid: string, body: AnyRecord): Promise<AnyRecord> {
    return request<AnyRecord>(`api/studio/${encode(wid)}/nodes`, { method: 'POST', body: { draft: body } })
  },

  async updateDraftNode(wid: string, nid: string, body: AnyRecord): Promise<AnyRecord> {
    return request<AnyRecord>(`api/studio/${encode(wid)}/nodes/${encode(nid)}`, { method: 'PATCH', body: { draft: body } })
  },

  async nodeImplementation(wid: string, nid: string, rev?: number): Promise<AnyRecord> {
    const suffix = rev !== undefined ? `?rev=${encodeURIComponent(String(rev))}` : ''
    return request<AnyRecord>(`api/studio/${encode(wid)}/nodes/${encode(nid)}/implementation${suffix}`)
  },

  async updateNode(wid: string, nid: string, patch: AnyRecord): Promise<Workflow> {
    return realOrMock(
      async () => {
        await request<AnyRecord>(`api/studio/${encode(wid)}/nodes/${encode(nid)}`, {
          method: 'PATCH',
          body: patch,
        })
        return this.getWorkflow(wid)
      },
      async () => {
        const { mockUpdateNode } = await import('./studioMock')
        return mockUpdateNode(wid, nid, patch)
      },
    )
  },

  async addNode(wid: string, body: AnyRecord): Promise<Workflow> {
    return realOrMock(
      async () => {
        await request<AnyRecord>(`api/studio/${encode(wid)}/nodes`, { method: 'POST', body })
        return this.getWorkflow(wid)
      },
      async () => {
        const { mockAddNode } = await import('./studioMock')
        return mockAddNode(wid, body)
      },
    )
  },

  async deleteNode(wid: string, nid: string): Promise<Workflow> {
    return realOrMock(
      async () => {
        await request<AnyRecord>(`api/studio/${encode(wid)}/nodes/${encode(nid)}`, { method: 'DELETE' })
        return this.getWorkflow(wid)
      },
      async () => {
        const { mockDeleteNode } = await import('./studioMock')
        return mockDeleteNode(wid, nid)
      },
    )
  },

  async addEdge(wid: string, body: { from: string; to: string; via?: string; kind: string }): Promise<Workflow> {
    return realOrMock(
      async () => {
        await request<AnyRecord>(`api/studio/${encode(wid)}/edges`, { method: 'POST', body })
        return this.getWorkflow(wid)
      },
      async () => {
        const { mockAddEdge } = await import('./studioMock')
        return mockAddEdge(wid, body)
      },
    )
  },

  async replan(wid: string, instruction: string): Promise<{ diff: StudioDiff; proposed_rev: number | null; proposed?: Workflow; locked_nodes?: string[] }> {
    return realOrMock(
      async () => {
        const payload = await request<AnyRecord>(`api/studio/${encode(wid)}/replan`, {
          method: 'POST',
          body: { instruction },
        })
        return {
          diff: normalizeDiff(payload.diff || payload),
          proposed_rev: payload.proposed_rev === null || payload.proposed_rev === undefined ? null : Number(payload.proposed_rev),
          locked_nodes: payload.locked_nodes || [],
        }
      },
      async () => {
        const { mockReplan } = await import('./studioMock')
        return mockReplan(wid, instruction)
      },
    )
  },

  async validate(wid: string, rev?: number): Promise<Validation> {
    return realOrMock(
      async () => {
        const payload = await request<AnyRecord>(`api/studio/${encode(wid)}/validate`, {
          method: 'POST',
          body: rev ? { rev } : {},
        })
        return normalizeValidation(payload)
      },
      async () => {
        const { mockValidate } = await import('./studioMock')
        return mockValidate(wid, rev)
      },
    )
  },

  async startDebug(
    wid: string,
    body: { rev: number; node_id: string; mode: string },
  ): Promise<{ session_id: string | null; legacy?: DebugSession; total?: number }> {
    return realOrMock(
      async () => {
        const payload = await request<AnyRecord>(`api/studio/${encode(wid)}/debug`, { method: 'POST', body })
        const sid = payload.session_id || payload.sid || payload.debug_session_id
        if (sid) return { session_id: String(sid), total: Number(payload.total || 0) }
        const trace = payload.trace || payload.steps
        if (Array.isArray(trace)) return { session_id: null, legacy: legacyDebugSession(payload) }
        return { session_id: null }
      },
      async () => {
        const { mockStartDebug } = await import('./studioMock')
        return mockStartDebug(wid, body)
      },
    )
  },

  async debugSession(sessionId: string, cursor: number | string = 0): Promise<DebugSession> {
    const mockMode = await ensureStudioMode()
    if (mockMode) {
      const { mockPollDebugSession } = await import('./studioMock')
      return mockPollDebugSession(sessionId, cursor)
    }
    const payload = await request<AnyRecord>(
      `api/studio/debug/${encode(sessionId)}?cursor=${encodeURIComponent(String(cursor ?? 0))}`,
    )
    return normalizeDebugSession(payload)
  },

  async review(wid: string, baseRev: number, rev: number): Promise<StudioDiff> {
    return realOrMock(
      async () => {
        const payload = await request<AnyRecord>(`api/studio/${encode(wid)}/review`, {
          method: 'POST',
          body: { base_rev: baseRev, rev },
        })
        return normalizeDiff(payload)
      },
      async () => {
        const { mockReview } = await import('./studioMock')
        return mockReview(wid, baseRev, rev)
      },
    )
  },

  async applyDiff(wid: string, proposedRev: number, accepted: string[]): Promise<Workflow> {
    return realOrMock(
      async () => {
        await request<AnyRecord>(`api/studio/${encode(wid)}/apply-diff`, {
          method: 'POST',
          body: { proposed_rev: proposedRev, accepted },
        })
        return this.getWorkflow(wid)
      },
      async () => {
        const { mockApplyDiff } = await import('./studioMock')
        return mockApplyDiff(wid, proposedRev, accepted)
      },
    )
  },

  async setState(
    wid: string,
    to: 'review' | 'approved' | 'rejected',
    note: string,
    confirmedRisks: string[] = [],
  ): Promise<Workflow> {
    return realOrMock(
      async () => {
        await request<AnyRecord>(`api/studio/${encode(wid)}/state`, {
          method: 'POST',
          body: { to, note, confirmed_risks: confirmedRisks },
        })
        return this.getWorkflow(wid)
      },
      async () => {
        const { mockSetState } = await import('./studioMock')
        return mockSetState(wid, to, note)
      },
    )
  },

  async publish(wid: string, rev: number, message: string): Promise<{ workflow_ref: string; task?: Task }> {
    return realOrMock(
      async () => request<{ workflow_ref: string; task?: Task }>(`api/studio/${encode(wid)}/publish`, {
        method: 'POST',
        body: { rev, message },
      }),
      async () => {
        const { mockPublish } = await import('./studioMock')
        return mockPublish(wid, rev, message)
      },
    )
  },

  /* Workflow triggers ------------------------------------------------------ */

  async listTriggers(wid: string): Promise<WorkflowTrigger[]> {
    const payload = await request<AnyRecord>(`api/studio/${encode(wid)}/triggers`)
    const items = Array.isArray(payload) ? payload : payload.triggers || payload.items || []
    return (items as AnyRecord[]).map(normalizeTrigger)
  },

  async createTrigger(wid: string, body: WorkflowTriggerCreate): Promise<WorkflowTriggerResult> {
    const payload = await request<AnyRecord>(`api/studio/${encode(wid)}/triggers`, {
      method: 'POST',
      body,
    })
    return triggerResult(payload)
  },

  async updateTrigger(tid: string, body: WorkflowTriggerPatch): Promise<WorkflowTriggerResult> {
    const payload = await request<AnyRecord>(`api/triggers/${encode(tid)}`, {
      method: 'PATCH',
      body,
    })
    return triggerResult(payload)
  },

  async deleteTrigger(tid: string): Promise<null> {
    return request<null>(`api/triggers/${encode(tid)}`, { method: 'DELETE' })
  },

  async runTrigger(tid: string): Promise<{ job_id: string; status: string }> {
    return request<{ job_id: string; status: string }>(`api/triggers/${encode(tid)}/run`, {
      method: 'POST',
      body: {},
    })
  },

  async regenerateTriggerToken(tid: string): Promise<WorkflowTriggerResult> {
    return this.updateTrigger(tid, { regenerate_token: true })
  },
}

export function normalizeValidation(payload: AnyRecord): Validation {
  const asIssues = (value: unknown): ValidationIssue[] => {
    if (!Array.isArray(value)) return []
    return value.map((item, index) => {
      if (typeof item === 'string') return { id: `issue-${index}`, message: item }
      const record = item as AnyRecord
      return {
        id: String(record.id || record.code || `issue-${index}`),
        node_id: record.node_id || record.node,
        edge_id: record.edge_id,
        message: String(record.message || record.detail || record.msg || '未命名问题'),
        code: record.code,
        severity: record.severity,
      }
    })
  }
  const risks: Risk[] = Array.isArray(payload.risks)
    ? payload.risks.map((item, index) => {
        if (typeof item === 'string') return { id: `risk-${index}`, message: item, requires_ack: true }
        return {
          ...item,
          id: String(item.id || item.kind || `risk-${index}`),
          message: String(item.message || item.detail || item.kind || '风险项'),
          severity: String(item.severity || item.level || 'medium'),
          node_ids: item.node_ids || (item.node_id ? [String(item.node_id)] : []),
          requires_ack: item.requires_ack !== false,
        }
      })
    : []
  const coverageRaw = payload.coverage && typeof payload.coverage === 'object' ? payload.coverage as AnyRecord : null
  const coverage = coverageRaw
    ? (() => {
        const total = Number(coverageRaw.total ?? 0)
        const matched = Number(coverageRaw.matched ?? 0)
        const ratio = coverageRaw.ratio === undefined || coverageRaw.ratio === null
          ? (total > 0 ? matched / total : 0)
          : Number(coverageRaw.ratio)
        const gate = typeof coverageRaw.gate === 'string' ? coverageRaw.gate.toLowerCase() : undefined
        const unmatchedDetails = Array.isArray(coverageRaw.unmatched_details)
          ? coverageRaw.unmatched_details.map((item: unknown, index: number) => {
              if (typeof item === 'string') return { node_id: `unmatched-${index}`, reason: item }
              const record = item as AnyRecord
              return {
                ...record,
                node_id: record.node_id === undefined || record.node_id === null ? undefined : String(record.node_id),
                atom: record.atom === undefined || record.atom === null ? undefined : String(record.atom),
                reason: record.reason === undefined || record.reason === null ? undefined : String(record.reason),
                predicted_atoms: Array.isArray(record.predicted_atoms) ? record.predicted_atoms.map(String) : undefined,
                new_atom: record.new_atom === true,
                role: record.role === undefined || record.role === null ? undefined : String(record.role),
              }
            })
          : []
        const unmatchedNodes = Array.isArray(coverageRaw.unmatched_nodes)
          ? coverageRaw.unmatched_nodes.map(String)
          : []
        return {
          matched,
          total,
          ratio: Number.isFinite(ratio) ? ratio : 0,
          gate,
          unmatched_details: unmatchedDetails,
          unmatched_nodes: unmatchedNodes,
          warnings: Array.isArray(coverageRaw.warnings) ? coverageRaw.warnings.map(String) : [],
        }
      })()
    : null
  const missingEnv = Array.isArray(payload.missing_env)
    ? payload.missing_env.map((item: unknown) => typeof item === 'string' ? item : String((item as AnyRecord)?.name || item))
    : []
  const rawGaps = Array.isArray(payload.capability_gaps) ? payload.capability_gaps : []
  const capabilityGaps = rawGaps.length
    ? rawGaps
    : asIssues(payload.errors).filter((issue) => issue.code === 'capability_gap').map((issue) => issue.message)
  return {
    errors: asIssues(payload.errors),
    warnings: asIssues(payload.warnings),
    risks,
    coverage,
    missing_env: missingEnv,
    capability_gaps: capabilityGaps as Array<CapabilityGap | string>,
  }
}

export function debugReasonLabel(reason?: string | null): string {
  const map: Record<string, string> = {
    when_false: '条件为假',
    foreach_empty: '循环为空',
    upstream_failed: '上游失败',
    upstream_skipped: '上游被跳过',
    missing_input: '输入为空',
    optional: '可选节点',
    assertion: '断言失败',
    network: '网络错误',
    missing_env: '缺少环境变量',
    missing_llm: '未配置 LLM',
    cancelled: '已取消',
  }
  return reason ? map[reason] || reason : ''
}

export function debugReasonDetail(reason?: string | null): string {
  const map: Record<string, string> = {
    when_false: '条件为假，按 when 规则跳过该节点。',
    foreach_empty: '循环集合为空，节点没有可执行项。',
    upstream_failed: '上游节点失败，当前节点依赖未满足。',
    upstream_skipped: '上游节点被跳过，当前节点没有可用输入。',
    missing_input: '上游节点被跳过或输入为空，当前节点无法执行。',
    optional: '节点标记为可选，未满足条件时跳过。',
    assertion: '断言未通过，请查看 outputs 与断言参数。',
    network: '网络请求失败，请确认目标可达。',
    missing_env: '缺少项目环境变量，节点无法执行。',
    missing_llm: '未配置 LLM，无法执行该节点。',
    cancelled: '调试被取消。',
  }
  return reason ? map[reason] || '' : ''
}

export function normalizeDebugSession(payload: AnyRecord, fallbackDone = false): DebugSession {
  const rawNodes = Array.isArray(payload.nodes)
    ? payload.nodes
    : Array.isArray(payload.trace)
      ? payload.trace
      : Array.isArray(payload.steps)
        ? payload.steps
        : []
  const byNode = new Map<string, DebugNodeState>()
  rawNodes.forEach((item: AnyRecord, index: number) => {
    const nodeId = String(item.node_id || item.node || item.id || `node-${index}`)
    byNode.set(nodeId, {
      ...item,
      node_id: nodeId,
      status: String(item.status || 'pending'),
      reason: item.reason || null,
    })
  })
  const nodes: DebugNodeState[] = Array.from(byNode.values())
  const counts = { passed: 0, failed: 0, skipped: 0 }
  const byReason: Record<string, number> = {}
  nodes.forEach((node) => {
    if (node.status === 'passed') counts.passed += 1
    else if (node.status === 'failed') counts.failed += 1
    else if (node.status === 'skipped') {
      counts.skipped += 1
      const reason = node.reason || 'unknown'
      byReason[reason] = (byReason[reason] || 0) + 1
    }
  })
  const rawSummary = payload.summary && typeof payload.summary === 'object' ? payload.summary as AnyRecord : {}
  const summary = {
    ...rawSummary,
    passed: Number(rawSummary.passed ?? counts.passed),
    failed: Number(rawSummary.failed ?? counts.failed),
    skipped: Number(rawSummary.skipped ?? counts.skipped),
    total: Number(rawSummary.total ?? payload.total ?? nodes.length),
    by_reason: (rawSummary.by_reason && typeof rawSummary.by_reason === 'object')
      ? rawSummary.by_reason as Record<string, number>
      : byReason,
  }
  return {
    ...payload,
    session_id: payload.session_id ? String(payload.session_id) : undefined,
    nodes,
    cursor: payload.cursor ?? 0,
    done: payload.done === undefined ? fallbackDone : Boolean(payload.done),
    summary,
    variables: payload.variables,
    error: payload.error ?? null,
  }
}

export function legacyDebugSession(payload: AnyRecord): DebugSession {
  const trace = Array.isArray(payload.trace) ? payload.trace : Array.isArray(payload.steps) ? payload.steps : []
  return normalizeDebugSession({
    ...payload,
    nodes: trace.map((step: AnyRecord) => ({ ...step, status: step.status || 'passed' })),
    done: true,
    cursor: 0,
    legacy: true,
  }, true)
}

export function normalizeDiff(payload: AnyRecord): StudioDiff {
  const rawItems = payload.items || payload.changes || payload.diff || payload
  const nodes = Array.isArray((rawItems as AnyRecord)?.nodes) ? (rawItems as AnyRecord).nodes as AnyRecord[] : []
  const edges = Array.isArray((rawItems as AnyRecord)?.edges) ? (rawItems as AnyRecord).edges as AnyRecord[] : []
  const flat = Array.isArray(rawItems) ? rawItems as AnyRecord[] : []
  const mapped: DiffItem[] = []

  const labelFor = (kind: 'node' | 'edge', action: string, record: AnyRecord): string => {
    const before = record.before || {}
    const after = record.after || {}
    const source = action === 'removed' ? before : after
    if (kind === 'node') {
      const name = source.name || source.atom || source.id || record.id || '节点'
      return `${action === 'added' ? '新增' : action === 'removed' ? '删除' : '修改'}节点：${name}`
    }
    const from = source.from || before.from || '?'
    const to = source.to || before.to || '?'
    return `${action === 'added' ? '新增' : '删除'}边：${from} → ${to}${source.via ? `（${source.via}）` : ''}`
  }

  const push = (kind: 'node' | 'edge', record: AnyRecord, index: number) => {
    const action = String(record.action || record.op || record.type || record.change_type || (record.change_id ? 'changed' : 'changed'))
    const op = action === 'added' || action === 'add' ? 'add' : action === 'removed' || action === 'remove' || action === 'delete' ? 'remove' : 'change'
    const id = String(record.change_id || record.id || record.changeId || `${kind}-${index}`)
    const sourceKind = String(record.kind || kind)
    mapped.push({
      ...record,
      id,
      op,
      kind: sourceKind,
      node_id: sourceKind === 'node' ? String(record.id || record.node_id || '') : undefined,
      edge_id: sourceKind === 'edge' ? String(record.id || record.edge_id || '') : undefined,
      label: String(record.label || record.summary || labelFor(sourceKind === 'edge' ? 'edge' : 'node', action, record)),
      message: record.message || record.detail,
      before: record.before,
      after: record.after,
    })
  }

  nodes.forEach((record, index) => push('node', record, index))
  edges.forEach((record, index) => push('edge', record, nodes.length + index))
  flat.forEach((record, index) => {
    if (typeof record === 'string') {
      mapped.push({ id: `change-${index}`, op: 'change', kind: 'node', label: record })
    } else {
      push('node', record, mapped.length)
    }
  })

  return {
    ...payload,
    items: mapped,
    risks: Array.isArray(payload.risks)
      ? payload.risks.map((risk: any, index: number) => ({
          ...risk,
          id: String(risk.id || `risk-${index}`),
          message: String(risk.message || risk.detail || '风险项'),
          severity: String(risk.severity || risk.level || 'medium'),
          node_ids: risk.node_ids || (risk.node_id ? [String(risk.node_id)] : []),
          requires_ack: risk.requires_ack !== false,
        }))
      : [],
    audit_tail: payload.audit_tail || [],
  }
}

function unwrapSchedule(payload: AnyRecord): Schedule {
  return (payload?.schedule || payload) as Schedule
}

/* -------------------------------------------------------------------------- */
/* Explore-first：先跑通，再固化                                              */
/* -------------------------------------------------------------------------- */

export const exploreApi = {
  /** POST /api/studio/explore —— 启动一次探索（无 LLM 时后端 503）。 */
  start: (body: {
    intent: string
    success_criteria?: unknown[]
    domain_pack?: string
    allow_scripts?: boolean
    max_steps?: number
  }) => request<ExploreStartResult>('api/studio/explore', { method: 'POST', body }),

  /** GET /api/studio/explore/{eid}?cursor=N —— 增量事件。 */
  get: (eid: string, cursor = 0) =>
    request<ExploreSession>(
      `api/studio/explore/${encodeURIComponent(eid)}?cursor=${encodeURIComponent(String(cursor))}`,
    ),

  /** GET /api/studio/explore —— 历史探索。 */
  list: async (limit = 30): Promise<ExploreHistoryItem[]> => {
    const payload = await request<{ sessions?: ExploreHistoryItem[] }>(
      `api/studio/explore?limit=${encodeURIComponent(String(limit))}`,
    )
    return Array.isArray(payload.sessions) ? payload.sessions : []
  },

  /** POST /api/studio/explore/{eid}/crystallize —— 固化为普通 studio 草稿。 */
  crystallize: (eid: string, body: { name?: string; include_scripts?: boolean } = {}) =>
    request<ExploreCrystallizeResult>(
      `api/studio/explore/${encodeURIComponent(eid)}/crystallize`,
      { method: 'POST', body },
    ),

  /** 产物原始 URL（只读）；图片直接作为 src，HTML 仅做沙箱预览。 */
  artifactUrl: (eid: string, path: string) =>
    `api/studio/explore/${encodeURIComponent(eid)}/artifact?path=${encodeURIComponent(path)}`,
}

export function errorHint(error: unknown): string {
  if (!(error instanceof ApiError)) return ''
  if (error.status === 0) return '提示：后端未启动或端口不通。'
  if (error.status === 401) return '提示：登录已过期，请重新登录。'
  if (error.status === 404) return '提示：资源不存在，或当前后端尚未包含该接口。'
  if (error.status === 409) return '提示：存在冲突（例如锁定节点阻止了自动改写）。'
  if (error.status >= 500) return '提示：服务端内部错误，详细原因见服务端日志。'
  return ''
}
