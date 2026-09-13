<script setup lang="ts">
/** Training, opened for one specific device from its own Simulation menu
 * (ObjectsPanel.vue) -- the model is already known (the device's own
 * active_simulation_model), same shape as calibration/CalibrationDrawer.vue.
 * Gated on any attached model (fmu or ai), not just 'ai' -- covers both
 * training a first ONNX model from an FMU-running device's own recordings,
 * and retraining/extending an already-attached AI model (see
 * ObjectsPanel.vue's trainingModel).
 *
 * Two modes, same underlying task_type distinction (regression vs
 * classification, see CreateTrainingExportJobRequest) presented in
 * building-automation language rather than ML terms: "Create Simulation
 * Model" (the existing surrogate flow, TrainingFlow.vue, unchanged) and
 * "Create Fault Detection Model" (ClassifierTrainingFlow.vue, new) --
 * both trained from this same device's own recordings and Simulation
 * Model input mapping, just predicting different things. */
import { ref, watch } from 'vue'
import type { Device } from '../../types'
import TrainingFlow from './TrainingFlow.vue'
import ClassifierTrainingFlow from './ClassifierTrainingFlow.vue'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

function close() {
  emit('update:open', false)
}

const mode = ref<'simulation' | 'detection'>('simulation')
// Reset to the entry choice every time the drawer opens for a (possibly
// different) device, rather than remembering the last mode across devices.
watch(() => props.open, (open) => { if (open) mode.value = 'simulation' })
</script>

<template>
  <a-drawer
    :open="open"
    :title="device ? `Training — ${device.name}` : 'Training'"
    width="560"
    @close="close"
  >
    <template v-if="device?.active_simulation_model">
      <div style="font-size:12px;color:var(--text-placeholder);margin-bottom:16px">
        Model: {{ device.active_simulation_model.name }}
      </div>

      <a-radio-group v-model:value="mode" style="width:100%;margin-bottom:16px">
        <a-radio-button value="simulation" style="width:50%;text-align:center">Create Simulation Model</a-radio-button>
        <a-radio-button value="detection" style="width:50%;text-align:center">Create Fault Detection Model</a-radio-button>
      </a-radio-group>
      <div style="font-size:12px;color:var(--text-placeholder);margin-bottom:16px">
        <template v-if="mode === 'simulation'">Learn equipment behavior to reproduce point values.</template>
        <template v-else>Learn from recorded equipment behavior to recognize conditions and faults.</template>
      </div>

      <!-- Keyed on the device (and mode) so switching resets the flow
           instead of reconciling it in place. -->
      <TrainingFlow
        v-if="mode === 'simulation'"
        :key="`sim-${device.id}`"
        :model-id="device.active_simulation_model.model_type" :device-id="device.id" :device="device"
      />
      <ClassifierTrainingFlow
        v-else
        :key="`clf-${device.id}`"
        :model-id="device.active_simulation_model.model_type" :device-id="device.id" :device="device"
      />
    </template>
    <a-alert
      v-else
      type="warning" show-icon
      message="No Simulation Model attached"
      description="Attach a Simulation Model to this device before training it."
    />
  </a-drawer>
</template>
