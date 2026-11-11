export type AnyRecord = Record<string, any>

export interface Port {
  name: string
  type?: string
  required?: boolean
  description?: string
}

export interface Atom {
  name: string
  description?: string
  category?: string
  domain?: string
  inputs?: AnyRecord
  outputs?: AnyRecord
  input_ports?: Port[]
  output_ports?: Port[]
  required?: string[]
  tags?: string[]
  aliases?: string[]
  version?: string
  effects?: string[]
  pack?: string
}

export interface Pack {
  name: string
  version?: string
  domain?: string
  builtin?: boolean
  source?: string
  description?: string
  atom_count?: number
  capabilities?: string[]
  atoms?: string[] | { name: string }[]
  tags?: string[]
  manifest?: AnyRecord
}

export interface Suite {
  suite_id: string
  name: string
  scenario?: string
  workflow_ref?: string
  env?: AnyRecord
  tags?: string[]
  created_at?: number
  updated_at?: number
}

export interface Project {
  project_id: string
  name: string
  base_url?: string
  env?: AnyRecord
  suites?: Suite[]
  suite_count?: number
  last_run?: RunSummary | null
  pass_rate?: PassRate | null
  created_at?: number
  updated_at?: number
  archived_at?: number
}

export interface PassRate {
  passed: number
  total: number
  rate?: number | null
}

export interface RunSummary {
  run_id: string
  started_at?: number
  finished_at?: number
  duration_ms?: number
  summary?: AnyRecord
  conclusion?: AnyRecord
  meta?: AnyRecord
  status?: string
  last_status?: string
  /** 任务卡 last_run 摘要直接给出成功与否。 */
  success?: boolean
  project_id?: string
  suite_id?: string
  suite_name?: string
  passed?: number
  total?: number
  at?: number
}

export interface RunDetail {
  report: AnyRecord
  plan?: AnyRecord | null
  execution_mode?: 'debug' | 'frozen' | string
  frozen_ref?: string
}

export interface Task {
  task_id: string
  name: string
  mode: 'demo' | 'scenario' | 'intent' | 'regression' | 'workflow' | string
  scenario?: string
  intent?: string
  env?: AnyRecord
  env_keys?: string[]
  regression?: boolean
  no_llm?: boolean
  updated_at?: number
  last_run_id?: string
  last_status?: string
  workflow_ref?: string
  archived_at?: number
  meta?: AnyRecord
}

export interface Scenario {
  id: string
  name: string
  source?: string
  cases?: number
  error?: string
}

export interface Job {
  job_id: string
  mode?: string
  status: string
  created_at?: number
  started_at?: number
  finished_at?: number
  elapsed_ms?: number
  run_id?: string
  phases?: { name: string; status: string; detail?: string }[]
  summary?: AnyRecord
  conclusion?: AnyRecord
  error?: string | null
}

export interface Batch {
  batch_id: string
  project_id: string
  status: string
  suites: {
    suite_id: string
    name: string
    job_id: string
    run_id: string
    status: string
    passed: number
    total: number
    error?: string | null
  }[]
  aggregate: { passed: number; total: number; suites_passed: number; suites_failed: number; duration_ms: number }
}

export interface StatusPayload {
  workspace?: string
  root?: string
  atoms?: number
  packs?: number
  llm_enabled?: boolean
  version?: string
  scenarios?: number
  http_backend?: string
}

export interface Insight {
  available: boolean
  source?: 'llm' | 'rules' | string
  degraded_reason?: string
  summary_md?: string
  findings?: AnyRecord[]
  suggestions?: AnyRecord[]
  stats?: AnyRecord
  corrections?: AnyRecord[]
  generated_at?: string
  [key: string]: any
}

export interface Schedule {
  sid: string
  name: string
  target: { kind: 'project' | string; project_id: string; suite_ids?: string[] }
  trigger: {
    kind: 'cron' | 'interval' | 'webhook' | string
    expr?: string
    tz?: string
    minutes?: number
    token?: string
  }
  env_override?: AnyRecord
  enabled: boolean
  archived?: boolean
  created_at?: number
  updated_at?: number
  last_run_at?: number | null
  last_batch_id?: string | null
  last_status?: string | null
  next_run_at?: number | null
}

