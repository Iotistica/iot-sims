<script setup lang="ts">
import {
  computed,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from 'vue'
import {
  message,
} from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import {
  CaretRightOutlined,
  ClearOutlined,
  DownloadOutlined,
  PauseOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { protocolDetailRenderers } from './network/protocolDetailRenderers'
import { api } from '../api'
import { authToken } from '../auth'
import type {
  CapturedPacket,
  Device,
  PacketCaptureFilters,
  PacketCaptureStatus,
  PacketDirection,
} from '../types'

const props = defineProps<{
  initialDeviceId?: number | null
}>()

const status = ref<PacketCaptureStatus | null>(null)
const packets = ref<CapturedPacket[]>([])
const selectedPacket = ref<CapturedPacket | null>(null)
const devices = ref<Device[]>([])

const loading = ref(false)
const actionLoading = ref<
  'start' | 'stop' | 'clear' | 'export' | null
>(null)
const detailLoading = ref(false)
const drawerOpen = ref(false)
const total = ref(0)

const filters = reactive<PacketCaptureFilters>({
  direction: undefined,
  operation: undefined,
  protocol: undefined,
  deviceId: props.initialDeviceId ?? undefined,
  unassociated: undefined,
  offset: 0,
  limit: 200,
})

// Single "Device" dropdown maps to two independent backend filters
// (deviceId / unassociated) — see PacketCaptureFilters.
const deviceFilterValue = computed<number | 'unassociated' | undefined>({
  get() {
    if (filters.unassociated) return 'unassociated'
    return filters.deviceId
  },
  set(value) {
    if (value === 'unassociated') {
      filters.unassociated = true
      filters.deviceId = undefined
    } else {
      filters.unassociated = undefined
      filters.deviceId = value
    }
  },
})

const deviceFilterOptions = computed(() => [
  { label: 'Unassociated', value: 'unassociated' },
  ...devices.value.map(d => ({ label: d.name, value: d.id })),
])

function deviceFilterOptionMatch(input: string, option: { label: string }): boolean {
  return option.label.toLowerCase().includes(input.toLowerCase())
}

// Panel is remounted fresh (v-else-if) whenever App.vue switches back to
// this view, so the initial value is already seeded above; this watcher
// only covers the case where the panel is already mounted and a new
// device filter arrives (e.g. clicking "View Traffic" for a different
// device while already on the Network view) — not `immediate`, since that
// would double-fire alongside onMounted's own load().
watch(() => props.initialDeviceId, (id) => {
  if (id != null) {
    filters.unassociated = undefined
    filters.deviceId = id
    applyFilters()
  }
})

let poll: ReturnType<typeof setInterval> | null = null

let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null
const LIVE_BUFFER_LIMIT = 1500 // UI-only cap; backend's 10,000-packet buffer is untouched
const suppressLivePackets = ref(true)
const stopInFlight = ref(false)

const isRunning = computed(
  () => status.value?.state === 'running',
)

const packetCountLabel = computed(() => {
  const count = status.value?.packets_stored ?? 0
  return `${count.toLocaleString()} packet${count === 1 ? '' : 's'}`
})

const droppedCount = computed(
  () => status.value?.packets_dropped ?? 0,
)

type PacketSection = Record<string, unknown> | null | undefined

const availableOperations = computed(() => {
  const values = new Set<string>()

  for (const packet of packets.value) {
    if (packet.operation) {
      values.add(packet.operation)
    }
  }

  return [...values]
    .sort((a, b) => a.localeCompare(b))
    .map(value => ({
      label: value,
      value,
    }))
})

async function loadDevices(): Promise<void> {
  try {
    devices.value = await api.devices.list()
  } catch {
    // Device filter options just stay empty — not fatal to packet capture.
  }
}

async function loadStatus(): Promise<void> {
  try {
    status.value = await api.packetCapture.status()
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to load capture status',
    )
  }
}

async function loadPackets(): Promise<void> {
  loading.value = true

  try {
    const result = await api.packetCapture.list(filters)
    packets.value = result.items as CapturedPacket[]
    total.value = result.total
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to load packets',
    )
  } finally {
    loading.value = false
  }
}

