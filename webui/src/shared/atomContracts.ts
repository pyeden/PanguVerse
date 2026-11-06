import type {
  AnyRecord,
  ExploreAtomCreated,
  ExplorePendingAtom,
  StudioNode,
} from './types'

/**
 * 探索固化结果里的 atoms_created 是对象数组，而不是原子名字符串数组。
 * 这里集中做运行时归一化，保证任何历史/异常形状都不会把对象塞进 React child。
 */
export const NEW_ATOM_CACHE_PREFIX = 'panguverse.studio.newAtoms.'

export function recordOf(value: unknown): AnyRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as AnyRecord : null
}

export function textOf(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return String(value)
  }
  return ''
}

function normalizeAtom(value: unknown, index: number): ExploreAtomCreated {
  if (typeof value === 'string') return { name: value }
  const record = recordOf(value) || {}
  const name = textOf(record.name)
    || textOf(record.atom)
    || textOf(record.path)
    || `atom-${index + 1}`
  return { ...record, name }
}

/** atoms_created 可能是对象数组、字符串数组或异常值；统一成契约对象。 */
export function atomContracts(value: unknown): ExploreAtomCreated[] {
  if (!Array.isArray(value)) return []
  return value.map((item, index) => normalizeAtom(item, index))
}

function normalizePending(value: unknown, index: number): ExplorePendingAtom {
  if (typeof value === 'string') return { name: value, status: 'pending' }
  const record = recordOf(value) || {}
  return {
    ...record,
    name: textOf(record.name) || textOf(record.atom) || `pending-${index + 1}`,
    status: textOf(record.status) || 'pending',
  }
}

/** pending_atoms 是对象数组（name/path/reason/status），绝不能直接当 React child。 */
export function pendingAtoms(value: unknown): ExplorePendingAtom[] {
  if (!Array.isArray(value)) return []
  return value.map((item, index) => normalizePending(item, index))
}

/** 展示契约字段：字符串原样展示，对象/数组转 JSON，空值显示“无”。 */
export function contractText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '无'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return String(value)
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return '无法序列化'
  }
}

export function effectsText(value: unknown): string {
  if (!Array.isArray(value) || !value.length) return '无声明'
  return value.map((item) => textOf(item) || contractText(item)).filter(Boolean).join(' / ')
}

export function isNewAtomNode(node?: StudioNode | null): boolean {
  if (!node) return false
  if (node.new_atom === true) return true
  if (recordOf(node.extra)?.new_atom === true) return true
  return Array.isArray(node.tags) && node.tags.includes('new_atom')
}

export interface NewAtomCacheEntry {
  rev?: number
  saved_at?: number
  atoms: ExploreAtomCreated[]
}

export function readNewAtomCache(wid: string): Record<string, ExploreAtomCreated> {
  if (!wid) return {}
  try {
    const raw = localStorage.getItem(NEW_ATOM_CACHE_PREFIX + wid)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as AnyRecord
    const atoms = atomContracts(parsed?.atoms)
    const map: Record<string, ExploreAtomCreated> = {}
    atoms.forEach((atom) => { map[atom.name] = atom })
    return map
  } catch {
    return {}
  }
}

/**
 * 固化接口返回的 atoms_created 含 output_paths / code_sha256，
 * 但 /api/atoms 与 /api/packs 的契约字段不含这两个值。
 * 在同一次固化 → 画布的跳转中把完整契约缓存到本地，作为画布契约面板的首选数据源。
 */
export function writeNewAtomCache(wid: string, rev: number | undefined, atoms: ExploreAtomCreated[]): void {
  if (!wid || !atoms.length) return
  try {
    localStorage.setItem(NEW_ATOM_CACHE_PREFIX + wid, JSON.stringify({
      rev: rev ?? 0,
      saved_at: Date.now(),
      atoms,
    } satisfies NewAtomCacheEntry))
  } catch {
    /* 本地缓存失败不应阻断固化结果展示 */
  }
}
