<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined, UploadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type {
  SimulationModelCatalogEntry,
  SimulationModelConfig,
  SimulationModelPayload,
  SimulationModelPointOption,
  SimulationProviderCatalogEntry,
  SimulationProviderRuntime,
  SimulationProviderType,
  WeatherProvenance,
} from '../api'
import type { Device } from '../types'
import MappingSuggestionsModal from './MappingSuggestionsModal.vue'
import BooleanToggle from './BooleanToggle.vue'

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
  /** A fixed enumerated set of legal values (e.g. a genset's fuelType) -- when
   * present, renders a select instead of a free-entry box, regardless of `type`. */
  allowed_values?: { value: unknown; label: string }[] | null
}
interface CatalogVariable {
  name: string
  label: string
  direction: 'input' | 'output'
  unit?: string | null
  default?: unknown
  required?: boolean
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

// form.mappings must be keyed by (direction, name), not name alone: a
// model can declare an input and an output under the identical variable
// name (e.g. RTU's fan_command_pct is both uFan's input AND yFan's
// output, alongside a separately-named supply_fan_speed_pct output for
// the same yFan signal). A name-only key made the input row's point
// selection silently bleed into the identically-named output row (and
// vice versa) -- selecting the fan command's source point also, invisibly,
// set it as that output's target, reproducing a self-referential mapping
// every time the drawer was saved regardless of what the backend held.
function mappingKey(v: { name: string; direction: string }): string {
  return `${v.direction}:${v.name}`
}

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; saved: [] }>()

const loading = ref(false)
const saving = ref(false)
const catalog = ref<ModelCatalogEntry[]>([])
const providers = ref<SimulationProviderCatalogEntry[]>([])
const points = ref<PointOption[]>([])

// File-type Parameter upload (see remote_catalog.py's is_file/"file" param
// type) -- one shared hidden <input>, not one per parameter, since only one
// upload can be in flight from a single click anyway; resourceUploadTarget
// tracks which parameter's value the next file picked should populate.
const resourceFileInput = ref<HTMLInputElement>()
const resourceUploadTarget = ref<string | null>(null)
const resourceUploading = ref<string | null>(null)

function pickResourceFile(paramName: string) {
  resourceUploadTarget.value = paramName
  resourceFileInput.value?.click()
}

function resourceFileLabel(value: unknown): string {
  if (!value || typeof value !== 'string') return ''
  return value.split(/[\\/]/).pop() || value
}

async function onResourceFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  const paramName = resourceUploadTarget.value
  input.value = '' // allow re-selecting the same filename later
  if (!file || !paramName) return
  resourceUploading.value = paramName
  try {
    const result = await api.simulationResources.upload(file)
    // "wea_filename" wants the Modelica-table (.mos) form; every other
    // file parameter (e.g. "epw_filename") gets the upload as-is. Both
    // names are a convention shared by every Buildings-library
    // weather-consuming model's model.json (Weather's wea_filename,
    // EnergyPlusThermalZone's epw_filename + wea_filename) -- not
    // hardcoded to any one model.
    form.parameters[paramName] = paramName === 'wea_filename'
      ? (result.converted_mos?.path ?? result.path)
      : result.path
    // One upload sets both halves of an EPW/MOS pair when the selected
    // model declares both parameters -- e.g. uploading
    // EnergyPlusThermalZone's "Weather (EPW)" field also fills its
    // "Weather (MOS)" field automatically, so the same source file only
    // needs picking once even though two FMI parameters need it.
    const siblingName = paramName === 'epw_filename' ? 'wea_filename'
      : paramName === 'wea_filename' ? 'epw_filename'
      : null
    if (siblingName && selectedModel.value?.parameters.some(p => p.name === siblingName)) {
      if (siblingName === 'wea_filename' && result.converted_mos) {
        form.parameters[siblingName] = result.converted_mos.path
      } else if (siblingName === 'epw_filename') {
        form.parameters[siblingName] = result.path
      }
    }
    weatherProvenance.value = result.weather_provenance
    message.success(`Uploaded ${result.filename}`)
  } catch (err: unknown) {
    message.error((err as Error).message ?? 'Upload failed')
  } finally {
    resourceUploading.value = null
  }
}

// Which real calendar year each month of the uploaded weather file's data
// was actually drawn from (a TMYx-style composite file's own #COMMENTS
// header) -- drives the "Playback Start Month" dropdown's per-option
// labels below, so a user picking "July" can see it's really 2024 data
// without having to open the source file themselves.
const weatherProvenance = ref<WeatherProvenance | null>(null)
const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function monthOptionLabel(monthNum: number): string {
  const year = weatherProvenance.value?.months?.[String(monthNum)]
  const name = MONTH_NAMES[monthNum - 1]
  return year ? `${name} (${year} data)` : name
}

function firstFileParamFilename(): string | null {
  for (const p of selectedModel.value?.parameters ?? []) {
    if (p.type !== 'file') continue
    const value = form.parameters[p.name]
    if (typeof value === 'string' && value) return resourceFileLabel(value)
  }
  return null
}

// Re-parses an already-uploaded file's header on drawer open -- the
// upload response's own weather_provenance (set directly in
// onResourceFileChange above) only exists for the file just picked in
// *this* session, not one uploaded in an earlier session and only
// referenced here by its saved path.
async function refreshWeatherProvenance() {
  const filename = firstFileParamFilename()
  if (!filename) {
    weatherProvenance.value = null
    return
  }
  try {
    const result = await api.simulationResources.provenance(filename)
    weatherProvenance.value = result.weather_provenance
  } catch {
    weatherProvenance.value = null
  }
}
const savedModelId = ref<number | null>(null)

