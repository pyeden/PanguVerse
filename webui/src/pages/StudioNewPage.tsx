import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, api, isStudioMock, onStudioMockChange, studioApi } from '../shared/api'
import { navigate, href } from '../shared/router'
import { relativeTime, statusLabel } from '../shared/utils'
import type { AnyRecord, Atom, Pack, Workflow } from '../shared/types'
import { Icon } from '../components/Icon'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  SectionHeader,
  StatusBadge,
  useToast,
} from '../components/ui'

const AUTO_PACK = 'auto'

const quickPrompts: Record<string, { title: string; text: string }[]> = {
  auto: [
    { title: '百度热搜', text: '打开百度网站，提取今天的热点新闻' },
    { title: '健康检查', text: '调用健康检查接口，确认返回 200，并断言响应体包含 status=ok。' },
    { title: '配置巡检', text: '检查配置目录中的必要文件是否存在，并读取 JSON 校验 version 字段。' },
  ],
  api_testing: [
    { title: '登录与订单', text: '登录系统，获取访问令牌，拉取订单列表，并确认返回中至少包含一条订单。' },
    { title: '健康检查', text: '调用健康检查接口，确认返回 200，并断言响应体包含 status=ok。' },
    { title: '鉴权边界', text: '使用无效令牌访问受保护接口，确认返回 401 且错误码为 unauthorized。' },
  ],
  local_fs: [
    { title: '订单归档', text: '读取待处理订单清单，把每条订单写入 archive 目录，并确认归档文件存在。' },
    { title: '配置巡检', text: '检查配置目录中的必要文件是否存在，并读取 JSON 校验 version 字段。' },
    { title: '清理临时文件', text: '列出临时目录文件，删除超过保留期的文件，并记录删除数量。' },
  ],
  web_research: [
    { title: '首页布局报告', text: '打开 https://seres-ai.com 首页，提取正文文本，并给出布局与结构报告。' },
    { title: '文章要点总结', text: '抓取指定文章页面，提取正文并总结三条要点。' },
    { title: '链接结构盘点', text: '抓取页面 HTML，统计标题层级、链接与区块结构。' },
  ],
}

const missingInputLabels: Record<string, string> = {
  url: '目标网页 URL',
}

