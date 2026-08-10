/**
 * Coerces a raw value (a manual write, or a present-value snapshot read off
 * an external device) into whatever shape the simulator actually stores for
 * a given object type -- boolean for binary-*, an in-range integer state
 * for multi-state-*, a float for everything else. Falls back to a safe
 * per-type default (false / state 1 / 0) when no usable value is available
 * at all, rather than propagating undefined/NaN into the simulator.
 *
 * Single source of truth for this coercion -- used both by the Set Value
 * modal (ObjectsPanel.vue) and by Create Simulated Copy's present-value
 * snapshot (deviceCopy.ts), which previously would have needed two
 * separately-maintained, easily-diverging copies of the same logic.
 */
export function coerceValueForObjectType(
  objectType: string,
  value: unknown,
  numberOfStates?: number,
): number | boolean {
  if (objectType.startsWith('binary')) {
    if (typeof value === 'boolean') return value
    if (value === undefined || value === null) return false
    const n = Number(value)
    return !Number.isNaN(n) && n >= 0.5
  }

  if (objectType.startsWith('multi-state')) {
    const max = numberOfStates && numberOfStates > 0 ? numberOfStates : 254
    if (value === undefined || value === null) return 1
    const n = Math.round(Number(value))
    if (Number.isNaN(n)) return 1
    return Math.min(Math.max(n, 1), max)
  }

  if (value === undefined || value === null) return 0
  const n = Number(value)
  return Number.isNaN(n) ? 0 : n
}
