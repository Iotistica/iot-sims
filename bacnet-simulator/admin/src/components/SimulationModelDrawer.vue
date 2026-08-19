<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type {
  SimulationModelCatalogEntry,
  SimulationModelConfig,
  SimulationModelMappingHints,
  SimulationModelPayload,
  SimulationModelPointOption,
  SimulationProviderCatalogEntry,
  SimulationProviderType,
} from '../api'
import type { Device } from '../types'
import MappingSuggestionsModal from './MappingSuggestionsModal.vue'

interface CatalogParameter {
  name: string
  label: string
  type: string
  default?: unknown
  unit?: string | null
  required?: boolean
  advanced?: boolean
  minimum?: number | null
  maximum?: number | null
}
interface CatalogVariable {
  name: string
  label: string
  direction: 'input' | 'output'
  unit?: string | null
  default?: unknown
  required?: boolean
  mapping_hints?: SimulationModelMappingHints | null
}

// Mirrors src/core/config.py's BINARY_TYPES | MULTISTATE_TYPES -- a small,
// stable enum duplicated client-side (same tradeoff already accepted by
// matchingDevicePoint's hardcoded matcher table below), used as a hard
// eligibility filter for Aggregate source points.
const NON_NUMERIC_OBJECT_TYPES = new Set([
  'binary-input', 'binary-output', 'binary-value',
  'multi-state-input', 'multi-state-output', 'multi-state-value',
])
interface ModelCatalogEntry extends SimulationModelCatalogEntry {
  parameters: CatalogParameter[]
  inputs: CatalogVariable[]
  outputs: CatalogVariable[]
}

interface PointOption extends SimulationModelPointOption {}

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: [] }>()

const loading = ref(false)
const saving = ref(false)
const catalog = ref<ModelCatalogEntry[]>([])
const providers = ref<SimulationProviderCatalogEntry[]>([])
const points = ref<PointOption[]>([])
const advancedOpen = ref<string[]>([])
const savedModelId = ref<number | null>(null)
const mappingModalOpen = ref(false)
const savingDraft = ref(false)

const form = reactive({
  provider_type: 'fmu' as SimulationModelPayload['provider_type'],
  model_type: '',
  name: '',
  enabled: true,
  parameters: {} as Record<string, unknown>,
  mappings: {} as Record<string, number | undefined>,
  inputSources: {} as Record<string, 'constant' | 'point' | 'aggregate'>,
  inputDefaults: {} as Record<string, unknown>,
  /** Only used for operation='max' (the multi-select UI, unchanged). */
  aggregatePoints: {} as Record<string, number[]>,
  aggregateOperation: {} as Record<string, 'max' | 'weighted_average'>,
  /** Only used for operation='weighted_average': independent paired rows,
   * each a { value, weight } point-id pair -- NOT derived from
   * aggregatePoints, so there's no multi-select/per-row duplication and no
   * index-syncing between two separate arrays to get wrong. A row with
   * either side unset is "in progress"; both sides must be set for the
   * row to be valid (see validateMappings/buildPayload). */
  aggregatePairs: {} as Record<string, Array<{ value?: number; weight?: number }>>,
})

// Per-variable ranked candidate order (point_id -> rank index, lower =
// better match), populated on demand from POST
// /simulation/models/variable-candidates. Advisory only -- see
// aggregatePointOptions: a point missing from this map still sorts last,
// it's never excluded, so the picker stays usable even when the hint
// scoring doesn't have a strong match.
const variableCandidateScores = ref<Record<string, Map<number, number>>>({})

const providerOptions = computed(() => providers.value
  .filter(p => p.provider_type !== 'builtin' && p.provider_type !== 'learned')
  .map(p => ({
    value: p.provider_type,
    label: p.label,
    disabled: !p.available,
  })))
