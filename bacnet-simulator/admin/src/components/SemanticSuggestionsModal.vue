<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { BulbOutlined, DownOutlined, RightOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, SimObject, Meta, SemanticSuggestionEntry } from '../types'

const props = defineProps<{
  open: boolean
  device: Device | null
  objects: SimObject[]
  meta: Meta
}>()
const emit = defineEmits<{ 'update:open': [v: boolean]; applied: [] }>()

const loading = ref(false)
const applying = ref(false)

const deviceSuggestion = ref<SemanticSuggestionEntry | null>(null)
const deviceIncluded = ref(false)
const deviceChosenClass = ref<string | null>(null)

interface PointRow {
  suggestion: SemanticSuggestionEntry
  included: boolean
  chosenClass: string | null
  aiLoading: boolean
}
const pointRows = ref<PointRow[]>([])
const unclassifiedRows = ref<PointRow[]>([])
const showUnclassified = ref(false)

const selectablePointRows = computed(() =>
  pointRows.value.filter((row) => row.suggestion.existing_class === null && !!row.chosenClass),
)

const selectedPointRowsCount = computed(() =>
  selectablePointRows.value.filter((row) => row.included).length,
)

const allPointRowsSelected = computed(() =>
  selectablePointRows.value.length > 0
  && selectedPointRowsCount.value === selectablePointRows.value.length,
)

function setAllPointRows(checked: boolean) {
  for (const row of selectablePointRows.value) row.included = checked
}

// High/medium default checked; low defaults unchecked; an already-classified
// record (existing_class set) always defaults unchecked -- existing
// user-entered semantics are never silently replaced (requirement 20).
function defaultIncluded(entry: SemanticSuggestionEntry): boolean {
  return entry.existing_class === null && (entry.confidence === 'high' || entry.confidence === 'medium')
}

function toRow(entry: SemanticSuggestionEntry): PointRow {
  return { suggestion: entry, included: defaultIncluded(entry), chosenClass: entry.existing_class ?? entry.suggested_class, aiLoading: false }
}

watch(() => props.open, async (v) => {
  if (!v || !props.device) return
  loading.value = true
  deviceSuggestion.value = null
  deviceIncluded.value = false
  deviceChosenClass.value = null
  pointRows.value = []
  unclassifiedRows.value = []
  showUnclassified.value = false
  try {
    const result = await api.semanticSuggestions.forDevice(props.device.id)
    deviceSuggestion.value = result.device
    deviceIncluded.value = defaultIncluded(result.device)
    deviceChosenClass.value = result.device.existing_class ?? result.device.suggested_class

    const rows: PointRow[] = []
    const unclassified: PointRow[] = []
    for (const p of result.points) {
      // Genuinely unclassifiable (no rule match, nothing existing) -- kept
      // out of the main table by default (large real controllers can have
      // dozens of these) but still fully reachable via "Show unclassified",
      // since that's exactly the case Use AI exists for.
      if (p.existing_class === null && p.confidence === 'none') {
        unclassified.push(toRow(p))
        continue
      }
      rows.push(toRow(p))
    }
    pointRows.value = rows
    unclassifiedRows.value = unclassified
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load semantic suggestions')
  } finally {
    loading.value = false
  }
})

const CONFIDENCE_COLOR: Record<string, string> = { high: 'green', medium: 'blue', low: 'orange', none: 'default' }

function reasonText(entry: SemanticSuggestionEntry): string {
  if (entry.existing_class) return `Existing: ${entry.existing_class}`
  if (!entry.reasons.length) return 'No suggestion'
  return entry.reasons.slice(0, 2).join('; ')
}

function showUseAi(entry: SemanticSuggestionEntry): boolean {
  return entry.existing_class === null && (entry.confidence === 'low' || entry.confidence === 'none')
}

