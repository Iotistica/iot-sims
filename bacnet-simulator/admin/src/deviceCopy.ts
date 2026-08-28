import { api } from './api'
import type { Device, SemanticEntity, SimObject, ReplayRecording } from './types'
import type { SimulationModelPayload } from './api'
import { coerceValueForObjectType } from './objectValue'

interface CopyDeviceOptions {
  name: string
  deviceInstance: number
  presentValues?: Record<number, unknown>
  simulationMode?: 'simulation' | 'mirror' | 'replay'
  sourceDeviceId?: number | null
  locationId?: number | null
  copySemantics?: boolean
  copySimulation?: boolean
}

interface CopyDeviceResult {
  device: Device
  objectCount: number
  objectIdMap: Record<number, number>
  simulationModelCount: number
}

function semanticEntityKey(entity: Omit<SemanticEntity, 'id' | 'semantic_key'>): string {
  return [
    entity.entity_kind,
    entity.brick_class,
    entity.local_slug ?? '',
    entity.device_id ?? '',
    entity.object_id ?? '',
    entity.location_id ?? '',
    entity.equipment_id ?? '',
  ].join('|')
}

async function copySemanticGraph(
  source: Device,
  created: Device,
  srcObjects: SimObject[],
  objectIdMap: Record<number, number>,
) {
  const srcObjectIds = new Set(srcObjects.map(o => o.id))
  const allEntities = await api.semanticEntities.list()
  const sourceEntities = allEntities.filter(entity =>
    entity.device_id === source.id ||
    (entity.object_id != null && srcObjectIds.has(entity.object_id))
  )
  if (!sourceEntities.length) return

  const newObjectIds = new Set(Object.values(objectIdMap))
  const newEntities = allEntities.filter(entity =>
    entity.device_id === created.id ||
    (entity.object_id != null && newObjectIds.has(entity.object_id))
  )
  const existingByKey = new Map(
    newEntities.map(entity => [semanticEntityKey(entity), entity])
  )
  const entityIdMap = new Map<number, number>()

  for (const entity of sourceEntities) {
    const body: Omit<SemanticEntity, 'id' | 'semantic_key'> = {
      name: entity.device_id === source.id && entity.name === source.name
        ? created.name
        : entity.name,
      local_slug: entity.local_slug ?? null,
      brick_class: entity.brick_class,
      entity_kind: entity.entity_kind,
      device_id: entity.device_id === source.id ? created.id : null,
      object_id: entity.object_id != null ? objectIdMap[entity.object_id] ?? null : null,
      location_id: null,
      equipment_id: null,
    }
    if (body.device_id == null && body.object_id == null) continue

    const key = semanticEntityKey(body)
    const existing = existingByKey.get(key)
    if (existing) {
      entityIdMap.set(entity.id, existing.id)
      continue
    }

    const copied = await api.semanticEntities.create(body)
    existingByKey.set(key, copied)
    entityIdMap.set(entity.id, copied.id)
  }

  if (!entityIdMap.size) return
  const allRelationships = await api.semanticRelationships.list()
  for (const relationship of allRelationships) {
    const sourceEntityId = entityIdMap.get(relationship.source_entity_id)
    const targetEntityId = entityIdMap.get(relationship.target_entity_id)
    if (sourceEntityId == null || targetEntityId == null) continue
    try {
      await api.semanticRelationships.create({
        source_entity_id: sourceEntityId,
        predicate: relationship.predicate,
        target_entity_id: targetEntityId,
      })
    } catch {
      // Duplicate relationships are harmless; continue copying the rest.
    }
  }
}

/** high/medium only -- same threshold MappingSuggestionsModal.vue's own
 * defaultIncluded() already uses for pre-checking a suggestion row. A
 * low-confidence guess is worse than leaving the variable unmapped for
 * the user to fill in by hand (same as a fresh "Add Model" would need). */
function isConfidentSuggestion(entry: { suggested_point_id: number | null; confidence: string }): boolean {
  return entry.suggested_point_id != null && (entry.confidence === 'high' || entry.confidence === 'medium')
}

