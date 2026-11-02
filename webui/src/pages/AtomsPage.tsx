import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api'
import { copyText } from '../shared/utils'
import type { Atom, Pack } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  JsonBlock,
  Loading,
  Modal,
  PageHeader,
  SearchInput,
  SectionHeader,
  Select,
  useToast,
} from '../components/ui'

const effectLabels: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' | 'cyan' }> = {
  pure: { label: '纯函数', tone: 'neutral' },
  read: { label: '只读', tone: 'ok' },
  write: { label: '写入', tone: 'warn' },
  mutate: { label: '改变状态', tone: 'warn' },
  destructive: { label: '破坏性', tone: 'bad' },
  needs_key: { label: '需要密钥', tone: 'cyan' },
}

export function AtomsPage() {
  const { push } = useToast()
  const atomsQuery = useQuery({ queryKey: ['atoms'], queryFn: api.atoms, staleTime: 60_000 })
  const packsQuery = useQuery({ queryKey: ['packs'], queryFn: api.packs, staleTime: 60_000 })
  const [search, setSearch] = useState('')
  const [domain, setDomain] = useState('all')
  const [category, setCategory] = useState('all')
  const [tag, setTag] = useState('all')
  const [selected, setSelected] = useState<Atom | null>(null)
  const [packDetail, setPackDetail] = useState<Pack | null>(null)

  const atoms = atomsQuery.data || []
  const packs = packsQuery.data || []
  const domains = useMemo(() => Array.from(new Set(atoms.map((atom) => atom.domain).filter(Boolean))).sort(), [atoms])
  const categories = useMemo(() => Array.from(new Set(atoms.map((atom) => atom.category).filter(Boolean))).sort(), [atoms])
  const tags = useMemo(() => Array.from(new Set(atoms.flatMap((atom) => atom.tags || []))).sort(), [atoms])

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    return atoms.filter((atom) => {
      if (domain !== 'all' && atom.domain !== domain) return false
      if (category !== 'all' && atom.category !== category) return false
      if (tag !== 'all' && !(atom.tags || []).includes(tag)) return false
      if (query) {
        const haystack = `${atom.name} ${atom.description || ''} ${(atom.tags || []).join(' ')} ${(atom.aliases || []).join(' ')}`.toLowerCase()
        if (!haystack.includes(query)) return false
      }
      return true
    })
  }, [atoms, category, domain, search, tag])

  const reset = () => {
    setSearch('')
    setDomain('all')
    setCategory('all')
    setTag('all')
  }

  const activeFilters = [domain, category, tag].filter((value) => value !== 'all').length + (search ? 1 : 0)

  return (
    <div className="page atoms-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 六模块 · 料</>}
        title="原子库"
        description="原子是可编排的最小动作。每个原子暴露输入/输出端口、副作用与来源领域包；AI 只能从这里选择和组合。"
        actions={<Button variant="ghost" icon="layers" onClick={() => document.getElementById('packs-section')?.scrollIntoView({ behavior: 'smooth' })}>领域包 {packs.length}</Button>}
      />

      <div className="atom-summary">
        <div><span className="summary-icon"><Icon name="atoms" size={20} /></span><strong>{atoms.length}</strong><span>可用原子</span></div>
        <div><span className="summary-icon"><Icon name="layers" size={20} /></span><strong>{packs.length}</strong><span>领域包</span></div>
        <div><span className="summary-icon warn"><Icon name="bolt" size={20} /></span><strong>{atoms.filter((atom) => (atom.effects || []).some((effect) => ['write', 'mutate', 'destructive'].includes(effect))).length}</strong><span>带副作用</span></div>
        <div><span className="summary-icon"><Icon name="shield" size={20} /></span><strong>{atoms.filter((atom) => (atom.effects || []).some((effect) => ['needs_key', 'destructive'].includes(effect))).length}</strong><span>需审阅确认</span></div>
      </div>

      <div className="catalog-toolbar panel">
        <SearchInput value={search} onChange={setSearch} placeholder="搜索原子、别名或描述" />
        <Select value={domain} onChange={(event) => setDomain(event.target.value)} aria-label="按领域筛选">
          <option value="all">全部领域</option>
          {domains.map((value) => <option key={value} value={value}>{value}</option>)}
        </Select>
        <Select value={category} onChange={(event) => setCategory(event.target.value)} aria-label="按分类筛选">
          <option value="all">全部分类</option>
          {categories.map((value) => <option key={value} value={value}>{value}</option>)}
        </Select>
        <Select value={tag} onChange={(event) => setTag(event.target.value)} aria-label="按标签筛选">
          <option value="all">全部标签</option>
          {tags.map((value) => <option key={value} value={value}>{value}</option>)}
        </Select>
        {activeFilters ? <Button size="sm" variant="ghost" icon="close" onClick={reset}>清除 {activeFilters}</Button> : null}
        <span className="toolbar-spacer" />
        <span className="toolbar-count">{filtered.length} / {atoms.length}</span>
      </div>

      {atomsQuery.isLoading ? <Loading label="正在读取原子契约…" /> : null}
      {atomsQuery.error ? <ErrorState error={atomsQuery.error} onRetry={() => atomsQuery.refetch()} title="原子库读取失败" /> : null}

      {!atomsQuery.isLoading && !filtered.length ? (
        <EmptyState icon="atoms" title="没有匹配的原子" actions={<Button size="sm" onClick={reset}>清除筛选</Button>}>换一个关键词，或切换领域/分类。</EmptyState>
      ) : null}

      <div className="atom-grid">
        {filtered.map((atom) => (
          <button type="button" className="atom-card" key={atom.name} onClick={() => setSelected(atom)}>
            <div className="atom-card-head">
              <span className="atom-glyph"><Icon name={iconForCategory(atom.category)} size={19} /></span>
              <span className="atom-title"><strong>{atom.name}</strong><small>{atom.category || '未分类'} · {atom.domain || '通用'}</small></span>
              <Badge tone="neutral">v{atom.version || '0.1'}</Badge>
            </div>
            <p className="atom-desc">{atom.description || '未提供描述'}</p>
            <div className="atom-effects">
              {(atom.effects || ['pure']).map((effect) => {
                const meta = effectLabels[effect] || { label: effect, tone: 'neutral' as const }
                return <Badge key={effect} tone={meta.tone}>{meta.label}</Badge>
              })}
              {atom.pack ? <span className="atom-pack">{atom.pack}</span> : null}
            </div>
            <div className="atom-ports">
              <span><strong>{atom.input_ports?.length ?? Object.keys(atom.inputs || {}).length}</strong> 输入</span>
              <span><strong>{atom.output_ports?.length ?? Object.keys(atom.outputs || {}).length}</strong> 输出</span>
              <span><strong>{atom.required?.length || 0}</strong> 必填</span>
            </div>
            {(atom.required || []).length ? (
              <div className="atom-required">
                <span>必填</span>
                {(atom.required || []).slice(0, 4).map((item) => <code key={item}>{item}</code>)}
              </div>
            ) : null}
            <span className="atom-open">查看契约 <Icon name="arrow" size={13} /></span>
          </button>
        ))}
      </div>

      <section id="packs-section" className="panel packs-section">
        <SectionHeader title="领域包" description="领域包决定 AI 的能力边界与意图模板；切换包不改代码。" icon="layers" actions={<Badge tone="neutral">{packs.length} 个包</Badge>} />
        {packsQuery.isLoading ? <Loading label="正在读取领域包…" /> : null}
        {packsQuery.error ? <ErrorState error={packsQuery.error} onRetry={() => packsQuery.refetch()} /> : null}
        <div className="pack-strip">
          {packs.map((pack) => (
            <button type="button" className="pack-card" key={pack.name} onClick={() => setPackDetail(pack)}>
              <div className="pack-card-head"><strong>{pack.name}</strong><Badge tone={pack.builtin ? 'neutral' : 'warn'}>{pack.builtin ? '内置' : '工作区'}</Badge></div>
              <p>{pack.description || '未提供描述'}</p>
              <div className="pack-meta"><span>{pack.atom_count ?? pack.atoms?.length ?? '—'} 个原子</span><span>{pack.domain || '通用'}</span><span>v{pack.version || '—'}</span></div>
              <div className="pack-caps">{(pack.capabilities || []).slice(0, 3).map((cap) => <span key={cap}>{cap}</span>)}</div>
            </button>
          ))}
        </div>
      </section>

      <Modal
        open={!!selected}
        onClose={() => setSelected(null)}
        title={selected?.name}
        subtitle={selected ? `${selected.category || '未分类'} · ${selected.domain || '通用'} · v${selected.version || '—'}` : ''}
        size="lg"
        footer={<><Button variant="ghost" onClick={() => selected && copyText(selected.name).then(() => push('原子名已复制', 'ok'))}>复制名称</Button><Button onClick={() => setSelected(null)}>关闭</Button></>}
      >
        {selected ? <AtomDetail atom={selected} /> : null}
      </Modal>

      <Modal
        open={!!packDetail}
        onClose={() => setPackDetail(null)}
        title={packDetail?.name}
        subtitle={packDetail?.domain || '领域包'}
        size="lg"
        footer={<Button onClick={() => setPackDetail(null)}>关闭</Button>}
      >
        {packDetail ? (
          <div className="stack-lg">
            <p className="muted">{packDetail.description || '未提供描述'}</p>
            <div className="chip-row">
              <Badge tone="neutral">v{packDetail.version || '—'}</Badge>
              <Badge tone={packDetail.builtin ? 'neutral' : 'warn'}>{packDetail.builtin ? '内置' : '工作区'}</Badge>
              <Badge tone="cyan">{packDetail.atom_count ?? packDetail.atoms?.length ?? 0} 个原子</Badge>
            </div>
            <div className="field">
              <span className="field-label">能力</span>
              <div className="chip-row">{(packDetail.capabilities || []).map((cap) => <Badge key={cap}>{cap}</Badge>)}</div>
            </div>
            <div className="field">
              <span className="field-label">包含原子</span>
              <div className="chip-row">
                {(packDetail.atoms || []).map((atom) => {
                  const name = typeof atom === 'string' ? atom : atom.name
                  return <code key={name} className="chip-code">{name}</code>
                })}
              </div>
            </div>
            {packDetail.manifest ? <JsonBlock data={packDetail.manifest} /> : null}
          </div>
        ) : null}
      </Modal>
    </div>
  )
}