const filteredCatalog = computed(() => catalog.value.filter(m => m.provider_type === form.provider_type))
const selectedProvider = computed(() => providers.value.find(p => p.provider_type === form.provider_type) ?? null)
const selectedModel = computed(() => filteredCatalog.value.find(m => m.model_type === form.model_type) ?? null)
const commonParameters = computed(() => selectedModel.value?.parameters.filter(p => !p.advanced) ?? [])
const advancedParameters = computed(() => selectedModel.value?.parameters.filter(p => p.advanced) ?? [])
const inputs = computed(() => selectedModel.value?.inputs ?? [])
const outputs = computed(() => selectedModel.value?.outputs ?? [])
const variables = computed(() => [...inputs.value, ...outputs.value])
const primaryActionLabel = computed(() => savedModelId.value == null ? 'Create' : 'Apply')
const pointOptions = computed(() => points.value.map(p => ({
  value: p.id,
  label: p.device_name ? `${p.device_name} / ${p.name}` : p.name,
})))

/** Aggregate source options: same label format as pointOptions, hard-
 * filtered to numeric/analog-compatible points, soft-sorted by the ranked
 * candidate order for this variable when available (a point outside the
 * ranked shortlist still sorts last rather than being hidden). */
function aggregatePointOptions(variableName: string) {
  const ranked = variableCandidateScores.value[variableName]
  return points.value
    .filter(p => !NON_NUMERIC_OBJECT_TYPES.has(p.object_type ?? ''))
    .map(p => ({
      value: p.id,
      label: p.device_name ? `${p.device_name} / ${p.name}` : p.name,
      _rank: ranked?.get(p.id) ?? Number.POSITIVE_INFINITY,
    }))
    .sort((a, b) => a._rank - b._rank)
}

function addAggregatePair(variableName: string) {
  const pairs = form.aggregatePairs[variableName] ?? []
  form.aggregatePairs[variableName] = [...pairs, {}]
}

function removeAggregatePair(variableName: string, index: number) {
  const pairs = form.aggregatePairs[variableName] ?? []
  form.aggregatePairs[variableName] = pairs.filter((_pair, i) => i !== index)
}

function onAggregateOperationChange(variableName: string, operation: 'max' | 'weighted_average') {
  form.aggregateOperation[variableName] = operation
  if (operation === 'weighted_average' && !(form.aggregatePairs[variableName]?.length)) {
    // First time switching to Weighted Average for this variable: seed one
    // pair per point already selected in Maximum mode's multi-select (so
    // toggling operation doesn't discard a selection the user already
    // made), or a single empty row if nothing was selected yet.
    const existingPoints = form.aggregatePoints[variableName] ?? []
    form.aggregatePairs[variableName] = existingPoints.length
      ? existingPoints.map(value => ({ value }))
      : [{}]
  }
}

/** Ranks candidates for one variable via the same discovery/scoring path
 * Auto Map already uses server-side (mapping_hints/suggested_point_types),
 * so the Aggregate picker can sort by relevance without duplicating any
 * scoring logic client-side. Advisory only -- failure just leaves the
 * picker in its default (unranked) order, never blocks selection. */
async function loadVariableCandidates(v: CatalogVariable) {
  if (!props.device) return
  try {
    const res = await api.simulationModels.variableCandidates({
      model_type: form.model_type,
      variable: v.name,
      created_from_device_id: props.device.id,
      current_model_id: savedModelId.value,
    })
    variableCandidateScores.value[v.name] = new Map(res.candidates.map((c, i) => [c.id, i]))
  } catch {
    // Ranking is advisory only -- fall back to unranked pointOptions order.
  }
}

function normalizedText(value: unknown): string {
  return String(value ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '')
}

function numericValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function matchingDevicePoint(variableName: string): PointOption | null {
  if (!props.device) return null
  const sameDevice = points.value.filter(p => p.device_id === props.device!.id)
  const matchers: Record<string, string[]> = {
    heating_setpoint_c: ['heatingsp', 'heatingsetpoint', 'heatingsetpoint'],
    cooling_setpoint_c: ['coolingsp', 'coolingsetpoint', 'coolingsetpoint'],
    supply_air_temp_c: ['supplyairtemp', 'supplytemp', 'sat'],
    outdoor_temp_c: ['outdoortemp', 'outsideairtemp', 'oat'],
    internal_gain_w: ['internalgain', 'internalload', 'load'],
  }
  const tokens = matchers[variableName] ?? [normalizedText(variableName)]
  return sameDevice.find((point) => {
    const haystack = normalizedText(`${point.name} ${point.point_type ?? ''}`)
    return tokens.some(token => haystack.includes(token))
  }) ?? null
}

function configuredPointValue(point: PointOption | null): unknown {
  return point?.configured_value
}

function inputMismatch(v: CatalogVariable): { point: PointOption; pointValue: number; inputValue: number } | null {
  if (form.provider_type !== 'fmu' || form.inputSources[v.name] === 'point' || form.inputSources[v.name] === 'aggregate') return null
  const point = matchingDevicePoint(v.name)
  const pointValue = numericValue(configuredPointValue(point))
  const inputValue = numericValue(form.inputDefaults[v.name])
  if (!point || pointValue === null || inputValue === null) return null
  return Math.abs(pointValue - inputValue) > 0.0001
    ? { point, pointValue, inputValue }
    : null
}

function defaultForInput(v: CatalogVariable): unknown {
  if (v.default !== undefined) return v.default
  if (v.name === 'heating_setpoint_c') return 20
  if (v.name === 'cooling_setpoint_c') return 23
  if (v.name === 'supply_air_temp_c') return 13
  if (v.name === 'outdoor_temp_c') return 30
  if (v.name === 'internal_gain_w') return 1000
  if (v.unit === '°C') return 20
  if (v.unit === 'W') return 0
  return 0
}

function resetForModel(modelType: string) {
  const model = filteredCatalog.value.find(m => m.model_type === modelType)
  form.model_type = modelType
  form.provider_type = model?.provider_type ?? 'fmu'
  form.parameters = {}
  form.mappings = {}
  form.inputSources = {}
  form.inputDefaults = {}
  form.aggregatePoints = {}
  form.aggregateOperation = {}
  form.aggregatePairs = {}
  advancedOpen.value = []
  for (const p of model?.parameters ?? []) {
    if (p.default !== undefined) form.parameters[p.name] = p.default
  }
  for (const v of model?.inputs ?? []) {
    const matchingPoint = form.provider_type === 'fmu' ? matchingDevicePoint(v.name) : null
    form.inputSources[v.name] = matchingPoint ? 'point' : (form.provider_type === 'fmu' ? 'constant' : 'point')
    form.mappings[v.name] = matchingPoint?.id
    form.inputDefaults[v.name] = configuredPointValue(matchingPoint) ?? defaultForInput(v)
    form.aggregatePoints[v.name] = []
    form.aggregateOperation[v.name] = 'max'
    form.aggregatePairs[v.name] = []
  }
  if (props.device && model) form.name = `${props.device.name} ${model.label}`
}

function resetForProvider(providerType: SimulationProviderType) {
  if (providerType === 'builtin' || providerType === 'learned') return
  form.provider_type = providerType
  const first = catalog.value.find(m => m.provider_type === providerType)
  if (first) resetForModel(first.model_type)
}

