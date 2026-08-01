<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, NotificationClass, NotificationRecipient } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const list = ref<NotificationClass[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<NotificationClass | null>(null)
const formOpen = ref(false)

const TRANSITIONS = [
  { label: 'To Off-Normal', value: 'to-offnormal' },
  { label: 'To Fault', value: 'to-fault' },
  { label: 'To Normal', value: 'to-normal' },
]

const form = reactive({
  name: '',
  priority_to_offnormal: 100,
  priority_to_fault: 100,
  priority_to_normal: 100,
  ack_required_transitions: ['to-offnormal', 'to-fault'] as string[],
  recipients: [] as NotificationRecipient[],
})

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    list.value = await api.notificationClasses.list(props.device.id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load notification classes')
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  if (v) load()
  else { formOpen.value = false; editing.value = null }
})

function openAdd() {
  editing.value = null
  Object.assign(form, {
    name: '', priority_to_offnormal: 100, priority_to_fault: 100, priority_to_normal: 100,
    ack_required_transitions: ['to-offnormal', 'to-fault'], recipients: [],
  })
  formOpen.value = true
}

function openEdit(nc: NotificationClass) {
  editing.value = nc
  Object.assign(form, {
    name: nc.name,
    priority_to_offnormal: nc.priority_to_offnormal,
    priority_to_fault: nc.priority_to_fault,
    priority_to_normal: nc.priority_to_normal,
    ack_required_transitions: [...nc.ack_required_transitions],
    recipients: nc.recipients.map(r => ({ ...r })),
  })
  formOpen.value = true
}

function addRecipient() {
  form.recipients.push({ address: '', confirmed: false, process_identifier: 1 })
}
function removeRecipient(i: number) {
  form.recipients.splice(i, 1)
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  saving.value = true
  try {
    const body = { ...form, recipients: form.recipients.filter(r => r.address?.trim()) }
    if (editing.value) {
      await api.notificationClasses.update(editing.value.id, body)
      message.success('Notification class updated')
    } else {
      await api.notificationClasses.create(props.device.id, body)
      message.success('Notification class created')
    }
    formOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Save failed')
  } finally {
    saving.value = false
  }
}

function confirmDelete(nc: NotificationClass) {
  Modal.confirm({
    title: `Delete "${nc.name}"?`,
    content: 'Objects referencing this notification class will keep their alarm config but stop routing notifications until reassigned.',
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.notificationClasses.del(nc.id)
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
    :title="device ? `Notification Classes — ${device.name}` : 'Notification Classes'"
    :open="open"
    width="520"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <a-button type="primary" block @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Add Notification Class
      </a-button>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:#bbb;padding:40px 0;font-size:13px">
          No notification classes yet — objects with alarming enabled won't route notifications until one exists.
        </div>
        <div
          v-for="nc in list" :key="nc.id"
          style="border:1px solid #e8e8e8;border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px">{{ nc.name }}</div>
              <div style="font-size:11px;color:#888;margin-top:2px">
                Priorities: {{ nc.priority_to_offnormal }} / {{ nc.priority_to_fault }} / {{ nc.priority_to_normal }}
                (offnormal / fault / normal)
              </div>
              <div style="font-size:11px;color:#aaa;margin-top:2px">
                {{ nc.recipients.length }} recipient{{ nc.recipients.length !== 1 ? 's' : '' }}
              </div>
            </div>
            <a-space :size="4">
              <a-button size="small" title="Edit" @click="openEdit(nc)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(nc)">
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
          <a-input v-model:value="form.name" placeholder="e.g. Critical-Alarms" />
        </a-form-item>

        <div style="font-size:11px;font-weight:600;color:#555;margin-bottom:6px">Priority (0-255, lower = more urgent)</div>
        <a-row :gutter="8">
          <a-col :span="8">
            <a-form-item label="To Off-Normal">
              <a-input-number v-model:value="form.priority_to_offnormal" :min="0" :max="255" style="width:100%" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="To Fault">
              <a-input-number v-model:value="form.priority_to_fault" :min="0" :max="255" style="width:100%" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="To Normal">
              <a-input-number v-model:value="form.priority_to_normal" :min="0" :max="255" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="Acknowledgment required for">
          <a-checkbox-group v-model:value="form.ack_required_transitions" :options="TRANSITIONS" />
        </a-form-item>

        <div style="font-size:11px;font-weight:600;color:#555;margin:12px 0 6px">
          Recipients
          <span style="font-weight:400;color:#aaa">— network address to send Event Notifications to; recipients without an address are skipped on send but still visible in the Alarms log</span>
        </div>
        <div
          v-for="(r, i) in form.recipients" :key="i"
          style="display:flex;gap:8px;align-items:center;margin-bottom:8px;background:#fafafa;border:1px solid #e8e8e8;border-radius:6px;padding:8px"
        >
          <a-input v-model:value="r.address" placeholder="192.168.1.50:47808" style="flex:1" />
          <a-input-number v-model:value="r.process_identifier" :min="0" style="width:80px" title="Process ID" />
          <a-checkbox v-model:checked="r.confirmed">Confirmed</a-checkbox>
          <a-button type="text" size="small" danger @click="removeRecipient(i)">
            <template #icon><MinusCircleOutlined /></template>
          </a-button>
        </div>
        <a-button size="small" block @click="addRecipient">
          <template #icon><PlusOutlined /></template>
          Add Recipient
        </a-button>
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
