import { api } from './api'
import type { Device, SimObject } from './types'
import { coerceValueForObjectType } from './objectValue'

/**
 * Creates a new device + its objects by copying from `source` -- the shared
 * core of both the existing instant "Duplicate Device" action (simulated
 * devices, unchanged behavior) and "Create Simulated Copy" (external
 * devices, via CreateSimulatedCopyModal.vue). The created device is always
 * source_type='simulated' -- POST /devices never accepts that field, so
 * there is no way for this to accidentally produce another external row.
 *
 * When `opts.presentValues` is provided (external source), each object's
 * behavior becomes a fixed 'constant' seeded from that reading -- copying
 * the source object's own behavior/behavior_params would be meaningless for
 * an external row (inert DB defaults, since the real device is the only
 * thing that ever set its value). Object identity (name/type/instance/
 * units) is always copied byte-for-byte, regardless of source -- never
 * renamed.
 *
 * equipment_type/point_type (project-local Brick semantic classification,
 * e.g. from Suggest Semantics) are carried over too -- without this, an
 * approved classification would be silently lost the moment a device gets
 * copied, forcing a from-scratch re-classification of the copy.
 */
export async function copyDeviceAndObjects(
  source: Device,
  srcObjects: SimObject[],
  opts: { name: string; deviceInstance: number; presentValues?: Record<number, unknown> },
): Promise<{ device: Device; objectCount: number }> {
  const created = await api.devices.create({
    device_instance: opts.deviceInstance,
    name:            opts.name,
    description:     source.description,
    vendor_name:     source.vendor_name,
    model_name:      source.model_name,
    enabled:         source.enabled,
    firmware_revision:        source.firmware_revision,
    protocol_revision:        source.protocol_revision,
    max_apdu_length_accepted: source.max_apdu_length_accepted,
    segmentation_supported:   source.segmentation_supported,
    location_id:              source.location_id,
    equipment_type:           source.equipment_type ?? null,
  })

  for (const obj of srcObjects) {
    const seeded = opts.presentValues !== undefined
    const behavior = seeded ? 'constant' : obj.behavior
    const behaviorParams = seeded
      ? JSON.stringify({ value: coerceValueForObjectType(obj.object_type, opts.presentValues?.[obj.id], obj.number_of_states) })
      : obj.behavior_params

    await api.objects.create(created.id, {
      object_type:      obj.object_type,
      object_instance:  obj.object_instance,
      name:             obj.name,
      units:            obj.units,
      behavior,
      behavior_params:  behaviorParams,
      enabled:          obj.enabled,
      number_of_states: obj.number_of_states,
      reliability:      obj.reliability,
      polarity:         obj.polarity,
      point_type:       obj.point_type ?? null,
    })
  }

  return { device: created, objectCount: srcObjects.length }
}
