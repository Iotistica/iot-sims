<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { isDark } from '../theme'
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
  ReloadOutlined,
  DownloadOutlined,
  WifiOutlined,
  DisconnectOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'
import { authToken } from '../auth'
import type { AnalyticsSnapshot } from '../types'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, BarElement, ArcElement, Title, Tooltip, Legend)

// ─── Live data ──────────────────────────────────────────────────────────────
// Own WS connection, fully independent of App.vue's /ws (device values,
// 5s cadence) — this one pushes at 1s from the backend's separate
// metrics_loop(), so the two can never contend with or slow each other down.

const connected = ref(false)
const snapshot = ref<AnalyticsSnapshot | null>(null)
let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null

// Rolling client-side history for trend charts — the backend snapshot is a
// point-in-time view; trends over time are built here from successive pushes.
interface HistoryPoint {
  ts: number
  requestsPerSec: number
  avgResponseMs: number
  p95ResponseMs: number
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

const filterDevice = ref<number | null>(null) // device_instance
const filterClient = ref<string | null>(null) // source address
const timeWindowSec = ref(60) // controls how much of `history` the trend charts show

const deviceOptions = computed(() =>
  (snapshot.value?.devices.list ?? []).map((d) => ({ value: d.device_instance, label: d.name })),
)
const clientOptions = computed(() => {
  const sources = new Set<string>()
  for (const r of snapshot.value?.traffic.recent_requests ?? []) {
    if (r.source) sources.add(r.source)
  }
  return Array.from(sources).map((s) => ({ value: s, label: s }))
})

// a-select's "allow-clear" × button resets the bound value to `undefined`,
// not `null` — every check below used to compare against `null` specifically
// (`filterDevice.value !== null`), so clearing the filter left it stuck
// "filtering for device `undefined`" (which matches nothing) instead of
// going back to showing everything (GH #3, the clear-button follow-up).
// Normalized once here so every computed below only has one nullish check
// to get right.
const activeDeviceFilter = computed<number | null>(() => filterDevice.value ?? null)

const filteredRequests = computed(() => {
  let rows = snapshot.value?.traffic.recent_requests ?? []
  if (filterClient.value) rows = rows.filter((r) => r.source === filterClient.value)
  if (activeDeviceFilter.value !== null) {
    const prefix = `,${activeDeviceFilter.value}`
    rows = rows.filter((r) => r.object?.includes(prefix) || r.device === activeDeviceFilter.value)
  }
  return rows
})

// The device filter previously only touched filteredRequests above — every
// other section (Overview tiles, Device Analytics, Recent Errors) read the
// unfiltered snapshot directly, so picking a device visibly changed nothing
// (GH #3). Scoped here to whatever can be honestly derived from data already
// in the snapshot; Performance/Discovery stay global since they're genuinely
// process-wide, not per-device.

const filteredDevices = computed(() => {
  const all = snapshot.value?.devices.list ?? []
  return activeDeviceFilter.value === null ? all : all.filter((d) => d.device_instance === activeDeviceFilter.value)
})

const filteredErrors = computed(() => {
  let rows = snapshot.value?.errors.recent ?? []
  if (activeDeviceFilter.value !== null) {
    const prefix = `,${activeDeviceFilter.value}`
    rows = rows.filter((r) => r.object?.includes(prefix))
  }
  return rows
})

const filteredDuplicateIds = computed(() => {
  const all = snapshot.value?.errors.duplicate_device_ids ?? []
  return activeDeviceFilter.value === null ? all : all.filter((d) => d.device_instance === activeDeviceFilter.value)
})

const overviewTotalErrors = computed(() =>
  activeDeviceFilter.value === null ? snapshot.value?.errors.total ?? 0 : filteredErrors.value.length,
)

const overviewTotalDevices = computed(() =>
  activeDeviceFilter.value === null ? snapshot.value?.overview.total_devices ?? 0 : filteredDevices.value.length,
)
const overviewOnlineDevices = computed(() =>
  activeDeviceFilter.value === null
    ? snapshot.value?.overview.online_devices ?? 0
    : filteredDevices.value.filter((d) => d.enabled).length,
)
const overviewOfflineDevices = computed(() =>
  activeDeviceFilter.value === null
    ? snapshot.value?.overview.offline_devices ?? 0
    : filteredDevices.value.filter((d) => !d.enabled).length,
)

const filteredHistory = computed(() => history.value.slice(-timeWindowSec.value))

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

// ─── Chart helpers ──────────────────────────────────────────────────────────

const chartOptionsBase = computed(() => {
  const textColor = isDark.value ? 'rgba(255,255,255,0.65)' : 'rgba(0,0,0,0.65)'
  const gridColor = isDark.value ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'
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
    {
      label: 'Avg (ms)',
      data: filteredHistory.value.map((h) => h.avgResponseMs),
      borderColor: '#52c41a',
      pointRadius: 0,
      tension: 0.3,
    },
    {
      label: 'P95 (ms)',
      data: filteredHistory.value.map((h) => h.p95ResponseMs),
      borderColor: '#faad14',
      pointRadius: 0,
      tension: 0.3,
    },
  ],
}))

