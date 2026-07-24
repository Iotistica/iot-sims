<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Device } from '../types'

const props = defineProps<{
  open: boolean
  device: Device | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  saved: []
}>()

const loading = ref(false)
const form = reactive({
  name: '',
  description: '',
  manufacturer: '',
  model: '',
  enabled: true,
})

watch(() => props.open, (v) => {
  if (!v) return
  if (props.device) {
    Object.assign(form, {
      name: props.device.name,
      description: props.device.description ?? '',
      manufacturer: props.device.manufacturer ?? '',
      model: props.device.model ?? '',
      enabled: !!props.device.enabled,
    })
  } else {
    Object.assign(form, { name: '', description: '', manufacturer: '', model: '', enabled: true })
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
      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="AHU-1-Controller" />
      </a-form-item>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Air Handling Unit 1" />
      </a-form-item>

      <a-row :gutter="12">
        <a-col :span="12">
          <a-form-item label="Manufacturer">
            <a-input v-model:value="form.manufacturer" placeholder="e.g. Siemens" />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="Model">
            <a-input v-model:value="form.model" placeholder="e.g. RXB29.1" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Enabled" style="margin-top:16px;margin-bottom:0">
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