async function copySimulationModels(
  source: Device,
  created: Device,
  objectIdMap: Record<number, number>,
): Promise<number> {
  const copiedModels = await api.simulationModels.list(source.id)
  if (!copiedModels.length) {
    throw new Error(
      `Simulation is checked, but "${source.name}" has no saved simulation model attached to this controller.`
    )
  }
  let count = 0

  for (const model of copiedModels) {
    // A mapping owned by the source device's own points (every output,
    // and any input that happens to be one of the source's own points)
    // remaps 1:1 via objectIdMap -- guaranteed correct, the same point
    // role on the freshly duplicated copy.
    //
    // A mapping referencing a point on ANOTHER device (an upstream VAV, a
    // weather station) is NOT carried over verbatim -- "created" has its
    // own identity and needs its own upstream equipment (e.g. "VAV-1 Zone
    // 3", not "VAV-1 Zone 2"'s point, which a blind copy would otherwise
    // produce -- the exact bug this replaced). Those are instead looked
    // up fresh via the same mapping-suggestion engine "Auto Map" already
    // uses, kept only at high/medium confidence (isConfidentSuggestion
    // above) -- a low-confidence guess is worse than leaving it blank.
    // Aggregate mappings (max/weighted_average, potentially several
    // cross-device points per variable) aren't attempted here at all --
    // the suggestion engine only returns one point per variable -- so
    // they're left for the user to reconfigure via the drawer's own
    // Aggregate UI, same as any variable that gets no confident
    // suggestion.
    const ownMappings: Array<{ variable: string; direction: 'input' | 'output'; point_id: number }> = []
    const externalInputVariables: string[] = []
    for (const mapping of model.mappings) {
      if (!('point_id' in mapping)) continue // aggregate row -- not remapped, see above
      const remappedPointId = objectIdMap[mapping.point_id]
      if (remappedPointId != null) {
        ownMappings.push({ variable: mapping.variable, direction: mapping.direction, point_id: remappedPointId })
      } else if (mapping.direction === 'input') {
        externalInputVariables.push(mapping.variable)
      }
      // An output whose point isn't in objectIdMap shouldn't happen
      // structurally (outputs always belong to the source device's own
      // points) -- silently dropped rather than thrown, since a copy
      // shouldn't hard-fail on an inconsistency in the source data.
    }

    const suggestedMappings: Array<{ variable: string; direction: 'input'; point_id: number }> = []
    if (externalInputVariables.length) {
      try {
        const suggestions = await api.simulationModels.mappingSuggestions({
          model_type: model.model_type,
          provider_type: model.provider_type,
          created_from_device_id: created.id,
        })
        const byVariable = new Map(suggestions.variables.map(s => [s.variable, s]))
        for (const variable of externalInputVariables) {
          const suggestion = byVariable.get(variable)
          if (suggestion && isConfidentSuggestion(suggestion)) {
            suggestedMappings.push({ variable, direction: 'input', point_id: suggestion.suggested_point_id! })
          }
        }
      } catch {
        // Suggestions are advisory -- if the lookup fails, the copy still
        // proceeds with whatever mapped cleanly via objectIdMap; the rest
        // is left for the user to map manually, same as a lookup failure
        // already does for the drawer's own "Auto Map" button.
      }
    }

    const payload: SimulationModelPayload = {
      name: `${model.name} Copy`,
      provider_type: model.provider_type,
      model_type: model.model_type,
      // Always a draft, never auto-enabled -- even a fully/confidently
      // remapped copy shouldn't self-activate unreviewed (matches this
      // app's own "suggestions are never auto-applied without a human
      // reviewing them" convention), and it sidesteps a separate,
      // unrelated failure mode: a brand-new provider registration can
      // fail its very first activation because the simulation engine
      // hasn't cached a live value for an input yet, even one that's
      // genuinely correctly mapped (see FMUInputResolutionError's
      // docstring in providers/fmu.py -- recover_unhealthy_simulation_
      // models() self-heals this a few seconds later regardless, but
      // it's still a needless scary error to surface on every duplicate).
      enabled: false,
      parameters: model.parameters,
      created_from_device_id: created.id,
      mappings: [...ownMappings, ...suggestedMappings],
      aggregate_mappings: [],
      // Input exposures target a specific point; a copy's cross-device
      // points are re-suggested (see externalInputVariables above), so
      // there's no safe automatic target to carry an exposure over to --
      // left for the user to reconfigure via the drawer, same as an
      // aggregate mapping.
      input_exposures: [],
    }
    await api.simulationModels.create(payload)
    count += 1
  }

  return count
}

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
  opts: CopyDeviceOptions,
): Promise<CopyDeviceResult> {
  const copySemantics = opts.copySemantics ?? true
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
    location_id:              opts.locationId !== undefined ? opts.locationId : source.location_id,
    equipment_type:           copySemantics ? source.equipment_type ?? null : null,
    simulation_mode:          opts.simulationMode ?? 'simulation',
    source_device_id:         opts.sourceDeviceId ?? null,
  })

  const objectIdMap: Record<number, number> = {}
  for (const obj of srcObjects) {
    const seeded = opts.presentValues !== undefined
    const behavior = seeded ? 'constant' : obj.behavior
    const behaviorParams = seeded
      ? JSON.stringify({ value: coerceValueForObjectType(obj.object_type, opts.presentValues?.[obj.id], obj.number_of_states) })
      : obj.behavior_params

    const copied = await api.objects.create(created.id, {
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
      point_type:       copySemantics ? obj.point_type ?? null : null,
    })
    objectIdMap[obj.id] = copied.id
  }

  if (copySemantics) {
    await copySemanticGraph(source, created, srcObjects, objectIdMap)
  }

  const simulationModelCount = opts.copySimulation
    ? await copySimulationModels(source, created, objectIdMap)
    : 0

  if (simulationModelCount > 0) {
    await api.simulationModels.reconcile()
  }

  return {
    device: created,
    objectCount: srcObjects.length,
    objectIdMap,
    simulationModelCount,
  }
}