// Merges a REST-authoritative packet list into packets.value by packet_id
// instead of replacing it wholesale. Unlike loadPackets() (a full replace --
// correct when the user explicitly changes filters or clicks Refresh), this
// is used for automatic reconciliation that can race against handleLivePacket()
// unshifting new packets onto packets.value concurrently: a blind replace at
// the wrong moment could silently drop a just-arrived live packet (overwritten
// by a REST snapshot taken before it existed) or duplicate one. Every packet
// has a unique packet_id, so merging is always safe/idempotent.
function mergePacketsById(incoming: CapturedPacket[]): void {
  const byId = new Map<string, CapturedPacket>()
  for (const p of packets.value) byId.set(p.packet_id, p)
  for (const p of incoming) byId.set(p.packet_id, p)
  const merged = Array.from(byId.values()).sort((a, b) => b.timestamp - a.timestamp)
  packets.value = merged.length > LIVE_BUFFER_LIMIT ? merged.slice(0, LIVE_BUFFER_LIMIT) : merged
}

// Closes the gap between the backend starting capture (immediate, on the
// /start response) and this client's WebSocket being fully accepted --
// /packet-capture/stream deliberately performs no replay (see its own
// docstring), so anything captured in that window would otherwise never
// reach this session's live view until the next full remount. Called once
// when the stream opens (see wsConnect()) and once when capture stops.
async function reconcilePackets(): Promise<void> {
  try {
    const result = await api.packetCapture.list(filters)
    mergePacketsById(result.items as CapturedPacket[])
    total.value = result.total
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to reconcile packets',
    )
  }
}

async function load(): Promise<void> {
  await Promise.all([
    loadStatus(),
    loadPackets(),
  ])
}

async function startCapture(): Promise<void> {
  actionLoading.value = 'start'

  try {
    suppressLivePackets.value = false
    status.value = await api.packetCapture.start()
    packets.value = []
    total.value = 0
    message.success('Packet capture started')
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to start packet capture',
    )
  } finally {
    actionLoading.value = null
  }
}

async function stopCapture(): Promise<void> {
  actionLoading.value = 'stop'
  stopInFlight.value = true
  suppressLivePackets.value = true
  wsDisconnect()

  try {
    status.value = await api.packetCapture.stop()
    await reconcilePackets()
    message.success('Packet capture stopped')
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to stop packet capture',
    )
  } finally {
    stopInFlight.value = false
    actionLoading.value = null
  }
}

function wsConnect(): void {
  if (ws) return
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(
    `${proto}//${location.host}/packet-capture/stream?token=${encodeURIComponent(authToken.value ?? '')}`,
  )
  // Reconcile once the stream is actually accepted -- /packet-capture/stream
  // has no replay, so this picks up anything captured between backend Start
  // and this handshake completing (see reconcilePackets()'s own comment).
  ws.onopen = () => { void reconcilePackets() }
  ws.onmessage = (e) => handleLivePacket(JSON.parse(e.data) as CapturedPacket)
  ws.onclose = () => {
    ws = null
    if (authToken.value && isRunning.value) wsTimer = setTimeout(wsConnect, 3000)
  }
  ws.onerror = () => ws?.close()
}

function wsDisconnect(): void {
  if (wsTimer) {
    clearTimeout(wsTimer)
    wsTimer = null
  }
  ws?.close()
  ws = null
}

function handleLivePacket(packet: CapturedPacket): void {
  if (suppressLivePackets.value) return

  // Live enrichment may be incomplete. Device-filtered views use the
  // authoritative REST result; Refresh remains available during capture.
  if (filters.deviceId !== undefined || filters.unassociated) return

  if (filters.protocol && packet.protocol !== filters.protocol) return
  if (filters.direction && packet.direction !== filters.direction) return
  if (filters.operation && packet.operation !== filters.operation) return

  packets.value.unshift(packet)
  if (packets.value.length > LIVE_BUFFER_LIMIT) packets.value.length = LIVE_BUFFER_LIMIT
  // total.value is intentionally left untouched -- stays REST-authoritative.
}