// Per-device runtime controls (Start/Pause/Reset) -- freeze/resume/reset
// just THIS model's own provider while everything else keeps running.
// Distinct from the Enabled switch in the footer below (that fully
// unregisters/re-registers the provider; this freezes an already-
// registered one in place) -- null/hidden entirely when nothing's actually
// registered (a disabled or never-applied model has no live provider).
const runtimeStatus = ref<SimulationProviderRuntime | null>(null)
const runtimeActionLoading = ref(false)

async function loadRuntimeStatus() {
  if (savedModelId.value == null) { runtimeStatus.value = null; return }
  try {
    const r = await api.simulationModels.getRuntime(savedModelId.value)
    runtimeStatus.value = r.runtime
  } catch {
    runtimeStatus.value = null
  }
}

async function pauseRuntime() {
  if (savedModelId.value == null) return
  runtimeActionLoading.value = true
  try {
    const r = await api.simulationModels.pause(savedModelId.value)
    runtimeStatus.value = r.runtime
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to pause')
  } finally {
    runtimeActionLoading.value = false
  }
}

async function resumeRuntime() {
  if (savedModelId.value == null) return
  runtimeActionLoading.value = true
  try {
    const r = await api.simulationModels.resume(savedModelId.value)
    runtimeStatus.value = r.runtime
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to resume')
  } finally {
    runtimeActionLoading.value = false
  }
}

async function resetRuntime() {
  if (savedModelId.value == null) return
  runtimeActionLoading.value = true
  try {
    const r = await api.simulationModels.resetRuntime(savedModelId.value)
    runtimeStatus.value = r.runtime
    message.success('Model runtime reset')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to reset')
  } finally {
    runtimeActionLoading.value = false
  }
}

const RUNTIME_STATUS_COLOR: Record<string, string> = {
  running: '#52c41a', paused: '#faad14', stopped: '#8c8c8c',
  not_configured: '#8c8c8c', ready: '#8c8c8c', error: '#ff4d4f',
}

// Kept as its own editable ref (not read straight off runtimeStatus.value)
// so typing a new value doesn't fight the field mid-edit while a poll/
// reload could otherwise overwrite it -- committed via @change, then
// re-synced from the server's own echoed value once that resolves.
const rtfTargetInput = ref(1)
watch(runtimeStatus, (r) => { if (r) rtfTargetInput.value = r.real_time_factor })

async function setRtf() {
  if (savedModelId.value == null || rtfTargetInput.value <= 0) return
  runtimeActionLoading.value = true
  try {
    const r = await api.simulationModels.setRtf(savedModelId.value, rtfTargetInput.value)
    runtimeStatus.value = r.runtime
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to set real-time factor')
  } finally {
    runtimeActionLoading.value = false
  }
}
const mappingModalOpen = ref(false)
// Set by the Inputs/Outputs section's own "Auto Map" button (inline with
// that section's own divider, not a single combined button above both) --
// scopes MappingSuggestionsModal to just that direction's variables.
const mappingModalDirection = ref<'input' | 'output' | null>(null)
function openMappingModal(direction: 'input' | 'output') {
  mappingModalDirection.value = direction
  mappingModalOpen.value = true
}
const savingDraft = ref(false)

// Footer "Create points for outputs" checkbox: on check, POST
// /simulation/models/scaffold-points to create one BACnet object on this
// controller per model output that has no same-named point, then wire the
// new point ids into the (still-unsaved) output mappings so the user can
// review and hit Apply. Additive only -- unchecking never deletes the
// points, so the box latches (disabled) once it has run.
const scaffoldOutputs = ref(false)
const scaffoldingPoints = ref(false)
const scaffoldedCount = ref(0)

async function onScaffoldOutputsToggle(e: { target: { checked: boolean } }) {
  if (!e.target.checked || !props.device || !selectedModel.value) return
  scaffoldingPoints.value = true
  try {
    const res = await api.simulationModels.scaffoldPoints({
      model_type: form.model_type,
      created_from_device_id: props.device.id,
      directions: ['output'],
    })
    for (const p of res.created) {
      if (!points.value.some(existing => existing.id === p.point_id)) {
        points.value = [...points.value, {
          id: p.point_id, name: p.name, device_id: p.device_id,
          device_name: p.device_name, object_type: p.object_type,
          object_instance: p.object_instance, units: p.units,
        }]
      }
      // Never clobber a mapping the user already set by hand.
      const key = `output:${p.variable}`
      if (form.mappings[key] == null) form.mappings[key] = p.point_id
    }
    scaffoldedCount.value = res.created.length
    if (res.created.length) {
      message.success(`Created ${res.created.length} point${res.created.length === 1 ? '' : 's'} — review the Outputs and Apply`)
    } else {
      message.info('Every output already has a point')
    }
  } catch (err: unknown) {
    scaffoldOutputs.value = false
    message.error((err as Error).message ?? 'Failed to create points')
  } finally {
    scaffoldingPoints.value = false
  }
}

// FMU and AI both get the full Constant/Point/Aggregate input-sourcing UI
// (backend: simulation.py's _normalized_parameters/_mapping_dicts/
// _aggregate_mapping_dicts/_input_exposure_dicts all widened the same way)
// -- only 'builtin' stays the plain point-only path.
function supportsInputSourcing(providerType: string): boolean {
  return providerType === 'fmu' || providerType === 'ai'
}

