<script setup lang="ts">
/** Calibration, opened for one specific device from its own "More actions"
 * menu (ObjectsPanel.vue) -- the model is already known (the device's own
 * active_simulation_model), so this skips straight to Select Recording via
 * CalibrationFlow.vue. See CalibrationView.vue for the standalone,
 * model-agnostic entry point this shares its flow with. */
import type { Device } from '../../types'
import CalibrationFlow from './CalibrationFlow.vue'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

function close() {
  emit('update:open', false)
}
</script>

<template>
  <a-drawer
    :open="open"
    :title="device ? `Calibration — ${device.name}` : 'Calibration'"
    width="560"
    @close="close"
  >
    <template v-if="device?.active_simulation_model">
      <div style="font-size:12px;color:var(--text-placeholder);margin-bottom:16px">
        Model: {{ device.active_simulation_model.name }}
      </div>
      <!-- Keyed on the device so switching which device this drawer is open
           for fully resets the flow instead of reconciling it in place. -->
      <CalibrationFlow :key="device.id" :model-id="device.active_simulation_model.model_type" />
    </template>
    <a-alert
      v-else
      type="warning" show-icon
      message="No Simulation Model attached"
      description="Attach a Simulation Model to this device before calibrating it."
    />
  </a-drawer>
</template>