async function clearCapture(): Promise<void> {
  actionLoading.value = 'clear'

  try {
    status.value = await api.packetCapture.clear()
    packets.value = []
    total.value = 0
    selectedPacket.value = null
    drawerOpen.value = false
    message.success('Captured packets cleared')
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to clear packets',
    )
  } finally {
    actionLoading.value = null
  }
}

async function exportPcap(): Promise<void> {
  actionLoading.value = 'export'

  try {
    await api.packetCapture.exportPcap()
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to export PCAP',
    )
  } finally {
    actionLoading.value = null
  }
}

async function openPacket(packet: CapturedPacket): Promise<void> {
  drawerOpen.value = true
  detailLoading.value = true
  selectedPacket.value = packet

  try {
    selectedPacket.value =
      await api.packetCapture.get(packet.packet_id) as CapturedPacket
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to load packet details',
    )
  } finally {
    detailLoading.value = false
  }
}

function applyFilters(): void {
  filters.offset = 0
  void loadPackets()
}

function clearFilters(): void {
  filters.direction = undefined
  filters.operation = undefined
  filters.protocol = undefined
  filters.deviceId = undefined
  filters.unassociated = undefined
  filters.offset = 0

  void loadPackets()
}

function formatTimestamp(packet: CapturedPacket): string {
  const source =
    packet.timestamp_utc ||
    new Date(packet.timestamp * 1000).toISOString()

  const parsed = new Date(source)

  if (Number.isNaN(parsed.getTime())) {
    return source
  }

  const base = parsed.toLocaleString(undefined, {
    hour12: false,
  })

  const milliseconds = String(parsed.getMilliseconds()).padStart(3, '0')

  return `${base}.${milliseconds}`
}

function timestampValue(packet: CapturedPacket): number {
  const source = packet.timestamp_utc ||
    new Date(packet.timestamp * 1000).toISOString()

  const value = new Date(source).getTime()
  return Number.isNaN(value) ? 0 : value
}

function endpoint(value: { address: string; port: number | null }): string {
  return value.port === null ? value.address : `${value.address.includes(':') ? `[${value.address}]` : value.address}:${value.port}`
}

function directionColor(direction: PacketDirection): string {
  return direction === 'inbound' ? 'blue' : 'purple'
}

function statusColor(value: string): string {
  switch (value.toLowerCase()) {
    case 'success': case 'ok': return 'success'
    case 'error': return 'error'
    case 'rejected': return 'orange'
    case 'aborted': return 'volcano'
    default: return 'default'
  }
}

function serviceColor(value?: string | null): string {
  switch (value) {
    case 'Error':
      return 'error'
    case 'Abort':
      return 'volcano'
    case 'Reject':
      return 'orange'
    case 'Simple-ACK':
    case 'Complex-ACK':
      return 'success'
    case 'Confirmed-Request':
      return 'cyan'
    case 'Who-Is':
      return 'blue'
    case 'I-Am':
      return 'geekblue'
    default:
      return 'default'
  }
}

function decodeStatusColor(value: string): string {
  if (value === 'decoded') return 'success'
  if (value === 'warning' || value === 'partial') return 'warning'
  if (value === 'error') return 'error'
  return 'default'
}

function sectionValue(
  section: PacketSection,
  ...keys: string[]
): unknown {
  if (!section) return undefined

  for (const key of keys) {
    const value = section[key]

    if (
      value !== undefined &&
      value !== null &&
      value !== ''
    ) {
      return value
    }
  }

  return undefined
}