async function useAi(row: PointRow) {
  if (!props.device) return
  row.aiLoading = true
  try {
    const result = await api.semanticSuggestions.aiForPoint(props.device.id, row.suggestion.source_id)
    // Replace the suggestion (class/confidence/reason/source) and refresh
    // the picker's value -- but deliberately leave `included` exactly as
    // the user had it. Use AI reconsiders a suggestion; it never accepts
    // one on the user's behalf, so Apply Selected still requires an
    // explicit, separate check.
    row.suggestion = result
    row.chosenClass = result.suggested_class
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'AI suggestion is unavailable. Check Azure OpenAI configuration.')
  } finally {
    row.aiLoading = false
  }
}

const columns: TableColumnsType<PointRow> = [
  { title: '', key: 'included', width: 36 },
  { title: 'Name', dataIndex: ['suggestion', 'source_name'], key: 'name', width: 150 },
  { title: 'Brick Class', key: 'class', width: 220 },
  { title: 'Confidence', key: 'confidence', width: 110 },
  { title: 'Reason', key: 'reason' },
  { title: '', key: 'ai', width: 90 },
]

async function apply() {
  if (!props.device) return
  const device = props.device
  applying.value = true
  try {
    let count = 0
    if (deviceIncluded.value && deviceChosenClass.value) {
      await api.devices.update(device.id, {
        device_instance: device.device_instance, name: device.name, description: device.description,
        vendor_name: device.vendor_name, model_name: device.model_name, enabled: device.enabled,
        firmware_revision: device.firmware_revision, protocol_revision: device.protocol_revision,
        max_apdu_length_accepted: device.max_apdu_length_accepted, segmentation_supported: device.segmentation_supported,
        location_id: device.location_id, equipment_type: deviceChosenClass.value,
        can_receive_event_notifications: device.can_receive_event_notifications,
      })
      count++
    }

    for (const row of [...pointRows.value, ...unclassifiedRows.value]) {
      if (!row.included || !row.chosenClass) continue
      const obj = props.objects.find(o => o.id === row.suggestion.source_id)
      if (!obj) continue
      await api.objects.update(device.id, obj.id, {
        object_type: obj.object_type, object_instance: obj.object_instance, name: obj.name,
        units: obj.units, behavior: obj.behavior, behavior_params: obj.behavior_params,
        enabled: obj.enabled, number_of_states: obj.number_of_states, reliability: obj.reliability,
        polarity: obj.polarity, point_type: row.chosenClass,
      })
      count++
    }

    message.success(count ? `Applied ${count} classification${count !== 1 ? 's' : ''}` : 'Nothing selected to apply')
    emit('update:open', false)
    emit('applied')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to apply semantic suggestions')
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    :title="`Semantic Suggestions — ${device?.name ?? ''}`"
    width="860px"
    ok-text="Apply Selected"
    :confirm-loading="applying"
    :ok-button-props="{ disabled: loading }"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="apply"
  >
    <a-spin :spinning="loading">
      <div v-if="deviceSuggestion" style="border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:14px;display:flex;align-items:center;gap:10px">
        <a-checkbox v-model:checked="deviceIncluded" :disabled="!deviceChosenClass" />
        <div style="flex:1;min-width:0">
          <div style="font-weight:600">{{ deviceSuggestion.source_name }}</div>
          <div v-if="deviceSuggestion.existing_class" style="font-size:11px;color:var(--text-secondary)">Existing: {{ deviceSuggestion.existing_class }}</div>
          <div v-else-if="deviceSuggestion.reasons.length" style="font-size:11px;color:var(--text-secondary)">{{ reasonText(deviceSuggestion) }}</div>
        </div>
        <a-tag v-if="!deviceSuggestion.existing_class" :color="CONFIDENCE_COLOR[deviceSuggestion.confidence]" style="margin:0">{{ deviceSuggestion.confidence }}</a-tag>
        <a-select
          v-model:value="deviceChosenClass"
          show-search
          allow-clear
          placeholder="Not classified"
          :options="meta.equipment_types"
          style="width:260px"
        />
      </div>

      <a-table
        :data-source="pointRows"
        :columns="columns"
        :pagination="false"
        :show-sorter-tooltip="false"
        size="small"
        row-key="suggestion.source_id"
      >
        <template #title>
          <div style="display:flex;align-items:center;justify-content:space-between">
            <a-checkbox
              :checked="allPointRowsSelected"
              :disabled="selectablePointRows.length === 0"
              @change="(e: { target: { checked: boolean } }) => setAllPointRows(e.target.checked)"
            >
              Select all
            </a-checkbox>
            <span style="font-size:12px;color:var(--text-secondary)">
              {{ selectedPointRowsCount }}/{{ selectablePointRows.length }} selected
            </span>
          </div>
        </template>
        <template #emptyText>
          <div style="padding:16px;color:var(--text-placeholder)">No classifiable points found</div>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'included'">
            <a-checkbox
              v-model:checked="(record as PointRow).included"
              :disabled="(record as PointRow).suggestion.existing_class !== null || !(record as PointRow).chosenClass"
            />
          </template>
          <template v-else-if="column.key === 'class'">
            <a-select
              v-model:value="(record as PointRow).chosenClass"
              show-search
              allow-clear
              size="small"
              placeholder="Not classified"
              :options="meta.point_types"
              style="width:100%"
            />
          </template>
          <template v-else-if="column.key === 'confidence'">
            <a-tag v-if="(record as PointRow).suggestion.existing_class" color="default" style="margin:0">existing</a-tag>
            <template v-else>
              <a-tag :color="CONFIDENCE_COLOR[(record as PointRow).suggestion.confidence]" style="margin:0">{{ (record as PointRow).suggestion.confidence }}</a-tag>
              <a-tag v-if="(record as PointRow).suggestion.source === 'ai'" color="purple" style="margin:0 0 0 4px">AI</a-tag>
            </template>
          </template>
          <template v-else-if="column.key === 'reason'">
            <span style="font-size:12px;color:var(--text-secondary)">{{ reasonText((record as PointRow).suggestion) }}</span>
          </template>
          <template v-else-if="column.key === 'ai'">
            <a-button v-if="showUseAi((record as PointRow).suggestion)" size="small" :loading="(record as PointRow).aiLoading" @click="useAi(record as PointRow)">
              <template #icon><BulbOutlined /></template>
              Use AI
            </a-button>
          </template>
        </template>
      </a-table>

      <div v-if="unclassifiedRows.length" style="margin-top:10px">
        <a @click="showUnclassified = !showUnclassified" style="font-size:12px">
          <component :is="showUnclassified ? DownOutlined : RightOutlined" style="font-size:10px;margin-right:4px" />
          {{ unclassifiedRows.length }} point{{ unclassifiedRows.length !== 1 ? 's' : '' }} could not be classified
          <template v-if="!showUnclassified">— Show unclassified</template>
        </a>

        <div v-if="showUnclassified" style="margin-top:8px;border:1px solid var(--border);border-radius:6px">
          <div
            v-for="row in unclassifiedRows" :key="row.suggestion.source_id"
            style="display:flex;align-items:center;gap:10px;padding:6px 10px;border-bottom:1px solid var(--border-subtle)"
          >
            <a-checkbox v-model:checked="row.included" :disabled="!row.chosenClass" />
            <span style="width:140px;flex-shrink:0;font-size:12.5px">{{ row.suggestion.source_name }}</span>
            <a-select
              v-model:value="row.chosenClass"
              show-search
              allow-clear
              size="small"
              placeholder="No suggestion"
              :options="meta.point_types"
              style="flex:1"
            />
            <a-tag v-if="row.suggestion.source === 'ai'" color="purple" style="margin:0">AI</a-tag>
            <a-button size="small" :loading="row.aiLoading" @click="useAi(row)">
              <template #icon><BulbOutlined /></template>
              Use AI
            </a-button>
          </div>
        </div>
      </div>
    </a-spin>
  </a-modal>
</template>
