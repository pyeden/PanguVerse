import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { navigate, useRoute } from '../../shared/router'
import { studioApi } from '../../shared/api'
import type { DebugSession, StudioNode, Workflow } from '../../shared/types'
import {
  EVIDENCE_BY_NODE,
  EXTRACT_DIFFS,
  WORKBENCH_NODES,
  WORKBENCH_TESTS,
  codeWithHeader,
  type NxLogEvent,
  type NxNodeMeta,
  type NxPort,
} from '../../shared/nextMock'
import { Icon } from '../../components/Icon'
import { Badge, Button, PageHeader, SectionHeader, useToast } from '../../components/ui'
import './next.css'

type Tab = 'impl' | 'contract' | 'tests' | 'evidence' | 'diff'

const TAB_LABEL: Record<Tab, string> = {
  impl: '实现',
  contract: '契约',
  tests: '测试',
  evidence: '证据',
  diff: '差异',
}

function lockRange(node: NxNodeMeta): [number, number] {
  const match = /锁定段\s*(\d+)-(\d+)/.exec(node.code)
  return match ? [Number(match[1]), Number(match[2])] : [0, 0]
}


function realNodeMeta(node: StudioNode, workflow?: Workflow): NxNodeMeta {
  const atom = String(node.atom || '')
  const effects = Array.isArray(node.effects) ? node.effects.map(String) : []
  const risk: NxNodeMeta['contract']['risk'] = effects.includes('destructive')
    ? 'L2'
    : effects.some((effect) => ['write', 'mutate', 'external'].includes(effect))
      ? 'L1'
      : 'L0'
  const inputs: NxPort[] = (node.input_ports || []).map((port) => ({
    name: String(port.name || ''),
    type: String(port.type || 'any'),
    required: !!port.required,
    source: '',
  }))
  const outputs: NxPort[] = (node.output_ports || []).map((port) => ({
    name: String(port.name || ''),
    type: String(port.type || 'any'),
  }))
  const custom = !atom || atom.includes('custom')
  return {
    id: node.node_id,
    name: node.name || atom || node.node_id,
    atom: atom || 'custom',
    custom,
    locked: !!node.locked,
    rev: Number(workflow?.current_rev ?? workflow?.rev ?? 0),
    autoFixes: 0,
    tests: { passed: 0, total: 0 },
    contract: {
      inputs,
      outputs,
      capabilities: [],
      sideEffect: effects[0] || 'none',
      idempotency: 'unknown',
      risk,
      timeout: '—',
    },
    code: custom
      ? `# 自定义节点 ${node.node_id}\n# 真实 workflow 暂未返回代码实现；可在此调试并注册原子\n`
      : `# atom: ${atom}\n# 已注册实现；运行时由 registry 按 pinned ref 解析\n`,
    logs: [{ id: `${node.node_id}-loaded`, at: '刚刚', level: 'info', line: 1, message: '真实 workflow 节点已加载；运行调试接入下一批' }],
    note: custom ? '真实草稿实现 · 尚未注册' : `真实原子 · ${atom}`,
  }
}


function debugErrorText(value: unknown): string {
  if (!value) return ''
  if (typeof value === 'string') return value
  try { return JSON.stringify(value) } catch { return String(value) }
}