function hydrateFromSavedModel(saved: SimulationModelConfig) {
  savedModelId.value = saved.id
  form.model_type = saved.model_type
  form.provider_type = saved.provider_type
  form.name = saved.name
  form.enabled = saved.enabled
  const parameters = { ...saved.parameters }
  delete parameters.model
  delete parameters.runtime_url
  delete parameters.timeout_s
  const inputDefaults = { ...((parameters.input_defaults as Record<string, unknown> | undefined) ?? {}) }
  const inputSources = { ...((parameters.input_sources as Record<string, 'constant' | 'point' | 'aggregate'> | undefined) ?? {}) }
  const draftOutputMappings = { ...((parameters.draft_output_mappings as Record<string, number | undefined> | undefined) ?? {}) }
  delete parameters.input_defaults
  delete parameters.input_sources
  delete parameters.draft_output_mappings
  form.parameters = parameters
  form.inputDefaults = inputDefaults

  const mappings: Record<string, number | undefined> = {}
  const aggregatePoints: Record<string, number[]> = {}
  const aggregateOperation: Record<string, 'max' | 'weighted_average'> = {}
  const aggregatePairs: Record<string, Array<{ value?: number; weight?: number }>> = {}
  for (const m of saved.mappings) {
    // Same "point_ids" (plural) vs "point_id" discriminator model_runtime.
    // _is_aggregate_row uses server-side -- keeps both ends of the contract
    // lexically matched.
    if ('point_ids' in m) {
      aggregatePoints[m.variable] = [...m.point_ids]
      aggregateOperation[m.variable] = m.operation === 'weighted_average' ? 'weighted_average' : 'max'
      aggregatePairs[m.variable] = m.operation === 'weighted_average'
        ? m.point_ids.map((pid, i) => ({ value: pid, weight: (m.weight_point_ids ?? [])[i] ?? undefined }))
        : []
    } else {
      mappings[m.variable] = m.point_id
    }
  }
  for (const [variable, pointId] of Object.entries(draftOutputMappings)) {
    if (mappings[variable] == null) mappings[variable] = pointId
  }
  form.mappings = mappings
  form.aggregatePoints = aggregatePoints
  form.aggregateOperation = aggregateOperation
  form.aggregatePairs = aggregatePairs
  form.inputSources = {}
  for (const v of catalog.value.find(m => m.model_type === saved.model_type)?.inputs ?? []) {
    const savedSource = inputSources[v.name]
    form.inputSources[v.name] = savedSource === 'constant' || savedSource === 'point' || savedSource === 'aggregate'
      ? savedSource
      : (mappings[v.name] != null ? 'point' : (aggregatePoints[v.name]?.length ? 'aggregate' : 'constant'))
    if (form.inputDefaults[v.name] === undefined) form.inputDefaults[v.name] = defaultForInput(v)
    if (form.aggregatePoints[v.name] === undefined) form.aggregatePoints[v.name] = []
    if (form.aggregateOperation[v.name] === undefined) form.aggregateOperation[v.name] = 'max'
    if (form.aggregatePairs[v.name] === undefined) form.aggregatePairs[v.name] = []
    if (form.inputSources[v.name] === 'aggregate') void loadVariableCandidates(v)
  }
  advancedOpen.value = []
}

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    ;[providers.value, catalog.value, points.value] = await Promise.all([
      api.simulationProviders.catalog(),
      api.simulationModels.catalog(),
      api.simulationModels.pointOptions(),
    ])
    savedModelId.value = null

    // Per-controller state: if a model was saved for this source
    // controller, hydrate from DB. Existing/legacy FMU configs may only be
    // discoverable from the device's active output-owner summary, so fall
    // back to that model id before resetting to fresh defaults.
    const existing = await api.simulationModels.list(props.device.id)
    if (existing.length > 0) {
      hydrateFromSavedModel(existing[0])
      return
    }
    const activeModelId = props.device.active_simulation_model?.id
    if (activeModelId != null) {
      try {
        hydrateFromSavedModel(await api.simulationModels.get(activeModelId))
        return
      } catch {
        // The active-model summary is advisory; if it points at a deleted
        // row, keep opening a fresh drawer instead of blocking the user.
      }
    }

    const first = catalog.value.find(m => m.provider_type === 'fmu') ?? catalog.value[0]
    if (first) resetForModel(first.model_type)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load simulation model catalog')
  } finally {
    loading.value = false
  }
}

watch(
  [() => props.open, () => props.device?.id],
  ([open, deviceId], [prevOpen, prevDeviceId]) => {
    if (!open || !deviceId) return
    if (!prevOpen || deviceId !== prevDeviceId) void load()
  },
)