const systemLoadData = computed(() => ({
  labels: filteredHistory.value.map((h) => fmtTime(h.ts)),
  datasets: [
    {
      label: 'CPU %',
      data: filteredHistory.value.map((h) => h.cpuPercent),
      borderColor: '#eb2f96',
      pointRadius: 0,
      tension: 0.3,
      yAxisID: 'y',
    },
    {
      label: 'Memory (MB)',
      data: filteredHistory.value.map((h) => h.memoryMb),
      borderColor: '#722ed1',
      pointRadius: 0,
      tension: 0.3,
      yAxisID: 'y1',
    },
  ],
}))
const systemLoadOptions = computed(() => ({
  ...chartOptionsBase.value,
  scales: {
    ...chartOptionsBase.value.scales,
    y1: { position: 'right' as const, ticks: chartOptionsBase.value.scales.y.ticks, grid: { display: false } },
  },
}))

const CHART_COLORS = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1', '#13c2c2', '#eb2f96', '#a0d911']

const serviceDistData = computed(() => {
  const byService = snapshot.value?.traffic.requests_by_service ?? {}
  const labels = Object.keys(byService)
  return {
    labels,
    datasets: [{ label: 'Requests', data: labels.map((k) => byService[k]), backgroundColor: CHART_COLORS }],
  }
})

const readWriteData = computed(() => ({
  labels: ['Reads', 'Writes'],
  datasets: [{
    data: [snapshot.value?.traffic.reads_total ?? 0, snapshot.value?.traffic.writes_total ?? 0],
    backgroundColor: ['#1890ff', '#faad14'],
  }],
}))

const broadcastUnicastData = computed(() => ({
  labels: ['Broadcast', 'Unicast'],
  datasets: [{
    data: [snapshot.value?.traffic.broadcast ?? 0, snapshot.value?.traffic.unicast ?? 0],
    backgroundColor: ['#f5222d', '#52c41a'],
  }],
}))

const topDevicesData = computed(() => {
  const rows = snapshot.value?.traffic.top_devices ?? []
  return {
    labels: rows.map((r) => r.name),
    datasets: [{ label: 'Requests', data: rows.map((r) => r.count), backgroundColor: '#1890ff' }],
  }
})

const errorsByTypeData = computed(() => {
  const byType = snapshot.value?.errors.by_type ?? {}
  const labels = Object.keys(byType)
  return {
    labels,
    datasets: [{ label: 'Count', data: labels.map((k) => byType[k]), backgroundColor: '#f5222d' }],
  }
})

// ─── Table columns ──────────────────────────────────────────────────────────

const deviceColumns = [
  { title: 'Device', dataIndex: 'name', key: 'name' },
  { title: 'Instance', dataIndex: 'device_instance', key: 'device_instance', width: 100 },
  { title: 'Status', key: 'enabled', width: 100 },
  { title: 'Objects', dataIndex: 'object_count', key: 'object_count', width: 90 },
  { title: 'Activity', dataIndex: 'activity', key: 'activity', width: 90 },
]

