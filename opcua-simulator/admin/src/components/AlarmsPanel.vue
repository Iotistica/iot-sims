<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import { CheckOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import { authToken } from '../auth'
import type { AnalyticsSnapshot, AnalyticsAlarm, AnalyticsAlarmEvent } from '../types'

// Own WS connection to the same /ws/analytics feed AnalyticsDashboard.vue
// uses — alarms are one slice of that shared snapshot, not a separate REST
// list endpoint, so this panel subscribes independently rather than reaching
// into a sibling component's state (keeps this tab fully standalone, same as
// BACnet's AlarmsPanel.vue).
const connected = ref(false)
const active = ref(0)
const avgAckTimeS = ref<number | null>(null)
const activeList = ref<AnalyticsAlarm[]>([])
const recentEvents = ref<AnalyticsAlarmEvent[]>([])
let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null

function applySnapshot(data: AnalyticsSnapshot) {
  active.value = data.alarms.active
  avgAckTimeS.value = data.alarms.avg_ack_time_s
  activeList.value = data.alarms.active_list
  recentEvents.value = data.alarms.recent_events
}

function wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${proto}//${location.host}/ws/analytics?token=${encodeURIComponent(authToken.value ?? '')}`)
  ws.onopen = () => { connected.value = true }
  ws.onmessage = (ev) => applySnapshot(JSON.parse(ev.data) as AnalyticsSnapshot)
  ws.onclose = () => {
    connected.value = false
    if (authToken.value) wsTimer = setTimeout(wsConnect, 3000)
  }
  ws.onerror = () => ws?.close()
}

onMounted(async () => {
  try { applySnapshot(await api.analytics.snapshot()) } catch { /* WS will populate shortly */ }
  wsConnect()
})
onUnmounted(() => {
  if (wsTimer) clearTimeout(wsTimer)
  ws?.close()
})

const acking = ref<Set<number>>(new Set())
async function ackAlarm(tagId: number) {
  acking.value = new Set([...acking.value, tagId])
  try {
    await api.analytics.ackAlarm(tagId)
    message.success('Alarm acknowledged')
  } catch (err: unknown) {
    message.error((err as { message?: string })?.message ?? 'Failed to acknowledge alarm')
  } finally {
    const next = new Set(acking.value)
    next.delete(tagId)
    acking.value = next
  }
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString()
}
function fmtDuration(s: number | null | undefined): string {
  if (s == null) return '—'
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`
}

const alarmColumns = [
  { title: 'Device', dataIndex: 'device_name', key: 'device_name' },
  { title: 'Tag', dataIndex: 'tag_name', key: 'tag_name' },
  { title: 'Fault', dataIndex: 'fault_type', key: 'fault_type', width: 100 },
  { title: 'Severity', key: 'severity', width: 100 },
  { title: 'Opened', key: 'opened_ts', width: 110 },
  { title: 'Status', key: 'ack_status', width: 110 },
  { title: '', key: 'actions', width: 90 },
]

const alarmEventColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Event', dataIndex: 'event', key: 'event', width: 90 },
  { title: 'Device', dataIndex: 'device_name', key: 'device_name' },
  { title: 'Tag', dataIndex: 'tag_name', key: 'tag_name' },
  { title: 'Severity', key: 'severity', width: 100 },
]
</script>

<template>
  <div style="padding:20px;overflow:auto;height:100%">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <h2 style="margin:0;font-size:16px">Alarms</h2>
      <a-tag v-if="active" color="warning">{{ active }} active</a-tag>
      <div style="flex:1" />
      <span style="font-size:12px;color:#aaa">{{ connected ? 'Live' : 'Reconnecting…' }}</span>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px">
      <a-card size="small"><a-statistic title="Active Alarms" :value="active" :value-style="{ color: active ? '#faad14' : undefined }" /></a-card>
      <a-card size="small"><a-statistic title="Avg Acknowledgement Time" :value="fmtDuration(avgAckTimeS)" /></a-card>
    </div>

    <a-card size="small" title="Active Alarms" style="margin-bottom: 16px">
      <a-table :columns="alarmColumns" :data-source="activeList" :pagination="{ pageSize: 10 }" row-key="tag_id" size="small">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'severity'">
            <a-tag :color="record.severity === 'critical' ? 'red' : 'gold'">{{ record.severity }}</a-tag>
          </template>
          <template v-else-if="column.key === 'opened_ts'">{{ fmtTime(record.opened_ts) }}</template>
          <template v-else-if="column.key === 'ack_status'">
            <a-tag v-if="record.acknowledged" color="green"><CheckOutlined /> Acked</a-tag>
            <a-tag v-else color="orange">Unacked</a-tag>
          </template>
          <template v-else-if="column.key === 'actions'">
            <a-button
              v-if="!record.acknowledged"
              size="small"
              :loading="acking.has(record.tag_id)"
              @click="ackAlarm(record.tag_id)"
            >
              Ack
            </a-button>
          </template>
        </template>
        <template #emptyText>
          <div style="padding:24px;color:#bbb">No active alarms</div>
        </template>
      </a-table>
    </a-card>

    <a-card size="small" title="Alarm History">
      <a-table :columns="alarmEventColumns" :data-source="recentEvents.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
          <template v-else-if="column.key === 'severity'">
            <a-tag :color="record.severity === 'critical' ? 'red' : 'gold'">{{ record.severity }}</a-tag>
          </template>
        </template>
        <template #emptyText>
          <div style="padding:24px;color:#bbb">No alarm history yet</div>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