function validateMappings(requireComplete: boolean): boolean {
  if (!requireComplete) return true
  if (!selectedModel.value) return false

  for (const v of outputs.value) {
    if (v.required !== false && !form.mappings[v.name]) {
      message.error(`${v.label} mapping is required`)
      return false
    }
  }
  for (const v of inputs.value) {
    if (form.provider_type !== 'fmu' || form.inputSources[v.name] === 'point') {
      if (v.required !== false && !form.mappings[v.name]) {
        message.error(`${v.label} mapping is required`)
        return false
      }
    }
    if (form.provider_type === 'fmu' && form.inputSources[v.name] === 'aggregate') {
      const operation = form.aggregateOperation[v.name] ?? 'max'
      if (operation === 'weighted_average') {
        const pairs = form.aggregatePairs[v.name] ?? []
        const incomplete = pairs.some(p => (p.value != null) !== (p.weight != null))
        if (incomplete) {
          message.error(`${v.label} has a Weighted Average row with only a value point or only a weight point selected -- complete or remove it`)
          return false
        }
        const validPairs = pairs.filter(p => p.value != null && p.weight != null)
        if (v.required !== false && !validPairs.length) {
          message.error(`${v.label} requires at least one complete value/weight pair for the Weighted Average aggregate`)
          return false
        }
      } else if (v.required !== false && !(form.aggregatePoints[v.name]?.length)) {
        message.error(`${v.label} requires at least one source point for the Maximum aggregate`)
        return false
      }
    }
  }
  return true
}

function buildPayload(enabled: boolean): SimulationModelPayload | null {
  if (!props.device || !selectedModel.value) return null
  const modelName = `${props.device.name} ${selectedModel.value.label}`.trim()

  const parameters = { ...form.parameters }
  delete parameters.model
  delete parameters.runtime_url
  delete parameters.timeout_s
  if (form.provider_type === 'fmu') {
    parameters.input_sources = Object.fromEntries(
      inputs.value.map(v => [v.name, form.inputSources[v.name] ?? 'constant']),
    )
    parameters.input_defaults = Object.fromEntries(
      inputs.value
        .filter(v => form.inputSources[v.name] !== 'point')
        .map(v => [v.name, form.inputDefaults[v.name]]),
    )
  }
  if (!enabled) {
    parameters.draft_output_mappings = Object.fromEntries(
      outputs.value
        .filter(v => form.mappings[v.name] != null)
        .map(v => [v.name, form.mappings[v.name]]),
    )
  }

  return {
    name: modelName,
    provider_type: form.provider_type,
    model_type: form.model_type,
    enabled,
    created_from_device_id: props.device.id,
    parameters,
    mappings: variables.value
      .filter(v => enabled || v.direction !== 'output')
      .filter(v => v.direction === 'output' || form.inputSources[v.name] === 'point' || form.provider_type !== 'fmu')
      .filter(v => form.mappings[v.name] != null)
      .map(v => ({ variable: v.name, direction: v.direction, point_id: form.mappings[v.name]! })),
    aggregate_mappings: inputs.value
      .filter(v => form.provider_type === 'fmu' && form.inputSources[v.name] === 'aggregate')
      // Only complete (value+weight) pairs are ever sent for
      // weighted_average -- an in-progress/incomplete row is silently
      // omitted here (mirroring how a zero-point Maximum aggregate is
      // already omitted below), so "Save" tolerates a mid-edit draft the
      // same way it already does for "Maximum"; "Apply" is the one that
      // blocks with a clear message, via validateMappings above.
      .filter(v => {
        if ((form.aggregateOperation[v.name] ?? 'max') === 'weighted_average') {
          return (form.aggregatePairs[v.name] ?? []).some(p => p.value != null && p.weight != null)
        }
        return (form.aggregatePoints[v.name]?.length ?? 0) > 0
      })
      .map(v => {
        const operation = form.aggregateOperation[v.name] ?? 'max'
        if (operation === 'weighted_average') {
          const validPairs = (form.aggregatePairs[v.name] ?? []).filter(p => p.value != null && p.weight != null)
          return {
            variable: v.name,
            direction: 'input' as const,
            operation,
            point_ids: validPairs.map(p => p.value!),
            weight_point_ids: validPairs.map(p => p.weight!),
          }
        }
        return {
          variable: v.name,
          direction: 'input' as const,
          operation: 'max' as const,
          point_ids: form.aggregatePoints[v.name]!,
        }
      }),
  }
}

