import { studioApi } from './api'
import type { NxPlanStep } from './nextMock'
import type { StudioNode } from './types'

/** 把真实 studio 草稿映射成控制台计划步骤。 */
export interface NextPlanGap {
  kind: 'input' | 'atom' | 'capability'
  title: string
  detail: string
  suggestion: string
}

export async function planIntent(intent: string): Promise<{ wid: string; steps: NxPlanStep[]; gaps: NextPlanGap[]; planner?: string; source?: string }> {
  let workflow
  try {
    workflow = await studioApi.createDraft({ intent, allow_llm: true })
  } catch (error) {
    const raw = (error as { data?: { error?: Record<string, unknown> } }).data?.error || {}
    const missingInputs = Array.isArray(raw.missing_inputs) ? raw.missing_inputs.map(String) : []
    const missingAtoms = Array.isArray(raw.missing_atoms) ? raw.missing_atoms.map(String) : []
    const suggestions = Array.isArray(raw.suggestions) ? raw.suggestions.map(String) : []
    const gaps: NextPlanGap[] = [
      ...missingInputs.map((item, index) => ({
        kind: 'input' as const,
        title: `缺少输入 · ${item}`,
        detail: String(raw.message || 'AI 无法继续规划，需要补充输入。'),
        suggestion: suggestions[index] || `请提供 ${item}`,
      })),
      ...missingAtoms.map((item) => ({
        kind: 'atom' as const,
        title: `缺少原子 · ${item}`,
        detail: '当前原子库没有可直接使用的实现。',
        suggestion: '可以插入自定义节点，先调试再注册原子。',
      })),
    ]
    if (gaps.length) return { wid: '', steps: [], gaps }
    throw error
  }
  const nodes: StudioNode[] = workflow.graph?.nodes || workflow.nodes || []
  const steps = nodes.map((node, index) => {
    const atom = String(node.atom || '')
    const effects = Array.isArray(node.effects) ? node.effects.map(String) : []
    const risk: NxPlanStep['risk'] = effects.includes('destructive')
      ? 'L2'
      : effects.some((effect) => ['write', 'mutate', 'external'].includes(effect))
        ? 'L1'
        : 'L0'
    const custom = !atom || atom.includes('custom')
    return {
      id: node.node_id || `step-${index + 1}`,
      title: node.name || atom || `步骤 ${index + 1}`,
      atom: atom || 'custom',
      kind: custom ? 'custom' : 'atom',
      risk,
      status: 'queued',
      note: custom ? '真实草稿实现 · 待调试' : `真实原子 · ${atom}`,
    } satisfies NxPlanStep
  })
  if (!steps.length) throw new Error('AI 没有生成任何步骤')
  return { wid: workflow.wid, steps, gaps: [], planner: workflow.planner, source: workflow.plan_source }
}
