<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, NotificationClass, NotificationRecipient } from '../types'

const props = defineProps<{ open: boolean; device: Device | null; devices?: Device[] }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

// Recipients follow BACnet's own Recipient CHOICE: either a Device object
// identifier or a network Address, never both at once. Every virtual device
// in this simulator shares one BACnet/IP socket, so a device-type recipient
// always resolves to this same address server-side — the picker just saves
// you from knowing/typing it, and from converting device -> address by hand.
//
// Not every device supports receiving Event Notifications (real BACnet
// devices vary here too — see effective_can_receive_event_notifications).
// Incapable devices stay selectable (for teaching purposes) but are visibly
// flagged, and the server records the delivery attempt as rejected rather
// than pretending it succeeded.
const deviceOptions = computed(() =>
  (props.devices ?? []).map(d => ({
    value: d.device_instance,
    label: d.effective_can_receive_event_notifications === false
      ? `⚠ ${d.name} (${d.device_instance}) — cannot receive Event Notifications`
      : `${d.name} (${d.device_instance})`,
  }))
)

function recipientDeviceCapable(r: NotificationRecipient): boolean {
  if (r.recipient_type !== 'device' || r.device_instance == null) return true
  const d = (props.devices ?? []).find(dev => dev.device_instance === r.device_instance)
  return d?.effective_can_receive_event_notifications !== false
}

function filterRecipientOption(input: string, opt: { value?: string | number; label?: string }) {
  return (opt.label ?? '').toLowerCase().includes(input.toLowerCase())
}

function newRecipient(): NotificationRecipient {
  return { recipient_type: 'address', device_instance: null, ip_address: '', port: 47808, confirmed: false, process_identifier: 1 }
}

/** Normalizes both current-shape and pre-split legacy rows ({address: "ip:port"}) into the editable shape. */
function normalizeRecipient(r: NotificationRecipient): NotificationRecipient {
  if (r.recipient_type === 'device' || r.recipient_type === 'address') {
    return {
      recipient_type: r.recipient_type,
      device_instance: r.device_instance ?? null,
      ip_address: r.ip_address ?? '',
      port: r.port ?? 47808,
      confirmed: !!r.confirmed,
      process_identifier: r.process_identifier ?? 1,
    }
  }
  const [ip, portStr] = (r.address ?? '').split(':')
  return {
    recipient_type: 'address',
    device_instance: null,
    ip_address: ip ?? '',
    port: portStr ? Number(portStr) : 47808,
    confirmed: !!r.confirmed,
    process_identifier: r.process_identifier ?? 1,
  }
}

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
    recipients: nc.recipients.map(normalizeRecipient),
  })
  formOpen.value = true
}

function addRecipient() {
  form.recipients.push(newRecipient())
}
function removeRecipient(i: number) {
  form.recipients.splice(i, 1)
}

function isRecipientFilled(r: NotificationRecipient): boolean {
  return r.recipient_type === 'device' ? r.device_instance != null : !!r.ip_address?.trim()
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  saving.value = true
  try {
    const body = { ...form, recipients: form.recipients.filter(isRecipientFilled) }
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
        <div v-if="!list.length && !loading" style="text-align:center;color:var(--text-placeholder);padding:40px 0;font-size:13px">
          No notification classes yet — objects with alarming enabled won't route notifications until one exists.
        </div>
        <div
          v-for="nc in list" :key="nc.id"
          style="border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px;color:var(--text-primary)">{{ nc.name }}</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                Priorities: {{ nc.priority_to_offnormal }} / {{ nc.priority_to_fault }} / {{ nc.priority_to_normal }}
                (offnormal / fault / normal)
              </div>
              <div style="font-size:11px;color:var(--text-secondary);margin-top:2px">
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

        <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:6px">Priority (0-255, lower = more urgent)</div>
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

        <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin:12px 0 6px">
          Recipients
          <span style="font-weight:400;color:var(--text-secondary)">— either one of this simulator's devices, or a network address; recipients that don't resolve to an address are skipped on send but still visible in the Alarms log</span>
        </div>
        <div
          v-for="(r, i) in form.recipients" :key="i"
          style="margin-bottom:8px;background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:10px"
        >
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <a-radio-group v-model:value="r.recipient_type" size="small" button-style="solid">
              <a-radio-button value="device">BACnet Device</a-radio-button>
              <a-radio-button value="address">Network Address</a-radio-button>
            </a-radio-group>
            <a-button type="text" size="small" danger @click="removeRecipient(i)">
              <template #icon><MinusCircleOutlined /></template>
            </a-button>
          </div>

          <template v-if="r.recipient_type === 'device'">
            <a-select
              v-model:value="r.device_instance"
              :options="deviceOptions"
              :filter-option="filterRecipientOption"
              show-search
              allow-clear
              placeholder="Select device…"
              style="width:100%;margin-bottom:8px"
            />
            <div v-if="!recipientDeviceCapable(r)" style="font-size:11px;color:#faad14;margin:-4px 0 8px">
              ⚠ This device does not support Event Notification reception — delivery will be recorded as rejected, not sent.
            </div>
          </template>
          <a-row v-else :gutter="8" style="margin-bottom:8px">
            <a-col :span="16">
              <a-input v-model:value="r.ip_address" placeholder="IP Address, e.g. 192.168.1.50" />
            </a-col>
            <a-col :span="8">
              <a-input-number v-model:value="r.port" :min="1" :max="65535" placeholder="Port" style="width:100%" />
            </a-col>
          </a-row>

          <div style="display:flex;gap:10px;align-items:center">
            <span style="font-size:12px;color:var(--text-secondary)">Process ID</span>
            <a-input-number v-model:value="r.process_identifier" :min="0" style="width:80px" />
            <a-checkbox v-model:checked="r.confirmed">Confirmed</a-checkbox>
          </div>
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
