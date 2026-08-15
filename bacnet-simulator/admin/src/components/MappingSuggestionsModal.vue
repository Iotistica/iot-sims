<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { BulbOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { MappingSuggestionEntry, SimulationModelPointOption, SimulationProviderType } from '../api'
import type { Device } from '../types'

interface ModalVariable {
  name: string
  label: string
  direction: 'input' | 'output'
  unit?: string | null
}

interface ModalModel {
  model_type: string
  label: string
  inputs: ModalVariable[]
  outputs: ModalVariable[]
}

const props = defineProps<{
  open: boolean
  device: Device | null
  model: ModalModel | null
  providerType: SimulationProviderType
  pointOptions: SimulationModelPointOption[]
  currentMappings: Record<string, number | undefined>
  currentModelId?: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  apply: [payload: { mappings: Record<string, number>; switchToPoint: string[] }]
}>()

const loading = ref(false)
const applying = ref(false)

interface VariableRow {
  variable: ModalVariable
  suggestion: MappingSuggestionEntry
  included: boolean
  chosenPointId: number | undefined
  aiLoading: boolean
}
const rows = ref<VariableRow[]>([])

// High/medium default checked; low/none default unchecked -- same rule as
// SemanticSuggestionsModal's defaultIncluded, minus the "existing_class"
// carve-out (mapping suggestions don't replace an existing classification,
// they only prefill the drawer's own in-memory form).
function defaultIncluded(entry: MappingSuggestionEntry): boolean {
  return entry.confidence === 'high' || entry.confidence === 'medium'
}

function pointOptionsList() {
  return props.pointOptions.map(p => ({
    value: p.id,
    label: p.device_name ? `${p.device_name} / ${p.name}` : p.name,
  }))
}

function emptySuggestion(variable: ModalVariable): MappingSuggestionEntry {
  return {
    variable: variable.name, direction: variable.direction,
    suggested_point_id: null, suggested_point_name: null,
    confidence: 'none', score: 0, reasons: [], alternatives: [],
    source: 'rule', equipment_scope_used: 'self', related_equipment_name: null,
  }
}

watch(() => props.open, async (v) => {
  if (!v || !props.device || !props.model) return
  loading.value = true
  rows.value = []
  try {
    const result = await api.simulationModels.mappingSuggestions({
      model_type: props.model.model_type,
      provider_type: props.providerType,
      created_from_device_id: props.device.id,
      current_model_id: props.currentModelId ?? null,
    })
    const suggestionByVariable = new Map(result.variables.map(v => [v.variable, v]))
    const variables: ModalVariable[] = [...props.model.inputs, ...props.model.outputs]

    rows.value = variables.map((variable) => {
      const suggestion = suggestionByVariable.get(variable.name) ?? emptySuggestion(variable)
      const existingPointId = props.currentMappings[variable.name]
      return {
        variable,
        suggestion,
        included: defaultIncluded(suggestion),
        chosenPointId: existingPointId ?? suggestion.suggested_point_id ?? undefined,
        aiLoading: false,
      }
    })
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load mapping suggestions')
  } finally {
    loading.value = false
  }
})

const CONFIDENCE_COLOR: Record<string, string> = { high: 'green', medium: 'blue', low: 'orange', none: 'default' }

function reasonText(entry: MappingSuggestionEntry): string {
  if (!entry.reasons.length) return 'No suggestion'
  return entry.reasons.slice(0, 2).join('; ')
}

function crossDeviceTag(row: VariableRow): string | null {
  if (!row.suggestion.related_equipment_name) return null
  if (row.suggestion.equipment_scope_used !== 'upstream' && row.suggestion.equipment_scope_used !== 'downstream') return null
  return row.suggestion.related_equipment_name
}

function selectedFromCurrentMapping(row: VariableRow): boolean {
  return row.chosenPointId != null
    && props.currentMappings[row.variable.name] === row.chosenPointId
    && row.suggestion.suggested_point_id !== row.chosenPointId
}

function showUseAi(entry: MappingSuggestionEntry): boolean {
  return entry.confidence === 'low' || entry.confidence === 'none'
}

async function useAi(row: VariableRow) {
  if (!props.device || !props.model) return
  row.aiLoading = true
  try {
    const result = await api.simulationModels.mappingSuggestionAi({
      model_type: props.model.model_type,
      variable: row.variable.name,
      created_from_device_id: props.device.id,
      current_model_id: props.currentModelId ?? null,
    })
    row.suggestion = result
    row.chosenPointId = result.suggested_point_id ?? row.chosenPointId
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'AI suggestion is unavailable. Check Azure OpenAI configuration.')
  } finally {
    row.aiLoading = false
  }
}

