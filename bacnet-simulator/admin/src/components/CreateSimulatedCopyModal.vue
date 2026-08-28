<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import { nextFreeInstance } from '../deviceInstance'
import { copyDeviceAndObjects, createReplayDevice } from '../deviceCopy'
import type { Device, ExternalObjectRow, ReplayRecording } from '../types'

const props = defineProps<{
  open: boolean
  sourceDevice: Device | null
  existingInstances: number[]
  // Set when opened via a specific recording's "Create Replay" action
  // (ReplayRecordingDrawer.vue) -- preselects Replay mode and that
  // recording, skipping the normal Standard default. Name/Device Instance
  // stay editable either way. null/undefined for the ordinary "Create
  // Simulation" entry point (device context menu), which still defaults
  // to Standard exactly as before.
  preselectedRecordingId?: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  created: [device: Device]
}>()

const loading = ref(false)
const fetchingSource = ref(false)
const sourceObjects = ref<ExternalObjectRow[]>([])
const simulationMode = ref<'simulation' | 'mirror' | 'replay'>('simulation')
const replayRecordings = ref<ReplayRecording[]>([])
const selectedRecordingId = ref<number | null>(null)
const form = reactive({
  name: '',
  device_instance: 1001,
})

// Only a completed, non-empty recording can drive a Replay device -- see
// ReplayRecordingDrawer.vue's own "Completed"/"Recording" status vocabulary.
const availableRecordings = computed(() =>
  replayRecordings.value.filter(r => r.status === 'completed' && r.sample_count > 0))
const replayAvailable = computed(() => availableRecordings.value.length > 0)

// A fresh read at open-time rather than depending on whatever ObjectsPanel
// happens to have polled — this works correctly even when the source
// device isn't the one currently selected/displayed.
watch(() => props.open, async (v) => {
  if (!v || !props.sourceDevice) return
  const source = props.sourceDevice
  form.name = `${source.name} Copy`
  form.device_instance = nextFreeInstance(props.existingInstances)
  sourceObjects.value = []
  simulationMode.value = props.preselectedRecordingId != null ? 'replay' : 'simulation'
  replayRecordings.value = []
  selectedRecordingId.value = null
  fetchingSource.value = true
  try {
    const [result, recordings] = await Promise.all([
      api.externalObjects.refresh(source.id),
      api.replayRecordings.list(source.id),
    ])
    sourceObjects.value = result.objects
    replayRecordings.value = recordings
    if (props.preselectedRecordingId != null) {
      selectedRecordingId.value = props.preselectedRecordingId
    }
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to read current values from the external device')
  } finally {
    fetchingSource.value = false
  }
})

watch(availableRecordings, (list) => {
  if (!list.some(r => r.id === selectedRecordingId.value)) {
    selectedRecordingId.value = list[0]?.id ?? null
  }
})

const instanceTaken = computed(() => props.existingInstances.includes(form.device_instance))
const canCreate = computed(() =>
  !!form.name.trim() && !instanceTaken.value && !fetchingSource.value
  && (simulationMode.value !== 'replay' || selectedRecordingId.value != null))

