<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { BACnetConnectionConfig, BACnetDiscoveryConnection } from '../types'

const props = defineProps<{
  open: boolean
  discoveryConnections: BACnetDiscoveryConnection[]
  activeProjectId: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  discovered: [savedConnections: BACnetDiscoveryConnection[] | null]
}>()

const savedConnections = ref<BACnetDiscoveryConnection[]>([])
const selectedConnectionId = ref<number | null>(null)
const connectionName = ref('')
const discoveryTarget = ref('')
const deviceInstanceLow = ref(0)
const deviceInstanceHigh = ref(4194303)
const timeoutMs = ref(5000)
const enabled = ref(true)
const discovering = ref(false)
const saving = ref(false)

const canSaveConnection = computed(() => props.activeProjectId != null)

function defaultConnectionName(config: BACnetConnectionConfig): string {
  return config.discovery_target?.trim() || 'Local BACnet'
}

function targetLabel(connection: BACnetDiscoveryConnection): string {
  return connection.target?.trim() || 'local broadcast'
}

function resetForm() {
  selectedConnectionId.value = null
  connectionName.value = ''
  discoveryTarget.value = ''
  deviceInstanceLow.value = 0
  deviceInstanceHigh.value = 4194303
  timeoutMs.value = 5000
  enabled.value = true
}

function loadConnection(connection: BACnetDiscoveryConnection) {
  selectedConnectionId.value = connection.id
  connectionName.value = connection.name
  discoveryTarget.value = connection.target ?? ''
  deviceInstanceLow.value = connection.device_instance_low
  deviceInstanceHigh.value = connection.device_instance_high
  timeoutMs.value = connection.timeout_ms
  enabled.value = connection.enabled
}

function currentConfig(): BACnetConnectionConfig {
  return {
    discovery_target: discoveryTarget.value.trim() || null,
    device_instance_low: deviceInstanceLow.value,
    device_instance_high: deviceInstanceHigh.value,
    timeout_ms: timeoutMs.value,
  }
}

function currentPayload(): Omit<BACnetDiscoveryConnection, 'id'> {
  const config = currentConfig()
  return {
    name: connectionName.value.trim() || defaultConnectionName(config),
    target: config.discovery_target,
    device_instance_low: config.device_instance_low,
    device_instance_high: config.device_instance_high,
    timeout_ms: config.timeout_ms,
    enabled: enabled.value,
  }
}

function replaceConnection(connection: BACnetDiscoveryConnection) {
  const index = savedConnections.value.findIndex(c => c.id === connection.id)
  if (index === -1) savedConnections.value = [...savedConnections.value, connection]
  else savedConnections.value = savedConnections.value.map(c => c.id === connection.id ? connection : c)
  emit('discovered', savedConnections.value)
}

async function saveCurrentConnection(showToast = true): Promise<BACnetDiscoveryConnection | null> {
  if (!canSaveConnection.value) {
    message.warning('Save the project before saving discovery connections.')
    return null
  }

  saving.value = true
  try {
    const payload = currentPayload()
    const connection = selectedConnectionId.value == null
      ? await api.discovery.connections.create(props.activeProjectId!, payload)
      : await api.discovery.connections.update(props.activeProjectId!, selectedConnectionId.value, payload)
    replaceConnection(connection)
    loadConnection(connection)
    if (showToast) message.success(`"${connection.name}" saved`)
    return connection
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save discovery connection')
    return null
  } finally {
    saving.value = false
  }
}

async function deleteConnection(connection: BACnetDiscoveryConnection) {
  if (props.activeProjectId == null) return
  try {
    await api.discovery.connections.delete(props.activeProjectId, connection.id)
    savedConnections.value = savedConnections.value.filter(c => c.id !== connection.id)
    emit('discovered', savedConnections.value)
    if (selectedConnectionId.value === connection.id) resetForm()
    message.success(`"${connection.name}" removed`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to delete discovery connection')
  }
}

async function doDiscover() {
  discovering.value = true
  try {
    const result = await api.discovery.sync(currentConfig())
    message.success(result.devices.length
      ? `Discovered ${result.devices.length} device${result.devices.length !== 1 ? 's' : ''}`
      : 'No devices found')
    emit('discovered', null)
    emit('update:open', false)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Discovery failed')
  } finally {
    discovering.value = false
  }
}

watch(() => props.open, (v) => {
  if (!v) return
  savedConnections.value = props.discoveryConnections.map(c => ({ ...c }))
  if (savedConnections.value.length) loadConnection(savedConnections.value[0])
  else resetForm()
})
</script>

<template>
  <a-modal
    :open="open"
    title="Discovery Connections"
    :confirm-loading="discovering"
    @update:open="(v: boolean) => emit('update:open', v)"
  >
    <div v-if="savedConnections.length" style="margin-bottom:14px">
      <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;margin-bottom:6px">
        Saved Connections
      </div>
      <div
        v-for="connection in savedConnections"
        :key="connection.id"
        style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)"
      >
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:6px">
            <span style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ connection.name }}</span>
            <a-tag v-if="!connection.enabled" color="default" style="font-size:10px;line-height:16px;margin:0">Disabled</a-tag>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            {{ targetLabel(connection) }}
          </div>
        </div>
        <a-button size="small" title="Edit" @click="loadConnection(connection)">
          <template #icon><EditOutlined /></template>
        </a-button>
        <a-button size="small" danger title="Delete" @click="deleteConnection(connection)">
          <template #icon><DeleteOutlined /></template>
        </a-button>
      </div>
    </div>

    <a-form layout="vertical" style="margin-top:8px">
      <a-form-item
        label="Discovery Target"
        help="Leave empty to use normal local BACnet broadcast discovery. Enter an IP/host when broadcast is unavailable or when targeting a known BACnet host/gateway."
      >
        <a-input v-model:value="discoveryTarget" placeholder="e.g. 10.20.1.255" />
      </a-form-item>

      <a-form-item label="Device Instance Range" style="margin-bottom:12px">
        <a-input-group compact>
          <a-input-number v-model:value="deviceInstanceLow" :min="0" :max="4194303" addon-before="Low" style="width:50%" />
          <a-input-number v-model:value="deviceInstanceHigh" :min="0" :max="4194303" addon-before="High" style="width:50%" />
        </a-input-group>
      </a-form-item>

      <a-form-item label="Discovery Timeout (ms)" style="margin-bottom:0">
        <a-input-number v-model:value="timeoutMs" :min="100" :max="60000" style="width:100%" />
      </a-form-item>

      <a-form-item label="Connection Name" style="margin-top:12px;margin-bottom:10px">
        <a-input v-model:value="connectionName" placeholder="e.g. Mechanical VLAN" />
      </a-form-item>
      <a-checkbox v-model:checked="enabled">Enabled for All Connections discovery</a-checkbox>
      <div v-if="!canSaveConnection" style="font-size:12px;color:var(--text-secondary);margin-top:8px">
        Save the project before saving discovery connections.
      </div>
    </a-form>

    <template #footer>
      <a-button @click="resetForm">
        <template #icon><PlusOutlined /></template>
        New
      </a-button>
      <a-button @click="emit('update:open', false)">Close</a-button>
      <a-button :disabled="!canSaveConnection" :loading="saving" @click="saveCurrentConnection()">
        Save Connection
      </a-button>
      <a-button type="primary" :loading="discovering" @click="doDiscover">
        Discover
      </a-button>
    </template>
  </a-modal>
</template>