const columns: TableColumnsType<VariableRow> = [
  { title: '', key: 'included', width: 36 },
  { title: 'Model Variable', key: 'variable', width: 200 },
  { title: 'Suggested Point', key: 'point' },
  { title: 'Confidence', key: 'confidence', width: 120 },
  { title: 'Reason', key: 'reason' },
  { title: '', key: 'ai', width: 90 },
]

function apply() {
  const mappings: Record<string, number> = {}
  const switchToPoint: string[] = []

  for (const row of rows.value) {
    if (!row.included || !row.chosenPointId) continue
    mappings[row.variable.name] = row.chosenPointId
    if (props.providerType === 'fmu' && row.variable.direction === 'input') {
      switchToPoint.push(row.variable.name)
    }
  }

  applying.value = true
  try {
    emit('apply', { mappings, switchToPoint })
    emit('update:open', false)
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    :title="`Mapping Suggestions — ${model?.label ?? ''}`"
    width="860px"
    ok-text="Apply Selected"
    :confirm-loading="applying"
    :ok-button-props="{ disabled: loading }"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="apply"
  >
    <a-spin :spinning="loading">
      <a-table
        :data-source="rows"
        :columns="columns"
        :pagination="false"
        :show-sorter-tooltip="false"
        size="small"
        row-key="variable.name"
      >
        <template #emptyText>
          <div style="padding:16px;color:var(--text-placeholder)">No model variables to map</div>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'included'">
            <a-checkbox
              v-model:checked="(record as VariableRow).included"
              :disabled="!(record as VariableRow).chosenPointId"
            />
          </template>
          <template v-else-if="column.key === 'variable'">
            <div style="font-weight:600">{{ (record as VariableRow).variable.label }}</div>
            <div style="font-size:11px;color:var(--text-secondary)">
              {{ (record as VariableRow).variable.direction }}<template v-if="(record as VariableRow).variable.unit"> · {{ (record as VariableRow).variable.unit }}</template>
            </div>
          </template>
          <template v-else-if="column.key === 'point'">
            <a-select
              v-model:value="(record as VariableRow).chosenPointId"
              show-search
              allow-clear
              size="small"
              :options="pointOptionsList()"
              option-filter-prop="label"
              placeholder="Select a point"
              style="width:100%"
            />
            <a-tag v-if="crossDeviceTag(record as VariableRow)" color="blue" style="margin-top:4px">
              {{ (record as VariableRow).suggestion.equipment_scope_used === 'upstream' ? 'Upstream' : 'Downstream' }}: {{ crossDeviceTag(record as VariableRow) }}
            </a-tag>
            <a-tag v-else-if="selectedFromCurrentMapping(record as VariableRow)" style="margin-top:4px">
              Current mapping
            </a-tag>
          </template>
          <template v-else-if="column.key === 'confidence'">
            <a-tag :color="CONFIDENCE_COLOR[(record as VariableRow).suggestion.confidence]" style="margin:0">{{ (record as VariableRow).suggestion.confidence }}</a-tag>
            <a-tag v-if="(record as VariableRow).suggestion.source === 'ai'" color="purple" style="margin:0 0 0 4px">AI</a-tag>
          </template>
          <template v-else-if="column.key === 'reason'">
            <span style="font-size:12px;color:var(--text-secondary)">{{ reasonText((record as VariableRow).suggestion) }}</span>
          </template>
          <template v-else-if="column.key === 'ai'">
            <a-button v-if="showUseAi((record as VariableRow).suggestion)" size="small" :loading="(record as VariableRow).aiLoading" @click="useAi(record as VariableRow)">
              <template #icon><BulbOutlined /></template>
              Use AI
            </a-button>
          </template>
        </template>
      </a-table>
    </a-spin>
  </a-modal>
</template>
