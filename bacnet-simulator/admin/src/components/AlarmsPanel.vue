<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { CheckOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { AlarmLogEntry } from '../types'
import GridFilterToolbar from './GridFilterToolbar.vue'

const alarms = ref<AlarmLogEntry[]>([])
const deviceNames = ref<Record<number, string>>({})
const loading = ref(false)
const unackedOnly = ref(false)
const acking = ref<number | null>(null)
let poll: ReturnType<typeof setInterval> | null = null

const search = ref('')
const stateFilter = ref<string | undefined>(undefined)
const priorityFilter = ref<number | undefined>(undefined)

const priorityOptions = computed(() =>
  [...new Set(alarms.value.map(a => a.priority))].sort((a, b) => a - b),
)

const stateOptions = computed(() =>
  [...new Set(alarms.value.map(a => a.to_state))].sort().map(s => ({ value: s, label: s })),
)

const filteredAlarms = computed(() => {
  const q = search.value.trim().toLowerCase()
  return alarms.value.filter(a => {
    if (stateFilter.value && a.to_state !== stateFilter.value) return false
    if (priorityFilter.value !== undefined && a.priority !== priorityFilter.value) return false
    if (!q) return true
    return (
      a.object_name.toLowerCase().includes(q) ||
      deviceLabel(a.device_id).toLowerCase().includes(q)
    )
  })
})

const hasActiveFilters = computed(() =>
  !!search.value.trim() || stateFilter.value !== undefined || priorityFilter.value !== undefined,
)

function clearFilters() {
  search.value = ''
  stateFilter.value = undefined
  priorityFilter.value = undefined
}

async function load() {
  loading.value = true
  try {
    const [entries, devices] = await Promise.all([
      api.alarms.list(200, unackedOnly.value),
      api.devices.list(),
    ])
    alarms.value = entries
    deviceNames.value = Object.fromEntries(devices.map(d => [d.id, d.name]))
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load alarms')
  } finally {
    loading.value = false
  }
}

function deviceLabel(deviceId: number): string {
  return deviceNames.value[deviceId] ?? `#${deviceId}`
}

async function ack(a: AlarmLogEntry) {
  acking.value = a.id
  try {
    await api.alarms.ack(a.id)
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to acknowledge')
  } finally {
    acking.value = null
  }
}

const unackedCount = computed(() => alarms.value.filter(a => a.ack_required && !a.acknowledged).length)

function stateColor(state: string): string {
  if (state === 'normal') return 'success'
  if (state === 'fault') return 'error'
  return 'warning'
}

function fmtDate(iso: string): string {
  return new Date(iso.replace(' ', 'T') + 'Z').toLocaleString()
}

function compareText(
  a: string | null | undefined,
  b: string | null | undefined,
): number {
  return (a ?? '').localeCompare(b ?? '', undefined, {
    numeric: true,
    sensitivity: 'base',
  })
}

function timestampValue(ts: string): number {
  const parsed = new Date(ts.replace(' ', 'T') + 'Z').getTime()
  return Number.isNaN(parsed) ? 0 : parsed
}

const columns: TableColumnsType<AlarmLogEntry> = [
  {
    title: 'Time',
    dataIndex: 'ts',
    key: 'ts',
    width: 170,
    sorter: (a, b) => timestampValue(a.ts) - timestampValue(b.ts),
    defaultSortOrder: 'descend',
    sortDirections: ['descend', 'ascend'],
  },
  {
    title: 'Device',
    key: 'device',
    width: 160,
    sorter: (a, b) =>
      compareText(
        deviceLabel(a.device_id),
        deviceLabel(b.device_id),
      ),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Object',
    dataIndex: 'object_name',
    key: 'object_name',
    sorter: (a, b) => compareText(a.object_name, b.object_name),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Transition',
    key: 'transition',
    sorter: (a, b) => {
      const aTransition = `${a.from_state} → ${a.to_state}`
      const bTransition = `${b.from_state} → ${b.to_state}`

      return compareText(aTransition, bTransition)
    },
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Priority',
    dataIndex: 'priority',
    key: 'priority',
    width: 90,
    sorter: (a, b) => a.priority - b.priority,
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Value',
    dataIndex: 'value',
    key: 'value',
    width: 90,
    sorter: (a, b) => {
      const aNumber = Number(a.value)
      const bNumber = Number(b.value)

      if (!Number.isNaN(aNumber) && !Number.isNaN(bNumber)) {
        return aNumber - bNumber
      }

      return compareText(String(a.value ?? ''), String(b.value ?? ''))
    },
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Ack',
    key: 'ack',
    width: 140,
    sorter: (a, b) => {
      function ackSortValue(alarm: AlarmLogEntry): number {
        if (!alarm.ack_required) return 0
        if (!alarm.acknowledged) return 1
        return 2
      }

      return ackSortValue(a) - ackSortValue(b)
    },
    sortDirections: ['ascend', 'descend'],
  },
]
onMounted(() => {
  load()
  poll = setInterval(load, 5000)
})
onUnmounted(() => {
  if (poll) clearInterval(poll)
})
</script>

<template>
  <div style="padding:20px;overflow:auto;height:100%">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <h2 style="margin:0;font-size:16px">Alarms</h2>
      <a-tag v-if="unackedCount" color="warning">{{ unackedCount }} unacknowledged</a-tag>
      <div style="flex:1" />
      <a-checkbox v-model:checked="unackedOnly" @change="load">Unacknowledged only</a-checkbox>
      <a-button size="small" :loading="loading" @click="load">
        <template #icon><ReloadOutlined /></template>
      </a-button>
    </div>

    <GridFilterToolbar
      v-model:search="search"
      search-placeholder="Search object or device…"
      :can-clear="hasActiveFilters"
      @clear="clearFilters"
    >
      <a-select
        v-model:value="stateFilter"
        allow-clear
        size="small"
        placeholder="State"
        style="width:120px"
        :options="stateOptions"
      />
      <a-select
        v-model:value="priorityFilter"
        allow-clear
        size="small"
        placeholder="Priority"
        style="width:110px"
        :options="priorityOptions.map(p => ({ value: p, label: String(p) }))"
      />
    </GridFilterToolbar>

    <a-table
      :columns="columns"
      :data-source="filteredAlarms"
      :loading="loading"
      :show-sorter-tooltip="false"
      row-key="id"
      size="small"
      :pagination="{ pageSize: 25 }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'ts'">
          {{ fmtDate((record as AlarmLogEntry).ts) }}
        </template>
        <template v-else-if="column.key === 'device'">
          {{ deviceLabel((record as AlarmLogEntry).device_id) }}
        </template>
        <template v-else-if="column.key === 'transition'">
          <a-tag :color="stateColor((record as AlarmLogEntry).from_state)">{{ (record as AlarmLogEntry).from_state }}</a-tag>
          →
          <a-tag :color="stateColor((record as AlarmLogEntry).to_state)">{{ (record as AlarmLogEntry).to_state }}</a-tag>
        </template>
        <template v-else-if="column.key === 'ack'">
          <span v-if="!(record as AlarmLogEntry).ack_required" style="color:var(--text-placeholder);font-size:12px">n/a</span>
          <a-tag v-else-if="(record as AlarmLogEntry).acknowledged" color="default">
            Acked{{ (record as AlarmLogEntry).ack_by ? ` by ${(record as AlarmLogEntry).ack_by}` : '' }}
          </a-tag>
          <a-button v-else size="small" type="primary" ghost :loading="acking === (record as AlarmLogEntry).id" @click="ack(record as AlarmLogEntry)">
            <template #icon><CheckOutlined /></template>
            Acknowledge
          </a-button>
        </template>
      </template>
      <template #emptyText>
        <div v-if="alarms.length" style="padding:24px;color:var(--text-placeholder)">
          No alarms match your filters —
          <a @click="clearFilters">clear filters</a>
        </div>
        <div v-else style="padding:24px;color:var(--text-placeholder)">No alarms {{ unackedOnly ? '(unacknowledged)' : 'yet' }}</div>
      </template>
    </a-table>
  </div>
</template>