const topObjectColumns = [
  { title: 'Object', dataIndex: 'object', key: 'object' },
  { title: 'Reads + Writes', dataIndex: 'count', key: 'count', width: 130 },
]

const recentErrorColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Type', dataIndex: 'type', key: 'type' },
  { title: 'Service', dataIndex: 'service', key: 'service', width: 120 },
  { title: 'Object', dataIndex: 'object', key: 'object' },
]

const recentRequestColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Service', dataIndex: 'service', key: 'service', width: 120 },
  { title: 'Object', dataIndex: 'object', key: 'object' },
  { title: 'Result', key: 'ok', width: 90 },
  { title: 'Latency (ms)', key: 'latency_ms', width: 110 },
]

const discoveryColumns = [
  { title: 'Time', key: 'ts', width: 110 },
  { title: 'Device Instance', dataIndex: 'device_instance', key: 'device_instance' },
  { title: 'Source', dataIndex: 'source', key: 'source' },
]
</script>

<template>
  <div class="analytics-root" :class="{ dark: isDark }">
      <!-- Toolbar -->
      <div class="toolbar">
        <a-space wrap>
          <a-tag :color="connected ? 'green' : 'red'">
            <component :is="connected ? WifiOutlined : DisconnectOutlined" />
            {{ connected ? 'Live' : 'Reconnecting…' }}
          </a-tag>
          <a-select
            v-model:value="filterDevice"
            allow-clear
            placeholder="Filter by device"
            style="width: 200px"
            :options="deviceOptions"
          />
          <a-select
            v-model:value="filterClient"
            allow-clear
            placeholder="Filter by client"
            style="width: 200px"
            :options="clientOptions"
          />
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
            <a-card size="small"><a-statistic title="Total Devices" :value="overviewTotalDevices" /></a-card>
            <a-card size="small"><a-statistic title="Online" :value="overviewOnlineDevices" :value-style="{ color: '#52c41a' }" /></a-card>
            <a-card size="small"><a-statistic title="Offline" :value="overviewOfflineDevices" :value-style="{ color: overviewOfflineDevices ? '#f5222d' : undefined }" /></a-card>
            <a-card size="small"><a-statistic title="Active Clients" :value="snapshot.overview.active_clients" /></a-card>
            <a-card size="small"><a-statistic title="Requests/sec" :value="snapshot.overview.requests_per_sec" /></a-card>
            <a-card size="small"><a-statistic title="Avg Response (ms)" :value="snapshot.overview.avg_response_time_ms" :precision="2" /></a-card>
            <a-card size="small"><a-statistic title="Active Alarms" :value="snapshot.overview.active_alarms" :value-style="{ color: snapshot.overview.active_alarms ? '#faad14' : undefined }" /></a-card>
          </div>
        </section>

        <!-- ═══ Traffic ═══ -->
        <section>
          <h3>Traffic Analytics</h3>
          <div class="chart-row">
            <a-card size="small" title="Live Traffic (requests/sec)" class="chart-card wide">
              <div class="chart-box"><Line :data="requestsTimelineData" :options="chartOptionsBase" /></div>
            </a-card>
            <a-card size="small" title="Read vs Write" class="chart-card">
              <div class="chart-box"><Doughnut :data="readWriteData" :options="chartOptionsBase" /></div>
            </a-card>
            <a-card size="small" title="Broadcast vs Unicast" class="chart-card">
              <div class="chart-box"><Doughnut :data="broadcastUnicastData" :options="chartOptionsBase" /></div>
            </a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Service Distribution" class="chart-card">
              <div class="chart-box"><Bar :data="serviceDistData" :options="chartOptionsBase" /></div>
            </a-card>
            <a-card size="small" title="Top Active Devices" class="chart-card wide">
              <div class="chart-box"><Bar :data="topDevicesData" :options="chartOptionsBase" /></div>
            </a-card>
          </div>
          <a-card size="small" title="Recent Requests">
            <a-table
              :columns="recentRequestColumns"
              :data-source="filteredRequests.slice().reverse()"
              :pagination="{ pageSize: 10 }"
              row-key="ts"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
                <template v-else-if="column.key === 'ok'">
                  <a-tag :color="record.ok ? 'green' : 'red'">{{ record.ok ? 'OK' : 'Error' }}</a-tag>
                </template>
                <template v-else-if="column.key === 'latency_ms'">
                  {{ record.latency_ms != null ? record.latency_ms.toFixed(2) : '—' }}
                </template>
              </template>
            </a-table>
          </a-card>
        </section>

        <!-- ═══ Device Analytics ═══ -->
        <section>
          <h3>Device Analytics</h3>
          <a-card size="small">
            <a-table :columns="deviceColumns" :data-source="filteredDevices" :pagination="{ pageSize: 10 }" row-key="id" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'enabled'">
                  <a-tag :color="record.enabled ? 'green' : 'default'">{{ record.enabled ? 'Online' : 'Offline' }}</a-tag>
                </template>
              </template>
            </a-table>
          </a-card>
        </section>

        <!-- ═══ Object Analytics ═══ -->
        <section>
          <h3>Object Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Total Objects" :value="snapshot.objects.total" /></a-card>
            <a-card size="small"><a-statistic title="Unused Objects" :value="snapshot.objects.unused" /></a-card>
            <a-card size="small"><a-statistic title="Total Reads" :value="snapshot.objects.reads_total" /></a-card>
            <a-card size="small"><a-statistic title="Total Writes" :value="snapshot.objects.writes_total" /></a-card>
          </div>
          <a-card size="small" title="Most Accessed Objects">
            <a-table :columns="topObjectColumns" :data-source="snapshot.objects.top_accessed" :pagination="{ pageSize: 10 }" row-key="object" size="small" />
          </a-card>
        </section>

        <!-- ═══ Performance ═══ -->
        <section>
          <h3>Performance</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Avg Response (ms)" :value="snapshot.performance.avg_response_time_ms" :precision="2" /></a-card>
            <a-card size="small"><a-statistic title="P95 Response (ms)" :value="snapshot.performance.p95_response_time_ms" :precision="2" /></a-card>
            <a-card size="small"><a-statistic title="Throughput/sec" :value="snapshot.performance.throughput_per_sec" /></a-card>
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
            <a-card size="small"><a-statistic title="Total Errors" :value="overviewTotalErrors" :value-style="{ color: overviewTotalErrors ? '#f5222d' : undefined }" /></a-card>
            <a-card size="small"><a-statistic title="Duplicate Device IDs" :value="filteredDuplicateIds.length" :value-style="{ color: filteredDuplicateIds.length ? '#f5222d' : undefined }" /></a-card>
          </div>
          <div class="chart-row">
            <a-card size="small" title="Errors by Type (reject / abort / unknown object / unknown property / etc.)" class="chart-card wide">
              <div class="chart-box"><Bar :data="errorsByTypeData" :options="chartOptionsBase" /></div>
            </a-card>
          </div>
          <a-card size="small" title="Recent Errors">
            <a-table :columns="recentErrorColumns" :data-source="filteredErrors.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
              </template>
            </a-table>
          </a-card>
        </section>

        <!-- ═══ Discovery Analytics ═══ -->
        <section>
          <h3>Discovery Analytics</h3>
          <div class="kpi-grid">
            <a-card size="small"><a-statistic title="Who-Is Total" :value="snapshot.discovery.who_is_total" /></a-card>
            <a-card size="small"><a-statistic title="Devices Seen" :value="snapshot.discovery.devices_seen" /></a-card>
          </div>
          <a-card size="small" title="New Devices Discovered">
            <a-table :columns="discoveryColumns" :data-source="snapshot.discovery.new_devices_timeline.slice().reverse()" :pagination="{ pageSize: 10 }" row-key="ts" size="small">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'ts'">{{ fmtTime(record.ts) }}</template>
              </template>
            </a-table>
          </a-card>
        </section>
      </div>
  </div>
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
