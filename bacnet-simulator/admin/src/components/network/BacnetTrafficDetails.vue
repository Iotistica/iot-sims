<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CapturedPacket } from '../../types'
const props = defineProps<{ packet: CapturedPacket }>()
interface ByteRange { start: number; end: number }
interface ProtocolSection { [key: string]: unknown }
interface ProtocolTreeNode { key: string; title: string; value?: string; byteRange?: ByteRange; children?: ProtocolTreeNode[] }
interface ProtocolPacket extends CapturedPacket { details: Record<string, ProtocolSection> }
const activeHexRange = ref<ByteRange | null>(null)
const selectedProtocolKeys = ref<string[]>([])

function sectionValue(
  section: ProtocolSection | null | undefined,
  ...keys: string[]
): unknown {
  if (!section) return undefined

  for (const key of keys) {
    const value = section[key]
    if (value !== undefined && value !== null && value !== '') {
      return value
    }
  }

  return undefined
}

function renderHexHtml(
  value?: string,
  highlight?: ByteRange | null,
): string {
  const bytes = rawBytes(value)
  if (!bytes.length) return ''

  const lines: string[] = []

  for (let i = 0; i < bytes.length; i += 16) {
    const offset = i.toString(16).padStart(4, '0')
    const row: string[] = []

    for (let j = 0; j < 16 && i + j < bytes.length; j += 1) {
      const absolute = i + j
      const hex = bytes[absolute].toString(16).padStart(2, '0')
      const inRange = Boolean(
        highlight &&
        absolute >= highlight.start &&
        absolute <= highlight.end,
      )

      row.push(
        `<span class="hex-byte${inRange ? ' hex-byte--selected' : ''}">${hex}</span>`,
      )
    }

    lines.push(`${offset}  ${row.join(' ')}`)
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
  byteRange?: ByteRange,
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
      byteRange,
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

function protocolTree(
  packet: ProtocolPacket,
): ProtocolTreeNode[] {
  const bytes = rawBytes(packet.raw_hex)
  const lastByte = Math.max(0, bytes.length - 1)

  const clampedRange = (
    start: number,
    end: number,
  ): ByteRange | undefined => {
    if (!bytes.length) return undefined
    const s = Math.max(0, Math.min(start, lastByte))
    const e = Math.max(s, Math.min(end, lastByte))
    return { start: s, end: e }
  }

  const bvlcLength =
    bytes.length >= 4
      ? (bytes[2] << 8) | bytes[3]
      : packet.length

  const bvlcChildren: ProtocolTreeNode[] = [
    {
      key: 'bvlc.type',
      title: 'Type',
      byteRange: clampedRange(0, 0),
      value:
        bytes[0] === 0x81
          ? `BACnet/IP (${hexByte(bytes[0])})`
          : hexByte(bytes[0]),
    },
    {
      key: 'bvlc.function',
      title: 'Function',
      byteRange: clampedRange(1, 1),
      value:
        String(
          sectionValue(
            packet.details.bvlc,
            'function_name',
            'functionName',
          ) ??
          hexByte(bytes[1]),
        ) ??
        hexByte(bytes[1]),
    },
    {
      key: 'bvlc.length',
      title: 'Declared Length',
      byteRange: clampedRange(2, 3),
      value: `${bvlcLength} bytes`,
    },
    ...sectionNodes('bvlc', packet.details.bvlc, clampedRange(0, 3)),
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
      byteRange: clampedRange(npduOffset, npduOffset),
      value:
        npduVersion === undefined
          ? '—'
          : String(npduVersion),
    },
    {
      key: 'npdu.control',
      title: 'Control',
      byteRange: clampedRange(npduOffset + 1, npduOffset + 1),
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
    ...sectionNodes(
      'npdu',
      packet.details.npdu,
      clampedRange(npduOffset, Math.max(npduOffset, apduOffset - 1)),
    ),
  )

  const apduFirst = bytes[apduOffset]
  const apduType =
    apduFirst === undefined ? undefined : apduFirst >> 4

  const apduChildren: ProtocolTreeNode[] = [
    {
      key: 'apdu.type',
      title: 'Type',
      byteRange: clampedRange(apduOffset, apduOffset),
      value:
        apduType === undefined
          ? packet.operation ?? '—'
          : `${apduTypeName(apduType)} (${hexByte(apduFirst)})`,
    },
    {
      key: 'apdu.service',
      title: 'Operation',
      byteRange: clampedRange(apduOffset + 1, apduOffset + 1),
      value:
        String(
          sectionValue(
            packet.details.apdu,
            'service_name',
            'serviceName',
          ) ??
          packet.operation ??
          '',
        ) ||
        'Unknown',
    },
    ...sectionNodes('apdu', packet.details.apdu, clampedRange(apduOffset, lastByte)),
  ]

  const serviceChildren = [
    ...(packet.summary?.object_identifier
      ? [{
          key: 'service.object_identifier',
          title: 'Object Identifier',
          byteRange: clampedRange(apduOffset + 1, lastByte),
          value: String(packet.summary.object_identifier),
        }]
      : []),
    ...(packet.summary?.property
      ? [{
          key: 'service.property',
          title: 'Property',
          byteRange: clampedRange(apduOffset + 1, lastByte),
          value: String(packet.summary.property),
        }]
      : []),
    ...(packet.summary?.value !== undefined &&
    packet.summary?.value !== null
      ? [{
          key: 'service.value',
          title: 'Value',
          byteRange: clampedRange(apduOffset + 1, lastByte),
          value: String(packet.summary.value),
        }]
      : []),
    ...sectionNodes('service', packet.details.service, clampedRange(apduOffset + 1, lastByte)),
  ]

  const tree: ProtocolTreeNode[] = [
    {
      key: 'bvlc',
      title: 'BACnet Virtual Link Control',
      byteRange: clampedRange(0, 3),
      value: `${bvlcLength} bytes`,
      children: bvlcChildren,
    },
    {
      key: 'npdu',
      title:
        'Building Automation and Control Network NPDU',
      byteRange: clampedRange(npduOffset, Math.max(npduOffset, apduOffset - 1)),
      children: npduChildren,
    },
    {
      key: 'apdu',
      title:
        'Building Automation and Control Network APDU',
      byteRange: clampedRange(apduOffset, lastByte),
      children: apduChildren,
    },
  ]

  if (serviceChildren.length) {
    tree.push({
      key: 'service',
      title:
        packet.operation ??
        packet.operation ??
        'BACnet Service',
      byteRange: clampedRange(apduOffset + 1, lastByte),
      children: serviceChildren,
    })
  }

  return tree
}

function onProtocolNodeSelect(
  selectedKeys: Array<string | number>,
  info: unknown,
): void {
  selectedProtocolKeys.value = selectedKeys.map(String)

  const maybeInfo = info as {
    node?: {
      byteRange?: ByteRange
      dataRef?: {
        byteRange?: ByteRange
      }
    }
  } | undefined

  activeHexRange.value =
    maybeInfo?.node?.byteRange ??
    maybeInfo?.node?.dataRef?.byteRange ??
    null
}

const tree = computed(() => protocolTree(props.packet as ProtocolPacket))
const hex = computed(() => renderHexHtml(props.packet.raw_hex, activeHexRange.value))
</script>
<template>
  <div class="hex-header">
    BACnet Frame
  </div>

  <pre class="packet-hex" v-html="hex"></pre>

  <div class="protocol-tree-header">
    Protocol Details
  </div>

  <div class="protocol-tree-panel">
    <a-tree
      :tree-data="tree"
      :selected-keys="selectedProtocolKeys"
      default-expand-all
      block-node
      @select="onProtocolNodeSelect"
    >
      <template #title="{ title, value }">
        <div class="protocol-tree-row">
          <span class="protocol-tree-label">{{ title }}</span>
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
<style scoped>
.hex-header,
.protocol-tree-header {
  margin-top: 18px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, rgba(255, 255, 255, 0.88));
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


</style>
