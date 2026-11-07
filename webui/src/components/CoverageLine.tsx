import type { CoverageStat } from '../shared/types'
import { COVERAGE_ADVISORY_NOTE, coverageGate, coverageTone } from '../shared/coverage'

interface CoverageLineProps {
  coverage: CoverageStat
  isExplore?: boolean
  testId?: string
}

/**
 * 语义覆盖率统一展示：advisory 用 warn（未达标）或 ok，不标红；
 * 未匹配节点原因放在可折叠详情里，避免只看到一个比例。
 */
export function CoverageLine({ coverage, isExplore = false, testId = 'coverage-line' }: CoverageLineProps) {
  const gate = coverageGate(coverage, isExplore)
  const tone = coverageTone(coverage, isExplore)
  const details = coverage.unmatched_details || []
  return (
    <div className={`coverage-line ${tone}`} data-coverage-gate={gate} data-testid={testId}>
      <div className="coverage-line-main">
        <span>
          意图覆盖率
          {gate === 'advisory' ? <em className="coverage-gate-tag">参考</em> : null}
        </span>
        <strong className="mono">{coverage.matched}/{coverage.total} = {coverage.ratio.toFixed(2)}</strong>
      </div>
      {gate === 'advisory' ? (
        <p className="coverage-advisory" data-testid="coverage-advisory" data-coverage-gate="advisory">
          {COVERAGE_ADVISORY_NOTE}
        </p>
      ) : null}
      {details.length ? (
        <details className="coverage-unmatched" data-testid="coverage-unmatched-details">
          <summary>未匹配节点 {details.length} 个 · 查看原因</summary>
          <ul>
            {details.map((detail, index) => (
              <li key={`${detail.node_id || 'node'}-${index}`}>
                <div className="coverage-unmatched-head">
                  <code>{detail.node_id || `未命名节点 ${index + 1}`}</code>
                  {detail.atom ? <span className="coverage-unmatched-atom">{detail.atom}</span> : null}
                  {detail.new_atom ? <span className="coverage-unmatched-new">新增原子</span> : null}
                </div>
                <small>{detail.reason || '未匹配到意图动作或实体'}</small>
                {detail.role ? <small className="coverage-unmatched-role">角色：{detail.role}</small> : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}
