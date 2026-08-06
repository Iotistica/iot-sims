<script setup lang="ts">
import {
  computed,
  onMounted,
  onUnmounted,
  reactive,
  ref,
} from 'vue'
import {
  message,
  Modal,
} from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import {
  CaretRightOutlined,
  ClearOutlined,
  DownloadOutlined,
  PauseOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'
import type {
  CapturedPacket,
  PacketCaptureFilters,
  PacketCaptureStatus,
  PacketDirection,
} from '../types'

interface ProtocolSection {
  [key: string]: unknown
}

interface ProtocolPacket extends CapturedPacket {
  summary?: {
    operation?: string | null
    object_identifier?: string | null
    object_type?: string | null
    object_instance?: number | null
    property?: string | null
    property_identifier?: string | number | null
    array_index?: number | null
    invoke_id?: number | null
    priority?: number | null
    value?: unknown
  } | null
  bvlc?: ProtocolSection | null
  npdu?: ProtocolSection | null
  apdu?: ProtocolSection | null
  service?: ProtocolSection | null
}

interface ProtocolTreeNode {
  key: string
  title: string
  value?: string
  children?: ProtocolTreeNode[]
}

const status = ref<PacketCaptureStatus | null>(null)
const packets = ref<ProtocolPacket[]>([])
const selectedPacket = ref<ProtocolPacket | null>(null)

const loading = ref(false)
const actionLoading = ref<
  'start' | 'stop' | 'clear' | 'export' | null
>(null)
const detailLoading = ref(false)
const drawerOpen = ref(false)
const total = ref(0)

const filters = reactive<PacketCaptureFilters>({
  direction: undefined,
  service: undefined,
  sourceIp: undefined,
  destinationIp: undefined,
  offset: 0,
  limit: 200,
})

let poll: ReturnType<typeof setInterval> | null = null

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

const availableServices = computed(() => {
  const values = new Set<string>()

  for (const packet of packets.value) {
    if (packet.service_name) {
      values.add(packet.service_name)
    }
  }

  return [...values]
    .sort((a, b) => a.localeCompare(b))
    .map(value => ({
      label: value,
      value,
    }))
})

function serviceColor(
  service?: string | null,
): string {
  switch (service) {
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
    packets.value = result.items as ProtocolPacket[]
    total.value = result.total
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to load packets',
    )
  } finally {
    loading.value = false
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

  try {
    status.value = await api.packetCapture.stop()
    await loadPackets()
    message.success('Packet capture stopped')
  } catch (e: unknown) {
    message.error(
      (e as Error).message ?? 'Failed to stop packet capture',
    )
  } finally {
    actionLoading.value = null
  }
}

function confirmClear(): void {
  Modal.confirm({
    title: 'Clear captured packets?',
    content:
      'This removes all packets currently stored in memory. Export the PCAP first if you want to keep them.',
    okText: 'Clear',
    okType: 'danger',
    cancelText: 'Cancel',
    async onOk() {
      await clearCapture()
    },
  })
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

async function openPacket(packet: ProtocolPacket): Promise<void> {
  drawerOpen.value = true
  detailLoading.value = true
  selectedPacket.value = packet

  try {
    selectedPacket.value =
      await api.packetCapture.get(packet.packet_id) as ProtocolPacket
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
  filters.service = undefined
  filters.sourceIp = undefined
  filters.destinationIp = undefined
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

function endpoint(ip: string, port: number): string {
  return `${ip}:${port}`
}

function directionColor(direction: PacketDirection): string {
  return direction === 'inbound' ? 'blue' : 'purple'
}

function decodeStatusColor(value: string): string {
  if (value === 'decoded') return 'success'
  if (value === 'warning' || value === 'partial') return 'warning'
  if (value === 'error') return 'error'
  return 'default'
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


function rawBytes(value?: string): number[] {
  if (!value) return []

  const matches = value
    .replace(/\s+/g, '')
    .match(/.{1,2}/g)

  if (!matches) return []

  return matches
    .map(value => Number.parseInt(value, 16))
    .filter(value => !Number.isNaN(value))
}

function hexByte(value: number | undefined): string {
  if (value === undefined) return '—'
  return `0x${value.toString(16).padStart(2, '0')}`
}

function sectionNodes(
  prefix: string,
  section?: ProtocolSection | null,
): ProtocolTreeNode[] {
  if (!section) return []

  return Object.entries(section)
    .filter(([, value]) =>
      value !== null &&
      value !== undefined &&
      value !== '',
    )
    .map(([key, value]) => ({
      key: `${prefix}.${key}`,
      title: key
        .replace(/_/g, ' ')
        .replace(/\b\w/g, char => char.toUpperCase()),
      value:
        typeof value === 'object'
          ? JSON.stringify(value)
          : String(value),
    }))
}

function apduTypeName(type: number): string {
  const names: Record<number, string> = {
    0: 'Confirmed Request',
    1: 'Unconfirmed Request',
    2: 'Simple ACK',
    3: 'Complex ACK',
    4: 'Segment ACK',
    5: 'Error',
    6: 'Reject',
    7: 'Abort',
  }

  return names[type] ?? `Unknown APDU ${type}`
}

function sectionValue(
  section: ProtocolSection | null | undefined,
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

function packetOperation(
  packet: ProtocolPacket,
): string | undefined {
  const value =
    packet.summary?.operation ??
    sectionValue(
      packet.service,
      'operation',
      'service_name',
      'service',
    ) ??
    packet.service_name

  return value === undefined ? undefined : String(value)
}

function packetObjectIdentifier(
  packet: ProtocolPacket,
): string | undefined {
  const direct =
    packet.summary?.object_identifier ??
    sectionValue(
      packet.service,
      'object_identifier',
      'objectIdentifier',
      'object',
    )

  if (direct !== undefined) return String(direct)

  const objectType =
    packet.summary?.object_type ??
    sectionValue(
      packet.service,
      'object_type',
      'objectType',
    )

  const objectInstance =
    packet.summary?.object_instance ??
    sectionValue(
      packet.service,
      'object_instance',
      'objectInstance',
    )

  if (
    objectType !== undefined &&
    objectInstance !== undefined
  ) {
    return `${objectType}:${objectInstance}`
  }

  return undefined
}

function packetProperty(
  packet: ProtocolPacket,
): string | undefined {
  const value =
    packet.summary?.property ??
    packet.summary?.property_identifier ??
    sectionValue(
      packet.service,
      'property',
      'property_name',
      'property_identifier_name',
      'property_identifier',
      'propertyIdentifier',
    )

  return value === undefined ? undefined : String(value)
}

function packetArrayIndex(
  packet: ProtocolPacket,
): string | undefined {
  const value =
    packet.summary?.array_index ??
    sectionValue(
      packet.service,
      'array_index',
      'arrayIndex',
    )

  return value === undefined ? undefined : String(value)
}

function packetInvokeId(
  packet: ProtocolPacket,
): string | undefined {
  const value =
    packet.summary?.invoke_id ??
    sectionValue(
      packet.apdu,
      'invoke_id',
      'invokeId',
    )

  return value === undefined ? undefined : String(value)
}

function packetPriority(
  packet: ProtocolPacket,
): string | undefined {
  const value =
    packet.summary?.priority ??
    sectionValue(
      packet.service,
      'priority',
      'write_priority',
      'writePriority',
    )

  return value === undefined ? undefined : String(value)
}

function packetValue(
  packet: ProtocolPacket,
): unknown {
  if (
    packet.summary?.value !== undefined &&
    packet.summary?.value !== null
  ) {
    return packet.summary.value
  }

  return sectionValue(
    packet.service,
    'value',
    'decoded_value',
    'decodedValue',
    'property_value',
    'propertyValue',
    'written_value',
    'writtenValue',
  )
}

function hasServiceDetails(
  packet: ProtocolPacket,
): boolean {
  return Boolean(
    packetOperation(packet) ||
    packetObjectIdentifier(packet) ||
    packetProperty(packet) ||
    packetArrayIndex(packet) ||
    packetInvokeId(packet) ||
    packetPriority(packet) ||
    packetValue(packet) !== undefined,
  )
}

function protocolTree(
  packet: ProtocolPacket,
): ProtocolTreeNode[] {
  const bytes = rawBytes(packet.raw_hex)

  const bvlcLength =
    bytes.length >= 4
      ? (bytes[2] << 8) | bytes[3]
      : packet.length

  const bvlcChildren: ProtocolTreeNode[] = [
    {
      key: 'bvlc.type',
      title: 'Type',
      value:
        bytes[0] === 0x81
          ? `BACnet/IP (${hexByte(bytes[0])})`
          : hexByte(bytes[0]),
    },
    {
      key: 'bvlc.function',
      title: 'Function',
      value:
        packet.bvlc_function ??
        hexByte(bytes[1]),
    },
    {
      key: 'bvlc.length',
      title: 'Declared Length',
      value: `${bvlcLength} bytes`,
    },
    ...sectionNodes('bvlc', packet.bvlc),
  ]

  let npduOffset = 4
  if (bytes[1] === 0x04 && bytes.length >= 10) {
    npduOffset = 10
  }

  const npduVersion = bytes[npduOffset]
  const control = bytes[npduOffset + 1]
  let apduOffset = npduOffset + 2

  if (control !== undefined) {
    if ((control & 0x20) !== 0 && bytes.length > apduOffset + 2) {
      const destinationLength = bytes[apduOffset + 2] ?? 0
      apduOffset += 3 + destinationLength + 1
    }

    if ((control & 0x08) !== 0 && bytes.length > apduOffset + 2) {
      const sourceLength = bytes[apduOffset + 2] ?? 0
      apduOffset += 3 + sourceLength
    }
  }

  const npduChildren: ProtocolTreeNode[] = [
    {
      key: 'npdu.version',
      title: 'Version',
      value:
        npduVersion === undefined
          ? '—'
          : String(npduVersion),
    },
    {
      key: 'npdu.control',
      title: 'Control',
      value: hexByte(control),
    },
  ]

  if (control !== undefined) {
    npduChildren.push(
      {
        key: 'npdu.network_message',
        title: 'Network Layer Message',
        value:
          (control & 0x80) !== 0 ? 'Yes' : 'No',
      },
      {
        key: 'npdu.destination',
        title: 'Destination Specified',
        value:
          (control & 0x20) !== 0 ? 'Yes' : 'No',
      },
      {
        key: 'npdu.source',
        title: 'Source Specified',
        value:
          (control & 0x08) !== 0 ? 'Yes' : 'No',
      },
      {
        key: 'npdu.expecting_reply',
        title: 'Expecting Reply',
        value:
          (control & 0x04) !== 0 ? 'Yes' : 'No',
      },
      {
        key: 'npdu.priority',
        title: 'Priority',
        value: String(control & 0x03),
      },
    )
  }

  npduChildren.push(
    ...sectionNodes('npdu', packet.npdu),
  )

  const apduFirst = bytes[apduOffset]
  const apduType =
    apduFirst === undefined ? undefined : apduFirst >> 4

  const apduChildren: ProtocolTreeNode[] = [
    {
      key: 'apdu.type',
      title: 'Type',
      value:
        apduType === undefined
          ? packet.service_name ?? '—'
          : `${apduTypeName(apduType)} (${hexByte(apduFirst)})`,
    },
    {
      key: 'apdu.service',
      title: 'Operation',
      value:
        packet.summary?.operation ??
        packet.service_name ??
        'Unknown',
    },
    ...sectionNodes('apdu', packet.apdu),
  ]

  const serviceChildren = [
    ...(packet.summary?.object_identifier
      ? [{
          key: 'service.object_identifier',
          title: 'Object Identifier',
          value: packet.summary.object_identifier,
        }]
      : []),
    ...(packet.summary?.property
      ? [{
          key: 'service.property',
          title: 'Property',
          value: packet.summary.property,
        }]
      : []),
    ...(packet.summary?.value !== undefined &&
    packet.summary?.value !== null
      ? [{
          key: 'service.value',
          title: 'Value',
          value: String(packet.summary.value),
        }]
      : []),
    ...sectionNodes('service', packet.service),
  ]

  const tree: ProtocolTreeNode[] = [
    {
      key: 'bvlc',
      title: 'BACnet Virtual Link Control',
      value: `${bvlcLength} bytes`,
      children: bvlcChildren,
    },
    {
      key: 'npdu',
      title:
        'Building Automation and Control Network NPDU',
      children: npduChildren,
    },
    {
      key: 'apdu',
      title:
        'Building Automation and Control Network APDU',
      children: apduChildren,
    },
  ]

  if (serviceChildren.length) {
    tree.push({
      key: 'service',
      title:
        packet.summary?.operation ??
        packet.service_name ??
        'BACnet Service',
      children: serviceChildren,
    })
  }

  return tree
}

const selectedProtocolTree = computed<
  ProtocolTreeNode[]
>(() => {
  if (!selectedPacket.value) return []
  return protocolTree(selectedPacket.value)
})

const columns: TableColumnsType<ProtocolPacket> = [
  {
    title: 'Time',
    key: 'time',
    width: 210,
    sorter: (a, b) => timestampValue(a) - timestampValue(b),
    defaultSortOrder: 'descend',
    sortDirections: ['descend', 'ascend'],
  },
  {
    title: 'Direction',
    dataIndex: 'direction',
    key: 'direction',
    width: 100,
    sorter: (a, b) =>
      compareText(a.direction, b.direction),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Source',
    key: 'source',
    width: 190,
    sorter: (a, b) =>
      compareText(
        endpoint(a.source_ip, a.source_port),
        endpoint(b.source_ip, b.source_port),
      ),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Destination',
    key: 'destination',
    width: 190,
    sorter: (a, b) =>
      compareText(
        endpoint(a.destination_ip, a.destination_port),
        endpoint(b.destination_ip, b.destination_port),
      ),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'BVLC',
    dataIndex: 'bvlc_function',
    key: 'bvlc',
    width: 190,
    sorter: (a, b) =>
      compareText(a.bvlc_function, b.bvlc_function),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Service',
    dataIndex: 'service_name',
    key: 'service',
    width: 190,
    sorter: (a, b) =>
      compareText(a.service_name, b.service_name),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Length',
    dataIndex: 'length',
    key: 'length',
    width: 90,
    sorter: (a, b) => a.length - b.length,
    sortDirections: ['ascend', 'descend'],
  }
]

onMounted(async () => {
  await load()

  poll = setInterval(async () => {
    await loadStatus()

    if (isRunning.value) {
      await loadPackets()
    }
  }, 2500)
})

onUnmounted(() => {
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

      <div class="toolbar">
        <a-button
          size="small"
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
          size="small"
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
          size="small"
          danger
          :disabled="!status?.packets_stored"
          :loading="actionLoading === 'clear'"
          @click="confirmClear"
        >
          <template #icon>
            <ClearOutlined />
          </template>
          Clear
        </a-button>

        <a-button
          size="small"
          :disabled="!status?.packets_stored"
          :loading="actionLoading === 'export'"
          @click="exportPcap"
        >
          <template #icon>
            <DownloadOutlined />
          </template>
          Export PCAP
        </a-button>

        <a-button
          size="small"
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
        </a-button>
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
        v-model:value="filters.service"
        allow-clear
        show-search
        placeholder="BACnet service"
        style="width:220px"
        :options="availableServices"
        @change="applyFilters"
      />

      <a-input
        v-model:value="filters.sourceIp"
        allow-clear
        placeholder="Source IP"
        style="width:170px"
        @press-enter="applyFilters"
      />

      <a-input
        v-model:value="filters.destinationIp"
        allow-clear
        placeholder="Destination IP"
        style="width:170px"
        @press-enter="applyFilters"
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

      <div style="flex:1" />

      <span class="result-count">
        {{ total.toLocaleString() }} matching
      </span>
    </div>

    <a-table
      :columns="columns"
      :data-source="packets"
      :loading="loading"
      row-key="packet_id"
      size="small"
      :scroll="{ x: 1250 }"
      :pagination="{
        pageSize: 50,
        showSizeChanger: true,
        pageSizeOptions: ['25', '50', '100', '200'],
        showTotal: (value: number) => `${value} packets`,
      }"
      :custom-row="(record: ProtocolPacket) => ({
        onClick: () => openPacket(record),
        style: {
          cursor: 'pointer',
        },
      })"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'time'">
          {{ formatTimestamp(record as CapturedPacket) }}
        </template>

        <template v-else-if="column.key === 'direction'">
          <a-tag
            :color="
              directionColor(
                (record as CapturedPacket).direction,
              )
            "
          >
            {{ (record as CapturedPacket).direction }}
          </a-tag>
        </template>

        <template v-else-if="column.key === 'source'">
          <span class="monospace">
            {{
              endpoint(
                (record as CapturedPacket).source_ip,
                (record as CapturedPacket).source_port,
              )
            }}
          </span>
        </template>

        <template v-else-if="column.key === 'destination'">
          <span class="monospace">
            {{
              endpoint(
                (record as CapturedPacket).destination_ip,
                (record as CapturedPacket).destination_port,
              )
            }}
          </span>
        </template>

        <template v-else-if="column.key === 'bvlc'">
          {{
            (record as CapturedPacket).bvlc_function ?? '—'
          }}
        </template>

        <template v-else-if="column.key === 'service'">
        <a-tag
          v-if="(record as CapturedPacket).service_name"
          :color="serviceColor((record as CapturedPacket).service_name)"
        >
          {{ (record as CapturedPacket).service_name }}
        </a-tag>

        <span v-else class="placeholder">—</span>
      </template>

        <template v-else-if="column.key === 'length'">
          {{ (record as CapturedPacket).length }} B
        </template>

       
      </template>

      <template #emptyText>
        <div class="empty-state">
          <div>No packets captured yet</div>
          <div class="empty-hint">
            Start capture, then use YABE or Niagara to send a
            Who-Is request.
          </div>
        </div>
      </template>
    </a-table>

    <a-drawer
      v-model:open="drawerOpen"
      title="Packet Details"
      placement="right"
      width="620"
    >
      <a-spin :spinning="detailLoading">
        <template v-if="selectedPacket">
          <a-descriptions
            bordered
            size="small"
            :column="1"
          >
            <a-descriptions-item label="Packet ID">
              <span class="monospace">
                {{ selectedPacket.packet_id }}
              </span>
            </a-descriptions-item>

            <a-descriptions-item label="Timestamp">
              {{ formatTimestamp(selectedPacket) }}
            </a-descriptions-item>

            <a-descriptions-item label="Direction">
              <a-tag
                :color="
                  directionColor(selectedPacket.direction)
                "
              >
                {{ selectedPacket.direction }}
              </a-tag>
            </a-descriptions-item>

            <a-descriptions-item label="Source">
              <span class="monospace">
                {{
                  endpoint(
                    selectedPacket.source_ip,
                    selectedPacket.source_port,
                  )
                }}
              </span>
            </a-descriptions-item>

            <a-descriptions-item label="Destination">
              <span class="monospace">
                {{
                  endpoint(
                    selectedPacket.destination_ip,
                    selectedPacket.destination_port,
                  )
                }}
              </span>
            </a-descriptions-item>

            <a-descriptions-item label="Length">
              {{ selectedPacket.length }} bytes
            </a-descriptions-item>

            <a-descriptions-item label="BVLC Function">
              {{ selectedPacket.bvlc_function ?? '—' }}
            </a-descriptions-item>

            <a-descriptions-item label="BACnet Service">
             <a-tag
                v-if="selectedPacket.service_name"
                :color="serviceColor(selectedPacket.service_name)"
              >
                {{ selectedPacket.service_name }}
              </a-tag>
              <span v-else>—</span>
            </a-descriptions-item>


            <a-descriptions-item
              v-if="
                packetOperation(selectedPacket) &&
                packetOperation(selectedPacket) !== selectedPacket.service_name
              "
              label="Operation"
            >
              <a-tag color="cyan">
                {{ packetOperation(selectedPacket) }}
              </a-tag>
            </a-descriptions-item>

            <a-descriptions-item
              v-if="packetInvokeId(selectedPacket)"
              label="Invoke ID"
            >
              <span class="monospace">
                {{ packetInvokeId(selectedPacket) }}
              </span>
            </a-descriptions-item>

            <a-descriptions-item
              v-if="packetObjectIdentifier(selectedPacket)"
              label="Object Identifier"
            >
              <span class="monospace">
                {{ packetObjectIdentifier(selectedPacket) }}
              </span>
            </a-descriptions-item>

            <a-descriptions-item
              v-if="packetProperty(selectedPacket)"
              label="Property"
            >
              {{ packetProperty(selectedPacket) }}
            </a-descriptions-item>

            <a-descriptions-item
              v-if="packetArrayIndex(selectedPacket)"
              label="Array Index"
            >
              {{ packetArrayIndex(selectedPacket) }}
            </a-descriptions-item>

            <a-descriptions-item
              v-if="packetPriority(selectedPacket)"
              label="Write Priority"
            >
              {{ packetPriority(selectedPacket) }}
            </a-descriptions-item>

            <a-descriptions-item
              v-if="packetValue(selectedPacket) !== undefined"
              label="Value"
            >
              <span class="packet-detail-value">
                {{ String(packetValue(selectedPacket)) }}
              </span>
            </a-descriptions-item>

            <a-descriptions-item label="Decode Status">
              <a-tag
                :color="
                  decodeStatusColor(
                    selectedPacket.decode_status,
                  )
                "
              >
                {{ selectedPacket.decode_status }}
              </a-tag>
            </a-descriptions-item>

            <a-descriptions-item
              v-if="selectedPacket.decode_error"
              label="Decode Error"
            >
              <span class="decode-error">
                {{ selectedPacket.decode_error }}
              </span>
            </a-descriptions-item>
            <a-descriptions-item
  v-if="selectedPacket.service?.failed_service"
  label="Failed Service"
>
  {{ selectedPacket.service.failed_service }}
</a-descriptions-item>

      <a-descriptions-item
        v-if="selectedPacket.service?.error_class_name"
        label="Error Class"
      >
        {{ selectedPacket.service.error_class_name }}
      </a-descriptions-item>

      <a-descriptions-item
        v-if="selectedPacket.service?.error_code_name"
        label="Error Code"
      >
        <a-tag color="red">
          {{ selectedPacket.service.error_code_name }}
        </a-tag>
      </a-descriptions-item>
          </a-descriptions>

      

          <div class="hex-header">
            Raw BACnet/IP payload
          </div>

          <pre class="packet-hex">{{
            formatHex(selectedPacket.raw_hex)
          }}</pre>


          <div class="protocol-tree-header">
            Protocol Details
          </div>

          <div class="protocol-tree-panel">
            <a-tree
              :tree-data="selectedProtocolTree"
              default-expand-all
              block-node
            >
              <template #title="{ title, value }">
                <div class="protocol-tree-row">
                  <span class="protocol-tree-label">
                    {{ title }}
                  </span>
                  <span
                    v-if="value !== undefined"
                    class="protocol-tree-value"
                  >
                    {{ value }}
                  </span>
                </div>
              </template>
            </a-tree>
          </div>
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

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
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

.result-count {
  color: var(--text-placeholder);
  font-size: 12px;
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