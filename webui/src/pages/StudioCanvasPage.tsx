import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api, debugReasonDetail, debugReasonLabel, isStudioMock, onStudioMockChange, readStudioProvenance, studioApi } from '../shared/api'
import { navigate, href, useRoute } from '../shared/router'
import { errorText, formatDuration, pretty, statusLabel } from '../shared/utils'
import { contractText, effectsText, isNewAtomNode, readNewAtomCache } from '../shared/atomContracts'
import { COVERAGE_ADVISORY_NOTE, blockingValidationErrors, coverageGate, coverageTone } from '../shared/coverage'
import type {
  AnyRecord,
  Atom,
  DebugNodeState,
  DebugSession,
  DebugSummary,
  ExploreAtomCreated,
  Risk,
  StudioEdge,
  StudioNode,
  Validation,
  ValidationIssue,
  Workflow,
} from '../shared/types'
import { Icon } from '../components/Icon'
import { CoverageLine } from '../components/CoverageLine'
import { TriggerPanel, WorkflowRunsPanel } from '../components/TriggerPanel'
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Field,
  IconButton,
  JsonBlock,
  JsonTree,
  Loading,
  Modal,
  SearchInput,
  Select,
  StatusBadge,
  useToast,
} from '../components/ui'

interface AtomNodeData extends Record<string, unknown> {
  studioNode: StudioNode
  issues: ValidationIssue[]
  risks: Risk[]
  newAtomContract?: ExploreAtomCreated | null
  debugStatus?: string
  debugReason?: string
  debugCurrent?: boolean
}

interface GapItem {
  atom: string
  suggestions: string[]
  message?: string
}

function gapItems(value: unknown): GapItem[] {
  if (!Array.isArray(value)) return []
  return value.map((item, index) => {
    if (typeof item === 'string') return { atom: item, suggestions: [] }
    const record = item as AnyRecord
    const suggestions = Array.isArray(record.suggestions)
      ? record.suggestions.map(String)
      : record.suggestion
        ? [String(record.suggestion)]
        : []
    return {
      atom: String(record.atom || record.name || record.missing_atom || record.message || `gap-${index}`),
      suggestions,
      message: record.message ? String(record.message) : undefined,
    }
  })
}

function summarizeNodes(nodes: DebugNodeState[], total: number, base?: DebugSummary): DebugSummary {
  let passed = 0
  let failed = 0
  let skipped = 0
  const byReason: Record<string, number> = {}
  nodes.forEach((node) => {
    if (node.status === 'passed') passed += 1
    else if (node.status === 'failed') failed += 1
    else if (node.status === 'skipped') {
      skipped += 1
      const reason = node.reason || 'unknown'
      byReason[reason] = (byReason[reason] || 0) + 1
    }
  })
  return {
    ...(base || {}),
    passed,
    failed,
    skipped,
    total: total || base?.total || nodes.length,
    by_reason: byReason,
  }
}

type AtomFlowNode = Node<AtomNodeData, 'atom'>

function atomEffects(node: StudioNode): string[] {
  if (node.effects?.length) return node.effects
  const atom = String(node.atom || '')
  const method = String(node.with?.method || '').toUpperCase()
  if (atom === 'http_request') return method && method !== 'GET' ? ['mutate'] : ['read']
  if (atom === 'fs_delete') return ['destructive']
  if (atom === 'fs_write_json' || atom === 'fs_write_text' || atom === 'fs_copy') return ['write']
  if (atom.startsWith('fs_')) return ['read']
  return ['pure']
}

const AtomNode = memo(function AtomNode({ data, selected }: NodeProps<AtomFlowNode>) {
  const node = data.studioNode
  const isNewAtom = isNewAtomNode(node)
  const contract = data.newAtomContract || null
  const debugStatus = data.debugStatus || ''
  const debugReason = data.debugReason || ''
  const reasonLabel = debugReasonLabel(debugReason)
  const effects = atomEffects(node)
  const hasDestructive = effects.includes('destructive')
  const hasWrite = effects.includes('write') || effects.includes('mutate')
  const errors = data.issues.length
  const risks = data.risks.length
  return (
    <div
      className={`flow-node ${selected ? 'is-selected' : ''} ${errors ? 'has-error' : ''} ${node.locked ? 'is-locked' : ''} ${debugStatus ? `is-debug-${debugStatus}` : ''} ${data.debugCurrent ? 'is-debug-current' : ''}`}
      data-node-id={node.node_id}
      data-debug-status={debugStatus || undefined}
      data-debug-reason={debugReason || undefined}
    >
      <Handle type="target" position={Position.Left} className="flow-handle" />
      <div className="flow-node-top">
        <span className={`flow-status ${debugStatus ? `debug-${debugStatus}` : ''}`} aria-hidden="true" />
        <span className="flow-atom mono">{node.atom}</span>
        <span className={`flow-origin ${node.origin === 'human' ? 'human' : 'ai'}`}>{node.origin === 'human' ? '人工' : 'AI'}</span>
        {node.locked ? <span className="flow-lock" title="锁定节点，AI 不可改写"><Icon name="lock" size={11} /></span> : null}
        {debugStatus ? (
          <span className={`flow-debug-chip ${debugStatus}`} title={reasonLabel ? `${statusLabel(debugStatus)} · ${reasonLabel}` : statusLabel(debugStatus)}>
            {debugStatus === 'running' ? <span className="mini-pulse" /> : null}
            {statusLabel(debugStatus)}
          </span>
        ) : null}
      </div>
      <strong className="flow-name">{node.name || node.atom}</strong>
      {isNewAtom ? (
        <div className="flow-new-atom" data-testid="canvas-new-atom" data-atom={node.atom}>
          <span className="flow-new-atom-badge"><Icon name="spark" size={9} />新原子</span>
          <b className="flow-new-atom-name mono">{node.atom}</b>
          <span className="flow-new-atom-summary">{contract ? `${effectsText(contract.effects)} · ${Object.keys(contract.inputs || {}).length} 入 / ${Object.keys(contract.outputs || {}).length} 出` : '契约待读取'}</span>
        </div>
      ) : null}
      <div className="flow-params">
        {Object.entries(node.with || {}).slice(0, 2).map(([key, value]) => (
          <span key={key}><b>{key}</b><em>{typeof value === 'object' ? '{…}' : String(value).replace(/\$\{env\.([^}]+)\}/g, '${$1}').slice(0, 22)}</em></span>
        ))}
        {!Object.keys(node.with || {}).length ? <span className="muted">无参数</span> : null}
      </div>
      {(debugStatus === 'skipped' || debugStatus === 'failed') && reasonLabel ? (
        <div className="flow-debug-reason" data-debug-reason-text={debugReason}>
          {debugStatus === 'skipped' ? '跳过' : '失败'} · {reasonLabel} <code>{debugReason}</code>
        </div>
      ) : null}
      <div className="flow-node-foot">
        <div className="flow-effects">
          {hasDestructive ? <span className="effect-badge destructive">破坏性</span> : hasWrite ? <span className="effect-badge write">写副作用</span> : <span className="effect-badge pure">纯</span>}
          {Object.keys(node.capture || {}).length ? <span className="capture-badge">capture {Object.keys(node.capture || {}).slice(0, 2).join(', ')}</span> : null}
        </div>
        {errors ? <span className="flow-error-badge" title={data.issues.map((issue) => issue.message).join('\n')}>{errors}</span> : risks ? <span className="flow-risk-badge" title="存在需审阅风险">!</span> : null}
      </div>
      <Handle type="source" position={Position.Right} className="flow-handle" />
    </div>
  )
})

const nodeTypes = { atom: AtomNode }

function positionsKey(wid: string): string {
  return `panguverse.studio.positions.${wid}`
}

function loadPositions(wid: string): Record<string, { x: number; y: number }> {
  try {
    return JSON.parse(localStorage.getItem(positionsKey(wid)) || '{}')
  } catch {
    return {}
  }
}

function savePositions(wid: string, nodes: Node<AtomNodeData>[]): void {
  try {
    const map: Record<string, { x: number; y: number }> = {}
    nodes.forEach((node) => { map[node.id] = node.position })
    localStorage.setItem(positionsKey(wid), JSON.stringify(map))
  } catch {
    /* local-only convenience */
  }
}

const DEBUG_WIDTH_KEY = 'panguverse.studio.debugWidth'
const DEBUG_OPEN_KEY = 'panguverse.studio.debugOpen'

function loadDebugWidth(): number {
  try {
    const value = Number(localStorage.getItem(DEBUG_WIDTH_KEY))
    if (Number.isFinite(value) && value > 0) return Math.min(560, Math.max(240, Math.round(value)))
  } catch {
    /* local preference only */
  }
  return 320
}

function clampDebugWidth(value: number): number {
  return Math.min(560, Math.max(240, Math.round(value)))
}

export function StudioCanvasPage({ wid }: { wid: string }) {
  return (
    <ReactFlowProvider>
      <StudioCanvasInner wid={wid} />
    </ReactFlowProvider>
  )
}

type StudioTab = 'canvas' | 'triggers' | 'runs'

/** 模块级空值：useQuery 尚未返回时复用同一引用，避免 memo 依赖每渲染变化。 */
const EMPTY_ATOMS: Atom[] = []
const EMPTY_CONTRACTS: Record<string, ExploreAtomCreated> = {}
const EMPTY_ISSUES: ValidationIssue[] = []
const EMPTY_RISKS: Risk[] = []

function safeKey(value: unknown): string {
  try {
    return JSON.stringify(value)
  } catch {
    return ''
  }
}

/**
 * 节点结构指纹：等价判断只看会影响画布内容与交互的字段，
 * 相同指纹时不 setNodes，避免 effect → setState → 重建 callback 的循环。
 */
function flowNodeDigest(node: AtomFlowNode): string {
  const data = node.data
  const studio = data.studioNode
  return safeKey([
    node.id,
    node.position?.x,
    node.position?.y,
    Boolean(node.selected),
    studio?.atom,
    studio?.name,
    studio?.origin,
    Boolean(studio?.locked),
    studio?.with || null,
    studio?.capture || null,
    isNewAtomNode(studio),
    data.newAtomContract || null,
    data.issues.map((issue) => [issue.node_id, issue.code, issue.message, issue.severity]),
    data.risks.map((risk) => [risk.id, risk.kind, risk.message, risk.severity, risk.node_ids || []]),
    data.debugStatus || '',
    data.debugReason || '',
    Boolean(data.debugCurrent),
  ])
}

function sameFlowNodes(current: AtomFlowNode[], next: AtomFlowNode[]): boolean {
  if (current === next) return true
  if (current.length !== next.length) return false
  for (let index = 0; index < next.length; index += 1) {
    const left = current[index]
    const right = next[index]
    if (left.id !== right.id) return false
    if (flowNodeDigest(left) !== flowNodeDigest(right)) return false
  }
  return true
}

function edgeDigest(edge: Edge): string {
  const data = (edge.data || {}) as AnyRecord
  return safeKey([edge.id, edge.source, edge.target, edge.label, data.kind, data.via])
}

