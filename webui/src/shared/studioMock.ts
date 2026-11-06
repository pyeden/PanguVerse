import type {
  AnyRecord,
  DebugSession,
  DebugNodeState,
  DiffItem,
  Risk,
  StudioDiff,
  StudioEdge,
  StudioGraph,
  StudioNode,
  Validation,
  ValidationIssue,
  Workflow,
} from './types'

const STORE_KEY = 'panguverse.studio.mock.v2'
const PROPOSED_PREFIX = 'panguverse.studio.proposed.v2.'

interface StoredWorkflow extends Workflow {
  revisions: Record<string, { graph: StudioGraph; message?: string; created_by?: string; created_at?: number }>
  audit: AnyRecord[]
}

interface MockStore {
  workflows: Record<string, StoredWorkflow>
}

let memoryStore: MockStore | null = null

const ATOMS: Record<string, { effects: string[]; required: string[] }> = {
  http_request: { effects: ['read'], required: [] },
  assert_status: { effects: ['pure'], required: ['expected'] },
  assert_json: { effects: ['pure'], required: ['path'] },
  assert_body_contains: { effects: ['pure'], required: ['text'] },
  json_get: { effects: ['pure'], required: ['path'] },
  set_var: { effects: ['pure'], required: ['name'] },
  fs_read_text: { effects: ['read'], required: ['path'] },
  fs_write_json: { effects: ['write'], required: ['path'] },
  fs_exists: { effects: ['read'], required: ['path'] },
  fs_delete: { effects: ['destructive'], required: ['path'] },
  assert_file_exists: { effects: ['pure'], required: ['path'] },
}

function now(): number {
  return Date.now() / 1000
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value))
}

