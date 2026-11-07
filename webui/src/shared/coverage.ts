import type { CoverageStat, ValidationIssue } from './types'

export type CoverageGate = 'advisory' | 'blocking'
export type CoverageTone = 'ok' | 'warn' | 'bad'

export const COVERAGE_ADVISORY_NOTE = '覆盖率仅参考，探索流程以重放结果为准'

/**
 * 覆盖率门禁以服务端 `coverage.gate` 为准；旧服务没有 gate 时按流程来源兜底。
 * 探索固化流程来自真实执行轨迹，覆盖率不足以阻断发布。
 */
export function coverageGate(coverage?: CoverageStat | null, isExplore = false): CoverageGate {
  const gate = String(coverage?.gate || '').toLowerCase()
  if (gate === 'advisory' || gate === 'blocking') return gate
  return isExplore ? 'advisory' : 'blocking'
}

export function isCoverageAdvisory(coverage?: CoverageStat | null, isExplore = false): boolean {
  return coverageGate(coverage, isExplore) === 'advisory'
}

/** advisory 只提示、不标红；blocking 且覆盖率 < 60% 才保持红色阻断。 */
export function coverageTone(coverage?: CoverageStat | null, isExplore = false): CoverageTone {
  if (!coverage) return 'ok'
  if (isCoverageAdvisory(coverage, isExplore)) {
    return coverage.ratio < 0.6 ? 'warn' : 'ok'
  }
  return coverage.ratio < 0.6 ? 'bad' : 'ok'
}

/**
 * 探索流程的覆盖率 / 能力缺口错误由服务端降级为 advisory，前端不应再据此阻断
 * 提交审阅或发布；其他真实校验错误（如缺参数、缺环境变量）仍然阻断。
 */
export function blockingValidationErrors(
  errors: ValidationIssue[],
  coverage?: CoverageStat | null,
  isExplore = false,
): ValidationIssue[] {
  if (!isCoverageAdvisory(coverage, isExplore)) return errors
  const advisoryCodes = new Set(['intent_mismatch', 'intent_coverage_advisory', 'capability_gap'])
  return errors.filter((issue) => !advisoryCodes.has(String(issue.code || '')))
}
