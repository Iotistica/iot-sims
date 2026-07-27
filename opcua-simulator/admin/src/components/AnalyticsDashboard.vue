<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { message, theme } from 'ant-design-vue'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Line, Bar, Doughnut } from 'vue-chartjs'
import {
  DownloadOutlined,
  BulbOutlined,
  WifiOutlined,
  DisconnectOutlined,
  CheckOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'
import { authToken } from '../auth'
import type { AnalyticsSnapshot } from '../types'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, BarElement, ArcElement, Title, Tooltip, Legend)

// ─── Live data ──────────────────────────────────────────────────────────────
// Own WS connection, fully independent of App.vue's /ws (device values, 5s
// cadence) — this one pushes at 1s from the backend's separate
// metrics_loop(), so the two can never contend with or slow each other down.

const connected = ref(false)
const snapshot = ref<AnalyticsSnapshot | null>(null)
let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null

interface HistoryPoint {
  ts: number
  requestsPerSec: number
  avgResponseMs: number
  p95ResponseMs: number
  p99ResponseMs: number
  cpuPercent: number
  memoryMb: number
}
const MAX_HISTORY = 900 // 15 min at 1 point/sec
const history = ref<HistoryPoint[]>([])

function wsConnectMetrics() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${proto}//${location.host}/ws/analytics?token=${encodeURIComponent(authToken.value ?? '')}`)
  ws.onopen = () => { connected.value = true }
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data) as AnalyticsSnapshot
    snapshot.value = data
    history.value.push({
      ts: data.ts,
      requestsPerSec: data.overview.requests_per_sec,
      avgResponseMs: data.performance.avg_response_time_ms,
      p95ResponseMs: data.performance.p95_response_time_ms,
      p99ResponseMs: data.performance.p99_response_time_ms,
      cpuPercent: data.performance.cpu_percent,
      memoryMb: data.performance.memory_mb,
    })
    if (history.value.length > MAX_HISTORY) history.value.shift()
  }
  ws.onclose = () => {
    connected.value = false
    if (authToken.value) wsTimer = setTimeout(wsConnectMetrics, 3000)
  }
  ws.onerror = () => ws?.close()
}

onMounted(async () => {
  try { snapshot.value = await api.analytics.snapshot() } catch { /* WS will populate shortly */ }
  wsConnectMetrics()
})
onUnmounted(() => {
  if (wsTimer) clearTimeout(wsTimer)
  ws?.close()
})

// ─── Filters ────────────────────────────────────────────────────────────────
// No per-server filter — this simulator only ever runs one OPC UA server, so
// a dropdown with a single always-selected option would be pure clutter.

const filterClient = ref<string | null>(null)
const filterSession = ref<string | null>(null)
const filterNode = ref('')
const timeWindowSec = ref(60)

const clientOptions = computed(() => {
  const peers = new Set<string>()
  for (const r of snapshot.value?.traffic.recent_requests ?? []) peers.add(r.peer)
  return Array.from(peers).map((p) => ({ value: p, label: p }))
})
const sessionOptions = computed(() =>
  (snapshot.value?.sessions.list ?? []).map((s) => ({ value: s.session_id, label: `${s.session_id} (${s.peer})` })),
)

const filteredRequests = computed(() => {
  let rows = snapshot.value?.traffic.recent_requests ?? []
  if (filterClient.value) rows = rows.filter((r) => r.peer === filterClient.value)
  return rows
})

function nodeMatches(row: { node: string; device: string | null; tag: string }, needle: string): boolean {
  const n = needle.toLowerCase()
  return row.node.toLowerCase().includes(n) || row.tag.toLowerCase().includes(n) || (row.device?.toLowerCase().includes(n) ?? false)
}

const filteredTopReadNodes = computed(() => {
  const rows = snapshot.value?.nodes.top_read ?? []
  return filterNode.value ? rows.filter((r) => nodeMatches(r, filterNode.value)) : rows
})
const filteredTopWrittenNodes = computed(() => {
  const rows = snapshot.value?.nodes.top_written ?? []
  return filterNode.value ? rows.filter((r) => nodeMatches(r, filterNode.value)) : rows
})
const filteredRecentErrors = computed(() => {
  let rows = snapshot.value?.errors.recent ?? []
  if (filterNode.value) {
    const n = filterNode.value.toLowerCase()
    rows = rows.filter((r) => (r.node ?? '').toLowerCase().includes(n))
  }
  return rows
})
const filteredSessionEvents = computed(() => {
  let rows = snapshot.value?.sessions.recent_events ?? []
  if (filterSession.value) rows = rows.filter((r) => r.session_id === filterSession.value)
  return rows
})

