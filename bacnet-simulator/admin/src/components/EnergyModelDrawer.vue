<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, EnergyModelConfig, Meta } from '../types'

const props = defineProps<{ open: boolean; device: Device | null; meta: Meta }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

// Suggestion only -- pre-fills the Model Type dropdown when adding a new
// config for a device whose equipment_type (Brick class) has an obvious
// energy-model counterpart. Nothing here auto-creates or auto-enables a
// config; Brick answers "what type of equipment is this", this drawer
// answers "how is its energy calculated" -- kept fully independent.
const EQUIPMENT_TYPE_SUGGESTION: Record<string, EnergyModelConfig['model_type']> = {
  Air_Handling_Unit: 'ahu',
  Chiller: 'chiller',
  Boiler: 'boiler',
  Lighting_Equipment: 'lighting',
}

interface FieldDef {
  key: string
  label: string
  suffix?: string
  min?: number
  max?: number
  step?: number
  required?: boolean
  boolean?: boolean
}

const CHILLER_COMMON: FieldDef[] = [
  { key: 'full_load_cop', label: 'Full-Load COP', min: 0, step: 0.1 },
  { key: 'rated_electrical_power_kw', label: 'Rated Electrical Power', suffix: 'kW', min: 0, step: 1 },
]
const CHILLER_ADVANCED: FieldDef[] = [
  { key: 'iplv_cop', label: 'IPLV COP', min: 0, step: 0.1 },
  { key: 'full_load_kw_per_ton', label: 'Full-Load kW/ton', min: 0, step: 0.01 },
  { key: 'running_power_fraction', label: 'Running Power Fraction', min: 0, max: 1.5, step: 0.01 },
  { key: 'minimum_load_fraction', label: 'Minimum Load Fraction', min: 0, max: 1, step: 0.01 },
  { key: 'maximum_load_fraction', label: 'Maximum Load Fraction', min: 0, max: 1.5, step: 0.01 },
  { key: 'include_auxiliary_power_kw', label: 'Auxiliary Power', suffix: 'kW', min: 0, step: 0.1 },
]

const BOILER_COMMON: FieldDef[] = [
  { key: 'rated_thermal_capacity_kw', label: 'Rated Thermal Capacity', suffix: 'kW', min: 0, step: 1 },
  { key: 'thermal_efficiency', label: 'Thermal Efficiency', min: 0, max: 1, step: 0.01 },
]
const BOILER_ADVANCED: FieldDef[] = [
  { key: 'rated_fuel_input_kw', label: 'Rated Fuel Input', suffix: 'kW', min: 0, step: 1 },
  { key: 'auxiliary_electric_power_kw', label: 'Auxiliary Electric Power', suffix: 'kW', min: 0, step: 0.1 },
  { key: 'natural_gas_kwh_per_cubic_meter', label: 'Natural Gas', suffix: 'kWh/m³', min: 0, step: 0.01 },
  { key: 'running_fuel_fraction', label: 'Running Fuel Fraction', min: 0, max: 1.5, step: 0.01 },
  { key: 'minimum_firing_fraction', label: 'Minimum Firing Fraction', min: 0, max: 1, step: 0.01 },
  { key: 'maximum_firing_fraction', label: 'Maximum Firing Fraction', min: 0, max: 1.5, step: 0.01 },
]

const AHU_COMMON: FieldDef[] = [
  { key: 'supply_fan_rated_power_kw', label: 'Supply Fan Rated Power', suffix: 'kW', min: 0, step: 0.1 },
  { key: 'return_fan_rated_power_kw', label: 'Return Fan Rated Power', suffix: 'kW', min: 0, step: 0.1 },
]
const AHU_ADVANCED: FieldDef[] = [
  { key: 'fan_power_exponent', label: 'Fan Power Exponent', min: 0, step: 0.1 },
  { key: 'minimum_fan_power_fraction', label: 'Minimum Fan Power Fraction', min: 0, max: 1, step: 0.01 },
  { key: 'supply_fan_running_fraction', label: 'Supply Fan Running Fraction', min: 0, max: 1, step: 0.01 },
  { key: 'return_fan_running_fraction', label: 'Return Fan Running Fraction', min: 0, max: 1, step: 0.01 },
  { key: 'cooling_efficiency_cop', label: 'Cooling Efficiency (COP)', min: 0, step: 0.1 },
  { key: 'heating_efficiency', label: 'Heating Efficiency', min: 0, max: 1, step: 0.01 },
  { key: 'auxiliary_power_kw', label: 'Auxiliary Power', suffix: 'kW', min: 0, step: 0.1 },
  { key: 'include_coil_energy', label: 'Include Coil Energy', boolean: true },
]

