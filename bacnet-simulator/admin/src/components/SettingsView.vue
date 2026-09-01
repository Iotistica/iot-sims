<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Settings } from '../types'
import UsersDrawer from './UsersDrawer.vue'
import BackupsPanel from './BackupsPanel.vue'

const loading = ref(false)
const savingGeneral = ref(false)
const savingSimulation = ref(false)
const savingBuffers = ref(false)
const savingAi = ref(false)

const form = reactive<Settings>({
  tick_seconds: 5.0,
  device_log_maxlen: 300,
  global_log_maxlen: 1000,
  metrics_errors_maxlen: 200,
  metrics_new_devices_maxlen: 200,
  metrics_duplicate_id_maxlen: 100,
  metrics_traffic_feed_maxlen: 500,
  object_history_maxlen: 720,
  trend_log_default_interval: 60,
  trend_log_default_buffer_size: 1000,
  jwt_expire_hours: 24,
  fmu_runtime_url: 'http://localhost:8002',
  fmu_runtime_timeout_s: 20,
  fmu_runtime_api_key: '',
  azure_openai_endpoint: '',
  azure_openai_api_key: '',
  azure_openai_deployment: '',
  azure_openai_api_version: '2024-10-21',
  llm_provider: 'azure_openai',
  openai_api_key: '',
  openai_model: '',
  openai_compatible_base_url: '',
  openai_compatible_api_key: '',
  openai_compatible_model: '',
})

async function load() {
  loading.value = true
  try {
    Object.assign(form, await api.settings.get())
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load settings')
  } finally {
    loading.value = false
  }
}

async function saveGeneral() {
  savingGeneral.value = true
  try {
    Object.assign(form, await api.settings.update({ ...form }))
    message.success('Settings saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save settings')
  } finally {
    savingGeneral.value = false
  }
}

async function saveSimulation() {
  savingSimulation.value = true
  try {
    Object.assign(form, await api.settings.update({ ...form }))
    message.success('Settings saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save settings')
  } finally {
    savingSimulation.value = false
  }
}

async function saveBuffers() {
  savingBuffers.value = true
  try {
    Object.assign(form, await api.settings.update({ ...form }))
    message.success('Settings saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save settings')
  } finally {
    savingBuffers.value = false
  }
}

async function saveAi() {
  savingAi.value = true
  try {
    Object.assign(form, await api.settings.update({ ...form }))
    message.success('Settings saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save settings')
  } finally {
    savingAi.value = false
  }
}

onMounted(load)
</script>

