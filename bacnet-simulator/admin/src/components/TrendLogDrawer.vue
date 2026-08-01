<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { PlusOutlined, EditOutlined, DeleteOutlined, ThunderboltOutlined, ClearOutlined, UnorderedListOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, SimObject, TrendLog, TrendLogRecord } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const list = ref<TrendLog[]>([])
const allObjects = ref<SimObject[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<TrendLog | null>(null)
const formOpen = ref(false)

const LOGGING_TYPES = [
  { label: 'Polled (sample every N seconds)', value: 'polled' },
  { label: 'COV (sample on value change)', value: 'cov' },
  { label: 'Triggered (manual sample only)', value: 'triggered' },
]

const form = reactive({
  name: '',
  description: '',
  monitored_object_id: null as number | null,
  logging_type: 'polled',
  log_interval: 60,
  cov_increment: 1.0,
  buffer_size: 1000,
  stop_when_full: 0,
  enabled: true,
})

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    const [logs, objects] = await Promise.all([
      api.trendLogs.list(props.device.id),
      api.objects.list(props.device.id),
    ])
    list.value = logs
    allObjects.value = objects
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load trend logs')
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  if (v) load()
  else { formOpen.value = false; editing.value = null }
})

function resetForm() {
  Object.assign(form, {
    name: '', description: '', monitored_object_id: null, logging_type: 'polled',
    log_interval: 60, cov_increment: 1.0, buffer_size: 1000, stop_when_full: 0, enabled: true,
  })
}

function openAdd() {
  editing.value = null
  resetForm()
  formOpen.value = true
}

function openEdit(tl: TrendLog) {
  editing.value = tl
  Object.assign(form, {
    name: tl.name,
    description: tl.description,
    monitored_object_id: tl.monitored_object_id,
    logging_type: tl.logging_type,
    log_interval: tl.log_interval,
    cov_increment: tl.cov_increment,
    buffer_size: tl.buffer_size,
    stop_when_full: tl.stop_when_full,
    enabled: !!tl.enabled,
  })
  formOpen.value = true
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (!form.monitored_object_id) { message.error('Choose an object to monitor'); return }

  saving.value = true
  try {
    const body = {
      name: form.name.trim(),
      description: form.description,
      monitored_object_id: form.monitored_object_id,
      logging_type: form.logging_type,
      log_interval: form.log_interval,
      cov_increment: form.cov_increment,
      buffer_size: form.buffer_size,
      stop_when_full: form.stop_when_full ? 1 : 0,
      enabled: form.enabled ? 1 : 0,
    }
    if (editing.value) {
      await api.trendLogs.update(editing.value.id, body as any)
      message.success('Trend log updated')
    } else {
      await api.trendLogs.create(props.device.id, body as any)
      message.success('Trend log created')
    }
    formOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Save failed')
  } finally {
    saving.value = false
  }
}

function objectLabel(o: SimObject): string {
  return `${o.name} (${o.object_type})`
}
function monitoredLabel(tl: TrendLog): string {
  const o = allObjects.value.find(x => x.id === tl.monitored_object_id)
  return o ? o.name : `object #${tl.monitored_object_id}`
}

function confirmDelete(tl: TrendLog) {
  Modal.confirm({
    title: `Delete "${tl.name}"?`,
    content: 'This also deletes all of its recorded samples.',
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.trendLogs.del(tl.id)
        message.success('Deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Delete failed')
      }
    },
  })
}

async function trigger(tl: TrendLog) {
  try {
    const result = await api.trendLogs.trigger(tl.id)
    message.success(`Sampled: ${result.value} (seq ${result.sequence_number})`)
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Trigger failed')
  }
}

function confirmClear(tl: TrendLog) {
  Modal.confirm({
    title: `Clear all records for "${tl.name}"?`,
    okType: 'danger',
    okText: 'Clear',
    async onOk() {
      try {
        await api.trendLogs.clear(tl.id)
        message.success('Records cleared')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Clear failed')
      }
    },
  })
}

// ── Records viewer ──────────────────────────────────────────────────────────

const recordsOpen = ref(false)
const recordsLoading = ref(false)
const recordsFor = ref<TrendLog | null>(null)
const records = ref<TrendLogRecord[]>([])

const recordColumns: TableColumnsType = [
  { title: 'Seq', dataIndex: 'sequence_number', key: 'sequence_number', width: 80 },
  { title: 'Time', dataIndex: 'ts', key: 'ts' },
  { title: 'Value', dataIndex: 'value', key: 'value' },
]

async function viewRecords(tl: TrendLog) {
  recordsFor.value = tl
  recordsOpen.value = true
  await loadRecords()
}

async function loadRecords() {
  if (!recordsFor.value) return
  recordsLoading.value = true
  try {
    records.value = await api.trendLogs.records(recordsFor.value.id, { order: 'desc', limit: 500 })
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load records')
  } finally {
    recordsLoading.value = false
  }
}
</script>