const LIGHTING_COMMON: FieldDef[] = [
  { key: 'rated_power_kw', label: 'Rated Power', suffix: 'kW', min: 0, step: 0.1, required: true },
  { key: 'standby_power_kw', label: 'Standby Power', suffix: 'kW', min: 0, step: 0.01 },
]
const LIGHTING_ADVANCED: FieldDef[] = [
  { key: 'minimum_dimmed_power_fraction', label: 'Minimum Dimmed Power Fraction', min: 0, max: 1, step: 0.01 },
  { key: 'dimming_exponent', label: 'Dimming Exponent', min: 0, step: 0.1 },
  { key: 'occupied_default_level_percent', label: 'Occupied Default Level', suffix: '%', min: 0, max: 100, step: 1 },
]

const FIELD_DEFS: Record<EnergyModelConfig['model_type'], { common: FieldDef[]; advanced: FieldDef[] }> = {
  chiller: { common: CHILLER_COMMON, advanced: CHILLER_ADVANCED },
  boiler: { common: BOILER_COMMON, advanced: BOILER_ADVANCED },
  ahu: { common: AHU_COMMON, advanced: AHU_ADVANCED },
  lighting: { common: LIGHTING_COMMON, advanced: LIGHTING_ADVANCED },
}

// Every model type allows multiple named instances per device (e.g.
// scenario-comparison chiller configs "Baseline"/"Efficient"/"Degraded",
// or multiple lighting zones "zone-a"/"zone-b") -- instance_key is always
// shown, for every type, labeled "Model Name" rather than the internal
// term. No cardinality restriction here; see registry.py.
function suggestedInstanceKey(modelType: EnergyModelConfig['model_type']): string {
  const hasExisting = list.value.some(c => c.model_type === modelType)
  return hasExisting ? '' : 'Baseline'
}

const list = ref<EnergyModelConfig[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<EnergyModelConfig | null>(null)
const formOpen = ref(false)
const advancedOpen = ref<string[]>([])

const form = reactive({
  model_type: 'chiller' as EnergyModelConfig['model_type'],
  instance_key: 'default',
  enabled: true,
  values: {} as Record<string, number | boolean | null | undefined>,
})

// Chiller-only: rated_capacity_kw / rated_capacity_tons are alternates on
// the backend (ChillerEnergyConfig.capacity_kw resolves whichever is set)
// -- presented here as one value + a unit toggle instead of two raw fields.
const capacityUnit = ref<'kw' | 'tons'>('kw')
const capacityValue = ref<number | null>(null)

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    list.value = await api.energyModels.list(props.device.id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load energy models')
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  if (v) load()
  else { formOpen.value = false; editing.value = null }
})

function resetForm(modelType: EnergyModelConfig['model_type']) {
  form.model_type = modelType
  form.instance_key = suggestedInstanceKey(modelType)
  form.enabled = true
  form.values = {}
  capacityUnit.value = 'kw'
  capacityValue.value = null
  advancedOpen.value = []
}

function openAdd() {
  editing.value = null
  const suggested = props.device?.equipment_type
    ? EQUIPMENT_TYPE_SUGGESTION[props.device.equipment_type]
    : undefined
  resetForm(suggested ?? 'chiller')
  formOpen.value = true
}

function openEdit(config: EnergyModelConfig) {
  editing.value = config
  form.model_type = config.model_type
  form.instance_key = config.instance_key
  form.enabled = config.enabled
  form.values = { ...config.parameters }
  advancedOpen.value = []

  if (config.model_type === 'chiller') {
    if (config.parameters.rated_capacity_tons != null) {
      capacityUnit.value = 'tons'
      capacityValue.value = config.parameters.rated_capacity_tons as number
    } else {
      capacityUnit.value = 'kw'
      capacityValue.value = (config.parameters.rated_capacity_kw as number) ?? null
    }
  }

  formOpen.value = true
}

function onModelTypeChange() {
  // Switching type mid-form starts that type's fields fresh -- a chiller
  // COP doesn't mean anything carried over onto a lighting config.
  form.values = {}
  capacityValue.value = null
  advancedOpen.value = []
  // Only re-suggest a name when adding a brand new config -- switching
  // type while editing an existing row shouldn't clobber its real name.
  if (!editing.value) {
    form.instance_key = suggestedInstanceKey(form.model_type)
  }
}

const commonFields = computed(() => FIELD_DEFS[form.model_type].common)
const advancedFields = computed(() => FIELD_DEFS[form.model_type].advanced)

function summaryLine(config: EnergyModelConfig): string {
  const defs = FIELD_DEFS[config.model_type]
  const first = defs.common.find(f => !f.boolean && config.parameters[f.key] != null)
  if (!first) return ''
  const value = config.parameters[first.key]
  return `${first.label}: ${value}${first.suffix ? ' ' + first.suffix : ''}`
}

function confirmDelete(config: EnergyModelConfig) {
  Modal.confirm({
    title: `Delete this ${config.model_type} energy model?`,
    content: 'This device will stop contributing to energy analytics for this model/instance until a new one is configured.',
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.energyModels.del(config.id)
        message.success('Deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Delete failed')
      }
    },
  })
}

