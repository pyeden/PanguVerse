import type { Artifact } from './types'

export type ArtifactKind = 'report' | 'deliverable'

/** 平台运行报告固定名称；旧 manifest 没有 kind 时按名称回退。 */
export const REPORT_ARTIFACT_NAMES = ['report.html', 'report.json', 'junit.xml', 'plan.json'] as const
const REPORT_NAME_SET = new Set<string>(REPORT_ARTIFACT_NAMES)

export function artifactKind(artifact?: Pick<Artifact, 'kind' | 'name'> | null): ArtifactKind {
  const declared = String(artifact?.kind || '')
  if (declared === 'report' || declared === 'deliverable') return declared
  return REPORT_NAME_SET.has(String(artifact?.name || '')) ? 'report' : 'deliverable'
}

export function splitArtifacts<T extends Pick<Artifact, 'kind' | 'name'>>(items?: T[] | null) {
  const reports: T[] = []
  const deliverables: T[] = []
  for (const item of items || []) {
    if (artifactKind(item) === 'report') reports.push(item)
    else deliverables.push(item)
  }
  return { reports, deliverables }
}

/** 用一句话说明这个产出物是什么，避免两类分区只有文件名。 */
export function artifactHint(name?: string): string {
  switch (String(name || '')) {
    case 'report.html':
      return '可读运行报告'
    case 'report.json':
      return '结构化报告原始数据'
    case 'junit.xml':
      return 'JUnit / CI 测试结果'
    case 'plan.json':
      return '执行计划与节点步骤'
    case 'news_data':
      return '采集到的原始数据'
    case 'report_html':
      return '渲染后的任务报告'
    case 'report_md':
      return 'Markdown 版任务报告'
    default:
      return '任务交付文件'
  }
}