function uid(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`
}

function graphFor(intent: string, pack: string, excluded: string[] = []): StudioGraph {
  const isFs = pack === 'local_fs'
  const base: StudioNode[] = isFs
    ? [
        {
          node_id: 'n-read',
          atom: 'fs_read_text',
          name: '读取待处理清单',
          with: { path: '${env.FS_ROOT}/orders.json' },
          origin: 'ai',
          locked: false,
          effects: ['read'],
          ui: { x: 40, y: 180 },
        },
        {
          node_id: 'n-write',
          atom: 'fs_write_json',
          name: '写回归档记录',
          with: { path: '${env.FS_ROOT}/archive/${order_id}.json', value: '${order}' },
          origin: 'ai',
          locked: false,
          effects: ['write'],
          ui: { x: 340, y: 80 },
        },
        {
          node_id: 'n-check',
          atom: 'assert_file_exists',
          name: '确认归档落盘',
          with: { path: '${env.FS_ROOT}/archive/${order_id}.json' },
          origin: 'ai',
          locked: false,
          effects: ['pure'],
          ui: { x: 640, y: 200 },
        },
      ]
    : [
        {
          node_id: 'n-login',
          atom: 'http_request',
          name: '登录并获取会话',
          with: {
            method: 'POST',
            url: '${env.BASE_URL}/api/auth/login',
            json: { username: '${env.API_USERNAME}', password: '${env.API_PASSWORD}' },
          },
          capture: { login: 'last_response' },
          origin: 'ai',
          locked: false,
          effects: ['mutate'],
          ui: { x: 40, y: 160 },
        },
        {
          node_id: 'n-status',
          atom: 'assert_status',
          name: '登录返回 200',
          with: { source: '${login}', expected: 200 },
          origin: 'ai',
          locked: false,
          effects: ['pure'],
          ui: { x: 340, y: 60 },
        },
        {
          node_id: 'n-token',
          atom: 'json_get',
          name: '提取访问令牌',
          with: { source: '${login}', path: 'access_token' },
          capture: { token: 'value' },
          origin: 'ai',
          locked: false,
          effects: ['pure'],
          ui: { x: 340, y: 270 },
        },
        {
          node_id: 'n-orders',
          atom: 'http_request',
          name: '拉取订单列表',
          with: {
            method: 'GET',
            url: '${env.BASE_URL}/api/orders',
            headers: { Authorization: 'Bearer ${token}' },
          },
          capture: { orders: 'last_response' },
          origin: 'ai',
          locked: false,
          effects: ['read'],
          ui: { x: 640, y: 160 },
        },
        {
          node_id: 'n-assert',
          atom: 'assert_body_contains',
          name: '确认返回订单数据',
          with: { source: '${orders}', text: 'orders' },
          origin: 'ai',
          locked: false,
          effects: ['pure'],
          ui: { x: 940, y: 160 },
        },
      ]

  const nodes = base.filter((node) => !excluded.includes(node.atom))
  const edges: StudioEdge[] = []
  if (!isFs) {
    const has = (id: string) => nodes.some((node) => node.node_id === id)
    if (has('n-login') && has('n-status')) edges.push({ edge_id: 'e-login-status', from: 'n-login', to: 'n-status', via: 'login', kind: 'data' })
    if (has('n-login') && has('n-token')) edges.push({ edge_id: 'e-login-token', from: 'n-login', to: 'n-token', via: 'login', kind: 'data' })
    if (has('n-token') && has('n-orders')) edges.push({ edge_id: 'e-token-orders', from: 'n-token', to: 'n-orders', via: 'token', kind: 'data' })
    if (has('n-orders') && has('n-assert')) edges.push({ edge_id: 'e-orders-assert', from: 'n-orders', to: 'n-assert', via: 'orders', kind: 'data' })
    if (has('n-status') && has('n-token')) edges.push({ edge_id: 'e-order-1', from: 'n-status', to: 'n-token', via: 'order-only', kind: 'order' })
  } else {
    edges.push(
      { edge_id: 'e-read-write', from: 'n-read', to: 'n-write', via: 'order-only', kind: 'order' },
      { edge_id: 'e-write-check', from: 'n-write', to: 'n-check', via: 'order-only', kind: 'order' },
    )
  }
  return { nodes, edges, message: `AI 依据「${intent.slice(0, 42)}」生成` }
}

function seed(): MockStore {
  const g1 = graphFor('登录后拉取订单并校验返回', 'api_testing')
  const rev1 = clone(g1)
  rev1.nodes = rev1.nodes.filter((node) => node.node_id !== 'n-assert')
  rev1.edges = rev1.edges.filter((edge) => edge.to !== 'n-assert')
  const rev2 = clone(g1)
  rev2.nodes = rev2.nodes.map((node) =>
    node.node_id === 'n-orders'
      ? { ...node, with: { ...node.with, timeout: 15 }, origin: 'human' }
      : node,
  )
  const workflow: StoredWorkflow = {
    wid: 'w-demo4a1c',
    name: '订单查询与断言',
    intent: '登录后拉取订单并校验返回',
    domain_pack: 'api_testing',
    state: 'draft',
    current_rev: 2,
    published_ref: '',
    graph: rev2,
    revisions: {
      '1': { graph: rev1, message: 'AI 初始规划', created_by: 'ai', created_at: now() - 1800 },
      '2': { graph: rev2, message: '人工补充订单断言并调整超时', created_by: 'human', created_at: now() - 900 },
    },
    audit: [
      { ts: now() - 1800, actor: 'planner', action: 'draft.created', to_state: 'draft', rev: 1, note: 'AI 生成 rev1' },
      { ts: now() - 900, actor: 'you', action: 'node.updated', to_state: 'draft', rev: 2, note: '补全订单断言' },
    ],
    created_at: now() - 1800,
    updated_at: now() - 900,
  }
  return { workflows: { [workflow.wid]: workflow } }
}

function load(): MockStore {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (raw) return JSON.parse(raw) as MockStore
  } catch {
    /* ignore corrupt demo state */
  }
  if (memoryStore) return memoryStore
  memoryStore = seed()
  save(memoryStore)
  return memoryStore
}

function save(store: MockStore): void {
  memoryStore = store
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(store))
  } catch {
    /* demo mode can stay in memory */
  }
}

function ensure(wid: string): StoredWorkflow {
  const store = load()
  const workflow = store.workflows[wid]
  if (!workflow) throw new Error('流程不存在')
  return workflow
}

function normalize(workflow: StoredWorkflow, rev?: number): Workflow {
  const targetRev = rev || workflow.current_rev || 1
  const revision = workflow.revisions[String(targetRev)]
  const graph = clone(revision?.graph || workflow.graph || { nodes: [], edges: [] })
  return clone({
    ...workflow,
    current_rev: workflow.current_rev,
    rev: targetRev,
    graph,
    validation: validateGraph(graph),
  })
}

function addRevision(workflow: StoredWorkflow, graph: StudioGraph, message: string, origin = 'human'): void {
  const rev = Math.max(0, ...Object.keys(workflow.revisions).map(Number)) + 1
  workflow.revisions[String(rev)] = {
    graph: clone(graph),
    message,
    created_by: origin,
    created_at: now(),
  }
  workflow.current_rev = rev
  workflow.graph = clone(graph)
  workflow.updated_at = now()
  workflow.audit = workflow.audit || []
  workflow.audit.push({ ts: now(), actor: origin === 'ai' ? 'planner' : 'you', action: 'revision.created', to_state: workflow.state, rev, note: message })
}

function effectsFor(node: StudioNode): string[] {
  if (node.effects?.length) return node.effects
  const meta = ATOMS[node.atom]
  if (meta) return meta.effects
  if (String(node.atom).startsWith('fs_')) return ['write']
  return ['pure']
}

function validateGraph(graph: StudioGraph): Validation {
  const errors: ValidationIssue[] = []
  const warnings: ValidationIssue[] = []
  const risks: Risk[] = []
  const captures = new Set<string>()
  const nodeIds = new Set(graph.nodes.map((node) => node.node_id))
  const dataIn = new Map<string, string[]>()
  graph.edges.forEach((edge) => {
    if (!nodeIds.has(edge.from) || !nodeIds.has(edge.to)) {
      errors.push({ id: `edge-${edge.edge_id || edge.from}`, edge_id: edge.edge_id, message: `边引用了不存在的节点：${edge.from} → ${edge.to}` })
    }
    if (edge.kind === 'data') {
      const list = dataIn.get(edge.to) || []
      list.push(edge.via || edge.from)
      dataIn.set(edge.to, list)
    }
  })
  const ordered = graph.nodes
  ordered.forEach((node) => {
    const required = ATOMS[node.atom]?.required || []
    required.forEach((key) => {
      const value = node.with?.[key]
      if (value === undefined || value === null || value === '') {
        errors.push({ id: `req-${node.node_id}-${key}`, node_id: node.node_id, message: `${node.name || node.atom} 缺少必填参数 ${key}` })
      }
    })
    const refs = new Set<string>()
    const scan = (value: unknown) => {
      if (typeof value === 'string') {
        for (const match of value.matchAll(/\$\{([a-zA-Z0-9_.-]+)\}/g)) {
          const key = match[1]
          if (key.startsWith('env.')) continue
          refs.add(key)
        }
      } else if (Array.isArray(value)) value.forEach(scan)
      else if (value && typeof value === 'object') Object.values(value as AnyRecord).forEach(scan)
    }
    scan(node.with)
    const localCaptures = new Set([...captures, ...(dataIn.get(node.node_id) || [])])
    refs.forEach((ref) => {
      if (!localCaptures.has(ref)) {
        errors.push({
          id: `var-${node.node_id}-${ref}`,
          node_id: node.node_id,
          message: `未定义变量 \${${ref}}（${node.name || node.atom}）`,
        })
      }
    })
    Object.keys(node.capture || {}).forEach((key) => captures.add(key))
    const effects = effectsFor(node)
    const method = String(node.with?.method || 'GET').toUpperCase()
    if (effects.includes('destructive')) {
      warnings.push({ id: `destructive-${node.node_id}`, node_id: node.node_id, message: `${node.name || node.atom} 具有破坏性副作用` })
      risks.push({ id: `risk-destructive-${node.node_id}`, kind: 'destructive', message: `${node.name || node.atom} 可能删除或覆盖数据`, severity: 'high', node_ids: [node.node_id], requires_ack: true })
    } else if (effects.includes('write') || effects.includes('mutate') || (node.atom === 'http_request' && method !== 'GET')) {
      risks.push({ id: `risk-write-${node.node_id}`, kind: 'write', message: `${node.name || node.atom} 会写入或改变外部状态`, severity: 'medium', node_ids: [node.node_id], requires_ack: true })
    }
    const refsText = JSON.stringify(node.with || {})
    if (/\$\{env\.[A-Z0-9_]*(PASSWORD|TOKEN|SECRET|KEY)[A-Z0-9_]*\}/i.test(refsText)) {
      risks.push({ id: `risk-secret-${node.node_id}`, kind: 'secret', message: `${node.name || node.atom} 使用密钥变量（出口已打码）`, severity: 'medium', node_ids: [node.node_id], requires_ack: true })
    }
  })
  if (!graph.nodes.length) errors.push({ id: 'empty-graph', message: '流程至少需要一个节点' })
  return { errors, warnings, risks }
}

function validateWorkflow(wid: string, rev?: number): Validation {
  const workflow = ensure(wid)
  const targetRev = rev || workflow.current_rev
  const revision = workflow.revisions[String(targetRev)]
  return validateGraph(revision?.graph || workflow.graph)
}

export async function mockListWorkflows(): Promise<Workflow[]> {
  const store = load()
  return Object.values(store.workflows)
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
    .map((workflow) => normalize(workflow))
}

export async function mockCreateDraft(body: { intent: string; domain_pack?: string; exclude_atoms?: string[] }): Promise<Workflow> {
  const store = load()
  const pack = body.domain_pack || 'auto'
  const graph = graphFor(body.intent, pack, body.exclude_atoms || [])
  const wid = uid('w')
  const workflow: StoredWorkflow = {
    wid,
    name: body.intent.trim().slice(0, 28) || '未命名流程',
    intent: body.intent,
    domain_pack: pack,
    state: 'draft',
    current_rev: 1,
    published_ref: '',
    planner: 'llm',
    plan_source: 'llm',
    confidence: 0.78,
    assumptions: [],
    capability_gaps: [],
    graph,
    revisions: {
      '1': { graph: clone(graph), message: 'AI 初始规划', created_by: 'ai', created_at: now() },
    },
    audit: [{ ts: now(), actor: 'planner', action: 'draft.created', to_state: 'draft', rev: 1, note: 'AI 生成 rev1' }],
    created_at: now(),
    updated_at: now(),
  }
  store.workflows[wid] = workflow
  save(store)
  return normalize(workflow)
}

export async function mockGetWorkflow(wid: string, rev?: number): Promise<Workflow> {
  return normalize(ensure(wid), rev)
}

export async function mockUpdateNode(wid: string, nid: string, patch: AnyRecord): Promise<Workflow> {
  const store = load()
  const workflow = ensure(wid)
  const graph: StudioGraph = clone(workflow.graph || { nodes: [], edges: [] })
  const index = graph.nodes.findIndex((node) => node.node_id === nid)
  if (index < 0) throw new Error('节点不存在')
  const current = graph.nodes[index]
  const next: StudioNode = {
    ...current,
    ...patch,
    node_id: nid,
    with: patch.with !== undefined ? { ...(current.with || {}), ...patch.with } : current.with,
    origin: 'human',
    locked: patch.locked !== undefined ? !!patch.locked : current.locked,
  }
  graph.nodes[index] = next
  addRevision(workflow, graph, patch.message || `人工修改节点 ${next.name || nid}`)
  store.workflows[wid] = workflow
  save(store)
  return normalize(workflow)
}

export async function mockAddNode(wid: string, body: AnyRecord): Promise<Workflow> {
  const store = load()
  const workflow = ensure(wid)
  const graph: StudioGraph = clone(workflow.graph || { nodes: [], edges: [] })
  const maxX = graph.nodes.reduce((max, node) => Math.max(max, Number(node.ui?.x || 0)), 0)
  const node: StudioNode = {
    node_id: body.node_id || uid('n'),
    atom: body.atom || 'log',
    name: body.name || body.atom || '新节点',
    with: body.with || {},
    capture: body.capture || {},
    origin: 'human',
    locked: false,
    effects: body.effects || effectsFor({ node_id: '', atom: body.atom || 'log' }),
    ui: body.ui || { x: maxX + 300, y: 180 },
  }
  graph.nodes.push(node)
  addRevision(workflow, graph, `新增节点 ${node.name}`)
  store.workflows[wid] = workflow
  save(store)
  return normalize(workflow)
}

export async function mockDeleteNode(wid: string, nid: string): Promise<Workflow> {
  const store = load()
  const workflow = ensure(wid)
  const graph: StudioGraph = clone(workflow.graph || { nodes: [], edges: [] })
  const node = graph.nodes.find((item) => item.node_id === nid)
  if (node?.locked) throw new Error('节点已锁定，不能删除')
  graph.nodes = graph.nodes.filter((item) => item.node_id !== nid)
  graph.edges = graph.edges.filter((edge) => edge.from !== nid && edge.to !== nid)
  addRevision(workflow, graph, `删除节点 ${node?.name || nid}`)
  store.workflows[wid] = workflow
  save(store)
  return normalize(workflow)
}

export async function mockAddEdge(wid: string, body: { from: string; to: string; via?: string; kind: string }): Promise<Workflow> {
  const store = load()
  const workflow = ensure(wid)
  const graph: StudioGraph = clone(workflow.graph || { nodes: [], edges: [] })
  if (!graph.nodes.some((node) => node.node_id === body.from) || !graph.nodes.some((node) => node.node_id === body.to)) {
    throw new Error('连线两端节点不存在')
  }
  if (graph.edges.some((edge) => edge.from === body.from && edge.to === body.to && edge.kind === body.kind)) {
    return normalize(workflow)
  }
  const edge: StudioEdge = {
    edge_id: uid('e'),
    from: body.from,
    to: body.to,
    via: body.kind === 'data' ? body.via || body.from : 'order-only',
    kind: body.kind,
  }
  graph.edges.push(edge)
  addRevision(workflow, graph, `新增${body.kind === 'data' ? '数据' : '顺序'}边 ${body.from} → ${body.to}`)
  store.workflows[wid] = workflow
  save(store)
  return normalize(workflow)
}

export async function mockReplan(wid: string, instruction: string): Promise<{ diff: StudioDiff; proposed_rev: number; proposed: Workflow; locked_nodes: string[] }> {
  const workflow = ensure(wid)
  const graph: StudioGraph = clone(workflow.graph || { nodes: [], edges: [] })
  const proposedRev = (workflow.current_rev || 1) + 1
  const newNode: StudioNode = {
    node_id: uid('n'),
    atom: 'assert_latency',
    name: '新增：响应耗时上限',
    with: { source: '${orders}', max_ms: 1200 },
    origin: 'ai',
    locked: false,
    effects: ['pure'],
    ui: { x: 940, y: 320 },
  }
  graph.nodes.push(newNode)
  if (graph.nodes.some((node) => node.node_id === 'n-orders')) {
    graph.edges.push({ edge_id: uid('e'), from: 'n-orders', to: newNode.node_id, via: 'orders', kind: 'data' })
  }
  const targetId = graph.nodes.find((node) => node.node_id === 'n-orders')?.node_id
  const items: DiffItem[] = [
    { id: 'change-timeout', op: 'change', kind: 'node', node_id: targetId, label: '订单请求超时从 10s 调整为 15s', message: `依据：${instruction}` },
    { id: `add-${newNode.node_id}`, op: 'add', kind: 'node', node_id: newNode.node_id, label: '新增性能断言：响应耗时 ≤ 1200ms' },
  ]
  const diff: StudioDiff = {
    base_rev: workflow.current_rev || 1,
    rev: proposedRev,
    proposed_rev: proposedRev,
    items,
    risks: validateGraph(graph).risks,
    audit_tail: workflow.audit.slice(-3),
  }
  try {
    localStorage.setItem(PROPOSED_PREFIX + wid, JSON.stringify({ proposedRev, graph, diff }))
  } catch {
    /* ignore */
  }
  return { diff, proposed_rev: proposedRev, proposed: normalize({ ...workflow, graph, current_rev: proposedRev }), locked_nodes: graph.nodes.filter((node) => node.locked).map((node) => node.node_id) }
}

export async function mockValidate(wid: string, rev?: number): Promise<Validation> {
  return validateWorkflow(wid, rev)
}

interface MockDebugSession {
  sid: string
  wid: string
  mode: string
  nodes: StudioNode[]
  startedAt: number
  cursor: number
  variables: AnyRecord
}

const mockDebugSessions = new Map<string, MockDebugSession>()

export async function mockStartDebug(
  wid: string,
  body: { rev: number; node_id: string; mode: string },
): Promise<{ session_id: string; total: number; legacy?: never }> {
  const workflow = ensure(wid)
  const graph = workflow.revisions[String(body.rev)]?.graph || workflow.graph
  const index = Math.max(0, graph.nodes.findIndex((node) => node.node_id === body.node_id))
  let selected = graph.nodes
  if (body.mode === 'only') selected = graph.nodes.slice(index, index + 1)
  else if (body.mode === 'from_start') selected = graph.nodes.slice(0, index + 1)
  else selected = graph.nodes.slice(index)
  const sid = uid('dbg')
  mockDebugSessions.set(sid, {
    sid,
    wid,
    mode: body.mode,
    nodes: clone(selected),
    startedAt: Date.now(),
    cursor: 0,
    variables: {},
  })
  return { session_id: sid, total: selected.length }
}

export async function mockPollDebugSession(sessionId: string, cursor: number | string = 0): Promise<DebugSession> {
  const session = mockDebugSessions.get(sessionId)
  if (!session) throw new Error('调试会话不存在或已过期')
  const perNodeMs = 420
  const total = session.nodes.length
  const elapsed = (Date.now() - session.startedAt) / perNodeMs
  const finishedCount = Math.min(total, Math.floor(elapsed))
  const runningIndex = finishedCount < total ? finishedCount : -1
  let previousFailed = false
  const nodes: DebugNodeState[] = session.nodes.map((node, index) => {
    let status = 'pending'
    let reason: string | null = null
    let error: string | null = null
    if (index < finishedCount) {
      if (String(node.with?.when) === 'false') {
        status = 'skipped'
        reason = 'when_false'
      } else if (previousFailed) {
        status = 'skipped'
        reason = 'upstream_failed'
      } else if (node.node_id === 'n-check' && session.mode === 'downstream') {
        status = 'failed'
        reason = 'assertion'
        error = '断言失败：目标目录不可写（演示数据）'
      } else {
        status = 'passed'
      }
    } else if (index === runningIndex) {
      status = 'running'
    }
    if (status === 'failed') previousFailed = true
    const outputs: AnyRecord = status === 'running' || status === 'pending'
      ? {}
      : {
          status_code: 200,
          ok: status === 'passed',
          elapsed_ms: 42 + index * 17,
          preview: node.atom.startsWith('html_') ? '<html>…</html>' : 'demo-value',
        }
    Object.entries(node.capture || {}).forEach(([key]) => {
      if (status === 'passed') {
        session.variables[key] = { value: key.toLowerCase().includes('token') ? '***' : 'demo-value', from: node.node_id, snapshot: true }
      }
    })
    return {
      node_id: node.node_id,
      node: node.name,
      atom: node.atom,
      status,
      reason,
      duration_ms: status === 'pending' || status === 'running' ? undefined : 42 + index * 17,
      inputs: { ...(node.with || {}), password: '***' },
      outputs,
      error,
    }
  })
  session.cursor = (Number(cursor) || 0) + 1
  const summary = {
    passed: nodes.filter((node) => node.status === 'passed').length,
    failed: nodes.filter((node) => node.status === 'failed').length,
    skipped: nodes.filter((node) => node.status === 'skipped').length,
    total,
    by_reason: nodes.filter((node) => node.status === 'skipped').reduce((acc: Record<string, number>, node) => {
      const reason = node.reason || 'unknown'
      acc[reason] = (acc[reason] || 0) + 1
      return acc
    }, {}),
  }
  return {
    session_id: sessionId,
    nodes,
    cursor: session.cursor,
    done: finishedCount >= total,
    summary,
    variables: session.variables,
  }
}

function graphDiff(base: StudioGraph, target: StudioGraph): DiffItem[] {
  const items: DiffItem[] = []
  const baseNodes = new Map(base.nodes.map((node) => [node.node_id, node]))
  const targetNodes = new Map(target.nodes.map((node) => [node.node_id, node]))
  for (const [id, node] of targetNodes) {
    if (!baseNodes.has(id)) {
      items.push({ id: `add-${id}`, op: 'add', kind: 'node', node_id: id, label: `新增节点：${node.name || node.atom}` })
    } else {
      const before = baseNodes.get(id)!
      if (JSON.stringify(before.with || {}) !== JSON.stringify(node.with || {}) || before.atom !== node.atom || before.name !== node.name || before.locked !== node.locked) {
        items.push({ id: `change-${id}`, op: 'change', kind: 'node', node_id: id, label: `修改节点：${node.name || node.atom}`, before: before as AnyRecord, after: node as AnyRecord })
      }
    }
  }
  for (const [id, node] of baseNodes) {
    if (!targetNodes.has(id)) items.push({ id: `remove-${id}`, op: 'remove', kind: 'node', node_id: id, label: `删除节点：${node.name || node.atom}` })
  }
  const baseEdges = new Set(base.edges.map((edge) => `${edge.from}|${edge.to}|${edge.kind}`))
  const targetEdges = new Set(target.edges.map((edge) => `${edge.from}|${edge.to}|${edge.kind}`))
  target.edges.forEach((edge) => {
    if (!baseEdges.has(`${edge.from}|${edge.to}|${edge.kind}`)) items.push({ id: `add-${edge.edge_id || edge.from + edge.to}`, op: 'add', kind: 'edge', edge_id: edge.edge_id, label: `新增边：${edge.from} → ${edge.to}` })
  })
  base.edges.forEach((edge) => {
    if (!targetEdges.has(`${edge.from}|${edge.to}|${edge.kind}`)) items.push({ id: `remove-${edge.edge_id || edge.from + edge.to}`, op: 'remove', kind: 'edge', edge_id: edge.edge_id, label: `删除边：${edge.from} → ${edge.to}` })
  })
  return items
}

export async function mockReview(wid: string, baseRev: number, rev: number): Promise<StudioDiff> {
  const workflow = ensure(wid)
  let proposed: { proposedRev: number; graph: StudioGraph; diff: StudioDiff } | null = null
  try {
    const raw = localStorage.getItem(PROPOSED_PREFIX + wid)
    if (raw) proposed = JSON.parse(raw)
  } catch {
    proposed = null
  }
  const target: StudioGraph = proposed && Number(proposed.proposedRev) === Number(rev)
    ? proposed.graph
    : (workflow.revisions[String(rev)]?.graph || workflow.graph || { nodes: [], edges: [] })
  const base: StudioGraph = workflow.revisions[String(baseRev)]?.graph || workflow.graph || { nodes: [], edges: [] }
  const items = proposed && Number(proposed.proposedRev) === Number(rev)
    ? proposed.diff.items || []
    : graphDiff(base, target)
  const validation = validateGraph(target)
  const risks: Risk[] = [
    ...(validation.risks || []),
    ...(workflow.graph?.nodes || [])
      .filter((node) => node.locked)
      .map((node) => ({ id: `risk-lock-${node.node_id}`, kind: 'locked', message: `锁定节点「${node.name || node.atom}」不会被自动改写`, severity: 'low', node_ids: [node.node_id], requires_ack: false })),
  ]
  const dedup = new Map(risks.map((risk) => [risk.id, risk]))
  return {
    base_rev: baseRev,
    rev,
    items,
    risks: [...dedup.values()],
    audit_tail: workflow.audit.slice(-8),
  }
}

export async function mockApplyDiff(wid: string, proposedRev: number, accepted: string[]): Promise<Workflow> {
  const store = load()
  const workflow = ensure(wid)
  let proposed: { proposedRev: number; graph: StudioGraph; diff: StudioDiff } | null = null
  try {
    const raw = localStorage.getItem(PROPOSED_PREFIX + wid)
    if (raw) proposed = JSON.parse(raw)
  } catch {
    proposed = null
  }
  if (!proposed) throw new Error('没有待应用的 AI 提议')
  const acceptedSet = new Set(accepted)
  if (acceptedSet.size === 0) throw new Error('至少接受一条变更')
  const graph = clone(proposed.graph)
  workflow.revisions[String(proposedRev)] = {
    graph: clone(graph),
    message: `应用 AI 提议（接受 ${accepted}/${proposed.diff.items?.length || 0} 项）`,
    created_by: 'human',
    created_at: now(),
  }
  workflow.current_rev = proposedRev
  workflow.graph = clone(graph)
  workflow.updated_at = now()
  workflow.audit.push({ ts: now(), actor: 'you', action: 'diff.applied', to_state: workflow.state, rev: proposedRev, note: `接受 ${acceptedSet.size} 条变更` })
  store.workflows[wid] = workflow
  save(store)
  try {
    localStorage.removeItem(PROPOSED_PREFIX + wid)
  } catch {
    /* ignore */
  }
  return normalize(workflow)
}

export async function mockSetState(wid: string, to: 'review' | 'approved' | 'rejected', note: string): Promise<Workflow> {
  const store = load()
  const workflow = ensure(wid)
  const from = workflow.state
  workflow.state = to
  workflow.updated_at = now()
  workflow.audit.push({ ts: now(), actor: 'you', action: 'state.changed', from_state: from, to_state: to, rev: workflow.current_rev, note })
  store.workflows[wid] = workflow
  save(store)
  return normalize(workflow)
}

export async function mockPublish(wid: string, rev: number, message: string): Promise<{ workflow_ref: string }> {
  const store = load()
  const workflow = ensure(wid)
  if (workflow.state !== 'approved') throw new Error('流程尚未批准，不能发布')
  if (!message.trim()) throw new Error('版本说明不能为空')
  const workflowRef = `${wid}@${rev}`
  workflow.published_ref = workflowRef
  workflow.state = 'published'
  workflow.updated_at = now()
  workflow.audit.push({ ts: now(), actor: 'you', action: 'workflow.published', to_state: 'published', rev, note: message })
  const target = workflow.revisions[String(rev)]
  if (target) target.message = message
  store.workflows[wid] = workflow
  save(store)
  return { workflow_ref: workflowRef }
}

export function mockReset(): void {
  try {
    localStorage.removeItem(STORE_KEY)
  } catch {
    /* ignore */
  }
  memoryStore = null
}