export interface ScheduleRunResult {
  job_id: string
  batch_id: string
}

export type TriggerKind = 'schedule' | 'interval' | 'webhook'

export interface TriggerSchedule {
  cron: string
  tz?: string
}

export interface TriggerInterval {
  minutes: number
}

export interface WorkflowTrigger {
  trigger_id: string
  workflow_ref: string
  kind: TriggerKind | string
  enabled: boolean
  schedule?: TriggerSchedule
  interval?: TriggerInterval
  webhook?: { token_hint?: string }
  next_run_at?: number | string | null
  last_status?: string | null
  last_job_id?: string | null
  env_override?: AnyRecord
  created_at?: number
  updated_at?: number
}

export interface WorkflowTriggerCreate {
  workflow_ref: string
  kind: TriggerKind
  schedule?: TriggerSchedule
  interval?: TriggerInterval
  env_override?: AnyRecord
}

export interface WorkflowTriggerPatch {
  enabled?: boolean
  schedule?: TriggerSchedule
  interval?: TriggerInterval
  env_override?: AnyRecord
  regenerate_token?: boolean
}

export interface WorkflowTriggerResult {
  trigger: WorkflowTrigger
  /** Webhook token in plaintext. Only returned on create / regenerate. */
  token?: string
}

export interface Artifact {
  artifact_id: string
  run_id: string
  project_id?: string
  name: string
  type: 'markdown' | 'image' | 'json' | 'xml' | 'text' | 'file' | string
  /** 平台运行报告（report）与任务交付物（deliverable）。旧数据可能缺失。 */
  kind?: 'report' | 'deliverable' | string
  /** 任务内部相对路径；旧 manifest 可能只有 file。 */
  path?: string
  file?: string
  step_id?: string
  case_id?: string
  bytes: number
  sha256?: string
  node_id?: string
  created_at?: number
  truncated?: boolean
  content?: string | null
  download_url?: string | null
  share_token?: string | null
  share_expires_at?: number | null
}

export interface ArtifactPageData {
  total: number
  items: Artifact[]
}

export interface ArtifactShare {
  url_token: string
  expires_at: number
  url?: string
}


export interface Execution {
  eid: string
  execution_id: string
  run_id: string
  job_id: string
  batch_id: string
  mode: string
  trigger: string
  project_id: string
  suite_id: string
  workflow_ref: string
  status: string
  run_status: string
  passed: number
  total: number
  duration_ms: number
  started_at?: number
  finished_at?: number
  created_at?: number
  error?: string
  conclusion?: AnyRecord
  summary?: AnyRecord
  phases?: { name: string; status: string; detail?: string }[]
  cancellable: boolean
  execution_mode?: 'debug' | 'frozen' | string
  frozen_ref?: string
}

export interface ExecutionPageData {
  executions: Execution[]
  total: number
  offset: number
  limit: number
}

export interface ExecutionCancelResult {
  eid: string
  job_id?: string
  batch_id?: string
  status: string
  cancelled_suites?: string[]
}

export interface ReportSummary {
  run_id: string
  project_id?: string
  suite_id?: string
  suite_name?: string
  trigger?: string
  status?: string
  conclusion?: AnyRecord
  summary?: AnyRecord
  passed?: number
  total?: number
  duration_ms?: number
  workflow_ref?: string
  has_insight?: boolean
  insight_source?: string
  sign_state?: 'unsigned' | 'signed' | 'voided' | string
  signed?: boolean
  voided?: boolean
  verdict?: string
  note?: string
  signed_by?: string
  signed_at?: number
  void_reason?: string
  voided_by?: string
  voided_at?: number
  started_at?: number
  finished_at?: number
}

export interface ReportPageData {
  reports: ReportSummary[]
  total: number
  offset: number
  limit: number
}

export interface StudioNode {
  node_id: string
  atom: string
  name?: string
  with?: AnyRecord
  capture?: AnyRecord
  origin?: 'ai' | 'human' | string
  locked?: boolean
  verified_at?: string | null
  ui?: { x?: number; y?: number }
  effects?: string[]
  input_ports?: Port[]
  output_ports?: Port[]
  [key: string]: any
}