function detailsSection(
  packet: CapturedPacket,
  key: string,
): PacketSection {
  const value = packet.details?.[key]
  return value && typeof value === 'object'
    ? value as Record<string, unknown>
    : undefined
}

function packetSummaryValue(
  packet: CapturedPacket,
  ...keys: string[]
): unknown {
  return sectionValue(packet.summary, ...keys)
}

function packetDetailsValue(
  packet: CapturedPacket,
  section: string,
  ...keys: string[]
): unknown {
  return sectionValue(detailsSection(packet, section), ...keys)
}

function packetDisplayValue(
  packet: CapturedPacket,
  ...lookups: Array<[PacketSection, string[]]>
): string | undefined {
  for (const [section, keys] of lookups) {
    const value = sectionValue(section, ...keys)
    if (value !== undefined) return String(value)
  }

  return undefined
}

function packetBvlcFunction(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [
      detailsSection(packet, 'bvlc'),
      ['function_name', 'functionName', 'function'],
    ],
  )
}

function packetServiceName(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [detailsSection(packet, 'apdu'), ['service_name', 'serviceName']],
    [detailsSection(packet, 'service'), ['service_name', 'serviceName']],
  )
}

function packetInvokeId(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [packet.summary, ['invoke_id', 'invokeId']],
    [detailsSection(packet, 'apdu'), ['invoke_id', 'invokeId']],
  )
}

function packetObjectIdentifier(
  packet: CapturedPacket,
): string | undefined {
  const direct = packetDisplayValue(
    packet,
    [
      packet.summary,
      ['object_identifier', 'objectIdentifier', 'object'],
    ],
    [
      detailsSection(packet, 'service'),
      ['object_identifier', 'objectIdentifier', 'object'],
    ],
  )

  if (direct) return direct

  const objectType = packetDisplayValue(
    packet,
    [packet.summary, ['object_type', 'objectType']],
    [detailsSection(packet, 'service'), ['object_type', 'objectType']],
  )
  const objectInstance = packetDisplayValue(
    packet,
    [packet.summary, ['object_instance', 'objectInstance']],
    [
      detailsSection(packet, 'service'),
      ['object_instance', 'objectInstance'],
    ],
  )

  return objectType && objectInstance
    ? `${objectType}:${objectInstance}`
    : undefined
}

function packetProperty(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [
      packet.summary,
      [
        'property',
        'property_name',
        'property_identifier_name',
        'property_identifier',
        'propertyIdentifier',
      ],
    ],
    [
      detailsSection(packet, 'service'),
      [
        'property',
        'property_name',
        'property_identifier_name',
        'property_identifier',
        'propertyIdentifier',
      ],
    ],
  )
}

function packetArrayIndex(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [packet.summary, ['array_index', 'arrayIndex']],
    [detailsSection(packet, 'service'), ['array_index', 'arrayIndex']],
  )
}

function packetPriority(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [packet.summary, ['priority', 'write_priority', 'writePriority']],
    [
      detailsSection(packet, 'service'),
      ['priority', 'write_priority', 'writePriority'],
    ],
  )
}

function packetValue(packet: CapturedPacket): unknown {
  const summaryValue = packetSummaryValue(packet, 'value')
  if (summaryValue !== undefined) return summaryValue

  return packetDetailsValue(
    packet,
    'service',
    'value',
    'decoded_value',
    'decodedValue',
    'property_value',
    'propertyValue',
    'written_value',
    'writtenValue',
  )
}

function packetFailedService(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [packet.summary, ['failed_service', 'failedService']],
    [detailsSection(packet, 'service'), ['failed_service', 'failedService']],
  )
}

function packetErrorClass(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [packet.summary, ['error_class_name', 'error_class']],
    [
      detailsSection(packet, 'service'),
      ['error_class_name', 'error_class'],
    ],
  )
}