const filteredHistory = computed(() => history.value.slice(-timeWindowSec.value))

// ─── Dark mode (scoped to this component only) ─────────────────────────────

const darkMode = ref(false)
const dashboardTheme = computed(() => ({
  algorithm: darkMode.value ? theme.darkAlgorithm : theme.defaultAlgorithm,
  token: { colorPrimary: '#1890ff', borderRadius: 4 },
}))

// ─── Export ─────────────────────────────────────────────────────────────────

const exporting = ref(false)
async function doExport(format: 'csv' | 'json') {
  exporting.value = true
  try {
    await api.analytics.export(format)
  } finally {
    exporting.value = false
  }
}

// ─── Alarm acknowledgement ──────────────────────────────────────────────────

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

// ─── Chart helpers ──────────────────────────────────────────────────────────

const chartOptionsBase = computed(() => {
  const textColor = darkMode.value ? 'rgba(255,255,255,0.65)' : 'rgba(0,0,0,0.65)'
  const gridColor = darkMode.value ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false as const,
    plugins: { legend: { labels: { color: textColor } } },
    scales: {
      x: { ticks: { color: textColor }, grid: { color: gridColor } },
      y: { ticks: { color: textColor }, grid: { color: gridColor } },
    },
  }
})

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString()
}
function fmtDuration(s: number | null | undefined): string {
  if (s == null) return '—'
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`
}

const requestsTimelineData = computed(() => ({
  labels: filteredHistory.value.map((h) => fmtTime(h.ts)),
  datasets: [{
    label: 'Requests/sec',
    data: filteredHistory.value.map((h) => h.requestsPerSec),
    borderColor: '#1890ff',
    backgroundColor: 'rgba(24,144,255,0.15)',
    fill: true,
    tension: 0.3,
    pointRadius: 0,
  }],
}))

const responseTimeData = computed(() => ({
  labels: filteredHistory.value.map((h) => fmtTime(h.ts)),
  datasets: [
    { label: 'Avg (ms)', data: filteredHistory.value.map((h) => h.avgResponseMs), borderColor: '#52c41a', pointRadius: 0, tension: 0.3 },
    { label: 'P95 (ms)', data: filteredHistory.value.map((h) => h.p95ResponseMs), borderColor: '#faad14', pointRadius: 0, tension: 0.3 },
    { label: 'P99 (ms)', data: filteredHistory.value.map((h) => h.p99ResponseMs), borderColor: '#f5222d', pointRadius: 0, tension: 0.3 },
  ],
}))

const systemLoadData = computed(() => ({
  labels: filteredHistory.value.map((h) => fmtTime(h.ts)),
  datasets: [
    { label: 'CPU %', data: filteredHistory.value.map((h) => h.cpuPercent), borderColor: '#eb2f96', pointRadius: 0, tension: 0.3, yAxisID: 'y' },
    { label: 'Memory (MB)', data: filteredHistory.value.map((h) => h.memoryMb), borderColor: '#722ed1', pointRadius: 0, tension: 0.3, yAxisID: 'y1' },
  ],
}))
const systemLoadOptions = computed(() => ({
  ...chartOptionsBase.value,
  scales: {
    ...chartOptionsBase.value.scales,
    y1: { position: 'right' as const, ticks: chartOptionsBase.value.scales.y.ticks, grid: { display: false } },
  },
}))

const CHART_COLORS = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1', '#13c2c2', '#eb2f96', '#a0d911', '#2f54eb', '#fa8c16']

const serviceDistData = computed(() => {
  const byService = snapshot.value?.traffic.requests_by_service ?? {}
  const labels = Object.keys(byService)
  return { labels, datasets: [{ label: 'Requests', data: labels.map((k) => byService[k]), backgroundColor: CHART_COLORS }] }
})

const requestMixData = computed(() => ({
  labels: ['Reads', 'Writes', 'Browse', 'Call'],
  datasets: [{
    data: [
      snapshot.value?.traffic.reads_total ?? 0,
      snapshot.value?.traffic.writes_total ?? 0,
      snapshot.value?.traffic.browse_total ?? 0,
      snapshot.value?.traffic.call_total ?? 0,
    ],
    backgroundColor: ['#1890ff', '#faad14', '#52c41a', '#722ed1'],
  }],
}))

const topClientsData = computed(() => {
  const rows = snapshot.value?.traffic.top_clients ?? []
  return { labels: rows.map((r) => r.client), datasets: [{ label: 'Requests', data: rows.map((r) => r.count), backgroundColor: '#1890ff' }] }
})

const errorsByTypeData = computed(() => {
  const byType = snapshot.value?.errors.by_type ?? {}
  const labels = Object.keys(byType)
  return { labels, datasets: [{ label: 'Count', data: labels.map((k) => byType[k]), backgroundColor: '#f5222d' }] }
})

// ─── Table columns ──────────────────────────────────────────────────────────

const recentRequestColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Service', dataIndex: 'service', key: 'service', width: 140 },
  { title: 'Peer', dataIndex: 'peer', key: 'peer' },
  { title: 'Result', key: 'ok', width: 90 },
  { title: 'Latency (ms)', key: 'latency_ms', width: 110 },
]

const nodeColumns = [
  { title: 'Device', dataIndex: 'device', key: 'device' },
  { title: 'Tag / Node', key: 'tag' },
  { title: 'Count', dataIndex: 'count', key: 'count', width: 90 },
]

const sessionColumns = [
  { title: 'Session ID', dataIndex: 'session_id', key: 'session_id' },
  { title: 'Peer', dataIndex: 'peer', key: 'peer' },
  { title: 'State', key: 'state', width: 100 },
  { title: 'Auth', dataIndex: 'user_role', key: 'user_role', width: 100 },
  { title: 'Timeout (s)', key: 'timeout_s', width: 100 },
]

const sessionEventColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Event', key: 'event', width: 110 },
  { title: 'Session ID', dataIndex: 'session_id', key: 'session_id' },
  { title: 'Detail', key: 'detail' },
]

const subscriptionColumns = [
  { title: 'Subscription ID', dataIndex: 'subscription_id', key: 'subscription_id', width: 130 },
  { title: 'Session ID', dataIndex: 'session_id', key: 'session_id' },
  { title: 'Publishing Interval (ms)', dataIndex: 'publishing_interval_ms', key: 'publishing_interval_ms', width: 180 },
  { title: 'Monitored Items', dataIndex: 'monitored_item_count', key: 'monitored_item_count', width: 140 },
  { title: 'Avg Queue Size', dataIndex: 'avg_queue_size', key: 'avg_queue_size', width: 130 },
]

const recentErrorColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Service', dataIndex: 'service', key: 'service', width: 120 },
  { title: 'Status', dataIndex: 'status', key: 'status' },
  { title: 'Node', dataIndex: 'node', key: 'node' },
]

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
  <a-config-provider :theme="dashboardTheme">
    <div class="analytics-root" :class="{ dark: darkMode }">
      <!-- Toolbar -->
      <div class="toolbar">
        <a-space wrap>
          <a-tag :color="connected ? 'green' : 'red'">
            <component :is="connected ? WifiOutlined : DisconnectOutlined" />
            {{ connected ? 'Live' : 'Reconnecting…' }}
          </a-tag>
          <a-select v-model:value="filterClient" allow-clear placeholder="Filter by client" style="width: 180px" :options="clientOptions" />
          <a-select v-model:value="filterSession" allow-clear placeholder="Filter by session" style="width: 220px" :options="sessionOptions" />
          <a-input v-model:value="filterNode" allow-clear placeholder="Filter by node / tag" style="width: 200px" />
          <a-select v-model:value="timeWindowSec" style="width: 130px">
            <a-select-option :value="60">Last 1 min</a-select-option>
            <a-select-option :value="300">Last 5 min</a-select-option>
            <a-select-option :value="900">Last 15 min</a-select-option>
          </a-select>
        </a-space>

        <a-space>
          <a-button size="small" :loading="exporting" @click="doExport('csv')">
            <template #icon><DownloadOutlined /></template>
            CSV
          </a-button>
          <a-button size="small" :loading="exporting" @click="doExport('json')">
            <template #icon><DownloadOutlined /></template>
            JSON
          </a-button>
          <a-button size="small" :type="darkMode ? 'primary' : 'default'" @click="darkMode = !darkMode">
            <template #icon><BulbOutlined /></template>
            Dark mode
          </a-button>
        </a-space>
      </div>

      <div v-if="!snapshot" class="loading-state">
        <a-spin size="large" />
      </div>

      <div v-else class="dashboard-body">
        <!-- ═══ Overview ═══ -->
        <section>
          <h3>Overview</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Servers" :value="snapshot.overview.total_servers" /></a-card>
            <a-card size="small"><a-statistic title="Active Sessions" :value="snapshot.overview.active_sessions" /></a-card>
            <a-card size="small"><a-statistic title="Connected Clients" :value="snapshot.overview.connected_clients" /></a-card>
            <a-card size="small"><a-statistic title="Subscriptions" :value="snapshot.overview.subscriptions" /></a-card>
            <a-card size="small"><a-statistic title="Monitored Items" :value="snapshot.overview.monitored_items" /></a-card>
            <a-card size="small"><a-statistic title="Requests/sec" :value="snapshot.overview.requests_per_sec" /></a-card>
            <a-card size="small"><a-statistic title="Active Alarms" :value="snapshot.overview.active_alarms" :value-style="{ color: snapshot.overview.active_alarms ? '#faad14' : undefined }" /></a-card>
          </div>
        </section>

        <!-- ═══ Traffic ═══ -->
        <section>
          <h3>Traffic Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Total Requests" :value="snapshot.traffic.requests_total" /></a-card>
            <a-card size="small"><a-statistic title="OK" :value="snapshot.traffic.requests_ok" :value-style="{ color: '#52c41a' }" /></a-card>
            <a-card size="small"><a-statistic title="Failed" :value="snapshot.traffic.requests_failed" :value-style="{ color: snapshot.traffic.requests_failed ? '#f5222d' : undefined }" /></a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Live Traffic (requests/sec)" class="chart-card wide">
              <div class="chart-box"><Line :data="requestsTimelineData" :options="chartOptionsBase" /></div>
            </a-card>
            <a-card size="small" title="Read / Write / Browse / Call" class="chart-card">
              <div class="chart-box"><Doughnut :data="requestMixData" :options="chartOptionsBase" /></div>
            </a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Service Distribution" class="chart-card wide">
              <div class="chart-box"><Bar :data="serviceDistData" :options="chartOptionsBase" /></div>
            </a-card>
            <a-card size="small" title="Most Active Clients" class="chart-card">
              <div class="chart-box"><Bar :data="topClientsData" :options="chartOptionsBase" /></div>
            </a-card>
          </div>
          <a-card size="small" title="Recent Requests">
            <a-table :columns="recentRequestColumns" :data-source="filteredRequests.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
                <template v-else-if="column.key === 'ok'">
                  <a-tag :color="record.ok ? 'green' : 'red'">{{ record.ok ? 'OK' : 'Error' }}</a-tag>
                </template>
                <template v-else-if="column.key === 'latency_ms'">{{ record.latency_ms.toFixed(2) }}</template>
              </template>
            </a-table>
          </a-card>
        </section>

        <!-- ═══ Session Analytics ═══ -->
        <section>
          <h3>Session Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Active Sessions" :value="snapshot.sessions.active" /></a-card>
            <a-card size="small"><a-statistic title="Created (total)" :value="snapshot.sessions.created_total" /></a-card>
            <a-card size="small"><a-statistic title="Closed (total)" :value="snapshot.sessions.closed_total" /></a-card>
            <a-card size="small"><a-statistic title="Avg Duration" :value="fmtDuration(snapshot.sessions.avg_duration_s)" /></a-card>
          </div>
          <a-card size="small" title="Active Sessions" style="margin-bottom: 10px">
            <a-table :columns="sessionColumns" :data-source="snapshot.sessions.list" :pagination="{ pageSize: 10 }" row-key="session_id" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'state'">
                  <a-tag :color="record.state === 'Activated' ? 'green' : 'default'">{{ record.state }}</a-tag>
                </template>
                <template v-else-if="column.key === 'timeout_s'">{{ record.timeout_s != null ? (record.timeout_s / 1000).toFixed(0) : '—' }}</template>
              </template>
            </a-table>
          </a-card>
          <a-card size="small" title="Session History">
            <a-table :columns="sessionEventColumns" :data-source="filteredSessionEvents.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
                <template v-else-if="column.key === 'event'">
                  <a-tag :color="record.event === 'closed' ? 'default' : record.event === 'auth_failed' ? 'red' : 'green'">{{ record.event }}</a-tag>
                </template>
                <template v-else-if="column.key === 'detail'">
                  <span v-if="record.event === 'created'">from {{ record.name?.[0] }}:{{ record.name?.[1] }}</span>
                  <span v-else-if="record.event === 'activated'">auth: {{ record.auth_method }}</span>
                  <span v-else-if="record.event === 'closed'">duration: {{ fmtDuration(record.duration_s) }}, auth: {{ record.auth_method ?? '—' }}</span>
                  <span v-else-if="record.event === 'auth_failed'">{{ record.status }}</span>
                </template>
              </template>
            </a-table>
          </a-card>
        </section>

        <!-- ═══ Node Analytics ═══ -->
        <section>
          <h3>Node Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Distinct Nodes Accessed" :value="snapshot.nodes.distinct_nodes_accessed" /></a-card>
            <a-card size="small"><a-statistic title="Total Reads" :value="snapshot.nodes.reads_total" /></a-card>
            <a-card size="small"><a-statistic title="Total Writes" :value="snapshot.nodes.writes_total" /></a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Most Read Nodes" class="chart-card wide">
              <a-table :columns="nodeColumns" :data-source="filteredTopReadNodes" :pagination="{ pageSize: 5 }" row-key="node" size="small">
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'tag'">{{ record.tag }}</template>
                </template>
              </a-table>
            </a-card>
            <a-card size="small" title="Most Written Nodes" class="chart-card wide">
              <a-table :columns="nodeColumns" :data-source="filteredTopWrittenNodes" :pagination="{ pageSize: 5 }" row-key="node" size="small">
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'tag'">{{ record.tag }}</template>
                </template>
              </a-table>
            </a-card>
          </div>
        </section>

        <!-- ═══ Subscription Analytics ═══ -->
        <section>
          <h3>Subscription Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Active Subscriptions" :value="snapshot.subscriptions.active" /></a-card>
            <a-card size="small"><a-statistic title="Monitored Items" :value="snapshot.subscriptions.monitored_items" /></a-card>
            <a-card size="small"><a-statistic title="Items Created (total)" :value="snapshot.subscriptions.monitored_items_created_total" /></a-card>
            <a-card size="small"><a-statistic title="Items Deleted (total)" :value="snapshot.subscriptions.monitored_items_deleted_total" /></a-card>
            <a-card size="small"><a-statistic title="Dropped Notifications" :value="snapshot.subscriptions.dropped_notifications" :value-style="{ color: snapshot.subscriptions.dropped_notifications ? '#f5222d' : undefined }" /></a-card>
          </div>
          <a-card size="small" title="Active Subscriptions">
            <a-table :columns="subscriptionColumns" :data-source="snapshot.subscriptions.list" :pagination="{ pageSize: 10 }" row-key="subscription_id" size="small" />
          </a-card>
        </section>

        <!-- ═══ Performance ═══ -->
        <section>
          <h3>Performance</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Avg Response (ms)" :value="snapshot.performance.avg_response_time_ms" :precision="2" /></a-card>
            <a-card size="small"><a-statistic title="P95 Response (ms)" :value="snapshot.performance.p95_response_time_ms" :precision="2" /></a-card>
            <a-card size="small"><a-statistic title="P99 Response (ms)" :value="snapshot.performance.p99_response_time_ms" :precision="2" /></a-card>
            <a-card size="small"><a-statistic title="Requests/sec" :value="snapshot.performance.requests_per_sec" /></a-card>
            <a-card size="small"><a-statistic title="Concurrent Clients" :value="snapshot.performance.concurrent_clients" /></a-card>
            <a-card size="small"><a-statistic title="CPU %" :value="snapshot.performance.cpu_percent" :precision="1" /></a-card>
            <a-card size="small"><a-statistic title="Memory (MB)" :value="snapshot.performance.memory_mb" :precision="1" /></a-card>
            <a-card size="small"><a-statistic title="Error Rate %" :value="snapshot.performance.error_rate_percent" :precision="2" :value-style="{ color: snapshot.performance.error_rate_percent > 5 ? '#f5222d' : undefined }" /></a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Response Time Trend" class="chart-card wide">
              <div class="chart-box"><Line :data="responseTimeData" :options="chartOptionsBase" /></div>
            </a-card>
            <a-card size="small" title="CPU & Memory" class="chart-card wide">
              <div class="chart-box"><Line :data="systemLoadData" :options="systemLoadOptions" /></div>
            </a-card>
          </div>
        </section>

        <!-- ═══ Error Analytics ═══ -->
        <section>
          <h3>Error Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Total Errors" :value="snapshot.errors.total" :value-style="{ color: snapshot.errors.total ? '#f5222d' : undefined }" /></a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Errors by Status Code (BadNodeIdUnknown, BadUserAccessDenied, BadSessionIdInvalid, BadTimeout, etc.)" class="chart-card wide">
              <div class="chart-box"><Bar :data="errorsByTypeData" :options="chartOptionsBase" /></div>
            </a-card>
          </div>
          <a-card size="small" title="Recent Errors">
            <a-table :columns="recentErrorColumns" :data-source="filteredRecentErrors.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
              </template>
            </a-table>
          </a-card>
        </section>

        <!-- ═══ Security Analytics ═══ -->
        <section>
          <h3>Security Analytics</h3>
          <a-alert
            type="info"
            show-icon
            message="This simulator runs with NoSecurity only (no signing/encryption, anonymous auth) — policy/certificate variety isn't meaningful here. The counters below reflect real connection activity."
            style="margin-bottom: 10px"
          />
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Security Policy" :value="snapshot.security.policy" /></a-card>
            <a-card size="small"><a-statistic title="Secure Channel Opens" :value="snapshot.security.secure_channel_opens" /></a-card>
            <a-card size="small"><a-statistic title="Auth Failures" :value="snapshot.security.auth_failures" :value-style="{ color: snapshot.security.auth_failures ? '#f5222d' : undefined }" /></a-card>
            <a-card size="small"><a-statistic title="Rejected Connections" :value="snapshot.security.rejected_connections" :value-style="{ color: snapshot.security.rejected_connections ? '#f5222d' : undefined }" /></a-card>
          </div>
        </section>

        <!-- ═══ Alarm & Event Analytics ═══ -->
        <section>
          <h3>Alarm &amp; Event Analytics</h3>
          <a-alert
            type="warning"
            show-icon
            message="Simulated — derived from tags using a &quot;fault&quot; behavior, not real OPC UA Alarms &amp; Conditions. This simulator doesn't implement A&amp;C, so these alarms exist only in this dashboard and aren't visible to real OPC UA clients."
            style="margin-bottom: 10px"
          />
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Active Alarms" :value="snapshot.alarms.active" :value-style="{ color: snapshot.alarms.active ? '#faad14' : undefined }" /></a-card>
            <a-card size="small"><a-statistic title="Avg Acknowledgement Time" :value="fmtDuration(snapshot.alarms.avg_ack_time_s)" /></a-card>
          </div>
          <a-card size="small" title="Active Alarms" style="margin-bottom: 10px">
            <a-table :columns="alarmColumns" :data-source="snapshot.alarms.active_list" :pagination="{ pageSize: 10 }" row-key="tag_id" size="small">
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
            </a-table>
          </a-card>
          <a-card size="small" title="Alarm History">
            <a-table :columns="alarmEventColumns" :data-source="snapshot.alarms.recent_events.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
                <template v-else-if="column.key === 'severity'">
                  <a-tag :color="record.severity === 'critical' ? 'red' : 'gold'">{{ record.severity }}</a-tag>
                </template>
              </template>
            </a-table>
          </a-card>
        </section>
      </div>
    </div>
  </a-config-provider>
</template>

<style scoped>
.analytics-root {
  height: 100%;
  overflow: auto;
  padding: 16px 20px 40px;
  background: #f5f5f5;
}
.analytics-root.dark {
  background: #141414;
  color: rgba(255, 255, 255, 0.85);
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
  position: sticky;
  top: 0;
  z-index: 1;
}
.loading-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 300px;
}
.dashboard-body section {
  margin-bottom: 28px;
}
.dashboard-body h3 {
  font-size: 13px;
  font-weight: 700;
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 10px;
}
.analytics-root.dark .dashboard-body h3 {
  color: rgba(255, 255, 255, 0.45);
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.chart-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}
.chart-card.wide {
  grid-column: span 2;
}
.chart-box {
  height: 220px;
  position: relative;
}
@media (max-width: 700px) {
  .chart-card.wide { grid-column: span 1; }
}
</style>
