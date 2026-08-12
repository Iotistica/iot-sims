<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import { nextFreeInstance } from '../deviceInstance'
import { copyDeviceAndObjects } from '../deviceCopy'
import type { Device, ExternalObjectRow } from '../types'

const props = defineProps<{
  open: boolean
  sourceDevice: Device | null
  existingInstances: number[]
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  created: [device: Device]
}>()

const loading = ref(false)
const fetchingSource = ref(false)
const sourceObjects = ref<ExternalObjectRow[]>([])
const simulationMode = ref<'mirror' | 'replay'>('mirror')
const form = reactive({
  name: '',
  device_instance: 1001,
})

// A fresh read at open-time rather than depending on whatever ObjectsPanel
// happens to have polled — this works correctly even when the source
// device isn't the one currently selected/displayed.
watch(() => props.open, async (v) => {
  if (!v || !props.sourceDevice) return
  const source = props.sourceDevice
  form.name = `${source.name} Copy`
  form.device_instance = nextFreeInstance(props.existingInstances)
  sourceObjects.value = []
  simulationMode.value = 'mirror'
  fetchingSource.value = true
  try {
    const result = await api.externalObjects.refresh(source.id)
    sourceObjects.value = result.objects
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to read current values from the external device')
  } finally {
    fetchingSource.value = false
  }
})

const instanceTaken = computed(() => props.existingInstances.includes(form.device_instance))

async function create() {
  if (!props.sourceDevice) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (instanceTaken.value) { message.error('That device instance is already in use'); return }
  loading.value = true
  try {
    const presentValues: Record<number, unknown> = {}
    for (const o of sourceObjects.value) presentValues[o.id] = o.present_value
    const { device, objectCount } = await copyDeviceAndObjects(props.sourceDevice, sourceObjects.value, {
      name: form.name.trim(),
      deviceInstance: form.device_instance,
      presentValues,
      simulationMode: simulationMode.value,
      sourceDeviceId: props.sourceDevice.id,
    })
    message.success(`Created "${device.name}" with ${objectCount} object${objectCount !== 1 ? 's' : ''}`)
    emit('update:open', false)
    emit('created', device)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to create simulated copy')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title="Create Simulation"
    ok-text="Create"
    :confirm-loading="loading"
    :ok-button-props="{ disabled: !form.name.trim() || instanceTaken || fetchingSource }"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="create"
  >
    <div v-if="sourceDevice" style="padding:4px 0">
      <div style="background:var(--surface-alt);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin-bottom:16px;font-size:12px;color:var(--text-secondary)">
        Source: {{ sourceDevice.name }} · External BACnet · Instance {{ sourceDevice.device_instance }}
        <div style="margin-top:2px">
          Creates an independent simulated device seeded with the current reading of each object. The external device is untouched and keeps reading the real network.
        </div>
      </div>

      <div style="margin-bottom:16px">
        <div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Mode</div>
        <a-radio-group v-model:value="simulationMode" style="display:flex;flex-direction:column;gap:8px">
          <a-radio value="mirror">
            <span style="font-weight:500">Mirror</span>
            <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;margin-left:24px">Follow values from the external device in real time.</div>
          </a-radio>
          <a-tooltip title="Replay mode is not yet available.">
            <a-radio value="replay" :disabled="true">
              <span style="font-weight:500;color:var(--text-disabled)">Replay</span>
              <div style="font-size:12px;color:var(--text-disabled);margin-top:2px;margin-left:24px">Record live values and replay them independently.</div>
            </a-radio>
          </a-tooltip>
        </a-radio-group>
      </div>

      <a-form layout="vertical" :colon="false">
        <a-form-item label="Name" required>
          <a-input v-model:value="form.name" placeholder="AHU-1 Copy" />
        </a-form-item>
        <a-form-item label="Device Instance" required :validate-status="instanceTaken ? 'error' : ''" :help="instanceTaken ? 'Already in use — choose a different instance' : ''">
          <a-input-number v-model:value="form.device_instance" :min="1" :max="4194302" style="width:100%" />
        </a-form-item>
      </a-form>

      <div v-if="fetchingSource" style="font-size:12px;color:var(--text-placeholder)">Reading current values from the external device…</div>
      <div v-else style="font-size:12px;color:var(--text-placeholder)">{{ sourceObjects.length }} object{{ sourceObjects.length !== 1 ? 's' : '' }} will be copied</div>
    </div>
  </a-modal>
</template>