function packetErrorCode(
  packet: CapturedPacket,
): string | undefined {
  return packetDisplayValue(
    packet,
    [packet.summary, ['error_code_name', 'error_code']],
    [
      detailsSection(packet, 'service'),
      ['error_code_name', 'error_code'],
    ],
  )
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

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`
  }

  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function formatHex(value?: string): string {
  if (!value) return ''

  const normalized = value
    .replace(/\s+/g, '')
    .match(/.{1,2}/g)

  if (!normalized) return value

  const lines: string[] = []

  for (let i = 0; i < normalized.length; i += 16) {
    const bytes = normalized.slice(i, i + 16)
    const offset = i.toString(16).padStart(4, '0')
    lines.push(`${offset}  ${bytes.join(' ')}`)
  }

  return lines.join('\n')
}

const availableProtocols = computed(() => [...new Set(packets.value.map(p => p.protocol))].map(value => ({value, label: value})))
const selectedRenderer = computed(() => {
  const protocol = selectedPacket.value?.protocol
  return protocol && Object.prototype.hasOwnProperty.call(protocolDetailRenderers, protocol) ? protocolDetailRenderers[protocol] : undefined
})
const columns: TableColumnsType<CapturedPacket> = [
  {
    title: 'Time',
    key: 'time',
    width: 210,
    sorter: (a, b) => timestampValue(a) - timestampValue(b),
    defaultSortOrder: 'descend',
    sortDirections: ['descend', 'ascend'],
  },
  {
    title: 'Protocol',
    dataIndex: 'protocol',
    key: 'protocol',
    width: 100,
    sorter: (a, b) => compareText(a.protocol, b.protocol),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Direction',
    dataIndex: 'direction',
    key: 'direction',
    width: 100,
    sorter: (a, b) => compareText(a.direction, b.direction),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Source',
    key: 'source',
    width: 190,
    sorter: (a, b) =>
      compareText(endpoint(a.source), endpoint(b.source)),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Destination',
    key: 'destination',
    width: 190,
    sorter: (a, b) =>
      compareText(endpoint(a.destination), endpoint(b.destination)),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Device',
    key: 'device',
    width: 170,
    sorter: (a, b) =>
      compareText(a.simulator.device_name, b.simulator.device_name),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Operation',
    dataIndex: 'operation',
    key: 'operation',
    width: 190,
    sorter: (a, b) => compareText(a.operation, b.operation),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Status',
    dataIndex: 'status',
    key: 'status',
    width: 110,
    sorter: (a, b) => compareText(a.status, b.status),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Size',
    key: 'length',
    width: 90,
    sorter: (a, b) => a.length - b.length,
    sortDirections: ['ascend', 'descend'],
  },
]
watch(isRunning, (running, wasRunning) => {
  if (running) {
    suppressLivePackets.value = false
    wsConnect()
  } else {
    suppressLivePackets.value = true
    wsDisconnect()
    if (wasRunning && !stopInFlight.value) {
      void reconcilePackets() // one authoritative resync when capture stops outside this tab
    }
  }
}, { immediate: true })

onMounted(async () => {
  void loadDevices()
  await load()

  poll = setInterval(async () => {
    await loadStatus()
  }, 2500)
})

onUnmounted(() => {
  wsDisconnect()

  if (poll) {
    clearInterval(poll)
    poll = null
  }
})
</script>
<template>
  <div class="packet-capture-page">
    <div class="page-header">
      <div class="title-area">
        <h2>Packet Capture</h2>

        <a-tag
          :color="isRunning ? 'processing' : 'default'"
        >
          {{ isRunning ? 'Capturing' : 'Stopped' }}
        </a-tag>
      </div>
    </div>


    <div class="filters">
      <a-select
        v-model:value="filters.direction"
        allow-clear
        placeholder="Direction"
        style="width:130px"
        @change="applyFilters"
      >
        <a-select-option value="inbound">
          Inbound
        </a-select-option>
        <a-select-option value="outbound">
          Outbound
        </a-select-option>
      </a-select>

      <a-select
        v-model:value="filters.operation"
        allow-clear
        show-search
        placeholder="Operation"
        style="width:220px"
        :options="availableOperations"
        @change="applyFilters"
      />

      <a-select v-model:value="filters.protocol" allow-clear show-search placeholder="Protocol" style="width:150px" :options="availableProtocols" @change="applyFilters" />

      <a-select
        v-model:value="deviceFilterValue"
        allow-clear
        show-search
        placeholder="All Devices"
        style="width:180px"
        :options="deviceFilterOptions"
        :filter-option="deviceFilterOptionMatch"
        @change="applyFilters"
      />

      <a-button
        size="small"
        type="primary"
        ghost
        @click="applyFilters"
      >
        Apply
      </a-button>

      <a-button
        size="small"
        @click="clearFilters"
      >
        Reset
      </a-button>

      <a-typography-text
        v-if="isRunning && (filters.deviceId !== undefined || filters.unassociated)"
        type="secondary"
        style="font-size:12px"
      >
        Live updates paused while a device filter is active — use Refresh
      </a-typography-text>

      <div style="flex:1" />

      <a-button
        type="primary"
        :disabled="isRunning"
        :loading="actionLoading === 'start'"
        @click="startCapture"
      >
        <template #icon>
          <CaretRightOutlined />
        </template>
        Start
      </a-button>

      <a-button
        :disabled="!isRunning"
        :loading="actionLoading === 'stop'"
        @click="stopCapture"
      >
        <template #icon>
          <PauseOutlined />
        </template>
        Stop
      </a-button>

      <a-button
        danger
        :disabled="!status?.packets_stored"
        :loading="actionLoading === 'clear'"
        @click="clearCapture"
      >
        <template #icon>
          <ClearOutlined />
        </template>
        Clear
      </a-button>

      <a-button
        :disabled="!status?.packets_stored"
        :loading="actionLoading === 'export'"
        @click="exportPcap"
      >
        <template #icon>
          <DownloadOutlined />
        </template>
        Export BACnet PCAP
      </a-button>

      <a-button
        :loading="loading"
        @click="load"
      >
        <template #icon>
          <ReloadOutlined />
        </template>
      </a-button>
    </div>

    <a-table :columns="columns" :data-source="packets" :loading="loading" :show-sorter-tooltip="false" row-key="packet_id" size="small" :scroll="{ x: 1350 }"
      :pagination="{ pageSize: 50, showSizeChanger: true, pageSizeOptions: ['25', '50', '100', '200'], showTotal: (value: number) => `${value} packets` }"
      :custom-row="(record: CapturedPacket) => ({ onClick: () => openPacket(record), style: { cursor: 'pointer' } })">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'time'">{{ formatTimestamp(record) }}</template>
        <template v-else-if="column.key === 'protocol'"><a-tag color="geekblue">{{ record.protocol }}</a-tag></template>
        <template v-else-if="column.key === 'direction'"><a-tag :color="directionColor(record.direction)">{{ record.direction }}</a-tag></template>
        <template v-else-if="column.key === 'operation'"><a-tag v-if="record.operation" color="cyan">{{ record.operation }}</a-tag><span v-else class="placeholder">—</span></template>
        <template v-else-if="column.key === 'status'"><a-tag v-if="record.status" :color="statusColor(record.status)">{{ record.status }}</a-tag><span v-else class="placeholder">—</span></template>
        <template v-else-if="column.key === 'source'"><span class="monospace">{{ endpoint(record.source) }}</span></template>
        <template v-else-if="column.key === 'destination'"><span class="monospace">{{ endpoint(record.destination) }}</span></template>
        <template v-else-if="column.key === 'device'">{{ record.simulator.device_name ?? '—' }}<span v-if="record.simulator.point_name"> / {{ record.simulator.point_name }}</span></template>
        <template v-else-if="column.key === 'length'">{{ record.length }} B</template>
      </template>
    </a-table>
    <a-drawer v-model:open="drawerOpen" title="Packet Details" placement="right" width="620">
      <a-spin :spinning="detailLoading">
        <template v-if="selectedPacket">
          <a-descriptions bordered size="small" :column="1">
            <a-descriptions-item label="Packet ID"><span class="monospace">{{ selectedPacket.packet_id }}</span></a-descriptions-item>
            <a-descriptions-item label="Timestamp">{{ formatTimestamp(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item label="Protocol"><a-tag color="geekblue">{{ selectedPacket.protocol }}</a-tag></a-descriptions-item>
            <a-descriptions-item label="Transport">{{ selectedPacket.transport }}</a-descriptions-item>
            <a-descriptions-item label="Direction"><a-tag :color="directionColor(selectedPacket.direction)">{{ selectedPacket.direction }}</a-tag></a-descriptions-item>
            <a-descriptions-item label="Source"><span class="monospace">{{ endpoint(selectedPacket.source) }}</span></a-descriptions-item>
            <a-descriptions-item label="Destination"><span class="monospace">{{ endpoint(selectedPacket.destination) }}</span></a-descriptions-item>
            <a-descriptions-item label="Length">{{ selectedPacket.length }} bytes</a-descriptions-item>
            <a-descriptions-item v-if="packetBvlcFunction(selectedPacket)" label="BVLC Function">{{ packetBvlcFunction(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item v-if="packetServiceName(selectedPacket)" label="BACnet Service"><a-tag :color="serviceColor(packetServiceName(selectedPacket))">{{ packetServiceName(selectedPacket) }}</a-tag></a-descriptions-item>
            <a-descriptions-item label="Operation"><a-tag v-if="selectedPacket.operation" color="cyan">{{ selectedPacket.operation }}</a-tag><span v-else class="placeholder">—</span></a-descriptions-item>
            <a-descriptions-item v-if="selectedPacket.status" label="Status"><a-tag :color="statusColor(selectedPacket.status)">{{ selectedPacket.status }}</a-tag></a-descriptions-item>
            <a-descriptions-item v-if="packetInvokeId(selectedPacket)" label="Invoke ID"><span class="monospace">{{ packetInvokeId(selectedPacket) }}</span></a-descriptions-item>
            <a-descriptions-item v-if="packetObjectIdentifier(selectedPacket)" label="Object Identifier"><span class="monospace">{{ packetObjectIdentifier(selectedPacket) }}</span></a-descriptions-item>
            <a-descriptions-item v-if="packetProperty(selectedPacket)" label="Property">{{ packetProperty(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item v-if="packetArrayIndex(selectedPacket)" label="Array Index">{{ packetArrayIndex(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item v-if="packetPriority(selectedPacket)" label="Write Priority">{{ packetPriority(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item v-if="packetValue(selectedPacket) !== undefined" label="Value"><span class="packet-detail-value">{{ String(packetValue(selectedPacket)) }}</span></a-descriptions-item>
            <a-descriptions-item v-if="packetFailedService(selectedPacket)" label="Failed Service">{{ packetFailedService(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item v-if="packetErrorClass(selectedPacket)" label="Error Class">{{ packetErrorClass(selectedPacket) }}</a-descriptions-item>
            <a-descriptions-item v-if="packetErrorCode(selectedPacket)" label="Error Code"><a-tag color="red">{{ packetErrorCode(selectedPacket) }}</a-tag></a-descriptions-item>
            <a-descriptions-item label="Decode Status"><a-tag :color="decodeStatusColor(selectedPacket.decode_status)">{{ selectedPacket.decode_status }}</a-tag></a-descriptions-item>
            <a-descriptions-item v-if="selectedPacket.decode_error" label="Decode Error"><span class="decode-error">{{ selectedPacket.decode_error }}</span></a-descriptions-item>
          </a-descriptions>
          <template v-if="selectedPacket.simulator.device_id != null">
            <div class="protocol-tree-header">Simulator</div>
            <a-descriptions bordered size="small" :column="1">
              <a-descriptions-item label="Device">{{ selectedPacket.simulator.device_name }}</a-descriptions-item>
              <a-descriptions-item v-if="selectedPacket.simulator.point_name" label="Point">{{ selectedPacket.simulator.point_name }}</a-descriptions-item>
            </a-descriptions>
          </template>
          <component :is="selectedRenderer" v-if="selectedRenderer" :key="selectedPacket.packet_id" :packet="selectedPacket" />
          <template v-else>
            <h4 class="protocol-tree-header">Protocol Details</h4>
            <a-alert type="error" :message="`Unsupported protocol detail renderer: ${selectedPacket.protocol}`" />
          </template>
        </template>
      </a-spin>
    </a-drawer>
  </div>
</template>
<style scoped>
.packet-capture-page {
  height: 100%;
  overflow: auto;
  padding: 20px;
}

.page-header {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 14px;
}

.title-area {
  display: flex;
  align-items: center;
  gap: 10px;
}

.title-area h2 {
  margin: 0;
  font-size: 16px;
}

.status-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  padding: 10px 14px;
  margin-bottom: 12px;
  border: 1px solid var(--border-color, #303030);
  border-radius: 6px;
  background: var(--panel-bg, rgba(255, 255, 255, 0.02));
}

.status-item {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 120px;
}

.status-label {
  color: var(--text-placeholder);
  font-size: 12px;
}

.status-value {
  font-size: 12px;
}

.filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.monospace {
  font-family:
    ui-monospace,
    SFMono-Regular,
    Menlo,
    Monaco,
    Consolas,
    monospace;
}

.placeholder {
  color: var(--text-placeholder);
}

.empty-state {
  padding: 30px;
  color: var(--text-placeholder);
}

.empty-hint {
  margin-top: 4px;
  font-size: 12px;
}

.hex-header {
  margin-top: 20px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, rgba(255,255,255,.88));
}


.service-summary-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 9px 11px;
  margin-top: 16px;
  border: 1px solid var(--border-color, #303030);
  border-radius: 6px;
  background: rgba(22, 119, 255, 0.06);
  font-size: 12px;
}

.service-summary-line > span:not(:last-child)::after {
  margin-left: 8px;
  color: var(--text-placeholder);
  content: '•';
}

.service-summary-operation {
  color: #69b1ff;
  font-weight: 600;
}

.service-summary-value,
.packet-detail-value {
  color: #95de64;
  overflow-wrap: anywhere;
}

.packet-hex {
  max-height: 420px;
  padding: 14px;
  overflow: auto;
  border: 1px solid var(--border-color, #303030);
  border-radius: 6px;
  background: #080b0f;
  color: #b8c7d9;
  font-family:
    ui-monospace,
    SFMono-Regular,
    Menlo,
    Monaco,
    Consolas,
    monospace;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre;
}

.packet-hex :deep(.hex-byte) {
  display: inline-block;
  min-width: 14px;
  text-align: center;
  border-radius: 2px;
}

.packet-hex :deep(.hex-byte--selected) {
  background: #264d73;
  color: #e6f7ff;
}


.protocol-tree-header {
  margin-top: 18px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, rgba(255,255,255,.88));
}

.protocol-tree-panel {
  max-height: 420px;
  padding: 8px 10px;
  overflow: auto;
  border: 1px solid var(--border-color, #303030);
  border-radius: 6px;
  background: var(
    --panel-bg,
    rgba(255, 255, 255, 0.02)
  );
}

.protocol-tree-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
  min-width: 0;
  padding-right: 6px;
}

.protocol-tree-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.protocol-tree-value {
  flex: 0 1 55%;
  overflow-wrap: anywhere;
  color: var(--text-placeholder);
  font-family:
    ui-monospace,
    SFMono-Regular,
    Menlo,
    Monaco,
    Consolas,
    monospace;
  font-size: 11px;
  text-align: right;
}

.decode-error {
  color: #ff7875;
  overflow-wrap: anywhere;
}
</style>
