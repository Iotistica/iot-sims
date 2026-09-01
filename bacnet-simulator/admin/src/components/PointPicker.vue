<script setup lang="ts">
/** Device+point selector for the Functional Test builder -- replaces the
 * old bare `a-select :options="meta.point_types"` (Brick class only, no
 * device, no live value). Device/Point Name is the primary identity;
 * semantic type/object type·instance/units/live value are secondary,
 * matching ObjectsPanel.vue's existing table pattern. Brick semantics are
 * filter-only here, never the value stored -- see PointRef in types.ts. */
import { computed, ref } from 'vue'
import type { TableColumnsType } from 'ant-design-vue'
import { PlusOutlined } from '@ant-design/icons-vue'
import type { Meta, PointRef, PointRow } from '../types'
import { formatPresentValue } from '../format'
import GridFilterToolbar from './GridFilterToolbar.vue'

const props = withDefaults(
  defineProps<{
    modelValue: PointRef | null
    points: PointRow[]
    meta: Meta
    disabled?: boolean
    placeholder?: string
    /** 'button' (default) is the original full-width text trigger. 'icon'
     * renders a small circular icon button instead -- same internal
     * open/filter/table logic either way, only the trigger's markup
     * changes. Used by SavedGraphCard.vue's compact header "+" action. */
    trigger?: 'button' | 'icon'
  }>(),
  { disabled: false, placeholder: 'Select point…', trigger: 'button' },
)

const emit = defineEmits<{ 'update:modelValue': [PointRef | null] }>()

const open = ref(false)
const search = ref('')
const deviceFilter = ref<number | undefined>(undefined)
const objectTypeFilter = ref<string | undefined>(undefined)
const semanticFilter = ref<string | undefined>(undefined)

function compareText(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
}

const pointTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of props.meta.point_types) map[o.value] = o.label
  return map
})

const deviceOptions = computed(() => {
  const seen = new Map<number, string>()
  for (const p of props.points) seen.set(p.device_id, p.device_name)
  return [...seen.entries()]
    .sort((a, b) => compareText(a[1], b[1]))
    .map(([value, label]) => ({ value, label }))
})

const objectTypeOptions = computed(() => (
  [...new Set(props.points.map(p => p.object_type))].sort(compareText).map(t => ({ value: t, label: t }))
))

const semanticOptions = computed(() => (
  [...new Set(props.points.map(p => p.point_type).filter((v): v is string => !!v))]
    .sort(compareText)
    .map(v => ({ value: v, label: pointTypeLabel.value[v] ?? v }))
))

const filteredPoints = computed(() => {
  const q = search.value.trim().toLowerCase()
  return props.points.filter(p => {
    if (deviceFilter.value !== undefined && p.device_id !== deviceFilter.value) return false
    if (objectTypeFilter.value && p.object_type !== objectTypeFilter.value) return false
    if (semanticFilter.value && p.point_type !== semanticFilter.value) return false
    if (!q) return true
    return p.name.toLowerCase().includes(q) || p.device_name.toLowerCase().includes(q)
  })
})

const filtersActive = computed(() => (
  !!search.value.trim() || deviceFilter.value !== undefined
  || objectTypeFilter.value !== undefined || semanticFilter.value !== undefined
))
function clearFilters() {
  search.value = ''
  deviceFilter.value = undefined
  objectTypeFilter.value = undefined
  semanticFilter.value = undefined
}

const selectedRow = computed(() => {
  const ref = props.modelValue
  if (!ref) return undefined
  return props.points.find(p => p.device_id === ref.device_id && p.object_id === ref.object_id)
})

const triggerLabel = computed(() => {
  if (!props.modelValue) return props.placeholder
  const row = selectedRow.value
  return row ? `${row.device_name} / ${row.name}` : 'Unknown point'
})

function fmtLive(row: PointRow): string {
  return formatPresentValue(row.object_type, row.live_value)
}

function selectRow(row: PointRow) {
  emit('update:modelValue', { device_id: row.device_id, object_id: row.object_id })
  open.value = false
}

function filterOption(input: string, option: { label?: string }): boolean {
  return (option.label ?? '').toLowerCase().includes(input.toLowerCase())
}

const columns: TableColumnsType<PointRow> = [
  { title: 'Device / Point Name', key: 'identity' },
  { title: 'Type', key: 'semantic', width: 190 },
  { title: 'Object', key: 'object', width: 140 },
  { title: 'Units', key: 'units', width: 90 },
  { title: 'Live Value', key: 'live', width: 100 },
]
</script>

<template>
  <a-button
    v-if="trigger === 'icon'"
    type="text"
    size="small"
    shape="circle"
    :disabled="disabled"
    :title="placeholder"
    @click="open = true"
  >
    <template #icon><PlusOutlined /></template>
  </a-button>
  <a-button
    v-else
    :disabled="disabled"
    style="width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
    @click="open = true"
  >
    {{ triggerLabel }}
  </a-button>

  <a-modal v-model:open="open" title="Select a point" width="820px" :footer="null">
    <GridFilterToolbar
      v-model:search="search"
      search-placeholder="Search device or point name…"
      :can-clear="filtersActive"
      @clear="clearFilters"
    >
      <a-select
        v-model:value="deviceFilter" allow-clear show-search size="small"
        placeholder="Device" style="width:170px" :options="deviceOptions" :filter-option="filterOption"
      />
      <a-select
        v-model:value="objectTypeFilter" allow-clear size="small"
        placeholder="Object Type" style="width:150px" :options="objectTypeOptions"
      />
      <a-select
        v-model:value="semanticFilter" allow-clear show-search size="small"
        placeholder="Type" style="width:190px" :options="semanticOptions" :filter-option="filterOption"
      />
    </GridFilterToolbar>

    <a-table
      :data-source="filteredPoints"
      :columns="columns"
      :pagination="{ pageSize: 8, size: 'small' }"
      size="small"
      row-key="object_id"
      :custom-row="(record: PointRow) => ({ style: 'cursor:pointer', onClick: () => selectRow(record) })"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'identity'">
          <div style="font-weight:600">{{ (record as PointRow).device_name }} / {{ (record as PointRow).name }}</div>
        </template>
        <template v-else-if="column.key === 'semantic'">
          <a-tag v-if="(record as PointRow).point_type">
            {{ pointTypeLabel[(record as PointRow).point_type!] ?? (record as PointRow).point_type }}
          </a-tag>
          <span v-else style="color:var(--text-disabled)">—</span>
        </template>
        <template v-else-if="column.key === 'object'">
          <span style="color:var(--text-secondary);font-size:12px">
            {{ (record as PointRow).object_type }} #{{ (record as PointRow).object_instance }}
          </span>
        </template>
        <template v-else-if="column.key === 'units'">
          <span style="color:var(--text-secondary);font-size:12px">
            {{ !(record as PointRow).units || (record as PointRow).units === 'no-units' ? '—' : (record as PointRow).units }}
          </span>
        </template>
        <template v-else-if="column.key === 'live'">
          <span style="font-family:monospace;color:#1890ff">{{ fmtLive(record as PointRow) }}</span>
        </template>
      </template>
    </a-table>
  </a-modal>
</template>
