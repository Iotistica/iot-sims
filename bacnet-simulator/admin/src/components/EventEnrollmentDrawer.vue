<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, SimObject, NotificationClass, EventEnrollment } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const list = ref<EventEnrollment[]>([])
const allObjects = ref<SimObject[]>([])
const notificationClasses = ref<NotificationClass[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<EventEnrollment | null>(null)
const formOpen = ref(false)

const ALGORITHMS = [
  { label: 'Change of State (binary / multi-state)', value: 'change-of-state' },
  { label: 'Out of Range (analog)', value: 'out-of-range' },
]
const TRANSITIONS = [
  { label: 'To Off-Normal', value: 'to-offnormal' },
  { label: 'To Fault', value: 'to-fault' },
  { label: 'To Normal', value: 'to-normal' },
]

const form = reactive({
  name: '',
  monitored_object_id: null as number | null,
  algorithm: 'change-of-state',
  notification_class_id: null as number | null,
  enabled: true,
  event_enable: ['to-offnormal', 'to-normal'] as string[],
  time_delay: 0,
  time_delay_normal: 0,
  alarm_value: true,
  alarm_values: [] as number[],
  high_limit: undefined as number | undefined,
  low_limit: undefined as number | undefined,
  deadband: 0,
})

// Which objects are eligible depends on the chosen algorithm — Change of
// State only applies to discrete properties, Out of Range only to analog.
const monitorableObjects = computed(() =>
  form.algorithm === 'out-of-range'
    ? allObjects.value.filter(o => o.object_type.startsWith('analog'))
    : allObjects.value.filter(o => o.object_type.startsWith('binary') || o.object_type.startsWith('multi-state')),
)
const monitoredObject = computed(() => allObjects.value.find(o => o.id === form.monitored_object_id) ?? null)
const isBinary = computed(() => monitoredObject.value?.object_type.startsWith('binary') ?? false)
const isMultistate = computed(() => monitoredObject.value?.object_type.startsWith('multi-state') ?? false)
const isOutOfRange = computed(() => form.algorithm === 'out-of-range')

function onAlgorithmChange() {
  // Clear the monitored-object selection if it no longer matches the newly
  // chosen algorithm's eligible object types.
  if (!monitorableObjects.value.some(o => o.id === form.monitored_object_id)) {
    form.monitored_object_id = null
  }
}

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    const [enrollments, objects, ncs] = await Promise.all([
      api.eventEnrollments.list(props.device.id),
      api.objects.list(props.device.id),
      api.notificationClasses.list(props.device.id),
    ])
    list.value = enrollments
    allObjects.value = objects
    notificationClasses.value = ncs
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load event enrollments')
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
    name: '', monitored_object_id: null, algorithm: 'change-of-state',
    notification_class_id: null, enabled: true,
    event_enable: ['to-offnormal', 'to-normal'],
    time_delay: 0, time_delay_normal: 0, alarm_value: true, alarm_values: [],
    high_limit: undefined, low_limit: undefined, deadband: 0,
  })
}

function openAdd() {
  editing.value = null
  resetForm()
  formOpen.value = true
}