export interface StudioEdge {
  edge_id?: string
  id?: string
  from: string
  to: string
  via?: string
  kind: 'data' | 'order' | string
}

export interface StudioGraph {
  nodes: StudioNode[]
  edges: StudioEdge[]
  env_schema?: AnyRecord
  message?: string
}

export interface ValidationIssue {
  id?: string
  node_id?: string
  edge_id?: string
  message: string
  code?: string
  severity?: string
}

export interface Risk {
  id?: string
  kind?: string
  message: string
  severity?: 'low' | 'medium' | 'high' | string
  node_ids?: string[]
  requires_ack?: boolean
  acknowledged?: boolean
}

export interface CoverageUnmatchedDetail {
  node_id?: string
  atom?: string
  reason?: string
  predicted_atoms?: string[]
  new_atom?: boolean
  role?: string
  [key: string]: any
}

export interface CoverageStat {
  matched: number
  total: number
  ratio: number
  /** 发布门禁：探索固化任务为 advisory，普通草稿为 blocking。 */
  gate?: 'advisory' | 'blocking' | string
  /** 未匹配节点的原子与原因，供前端折叠展示。 */
  unmatched_details?: CoverageUnmatchedDetail[]
  unmatched_nodes?: string[]
  warnings?: string[]
}

export interface CapabilityGap {
  atom?: string
  name?: string
  message?: string
  suggestions?: string[]
  suggestion?: string
  [key: string]: any
}

export interface Validation {
  errors?: ValidationIssue[]
  warnings?: ValidationIssue[]
  risks?: Risk[]
  coverage?: CoverageStat | null
  missing_env?: string[]
  capability_gaps?: Array<CapabilityGap | string>
}

export interface DiffItem {
  id: string
  op: 'add' | 'remove' | 'change' | string
  kind: 'node' | 'edge' | string
  label?: string
  message?: string
  node_id?: string
  edge_id?: string
  before?: AnyRecord
  after?: AnyRecord
  detail?: string
}

export interface StudioDiff {
  items?: DiffItem[]
  changes?: DiffItem[]
  risks?: Risk[]
  audit_tail?: AnyRecord[]
  base_rev?: number
  rev?: number
  [key: string]: any
}

export type DebugNodeStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped' | string

export interface DebugNodeState {
  node_id: string
  status: DebugNodeStatus
  reason?: string | null
  attempts?: number
  duration_ms?: number
  error?: unknown
  inputs?: AnyRecord
  outputs?: AnyRecord
  started_at?: number
  finished_at?: number
  [key: string]: any
}

export interface DebugSummary {
  passed: number
  failed: number
  skipped: number
  total?: number
  by_reason?: Record<string, number>
  [key: string]: any
}

export interface DebugSession {
  session_id?: string
  nodes: DebugNodeState[]
  cursor?: number | string
  done: boolean
  summary: DebugSummary
  variables?: AnyRecord
  error?: string | null
  legacy?: boolean
  [key: string]: any
}

export interface DebugStep {
  node_id?: string
  node?: string
  atom?: string
  name?: string
  status?: string
  duration_ms?: number
  duration?: number
  error?: string | null
  inputs?: AnyRecord
  outputs?: AnyRecord
  [key: string]: any
}

export interface DebugResult {
  trace?: DebugStep[]
  steps?: DebugStep[]
  variables?: AnyRecord
  error?: string
  debug_id?: string
  ran_at?: string
}

export interface GoalCheck {
  id: string
  kind?: string
  description?: string
  passed?: boolean
  evidence?: string
  [key: string]: any
}