const form = reactive({
  provider_type: 'fmu' as SimulationProviderType,
  model_type: '',
  name: '',
  enabled: true,
  parameters: {} as Record<string, unknown>,
  mappings: {} as Record<string, number | undefined>,
  inputSources: {} as Record<string, 'constant' | 'point' | 'aggregate'>,
  inputDefaults: {} as Record<string, unknown>,
  /** Only used for operation='max'/'min' (the multi-select UI, unchanged). */
  aggregatePoints: {} as Record<string, number[]>,
  aggregateOperation: {} as Record<string, 'max' | 'min' | 'weighted_average'>,
  /** Only used for operation='weighted_average': independent paired rows,
   * each a { value, weight } point-id pair -- NOT derived from
   * aggregatePoints, so there's no multi-select/per-row duplication and no
   * index-syncing between two separate arrays to get wrong. A row with
   * either side unset is "in progress"; both sides must be set for the
   * row to be valid (see validateMappings/buildPayload). */
  aggregatePairs: {} as Record<string, Array<{ value?: number; weight?: number }>>,
  /** variable -> a second BACnet point that mirrors this input's already-
   * resolved value (whichever of Constant/Point/Aggregate currently
   * sources it) each step. Independent of inputSources -- applies no
   * matter which of the three modes is active, since it just relays the
   * value already computed for the FMU, not an alternate way to compute
   * it. undefined/absent means "not exposed" for that variable. */
  inputExposures: {} as Record<string, number | undefined>,
})

// Per-variable ranked candidate order (point_id -> rank index, lower =
// better match), populated on demand from POST
// /simulation/models/variable-candidates. Advisory only -- see
// aggregatePointOptions: a point missing from this map still sorts last,
// it's never excluded, so the picker stays usable even when the hint
// scoring doesn't have a strong match.
const variableCandidateScores = ref<Record<string, Map<number, number>>>({})

const providerOptions = computed(() => providers.value
  .map(p => ({
    value: p.provider_type,
    label: p.label,
    disabled: !p.available,
  })))
const filteredCatalog = computed(() => catalog.value.filter(m => m.provider_type === form.provider_type))
const selectedProvider = computed(() => providers.value.find(p => p.provider_type === form.provider_type) ?? null)
const selectedModel = computed(() => filteredCatalog.value.find(m => m.model_type === form.model_type) ?? null)
// Parameters flagged "advanced" (e.g. EnergyPlusThermalZone's wea_filename,
// which an epw_filename upload already auto-derives -- see
// onResourceFileChange's sibling-autofill) are resolved and submitted like
// any other parameter, just never shown their own control here.
const commonParameters = computed(() => selectedModel.value?.parameters.filter(p => !p.advanced) ?? [])
// FMI parameter overrides a calibration run applied (see CalibrationFlow.vue's
// "Apply to Model" button) -- lives under parameters.parameter_overrides,
// passed through form.parameters untouched by populateForm/buildPayload
// (neither one deletes or reconstructs this key, unlike input_defaults/
// input_sources above), so this is display-only: editing happens by running
// a new calibration, not here.
const appliedParameterOverrides = computed(() => {
  const overrides = form.parameters.parameter_overrides as Record<string, number> | undefined
  return Object.entries(overrides ?? {}).map(([name, value]) => ({ name, value }))
})
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

/** Input-exposure target options: same numeric-only filter as
 * aggregatePointOptions (an exposure writes a Present Value, same as an
 * aggregate reads one), unranked -- exposure targets aren't scored by the
 * mapping-suggestion engine the way aggregate source candidates are. */
const numericPointOptions = computed(() => points.value
  .filter(p => !NON_NUMERIC_OBJECT_TYPES.has(p.object_type ?? ''))
  .map(p => ({
    value: p.id,
    label: p.device_name ? `${p.device_name} / ${p.name}` : p.name,
  })))

function addAggregatePair(variableName: string) {
  const pairs = form.aggregatePairs[variableName] ?? []
  form.aggregatePairs[variableName] = [...pairs, {}]
}

function removeAggregatePair(variableName: string, index: number) {
  const pairs = form.aggregatePairs[variableName] ?? []
  form.aggregatePairs[variableName] = pairs.filter((_pair, i) => i !== index)
}

/** The "Also write this resolved value to a point" exposure control only
 * makes sense for Aggregate inputs -- that's the mode where the resolved
 * value isn't already sitting on a single BACnet point somewhere (a plain
 * Point input already IS a point; Constant has no upstream reading at
 * all). Showing it on every input was reported as visual clutter, so it's
 * now gated to Aggregate (Maximum, Minimum, and Weighted Average alike) here. */
function exposureApplicable(variableName: string): boolean {
  return form.inputSources[variableName] === 'aggregate'
}

function onAggregateOperationChange(variableName: string, operation: 'max' | 'min' | 'weighted_average') {
  form.aggregateOperation[variableName] = operation
  if (operation === 'weighted_average' && !(form.aggregatePairs[variableName]?.length)) {
    // First time switching to Weighted Average for this variable: seed one
    // pair per point already selected in the Maximum/Minimum multi-select
    // (so toggling operation doesn't discard a selection the user already
    // made), or a single empty row if nothing was selected yet.
    const existingPoints = form.aggregatePoints[variableName] ?? []
    form.aggregatePairs[variableName] = existingPoints.length
      ? existingPoints.map(value => ({ value }))
      : [{}]
  }
}

