<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type {
  SimulationModelCatalogEntry,
  SimulationModelConfig,
  SimulationModelPayload,
  SimulationModelPointOption,
} from '../api'
import type { Device } from '../types'

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
const points = ref<PointOption[]>([])
const advancedOpen = ref<string[]>([])

const form = reactive({
  provider_type: 'system' as SimulationModelPayload['provider_type'],
  model_type: '',
  name: '',
  enabled: true,
  parameters: {} as Record<string, unknown>,
  mappings: {} as Record<string, number | undefined>,
})

const selectedModel = computed(() => catalog.value.find(m => m.model_type === form.model_type) ?? null)
const commonParameters = computed(() => selectedModel.value?.parameters.filter(p => !p.advanced) ?? [])
const advancedParameters = computed(() => selectedModel.value?.parameters.filter(p => p.advanced) ?? [])
const variables = computed(() => [...(selectedModel.value?.inputs ?? []), ...(selectedModel.value?.outputs ?? [])])
const pointOptions = computed(() => points.value.map(p => ({
  value: p.id,
  label: p.device_name ? `${p.device_name} / ${p.name}` : p.name,
})))

function resetForModel(modelType: string) {
  const model = catalog.value.find(m => m.model_type === modelType)
  form.model_type = modelType
  form.provider_type = model?.provider_type ?? 'system'
  form.parameters = {}
  form.mappings = {}
  advancedOpen.value = []
  for (const p of model?.parameters ?? []) {
    if (p.default !== undefined) form.parameters[p.name] = p.default
  }
  if (props.device && model) form.name = `${props.device.name} ${model.label}`
}

function hydrateFromSavedModel(saved: SimulationModelConfig) {
  form.model_type = saved.model_type
  form.provider_type = saved.provider_type
  form.name = saved.name
  form.enabled = saved.enabled
  form.parameters = { ...saved.parameters }

  const mappings: Record<string, number | undefined> = {}
  for (const m of saved.mappings) mappings[m.variable] = m.point_id
  form.mappings = mappings
  advancedOpen.value = []
}

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    catalog.value = await api.simulationModels.catalog()
    points.value = await api.simulationModels.pointOptions()

    // Per-controller state: if a model was already saved for this source
    // controller, hydrate from DB. Otherwise reset to fresh defaults.
    const existing = await api.simulationModels.list(props.device.id)
    if (existing.length > 0) {
      hydrateFromSavedModel(existing[0])
      return
    }

    const first = catalog.value.find(m => m.provider_type === 'system') ?? catalog.value[0]
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
  if (!form.name.trim()) return void message.error('Model Name is required')

  for (const v of variables.value) {
    if (v.required !== false && !form.mappings[v.name]) {
      return void message.error(`${v.label} mapping is required`)
    }
  }

  saving.value = true
  try {
    await api.simulationModels.create({
      name: form.name.trim(),
      provider_type: form.provider_type,
      model_type: form.model_type,
      enabled: form.enabled,
      created_from_device_id: props.device.id,
      parameters: { ...form.parameters },
      mappings: variables.value
        .filter(v => form.mappings[v.name] != null)
        .map(v => ({ variable: v.name, direction: v.direction, point_id: form.mappings[v.name]! })),
    })
    message.success('Simulation model added')
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to add simulation model')
  } finally {
    saving.value = false
  }
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
        <a-form-item label="Model Type" required>
          <a-select
            v-model:value="form.model_type"
            :options="catalog.map(m => ({ value: m.model_type, label: m.label }))"
            @change="resetForModel"
          />
          <div v-if="selectedModel?.description" style="font-size:12px;color:var(--text-muted);margin-top:5px">
            {{ selectedModel.description }}
          </div>
        </a-form-item>

        <a-form-item label="Model Name" required>
          <a-input v-model:value="form.name" placeholder="e.g. AHU Supply Air" />
        </a-form-item>

        <a-divider orientation="left">Parameters</a-divider>
        <a-form-item v-for="p in commonParameters" :key="p.name" :label="p.label" :required="p.required">
          <a-switch v-if="p.type === 'boolean'" v-model:checked="(form.parameters[p.name] as boolean)" />
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

        <a-divider orientation="left">Point Mapping</a-divider>
        <a-alert
          type="info"
          show-icon
          style="margin-bottom:14px"
          message="Inputs may come from other controllers. Output points are owned by this model while it is enabled."
        />
        <a-form-item
          v-for="v in variables"
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
</template>
