/** Suggests the next unused BACnet device instance, scanning from 1001.
 * Shared by DeviceDrawer (Add/Edit Equipment) and CreateSimulatedCopyModal
 * so both suggest against the same, single collision check across every
 * loaded device (both source types). */
export function nextFreeInstance(existingInstances: number[]): number {
  const taken = new Set(existingInstances)
  let id = 1001
  while (taken.has(id)) id++
  return id
}
