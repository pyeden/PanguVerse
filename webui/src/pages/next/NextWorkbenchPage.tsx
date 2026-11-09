import { useMemo, useState } from 'react'
import { navigate, useRoute } from '../../shared/router'
import {
  EVIDENCE_BY_NODE,
  EXTRACT_DIFFS,
  WORKBENCH_NODES,
  WORKBENCH_TESTS,
  codeWithHeader,
  type NxNodeMeta,
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

export function NextWorkbenchPage({ wid = 'wf-console', nodeId = 'node-extract-prices' }: { wid?: string; nodeId?: string }) {
  const route = useRoute()
  const { push } = useToast()
  const initialNode = WORKBENCH_NODES.find((item) => item.id === nodeId) || WORKBENCH_NODES[1]
  const [selectedId, setSelectedId] = useState(initialNode.id)
  const node = WORKBENCH_NODES.find((item) => item.id === selectedId) || initialNode
  const [tab, setTab] = useState<Tab>('impl')
  const [rev, setRev] = useState(node.rev)
  const [autoFixes, setAutoFixes] = useState(node.autoFixes)
  const [accepted, setAccepted] = useState(false)
  const [registered, setRegistered] = useState(!node.custom)
  const [editing, setEditing] = useState(false)
  const [editedCode, setEditedCode] = useState('')
  const [logs, setLogs] = useState(node.logs)
  const [activeLine, setActiveLine] = useState<number>(() => {
    const value = Number(route.query.get('line') || '')
    return Number.isFinite(value) && value > 0 ? value : 0
  })

  const code = editing ? editedCode : codeWithHeader(node.code, node.name, rev, autoFixes)
  const lines = useMemo(() => code.split('\n'), [code])
  const [lockStart, lockEnd] = useMemo(() => lockRange(node), [node])
  const evidence = EVIDENCE_BY_NODE[node.id]
  const checks = [
    { key: 'contract', label: '契约完整', ok: node.contract.inputs.length > 0 && node.contract.outputs.length > 0 },
    { key: 'tests', label: `契约测试 ${node.tests.passed}/${node.tests.total}`, ok: node.tests.passed === node.tests.total },
    { key: 'task', label: '整任务跑通', ok: true },
    { key: 'human', label: '人工确认风险', ok: accepted },
  ]
  const canRegister = node.custom && !registered && checks.every((item) => item.ok)

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

  const register = () => {
    if (!canRegister) return
    setRegistered(true)
    push('已注册原子：implementation 替换为 atom 引用，节点身份不变', 'ok')
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
        description={`${node.custom ? '草稿实现 · 可编辑 · 尚需注册' : '已注册原子 · 实现只读'}；节点身份不变，只替换 implementation。`}
        actions={
          <>
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
          <SectionHeader icon="flow" title="任务步骤" description={`${WORKBENCH_NODES.length} 个节点`} />
          <div style={{ marginTop: 8 }}>
            {WORKBENCH_NODES.map((item, index) => (
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
            <p className="nx-register-note">注册只替换 implementation 与 registration；节点 id、契约、端口、图位置不变。</p>
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