<template>
  <a-drawer
    :title="device ? `Trend Logs — ${device.name}` : 'Trend Logs'"
    :open="open"
    width="560"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
        Records a monitored object's value over time into a circular buffer. Polled logging samples on
        an interval; triggered logging only records when you (or the API) request a sample.
      </div>
      <a-button type="primary" block :disabled="!allObjects.length" @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Add Trend Log
      </a-button>
      <div v-if="!allObjects.length" style="font-size:12px;color:#faad14;margin-top:-10px;margin-bottom:16px">
        This device has no points to monitor yet.
      </div>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:var(--text-placeholder);padding:40px 0;font-size:13px">
          No trend logs yet
        </div>
        <div
          v-for="tl in list" :key="tl.id"
          style="border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px">{{ tl.name }}</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                Monitors <b>{{ monitoredLabel(tl) }}</b> ·
                {{ tl.logging_type === 'polled' ? `every ${tl.log_interval}s`
                   : tl.logging_type === 'cov' ? `on change (±${tl.cov_increment})`
                   : 'manual trigger' }}
              </div>
              <div style="font-size:11px;color:var(--text-secondary);margin-top:2px">
                {{ tl.record_count }}/{{ tl.buffer_size }} records
                <span v-if="tl.stop_when_full && tl.record_count >= tl.buffer_size" style="color:#faad14">(full — logging stopped)</span>
                · total {{ tl.total_record_count }} · {{ tl.enabled ? 'Enabled' : 'Disabled' }}
              </div>
            </div>
            <a-space :size="4">
              <a-button size="small" title="View records" @click="viewRecords(tl)">
                <template #icon><UnorderedListOutlined /></template>
              </a-button>
              <a-button size="small" title="Trigger sample now" @click="trigger(tl)">
                <template #icon><ThunderboltOutlined /></template>
              </a-button>
              <a-button size="small" title="Clear records" @click="confirmClear(tl)">
                <template #icon><ClearOutlined /></template>
              </a-button>
              <a-button size="small" title="Edit" @click="openEdit(tl)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(tl)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-space>
          </div>
        </div>
      </a-spin>
    </template>

    <template v-else>
      <a-form layout="vertical" :colon="false">
        <a-form-item label="Name" required>
          <a-input v-model:value="form.name" placeholder="e.g. Zone-Temp-Log" />
        </a-form-item>

        <a-form-item label="Description">
          <a-input v-model:value="form.description" placeholder="Optional description" />
        </a-form-item>

        <a-form-item label="Monitored Object" required>
          <a-select v-model:value="form.monitored_object_id" show-search placeholder="Choose a point to log" :filter-option="(input: string, opt: any) => opt.label.toLowerCase().includes(input.toLowerCase())">
            <a-select-option v-for="o in allObjects" :key="o.id" :value="o.id" :label="objectLabel(o)">{{ objectLabel(o) }}</a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="Logging Type">
          <a-select v-model:value="form.logging_type" :options="LOGGING_TYPES" />
        </a-form-item>

        <a-row :gutter="12" v-if="form.logging_type === 'polled'">
          <a-col :span="24">
            <a-form-item label="Log Interval (seconds)">
              <a-input-number v-model:value="form.log_interval" :min="1" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="12" v-else-if="form.logging_type === 'cov'">
          <a-col :span="24">
            <a-form-item
              label="COV Increment"
              tooltip="Minimum change in value before a new sample is recorded. Any change is logged for binary/multi-state points."
            >
              <a-input-number v-model:value="form.cov_increment" :min="0" :step="0.1" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="12">
          <a-col :span="12">
            <a-form-item label="Buffer Size (records)">
              <a-input-number v-model:value="form.buffer_size" :min="1" :max="100000" style="width:100%" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="When buffer is full" tooltip="Overwrite: oldest record is discarded to make room. Stop: logging pauses until cleared.">
              <a-radio-group v-model:value="form.stop_when_full">
                <a-radio-button :value="0">Overwrite oldest</a-radio-button>
                <a-radio-button :value="1">Stop logging</a-radio-button>
              </a-radio-group>
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="Enabled" style="margin-bottom:0">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>
      </a-form>
    </template>

    <template #footer>
      <a-space v-if="formOpen">
        <a-button @click="formOpen = false">Cancel</a-button>
        <a-button type="primary" :loading="saving" @click="save">{{ editing ? 'Save' : 'Create' }}</a-button>
      </a-space>
      <a-button v-else @click="emit('update:open', false)">Close</a-button>
    </template>
  </a-drawer>

  <a-modal
    :open="recordsOpen"
    :title="recordsFor ? `Records — ${recordsFor.name}` : 'Records'"
    :width="640"
    :footer="null"
    @cancel="recordsOpen = false"
  >
    <div style="display:flex;justify-content:flex-end;margin-bottom:10px">
      <a-button size="small" :loading="recordsLoading" @click="loadRecords">
        <template #icon><ReloadOutlined /></template>
        Refresh
      </a-button>
    </div>
    <a-table
      :columns="recordColumns"
      :data-source="records"
      :loading="recordsLoading"
      row-key="id"
      size="small"
      :pagination="{ pageSize: 20 }"
    >
      <template #emptyText>
        <div style="padding:24px;color:var(--text-placeholder)">No records yet</div>
      </template>
    </a-table>
  </a-modal>
</template>
