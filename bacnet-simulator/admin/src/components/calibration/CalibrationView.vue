<script setup lang="ts">
/** Standalone Calibration screen: Select Model, then hand off to
 * CalibrationFlow.vue for Select Recording -> Map Points -> Start
 * Calibration -> Status/Results. Entry point in its own right (mounted via
 * App.vue's activeView switcher) -- not reached through any specific
 * device or recording; see CalibrationDrawer.vue for the device-scoped
 * entry point, which already knows its model and skips this step. */
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ExperimentOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type { CalibrationModelSummary } from '../../types'
import CalibrationFlow from './CalibrationFlow.vue'

const loadingModels = ref(false)
const models = ref<CalibrationModelSummary[]>([])
const selectedModelId = ref<string | null>(null)

async function loadModels() {
  loadingModels.value = true
  try {
    models.value = await api.calibration.listModels()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load models')
  } finally {
    loadingModels.value = false
  }
}
onMounted(loadModels)
</script>

<template>
  <div style="height:100%;padding:20px;overflow:auto;max-width:900px;margin:0 auto">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
      <h2 style="margin:0;font-size:16px"><ExperimentOutlined /> Calibration</h2>
    </div>

    <div v-if="loadingModels" style="padding:24px;text-align:center"><a-spin /></div>

    <template v-else>
      <a-card size="small" title="1. Select Model" style="margin-bottom:16px">
        <a-select
          v-model:value="selectedModelId"
          placeholder="Choose a model"
          style="width:100%"
          show-search
          :filter-option="(input: string, option: any) => option.label.toLowerCase().includes(input.toLowerCase())"
        >
          <a-select-option
            v-for="m in models" :key="m.id" :value="m.id"
            :disabled="!m.calibration_enabled"
            :label="m.label"
          >
            <a-tooltip :title="m.calibration_enabled ? undefined : 'This model has no calibration configuration'">
              <span>{{ m.label }}</span>
              <span v-if="!m.calibration_enabled" style="color:var(--text-placeholder)"> (no calibration config)</span>
            </a-tooltip>
          </a-select-option>
        </a-select>
      </a-card>

      <!-- Keyed on the model so switching models fully resets the rest of
           the flow (recording pick, mapping, any in-flight job state)
           instead of trying to reconcile it in place. -->
      <CalibrationFlow v-if="selectedModelId" :key="selectedModelId" :model-id="selectedModelId" />
    </template>
  </div>
</template>