function sameEdges(current: Edge[], next: Edge[]): boolean {
  if (current === next) return true
  if (current.length !== next.length) return false
  for (let index = 0; index < next.length; index += 1) {
    if (edgeDigest(current[index]) !== edgeDigest(next[index])) return false
  }
  return true
}

function sameRecord<T>(left: Record<string, T>, right: Record<string, T>): boolean {
  if (left === right) return true
  const leftKeys = Object.keys(left)
  if (leftKeys.length !== Object.keys(right).length) return false
  return leftKeys.every((key) => key in right && left[key] === right[key])
}

function StudioCanvasInner({ wid }: { wid: string }) {
  const route = useRoute()
  const queryClient = useQueryClient()
  const { push } = useToast()
  const tabParam = route.query.get('tab')
  const tab: StudioTab = tabParam === 'triggers' || tabParam === 'runs' ? tabParam : 'canvas'
  const setTab = (next: StudioTab) => {
    const params = new URLSearchParams(route.query)
    if (next === 'canvas') params.delete('tab')
    else params.set('tab', next)
    const qs = params.toString()
    navigate(`/studio/${wid}${qs ? `?${qs}` : ''}`, { replace: true })
  }
  const [mock, setMock] = useState(isStudioMock())
  const [nodes, setNodes] = useState<AtomFlowNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [validation, setValidation] = useState<Validation>({ errors: [], warnings: [], risks: [] })
  const [addOpen, setAddOpen] = useState(false)
  const [addSearch, setAddSearch] = useState('')
  const [addAtom, setAddAtom] = useState<Atom | null>(null)
  const [addName, setAddName] = useState('')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [pendingConnection, setPendingConnection] = useState<{ source: string; target: string } | null>(null)
  const [edgeKind, setEdgeKind] = useState<'data' | 'order'>('data')
  const [edgeVia, setEdgeVia] = useState('')
  const [replanOpen, setReplanOpen] = useState(false)
  const [instruction, setInstruction] = useState('补充一条响应耗时断言，并保持锁定节点不变。')
  const [debugOpen, setDebugOpen] = useState<boolean>(() => {
    try {
      if (window.matchMedia('(max-width: 1099px)').matches) return false
      if (window.matchMedia('(max-height: 599px)').matches) return false
      return localStorage.getItem(DEBUG_OPEN_KEY) === '1'
    } catch {
      return false
    }
  })
  const [debugWidth, setDebugWidth] = useState<number>(loadDebugWidth)
  const [debugResizing, setDebugResizing] = useState(false)
  const [nodeRailCollapsed, setNodeRailCollapsed] = useState(false)
  const [debugMode, setDebugMode] = useState<'only' | 'from_start' | 'downstream'>('from_start')
  const [debugNodeId, setDebugNodeId] = useState('')
  const [debugSession, setDebugSession] = useState<DebugSession | null>(null)
  const [debugSessionId, setDebugSessionId] = useState('')
  const [debugPolling, setDebugPolling] = useState(false)
  const [debugError, setDebugError] = useState('')
  const [debugTotal, setDebugTotal] = useState(0)
  const [debugSyncing, setDebugSyncing] = useState(false)
  const [debugTab, setDebugTab] = useState<'timeline' | 'output' | 'vars'>('timeline')
  const [debugDetailOpen, setDebugDetailOpen] = useState(false)
  const [debugStep, setDebugStep] = useState<DebugNodeState | null>(null)
  const [followDebug, setFollowDebug] = useState(true)
  const cursorRef = useRef<number>(0)
  const flowRef = useRef<ReactFlowInstance<AtomFlowNode, Edge> | null>(null)
  const rowRefs = useRef<Map<string, HTMLButtonElement>>(new Map())
  const debugStatusRef = useRef<Map<string, DebugNodeState>>(new Map())
  const debugSessionRef = useRef<DebugSession | null>(null)
  const lastCenteredRef = useRef('')
  const debugQueueRef = useRef<Array<{ node: DebugNodeState; seq: number }>>([])
  const debugSeenSeqRef = useRef(0)
  const backendDoneRef = useRef(false)
  const finalSummaryRef = useRef<DebugSummary | null>(null)
  const drainTimerRef = useRef(0)
  const toastDoneRef = useRef(false)
  const drainRef = useRef<() => void>(() => {})
  const flowWrapRef = useRef<HTMLDivElement | null>(null)
  const debugTabPanelRef = useRef<HTMLDivElement | null>(null)
  const debugListScrollRef = useRef(0)
  const pendingScrollRestoreRef = useRef<number | null>(null)
  const debugWidthRef = useRef(debugWidth)
  debugWidthRef.current = debugWidth
  const storedProvenance = useMemo(() => readStudioProvenance(wid), [wid])
  const [panelOpen, setPanelOpen] = useState(false)
  const [paramName, setParamName] = useState('')
  const [paramWith, setParamWith] = useState<AnyRecord>({})
  const [paramAtom, setParamAtom] = useState('')
  const [paramJson, setParamJson] = useState<Record<string, string>>({})
  const [paramError, setParamError] = useState('')
  const [newAtomCache, setNewAtomCache] = useState<Record<string, ExploreAtomCreated>>(() => readNewAtomCache(wid))

  const workflowQuery = useQuery({ queryKey: ['studio-workflow', wid], queryFn: () => studioApi.getWorkflow(wid), enabled: !!wid })
  const atomsQuery = useQuery({ queryKey: ['atoms'], queryFn: api.atoms, staleTime: 60_000 })
  const workflow = workflowQuery.data
  const atoms = atomsQuery.data || EMPTY_ATOMS
  const selectedNode = workflow?.graph?.nodes.find((node) => node.node_id === selectedId) || null
  const selectedAtom = atoms.find((atom) => atom.name === (selectedNode?.atom || paramAtom)) || null
  /** 契约摘要不变时复用同一个对象，保证 buildFlowNodes 的依赖真正收敛。 */
  const contractsRef = useRef<{ key: string; map: Record<string, ExploreAtomCreated> }>({ key: '', map: EMPTY_CONTRACTS })
  const newAtomContracts = useMemo(() => {
    const map: Record<string, ExploreAtomCreated> = { ...newAtomCache }
    ;(workflow?.graph?.nodes || []).forEach((node) => {
      if (!isNewAtomNode(node)) return
      const spec = atoms.find((atom) => atom.name === node.atom)
      const cached = map[node.atom] || {}
      const extra = (node.extra && typeof node.extra === 'object' ? node.extra : {}) as AnyRecord
      map[node.atom] = {
        ...(spec ? {
          description: spec.description,
          inputs: spec.inputs,
          outputs: spec.outputs,
          effects: spec.effects,
          pack: spec.pack,
        } : {}),
        ...cached,
        name: node.atom,
        pack: cached.pack || String(extra.atom_pack || '') || spec?.pack,
        source_script: cached.source_script || String(extra.source_script || ''),
      }
    })
    const key = safeKey(map)
    if (key && contractsRef.current.key === key) return contractsRef.current.map
    contractsRef.current = { key, map }
    return map
  }, [atoms, newAtomCache, workflow])
  const selectedNewAtom = isNewAtomNode(selectedNode)
  const selectedContract = selectedNewAtom ? newAtomContracts[selectedNode?.atom || ''] || null : null

  useEffect(() => {
    const next = readNewAtomCache(wid)
    setNewAtomCache((current) => sameRecord(current, next) ? current : next)
  }, [wid])

  useEffect(() => onStudioMockChange(setMock), [])

  useEffect(() => {
    try {
      localStorage.setItem(DEBUG_OPEN_KEY, debugOpen ? '1' : '0')
    } catch {
      /* local preference only */
    }
  }, [debugOpen])

  useEffect(() => {
    const narrow = window.matchMedia('(max-width: 1099px)')
    const short = window.matchMedia('(max-height: 599px)')
    const apply = () => { if (narrow.matches || short.matches) setDebugOpen(false) }
    apply()
    narrow.addEventListener('change', apply)
    short.addEventListener('change', apply)
    return () => {
      narrow.removeEventListener('change', apply)
      short.removeEventListener('change', apply)
    }
  }, [])

  useEffect(() => {
    if (!selectedNode) {
      setParamName('')
      setParamWith({})
      setParamAtom('')
      setParamJson({})
      return
    }
    setParamName(selectedNode.name || '')
    setParamWith(JSON.parse(JSON.stringify(selectedNode.with || {})))
    setParamAtom(selectedNode.atom)
    const drafts: Record<string, string> = {}
    Object.entries(selectedNode.with || {}).forEach(([key, value]) => {
      if (value && typeof value === 'object') drafts[key] = JSON.stringify(value, null, 2)
    })
    setParamJson(drafts)
    setParamError('')
  }, [selectedNode?.node_id, selectedNode?.with])

  const buildFlowNodes = useCallback((source: Workflow, issues: ValidationIssue[], risks: Risk[]): AtomFlowNode[] => {
    const saved = loadPositions(wid)
    const graph = source.graph || { nodes: [], edges: [] }
    return (graph.nodes || []).map((node, index) => {
      const pos = saved[node.node_id] || node.ui || {
        x: 40 + (index % 5) * 240,
        y: 56 + Math.floor(index / 5) * 170,
      }
      const nodeIssues = issues.filter((issue) => issue.node_id === node.node_id)
      const nodeRisks = risks.filter((risk) => (risk.node_ids || []).includes(node.node_id))
      const debugState = debugStatusRef.current.get(node.node_id)
      return {
        id: node.node_id,
        type: 'atom',
        position: { x: Number(pos.x) || 40 + (index % 5) * 240, y: Number(pos.y) || 56 + Math.floor(index / 5) * 170 },
        data: {
          studioNode: node,
          issues: nodeIssues,
          risks: nodeRisks,
          newAtomContract: newAtomContracts[node.atom] || null,
          debugStatus: debugState?.status || '',
          debugReason: debugState?.reason || '',
          debugCurrent: debugState?.status === 'running',
        },
        selected: node.node_id === selectedId,
      }
    })
  }, [selectedId, wid, newAtomContracts])

  const buildFlowEdges = useCallback((source: Workflow): Edge[] => {
    return ((source.graph?.edges || []) as StudioEdge[]).map((edge, index) => ({
      id: edge.edge_id || `${edge.from}-${edge.to}-${index}`,
      source: edge.from,
      target: edge.to,
      label: edge.kind === 'data' ? (edge.via || 'data') : 'order-only',
      animated: false,
      markerEnd: { type: MarkerType.ArrowClosed, width: 20, height: 20, color: edge.kind === 'data' ? '#5eead4' : '#718096' },
      style: {
        stroke: edge.kind === 'data' ? '#5eead4' : '#718096',
        strokeWidth: edge.kind === 'data' ? 2.2 : 1.8,
        strokeDasharray: edge.kind === 'data' ? undefined : '6 6',
        vectorEffect: 'non-scaling-stroke',
      },
      labelStyle: { fill: edge.kind === 'data' ? '#9bf5e8' : '#9aa8bf', fontSize: 10, fontFamily: 'monospace' },
      labelBgStyle: { fill: '#0b1220', fillOpacity: 0.94 },
      labelBgPadding: [5, 3] as [number, number],
      labelBgBorderRadius: 4,
      data: { kind: edge.kind, via: edge.via },
    }))
  }, [])

  useEffect(() => {
    if (!workflow) return
    const currentValidation = validation
    const nextNodes = buildFlowNodes(workflow, currentValidation.errors || EMPTY_ISSUES, currentValidation.risks || EMPTY_RISKS)
    setNodes((current) => (sameFlowNodes(current, nextNodes) ? current : nextNodes))
    const nextEdges = buildFlowEdges(workflow)
    setEdges((current) => (sameEdges(current, nextEdges) ? current : nextEdges))
  }, [workflow, validation, buildFlowNodes, buildFlowEdges])

  useEffect(() => {
    if (selectedId && workflow?.graph?.nodes?.some((node) => node.node_id === selectedId)) {
      setDebugNodeId(selectedId)
    }
  }, [selectedId, workflow?.wid])

  useEffect(() => {
    if (!workflow) return
    if (workflow.validation) setValidation(normalizeLocal(workflow.validation))
    studioApi.validate(wid).then((result) => setValidation(result)).catch(() => undefined)
    if (!debugNodeId) setDebugNodeId(workflow.graph?.nodes?.[0]?.node_id || '')
  }, [wid, workflow?.current_rev])

  const refresh = (next: Workflow) => {
    queryClient.setQueryData(['studio-workflow', wid], next)
    queryClient.invalidateQueries({ queryKey: ['studio-workflow', wid] })
    studioApi.validate(wid).then(setValidation).catch(() => undefined)
  }

  const updateNode = useMutation({
    mutationFn: () => {
      if (!selectedNode) throw new Error('没有选中节点')
      let withValue = { ...paramWith }
      for (const [key, raw] of Object.entries(paramJson)) {
        try {
          withValue[key] = JSON.parse(raw)
        } catch {
          throw new Error(`参数 ${key} 不是合法 JSON`)
        }
      }
      return studioApi.updateNode(wid, selectedNode.node_id, { name: paramName, atom: paramAtom, with: withValue })
    },
    onSuccess: (next) => { refresh(next); push('节点参数已保存，生成新 rev', 'ok'); setParamError('') },
    onError: (error: Error) => { setParamError(error.message); push(error.message, 'error') },
  })

  const toggleLock = useMutation({
    mutationFn: () => selectedNode ? studioApi.updateNode(wid, selectedNode.node_id, { locked: !selectedNode.locked }) : Promise.reject(new Error('没有选中节点')),
    onSuccess: (next) => { refresh(next); push(selectedNode?.locked ? '节点已解锁' : '节点已锁定，AI 不可改写', 'ok') },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const addNode = useMutation({
    mutationFn: () => {
      if (!addAtom) throw new Error('请选择原子')
      return studioApi.addNode(wid, { atom: addAtom.name, name: addName.trim() || addAtom.name, with: {} })
    },
    onSuccess: (next) => {
      refresh(next)
      const created = next.graph?.nodes?.slice(-1)[0]
      if (created) { setSelectedId(created.node_id); setPanelOpen(true) }
      setAddOpen(false)
      setAddAtom(null)
      setAddName('')
      push('节点已新增', 'ok')
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const removeNode = useMutation({
    mutationFn: () => selectedNode ? studioApi.deleteNode(wid, selectedNode.node_id) : Promise.reject(new Error('没有选中节点')),
    onSuccess: (next) => { refresh(next); setSelectedId(''); setDeleteOpen(false); push('节点与相关边已删除', 'ok') },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const addEdgeMutation = useMutation({
    mutationFn: () => {
      if (!pendingConnection) throw new Error('连线信息缺失')
      return studioApi.addEdge(wid, { from: pendingConnection.source, to: pendingConnection.target, kind: edgeKind, via: edgeKind === 'data' ? edgeVia.trim() : 'order-only' })
    },
    onSuccess: (next) => { refresh(next); setPendingConnection(null); push('连线已写入新 rev', 'ok') },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const replan = useMutation({
    mutationFn: () => studioApi.replan(wid, instruction.trim()),
    onSuccess: (result) => {
      setReplanOpen(false)
      if (!result.proposed_rev) { push('AI 没有产生新的差异，未生成提案', 'info'); return }
      push(`AI 提议已生成 rev ${result.proposed_rev}，尚未应用`, 'ok')
      sessionStorage.setItem(`panguverse.proposal.${wid}`, JSON.stringify(result))
      navigate(`/studio/${wid}/review?base=${workflow?.current_rev || 0}&rev=${result.proposed_rev}&proposed=1`)
    },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const submitReview = useMutation({
    mutationFn: () => studioApi.setState(wid, 'review', '从画布提交审阅'),
    onSuccess: (next) => { refresh(next); navigate(`/studio/${wid}/review?base=${Math.max(0, (next.current_rev || 0) - 1)}&rev=${next.current_rev || 0}`) },
    onError: (error: Error) => push(error.message, 'error'),
  })

  const startDebug = useMutation({
    mutationFn: () => {
      const target = debugNodeId || selectedId || workflow?.graph?.nodes?.[0]?.node_id || ''
      if (!target) throw new Error('先在画布上选一个节点')
      return studioApi.startDebug(wid, { rev: workflow?.current_rev ?? 0, node_id: target, mode: debugMode })
    },
    onSuccess: (result) => {
      setDebugError('')
      setDebugStep(null)
      setDebugTab('timeline')
      setDebugDetailOpen(false)
      const allNodes = workflow?.graph?.nodes || []
      const targetIndex = Math.max(0, allNodes.findIndex((node) => node.node_id === (debugNodeId || selectedId)))
      const previewNodes = debugMode === 'only'
        ? allNodes.slice(targetIndex, targetIndex + 1)
        : debugMode === 'from_start'
          ? allNodes.slice(0, targetIndex + 1)
          : allNodes.slice(targetIndex)
      const previewTotal = Number(result.total || previewNodes.length || 0)
      const previewSession = result.legacy || {
        session_id: result.session_id || undefined,
        nodes: previewNodes.map((node) => ({
          node_id: node.node_id,
          node: node.name,
          atom: node.atom,
          status: 'pending' as const,
          reason: null,
        })),
        cursor: 0,
        done: false,
        summary: { passed: 0, failed: 0, skipped: 0, total: previewTotal, by_reason: {} },
      }
      setDebugSession(previewSession)
      debugSessionRef.current = previewSession
      cursorRef.current = 0
      lastCenteredRef.current = ''
      debugQueueRef.current = []
      debugSeenSeqRef.current = 0
      backendDoneRef.current = false
      finalSummaryRef.current = null
      toastDoneRef.current = false
      setDebugSyncing(false)
      window.clearTimeout(drainTimerRef.current)
      drainTimerRef.current = 0
      setDebugTotal(previewTotal)
      if (result.session_id) {
        setDebugSessionId(result.session_id)
        push('调试会话已创建，正在轮询节点状态…', 'info')
      } else if (result.legacy) {
        setDebugStep(result.legacy.nodes[0] || null)
        push('后端仍返回同步调试结果（v6.0 会话接口未联调）', 'warn')
      } else {
        setDebugSessionId('')
        push('调试请求未返回 session_id', 'error')
      }
    },
    onError: (error: Error) => {
      setDebugError(error.message)
      push(error.message, 'error')
    },
  })

  const onNodesChange = useCallback((changes: NodeChange<AtomFlowNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current))
  }, [])

  const onEdgesChange = useCallback((changes: EdgeChange<Edge>[]) => {
    setEdges((current) => applyEdgeChanges(changes, current))
  }, [])

  const onConnect = useCallback((connection: Connection) => {
    if (!connection.source || !connection.target) return
    setPendingConnection({ source: connection.source, target: connection.target })
    const sourceNode = workflow?.graph?.nodes.find((node) => node.node_id === connection.source)
    const captureKey = Object.keys(sourceNode?.capture || {})[0] || connection.source
    setEdgeVia(captureKey)
    setEdgeKind('data')
  }, [workflow])

  const centerNode = useCallback((nodeId: string) => {
    const instance = flowRef.current
    const flowNode = nodes.find((node) => node.id === nodeId)
    if (!instance || !flowNode) return
    const width = flowNode.measured?.width || 214
    const height = flowNode.measured?.height || 96
    instance.setCenter(flowNode.position.x + width / 2, flowNode.position.y + height / 2, {
      zoom: Math.max(instance.getZoom(), 1),
      duration: 420,
    })
  }, [nodes])

  const locateNode = useCallback((nodeId?: string) => {
    if (!nodeId) return
    setNodeRailCollapsed(false)
    setSelectedId(nodeId)
    setPanelOpen(true)
    centerNode(nodeId)
  }, [centerNode])

  const persistDebugWidth = useCallback((value: number) => {
    try {
      localStorage.setItem(DEBUG_WIDTH_KEY, String(clampDebugWidth(value)))
    } catch {
      /* local preference only */
    }
  }, [])

  const nudgeDebugWidth = useCallback((delta: number) => {
    const next = clampDebugWidth(debugWidthRef.current + delta)
    setDebugWidth(next)
    persistDebugWidth(next)
  }, [persistDebugWidth])

  const startDebugResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    let lastX = event.clientX
    let latestWidth = debugWidthRef.current
    setDebugResizing(true)
    const onMove = (moveEvent: PointerEvent) => {
      latestWidth = clampDebugWidth(latestWidth + (moveEvent.clientX - lastX))
      lastX = moveEvent.clientX
      debugWidthRef.current = latestWidth
      setDebugWidth(latestWidth)
    }
    const onUp = () => {
      setDebugResizing(false)
      debugWidthRef.current = latestWidth
      persistDebugWidth(latestWidth)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }, [persistDebugWidth])

  useEffect(() => {
    const element = flowWrapRef.current
    if (!element) return
    let timer = 0
    const observer = new ResizeObserver(() => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        flowRef.current?.fitView({ padding: 0.14, duration: 220 })
      }, 160)
    })
    observer.observe(element)
    return () => {
      observer.disconnect()
      window.clearTimeout(timer)
    }
  }, [workflow?.wid])

  useEffect(() => {
    const target = route.query.get('node')
    if (!target || !workflow?.graph?.nodes?.some((node) => node.node_id === target)) return
    setNodeRailCollapsed(false)
    setSelectedId(target)
    setPanelOpen(true)
  }, [route.path, route.query, workflow?.wid])

  const debugNodes = useMemo(() => debugSession?.nodes || [], [debugSession])
  const debugStatusMap = useMemo(
    () => new Map(debugNodes.map((node) => [node.node_id, node])),
    [debugNodes],
  )
  debugStatusRef.current = debugStatusMap

  const upstreamBlockedFor = useCallback((nodeId: string): string[] => {
    const edges = workflow?.graph?.edges || []
    const blocked: string[] = []
    const visited = new Set<string>()
    const stack = edges.filter((edge) => edge.to === nodeId).map((edge) => edge.from)
    while (stack.length) {
      const id = stack.pop()!
      if (!id || visited.has(id)) continue
      visited.add(id)
      const state = debugStatusMap.get(id)
      if (state && (state.status === 'skipped' || state.status === 'failed')) blocked.push(id)
      edges.filter((edge) => edge.to === id).forEach((edge) => stack.push(edge.from))
    }
    return blocked
  }, [debugStatusMap, workflow?.graph?.edges])

  const openDebugDetail = useCallback((step: DebugNodeState) => {
    pendingScrollRestoreRef.current = null
    debugListScrollRef.current = debugTabPanelRef.current?.scrollTop || 0
    setDebugStep(step)
    setDebugDetailOpen(true)
    setFollowDebug(false)
  }, [])

  const closeDebugDetail = useCallback(() => {
    pendingScrollRestoreRef.current = debugListScrollRef.current
    setDebugDetailOpen(false)
  }, [])

  useLayoutEffect(() => {
    if (debugDetailOpen) return
    const target = pendingScrollRestoreRef.current
    if (target === null) return
    const el = debugTabPanelRef.current
    if (!el) return
    let cancelled = false
    let tries = 0
    const settle = () => {
      if (cancelled || !el.isConnected) return
      if (el.scrollHeight <= el.clientHeight && tries < 8) {
        tries += 1
        window.requestAnimationFrame(settle)
        return
      }
      el.scrollTop = target
      window.requestAnimationFrame(() => {
        if (cancelled) return
        el.scrollTop = target
        window.requestAnimationFrame(() => {
          if (cancelled) return
          el.scrollTop = target
          window.setTimeout(() => {
            if (cancelled) return
            el.scrollTop = target
            pendingScrollRestoreRef.current = null
          }, 0)
        })
      })
    }
    window.requestAnimationFrame(() => window.requestAnimationFrame(settle))
    return () => { cancelled = true }
  }, [debugDetailOpen])

  useEffect(() => {
    setNodes((current) => {
      let changed = false
      const next = current.map((node) => {
        const state = debugStatusMap.get(node.id)
        const status = state?.status || ''
        const reason = state?.reason || ''
        const isCurrent = status === 'running'
        if ((node.data.debugStatus || '') === status
          && (node.data.debugReason || '') === reason
          && Boolean(node.data.debugCurrent) === isCurrent) return node
        changed = true
        return { ...node, data: { ...node.data, debugStatus: status, debugReason: reason, debugCurrent: isCurrent } }
      })
      return changed ? next : current
    })
  }, [debugStatusMap])

  const drainDebugQueue = useCallback(() => {
    drainTimerRef.current = 0
    const item = debugQueueRef.current.shift()
    if (!item) {
      setDebugSyncing(false)
      const previous = debugSessionRef.current
      if (backendDoneRef.current && previous) {
        const summary = summarizeNodes(previous.nodes, debugTotal, finalSummaryRef.current || previous.summary)
        const doneSession = { ...previous, done: true, summary }
        debugSessionRef.current = doneSession
        setDebugSession(doneSession)
        setDebugPolling(false)
        if (!toastDoneRef.current) {
          toastDoneRef.current = true
          push(`调试完成：通过 ${summary.passed} / 失败 ${summary.failed} / 跳过 ${summary.skipped}`, summary.failed ? 'error' : 'ok')
        }
      }
      return
    }
    setDebugSession((previous) => {
      if (!previous) return previous
      const nodeMap = new Map(previous.nodes.map((node) => [node.node_id, node]))
      nodeMap.set(item.node.node_id, item.node)
      const nodes = Array.from(nodeMap.values())
      const nextSession = { ...previous, nodes, summary: summarizeNodes(nodes, debugTotal, previous.summary), done: false }
      debugSessionRef.current = nextSession
      return nextSession
    })
    drainTimerRef.current = window.setTimeout(() => drainRef.current(), 220)
  }, [debugTotal, push])
  drainRef.current = drainDebugQueue

  useEffect(() => {
    if (!debugSessionId) return
    let cancelled = false
    let timer = 0
    const tick = async () => {
      try {
        const next = await studioApi.debugSession(debugSessionId, cursorRef.current)
        if (cancelled) return
        cursorRef.current = Number(next.cursor ?? cursorRef.current)
        const rawEvents = Array.isArray((next as AnyRecord).events) ? (next as AnyRecord).events as AnyRecord[] : []
        rawEvents.forEach((event) => {
          if (event.type && event.type !== 'node') return
          const status = String(event.status || '')
          if (!['passed', 'failed', 'skipped'].includes(status)) return
          const seq = Number(event.seq || 0)
          if (seq && seq <= debugSeenSeqRef.current) return
          if (seq) debugSeenSeqRef.current = seq
          const nodeId = String(event.node_id || '')
          if (!nodeId) return
          if (debugQueueRef.current.some((item) => item.node.node_id === nodeId && item.node.status === status)) return
          debugQueueRef.current.push({
            node: { ...event, node_id: nodeId, status, reason: event.reason || null } as DebugNodeState,
            seq,
          })
        })
        backendDoneRef.current = next.done
        if (next.done) finalSummaryRef.current = next.summary
        const previous = debugSessionRef.current
        const nodeMap = new Map(previous?.nodes.map((node) => [node.node_id, node]) || [])
        const hasQueue = debugQueueRef.current.length > 0
        next.nodes.forEach((node) => {
          if (hasQueue && ['passed', 'failed', 'skipped'].includes(node.status)) return
          nodeMap.set(node.node_id, node)
        })
        const nodes = Array.from(nodeMap.values())
        const hasSummary = next.nodes.length > 0 || next.summary.passed + next.summary.failed + next.summary.skipped > 0
        const merged: DebugSession = {
          ...next,
          nodes,
          variables: { ...(previous?.variables || {}), ...(next.variables || {}) },
          summary: summarizeNodes(nodes, debugTotal, hasSummary ? next.summary : previous?.summary),
          done: next.done && !hasQueue,
        }
        debugSessionRef.current = merged
        setDebugSession(merged)
        setDebugError(next.error ? String(next.error) : '')
        setDebugPolling(!next.done)
        if (hasQueue) {
          setDebugSyncing(true)
          if (!drainTimerRef.current) drainTimerRef.current = window.setTimeout(() => drainRef.current(), 220)
        }
        if (!next.done) {
          timer = window.setTimeout(tick, 150)
        } else if (!hasQueue && !toastDoneRef.current) {
          toastDoneRef.current = true
          setDebugSyncing(false)
          push(`调试完成：通过 ${merged.summary.passed} / 失败 ${merged.summary.failed} / 跳过 ${merged.summary.skipped}`, merged.summary.failed ? 'error' : 'ok')
        }
      } catch (error) {
        if (cancelled) return
        setDebugPolling(false)
        setDebugError((error as Error).message)
      }
    }
    setDebugPolling(true)
    timer = window.setTimeout(tick, 120)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      window.clearTimeout(drainTimerRef.current)
      drainTimerRef.current = 0
      setDebugPolling(false)
      setDebugSyncing(false)
    }
  }, [debugSessionId, debugTotal, push])

  const runningDebugId = useMemo(
    () => debugNodes.find((node) => node.status === 'running')?.node_id || '',
    [debugNodes],
  )
  const lastFinishedDebugId = useMemo(
    () => [...debugNodes].reverse().find((node) => ['passed', 'failed', 'skipped'].includes(node.status))?.node_id || '',
    [debugNodes],
  )
  const currentDebugId = runningDebugId || lastFinishedDebugId

  useEffect(() => {
    if (!followDebug || !currentDebugId) return
    const state = debugStatusMap.get(currentDebugId)
    if (!state) return
    setDebugStep(state)
    rowRefs.current.get(currentDebugId)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    if (lastCenteredRef.current !== currentDebugId) {
      lastCenteredRef.current = currentDebugId
      centerNode(currentDebugId)
    }
  }, [followDebug, currentDebugId, debugStatusMap, centerNode])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable) return
      if ((event.key === 'Delete' || event.key === 'Backspace') && selectedId && !deleteOpen) {
        event.preventDefault()
        setDeleteOpen(true)
      }
      if (event.key.toLowerCase() === 'n' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        setAddOpen(true)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [selectedId, deleteOpen])

  const errors = validation.errors || []
  const warnings = validation.warnings || []
  const risks = validation.risks || []
  const issuesByNode = useMemo(() => {
    const map = new Map<string, ValidationIssue[]>()
    errors.forEach((issue) => {
      if (!issue.node_id) return
      map.set(issue.node_id, [...(map.get(issue.node_id) || []), issue])
    })
    return map
  }, [errors])
  const state = workflow?.state || 'draft'
  const filteredAtoms = useMemo(() => {
    const query = addSearch.trim().toLowerCase()
    return atoms.filter((atom) => !query || `${atom.name} ${atom.description || ''} ${atom.category}`.toLowerCase().includes(query)).slice(0, 40)
  }, [addSearch, atoms])

  if (workflowQuery.isLoading) return <div className="page"><Loading label="正在读取任务…" /></div>
  if (workflowQuery.error || !workflow) return <div className="page"><ErrorState error={workflowQuery.error} onRetry={() => workflowQuery.refetch()} title="任务读取失败" /></div>
  const currentRev = workflow.current_rev ?? 0
  const debugSummary = debugSession?.summary || { passed: 0, failed: 0, skipped: 0, total: workflow.graph?.nodes?.length || 0 }
  const totalCount = debugTotal || Number(debugSummary.total ?? 0) || workflow.graph?.nodes?.length || 0
  const finishedCount = debugSummary.passed + debugSummary.failed + debugSummary.skipped
  const progressPct = totalCount ? Math.min(100, Math.round((finishedCount / totalCount) * 100)) : 0
  const runningDebug = debugNodes.find((node) => node.status === 'running') || null
  const skipGroups: Array<{ reason: string; ids: string[]; count: number }> = Object.entries(debugSummary.by_reason || {})
    .filter(([, count]) => Number(count) > 0)
    .map(([reason, count]) => ({
      reason,
      count: Number(count),
      ids: debugNodes.filter((node) => node.status === 'skipped' && (node.reason || 'unknown') === reason).map((node) => node.node_id),
    }))
  const nodeLabel = (nodeId: string) =>
    workflow.graph?.nodes.find((node) => node.node_id === nodeId)?.name
    || debugStatusMap.get(nodeId)?.node
    || nodeId
  const plannerRaw = String(workflow.planner || workflow.plan_source || storedProvenance?.planner || storedProvenance?.plan_source || '')
  const planMode = plannerRaw === 'llm' ? 'ai' : plannerRaw === 'rules' || plannerRaw === 'template' ? 'rules' : 'unknown'
  const confidenceValue = Number(workflow.confidence ?? storedProvenance?.confidence)
  const confidence = Number.isFinite(confidenceValue) ? confidenceValue : null
  const assumptions = (workflow.assumptions || storedProvenance?.assumptions || []).map(String)
  const capabilityGaps = gapItems(workflow.capability_gaps || storedProvenance?.capability_gaps || validation.capability_gaps || [])
  type ParamAuditEntry = { node_id?: string; atom?: string; param?: string; value?: string; source?: string; reason?: string }
  const paramAudit = (workflow.param_audit || {}) as { autofilled?: ParamAuditEntry[]; param_gaps?: ParamAuditEntry[] }
  const autofilled = Array.isArray(paramAudit.autofilled) ? paramAudit.autofilled : []
  const paramGaps = Array.isArray(paramAudit.param_gaps) ? paramAudit.param_gaps : []
  const coverage = validation.coverage || null
  const isExplore = workflow.origin === 'explore' || Boolean(workflow.explore_id)
  const coverageGateValue = coverage ? coverageGate(coverage, isExplore) : null
  const blockingErrorList = blockingValidationErrors(errors, coverage, isExplore)
  const blockingErrors = blockingErrorList.length > 0
  const missingEnv = validation.missing_env || []
  const selectedUpstreamBlocked = debugStep && debugStep.status === 'failed' ? upstreamBlockedFor(debugStep.node_id) : []

  return (
    <div className="studio-page studio-canvas-page">
      <div className="studio-top">
        <div className="studio-top-left">
          <a className="icon-btn" href={href('/studio/new')} aria-label="返回工作台"><Icon name="arrow" size={18} style={{ transform: 'rotate(180deg)' }} /></a>
          <div className="studio-title">
            <div className="studio-title-line">
              <h1>{workflow.name || '未命名任务'}</h1>
              <StatusBadge status={state} />
              {mock ? <Badge tone="warn">演示</Badge> : null}
              {workflow.origin === 'explore' ? (
                <a
                  className="badge badge-violet explore-origin-chip"
                  href={href(`/studio/explore/${workflow.explore_id || ''}`)}
                  title="该任务由探索成功后固化，参数来自真实调用值"
                  data-testid="canvas-explore-origin"
                >
                  <Icon name="bolt" size={11} />来自探索 {workflow.explore_id || 'eid 未知'}
                </a>
              ) : null}
              {Array.isArray(workflow.pending_atoms) && workflow.pending_atoms.length ? (
                <Badge tone="warn" className="pending-atoms-chip" data-testid="canvas-pending-atoms">
                  待封装脚本 {workflow.pending_atoms.length}
                </Badge>
              ) : null}
              {workflow.replay_status === 'replay_failed' ? (
                <Badge tone="bad" data-testid="canvas-replay-failed">重放未通过</Badge>
              ) : null}
              <span className="rev-chip mono">rev {currentRev}</span>
            </div>
            <p>{workflow.intent || '未记录意图'} <span>·</span> {workflow.domain_pack || '未标注领域包'} <span>·</span> {workflow.wid}</p>
          </div>
        </div>
        <div className="studio-top-actions">
          {coverage ? (
            <span
              className={`validation-pill ${coverageTone(coverage, isExplore)}`}
              title={coverageGateValue === 'advisory'
                ? `意图覆盖率 ${coverage.matched}/${coverage.total}；${COVERAGE_ADVISORY_NOTE}`
                : `意图覆盖率 ${coverage.matched}/${coverage.total}`}
              data-coverage-gate={coverageGateValue || undefined}
              data-testid="canvas-coverage-pill"
            >
              <Icon name="atoms" size={14} />
              覆盖 {coverage.matched}/{coverage.total}{coverageGateValue === 'advisory' ? ' · 参考' : ''}
            </span>
          ) : null}
          <span className={`validation-pill ${blockingErrors ? 'bad' : warnings.length ? 'warn' : 'ok'}`} title={blockingErrors ? '存在校验错误，不能提交审阅' : '校验通过'}>
            <Icon name={blockingErrors ? 'warning' : 'check'} size={14} />
            {errors.length} 错误 · {warnings.length} 警告
          </span>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => studioApi.validate(wid).then(setValidation).catch((error) => push(error.message, 'error'))}>校验</Button>
          <Button size="sm" variant="ghost" icon="spark" onClick={() => setReplanOpen(true)}>AI 重规划</Button>
          <Button size="sm" variant="ghost" icon="settings" className="mobile-only" onClick={() => { setNodeRailCollapsed(false); setPanelOpen(true) }}>节点设置</Button>
          <Button
            size="sm"
            variant="primary"
            icon="git"
            loading={submitReview.isPending}
            disabled={blockingErrors}
            title={blockingErrors ? `先修复 ${blockingErrorList.length} 个校验错误` : '提交审阅'}
            onClick={() => state === 'draft' ? submitReview.mutate() : navigate(`/studio/${wid}/review?base=${Math.max(0, currentRev - 1)}&rev=${currentRev}`)}
          >
            {state === 'draft' ? '提交审阅' : '打开审阅'}
          </Button>
        </div>
      </div>

      <nav className="studio-tabs" aria-label="任务视图" data-testid="studio-tabs">
        <button type="button" className={tab === 'canvas' ? 'is-active' : ''} aria-current={tab === 'canvas' ? 'page' : undefined} onClick={() => setTab('canvas')}>
          <Icon name="flow" size={14} />Canvas
        </button>
        <button type="button" className={tab === 'triggers' ? 'is-active' : ''} aria-current={tab === 'triggers' ? 'page' : undefined} onClick={() => setTab('triggers')}>
          <Icon name="clock" size={14} />Triggers
        </button>
        <button type="button" className={tab === 'runs' ? 'is-active' : ''} aria-current={tab === 'runs' ? 'page' : undefined} onClick={() => setTab('runs')}>
          <Icon name="history" size={14} />Runs
        </button>
        <span className="studio-tabs-hint">
          {workflow.published_ref ? <><Icon name="git" size={12} /><code>{workflow.published_ref}</code></> : '尚未发布'}
        </span>
      </nav>

      {tab === 'canvas' ? (
      <div
        className={`studio-workbench ${debugOpen ? 'is-debug-open' : 'is-debug-closed'} ${debugResizing ? 'is-resizing' : ''} ${nodeRailCollapsed ? 'is-node-collapsed' : ''}`}
        style={{ '--debug-w': debugOpen ? `${debugWidth}px` : '44px', '--props-w': nodeRailCollapsed ? '38px' : '300px' } as CSSProperties}
        data-debug-width={debugWidth}
        data-debug-open={debugOpen ? 'true' : 'false'}
      >
        {debugOpen ? (
          <>
            <section
              className="debug-column"
              role="region"
              aria-label="调试面板"
              data-testid="debug-column"
              data-width={debugWidth}
            >
              <div
                className="debug-resizer"
                role="separator"
                aria-orientation="vertical"
                aria-label="拖拽调整调试面板宽度"
                aria-valuemin={240}
                aria-valuemax={560}
                aria-valuenow={debugWidth}
                aria-valuetext={`${debugWidth}px`}
                title="拖拽调整宽度，双击复位"
                tabIndex={0}
                onPointerDown={startDebugResize}
                onKeyDown={(event) => {
                  if (event.key === 'ArrowLeft') { event.preventDefault(); nudgeDebugWidth(-16) }
                  if (event.key === 'ArrowRight') { event.preventDefault(); nudgeDebugWidth(16) }
                }}
                onDoubleClick={() => { setDebugWidth(320); persistDebugWidth(320) }}
              />
            <div className="debug-panel" role="region" aria-label="调试面板">
              <div className="debug-head">
                <div className="debug-title">
                  <span className="debug-label"><span className={`debug-pulse ${debugPolling ? '' : 'is-idle'}`} /> 调试执行</span>
                  <Badge tone="warn">不影响历史</Badge>
                  {debugSessionId ? <Badge tone={debugPolling ? 'warn' : 'ok'}>{debugPolling ? '轮询中' : debugSession?.done ? '已完成' : '等待'}</Badge> : null}
                  {debugSession?.legacy ? <Badge tone="warn">同步旧接口</Badge> : null}
                  <span className="debug-sub">POST 202 · GET cursor 增量</span>
                </div>
                <div className="debug-head-actions">
                  <button
                    type="button"
                    className={`follow-toggle ${followDebug ? 'is-on' : ''}`}
                    data-testid="debug-follow-toggle"
                    data-following={followDebug ? 'true' : 'false'}
                    onClick={() => setFollowDebug((value) => !value)}
                    title={followDebug ? '暂停自动跟随当前节点' : '恢复自动跟随当前节点'}
                  >
                    <Icon name={followDebug ? 'check' : 'play'} size={11} />
                    {followDebug ? '自动跟随' : '已暂停跟随'}
                  </button>
                  <button
                    type="button"
                    className="debug-collapse-btn"
                    data-testid="debug-collapse"
                    aria-label="收起调试"
                    title="收起调试"
                    onClick={() => setDebugOpen(false)}
                  >
                    <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
                    <span>收起调试</span>
                  </button>
                </div>
              </div>
              <div className="debug-controls">
                <div className="segmented debug-modes">
                  <button type="button" className={debugMode === 'only' ? 'is-active' : ''} onClick={() => setDebugMode('only')}>只跑此节点</button>
                  <button type="button" className={debugMode === 'from_start' ? 'is-active' : ''} onClick={() => setDebugMode('from_start')}>从头到此</button>
                  <button type="button" className={debugMode === 'downstream' ? 'is-active' : ''} onClick={() => setDebugMode('downstream')}>从此下游</button>
                </div>
                <Select value={debugNodeId} onChange={(event) => setDebugNodeId(event.target.value)} aria-label="调试目标节点">
                  <option value="">选择节点</option>
                  {(workflow.graph?.nodes || []).map((node) => <option key={node.node_id} value={node.node_id}>{node.name || node.atom}</option>)}
                </Select>
                <Button
                  size="sm"
                  variant="primary"
                  icon="play"
                  loading={startDebug.isPending}
                  disabled={!debugNodeId}
                  title={!debugNodeId ? '先在画布上选一个节点' : '运行调试'}
                  onClick={() => startDebug.mutate()}
                >运行调试</Button>
                {!debugNodeId ? <span className="debug-hint">先在画布上选一个节点</span> : null}
              </div>
              {debugError ? (
                <div className="debug-error-banner"><Icon name="warning" size={13} /><span>{debugError}</span></div>
              ) : null}
              <div className="debug-sticky-head">
                <div
                  className="debug-progress"
                  data-testid="debug-progress"
                  data-done={debugSession?.done ? 'true' : 'false'}
                  data-total={totalCount}
                  data-finished={finishedCount}
                >
                  <div className="debug-progress-head">
                    <span className="mono debug-progress-count">{finishedCount}/{totalCount}</span>
                    <span className="debug-progress-label">
                      {debugSyncing ? <>状态同步中 <strong>{finishedCount}/{totalCount}</strong></> : runningDebug ? <>正在跑 <strong>{nodeLabel(runningDebug.node_id)}</strong></> : debugSession?.done ? '调试完成' : debugSessionId ? '等待首个节点…' : '尚未开始'}
                    </span>
                    {debugSyncing ? <span className="sync-hint">逐节点回放</span> : null}
                    <span className="debug-counts">
                      <span className="count passed">通过 {debugSummary.passed}</span>
                      <span className="count failed">失败 {debugSummary.failed}</span>
                      <span className="count skipped">跳过 {debugSummary.skipped}</span>
                    </span>
                  </div>
                  <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={totalCount} aria-valuenow={finishedCount}>
                    <span className={`progress-fill ${debugSummary.failed ? 'has-fail' : ''}`} style={{ width: `${progressPct}%` }} />
                    {totalCount ? <span className="progress-ticks">{Array.from({ length: Math.min(totalCount, 12) }).map((_, index) => <i key={index} />)}</span> : null}
                  </div>
                </div>
              </div>
              <div className="debug-tabs" role="tablist" aria-label="调试视图">
                <button type="button" role="tab" data-testid="debug-tab-timeline" aria-selected={debugTab === 'timeline'} className={debugTab === 'timeline' ? 'is-active' : ''} onClick={() => setDebugTab('timeline')}>时间线</button>
                <button type="button" role="tab" data-testid="debug-tab-output" aria-selected={debugTab === 'output'} className={debugTab === 'output' ? 'is-active' : ''} onClick={() => setDebugTab('output')}>输出</button>
                <button type="button" role="tab" data-testid="debug-tab-vars" aria-selected={debugTab === 'vars'} className={debugTab === 'vars' ? 'is-active' : ''} onClick={() => setDebugTab('vars')}>变量</button>
              </div>
              <div className="debug-tabpanel" ref={debugTabPanelRef} data-testid="debug-tabpanel" data-tab={debugTab} role="tabpanel">
                {debugTab === 'timeline' ? (
                  debugDetailOpen && debugStep ? (
                    <div className="debug-node-detail" data-testid="debug-detail" data-node-id={debugStep.node_id}>
                      <button type="button" className="debug-back" data-testid="debug-back" onClick={closeDebugDetail}>← 返回节点列表</button>
                      <div className="debug-detail-head">
                        <strong>{nodeLabel(debugStep.node_id)}</strong>
                        <span className="mono">{debugStep.node_id}</span>
                        <StatusBadge status={debugStep.status} />
                      </div>
                      {debugStep.reason ? (
                        <div className="debug-reason" data-testid="debug-reason">
                          <Icon name="warning" size={13} />
                          <span>{debugStep.status === 'skipped' ? '跳过原因' : debugStep.status === 'failed' ? '失败原因' : '原因'}：{debugReasonLabel(debugStep.reason)}</span>
                          <code>{debugStep.reason}</code>
                          {debugReasonDetail(debugStep.reason) ? <small className="debug-reason-detail">{debugReasonDetail(debugStep.reason)}</small> : null}
                        </div>
                      ) : null}
                      {selectedUpstreamBlocked.length && !['missing_input', 'upstream_skipped'].includes(debugStep.reason || '') ? (
                        <div className="debug-reason failed" data-testid="debug-upstream-note">
                          <Icon name="warning" size={13} />
                          <span>上游节点被跳过或输入为空：{selectedUpstreamBlocked.map(nodeLabel).join('、')}</span>
                          <small className="debug-reason-detail">当前节点依赖未满足，后端原始错误可能被误分类。</small>
                        </div>
                      ) : null}
                      {debugStep.error ? (
                        <div className="debug-reason failed">
                          <Icon name="warning" size={13} />
                          <span>{debugStep.reason ? '后端原始信息：' : ''}{errorText(debugStep.error)}</span>
                        </div>
                      ) : null}
                      <button type="button" className="debug-jump-output" onClick={() => setDebugTab('output')}>查看输入 / 输出 →</button>
                    </div>
                  ) : (
                    <div className="debug-list-wrap">
                      {skipGroups.length ? (
                        <div className="skip-groups">
                          <span className="skip-groups-label">跳过原因</span>
                          {skipGroups.map((group) => (
                            <details className="skip-group" key={group.reason} data-testid={`debug-skip-group-${group.reason}`} data-reason={group.reason} data-count={group.count} open={skipGroups.length <= 2}>
                              <summary>
                                <code className="skip-reason-code">{group.reason}</code>
                                <span>{debugReasonLabel(group.reason)}</span>
                                <Badge tone="neutral">{group.count}</Badge>
                              </summary>
                              <div className="skip-group-nodes">
                                {group.ids.map((nodeId) => (
                                  <button
                                    type="button"
                                    key={nodeId}
                                    onClick={() => {
                                      const state = debugStatusMap.get(nodeId)
                                      if (state) openDebugDetail(state)
                                      centerNode(nodeId)
                                    }}
                                  >
                                    {nodeLabel(nodeId)} <code>{nodeId}</code>
                                  </button>
                                ))}
                              </div>
                            </details>
                          ))}
                        </div>
                      ) : null}
                      <div className="debug-trace" data-testid="debug-trace">
                        {debugNodes.length ? debugNodes.map((step) => (
                          <button
                            type="button"
                            key={step.node_id}
                            ref={(element) => {
                              if (element) rowRefs.current.set(step.node_id, element)
                              else rowRefs.current.delete(step.node_id)
                            }}
                            className={`debug-row is-${step.status} ${debugStep?.node_id === step.node_id ? 'is-active' : ''} ${debugStatusMap.get(step.node_id)?.status === 'running' ? 'is-current' : ''}`}
                            data-testid={`debug-node-${step.node_id}`}
                            data-node-id={step.node_id}
                            data-status={step.status}
                            data-debug-status={step.status}
                            data-reason={step.reason || ''}
                            data-debug-reason={step.reason || undefined}
                            onClick={() => {
                              openDebugDetail(step)
                              centerNode(step.node_id)
                            }}
                          >
                            <span><strong>{nodeLabel(step.node_id)}</strong><small className="mono">{step.node_id} · {step.atom || workflow.graph?.nodes.find((node) => node.node_id === step.node_id)?.atom}</small></span>
                            <span className={`debug-status ${step.status}`}>{step.status === 'running' ? <span className="mini-pulse" /> : null}{statusLabel(step.status)}</span>
                            <span className="mono">{step.status === 'pending' || step.status === 'running' ? '—' : formatDuration(step.duration_ms)}</span>
                            <span className="debug-error" title={step.reason ? `${debugReasonLabel(step.reason)}：${debugReasonDetail(step.reason)}` : step.status === 'failed' && upstreamBlockedFor(step.node_id).length ? `上游节点被跳过或输入为空：${upstreamBlockedFor(step.node_id).map(nodeLabel).join('、')}` : errorText(step.error)}>
                              {step.reason
                                ? `${debugReasonLabel(step.reason)} · ${debugReasonDetail(step.reason) || step.reason}`
                                : step.status === 'failed' && upstreamBlockedFor(step.node_id).length
                                  ? `上游节点被跳过或输入为空（${upstreamBlockedFor(step.node_id).map(nodeLabel).join('、')}）`
                                  : errorText(step.error) || '—'}
                            </span>
                          </button>
                        )) : (
                          <div className="debug-empty">
                            <Icon name="terminal" size={20} />
                            <span>选择节点和模式后运行。调试只使用项目已配置环境，不写入执行历史。</span>
                          </div>
                        )}
                      </div>
                    </div>
                  )
                ) : null}
                {debugTab === 'output' ? (
                  <div className="debug-output-pane">
                    {debugStep ? (
                      <>
                        <div className="debug-detail-head">
                          <strong>{nodeLabel(debugStep.node_id)}</strong>
                          <span className="mono">{debugStep.node_id}</span>
                          <StatusBadge status={debugStep.status} />
                        </div>
                        <div className="debug-io">
                          <div><span className="io-label">inputs</span><JsonTree data={debugStep.inputs || {}} /></div>
                          <div><span className="io-label">outputs</span><JsonTree data={debugStep.outputs || {}} /></div>
                        </div>
                      </>
                    ) : <div className="debug-empty"><Icon name="code" size={20} /><span>先在时间线选择一个节点。</span></div>}
                  </div>
                ) : null}
                {debugTab === 'vars' ? (
                  <div className="debug-vars-pane">
                    {(() => {
                      const bag = debugSession?.variables || {}
                      const vars = (bag.vars && typeof bag.vars === 'object') ? bag.vars as AnyRecord : bag
                      const env = (bag.env && typeof bag.env === 'object') ? bag.env as AnyRecord : null
                      const clean = Object.fromEntries(Object.entries(vars).filter(([key]) => key !== 'vars' && key !== 'env'))
                      const tree = env && Object.keys(env).length ? { ...clean, env } : clean
                      return Object.keys(tree).length
                        ? <JsonTree data={tree} name="variables" depth={1} />
                        : <p className="muted">暂无变量快照。</p>
                    })()}
                  </div>
                ) : null}
              </div>
            </div>
            </section>
          </>
        ) : (
          <aside className="debug-rail" aria-label="调试面板已折叠">
            <button
              type="button"
              className="debug-rail-btn"
              data-testid="debug-rail-toggle"
              onClick={() => setDebugOpen(true)}
              title="展开调试面板"
            >
              <Icon name="bug" size={16} />
              <span>调试</span>
              {debugSession ? <Badge tone={debugSession.done ? 'ok' : 'warn'}>{finishedCount}/{totalCount}</Badge> : null}
            </button>
          </aside>
        )}

        <section className="canvas-panel">
          <div className={`plan-banner plan-${planMode}`} data-testid="plan-banner">
            <span className="plan-source">
              <Icon name={planMode === 'ai' ? 'spark' : planMode === 'rules' ? 'layers' : 'warning'} size={14} />
              {planMode === 'ai' ? 'AI 规划' : planMode === 'rules' ? `规则模板${plannerRaw === 'template' ? ' · 模板' : ''}` : '来源未标注'}
            </span>
            <span className="plan-confidence">置信度 <strong>{confidence === null ? '—' : confidence.toFixed(2)}</strong></span>
            {planMode === 'rules' ? <span className="plan-low"><Icon name="warning" size={12} />低置信度，请人工核对</span> : null}
            {planMode === 'unknown' ? <span className="plan-low">请人工核对计划来源</span> : null}
            {assumptions.length ? (
              <span className="plan-assumptions">
                待确认环境变量
                {assumptions.map((item) => <code key={item}>{item}</code>)}
              </span>
            ) : <span className="plan-assumptions muted">没有待确认假设</span>}
            {autofilled.length ? (
              <span className="plan-assumptions" data-testid="param-autofilled" title="生成阶段按原子输入端口自动填入的 URL">
                <Icon name="check" size={12} /> 已自动填写
                {autofilled.slice(0, 3).map((item, index) => (
                  <code key={`${item.node_id}-${item.param}-${index}`}>{item.param}={item.value}</code>
                ))}
              </span>
            ) : null}
            {paramGaps.length ? (
              <span className="plan-assumptions plan-gaps" data-testid="param-gaps" title="必填端口缺失或仍是未解析的环境变量">
                <Icon name="warning" size={12} /> 参数缺口
                {paramGaps.slice(0, 3).map((item, index) => (
                  <code key={`${item.node_id}-${item.param}-${index}`}>{item.param}{item.reason === 'unresolved_env' ? '=env' : ''}</code>
                ))}
              </span>
            ) : null}
          </div>
          {capabilityGaps.length ? (
            <div className="capability-gap-bar" data-testid="capability-gap-bar">
              <Icon name="warning" size={14} />
              <strong>能力缺口 {capabilityGaps.length}</strong>
              {capabilityGaps.map((gap) => (
                <span className="gap-chip" key={gap.atom} title={gap.message || ''}>
                  <code>{gap.atom}</code>
                  {gap.suggestions.length ? <em>{gap.suggestions.join(' / ')}</em> : null}
                </span>
              ))}
            </div>
          ) : null}
          <div className="canvas-toolbar">
            <div className="canvas-toolbar-left">
              <Button size="sm" variant="ghost" icon="plus" onClick={() => setAddOpen(true)}>添加节点</Button>
              <span className="legend"><span className="legend-line data" />数据边</span>
              <span className="legend"><span className="legend-line order" />顺序边 order-only</span>
            </div>
            <div className="canvas-toolbar-right">
              <span>{workflow.graph?.nodes?.length || 0} 节点 · {workflow.graph?.edges?.length || 0} 边</span>
              <span className="canvas-tip"><Icon name="bug" size={13} /> 拖拽端点连线</span>
            </div>
          </div>
          <div className="flow-wrap" ref={flowWrapRef}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeDragStop={() => setNodes((current) => { savePositions(wid, current); return current })}
              onConnect={onConnect}
              onNodeClick={(_, node) => { setNodeRailCollapsed(false); setSelectedId(node.id); setPanelOpen(true) }}
              onPaneClick={() => { setSelectedId(''); setPanelOpen(false) }}
              onNodeDoubleClick={(_, node) => { setNodeRailCollapsed(false); setSelectedId(node.id); setPanelOpen(true) }}
              onInit={(instance) => { flowRef.current = instance }}
              fitView
              fitViewOptions={{ padding: 0.14 }}
              minZoom={0.35}
              maxZoom={1.8}
              defaultEdgeOptions={{ type: 'smoothstep' }}
              proOptions={{ hideAttribution: false }}
              className="studio-flow"
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#243149" />
              <Controls showInteractive={false} />
              <MiniMap pannable zoomable nodeColor={(node) => (node.selected ? '#8b7cff' : '#26344e')} maskColor="rgba(6,10,18,.72)" className="studio-minimap" />
            </ReactFlow>
            {!workflow.graph?.nodes?.length ? (
              <div className="canvas-empty">
                <Icon name="flow" size={26} />
                <strong>画布还是空的</strong>
                <p>添加一个原子节点开始编排。</p>
                <Button size="sm" variant="primary" icon="plus" onClick={() => setAddOpen(true)}>添加节点</Button>
              </div>
            ) : null}
          </div>
          <div className="canvas-statusbar">
            <div className="status-mini">
              <span className={`validation-pill ${blockingErrors ? 'bad' : 'ok'}`}><span className="status-dot" />{blockingErrors ? '校验未通过' : '校验通过'}</span>
              <span className="muted">节点拖动仅保存本地 ui.x/y，不产生新 rev</span>
            </div>
            <button type="button" className={`debug-trigger ${debugOpen ? 'is-active' : ''}`} onClick={() => setDebugOpen((value) => !value)}>
              <Icon name="bug" size={15} />
              调试
              {debugSession ? <Badge tone={debugSession.done ? 'ok' : 'warn'}>{finishedCount}/{totalCount}</Badge> : null}
            </button>
          </div>
        </section>

        {nodeRailCollapsed ? (
          <aside className="side-panel-rail" aria-label="节点属性面板已折叠">
            <button
              type="button"
              data-testid="node-panel-rail-toggle"
              onClick={() => {
                setNodeRailCollapsed(false)
                if (window.matchMedia('(max-width: 1080px)').matches) setPanelOpen(true)
              }}
              title="展开节点属性面板"
            >
              <Icon name="settings" size={16} />
              <span>节点</span>
            </button>
          </aside>
        ) : (
        <aside className={`side-panel studio-node-panel ${panelOpen ? 'is-mobile-open' : ''}`}>
          <div className="side-panel-head">
            <div><span className="side-kicker">{selectedNode ? '节点设置' : '任务检查'}</span><h2>{selectedNode?.name || '未选择节点'}</h2></div>
            <div className="side-head-actions">
              <IconButton label="折叠属性面板" icon="chevron" className="node-panel-collapse" onClick={() => setNodeRailCollapsed(true)} />
              <IconButton label="关闭面板" icon="close" className="mobile-only" onClick={() => setPanelOpen(false)} />
              {selectedNode ? <IconButton label={selectedNode.locked ? '解锁节点' : '锁定节点'} icon={selectedNode.locked ? 'lock' : 'unlock'} className={selectedNode.locked ? 'is-locked' : ''} onClick={() => toggleLock.mutate()} /> : null}
            </div>
          </div>

          {selectedNode ? (
            <div className="node-panel-content">
              <div className="node-meta-row">
                <span className={`node-origin ${selectedNode.origin === 'human' ? 'human' : 'ai'}`}>{selectedNode.origin === 'human' ? '人工修改' : 'AI 生成'}</span>
                <span className="mono">{selectedNode.node_id}</span>
                {selectedNode.locked ? <Badge tone="warn"><Icon name="lock" size={10} />锁定</Badge> : null}
              </div>
              {selectedNewAtom ? (
                <details className="new-atom-contract" data-testid="canvas-new-atom-contract" data-atom={selectedNode.atom} open>
                  <summary>
                    <span className="new-atom-contract-title"><Icon name="spark" size={12} />新原子契约</span>
                    <span className="mono">{selectedNode.atom}</span>
                  </summary>
                  <div className="new-atom-contract-body">
                    <div className="new-atom-contract-head">
                      <Badge tone="violet">新原子</Badge>
                      <strong className="mono">{selectedNode.atom}</strong>
                      {selectedContract?.pack ? <span className="new-atom-contract-pack mono">{selectedContract.pack}</span> : null}
                    </div>
                    {selectedContract?.description ? <p className="new-atom-contract-desc">{selectedContract.description}</p> : null}
                    <dl className="new-atom-contract-grid">
                      <div><dt>inputs</dt><dd><pre>{contractText(selectedContract?.inputs)}</pre></dd></div>
                      <div><dt>outputs</dt><dd><pre>{contractText(selectedContract?.outputs)}</pre></dd></div>
                      <div><dt>output_paths</dt><dd><pre>{contractText(selectedContract?.output_paths)}</pre></dd></div>
                      <div><dt>effects</dt><dd><pre>{contractText(selectedContract?.effects)}</pre></dd></div>
                      <div><dt>source_script</dt><dd><code className="mono">{contractText(selectedContract?.source_script)}</code></dd></div>
                      <div><dt>code_sha256</dt><dd><code className="mono new-atom-sha">{contractText(selectedContract?.code_sha256)}</code></dd></div>
                    </dl>
                    {!selectedContract?.output_paths || !selectedContract?.code_sha256 ? (
                      <p className="new-atom-contract-note">
                        完整的 output_paths / code_sha256 来自本次固化的结果缓存；若在新浏览器打开，原子接口不返回这两个字段。
                      </p>
                    ) : null}
                  </div>
                </details>
              ) : null}
              <Field label="业务名称"><input className="input" value={paramName} onChange={(event) => setParamName(event.target.value)} /></Field>
              <Field label="原子">
                <Select value={paramAtom} onChange={(event) => setParamAtom(event.target.value)}>
                  {atoms.map((atom) => <option key={atom.name} value={atom.name}>{atom.name}</option>)}
                </Select>
              </Field>
              <div className="param-section">
                <div className="param-section-head"><span>参数</span><span>{Object.keys(paramWith).length} 项</span></div>
                {Object.keys({ ...paramWith, ...paramJson }).map((key) => {
                  const port = selectedAtom?.input_ports?.find((item) => item.name === key)
                  const value = paramWith[key]
                  const type = port?.type || (typeof value === 'number' ? 'number' : typeof value === 'boolean' ? 'bool' : value && typeof value === 'object' ? 'json' : 'string')
                  const isJson = type.includes('json') || type.includes('object') || type.includes('array') || (value && typeof value === 'object')
                  const isBool = type.includes('bool')
                  const isNumber = type.includes('int') || type.includes('number')
                  return (
                    <label className="param-field" key={key}>
                      <span className="param-label"><code>{key}</code>{port?.required ? <Badge tone="bad">必填</Badge> : null}<small>{port?.type || typeof value}</small></span>
                      {isBool ? (
                        <input type="checkbox" checked={!!value} onChange={(event) => setParamWith({ ...paramWith, [key]: event.target.checked })} />
                      ) : isJson ? (
                        <textarea className="textarea mono" rows={4} value={paramJson[key] ?? JSON.stringify(value ?? {}, null, 2)} onChange={(event) => setParamJson({ ...paramJson, [key]: event.target.value })} spellCheck={false} />
                      ) : isNumber ? (
                        <input className="input mono" type="number" value={value ?? ''} onChange={(event) => setParamWith({ ...paramWith, [key]: event.target.value === '' ? '' : Number(event.target.value) })} />
                      ) : (
                        <textarea className="textarea mono" rows={String(value ?? '').length > 40 ? 3 : 2} value={value ?? ''} onChange={(event) => setParamWith({ ...paramWith, [key]: event.target.value })} spellCheck={false} />
                      )}
                    </label>
                  )
                })}
                {!Object.keys(paramWith).length && !Object.keys(paramJson).length ? <p className="muted">这个节点还没有参数；可在下方高级 JSON 中补充。</p> : null}
                <details className="advanced-params">
                  <summary>高级：完整 with JSON</summary>
                  <textarea
                    className="textarea mono"
                    rows={7}
                    value={JSON.stringify(paramWith, null, 2)}
                    onChange={(event) => {
                      try {
                        const parsed = JSON.parse(event.target.value)
                        setParamWith(parsed)
                        setParamError('')
                      } catch {
                        setParamError('完整 JSON 当前不合法，修正后才能保存')
                      }
                    }}
                    spellCheck={false}
                  />
                </details>
                {paramError ? <div className="inline-error"><Icon name="warning" size={13} />{paramError}</div> : null}
                <Button size="sm" variant="primary" icon="check" loading={updateNode.isPending} onClick={() => updateNode.mutate()}>保存参数 · 新 rev</Button>
              </div>

              <div className="port-binding-section">
                <div className="param-section-head"><span>端口绑定</span><span>{Object.keys(selectedNode.capture || {}).length} 输出 · {(workflow.graph?.edges || []).filter((edge) => edge.to === selectedNode.node_id && edge.kind === 'data').length} 输入</span></div>
                <div className="binding-block">
                  <span className="io-label">outputs → 下游 with</span>
                  {Object.keys(selectedNode.capture || {}).length ? Object.keys(selectedNode.capture || {}).map((key) => {
                    const targets = (workflow.graph?.edges || []).filter((edge) => edge.from === selectedNode.node_id && edge.kind === 'data' && edge.via === key)
                    return <div className="binding-port-row out" key={key}><code>{key}</code><Icon name="arrow" size={11} />{targets.length ? targets.map((edge) => <span key={edge.to}>{workflow.graph?.nodes.find((node) => node.node_id === edge.to)?.name || edge.to}</span>) : <span className="muted">暂无消费节点</span>}</div>
                  }) : <p className="muted">该节点没有 capture，不能提供数据边。</p>}
                </div>
                <div className="binding-block">
                  <span className="io-label">inputs ← 上游变量</span>
                  {(workflow.graph?.edges || []).filter((edge) => edge.to === selectedNode.node_id && edge.kind === 'data').length ? (workflow.graph?.edges || []).filter((edge) => edge.to === selectedNode.node_id && edge.kind === 'data').map((edge) => (
                    <div className="binding-port-row in" key={edge.edge_id || edge.from + edge.via}><code>{edge.via || 'data'}</code><span>{workflow.graph?.nodes.find((node) => node.node_id === edge.from)?.name || edge.from}</span></div>
                  )) : <p className="muted">没有数据边输入；执行顺序为 order-only。</p>}
                </div>
              </div>

              <div className="node-risk-section">
                <div className="param-section-head"><span>风险与副作用</span></div>
                <div className="node-effect-list">
                  {atomEffects(selectedNode).map((effect) => <Badge key={effect} tone={effect === 'destructive' ? 'bad' : effect === 'write' || effect === 'mutate' ? 'warn' : 'neutral'}>{effect}</Badge>)}
                  {risks.filter((risk) => (risk.node_ids || []).includes(selectedNode.node_id)).map((risk) => (
                    <div className="node-risk" key={risk.id || risk.message}><Icon name="warning" size={13} /><span>{risk.message}</span></div>
                  ))}
                  {!risks.some((risk) => (risk.node_ids || []).includes(selectedNode.node_id)) ? <p className="muted">没有阻断性风险；仍需在审阅页确认全局风险。</p> : null}
                </div>
              </div>

              {debugStatusMap.get(selectedNode.node_id) ? (
                <div className="recent-debug">
                  <span><Icon name="bug" size={13} /> 最近调试</span>
                  {(() => {
                    const step = debugStatusMap.get(selectedNode.node_id)!
                    return <><StatusBadge status={step.status} /><span className="mono">{formatDuration(step.duration_ms)}</span></>
                  })()}
                  {debugStatusMap.get(selectedNode.node_id)?.reason ? (
                    <span className="recent-debug-reason">原因：{debugReasonLabel(debugStatusMap.get(selectedNode.node_id)?.reason)} <code>{debugStatusMap.get(selectedNode.node_id)?.reason}</code>{debugReasonDetail(debugStatusMap.get(selectedNode.node_id)?.reason) ? <> · {debugReasonDetail(debugStatusMap.get(selectedNode.node_id)?.reason)}</> : null}</span>
                  ) : null}
                  <details className="recent-debug-io">
                    <summary>查看 reason / inputs / outputs</summary>
                    {(() => {
                      const step = debugStatusMap.get(selectedNode.node_id)!
                      return (
                        <>
                          {step.reason ? <div className="debug-reason"><Icon name="warning" size={13} /><span>{debugReasonLabel(step.reason)}</span><code>{step.reason}</code>{debugReasonDetail(step.reason) ? <small className="debug-reason-detail">{debugReasonDetail(step.reason)}</small> : null}</div> : null}
                          <div className="debug-io"><div><span className="io-label">inputs</span><JsonTree data={step.inputs || {}} /></div><div><span className="io-label">outputs</span><JsonTree data={step.outputs || {}} /></div></div>
                        </>
                      )
                    })()}
                  </details>
                </div>
              ) : null}

              <div className="node-danger">
                <Button size="sm" variant="ghost" icon="trash" onClick={() => selectedNode.locked ? push('锁定节点不能删除，先解锁', 'error') : setDeleteOpen(true)}>删除节点</Button>
                <span>删除会同时移除相关边并生成新 rev</span>
              </div>
            </div>
          ) : (
            <div className="flow-inspector">
              <div className="inspector-summary">
                <div><span>节点</span><strong>{workflow.graph?.nodes?.length || 0}</strong></div>
                <div><span>边</span><strong>{workflow.graph?.edges?.length || 0}</strong></div>
                <div><span>错误</span><strong className={errors.length ? 'bad' : 'ok'}>{errors.length}</strong></div>
                <div><span>警告</span><strong className={warnings.length ? 'warn' : 'ok'}>{warnings.length}</strong></div>
              </div>
              <div className="validation-digest">
                <div className="param-section-head"><span>语义校验</span><span>{coverage ? `${Math.round(coverage.ratio * 100)}%` : '未提供'}</span></div>
                {coverage ? (
                  <CoverageLine coverage={coverage} isExplore={isExplore} />
                ) : <p className="muted">后端未返回 coverage；无法判断意图覆盖。</p>}
                {missingEnv.length ? (
                  <div className="digest-block">
                    <span className="io-label">missing_env</span>
                    <div className="digest-chips">{missingEnv.map((key) => <code key={key}>{key}</code>)}</div>
                  </div>
                ) : null}
                {capabilityGaps.length ? (
                  <div className="digest-block">
                    <span className="io-label">capability_gaps</span>
                    {capabilityGaps.map((gap) => (
                      <div className="digest-gap" key={gap.atom}><code>{gap.atom}</code>{gap.suggestions.length ? <small>{gap.suggestions.join(' / ')}</small> : null}</div>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="issue-list">
                <div className="param-section-head"><span>校验问题</span><span>{errors.length + warnings.length}</span></div>
                {errors.map((issue) => (
                  <button type="button" key={issue.id || issue.message} className="issue-row bad" data-node-id={issue.node_id || undefined} onClick={() => locateNode(issue.node_id)} disabled={!issue.node_id} title={issue.node_id ? `定位节点 ${issue.node_id}` : issue.message}>
                    <Icon name="warning" size={13} /><span>{issue.message}{issue.code ? <small className="issue-code">{issue.code}</small> : null}</span><Icon name="chevron" size={12} />
                  </button>
                ))}
                {warnings.map((issue) => (
                  <button type="button" key={issue.id || issue.message} className="issue-row warn" data-node-id={issue.node_id || undefined} onClick={() => locateNode(issue.node_id)} disabled={!issue.node_id} title={issue.node_id ? `定位节点 ${issue.node_id}` : issue.message}>
                    <Icon name="bolt" size={13} /><span>{issue.message}{issue.code ? <small className="issue-code">{issue.code}</small> : null}</span><Icon name="chevron" size={12} />
                  </button>
                ))}
                {!errors.length && !warnings.length ? <div className="inspector-ok"><Icon name="check" size={14} />当前版本校验通过</div> : null}
              </div>
              <div className="risk-summary">
                <div className="param-section-head"><span>审阅风险</span><span>{risks.length}</span></div>
                {risks.length ? risks.map((risk) => <div className="node-risk" key={risk.id || risk.message}><Icon name="shield" size={13} /><span>{risk.message}</span></div>) : <p className="muted">没有需要确认的风险。</p>}
              </div>
              <div className="inspector-note">
                <Icon name="layers" size={16} />
                <div><strong>选择节点开始编辑</strong><p>点击画布节点打开参数、端口绑定和最近调试结果。拖动节点只保存本地位置。</p></div>
              </div>
            </div>
          )}
        </aside>
        )}
      </div>
      ) : tab === 'triggers' ? (
        <div className="studio-tab-panel">
          <TriggerPanel wid={wid} workflowRef={workflow.published_ref || ''} />
        </div>
      ) : (
        <div className="studio-tab-panel">
          <WorkflowRunsPanel wid={wid} workflowRef={workflow.published_ref || ''} />
        </div>
      )}

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="添加节点"
        subtitle="从当前领域包选择一个原子；新增会立即生成新 rev。"
        size="lg"
        footer={<><Button variant="ghost" onClick={() => setAddOpen(false)}>取消</Button><Button variant="primary" icon="plus" disabled={!addAtom} loading={addNode.isPending} onClick={() => addNode.mutate()}>添加节点</Button></>}
      >
        <SearchInput value={addSearch} onChange={setAddSearch} placeholder="搜索原子名称或描述" />
        <div className="atom-pick-list">
          {filteredAtoms.map((atom) => (
            <button type="button" key={atom.name} className={`atom-pick ${addAtom?.name === atom.name ? 'is-active' : ''}`} onClick={() => { setAddAtom(atom); setAddName(atom.name) }}>
              <span className="atom-pick-copy"><strong>{atom.name}</strong><small>{atom.description || atom.category}</small></span>
              <span className="atom-pick-effects">{(atom.effects || ['pure']).map((effect) => <Badge key={effect} tone={effect === 'destructive' ? 'bad' : effect === 'write' || effect === 'mutate' ? 'warn' : 'neutral'}>{effect}</Badge>)}</span>
            </button>
          ))}
        </div>
        {addAtom ? <Field label="业务名称"><input className="input" value={addName} onChange={(event) => setAddName(event.target.value)} placeholder={addAtom.name} /></Field> : null}
      </Modal>

      <Modal
        open={!!pendingConnection}
        onClose={() => setPendingConnection(null)}
        title="新增连线"
        subtitle="数据边代表变量依赖；顺序边只保证执行次序。"
        size="sm"
        footer={<><Button variant="ghost" onClick={() => setPendingConnection(null)}>取消</Button><Button variant="primary" loading={addEdgeMutation.isPending} onClick={() => addEdgeMutation.mutate()}>写入连线</Button></>}
      >
        <div className="edge-kind-grid">
          <button type="button" className={edgeKind === 'data' ? 'is-active' : ''} onClick={() => setEdgeKind('data')}><span className="edge-sample data" /><strong>数据边</strong><small>实线，label=变量名</small></button>
          <button type="button" className={edgeKind === 'order' ? 'is-active' : ''} onClick={() => setEdgeKind('order')}><span className="edge-sample order" /><strong>顺序边</strong><small>虚线，label=order-only</small></button>
        </div>
        {edgeKind === 'data' ? <Field label="变量名" hint="下游通过 ${变量名} 引用；需与 source 节点的 capture 对齐。"><input className="input mono" value={edgeVia} onChange={(event) => setEdgeVia(event.target.value)} placeholder="token" /></Field> : <p className="muted">顺序边不传递数据，只约束执行顺序。</p>}
        <div className="edge-endpoints"><code>{pendingConnection?.source}</code><Icon name="arrow" size={14} /><code>{pendingConnection?.target}</code></div>
      </Modal>

      <Modal
        open={replanOpen}
        onClose={() => setReplanOpen(false)}
        title="AI 重规划"
        subtitle="只生成 diff 与 proposed_rev，不自动应用；锁定节点冲突会返回 409。"
        footer={<><Button variant="ghost" onClick={() => setReplanOpen(false)}>取消</Button><Button variant="primary" icon="spark" loading={replan.isPending} onClick={() => replan.mutate()}>生成提议</Button></>}
      >
        <Field label="补充指令"><textarea className="textarea" rows={4} value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="描述要补充或调整的内容" /></Field>
        <div className="notice-inline"><Icon name="lock" size={15} /><div><strong>锁定优先</strong><p>当前 {workflow.graph?.nodes?.filter((node) => node.locked).length || 0} 个锁定节点将被逐节点比对，不会改动。</p></div></div>
      </Modal>

      <ConfirmDialog
        open={deleteOpen}
        title="删除节点"
        message={<>确定删除节点「{selectedNode?.name}」？相关边会一并删除，并生成新 rev。</>}
        confirmLabel="删除节点"
        danger
        busy={removeNode.isPending}
        onClose={() => setDeleteOpen(false)}
        onConfirm={() => removeNode.mutate()}
      />
    </div>
  )
}

function normalizeLocal(validation: Validation): Validation {
  return { errors: validation.errors || [], warnings: validation.warnings || [], risks: validation.risks || [] }
}
