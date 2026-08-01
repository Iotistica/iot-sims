<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { CheckOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { AlarmLogEntry } from '../types'

const alarms = ref<AlarmLogEntry[]>([])
const deviceNames = ref<Record<number, string>>({})
const loading = ref(false)
const unackedOnly = ref(false)
const acking = ref<number | null>(null)
let poll: ReturnType<typeof setInterval> | null = null

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

const columns: TableColumnsType = [
  { title: 'Time', dataIndex: 'ts', key: 'ts', width: 170 },
  { title: 'Device', key: 'device', width: 160 },
  { title: 'Object', dataIndex: 'object_name', key: 'object_name' },
  { title: 'Transition', key: 'transition' },
  { title: 'Priority', dataIndex: 'priority', key: 'priority', width: 90 },
  { title: 'Value', dataIndex: 'value', key: 'value', width: 90 },
  { title: 'Ack', key: 'ack', width: 140 },
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

    <a-table
      :columns="columns"
      :data-source="alarms"
      :loading="loading"
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
          <span v-if="!(record as AlarmLogEntry).ack_required" style="color:#bbb;font-size:12px">n/a</span>
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
        <div style="padding:24px;color:#bbb">No alarms {{ unackedOnly ? '(unacknowledged)' : 'yet' }}</div>
      </template>
    </a-table>
  </div>
</template>
