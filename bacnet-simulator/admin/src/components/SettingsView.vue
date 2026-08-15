<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Settings } from '../types'
import UsersDrawer from './UsersDrawer.vue'
import BackupsPanel from './BackupsPanel.vue'

const loading = ref(false)
const savingGeneral = ref(false)
const savingBuffers = ref(false)

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
              <a-form-item label="Tick cadence (seconds)" tooltip="How often the engine advances the simulation and samples object values">
                <a-input-number v-model:value="form.tick_seconds" :min="0.1" :max="3600" :step="0.5" style="width:100%" />
              </a-form-item>
              <a-form-item label="JWT expiry (hours)" tooltip="How long a login session stays valid. Already-issued tokens keep their original expiry.">
                <a-input-number v-model:value="form.jwt_expire_hours" :min="1" :max="8760" style="width:100%" />
              </a-form-item>
              <a-form-item label="Default trend-log interval (seconds)" tooltip="Used when creating a new trend log without specifying one">
                <a-input-number v-model:value="form.trend_log_default_interval" :min="1" style="width:100%" />
              </a-form-item>
              <a-form-item label="Default trend-log buffer size" tooltip="Used when creating a new trend log without specifying one" style="margin-bottom:0">
                <a-input-number v-model:value="form.trend_log_default_buffer_size" :min="1" :max="100000" style="width:100%" />
              </a-form-item>
              <a-divider orientation="left">FMU Runtime</a-divider>
              <a-form-item label="FMU Model Runtime URL" tooltip="Base URL for the generic IoT FMU model runtime">
                <a-input v-model:value="form.fmu_runtime_url" placeholder="http://localhost:8002" />
              </a-form-item>
              <a-form-item label="Request Timeout (seconds)" tooltip="Timeout for FMU model catalog and simulation runtime requests" style="margin-bottom:0">
                <a-input-number v-model:value="form.fmu_runtime_timeout_s" :min="1" :max="120" :step="1" style="width:100%" />
              </a-form-item>
            </a-form>
          </div>
          <a-button type="primary" :loading="savingGeneral" style="margin-top:14px" @click="saveGeneral">Save</a-button>
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
              <a-form-item label="Object history points" tooltip="Per-object value-history ring buffer used for the History chart">
                <a-input-number v-model:value="form.object_history_maxlen" :min="10" :max="100000" style="width:100%" />
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

      <a-tab-pane key="users" tab="Users">
        <UsersDrawer />
      </a-tab-pane>

      <a-tab-pane key="backup" tab="Backup & Restore">
        <BackupsPanel />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>