async function save() {
  if (!props.device) return

  const instanceKey = form.instance_key.trim()
  if (!instanceKey) {
    message.error('Model Name is required')
    return
  }

  const parameters: Record<string, number | boolean | null | undefined> = { ...form.values }

  if (form.model_type === 'chiller') {
    delete parameters.rated_capacity_kw
    delete parameters.rated_capacity_tons
    if (capacityValue.value != null) {
      parameters[capacityUnit.value === 'kw' ? 'rated_capacity_kw' : 'rated_capacity_tons'] = capacityValue.value
    }
  }

  if (form.model_type === 'lighting' && !parameters.rated_power_kw) {
    message.error('Rated Power is required')
    return
  }

  saving.value = true
  try {
    const body = { model_type: form.model_type, instance_key: instanceKey, enabled: form.enabled, parameters }
    if (editing.value) {
      await api.energyModels.update(editing.value.id, body)
      message.success('Energy model updated')
    } else {
      await api.energyModels.create(props.device.id, body)
      message.success('Energy model configured')
    }
    formOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Save failed')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <a-drawer
    :title="device ? `Energy Model — ${device.name}` : 'Energy Model'"
    :open="open"
    width="520"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <a-button type="primary" block @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Add Energy Model
      </a-button>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:var(--text-placeholder);padding:40px 0;font-size:13px">
          No energy models configured for this device yet — nothing here will show up in Utilities/Analytics until one is added.
        </div>
        <div
          v-for="config in list" :key="config.id"
          style="border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px;color:var(--text-primary)">
                {{ meta.energy_model_types.find(t => t.value === config.model_type)?.label ?? config.model_type }}
                <span v-if="config.instance_key !== 'default'" style="font-weight:400;color:var(--text-secondary)"> / {{ config.instance_key }}</span>
                <a-tag :color="config.enabled ? 'green' : 'default'" style="margin-left:8px">{{ config.enabled ? 'Enabled' : 'Disabled' }}</a-tag>
              </div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">{{ summaryLine(config) }}</div>
            </div>
            <a-space :size="4">
              <a-button size="small" title="Edit" @click="openEdit(config)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(config)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-space>
          </div>
        </div>
      </a-spin>
    </template>

    <template v-else>
      <a-form layout="vertical" :colon="false">
        <a-form-item label="Model Type" required>
          <a-select v-model:value="form.model_type" :options="meta.energy_model_types" @change="onModelTypeChange" />
        </a-form-item>

        <a-form-item label="Model Name" required help="Distinguishes multiple energy models of this type on the same device, e.g. &quot;Baseline&quot; vs &quot;Efficient&quot; for comparison scenarios, or zone-a / zone-b for multiple lighting zones.">
          <a-input v-model:value="form.instance_key" placeholder="e.g. Baseline" />
        </a-form-item>

        <template v-if="form.model_type === 'chiller'">
          <a-form-item label="Rated Capacity">
            <a-input-group compact style="display:flex">
              <a-input-number v-model:value="capacityValue" :min="0" style="flex:1" />
              <a-select v-model:value="capacityUnit" style="width:90px">
                <a-select-option value="kw">kW</a-select-option>
                <a-select-option value="tons">tons</a-select-option>
              </a-select>
            </a-input-group>
          </a-form-item>
        </template>

        <a-form-item v-for="f in commonFields" :key="f.key" :label="f.label" :required="f.required">
          <a-checkbox v-if="f.boolean" v-model:checked="(form.values[f.key] as boolean)" />
          <a-input-number
            v-else
            v-model:value="(form.values[f.key] as number)"
            :min="f.min" :max="f.max" :step="f.step ?? 1"
            :addon-after="f.suffix"
            style="width:100%"
          />
        </a-form-item>

        <a-collapse v-model:activeKey="advancedOpen" ghost style="margin-top:4px">
          <a-collapse-panel key="advanced" header="Advanced">
            <a-form-item v-for="f in advancedFields" :key="f.key" :label="f.label">
              <a-checkbox v-if="f.boolean" v-model:checked="(form.values[f.key] as boolean)" />
              <a-input-number
                v-else
                v-model:value="(form.values[f.key] as number)"
                :min="f.min" :max="f.max" :step="f.step ?? 1"
                :addon-after="f.suffix"
                style="width:100%"
              />
            </a-form-item>
          </a-collapse-panel>
        </a-collapse>

        <a-form-item label="Enabled" style="margin-top:16px;margin-bottom:0">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>
      </a-form>
    </template>

    <template #footer>
      <a-space v-if="formOpen">
        <a-button @click="formOpen = false">Cancel</a-button>
        <a-button type="primary" :loading="saving" @click="save">{{ editing ? 'Save' : 'Create' }}</a-button>
      </a-space>
      <a-button v-else @click="emit('update:open', false)">Close</a-button>
    </template>
  </a-drawer>
</template>