export function StudioNewPage() {
  const { push } = useToast()
  const [intent, setIntent] = useState('')
  const [pack, setPack] = useState(AUTO_PACK)
  const [excluded, setExcluded] = useState<string[]>([])
  const [mock, setMock] = useState(isStudioMock())
  const [planError, setPlanError] = useState<{
    code: string
    message: string
    missing: string[]
    missingInputs: string[]
    suggestions: string[]
  } | null>(null)
  const atomsQuery = useQuery({ queryKey: ['atoms'], queryFn: api.atoms, staleTime: 60_000 })
  const packsQuery = useQuery({ queryKey: ['packs'], queryFn: api.packs, staleTime: 60_000 })
  const draftsQuery = useQuery({ queryKey: ['studio-drafts'], queryFn: () => studioApi.listDrafts() })
  const atoms = atomsQuery.data || []
  const packs = packsQuery.data || []
  const drafts = draftsQuery.data || []

  useEffect(() => onStudioMockChange(setMock), [])

  useEffect(() => {
    if (!packs.length) return
    if (pack !== AUTO_PACK && !packs.some((item) => item.name === pack)) setPack(AUTO_PACK)
  }, [packs, pack])

  const recommended = useMemo(() => {
    const inPack = atoms.filter((atom) => atom.pack === pack)
    const pool = pack === AUTO_PACK
      ? atoms
      : inPack.length
        ? inPack
        : atoms.filter((atom) => pack === 'local_fs' ? atom.domain === 'fs' : atom.domain === 'api')
    const preferred = pack === AUTO_PACK
      ? ['http_get_page', 'html_extract_text', 'web_open', 'web_get_text', 'http_request', 'assert_json', 'fs_read_json']
      : pack === 'local_fs'
        ? ['fs_read_text', 'fs_write_json', 'fs_exists', 'assert_file_exists', 'fs_list']
        : pack === 'web_research'
          ? ['http_get_page', 'html_extract_text', 'html_extract_structure', 'llm_summarize']
          : ['http_request', 'assert_status', 'json_get', 'assert_body_contains', 'assert_json']
    return [...pool].sort((a, b) => {
      const ai = preferred.indexOf(a.name)
      const bi = preferred.indexOf(b.name)
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a.name.localeCompare(b.name)
    }).slice(0, 7)
  }, [atoms, pack])

  const selectedPack = packs.find((item) => item.name === pack)
  const activeRecommended = recommended.filter((atom) => !excluded.includes(atom.name))

  const create = useMutation({
    // 自动识别时不传 domain_pack，由后端按意图路由；只有用户显式选包才传。
    mutationFn: () => studioApi.createDraft({ intent: intent.trim(), domain_pack: pack === AUTO_PACK ? undefined : pack, exclude_atoms: excluded }),
    onSuccess: (workflow) => {
      setPlanError(null)
      push(`已生成 rev ${workflow.current_rev ?? 0} · ${workflow.graph?.nodes?.length || 0} 个节点`, 'ok')
      navigate(`/studio/${workflow.wid}`)
    },
    onError: (error: Error) => {
      if (error instanceof ApiError) {
        const raw = (error.data?.error && typeof error.data.error === 'object' ? error.data.error : error.data) as AnyRecord | undefined
        const missing = Array.isArray(raw?.missing_atoms) ? raw!.missing_atoms.map(String) : []
        const missingInputs = Array.isArray(raw?.missing_inputs) ? raw!.missing_inputs.map(String) : []
        const suggestions = Array.isArray(raw?.suggestions) ? raw!.suggestions.map(String) : []
        const code = String(raw?.code || error.code || '')
        if (
          error.status === 422 ||
          code === 'capability_gap' ||
          code === 'missing_inputs' ||
          missing.length ||
          missingInputs.length
        ) {
          setPlanError({ code, message: error.message, missing, missingInputs, suggestions })
          return
        }
      }
      push(error.message, 'error')
    },
  })

  const submit = () => {
    setPlanError(null)
    if (!intent.trim()) { push('先写一句要自动化的意图', 'error'); return }
    if (intent.trim().length < 6) { push('意图太短，补充目标与期望结果', 'error'); return }
    create.mutate()
  }

  return (
    <div className="page studio-new-page">
      <PageHeader
        eyebrow={<><span className="live-dot" /> 工作台 · 意图识别</>}
        title={<>一句话，生成<span className="text-gradient">可审阅的任务</span></>}
        description="AI 只负责提议：拆解意图、选择原子、生成初稿。你可以在画布上改、锁、调试，再逐条审阅后发布。"
        actions={<a className="btn btn-ghost btn-sm" href={href('/atoms')}><Icon name="atoms" size={15} />查看能力清单</a>}
      />

      {planError ? (
        <div className="capability-gap-panel" data-testid="plan-gap-panel">
          <span className="gap-panel-icon"><Icon name="warning" size={17} /></span>
          <div className="gap-panel-body">
            {planError.code === 'missing_inputs' ? (
              <strong>没有生成任务：缺少输入（422）</strong>
            ) : (
              <strong>没有生成任务：能力缺口（422 {planError.missing.length ? `· 缺少 ${planError.missing.length} 个原子` : ''}）</strong>
            )}
            <p>{planError.message}</p>
            {planError.missingInputs.length ? (
              <div className="gap-panel-row" data-testid="plan-gap-missing-inputs">
                <span>缺少输入</span>
                {planError.missingInputs.map((key) => <code key={key}>{missingInputLabels[key] || key}</code>)}
              </div>
            ) : null}
            {planError.missing.length ? (
              <div className="gap-panel-row"><span>缺失原子</span>{planError.missing.map((atom) => <code key={atom}>{atom}</code>)}</div>
            ) : null}
            {planError.suggestions.length ? (
              <ul className="gap-panel-suggestions">{planError.suggestions.map((item) => <li key={item}>{item}</li>)}</ul>
            ) : null}
            <small>{planError.code === 'missing_inputs' ? '补齐输入后重新生成即可，原子能力已经具备。' : '不会产出占位图。请调整意图、补齐原子，或更换领域包。'}</small>
          </div>
          <button type="button" className="gap-panel-close" onClick={() => setPlanError(null)} aria-label="关闭能力缺口提示"><Icon name="close" size={13} /></button>
        </div>
      ) : null}

      {mock ? (
        <div className="demo-banner">
          <span className="demo-icon"><Icon name="terminal" size={16} /></span>
          <div><strong>Studio 演示模式</strong><p><code>/api/studio/*</code> 尚未就绪，当前任务保存在浏览器本地，用来验证交互与门禁。后端就绪后刷新页面会自动切回真实接口。</p></div>
          <Badge tone="warn">未联调</Badge>
        </div>
      ) : null}

      <div className="studio-compose">
        <section className="intent-panel panel">
          <div className="intent-head">
            <span className="intent-step">01</span>
            <div><h2>描述要自动化的事</h2><p>说清目标、前置条件和成功标准。不要写具体代码。</p></div>
          </div>
          <div className="intent-box">
            <textarea
              value={intent}
              onChange={(event) => setIntent(event.target.value)}
              placeholder="例如：登录系统，获取访问令牌，拉取订单列表，并确认返回中至少包含一条订单。"
              rows={5}
              maxLength={1200}
              autoFocus
            />
            <div className="intent-box-foot">
              <span className="intent-ai"><Icon name="spark" size={14} /> AI 将拆解为目标 → 原子 → 依赖边</span>
              <span>{intent.length} / 1200</span>
            </div>
          </div>

          <div className="intent-section">
            <div className="intent-section-head"><strong>领域包</strong><span>决定 AI 的能力边界与意图模板</span></div>
            <div className="pack-choice-grid">
              {packsQuery.isLoading ? <Loading label="读取领域包…" /> : null}
              <button key={AUTO_PACK} type="button" className={`pack-choice ${pack === AUTO_PACK ? 'is-active' : ''}`} onClick={() => { setPack(AUTO_PACK); setExcluded([]) }}>
                <span className="pack-choice-icon"><Icon name="spark" size={19} /></span>
                <span className="pack-choice-copy"><strong>自动识别（推荐）</strong><small>按意图自动选择领域，不强迫套用固定包</small></span>
                <span className="pack-choice-count">全能力</span>
                {pack === AUTO_PACK ? <span className="pack-choice-check"><Icon name="check" size={12} /></span> : null}
              </button>
              {packs.map((item) => (
                <button key={item.name} type="button" className={`pack-choice ${pack === item.name ? 'is-active' : ''}`} onClick={() => { setPack(item.name); setExcluded([]) }}>
                  <span className="pack-choice-icon"><Icon name={item.name.includes('fs') ? 'file' : 'server'} size={19} /></span>
                  <span className="pack-choice-copy"><strong>{item.name}</strong><small>{item.description || item.domain || '领域包'}</small></span>
                  <span className="pack-choice-count">{item.atom_count ?? item.atoms?.length ?? 0} 原子</span>
                  {pack === item.name ? <span className="pack-choice-check"><Icon name="check" size={12} /></span> : null}
                </button>
              ))}
            </div>
          </div>

          <div className="intent-section">
            <div className="intent-section-head"><strong>快捷意图</strong><span>点击填入，可继续修改</span></div>
            <div className="prompt-chips">
              {(quickPrompts[pack] || quickPrompts.api_testing).map((prompt) => (
                <button type="button" key={prompt.title} onClick={() => setIntent(prompt.text)}>
                  <Icon name="bolt" size={13} /><span>{prompt.title}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="intent-submit">
            <div className="intent-submit-note">
              <Icon name="shield" size={15} />
              <span>调试只使用项目已配置环境；发布前必须经过审阅与风险确认。</span>
            </div>
            <Button variant="primary" size="lg" icon="spark" loading={create.isPending} onClick={submit} disabled={!intent.trim()}>
              {create.isPending ? '正在生成初稿…' : '生成任务初稿'}
            </Button>
          </div>
        </section>

        <aside className="preselect-panel panel">
          <div className="preselect-head">
            <div><span className="preselect-kicker"><Icon name="spark" size={13} /> AI 预选</span><h2>建议原子组合</h2></div>
            <Badge tone="cyan">{activeRecommended.length} 个</Badge>
          </div>
          <p className="preselect-desc">{pack === AUTO_PACK ? '按意图自动识别领域；不强迫套用任何领域包。' : selectedPack ? selectedPack.description : '根据领域包推荐可用原子。'}</p>
          <div className="preselect-list">
            {atomsQuery.isLoading ? <Loading label="读取原子…" /> : null}
            {activeRecommended.map((atom, index) => (
              <div className="preselect-item" key={atom.name}>
                <span className="preselect-index">{String(index + 1).padStart(2, '0')}</span>
                <div className="preselect-copy">
                  <strong>{atom.name}</strong>
                  <span>{atom.description || atom.category}</span>
                  <div className="preselect-tags">
                    {(atom.effects || ['pure']).map((effect) => <Badge key={effect} tone={effect === 'destructive' ? 'bad' : effect === 'write' || effect === 'mutate' ? 'warn' : 'neutral'}>{effect}</Badge>)}
                  </div>
                </div>
                <button type="button" className="exclude-btn" onClick={() => setExcluded((current) => current.includes(atom.name) ? current.filter((name) => name !== atom.name) : [...current, atom.name])} title={excluded.includes(atom.name) ? '恢复' : '排除'}>
                  {excluded.includes(atom.name) ? <Icon name="refresh" size={13} /> : <Icon name="close" size={13} />}
                </button>
              </div>
            ))}
          </div>
          {excluded.length ? (
            <div className="excluded-box">
              <span>已排除 {excluded.length} 个原子</span>
              <button type="button" onClick={() => setExcluded([])}>全部恢复</button>
            </div>
          ) : null}
          <div className="preselect-note">
            <Icon name="lock" size={14} />
            <p>生成后锁定的节点不会被 AI 重规划覆盖。排除名单会随请求提交。</p>
          </div>
        </aside>
      </div>

      <section className="draft-section">
        <SectionHeader
          title="最近任务"
          description="草案、待审、已发布都保留在本地 Studio 目录；新版本不覆盖旧 rev。"
          icon="history"
          actions={<Button size="sm" variant="ghost" icon="refresh" onClick={() => draftsQuery.refetch()}>刷新</Button>}
        />
        {draftsQuery.isLoading ? <Loading label="读取任务…" /> : null}
        {draftsQuery.error ? <ErrorState error={draftsQuery.error} onRetry={() => draftsQuery.refetch()} /> : null}
        {!draftsQuery.isLoading && !draftsQuery.error && !drafts.length ? (
          <EmptyState icon="flow" title="还没有任务">填写上面的意图，生成第一个可审阅初稿。</EmptyState>
        ) : (
          <div className="draft-grid">
            {drafts.map((workflow) => <DraftCard key={workflow.wid} workflow={workflow} />)}
          </div>
        )}
      </section>

      <div className="studio-principles">
        <div><span>01</span><strong>AI 提议</strong><p>初稿带 AI 徽标；每次重规划只产 diff，不自动应用。</p></div>
        <div><span>02</span><strong>人工裁决</strong><p>锁节点、改参数、逐条接受；锁定优先于任何自动改写。</p></div>
        <div><span>03</span><strong>门禁发布</strong><p>批准 + 风险勾选 + 非空版本说明，缺一不可。</p></div>
      </div>
    </div>
  )
}

function DraftCard({ workflow }: { workflow: Workflow }) {
  const state = workflow.published_ref ? 'published' : (workflow.state || 'draft')
  return (
    <a className={`draft-card state-${state}`} href={href(`/studio/${workflow.wid}`)}>
      <div className="draft-card-head">
        <span className={`draft-icon tone-${state === 'published' ? 'ok' : state === 'approved' ? 'ok' : state === 'review' ? 'warn' : 'neutral'}`}><Icon name="flow" size={18} /></span>
        <StatusBadge status={state} />
        <span className="draft-rev mono">rev {workflow.current_rev ?? 0}</span>
      </div>
      <strong className="draft-name">{workflow.name || workflow.intent || workflow.wid}</strong>
      <p className="draft-intent">{workflow.intent || '未记录意图'}</p>
      <div className="draft-meta">
        <span>{workflow.graph?.nodes?.length || workflow.node_count || 0} 节点</span>
        <span>{workflow.graph?.edges?.length || 0} 边</span>
        <span>{workflow.domain_pack || '未标注包'}</span>
      </div>
      <div className="draft-foot">
        <span>{relativeTime(workflow.updated_at || workflow.created_at)}</span>
        <span>{workflow.published_ref ? <Badge tone="ai">{workflow.published_ref}</Badge> : '打开画布'} <Icon name="arrow" size={13} /></span>
      </div>
    </a>
  )
}