function openEdit(ee: EventEnrollment) {
  editing.value = ee
  Object.assign(form, {
    name: ee.name,
    monitored_object_id: ee.monitored_object_id,
    algorithm: ee.algorithm,
    notification_class_id: ee.notification_class_id,
    enabled: ee.enabled,
    event_enable: [...ee.event_enable],
    time_delay: ee.time_delay,
    time_delay_normal: ee.time_delay_normal,
    alarm_value: (ee.event_parameters.alarm_value as boolean | undefined) ?? true,
    alarm_values: (ee.event_parameters.alarm_values as number[] | undefined) ?? [],
    high_limit: ee.event_parameters.high_limit as number | undefined,
    low_limit: ee.event_parameters.low_limit as number | undefined,
    deadband: (ee.event_parameters.deadband as number | undefined) ?? 0,
  })
  formOpen.value = true
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (!form.monitored_object_id) { message.error('Choose an object to monitor'); return }

  saving.value = true
  try {
    const event_parameters = isOutOfRange.value
      ? { high_limit: form.high_limit, low_limit: form.low_limit, deadband: form.deadband }
      : isMultistate.value
      ? { alarm_values: form.alarm_values }
      : { alarm_value: form.alarm_value }
    const body = {
      name: form.name.trim(),
      monitored_object_id: form.monitored_object_id,
      algorithm: form.algorithm,
      event_parameters,
      notification_class_id: form.notification_class_id,
      enabled: form.enabled ? 1 : 0,
      event_enable: form.event_enable,
      notify_type: 'event',
      time_delay: form.time_delay,
      time_delay_normal: form.time_delay_normal,
    }
    if (editing.value) {
      await api.eventEnrollments.update(editing.value.id, body as any)
      message.success('Event enrollment updated')
    } else {
      await api.eventEnrollments.create(props.device.id, body as any)
      message.success('Event enrollment created')
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
function monitoredLabel(ee: EventEnrollment): string {
  const o = allObjects.value.find(x => x.id === ee.monitored_object_id)
  return o ? o.name : `object #${ee.monitored_object_id}`
}

function confirmDelete(ee: EventEnrollment) {
  Modal.confirm({
    title: `Delete "${ee.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.eventEnrollments.del(ee.id)
        message.success('Deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Delete failed')
      }
    },
  })
}
</script>

<template>
  <a-drawer
    :title="device ? `Event Enrollments — ${device.name}` : 'Event Enrollments'"
    :open="open"
    width="520"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <div style="font-size:12px;color:#888;margin-bottom:12px">
        Watches another point's value independently of that point's own alarm config — useful for a second
        threshold on the same point, or alarming on a point that has no intrinsic reporting of its own.
        Change of State monitors binary/multi-state points; Out of Range monitors analog points.
      </div>
      <a-button type="primary" block :disabled="!allObjects.length" @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Add Event Enrollment
      </a-button>
      <div v-if="!allObjects.length" style="font-size:12px;color:#faad14;margin-top:-10px;margin-bottom:16px">
        This device has no points to monitor yet.
      </div>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:#bbb;padding:40px 0;font-size:13px">
          No event enrollments yet
        </div>
        <div
          v-for="ee in list" :key="ee.id"
          style="border:1px solid #e8e8e8;border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px">{{ ee.name }}</div>
              <div style="font-size:11px;color:#888;margin-top:2px">
                Monitors <b>{{ monitoredLabel(ee) }}</b> · {{ ee.algorithm }}
              </div>
              <div style="font-size:11px;color:#aaa;margin-top:2px">
                {{ ee.enabled ? 'Enabled' : 'Disabled' }}
              </div>
            </div>
            <a-space :size="4">
              <a-button size="small" title="Edit" @click="openEdit(ee)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(ee)">
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
          <a-input v-model:value="form.name" placeholder="e.g. Door-Left-Open-Watch" />
        </a-form-item>

        <a-form-item label="Algorithm">
          <a-select v-model:value="form.algorithm" :options="ALGORITHMS" @change="onAlgorithmChange" />
        </a-form-item>

        <a-form-item label="Monitored Object" required>
          <a-select
            v-model:value="form.monitored_object_id"
            :placeholder="isOutOfRange ? 'Choose an analog point' : 'Choose a binary/multi-state point'"
          >
            <a-select-option v-for="o in monitorableObjects" :key="o.id" :value="o.id">{{ objectLabel(o) }}</a-select-option>
          </a-select>
        </a-form-item>

        <template v-if="isOutOfRange">
          <a-row :gutter="12">
            <a-col :span="8">
              <a-form-item label="High Limit">
                <a-input-number v-model:value="form.high_limit" style="width:100%" :step="0.5" placeholder="none" />
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="Low Limit">
                <a-input-number v-model:value="form.low_limit" style="width:100%" :step="0.5" placeholder="none" />
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="Deadband">
                <a-input-number v-model:value="form.deadband" :min="0" style="width:100%" :step="0.5" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>
        <template v-else-if="isMultistate">
          <a-form-item label="Alarm states">
            <a-checkbox-group v-model:value="form.alarm_values">
              <a-checkbox v-for="s in (monitoredObject?.number_of_states ?? 0)" :key="s" :value="s">{{ s }}</a-checkbox>
            </a-checkbox-group>
          </a-form-item>
        </template>
        <template v-else-if="isBinary">
          <a-form-item label="Alarm when value is">
            <a-radio-group v-model:value="form.alarm_value">
              <a-radio-button :value="true">Active</a-radio-button>
              <a-radio-button :value="false">Inactive</a-radio-button>
            </a-radio-group>
          </a-form-item>
        </template>

        <a-form-item label="Notification Class">
          <a-select v-model:value="form.notification_class_id" allow-clear placeholder="None — logged locally only">
            <a-select-option v-for="nc in notificationClasses" :key="nc.id" :value="nc.id">{{ nc.name }}</a-select-option>
          </a-select>
        </a-form-item>

        <a-row :gutter="12">
          <a-col :span="12">
            <a-form-item label="Time Delay (s)">
              <a-input-number v-model:value="form.time_delay" :min="0" :step="5" style="width:100%" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="Time Delay Normal (s)">
              <a-input-number v-model:value="form.time_delay_normal" :min="0" :step="5" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="Generate notifications for">
          <a-checkbox-group v-model:value="form.event_enable" :options="TRANSITIONS" />
        </a-form-item>

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
</template>