async function create() {
  if (!props.sourceDevice) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (instanceTaken.value) { message.error('That device instance is already in use'); return }
  loading.value = true
  try {
    if (simulationMode.value === 'replay') {
      if (selectedRecordingId.value == null) { message.error('Choose a recording'); return }
      const recording = await api.replayRecordings.get(selectedRecordingId.value)
      const { device, objectCount } = await createReplayDevice(props.sourceDevice, {
        name: form.name.trim(),
        deviceInstance: form.device_instance,
        sourceDeviceId: props.sourceDevice.id,
        recording,
      })
      message.success(`Created "${device.name}" with ${objectCount} object${objectCount !== 1 ? 's' : ''}`)
      emit('update:open', false)
      emit('created', device)
      return
    }

    const presentValues: Record<number, unknown> = {}
    for (const o of sourceObjects.value) presentValues[o.id] = o.present_value
    const { device, objectCount } = await copyDeviceAndObjects(props.sourceDevice, sourceObjects.value, {
      name: form.name.trim(),
      deviceInstance: form.device_instance,
      presentValues,
      simulationMode: simulationMode.value,
      sourceDeviceId: simulationMode.value === 'mirror' ? props.sourceDevice.id : null,
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
    :ok-button-props="{ disabled: !canCreate }"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="create"
  >
    <div v-if="sourceDevice" style="padding:4px 0">
      <div style="background:var(--surface-alt);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin-bottom:16px;font-size:12px;color:var(--text-secondary)">
        Source: {{ sourceDevice.name }} · External BACnet · Instance {{ sourceDevice.device_instance }}
        <div style="margin-top:2px">
          Create a simulated copy of this device using its current values. The source device remains unchanged.
        </div>
      </div>

      <div style="margin-bottom:16px">
        <div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Mode</div>
        <a-radio-group v-model:value="simulationMode" class="mode-radio-group">
          <a-radio value="simulation">
            <div class="mode-radio-content">
              <div class="mode-radio-title">Standard</div>
              <div class="mode-radio-desc">Create a standalone simulation from the current device values.</div>
            </div>
          </a-radio>
          <a-radio value="mirror">
            <div class="mode-radio-content">
              <div class="mode-radio-title">Twin</div>
              <div class="mode-radio-desc">Keep the simulation synchronized with the source device.</div>
            </div>
          </a-radio>
          <a-radio value="replay" :disabled="!replayAvailable" :title="replayAvailable ? '' : 'No completed recordings for this device yet.'">
            <div class="mode-radio-content" :class="{ 'mode-radio-content--disabled': !replayAvailable }">
              <div class="mode-radio-title">Replay</div>
              <div class="mode-radio-desc">Record source values and replay them in the simulation.</div>
            </div>
          </a-radio>
        </a-radio-group>
      </div>

      <a-form-item v-if="simulationMode === 'replay'" label="Recording" required style="margin-bottom:16px">
        <a-select v-model:value="selectedRecordingId" placeholder="Choose a recording">
          <a-select-option v-for="r in availableRecordings" :key="r.id" :value="r.id">
            {{ r.name }} ({{ r.sample_count }} samples)
          </a-select-option>
        </a-select>
      </a-form-item>

      <a-form layout="vertical" :colon="false">
        <a-form-item label="Name" required>
          <a-input v-model:value="form.name" placeholder="AHU-1 Copy" />
        </a-form-item>
        <a-form-item label="Device Instance" required :validate-status="instanceTaken ? 'error' : ''" :help="instanceTaken ? 'Already in use — choose a different instance' : ''">
          <a-input-number v-model:value="form.device_instance" :min="1" :max="4194302" style="width:100%" />
        </a-form-item>
      </a-form>

      <div v-if="fetchingSource" style="font-size:12px;color:var(--text-placeholder)">Reading current values from the external device…</div>
      <div v-else-if="simulationMode !== 'replay'" style="font-size:12px;color:var(--text-placeholder)">{{ sourceObjects.length }} object{{ sourceObjects.length !== 1 ? 's' : '' }} will be copied</div>
    </div>
  </a-modal>
</template>

<style scoped>
/* Ant Design's .ant-radio-wrapper default-aligns its content to the
   radio dot's text *baseline*, not its top -- fine for a single line of
   text, but with a two-line title+description block underneath, that
   baseline math (plus each row's own manual margin-left guess, the
   previous approach here) drifted independently per row instead of
   lining every row's text up under a shared left edge. Forcing
   align-items:flex-start plus a single flex column for the title+
   description removes the guesswork entirely -- the gap between dot and
   text becomes the wrapper's own consistent padding, not a hand-picked
   margin. */
.mode-radio-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mode-radio-group :deep(.ant-radio-wrapper) {
  display: flex;
  align-items: flex-start;
}
.mode-radio-content {
  display: flex;
  flex-direction: column;
}
.mode-radio-title {
  font-weight: 500;
}
.mode-radio-desc {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 2px;
}
.mode-radio-content--disabled .mode-radio-title,
.mode-radio-content--disabled .mode-radio-desc {
  color: var(--text-disabled);
}
</style>