function AtomDetail({ atom }: { atom: Atom }) {
  const inputs = atom.input_ports || []
  const outputs = atom.output_ports || []
  return (
    <div className="stack-lg">
      <p className="atom-detail-desc">{atom.description || '未提供描述'}</p>
      <div className="chip-row">
        {(atom.effects || ['pure']).map((effect) => {
          const meta = effectLabels[effect] || { label: effect, tone: 'neutral' as const }
          return <Badge key={effect} tone={meta.tone}>{meta.label}</Badge>
        })}
        {atom.pack ? <Badge tone="cyan">来源 {atom.pack}</Badge> : null}
        {(atom.tags || []).map((tag) => <Badge key={tag} tone="neutral">#{tag}</Badge>)}
      </div>

      <div className="port-grid">
        <div>
          <h3 className="subsection-title">输入端口 <span>{inputs.length}</span></h3>
          {inputs.length ? inputs.map((port) => (
            <div className="port-row" key={port.name}>
              <span className="port-dot in" />
              <strong>{port.name}</strong>
              <code>{port.type || 'any'}</code>
              {port.required || atom.required?.includes(port.name) ? <Badge tone="bad">必填</Badge> : <Badge tone="neutral">可选</Badge>}
            </div>
          )) : <p className="muted">无输入端口</p>}
        </div>
        <div>
          <h3 className="subsection-title">输出端口 <span>{outputs.length}</span></h3>
          {outputs.length ? outputs.map((port) => (
            <div className="port-row" key={port.name}>
              <span className="port-dot out" />
              <strong>{port.name}</strong>
              <code>{port.type || 'any'}</code>
            </div>
          )) : <p className="muted">无输出端口</p>}
        </div>
      </div>

      {atom.aliases?.length ? <div className="field"><span className="field-label">别名</span><div className="chip-row">{atom.aliases.map((alias) => <code key={alias} className="chip-code">{alias}</code>)}</div></div> : null}
      <JsonBlock data={{ inputs: atom.inputs || {}, outputs: atom.outputs || {}, required: atom.required || [] }} />
    </div>
  )
}

function iconForCategory(category?: string): 'server' | 'check' | 'database' | 'bug' | 'file' | 'code' {
  const map: Record<string, 'server' | 'check' | 'database' | 'bug' | 'file' | 'code'> = {
    http: 'server',
    assert: 'check',
    extract: 'database',
    flow: 'bug',
    filesystem: 'file',
  }
  return map[category || ''] || 'code'
}
