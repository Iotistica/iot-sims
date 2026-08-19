/** Shared PointRef -> PointRow lookup, built once per builder/run-dialog
 * session from a single GET /points fetch -- so canvas summary rendering
 * (FunctionalTestNode.vue) never needs its own fetch, just this lookup. */
import type { PointRef, PointRow } from './types'

export type PointLookup = Map<string, PointRow>

export function pointRefKey(ref: PointRef | null | undefined): string {
  if (!ref) return ''
  return `${ref.device_id}:${ref.object_id}`
}

export function buildPointLookup(rows: PointRow[]): PointLookup {
  const map: PointLookup = new Map()
  for (const row of rows) {
    map.set(`${row.device_id}:${row.object_id}`, row)
  }
  return map
}

export function lookupPoint(lookup: PointLookup, ref: PointRef | null | undefined): PointRow | undefined {
  if (!ref) return undefined
  return lookup.get(pointRefKey(ref))
}

/** "${device_name} / ${name}" -- the display label used everywhere a point
 * is referenced (PointPicker's collapsed value, FunctionalTestNode's
 * summary lines). Falls back to a clear "point was deleted" signal rather
 * than silently showing nothing when a ref doesn't resolve. */
export function pointDisplayLabel(row: PointRow | undefined): string {
  if (!row) return 'Unknown point'
  return `${row.device_name} / ${row.name}`
}
