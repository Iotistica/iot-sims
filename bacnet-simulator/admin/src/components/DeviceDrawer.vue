<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Device } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit  = defineEmits<{ 'update:open': [v: boolean]; saved: [] }>()

const loading = ref(false)
const form = reactive({
  device_instance: 1001,
  name: '',
  description: '',
  vendor_name: 'Iotistica',
  model_name: 'BACnet Simulator',
  enabled: true,
})

// ── Vendor / model picker ─────────────────────────────────────────────────────

interface VendorModel { name: string; type?: string; typeLabel?: string }
interface Vendor { name: string; models: VendorModel[] }

const vendors = ref<Vendor[]>([])
const vendorsLoading = ref(false)

const vendorOptions = computed(() =>
  vendors.value.map(v => ({ value: v.name, label: v.name }))
)

const modelOptions = computed(() => {
  const v = vendors.value.find(v => v.name === form.vendor_name)
  if (!v) return []
  return v.models.map(m => ({
    value: m.name,
    label: m.typeLabel ? `${m.name} (${m.typeLabel})` : m.name,
  }))
})

function filterOption(input: string, opt: { value?: string; label?: string }) {
  return (opt.label ?? opt.value ?? '').toLowerCase().includes(input.toLowerCase())
}

async function loadVendors() {
  if (vendors.value.length || vendorsLoading.value) return
  vendorsLoading.value = true
  try {
    const res = await fetch('/bacnet-vendors.json')
    if (res.ok) vendors.value = (await res.json()).vendors ?? []
  } catch { } finally {
    vendorsLoading.value = false
  }
}

// ─────────────────────────────────────────────────────────────────────────────

watch(() => props.open, (v) => {
  if (!v) return
  loadVendors()
  if (props.device) {
    Object.assign(form, { ...props.device, enabled: !!props.device.enabled })
  } else {
    Object.assign(form, { device_instance: 1001, name: '', description: '', vendor_name: 'Iotistica', model_name: 'BACnet Simulator', enabled: true })
  }
})

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  loading.value = true
  const body = { ...form, enabled: form.enabled ? 1 : 0 }
  try {
    if (props.device) {
      await api.devices.update(props.device.id, body)
      message.success('Device updated')
    } else {
      await api.devices.create(body)
      message.success('Device created')
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <a-drawer
    :title="device ? 'Edit Device' : 'Add Device'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-row :gutter="12">
        <a-col :span="16">
          <a-form-item label="Name" required>
            <a-input v-model:value="form.name" placeholder="AHU-1-Controller" />
          </a-form-item>
        </a-col>
        <a-col :span="8">
          <a-form-item label="Device Instance" required>
            <a-input-number v-model:value="form.device_instance" :min="1" :max="4194302" style="width:100%" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Air Handling Unit 1" />
      </a-form-item>

      <a-row :gutter="12">
        <a-col :span="12">
          <a-form-item label="Vendor Name">
            <a-auto-complete
              v-model:value="form.vendor_name"
              :options="vendorOptions"
              :filter-option="filterOption"
              allow-clear
              placeholder="e.g. Siemens"
              @change="() => { form.model_name = '' }"
            />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="Model Name">
            <a-auto-complete
              v-model:value="form.model_name"
              :options="modelOptions"
              :filter-option="filterOption"
              allow-clear
              placeholder="e.g. PXC50-E.D"
            />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Enabled">
        <a-switch v-model:checked="form.enabled" />
      </a-form-item>
    </a-form>

    <template #footer>
      <a-space>
        <a-button @click="emit('update:open', false)">Cancel</a-button>
        <a-button type="primary" :loading="loading" @click="save">
          {{ device ? 'Save' : 'Create' }}
        </a-button>
      </a-space>
    </template>
  </a-drawer>
</template>
