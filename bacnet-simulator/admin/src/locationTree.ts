import type { Location } from './types'

export interface LocationTreeNode {
  title: string
  value: number
  key: string
  children?: LocationTreeNode[]
}

export function buildLocationTreeOptions(locations: Location[], excludeSubtreeOf?: number | null): LocationTreeNode[] {
  const excluded = new Set<number>()
  if (excludeSubtreeOf != null) {
    const collect = (id: number) => {
      excluded.add(id)
      for (const l of locations) if (l.parent_location_id === id) collect(l.id)
    }
    collect(excludeSubtreeOf)
  }
  const byParent = new Map<number | null, Location[]>()
  for (const l of locations) {
    if (excluded.has(l.id)) continue
    const list = byParent.get(l.parent_location_id) ?? []
    list.push(l)
    byParent.set(l.parent_location_id, list)
  }
  const build = (parentId: number | null): LocationTreeNode[] =>
    (byParent.get(parentId) ?? []).map((l) => ({
      title: l.name,
      value: l.id,
      key: String(l.id),
      children: build(l.id),
    }))
  return build(null)
}

export interface FlattenedLocation { id: number; label: string; depth: number }

export function flattenLocationTree(nodes: LocationTreeNode[], out: FlattenedLocation[] = [], depth = 0): FlattenedLocation[] {
  for (const n of nodes) {
    out.push({ id: n.value, label: n.title, depth })
    if (n.children && n.children.length) flattenLocationTree(n.children, out, depth + 1)
  }
  return out
}