/** Ranks candidates for one variable via the same discovery/scoring path
 * Auto Map already uses server-side (the semantic contract),
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
  if (!supportsInputSourcing(form.provider_type) || form.inputSources[v.name] === 'point' || form.inputSources[v.name] === 'aggregate') return null
  const point = matchingDevicePoint(v.name)
  const pointValue = numericValue(configuredPointValue(point))
  const inputValue = numericValue(form.inputDefaults[v.name])
  if (!point || pointValue === null || inputValue === null) return null
  return Math.abs(pointValue - inputValue) > 0.0001
    ? { point, pointValue, inputValue }
    : null
}

// A saved config's input_defaults can hold `null` (not just an absent key)
// for a variable whose numeric field was left empty at save time --
// Vue's v-model:value for an empty a-input-number is `undefined`, but
// `JSON.stringify(NaN)` (an intermediate state some numeric bindings can
// pass through) produces `null`, which survives the round trip and isn't
// caught by an `=== undefined` check on reload. Catches that plus a
// literal NaN so hydrateFromSavedModel always backfills a real default
// rather than sending an unusable value through to iot-models' /initialize
// (that endpoint's `inputs` field is dict[str, float] -- a null value
// fails Pydantic validation for the whole request, not just that field).
function needsInputDefault(value: unknown): boolean {
  return value === undefined || value === null || (typeof value === 'number' && Number.isNaN(value))
}

function defaultForInput(v: CatalogVariable): unknown {
  // A "boolean" FMI input (e.g. a genset start/breaker command) is driven
  // as 0/1 in the Constant field -- an <a-input-number> can't hold a real
  // `false`, which is what the catalog `default` carries, so it would blank
  // out on blur. Normalise to a number here.
  if (v.unit === 'boolean') return v.default === true || v.default === 1 ? 1 : 0
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
  scaffoldOutputs.value = false
  scaffoldedCount.value = 0
  form.model_type = modelType
  form.provider_type = model?.provider_type ?? 'fmu'
  form.parameters = {}
  form.mappings = {}
  form.inputSources = {}
  form.inputDefaults = {}
  form.aggregatePoints = {}
  form.aggregateOperation = {}
  form.aggregatePairs = {}
  form.inputExposures = {}
  weatherProvenance.value = null
  for (const p of model?.parameters ?? []) {
    if (p.default !== undefined) form.parameters[p.name] = p.default
  }
  for (const v of model?.inputs ?? []) {
    const matchingPoint = supportsInputSourcing(form.provider_type) ? matchingDevicePoint(v.name) : null
    form.inputSources[v.name] = matchingPoint ? 'point' : (supportsInputSourcing(form.provider_type) ? 'constant' : 'point')
    form.mappings[mappingKey(v)] = matchingPoint?.id
    form.inputDefaults[v.name] = configuredPointValue(matchingPoint) ?? defaultForInput(v)
    form.aggregatePoints[v.name] = []
    form.aggregateOperation[v.name] = 'max'
    form.aggregatePairs[v.name] = []
    form.inputExposures[v.name] = undefined
  }
  if (props.device && model) form.name = `${props.device.name} ${model.label}`
}

function resetForProvider(providerType: SimulationProviderType) {
  if (providerType === 'builtin') {
    form.provider_type = 'builtin'
    form.model_type = ''
    form.parameters = {}
    form.mappings = {}
    form.inputSources = {}
    form.inputDefaults = {}
    form.aggregatePoints = {}
    form.aggregateOperation = {}
    form.aggregatePairs = {}
    form.inputExposures = {}
    weatherProvenance.value = null
    scaffoldOutputs.value = false
    scaffoldedCount.value = 0
    return
  }
  form.provider_type = providerType
  const first = catalog.value.find(m => m.provider_type === providerType)
  if (first) resetForModel(first.model_type)
}

function hydrateFromSavedModel(saved: SimulationModelConfig) {
  savedModelId.value = saved.id
  scaffoldOutputs.value = false
  scaffoldedCount.value = 0
  loadRuntimeStatus()
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
  const aggregateOperation: Record<string, 'max' | 'min' | 'weighted_average'> = {}
  const aggregatePairs: Record<string, Array<{ value?: number; weight?: number }>> = {}
  for (const m of saved.mappings) {
    // Same "point_ids" (plural) vs "point_id" discriminator models.runtime.
    // _is_aggregate_row uses server-side -- keeps both ends of the contract
    // lexically matched.
    if ('point_ids' in m) {
      aggregatePoints[m.variable] = [...m.point_ids]
      aggregateOperation[m.variable] =
        m.operation === 'weighted_average' ? 'weighted_average' : m.operation === 'min' ? 'min' : 'max'
      aggregatePairs[m.variable] = m.operation === 'weighted_average'
        ? m.point_ids.map((pid, i) => ({ value: pid, weight: (m.weight_point_ids ?? [])[i] ?? undefined }))
        : []
    } else {
      mappings[`${m.direction}:${m.variable}`] = m.point_id
    }
  }
  for (const [variable, pointId] of Object.entries(draftOutputMappings)) {
    // draft_output_mappings is output-only (see buildPayload below) -- key
    // it to match.
    const key = `output:${variable}`
    if (mappings[key] == null) mappings[key] = pointId
  }
  form.mappings = mappings
  form.aggregatePoints = aggregatePoints
  form.aggregateOperation = aggregateOperation
  form.aggregatePairs = aggregatePairs
  form.inputExposures = {}
  for (const e of saved.input_exposures ?? []) {
    form.inputExposures[e.variable] = e.point_id
  }
  form.inputSources = {}
  for (const v of catalog.value.find(m => m.model_type === saved.model_type)?.inputs ?? []) {
    const savedSource = inputSources[v.name]
    form.inputSources[v.name] = savedSource === 'constant' || savedSource === 'point' || savedSource === 'aggregate'
      ? savedSource
      : (mappings[mappingKey(v)] != null ? 'point' : (aggregatePoints[v.name]?.length ? 'aggregate' : 'constant'))
    if (needsInputDefault(form.inputDefaults[v.name])) form.inputDefaults[v.name] = defaultForInput(v)
    // A "boolean"-unit input persisted as a real true/false (pre-numeric
    // configs) -> 0/1 so the Constant <a-input-number> can render it.
    if (v.unit === 'boolean' && typeof form.inputDefaults[v.name] === 'boolean') {
      form.inputDefaults[v.name] = form.inputDefaults[v.name] ? 1 : 0
    }
    if (form.aggregatePoints[v.name] === undefined) form.aggregatePoints[v.name] = []
    if (form.aggregateOperation[v.name] === undefined) form.aggregateOperation[v.name] = 'max'
    if (form.aggregatePairs[v.name] === undefined) form.aggregatePairs[v.name] = []
    if (form.inputExposures[v.name] === undefined) form.inputExposures[v.name] = undefined
    if (form.inputSources[v.name] === 'aggregate') void loadVariableCandidates(v)
  }
  void refreshWeatherProvenance()
}

// Every point id currently referenced anywhere in form -- plain
// mappings, Aggregate (max/min) members, Weighted Average value/weight
// pairs, and input-exposure targets. Used only to backfill labels for
// already-saved mappings that fall outside the topology-scoped `points`
// list (see backfillMissingPointOptions below); never used to constrain
// what a user can newly select.
function referencedPointIds(): number[] {
  const ids = new Set<number>()
  for (const id of Object.values(form.mappings)) if (id != null) ids.add(id)
  for (const list of Object.values(form.aggregatePoints)) for (const id of list) ids.add(id)
  for (const pairs of Object.values(form.aggregatePairs)) {
    for (const pair of pairs) {
      if (pair.value != null) ids.add(pair.value)
      if (pair.weight != null) ids.add(pair.weight)
    }
  }
  for (const id of Object.values(form.inputExposures)) if (id != null) ids.add(id)
  return [...ids]
}

// A saved model's mappings can reference points outside the just-loaded
// device's topology scope (points.value) -- e.g. a Weighted Average pair
// or "also write to" target picked before this device had a Controller/
// `feeds` topology, or a genuinely cross-branch mapping made via the
// unscoped picker previously. Without this, those ids have no matching
// option, and antd's <a-select> renders the bare numeric id instead of
// "Device / Name" (a mapping is never silently dropped by this -- it's
// purely a display/backfill step). Only appends entries points.value is
// missing; never removes/reorders what's already scoped in, so newly
// opened pickers still default to showing just the scoped candidates.
async function backfillMissingPointOptions() {
  const known = new Set(points.value.map(p => p.id))
  const missing = referencedPointIds().filter(id => !known.has(id))
  if (!missing.length) return
  try {
    const unscoped = await api.simulationModels.pointOptions()
    const missingSet = new Set(missing)
    points.value = [...points.value, ...unscoped.filter(p => missingSet.has(p.id))]
  } catch {
    // Best-effort label backfill -- leave the raw ids visible rather than
    // failing the whole drawer load over it.
  }
}

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    ;[providers.value, catalog.value, points.value] = await Promise.all([
      api.simulationProviders.catalog(),
      api.simulationModels.catalog(),
      api.simulationModels.pointOptions(props.device.id),
    ])
    savedModelId.value = null
    runtimeStatus.value = null

    // Per-controller state: if a model was saved for this source
    // controller, hydrate from DB. Existing/legacy FMU configs may only be
    // discoverable from the device's active output-owner summary, so fall
    // back to that model id before resetting to fresh defaults.
    const existing = await api.simulationModels.list(props.device.id)
    if (existing.length > 0) {
      hydrateFromSavedModel(existing[0])
      await backfillMissingPointOptions()
      return
    }
    const activeModelId = props.device.active_simulation_model?.id
    if (activeModelId != null) {
      try {
        hydrateFromSavedModel(await api.simulationModels.get(activeModelId))
        await backfillMissingPointOptions()
        return
      } catch {
        // The active-model summary is advisory; if it points at a deleted
        // row, keep opening a fresh drawer instead of blocking the user.
      }
    }

    resetForProvider('builtin')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load simulation model catalog')
    // providers.value/catalog.value are already [] here (the failed Promise.all
    // never assigned them), so the Provider select has zero options to pick a
    // label from -- left at its literal 'fmu' default, Ant's Select renders
    // that raw string instead of a blank field. Clear it explicitly so a
    // failed/unreachable catalog load reads as empty, not as a phantom
    // "fmu" selection nothing backs.
    form.provider_type = '' as SimulationProviderType
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
    if (v.required !== false && !form.mappings[mappingKey(v)]) {
      message.error(`${v.label} mapping is required`)
      return false
    }
  }
  for (const v of inputs.value) {
    if (!supportsInputSourcing(form.provider_type) || form.inputSources[v.name] === 'point') {
      if (v.required !== false && !form.mappings[mappingKey(v)]) {
        message.error(`${v.label} mapping is required`)
        return false
      }
    }
    if (supportsInputSourcing(form.provider_type) && form.inputSources[v.name] === 'aggregate') {
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
        const opLabel = operation === 'min' ? 'Minimum' : 'Maximum'
        message.error(`${v.label} requires at least one source point for the ${opLabel} aggregate`)
        return false
      }
    }
  }
  return true
}

function buildPayload(apply: boolean): SimulationModelPayload | null {
  if (!props.device || !selectedModel.value || form.provider_type === 'builtin') return null
  const modelName = `${props.device.name} ${selectedModel.value.label}`.trim()

  // Enabled/disabled is a persisted-state field, deliberately independent
  // of Save vs Apply (see toggleEnabled() below, the only thing allowed to
  // change it): editing an existing model always preserves its current
  // state; a brand-new model has no prior state to preserve, so Save
  // creates it disabled (configure first, enable later) and Create/Apply
  // creates it enabled -- same as today's create-time behavior.
  const enabled = savedModelId.value != null ? form.enabled : apply

  const parameters = { ...form.parameters }
  delete parameters.model
  delete parameters.runtime_url
  delete parameters.timeout_s
  if (supportsInputSourcing(form.provider_type)) {
    parameters.input_sources = Object.fromEntries(
      inputs.value.map(v => [v.name, form.inputSources[v.name] ?? 'constant']),
    )
    parameters.input_defaults = Object.fromEntries(
      inputs.value
        .filter(v => form.inputSources[v.name] !== 'point')
        // "boolean"-unit inputs are sent as 0/1 (the runtime takes floats) --
        // also coerces a legacy true/false saved before this field was numeric.
        .map(v => {
          const raw = form.inputDefaults[v.name]
          return [v.name, v.unit === 'boolean' ? (raw ? 1 : 0) : raw]
        }),
    )
  }
  if (!apply) {
    parameters.draft_output_mappings = Object.fromEntries(
      outputs.value
        .filter(v => form.mappings[mappingKey(v)] != null)
        .map(v => [v.name, form.mappings[mappingKey(v)]]),
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
      .filter(v => apply || v.direction !== 'output')
      .filter(v => v.direction === 'output' || form.inputSources[v.name] === 'point')
      .filter(v => form.mappings[mappingKey(v)] != null)
      .map(v => ({ variable: v.name, direction: v.direction, point_id: form.mappings[mappingKey(v)]! })),
    aggregate_mappings: inputs.value
      .filter(v => supportsInputSourcing(form.provider_type) && form.inputSources[v.name] === 'aggregate')
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
          operation: operation as 'max' | 'min',
          point_ids: form.aggregatePoints[v.name]!,
        }
      }),
    input_exposures: inputs.value
      .filter(v => form.inputExposures[v.name] != null)
      .map(v => ({ variable: v.name, point_id: form.inputExposures[v.name]! })),
  }
}

async function persist(apply: boolean) {
  if (form.provider_type === 'builtin') {
    if (savedModelId.value == null) { emit('update:open', false); return }
    const busy = apply ? saving : savingDraft
    busy.value = true
    try {
      await api.simulationModels.del(savedModelId.value)
      message.success('Simulation model removed -- device reverted to built-in behavior')
      emit('update:open', false)
      emit('saved')
    } catch (e: unknown) {
      message.error((e as Error).message ?? 'Failed to remove simulation model')
    } finally {
      busy.value = false
    }
    return
  }

  const payload = buildPayload(apply)
  if (!payload) return
  if (!validateMappings(apply)) return

  const busy = apply ? saving : savingDraft
  busy.value = true
  try {
    let saved: SimulationModelConfig
    if (savedModelId.value != null) {
      // apply threaded through to the query param -- see api.ts's own
      // comment. A plain save (apply=false) never touches the runtime
      // engine, whether this model is enabled or disabled.
      saved = await api.simulationModels.update(savedModelId.value, payload, apply)
      message.success(apply ? 'Simulation model applied' : 'Simulation model saved')
    } else {
      saved = await api.simulationModels.create(payload)
      message.success(apply ? 'Simulation model added' : 'Simulation model saved')
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

// ─── Enabled (ON/OFF SimEngine participation) ────────────────────────────
// Deliberately independent of persist()/buildPayload() above -- toggling
// this must never resend/re-validate mappings (see
// api.simulationModels.setEnabled's own comment), so a model already
// fully configured and applied can be switched off and back on without
// touching its saved configuration at all. Same minimal try/catch pattern
// as ScheduleDrawer.vue/CalendarDrawer.vue's own toggleEnabled.

const togglingEnabled = ref(false)

async function toggleEnabled(checked: boolean) {
  if (savedModelId.value == null) return
  togglingEnabled.value = true
  try {
    const updated = await api.simulationModels.setEnabled(savedModelId.value, checked)
    form.enabled = updated.enabled
    message.success(updated.enabled ? 'Simulation model enabled' : 'Simulation model disabled')
    await loadRuntimeStatus()
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to update enabled state')
  } finally {
    togglingEnabled.value = false
  }
}

function onMappingsApplied({ mappings, switchToPoint }: { mappings: Record<string, number>; switchToPoint: string[] }) {
  Object.assign(form.mappings, mappings)
  for (const variableName of switchToPoint) {
    form.inputSources[variableName] = 'point'
  }
}

function setInputSource(v: CatalogVariable, source: 'constant' | 'point' | 'aggregate') {
  form.inputSources[v.name] = source
  if (source === 'aggregate') void loadVariableCandidates(v)
  if (!exposureApplicable(v.name)) form.inputExposures[v.name] = undefined
}
</script>

<template>
  <a-drawer
    :open="open"
    :title="device ? `Simulation Model — ${device.name}` : 'Simulation Model'"
    width="520"
    :z-index="1050"
    :body-style="{ overflowX: 'hidden' }"
    @close="emit('update:open', false)"
  >
    <a-spin :spinning="loading">
      <a-form layout="vertical" :colon="false">
        <!-- Per-device runtime controls (Start/Pause/Reset) -- freezes/
             resumes/resets just THIS model's own provider, independent of
             the global simulation clock and separate from the Enabled
             switch in the footer (that fully unregisters/re-registers the
             provider; this freezes an already-registered one in place).
             Hidden entirely when nothing's actually registered (a disabled
             or never-applied model has no live provider to control). -->
        <a-form-item v-if="runtimeStatus" label="Runtime">
          <div style="display:flex;align-items:center;gap:8px">
            <a-tag :color="RUNTIME_STATUS_COLOR[runtimeStatus.status] ?? 'default'" style="text-transform:capitalize">
              {{ runtimeStatus.status.replace('_', ' ') }}
            </a-tag>
            <a-tooltip title="Resume">
              <a-button
                size="small" type="text" :disabled="runtimeStatus.status !== 'paused'" :loading="runtimeActionLoading"
                @click="resumeRuntime"
              >
                <template #icon><PlayCircleOutlined :style="{ color: runtimeStatus.status !== 'paused' ? '#555' : '#52c41a' }" /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip title="Pause -- freezes this model's own outputs in place; everything else keeps running">
              <a-button
                size="small" type="text" :disabled="runtimeStatus.status !== 'running'" :loading="runtimeActionLoading"
                @click="pauseRuntime"
              >
                <template #icon><PauseCircleOutlined :style="{ color: runtimeStatus.status !== 'running' ? '#555' : '#faad14' }" /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip title="Reset -- reinitializes this model's own runtime session fresh">
              <a-popconfirm title="Reset this model's runtime session?" @confirm="resetRuntime">
                <a-button size="small" type="text" :loading="runtimeActionLoading">
                  <template #icon><ReloadOutlined /></template>
                </a-button>
              </a-popconfirm>
            </a-tooltip>
            <span style="border-left:1px solid var(--border-color, #e0e0e0);height:16px;margin:0 2px" />
            <span style="font-size:12px;color:var(--text-muted)">RTF</span>
            <a-input-number
              v-model:value="rtfTargetInput"
              size="small" :min="0.01" :step="0.5" style="width:80px"
              :disabled="runtimeActionLoading"
              @change="setRtf"
            />
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">
            How much simulated time this model advances per shared tick, relative to every other
            model -- 1 = normal speed, 2 = twice as fast, 0.5 = half speed.
          </div>
        </a-form-item>

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

        <template v-if="form.provider_type !== 'builtin'">
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

        <template v-if="commonParameters.length">
          <a-divider orientation="left">Parameters</a-divider>
          <input
            ref="resourceFileInput"
            type="file"
            style="display:none"
            @change="onResourceFileChange"
          />
          <a-form-item v-for="p in commonParameters" :key="p.name" :label="p.label" :required="p.required">
            <a-select
              v-if="p.allowed_values?.length"
              v-model:value="(form.parameters[p.name] as unknown)"
              style="width:220px"
              :options="(p.allowed_values ?? []).map(v => ({ value: v.value as string | number, label: v.label }))"
            />
            <a-switch v-else-if="p.type === 'boolean'" v-model:checked="(form.parameters[p.name] as boolean)" />
            <div v-else-if="p.type === 'file'" style="display:flex;gap:10px;align-items:center">
              <a-button size="small" :loading="resourceUploading === p.name" @click="pickResourceFile(p.name)">
                <template #icon><UploadOutlined /></template>
                {{ form.parameters[p.name] ? 'Replace' : 'Upload' }}
              </a-button>
              <span v-if="form.parameters[p.name]" style="font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {{ resourceFileLabel(form.parameters[p.name]) }}
              </span>
            </div>
            <a-select
              v-else-if="p.type === 'month'"
              v-model:value="(form.parameters[p.name] as number)"
              style="width:220px"
            >
              <a-select-option v-for="m in 12" :key="m" :value="m">{{ monthOptionLabel(m) }}</a-select-option>
            </a-select>
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
        </template>

        <template v-if="appliedParameterOverrides.length">
          <a-divider orientation="left">Applied Calibration</a-divider>
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
            Parameter overrides from a calibration run (see Calibration). Read-only here --
            run a new calibration and apply it to change these.
          </div>
          <a-descriptions bordered size="small" :column="1" style="margin-bottom:16px">
            <a-descriptions-item v-for="o in appliedParameterOverrides" :key="o.name" :label="o.name">
              {{ o.value }}
            </a-descriptions-item>
          </a-descriptions>
        </template>

        <template v-if="supportsInputSourcing(form.provider_type)">
          <div v-if="inputs.length" style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);margin:16px 0 12px;padding-bottom:8px">
            <span style="font-size:14px;font-weight:600;color:var(--text-primary)">Inputs</span>
            <a-button size="small" :disabled="!selectedModel" @click="openMappingModal('input')">
              Auto Map
            </a-button>
          </div>
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
            <template v-if="form.inputSources[v.name] !== 'point' && form.inputSources[v.name] !== 'aggregate'">
              <BooleanToggle
                v-if="v.unit === 'boolean'"
                :value="form.inputDefaults[v.name] === 1"
                @update:value="(val: boolean) => { form.inputDefaults[v.name] = val ? 1 : 0 }"
              />
              <a-input-number
                v-else
                v-model:value="(form.inputDefaults[v.name] as number)"
                :addon-after="v.unit ?? undefined"
                style="width:100%"
              />
            </template>
            <a-alert
              v-if="inputMismatch(v)"
              type="warning"
              show-icon
              style="margin-top:8px"
              :message="`${inputMismatch(v)!.point.name} is ${inputMismatch(v)!.pointValue}${v.unit ? ` ${v.unit}` : ''}; this input is using constant ${inputMismatch(v)!.inputValue}${v.unit ? ` ${v.unit}` : ''}. Switch to Point if the model should use the BACnet setpoint.`"
            />
            <a-select
              v-if="form.inputSources[v.name] === 'point'"
              v-model:value="form.mappings[mappingKey(v)]"
              show-search allow-clear
              :options="pointOptions"
              option-filter-prop="label"
              :placeholder="`Select ${v.label} point`"
            />
            <template v-if="form.inputSources[v.name] === 'aggregate'">
              <div style="margin-bottom:8px">
                <div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">Operation:</div>
                <a-segmented
                  :value="form.aggregateOperation[v.name] ?? 'max'"
                  :options="[
                    { label: 'Maximum', value: 'max' },
                    { label: 'Minimum', value: 'min' },
                    { label: 'Weighted Average', value: 'weighted_average' },
                  ]"
                  size="small"
                  block
                  style="width:100%"
                  @change="(value: string | number) => onAggregateOperationChange(v.name, value as 'max' | 'min' | 'weighted_average')"
                />
              </div>

              <!-- Maximum/Minimum (and any future non-weighted operation): unchanged multi-select UI. -->
              <template v-if="(form.aggregateOperation[v.name] ?? 'max') !== 'weighted_average'">
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
                  {{ (form.aggregateOperation[v.name] ?? 'max') === 'min' ? 'Minimum' : 'Maximum' }} of {{ (form.aggregatePoints[v.name] ?? []).length }}
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
                    style="flex:1;min-width:0"
                  />
                  <a-select
                    v-model:value="pair.weight"
                    show-search allow-clear
                    :options="aggregatePointOptions(v.name)"
                    option-filter-prop="label"
                    placeholder="Select weight point"
                    style="flex:1;min-width:0"
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

            <div
              v-if="exposureApplicable(v.name)"
              style="margin-top:10px;padding-top:10px;border-top:1px dashed var(--border-color, #303030)"
            >
              <div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">
                Also write this resolved value to a point (optional)
              </div>
              <a-select
                v-model:value="form.inputExposures[v.name]"
                show-search allow-clear
                :options="numericPointOptions"
                option-filter-prop="label"
                placeholder="Not exposed"
                style="width:100%"
              />
            </div>
          </a-form-item>

          <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);margin:16px 0 12px;padding-bottom:8px">
            <span style="font-size:14px;font-weight:600;color:var(--text-primary)">Outputs</span>
            <a-button size="small" :disabled="!selectedModel" @click="openMappingModal('output')">
              Auto Map
            </a-button>
          </div>
        </template>

        <a-form-item
          v-for="v in (supportsInputSourcing(form.provider_type) ? outputs : variables)"
          :key="`${v.direction}:${v.name}`"
          :label="`${v.label} (${v.direction})`"
          :required="v.required !== false"
        >
          <a-select
            v-model:value="form.mappings[mappingKey(v)]"
            show-search allow-clear
            :options="pointOptions"
            option-filter-prop="label"
            :placeholder="`Select ${v.label} point`"
          />
          <div v-if="v.unit" style="font-size:11px;color:var(--text-muted);margin-top:3px">
            Expected unit: {{ v.unit }}
          </div>
        </a-form-item>
        </template>
      </a-form>
    </a-spin>

    <template #footer>
      <div
        v-if="form.provider_type !== 'builtin' && selectedModel && outputs.length"
        style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border-color, #303030)"
      >
        <a-checkbox
          v-model:checked="scaffoldOutputs"
          :disabled="scaffoldingPoints || scaffoldedCount > 0 || loading"
          @change="onScaffoldOutputsToggle"
        >
          Create a point on this controller for each output
        </a-checkbox>
        <a-spin v-if="scaffoldingPoints" size="small" />
        <span v-else-if="scaffoldedCount > 0" style="font-size:12px;color:var(--text-muted)">
          {{ scaffoldedCount }} created — review below and Apply
        </span>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;width:100%">
        <div style="display:flex;align-items:center;gap:8px">
          <a-switch
            :checked="form.enabled"
            :loading="togglingEnabled"
            :disabled="savedModelId == null || loading"
            title="Enabled = participates in SimEngine execution. Disabling preserves all configuration and mappings."
            @change="toggleEnabled"
          />
          <span style="font-size:12.5px;color:var(--text-secondary)">
            {{ form.enabled ? 'Enabled' : 'Disabled' }}
          </span>
        </div>
        <a-space>
          <a-button @click="emit('update:open', false)">Close</a-button>
          <a-button
            :loading="savingDraft"
            :disabled="loading || saving || (form.provider_type === 'builtin' ? savedModelId == null : !selectedModel)"
            @click="saveDraft"
          >
            Save
          </a-button>
          <a-button
            type="primary"
            :loading="saving"
            :disabled="loading || savingDraft || (form.provider_type === 'builtin' ? savedModelId == null : !selectedModel)"
            @click="applyModel"
          >
            {{ primaryActionLabel }}
          </a-button>
        </a-space>
      </div>
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
    :direction="mappingModalDirection"
    @apply="onMappingsApplied"
  />
</template>
