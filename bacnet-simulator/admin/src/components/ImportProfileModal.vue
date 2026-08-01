<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { UploadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [v: boolean]; imported: [] }>()

const format = ref<'json' | 'ede'>('json')
const name = ref('')
const desc = ref('')
const deviceName = ref('')
const fileName = ref('')
const fileData = ref<object | null>(null)
const rawFile = ref<File | null>(null)
const importing = ref(false)
const fileInput = ref<HTMLInputElement>()

watch(() => props.open, (v) => {
  if (!v) return
  format.value = 'json'
  name.value = ''
  desc.value = ''
  deviceName.value = ''
  fileName.value = ''
  fileData.value = null
  rawFile.value = null
})

function pickFile() {
  fileInput.value?.click()
}

function baseName(filename: string): string {
  return filename.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim()
}

function onFileChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return

  if (format.value === 'ede') {
    rawFile.value = file
    fileName.value = file.name
    if (!name.value.trim()) name.value = baseName(file.name)
    return
  }

  const reader = new FileReader()
  reader.onload = () => {
    try {
      const parsed = JSON.parse(reader.result as string)
      const data = Array.isArray(parsed?.devices) ? parsed : { devices: [] }
      if (!Array.isArray(parsed?.devices)) {
        message.warning('This file has no "devices" array — importing an empty profile')
      }
      fileData.value = data
      fileName.value = file.name
      if (!name.value.trim()) name.value = baseName(file.name)
    } catch {
      message.error('Not a valid JSON file')
      fileData.value = null
      fileName.value = ''
    }
  }
  reader.readAsText(file)
}

async function doImport() {
  if (!name.value.trim()) { message.error('Name is required'); return }

  importing.value = true
  try {
    let result: { name: string; device_count: number }
    if (format.value === 'ede') {
      if (!rawFile.value) { message.error('Choose an EDE file first'); return }
      result = await api.profiles.importEde(name.value.trim(), desc.value.trim(), deviceName.value.trim(), rawFile.value)
    } else {
      if (!fileData.value) { message.error('Choose a profile JSON file first'); return }
      result = await api.profiles.import_(name.value.trim(), desc.value.trim(), fileData.value)
    }
    message.success(`"${result.name}" imported — ${result.device_count} device${result.device_count !== 1 ? 's' : ''}`)
    emit('update:open', false)
    emit('imported')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Import failed')
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title="Import Profile"
    ok-text="Import"
    :confirm-loading="importing"
    @ok="doImport"
    @cancel="emit('update:open', false)"
  >
    <a-form layout="vertical" style="margin-top:8px">
      <a-form-item label="Format">
        <a-radio-group v-model:value="format" @change="fileName = ''; fileData = null; rawFile = null">
          <a-radio-button value="json">JSON profile</a-radio-button>
          <a-radio-button value="ede">EDE file</a-radio-button>
        </a-radio-group>
      </a-form-item>
      <a-form-item :label="format === 'ede' ? 'EDE file (.ede/.csv)' : 'Profile JSON file'" required>
        <input
          ref="fileInput"
          type="file"
          :accept="format === 'ede' ? '.ede,.csv,text/csv' : 'application/json,.json'"
          style="display:none"
          @change="onFileChange"
        />
        <a-button block @click="pickFile">
          <template #icon><UploadOutlined /></template>
          {{ fileName || 'Choose file…' }}
        </a-button>
      </a-form-item>
      <a-form-item label="Name" required>
        <a-input v-model:value="name" placeholder="e.g. Large Corporate Campus (12 storey)" />
      </a-form-item>
      <a-form-item label="Description">
        <a-input v-model:value="desc" placeholder="Optional description" />
      </a-form-item>
      <a-form-item
        v-if="format === 'ede'"
        label="Device name"
        help="EDE rows only carry a device instance number, not a name. Used for any device the file doesn't already identify by name (falls back to &quot;Device-&lt;instance&gt;&quot;)."
        style="margin-bottom:0"
      >
        <a-input v-model:value="deviceName" placeholder="e.g. AHU-1" />
      </a-form-item>
    </a-form>
  </a-modal>
</template>