interface CreateReplayDeviceOptions {
  name: string
  deviceInstance: number
  sourceDeviceId: number
  recording: ReplayRecording
  locationId?: number | null
}

/**
 * Replay mode's clone is deliberately NOT built through copyDeviceAndObjects
 * above: that function's object list (srcObjects: SimObject[]) and its
 * semantic-graph/simulation-model copying both assume real, currently-
 * existing source object rows -- neither applies to a recording, which is a
 * frozen historical snapshot (replay_recording_points, not `objects`) that
 * may already outlive the source device's current object structure. Objects
 * are seeded at a neutral default (0/false/first state) -- replay_playback_loop
 * drives their real values once playback starts, the same way a Twin's
 * objects start at whatever mirror_sync_loop next reads.
 */
export async function createReplayDevice(
  source: Device,
  opts: CreateReplayDeviceOptions,
): Promise<CopyDeviceResult> {
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
    location_id:              opts.locationId !== undefined ? opts.locationId : source.location_id,
    equipment_type:           null,
    simulation_mode:          'replay',
    source_device_id:         opts.sourceDeviceId,
    replay_recording_id:      opts.recording.id,
  })

  const objectIdMap: Record<number, number> = {}
  const points = opts.recording.points ?? []
  for (const p of points) {
    const defaultValue = coerceValueForObjectType(p.object_type, 0, 2)
    const copied = await api.objects.create(created.id, {
      object_type:      p.object_type,
      object_instance:  p.object_instance,
      name:             p.object_name,
      units:            p.units ?? 'no-units',
      behavior:         'constant',
      behavior_params:  JSON.stringify({ value: defaultValue }),
      enabled:          1,
      number_of_states: 2,
      point_type:       p.point_type ?? null,
    })
    objectIdMap[p.id] = copied.id
  }

  return {
    device: created,
    objectCount: points.length,
    objectIdMap,
    simulationModelCount: 0,
  }
}
