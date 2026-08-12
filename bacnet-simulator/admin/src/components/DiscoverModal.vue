<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { BACnetConnectionConfig } from '../types'

const props = defineProps<{
  open: boolean
  // The project's currently remembered connection, if any — authoritative
  // from the backend (never localStorage), so this modal always reflects
  // what's actually stored for the active project.
  connectionConfig: BACnetConnectionConfig | null
  activeProjectId: number | null
  activeProjectName: string | null
  activeProjectDesc: string
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  // The config that was just remembered, or null if "Remember connection"
  // was off or the remember write failed — App.vue only updates its cached
  // value when this is non-null, so an unremembered/failed attempt leaves
  // the project's previously remembered connection untouched.
  discovered: [rememberedConfig: BACnetConnectionConfig | null]
}>()

const discoveryTarget = ref('')
const deviceInstanceLow = ref(0)
const deviceInstanceHigh = ref(4194303)
const timeoutMs = ref(5000)
const remember = ref(false)
const discovering = ref(false)

watch(() => props.open, (v) => {
  if (!v) return
  const cfg = props.connectionConfig
  discoveryTarget.value = cfg?.discovery_target ?? ''
  deviceInstanceLow.value = cfg?.device_instance_low ?? 0
  deviceInstanceHigh.value = cfg?.device_instance_high ?? 4194303
  timeoutMs.value = cfg?.timeout_ms ?? 5000
  remember.value = !!cfg
})

async function doDiscover() {
  discovering.value = true
  try {
    const config: BACnetConnectionConfig = {
      discovery_target: discoveryTarget.value.trim() || null,
      device_instance_low: deviceInstanceLow.value,
      device_instance_high: deviceInstanceHigh.value,
      timeout_ms: timeoutMs.value,
    }

    const result = await api.discovery.sync(config)

    // Remembering the connection is a separate, independently-failable step
    // from discovery itself — a failure here must never undo or be reported
    // as a discovery failure.
    let rememberFailed = false
    if (remember.value && props.activeProjectId != null) {
      try {
        await api.projects.update(props.activeProjectId, props.activeProjectName ?? '', props.activeProjectDesc, config)
      } catch {
        rememberFailed = true
      }
    }

    const countMsg = result.devices.length
      ? `Discovered ${result.devices.length} device${result.devices.length !== 1 ? 's' : ''}`
      : 'No devices found'
    if (rememberFailed) {
      message.warning(`${countMsg}, but connection settings could not be remembered.`)
    } else {
      message.success(countMsg)
    }

    emit('discovered', (remember.value && !rememberFailed) ? config : null)
    emit('update:open', false)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Discovery failed')
  } finally {
    discovering.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title=""
    ok-text="Discover"
    :confirm-loading="discovering"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="doDiscover"
  >
    <a-form layout="vertical" style="margin-top:8px">
      <a-form-item
        label="Discovery Target"
        help="Leave empty to use normal local BACnet broadcast discovery. Enter an IP/host when broadcast is unavailable or when targeting a known BACnet host/gateway."
      >
        <a-input v-model:value="discoveryTarget" placeholder="e.g. 192.168.1.50 (optional)" />
      </a-form-item>

      <a-collapse ghost style="margin-bottom:12px">
        <a-collapse-panel key="advanced" header="Advanced">
          <a-form-item label="Device Instance Range" style="margin-bottom:12px">
            <a-input-group compact>
              <a-input-number v-model:value="deviceInstanceLow" :min="0" :max="4194303" addon-before="Low" style="width:50%" />
              <a-input-number v-model:value="deviceInstanceHigh" :min="0" :max="4194303" addon-before="High" style="width:50%" />
            </a-input-group>
          </a-form-item>
          <a-form-item label="Discovery Timeout (ms)" style="margin-bottom:0">
            <a-input-number v-model:value="timeoutMs" :min="100" :max="60000" style="width:100%" />
          </a-form-item>
        </a-collapse-panel>
      </a-collapse>

      <a-checkbox v-model:checked="remember">Remember connection</a-checkbox>
    </a-form>
  </a-modal>
</template>