<template>
  <div style="padding:20px;overflow:auto;height:100%">
    <h2 style="margin:0 0 16px;font-size:16px">Settings</h2>

    <a-tabs>
      <a-tab-pane key="general" tab="General">
        <a-spin :spinning="loading">
          <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:16px;max-width:480px">
            <a-form layout="vertical">
              <a-form-item label="JWT expiry (hours)" tooltip="How long a login session stays valid. Already-issued tokens keep their original expiry." style="margin-bottom:0">
                <a-input-number v-model:value="form.jwt_expire_hours" :min="1" :max="8760" style="width:100%" />
              </a-form-item>
            </a-form>
          </div>
          <a-button type="primary" :loading="savingGeneral" style="margin-top:14px" @click="saveGeneral">Save</a-button>
        </a-spin>
      </a-tab-pane>

      <a-tab-pane key="simulation" tab="Simulation">
        <a-spin :spinning="loading">
          <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:16px;max-width:480px">
            <a-form layout="vertical">
              <a-form-item label="Tick Interval (seconds)" tooltip="How often the engine advances the simulation and samples object values">
                <a-input-number v-model:value="form.tick_seconds" :min="0.1" :max="3600" :step="0.5" style="width:100%" />
              </a-form-item>

              <a-divider orientation="left">History &amp; Trending</a-divider>
              <a-form-item label="Object History Buffer (samples)" tooltip="Per-object value-history ring buffer used for the History chart">
                <a-input-number v-model:value="form.object_history_maxlen" :min="10" :max="100000" style="width:100%" />
              </a-form-item>
              <a-form-item label="Trend Log Default Interval (seconds)" tooltip="Used when creating a new trend log without specifying one">
                <a-input-number v-model:value="form.trend_log_default_interval" :min="1" style="width:100%" />
              </a-form-item>
              <a-form-item label="Trend Log Default Buffer (records)" tooltip="Used when creating a new trend log without specifying one">
                <a-input-number v-model:value="form.trend_log_default_buffer_size" :min="1" :max="100000" style="width:100%" />
              </a-form-item>

              <a-divider orientation="left">FMU Runtime</a-divider>
              <a-form-item label="Runtime URL" tooltip="Base URL for the generic IoT FMU model runtime">
                <a-input v-model:value="form.fmu_runtime_url" placeholder="http://localhost:8002" />
              </a-form-item>
              <a-form-item label="Runtime Timeout (seconds)" tooltip="Timeout for FMU model catalog and simulation runtime requests">
                <a-input-number v-model:value="form.fmu_runtime_timeout_s" :min="1" :max="120" :step="1" style="width:100%" />
              </a-form-item>
              <a-form-item label="Runtime API Key" tooltip="Sent as X-API-Key on every request. Leave blank if the runtime doesn't require one." style="margin-bottom:0">
                <a-input-password v-model:value="form.fmu_runtime_api_key" placeholder="Optional" autocomplete="new-password" />
              </a-form-item>
            </a-form>
          </div>
          <a-button type="primary" :loading="savingSimulation" style="margin-top:14px" @click="saveSimulation">Save</a-button>
        </a-spin>
      </a-tab-pane>

      <a-tab-pane key="buffers" tab="Buffers & Retention">
        <a-spin :spinning="loading">
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
            Shrinking a value truncates the in-memory buffer to the newest entries immediately.
          </div>
          <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:16px;max-width:480px">
            <a-form layout="vertical">
              <a-form-item label="Per-device log entries" tooltip="Activity log retained per device">
                <a-input-number v-model:value="form.device_log_maxlen" :min="10" :max="10000" style="width:100%" />
              </a-form-item>
              <a-form-item label="Global log entries" tooltip="Activity log retained across all devices combined">
                <a-input-number v-model:value="form.global_log_maxlen" :min="10" :max="50000" style="width:100%" />
              </a-form-item>
              <a-form-item label="Recent errors (analytics)">
                <a-input-number v-model:value="form.metrics_errors_maxlen" :min="10" :max="10000" style="width:100%" />
              </a-form-item>
              <a-form-item label="New-devices timeline (analytics)">
                <a-input-number v-model:value="form.metrics_new_devices_maxlen" :min="10" :max="10000" style="width:100%" />
              </a-form-item>
              <a-form-item label="Duplicate-ID events (analytics)">
                <a-input-number v-model:value="form.metrics_duplicate_id_maxlen" :min="10" :max="10000" style="width:100%" />
              </a-form-item>
              <a-form-item label="Live traffic feed (analytics)" style="margin-bottom:0">
                <a-input-number v-model:value="form.metrics_traffic_feed_maxlen" :min="10" :max="10000" style="width:100%" />
              </a-form-item>
            </a-form>
          </div>
          <a-button type="primary" :loading="savingBuffers" style="margin-top:14px" @click="saveBuffers">Save</a-button>
        </a-spin>
      </a-tab-pane>

      <a-tab-pane key="ai" tab="AI Suggestions">
        <a-spin :spinning="loading">
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
            Used by "Use AI" in the Simulation Model mapping review and the object tree's semantic
            suggestions -- only called when you explicitly ask for it, never automatically. Leave
            the selected provider's fields blank to disable; falls back to the matching environment
            variables when unset.
          </div>
          <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:16px;max-width:480px">
            <a-form layout="vertical">
              <a-form-item label="Provider" style="margin-bottom:16px">
                <a-select v-model:value="form.llm_provider" style="width:100%" :options="[
                  { value: 'azure_openai', label: 'Azure OpenAI' },
                  { value: 'openai', label: 'OpenAI' },
                  { value: 'openai_compatible', label: 'OpenAI-Compatible (Custom)' },
                ]" />
              </a-form-item>

              <template v-if="form.llm_provider === 'azure_openai'">
                <a-form-item label="Endpoint" tooltip="Azure OpenAI resource endpoint, e.g. https://my-resource.openai.azure.com">
                  <a-input v-model:value="form.azure_openai_endpoint" placeholder="https://my-resource.openai.azure.com" />
                </a-form-item>
                <a-form-item label="API Key">
                  <a-input-password v-model:value="form.azure_openai_api_key" placeholder="Azure OpenAI API key" autocomplete="new-password" />
                </a-form-item>
                <a-form-item label="Deployment" tooltip="Deployment name, not the underlying model name">
                  <a-input v-model:value="form.azure_openai_deployment" placeholder="gpt-4o-mapping" />
                </a-form-item>
                <a-form-item label="API Version" style="margin-bottom:0">
                  <a-input v-model:value="form.azure_openai_api_version" placeholder="2024-10-21" />
                </a-form-item>
              </template>

              <template v-else-if="form.llm_provider === 'openai'">
                <a-form-item label="API Key">
                  <a-input-password v-model:value="form.openai_api_key" placeholder="OpenAI API key" autocomplete="new-password" />
                </a-form-item>
                <a-form-item label="Model" style="margin-bottom:0">
                  <a-input v-model:value="form.openai_model" placeholder="gpt-4o-mini" />
                </a-form-item>
              </template>

              <template v-else-if="form.llm_provider === 'openai_compatible'">
                <a-form-item label="Base URL" tooltip="Any OpenAI-compatible chat completions endpoint, e.g. a local Ollama/vLLM server or a hosted provider like Groq/Together">
                  <a-input v-model:value="form.openai_compatible_base_url" placeholder="http://localhost:11434/v1" />
                </a-form-item>
                <a-form-item label="API Key" tooltip="Leave blank if the endpoint doesn't require one">
                  <a-input-password v-model:value="form.openai_compatible_api_key" placeholder="API key (if required)" autocomplete="new-password" />
                </a-form-item>
                <a-form-item label="Model" style="margin-bottom:0">
                  <a-input v-model:value="form.openai_compatible_model" placeholder="llama3.1" />
                </a-form-item>
              </template>
            </a-form>
          </div>
          <a-button type="primary" :loading="savingAi" style="margin-top:14px" @click="saveAi">Save</a-button>
        </a-spin>
      </a-tab-pane>

      <a-tab-pane key="users" tab="Users">
        <UsersDrawer />
      </a-tab-pane>

      <a-tab-pane key="backup" tab="Backup & Restore">
        <BackupsPanel />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>