async function persist(apply: boolean) {
  const payload = buildPayload(apply)
  if (!payload) return
  if (!validateMappings(apply)) return

  const busy = apply ? saving : savingDraft
  busy.value = true
  try {
    let saved: SimulationModelConfig
    if (savedModelId.value != null) {
      saved = await api.simulationModels.update(savedModelId.value, payload)
      message.success(apply && payload.enabled ? 'Simulation model applied' : 'Simulation model saved')
    } else {
      saved = await api.simulationModels.create(payload)
      message.success(apply && payload.enabled ? 'Simulation model added' : 'Simulation model saved')
    }
    savedModelId.value = saved.id
    form.enabled = saved.enabled
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save simulation model')
  } finally {
    busy.value = false
  }
}

const saveDraft = () => persist(false)
const applyModel = () => persist(true)

function onMappingsApplied({ mappings, switchToPoint }: { mappings: Record<string, number>; switchToPoint: string[] }) {
  Object.assign(form.mappings, mappings)
  for (const variableName of switchToPoint) {
    form.inputSources[variableName] = 'point'
  }
}

function setInputSource(v: CatalogVariable, source: 'constant' | 'point' | 'aggregate') {
  form.inputSources[v.name] = source
  if (source === 'aggregate') void loadVariableCandidates(v)
}
</script>

