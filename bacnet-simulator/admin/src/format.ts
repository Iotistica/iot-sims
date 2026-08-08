/**
 * Single source of truth for rendering a BACnet Present_Value in the UI.
 *
 * Present_Value for binary-input/output/value is BACnetBinaryPV (an
 * ENUMERATED{inactive,active} per ASHRAE 135's object type tables), not a
 * float or general integer -- the backend now always sends a plain bool
 * for these three object types (see normalize_present_value() in
 * src/legacy.py), but this still tolerates a stray number defensively
 * (e.g. history/chart data, which is always numeric regardless of object
 * type) so a binary point never renders as a bare "0.00"/"1.00" no matter
 * where its value came from.
 *
 * Used by every place a present value is displayed or sorted: the main
 * object table, the history chart, and ObjectDrawer's priority-array
 * panel -- previously three separate, disagreeing implementations.
 */
export function formatPresentValue(objectType: string, value: unknown): string {
  if (value === null || value === undefined) return '—'

  if (objectType.startsWith('binary')) {
    if (typeof value === 'boolean') return value ? 'ON' : 'OFF'
    const n = Number(value)
    if (!Number.isNaN(n)) return n >= 0.5 ? 'ON' : 'OFF'
    return String(value)
  }

  if (typeof value === 'boolean') return value ? 'ON' : 'OFF'
  const n = Number(value)
  return Number.isNaN(n) ? String(value) : n.toFixed(2)
}