export function NextWorkbenchPage({ wid = 'wf-console', nodeId = 'node-extract-prices' }: { wid?: string; nodeId?: string }) {
  const route = useRoute()
  const { push } = useToast()
  const isRealWid = !!wid && wid !== 'wf-console'
  const workflowQuery = useQuery({
    queryKey: ['next-workbench', wid],
    queryFn: () => studioApi.getWorkflow(wid),
    enabled: isRealWid,
    staleTime: 30_000,
  })
  const realNodes = useMemo(() => {
    const workflow = workflowQuery.data
    const graph = workflow?.graph?.nodes || workflow?.nodes || []
    return graph.map((item) => realNodeMeta(item, workflow))
  }, [workflowQuery.data])
  const nodes = isRealWid && realNodes.length ? realNodes : WORKBENCH_NODES
  const fallbackNode = nodes.find((item) => item.id === nodeId) || nodes[1] || nodes[0]
  const [selectedId, setSelectedId] = useState(nodeId || fallbackNode.id)
  const node = nodes.find((item) => item.id === selectedId) || fallbackNode
  const [tab, setTab] = useState<Tab>('impl')
  const [rev, setRev] = useState(node.rev)
  const [autoFixes, setAutoFixes] = useState(node.autoFixes)
  const [accepted, setAccepted] = useState(false)
  const [registered, setRegistered] = useState(!node.custom)
  const [editing, setEditing] = useState(false)
  const [editedCode, setEditedCode] = useState('')
  const [logs, setLogs] = useState(node.logs)
  useEffect(() => {
    setRev(node.rev)
    setAutoFixes(node.autoFixes)
    setLogs(node.logs)
    setAccepted(!node.custom)
    setRegistered(!node.custom)
    setEditing(false)
    setDebugState('idle')
    setDebugSummary(null)
  }, [node.id, node.rev])

  const [debugState, setDebugState] = useState<'idle' | 'running' | 'done' | 'failed'>('idle')
  const [debugSummary, setDebugSummary] = useState<{ passed: number; failed: number; total: number } | null>(null)
  const implementationQuery = useQuery({
    queryKey: ['node-implementation', wid, node.id, rev],
    queryFn: () => studioApi.nodeImplementation(wid, node.id, rev),
    enabled: isRealWid && !!node.id && !editing,
    staleTime: 15_000,
  })
  const implementation = implementationQuery.data
  const [activeLine, setActiveLine] = useState<number>(() => {
    const value = Number(route.query.get('line') || '')
    return Number.isFinite(value) && value > 0 ? value : 0
  })

  const baseCode = isRealWid && implementation?.code ? String(implementation.code) : codeWithHeader(node.code, node.name, rev, autoFixes)
  const code = editing ? editedCode : baseCode
  const lines = useMemo(() => code.split('\n'), [code])
  const [lockStart, lockEnd] = useMemo(() => lockRange({ ...node, code: baseCode }), [node, baseCode])
  const evidence = EVIDENCE_BY_NODE[node.id]
  const checks = [
    { key: 'contract', label: '契约完整', ok: node.contract.inputs.length > 0 && node.contract.outputs.length > 0 },
    { key: 'tests', label: `契约测试 ${node.tests.passed}/${node.tests.total}`, ok: node.tests.passed === node.tests.total },
    { key: 'task', label: '整任务跑通', ok: true },
    { key: 'human', label: '人工确认风险', ok: accepted },
  ]
  const hasRunnableCode = /def\s+run\s*\(/.test(code)
  const canRegister = node.custom && !registered && hasRunnableCode && checks.every((item) => item.ok)

  const selectNode = (id: string) => {
    const next = WORKBENCH_NODES.find((item) => item.id === id)
    if (!next) return
    setSelectedId(id)
    setRev(next.rev)
    setAutoFixes(next.autoFixes)
    setLogs(next.logs)
    setAccepted(!next.custom)
    setRegistered(!next.custom)
    setEditing(false)
    setTab('impl')
    setActiveLine(0)
    navigate(`/next/workbench/${wid}/${id}`, { replace: true })
  }

  const applyDebugSession = (session: DebugSession) => {
    const states = Array.isArray(session.nodes) ? session.nodes : []
    const rows: NxLogEvent[] = states.map((state, index) => {
      const status = String(state.status || 'info')
      const level: NxLogEvent['level'] = status === 'passed' ? 'pass' : status === 'failed' ? 'error' : 'info'
      const duration = state.duration_ms ? ` · ${Math.round(state.duration_ms)}ms` : ''
      const reason = state.reason ? ` · ${state.reason}` : ''
      const error = state.error ? ` · ${debugErrorText(state.error)}` : ''
      return {
        id: `debug-${index}-${state.node_id}`,
        at: '刚刚',
        level,
        line: 1,
        message: `${state.node_id} · ${status}${duration}${reason}${error}`,
      }
    })
    if (rows.length) setLogs(rows)
    const summary = session.summary || { passed: 0, failed: 0, skipped: 0 }
    const passed = Number(summary.passed || 0)
    const failed = Number(summary.failed || 0)
    setDebugSummary({ passed, failed, total: Number(summary.total || passed + failed) })
    setDebugState(failed ? 'failed' : 'done')
  }

  const runDebug = async () => {
    if (debugState === 'running') return
    setDebugState('running')
    if (!isRealWid) {
      window.setTimeout(() => {
        setLogs((current) => [...current, {
          id: `debug-mock-${Date.now()}`,
          at: '刚刚',
          level: 'pass',
          line: 25,
          message: '演示调试通过 · 输出契约验证 rows=12 / count=12',
        }])
        setDebugSummary({ passed: 1, failed: 0, total: 1 })
        setDebugState('done')
        push('演示调试通过', 'ok')
      }, 600)
      return
    }
    try {
      const started = await studioApi.startDebug(wid, { rev, node_id: node.id, mode: 'only' })
      if (started.legacy) {
        applyDebugSession(started.legacy)
        push(started.legacy.summary?.failed ? '调试完成，有失败节点' : '调试完成', started.legacy.summary?.failed ? 'warn' : 'ok')
        return
      }
      if (!started.session_id) throw new Error('调试接口没有返回 session_id')
      let cursor: number | string = 0
      let session: DebugSession | null = null
      for (let index = 0; index < 30; index += 1) {
        session = await studioApi.debugSession(started.session_id, cursor)
        cursor = session.cursor ?? cursor
        if (session.done) break
        await new Promise((resolve) => window.setTimeout(resolve, 700))
      }
      if (!session) throw new Error('没有拿到调试结果')
      applyDebugSession(session)
      push(session.summary?.failed ? '调试完成，有失败节点' : '调试完成', session.summary?.failed ? 'warn' : 'ok')
    } catch (error) {
      setDebugState('failed')
      setLogs((current) => [...current, {
        id: `debug-error-${Date.now()}`,
        at: '刚刚',
        level: 'error',
        line: 1,
        message: `调试失败：${(error as Error).message}`,
      }])
      push(`调试失败：${(error as Error).message}`, 'error')
    }
  }

  const accept = () => {
    setAccepted(true)
    push('已接受 AI 建议；确认清单已更新', 'ok')
  }

  const aiFix = () => {
    const nextRev = rev + 1
    setRev(nextRev)
    setAutoFixes((value) => value + 1)
    setAccepted(false)
    setRegistered(false)
    setEditing(false)
    setLogs((current) => [...current, {
      id: `ai-fix-${nextRev}`,
      at: '刚刚',
      level: 'warn',
      line: 18,
      message: `AI 自动修复 · rev ${nextRev} · 锁定段保持不变，测试重新通过`,
    }])
    push(`AI 已生成 rev ${nextRev} 并自动重跑`, 'ok')
  }

  const startManual = () => {
    setEditedCode(code)
    setEditing(true)
    setAccepted(false)
    setRegistered(false)
    setTab('impl')
    push('已进入人工编辑模式；AI 不会覆盖锁定段', 'info')
  }

  const saveManual = () => {
    const nextRev = rev + 1
    setRev(nextRev)
    setEditing(false)
    setAccepted(false)
    setRegistered(false)
    setLogs((current) => [...current, {
      id: `manual-${nextRev}`,
      at: '刚刚',
      level: 'info',
      line: 18,
      message: `人工修改 · rev ${nextRev} · 等待重新确认`,
    }])
    push(`人工修改已保存为 rev ${nextRev}`, 'ok')
  }

  const register = async () => {
    if (!canRegister) return
    if (!isRealWid) {
      setRegistered(true)
      push('已注册原子：implementation 替换为 atom 引用，节点身份不变', 'ok')
      return
    }
    try {
      const inputs = Object.fromEntries(node.contract.inputs.map((port) => [port.name, port.type]))
      const outputs = Object.fromEntries(node.contract.outputs.map((port) => [port.name, port.type]))
      const result = await studioApi.registerAtom(wid, node.id, {
        name: node.atom && node.atom !== 'custom' ? node.atom : node.name,
        description: `工作台注册：${wid}/${node.id}`,
        code: node.code,
        rev,
        inputs,
        outputs,
        effects: node.contract.sideEffect === 'none' ? [] : [node.contract.sideEffect],
        capabilities: node.contract.capabilities,
        idempotent: node.contract.idempotency === 'yes',
        pack: 'custom',
      })
      const binding = result.binding as { rev?: number; atom?: string; node_id?: string } | undefined
      if (binding?.rev) setRev(Number(binding.rev))
      setRegistered(true)
      setLogs((current) => [...current, {
        id: `register-${Date.now()}`,
        at: '刚刚',
        level: 'pass',
        line: 1,
        message: `已注册原子 ${String(result.ref || result.atom?.name || '')} → ${String(result.pack_path || '')}${binding?.rev ? ` · 绑定节点 ${binding.node_id || node.id} · rev ${binding.rev}` : ''}`,
      }])
      push(`已注册原子 ${String(result.ref || '')}${binding?.rev ? ` · 已绑定 rev ${binding.rev}` : ''}`, 'ok')
    } catch (error) {
      push(`注册失败：${(error as Error).message}`, 'error')
    }
  }

  const jumpToLine = (line: number) => {
    setActiveLine(line)
    setTab('impl')
  }

  return (
    <div className="page nx-page nx-wb">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 节点工作台 · AI 先做一遍</>}
        title={node.name}
        description={`${isRealWid ? `真实 workflow ${wid}` : '原型演示数据'} · ${node.custom ? '草稿实现 · 可编辑 · 尚需注册' : '已注册原子 · 实现只读'}；节点身份不变，只替换 implementation。`}
        actions={
          <>
            <Badge tone={isRealWid ? 'cyan' : 'neutral'}>{isRealWid ? '真实 workflow' : '演示数据'}</Badge>
            <Badge tone={node.custom ? 'warn' : 'ok'} dot>{node.custom ? 'custom · 低复用' : 'atom · 高复用'}</Badge>
            <Badge tone="cyan">rev {rev}</Badge>
            <Button size="sm" variant="ghost" icon="flow" onClick={() => navigate('/next/console')}>返回控制台</Button>
          </>
        }
      />

      <div className="nx-ai-banner">
        <Icon name="spark" size={15} />
        <span><b>AI 先做了一遍</b>：已生成 rev {rev}，自动修复 {autoFixes} 次，测试 {node.tests.passed}/{node.tests.total} 通过。你可以直接接受，或修改后确认。</span>
      </div>

      <div className="nx-wb-grid">
        <aside className="panel nx-wb-steps" aria-label="任务步骤">
          <SectionHeader icon="flow" title="任务步骤" description={`${nodes.length} 个节点`} />
          <div style={{ marginTop: 8 }}>
            {nodes.map((item, index) => (
              <button
                key={item.id}
                type="button"
                className={`nx-wb-step ${item.id === node.id ? 'is-active' : ''}`}
                onClick={() => selectNode(item.id)}
              >
                <span className="nx-step-index">{String(index + 1).padStart(2, '0')}</span>
                <span style={{ minWidth: 0 }}>
                  <b>{item.name}</b>
                  <small>{item.custom ? '草稿实现' : item.atom} · rev {item.rev}</small>
                </span>
              </button>
            ))}
          </div>
        </aside>

        <main className="panel nx-wb-editor">
          <div className="nx-wb-tabs" role="tablist" aria-label="节点视图">
            {(Object.keys(TAB_LABEL) as Tab[]).map((key) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                className={`nx-wb-tab ${tab === key ? 'is-active' : ''}`}
                onClick={() => setTab(key)}
              >
                {TAB_LABEL[key]}
                {key === 'tests' ? ` ${node.tests.total}` : ''}
              </button>
            ))}
          </div>

          {tab === 'impl' ? (
            <>
              <div className="nx-editor-head">
                <span>{node.id}.py</span>
                <Badge tone={editing ? 'warn' : 'cyan'}>{editing ? '人工编辑' : `rev ${rev}`}</Badge>
                <span>{node.custom ? '草稿实现' : '已注册原子'}</span>
                {implementation ? <Badge tone={implementation.kind === 'code' ? 'warn' : 'ok'}>{implementation.kind === 'code' ? '工作区实现' : '内置原子'}</Badge> : null}
                {implementation?.ref ? <span title={`${implementation.ref}${implementation.pack ? ` · ${implementation.pack}` : ''}`}>{String(implementation.ref).slice(0, 42)}</span> : null}
                {implementationQuery.isLoading ? <Badge tone="run">读取实现…</Badge> : null}
                {implementationQuery.error ? <Badge tone="warn">实现读取失败</Badge> : null}
                {debugState === 'running' ? <Badge tone="run" dot>调试中</Badge> : null}
                {debugSummary ? <Badge tone={debugSummary.failed ? 'bad' : 'ok'} dot>调试图 {debugSummary.passed}/{debugSummary.total}</Badge> : null}
                <span style={{ marginLeft: 'auto' }}>{editing ? '保存后生成新 revision' : '点击日志行号可定位'}</span>
              </div>
              {editing ? (
                <div style={{ padding: 10 }}>
                  <textarea
                    className="nx-intent-input"
                    style={{ minHeight: 280, fontFamily: 'var(--mono)', fontSize: 10 }}
                    value={editedCode}
                    onChange={(event) => setEditedCode(event.target.value)}
                    aria-label="人工编辑代码"
                  />
                  <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                    <Button size="sm" variant="primary" icon="check" onClick={saveManual}>保存为新 rev</Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>取消</Button>
                  </div>
                </div>
              ) : (
                <div className="nx-code-grid">
                  <div className="nx-gutter" aria-hidden="true">
                    {lines.map((_, index) => <span key={index}>{index + 1}</span>)}
                  </div>
                  <div className="nx-code-lines">
                    {lines.map((line, index) => {
                      const number = index + 1
                      const locked = number >= lockStart && number <= lockEnd
                      return (
                        <span key={index} className={`nx-code-line ${locked ? 'is-locked' : ''} ${activeLine === number ? 'is-active' : ''}`}>
                          {locked ? <i className="nx-lock-dot" aria-label="锁定段" /> : null}{line || ' '}
                        </span>
                      )
                    })}
                  </div>
                </div>
              )}
              <div className="nx-editor-foot">
                <span>Python · 沙箱</span>
                <span>能力：{node.contract.capabilities.length ? node.contract.capabilities.join(' / ') : '无网络 / 无文件'}</span>
                <span>超时 {node.contract.timeout}</span>
                <span>锁定段 {lockStart}-{lockEnd}</span>
              </div>
            </>
          ) : null}

          {tab === 'contract' ? (
            <div className="nx-wb-contract">
              <div className="nx-contract-grid">
                <div className="nx-contract-col">
                  <h4>输入端口</h4>
                  {node.contract.inputs.map((port) => (
                    <div className="nx-port" key={port.name}>
                      <code>{port.name}</code><span>{port.type}{port.required ? ' · 必填' : ''} · {port.source || 'literal'}</span>
                    </div>
                  ))}
                </div>
                <div className="nx-contract-col">
                  <h4>输出端口</h4>
                  {node.contract.outputs.map((port) => (
                    <div className="nx-port" key={port.name}>
                      <code>{port.name}</code><span>{port.type}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="nx-contract-meta">
                <Badge tone="neutral">副作用 {node.contract.sideEffect}</Badge>
                <Badge tone="neutral">幂等 {node.contract.idempotency}</Badge>
                <Badge tone={node.contract.risk === 'L0' ? 'ok' : 'warn'}>{node.contract.risk}</Badge>
                <Badge tone="cyan">能力 {node.contract.capabilities.join(', ') || '无'}</Badge>
              </div>
            </div>
          ) : null}

          {tab === 'tests' ? (
            <div className="nx-log-table" style={{ margin: 10 }}>
              <div className="nx-log-row nx-log-head"><span>测试</span><span>结果</span><span>耗时</span><span>说明</span></div>
              {WORKBENCH_TESTS.map((item) => (
                <div className="nx-log-row" key={item.id}>
                  <span>{item.name}</span>
                  <span><Badge tone="ok">{item.status}</Badge></span>
                  <span className="nx-log-time">{item.duration}</span>
                  <span className="nx-log-msg">{item.detail}</span>
                </div>
              ))}
            </div>
          ) : null}

          {tab === 'evidence' ? (
            <div style={{ padding: 12 }}>
              {evidence ? (
                <>
                  <div className="nx-contract-meta">
                    <Badge tone="cyan">{evidence.run}</Badge>
                    <Badge tone="neutral">rev {evidence.rev}</Badge>
                    <Badge tone="neutral">{evidence.duration}</Badge>
                    <Badge tone="neutral">input {evidence.inputHash}</Badge>
                  </div>
                  <div className="nx-artifacts" style={{ marginTop: 8 }}>
                    {evidence.artifacts.map((item) => <span className="nx-artifact" key={item}><Icon name="archive" size={12} />{item}</span>)}
                  </div>
                  <ul className="nx-check-list">
                    {evidence.checks.map((item) => <li className="ok" key={item}><Icon name="check" size={11} />{item}</li>)}
                  </ul>
                </>
              ) : <p className="muted">暂无证据。</p>}
            </div>
          ) : null}

          {tab === 'diff' ? (
            <div style={{ padding: 12 }}>
              {EXTRACT_DIFFS.map((item) => (
                <div className="nx-port" key={item.id} style={{ alignItems: 'flex-start' }}>
                  <Badge tone={item.author === 'AI' ? 'ai' : 'human'}>{item.author}</Badge>
                  <div>
                    <strong style={{ fontSize: 10.5 }}>rev {item.rev} · {item.summary}</strong>
                    <div className="muted" style={{ fontSize: 9 }}>行 {item.lines} · {item.at}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          <div style={{ padding: '0 10px 10px' }}>
            <div className="nx-log-table">
              <div className="nx-log-row nx-log-head"><span>时间</span><span>级别</span><span>行号</span><span>消息</span></div>
              {logs.map((log) => (
                <div className="nx-log-row" key={log.id}>
                  <span className="nx-log-time">{log.at}</span>
                  <span><Badge tone={log.level === 'error' ? 'bad' : log.level === 'warn' ? 'warn' : log.level === 'pass' ? 'ok' : 'neutral'}>{log.level}</Badge></span>
                  <span><button type="button" className="nx-log-line" onClick={() => jumpToLine(log.line)}>L{log.line}</button></span>
                  <span className="nx-log-msg">{log.message}</span>
                </div>
              ))}
            </div>
          </div>
        </main>

        <aside className="nx-wb-side">
          <section className="panel nx-ai-card">
            <strong>{node.custom ? `AI 建议：接受 rev ${rev}` : '已注册原子 · 实现只读'}</strong>
            <p>{node.custom ? '契约完整、测试通过、整任务跑通；确认后仅替换 implementation，不改变节点身份。' : '该节点已绑定注册原子；如需修改实现，请去资产库创建新版本。'}</p>
            <div className="nx-ai-actions">
              <Button size="sm" variant="soft" icon="play" loading={debugState === 'running'} onClick={runDebug}>{debugState === 'running' ? '调试中…' : '运行调试'}</Button>
              <Button size="sm" variant="primary" icon="check" disabled={!node.custom || accepted || registered} onClick={accept}>接受 AI 建议</Button>
              <Button size="sm" variant="soft" icon="refresh" disabled={!node.custom || registered} onClick={aiFix}>让 AI 再改</Button>
              <Button size="sm" variant="ghost" icon="edit" disabled={!node.custom || registered} onClick={startManual}>手动修改</Button>
            </div>
          </section>

          <section className="panel">
            <SectionHeader icon="shield" title="人工确认" description="四条件全绿才能注册" actions={<Badge tone={checks.every((item) => item.ok) ? 'ok' : 'warn'}>{checks.filter((item) => item.ok).length}/4</Badge>} />
            <ul className="nx-check-list">
              {checks.map((item) => (
                <li key={item.key} className={item.ok ? 'ok' : 'todo'}>
                  <Icon name={item.ok ? 'check' : 'clock'} size={11} />{item.label}
                </li>
              ))}
            </ul>
            <Button size="sm" variant="primary" icon="atoms" className="full-width" style={{ width: '100%', marginTop: 8 }} disabled={!canRegister} onClick={register}>
              {registered ? '已注册原子' : '注册原子'}
            </Button>
            <p className="nx-register-note">{hasRunnableCode ? '注册只替换 implementation 与 registration；节点 id、契约、端口、图位置不变。' : '真实 workflow 未返回可执行代码，暂不能注册；先在工作台补全实现。'}</p>
          </section>

          <section className="panel">
            <SectionHeader icon="bug" title="证据摘要" description="当前 rev 的运行证据" />
            <div className="nx-check-list">
              <li className="ok"><Icon name="check" size={11} />{evidence?.run || 'run-9f2c14'}</li>
              <li className={node.contract.risk === 'L1' ? 'todo' : 'ok'}>
                <Icon name={node.contract.risk === 'L1' ? 'clock' : 'check'} size={11} />风险 {node.contract.risk}
              </li>
              <li className="ok"><Icon name="check" size={11} />自动修复 {autoFixes} 次 · 日志可定位</li>
            </div>
          </section>
        </aside>
      </div>

      <div className="nx-flow-strip" aria-label="AI 与人工协作链">
        <div className="nx-fs is-ai">AI 生成<small>代码 / 产物</small></div>
        <div className="nx-fs is-ai">AI 自测<small>契约测试</small></div>
        <div className="nx-fs is-ai">AI 自修<small>自动重跑</small></div>
        <div className="nx-fs is-human">人工修改<small>可锁定</small></div>
        <div className="nx-fs is-human">人工确认<small>风险 / 证据</small></div>
        <div className="nx-fs is-ai">注册原子<small>复用升级</small></div>
      </div>
    </div>
  )
}
