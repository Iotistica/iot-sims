<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type {
  SimulationModelCatalogEntry,
  SimulationModelConfig,
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
}
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

const form = reactive({
  provider_type: 'fmu' as SimulationModelPayload['provider_type'],
  model_type: '',
  name: '',
  enabled: true,
  parameters: {} as Record<string, unknown>,
  mappings: {} as Record<string, number | undefined>,
  inputSources: {} as Record<string, 'constant' | 'point'>,
  inputDefaults: {} as Record<string, unknown>,
})

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
const pointOptions = computed(() => points.value.map(p => ({
  value: p.id,
  label: p.device_name ? `${p.device_name} / ${p.name}` : p.name,
})))

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
  if (form.provider_type !== 'fmu' || form.inputSources[v.name] === 'point') return null
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
  advancedOpen.value = []
  for (const p of model?.parameters ?? []) {
    if (p.default !== undefined) form.parameters[p.name] = p.default
  }
  for (const v of model?.inputs ?? []) {
    const matchingPoint = form.provider_type === 'fmu' ? matchingDevicePoint(v.name) : null
    form.inputSources[v.name] = matchingPoint ? 'point' : (form.provider_type === 'fmu' ? 'constant' : 'point')
    form.mappings[v.name] = matchingPoint?.id
    form.inputDefaults[v.name] = configuredPointValue(matchingPoint) ?? defaultForInput(v)
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
  const inputSources = { ...((parameters.input_sources as Record<string, 'constant' | 'point'> | undefined) ?? {}) }
  delete parameters.input_defaults
  delete parameters.input_sources
  form.parameters = parameters
  form.inputDefaults = inputDefaults

  const mappings: Record<string, number | undefined> = {}
  for (const m of saved.mappings) mappings[m.variable] = m.point_id
  form.mappings = mappings
  form.inputSources = {}
  for (const v of catalog.value.find(m => m.model_type === saved.model_type)?.inputs ?? []) {
    const savedSource = inputSources[v.name]
    form.inputSources[v.name] = savedSource === 'constant' || savedSource === 'point'
      ? savedSource
      : (mappings[v.name] != null ? 'point' : 'constant')
    if (form.inputDefaults[v.name] === undefined) form.inputDefaults[v.name] = defaultForInput(v)
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

async function save() {
  if (!props.device || !selectedModel.value) return
  const modelName = `${props.device.name} ${selectedModel.value.label}`.trim()

  for (const v of outputs.value) {
    if (v.required !== false && !form.mappings[v.name]) {
      return void message.error(`${v.label} mapping is required`)
    }
  }
  for (const v of inputs.value) {
    if (form.provider_type !== 'fmu' || form.inputSources[v.name] === 'point') {
      if (v.required !== false && !form.mappings[v.name]) {
        return void message.error(`${v.label} mapping is required`)
      }
    }
  }

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

  const payload = {
    name: modelName,
    provider_type: form.provider_type,
    model_type: form.model_type,
    enabled: form.enabled,
    created_from_device_id: props.device.id,
    parameters,
    mappings: variables.value
      .filter(v => v.direction === 'output' || form.inputSources[v.name] === 'point' || form.provider_type !== 'fmu')
      .filter(v => form.mappings[v.name] != null)
      .map(v => ({ variable: v.name, direction: v.direction, point_id: form.mappings[v.name]! })),
  }

  saving.value = true
  try {
    if (savedModelId.value != null) {
      await api.simulationModels.update(savedModelId.value, payload)
      message.success('Simulation model updated')
    } else {
      await api.simulationModels.create(payload)
      message.success('Simulation model added')
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to add simulation model')
  } finally {
    saving.value = false
  }
}

function onMappingsApplied({ mappings, switchToPoint }: { mappings: Record<string, number>; switchToPoint: string[] }) {
  Object.assign(form.mappings, mappings)
  for (const variableName of switchToPoint) {
    form.inputSources[variableName] = 'point'
  }
}

function setInputSource(variableName: string, source: 'constant' | 'point') {
  form.inputSources[variableName] = source
}
</script>

<template>
  <a-drawer
    :open="open"
    :title="device ? `Simulation Model — ${device.name}` : 'Simulation Model'"
    width="520"
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
              ]"
              style="margin-bottom:8px"
              @change="value => setInputSource(v.name, value as 'constant' | 'point')"
            />
            <a-input-number
              v-if="form.inputSources[v.name] !== 'point'"
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

        <a-form-item label="Enabled" style="margin-top:16px;margin-bottom:0">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>
      </a-form>
    </a-spin>

    <template #footer>
      <a-space>
        <a-button @click="emit('update:open', false)">Close</a-button>
        <a-button type="primary" :loading="saving" :disabled="loading || !selectedModel" @click="save">
          Create
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