export interface Workflow {
  wid: string
  name?: string
  intent?: string
  domain_pack?: string
  state?: string
  current_rev?: number
  rev?: number
  base_rev?: number
  published_ref?: string
  graph?: StudioGraph
  nodes?: StudioNode[]
  edges?: StudioEdge[]
  validation?: Validation
  planner?: 'llm' | 'rules' | 'template' | string
  plan_source?: 'llm' | 'rules' | 'template' | string
  confidence?: number
  assumptions?: string[]
  capability_gaps?: Array<CapabilityGap | string>
  audit?: AnyRecord[]
  audit_tail?: AnyRecord[]
  created_at?: number
  updated_at?: number
  node_count?: number
  archived_at?: number | null
  origin?: 'ai' | 'human' | 'explore' | string
  explore_id?: string
  pending_atoms?: string[]
  replay_status?: string
  replay?: AnyRecord
  /** 任务执行态：debug（默认，LLM 可自定义节点）/ frozen（确定性执行 wid@rev）。 */
  execution_mode?: 'debug' | 'frozen' | string
  frozen_ref?: string
  frozen_at?: number | null
  frozen_by?: string
  goal_checks?: GoalCheck[]
  /** 当前版本的最近一次正式运行；无运行时为 null。 */
  last_run?: RunSummary | null
  /** 当前版本是否满足固化条件（最近一次运行成功等）。 */
  freeze_ready?: boolean
  [key: string]: any
}

/* -------------------------------------------------------------------------- */
/* Explore-first (先跑通再固化)                                                */
/* -------------------------------------------------------------------------- */

export type ExploreCheckType = 'text_contains' | 'status_code' | 'regex' | 'file_exists' | string

export interface ExploreCheckSpec {
  id?: string
  type: ExploreCheckType
  value?: string
  equals?: number | string
  pattern?: string
  path?: string
  step?: number | string
  step_id?: string
  artifact?: string
  [key: string]: any
}

export interface ExploreCheck {
  id: string
  type: ExploreCheckType
  spec?: ExploreCheckSpec
  ok: boolean
  evidence?: string
}

export interface ExploreArtifact {
  path: string
  bytes?: number
  content_type?: string
}

export interface ExploreEvent {
  seq: number
  ts: number
  type: 'thought' | 'tool_call' | 'tool_result' | 'script' | 'verify' | 'end' | string
  [key: string]: any
}

export interface ExploreSummary {
  steps?: number
  tool_calls?: number
  scripts?: number
  duration_ms?: number
  reason?: string
  verdict?: string
  [key: string]: any
}

export interface ExploreSession {
  eid: string
  cursor?: number
  status: string
  done?: boolean
  intent?: string
  success_criteria?: unknown
  criteria?: ExploreCheckSpec[]
  summary?: ExploreSummary
  checks?: ExploreCheck[]
  verdict?: string
  artifacts?: ExploreArtifact[]
  events?: ExploreEvent[]
  error?: { code?: string; message?: string; [key: string]: any } | AnyRecord
  model?: string
  created_at?: number
  finished_at?: number
  [key: string]: any
}

export interface ExploreHistoryItem {
  eid: string
  intent: string
  status: string
  verdict?: string
  created_at?: number
  finished_at?: number
  summary?: ExploreSummary
}

export interface ExploreStartResult {
  eid: string
  status: string
  cursor?: number
}

export interface ExploreReplay {
  status?: string
  checks?: ExploreCheck[]
  errors?: AnyRecord[]
  duration_ms?: number
  [key: string]: any
}

/** 探索固化出的脚本原子契约（后端 atoms_created 的真实对象形状）。 */
export interface ExploreAtomCreated {
  name: string
  pack?: string
  path?: string
  entry?: string
  description?: string
  inputs?: AnyRecord
  outputs?: AnyRecord
  output_paths?: AnyRecord
  effects?: string[]
  idempotent?: boolean
  failure_class?: string
  origin?: string
  explore_id?: string
  trace_seq?: number
  source_script?: string
  code_sha256?: string
  inputs_snapshot?: string[]
  [key: string]: any
}

/** 未封装成原子的脚本 / 越域步骤。 */
export interface ExplorePendingAtom {
  name?: string
  path?: string
  reason?: string
  status?: string
  kind?: string
  code_sha256?: string
  exit_code?: number | null
  trace_seq?: number
  [key: string]: any
}

export interface ExploreCrystallizeResult {
  wid: string
  rev?: number
  explore_id?: string
  atoms_created?: ExploreAtomCreated[]
  pending_atoms?: ExplorePendingAtom[]
  replay?: ExploreReplay
  validation?: Validation | AnyRecord
  graph?: { nodes?: AnyRecord[]; edges?: AnyRecord[] }
  state?: string
  replay_status?: string
  [key: string]: any
}