<template>
  <a-drawer
    :open="open"
    :title="device ? `Simulation Model — ${device.name}` : 'Simulation Model'"
    width="520"
    :z-index="1050"
    @close="emit('update:open', false)"
  >
    <a-spin :spinning="loading">
      <a-form layout="vertical" :colon="false">
        <a-form-item label="Provider" required>
          <a-select
            v-model:value="form.provider_type"
            :options="providerOptions"
            @change="resetForProvider"
          />
          <div v-if="selectedProvider?.description" style="font-size:12px;color:var(--text-muted);margin-top:5px">
            {{ selectedProvider.description }}
          </div>
        </a-form-item>

        <a-form-item label="Model" required>
          <a-select
            v-model:value="form.model_type"
            :options="filteredCatalog.map(m => ({ value: m.model_type, label: m.label }))"
            @change="resetForModel"
          />
          <div v-if="selectedModel?.description" style="font-size:12px;color:var(--text-muted);margin-top:5px">
            {{ selectedModel.description }}
          </div>
        </a-form-item>

        <template v-if="commonParameters.length || advancedParameters.length">
          <a-divider orientation="left">Parameters</a-divider>
          <a-form-item v-for="p in commonParameters" :key="p.name" :label="p.label" :required="p.required">
            <a-switch v-if="p.type === 'boolean'" v-model:checked="(form.parameters[p.name] as boolean)" />
            <a-input
              v-else-if="p.type === 'string'"
              v-model:value="(form.parameters[p.name] as string)"
              style="width:100%"
            />
            <a-input-number
              v-else
              v-model:value="(form.parameters[p.name] as number)"
              :min="p.minimum ?? undefined"
              :max="p.maximum ?? undefined"
              :addon-after="p.unit ?? undefined"
              style="width:100%"
            />
          </a-form-item>

          <a-collapse v-if="advancedParameters.length" v-model:activeKey="advancedOpen" ghost style="margin-top:4px">
            <a-collapse-panel key="advanced" header="Advanced">
              <a-form-item v-for="p in advancedParameters" :key="p.name" :label="p.label">
                <a-switch v-if="p.type === 'boolean'" v-model:checked="(form.parameters[p.name] as boolean)" />
                <a-input
                  v-else-if="p.type === 'string'"
                  v-model:value="(form.parameters[p.name] as string)"
                  style="width:100%"
                />
                <a-input-number
                  v-else
                  v-model:value="(form.parameters[p.name] as number)"
                  :min="p.minimum ?? undefined"
                  :max="p.maximum ?? undefined"
                  :addon-after="p.unit ?? undefined"
                  style="width:100%"
                />
              </a-form-item>
            </a-collapse-panel>
          </a-collapse>
        </template>

        <a-divider orientation="left">Point Mapping</a-divider>
        <div style="display:flex;justify-content:flex-end;margin-bottom:10px">
          <a-button size="small" :disabled="!selectedModel" @click="mappingModalOpen = true">
            Auto Map
          </a-button>
        </div>
        <a-alert
          type="info"
          show-icon
          style="margin-bottom:14px"
          :message="form.provider_type === 'fmu'
            ? 'FMU inputs may use constants/defaults or BACnet points. FMU outputs are owned by this model while it is enabled.'
            : 'Inputs may come from other controllers. Output points are owned by this model while it is enabled.'"
        />

        <template v-if="form.provider_type === 'fmu'">
          <a-divider orientation="left">Inputs / Defaults</a-divider>
          <a-form-item
            v-for="v in inputs"
            :key="`input:${v.name}`"
            :label="v.label"
          >
            <a-segmented
              v-model:value="form.inputSources[v.name]"
              :options="[
                { label: 'Constant', value: 'constant' },
                { label: 'Point', value: 'point' },
                { label: 'Aggregate', value: 'aggregate' },
              ]"
              style="margin-bottom:8px"
              @change="(value: string | number) => setInputSource(v, value as 'constant' | 'point' | 'aggregate')"
            />
            <a-input-number
              v-if="form.inputSources[v.name] !== 'point' && form.inputSources[v.name] !== 'aggregate'"
              v-model:value="(form.inputDefaults[v.name] as number)"
              :addon-after="v.unit ?? undefined"
              style="width:100%"
            />
            <a-alert
              v-if="inputMismatch(v)"
              type="warning"
              show-icon
              style="margin-top:8px"
              :message="`${inputMismatch(v)!.point.name} is ${inputMismatch(v)!.pointValue}${v.unit ? ` ${v.unit}` : ''}; this FMU input is using constant ${inputMismatch(v)!.inputValue}${v.unit ? ` ${v.unit}` : ''}. Switch to Point if the FMU should use the BACnet setpoint.`"
            />
            <a-select
              v-if="form.inputSources[v.name] === 'point'"
              v-model:value="form.mappings[v.name]"
              show-search allow-clear
              :options="pointOptions"
              option-filter-prop="label"
              :placeholder="`Select ${v.label} point`"
            />
            <template v-if="form.inputSources[v.name] === 'aggregate'">
              <div style="margin-bottom:8px">
                <span style="font-size:12px;color:var(--text-secondary);margin-right:8px">Operation:</span>
                <a-segmented
                  :value="form.aggregateOperation[v.name] ?? 'max'"
                  :options="[
                    { label: 'Maximum', value: 'max' },
                    { label: 'Weighted Average', value: 'weighted_average' },
                  ]"
                  size="small"
                  @change="(value: string | number) => onAggregateOperationChange(v.name, value as 'max' | 'weighted_average')"
                />
              </div>

              <!-- Maximum (and any future non-weighted operation): unchanged multi-select UI. -->
              <template v-if="(form.aggregateOperation[v.name] ?? 'max') === 'max'">
                <a-select
                  v-model:value="form.aggregatePoints[v.name]"
                  mode="multiple"
                  show-search allow-clear
                  :options="aggregatePointOptions(v.name)"
                  option-filter-prop="label"
                  :placeholder="`Select ${v.label} source points`"
                  style="width:100%"
                />
                <div
                  v-if="(form.aggregatePoints[v.name] ?? []).length"
                  style="font-size:12px;color:var(--text-muted);margin-top:4px"
                >
                  Maximum of {{ (form.aggregatePoints[v.name] ?? []).length }}
                  point{{ (form.aggregatePoints[v.name] ?? []).length === 1 ? '' : 's' }}
                </div>
              </template>

              <!-- Weighted Average: independent paired rows, no multi-select. -->
              <template v-else>
                <div style="display:flex;gap:8px;font-size:11px;color:var(--text-muted);margin-bottom:4px">
                  <div style="flex:1">Value Point</div>
                  <div style="flex:1">Weight Point</div>
                  <div style="width:22px"></div>
                </div>
                <div
                  v-for="(pair, i) in (form.aggregatePairs[v.name] ?? [])"
                  :key="i"
                  style="display:flex;gap:8px;align-items:center;margin-bottom:6px"
                >
                  <a-select
                    v-model:value="pair.value"
                    show-search allow-clear
                    :options="aggregatePointOptions(v.name)"
                    option-filter-prop="label"
                    placeholder="Select value point"
                    style="flex:1"
                  />
                  <a-select
                    v-model:value="pair.weight"
                    show-search allow-clear
                    :options="aggregatePointOptions(v.name)"
                    option-filter-prop="label"
                    placeholder="Select weight point"
                    style="flex:1"
                  />
                  <a-button
                    type="text" danger size="small"
                    style="width:22px;padding:0"
                    :title="`Remove row ${i + 1}`"
                    @click="removeAggregatePair(v.name, i)"
                  >
                    ×
                  </a-button>
                </div>
                <a-button size="small" @click="addAggregatePair(v.name)">+ Add Pair</a-button>
                <div
                  v-if="(form.aggregatePairs[v.name] ?? []).some(p => p.value != null && p.weight != null)"
                  style="font-size:12px;color:var(--text-muted);margin-top:6px"
                >
                  Weighted average of {{ (form.aggregatePairs[v.name] ?? []).filter(p => p.value != null && p.weight != null).length }}
                  pair{{ (form.aggregatePairs[v.name] ?? []).filter(p => p.value != null && p.weight != null).length === 1 ? '' : 's' }}
                  = sum(value × weight) / sum(weight)
                </div>
              </template>
            </template>
          </a-form-item>

          <a-divider orientation="left">Output Mapping</a-divider>
        </template>

        <a-form-item
          v-for="v in (form.provider_type === 'fmu' ? outputs : variables)"
          :key="`${v.direction}:${v.name}`"
          :label="`${v.label} (${v.direction})`"
          :required="v.required !== false"
        >
          <a-select
            v-model:value="form.mappings[v.name]"
            show-search allow-clear
            :options="pointOptions"
            option-filter-prop="label"
            :placeholder="`Select ${v.label} point`"
          />
          <div v-if="v.unit" style="font-size:11px;color:var(--text-muted);margin-top:3px">
            Expected unit: {{ v.unit }}
          </div>
        </a-form-item>

        <a-form-item label="Status" style="margin-top:16px;margin-bottom:0">
          <a-tag :color="form.enabled ? 'green' : 'default'">
            {{ form.enabled ? 'Applied' : 'Saved draft' }}
          </a-tag>
        </a-form-item>
      </a-form>
    </a-spin>

    <template #footer>
      <a-space>
        <a-button @click="emit('update:open', false)">Close</a-button>
        <a-button :loading="savingDraft" :disabled="loading || !selectedModel || saving" @click="saveDraft">
          Save
        </a-button>
        <a-button type="primary" :loading="saving" :disabled="loading || !selectedModel || savingDraft" @click="applyModel">
          {{ primaryActionLabel }}
        </a-button>
      </a-space>
    </template>
  </a-drawer>

  <MappingSuggestionsModal
    v-model:open="mappingModalOpen"
    :device="device"
    :model="selectedModel"
    :provider-type="form.provider_type"
    :point-options="points"
    :current-mappings="form.mappings"
    :current-model-id="savedModelId"
    @apply="onMappingsApplied"
  />
</template>
