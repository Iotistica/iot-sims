<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { UploadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import { buildLocationTreeOptions } from '../locationTree'
import type { Device, Meta, Location } from '../types'

const props = defineProps<{
  open: boolean
  device: Device | null
  meta: Meta
  locations?: Location[]
  existingInstances?: number[]
  draftMode?: boolean
  draftDevice?: Record<string, any> | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  saved: []
  'draft-saved': [data: Record<string, any>]
}>()

const loading = ref(false)
const deleting = ref(false)
const form = reactive({
  device_instance: 1001,
  name: '',
  description: '',
  vendor_name: 'Iotistica',
  model_name: 'BACnet Simulator',
  enabled: true,
  firmware_revision: 'N/A',
  protocol_revision: 22,
  max_apdu_length_accepted: 1024,
  segmentation_supported: 'segmented-both',
  location_id: null as number | null,
  equipment_type: null as string | null,
})

const locationTreeOptions = computed(() => buildLocationTreeOptions(props.locations ?? []))

// ── Import points from EDE (only offered when adding a new, non-draft device) ──

const edeFile = ref<File | null>(null)
const edeFileName = ref('')
const edeFileInput = ref<HTMLInputElement>()

function onEdeFileChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  ;(e.target as HTMLInputElement).value = ''
  if (!file) return

  const reader = new FileReader()
  reader.onload = () => {
    const text = reader.result as string
    const lines = text.split(/\r?\n/).filter(l => l.trim() && !l.trim().startsWith('#'))
    if (lines.length < 2) { message.error('No data rows found in that EDE file'); return }

    const header = lines[0].split(';').map(h => h.trim().toLowerCase())
    const idx = header.indexOf('device-instance')
    const instances = new Set<number>()
    if (idx !== -1) {
      for (const line of lines.slice(1)) {
        const n = Number(line.split(';')[idx]?.trim())
        if (Number.isFinite(n)) instances.add(n)
      }
    }
    if (instances.size > 1) {
      message.warning('This EDE file covers multiple devices — add each device separately, or use project-level EDE import (Open Project → Import → EDE) instead.')
      return
    }

    edeFile.value = file
    edeFileName.value = file.name

    const [instance] = instances
    if (instance !== undefined) {
      if ((props.existingInstances ?? []).includes(instance)) {
        message.warning(`Device instance ${instance} from the file is already in use — keeping ${form.device_instance}.`)
      } else {
        form.device_instance = instance
      }
    }
    if (!form.name.trim()) {
      form.name = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim()
    }
  }
  reader.readAsText(file)
}

function nextFreeInstance(): number {
  const taken = new Set(props.existingInstances ?? [])
  let id = 1001
  while (taken.has(id)) id++
  return id
}

// ── Vendor / model picker ─────────────────────────────────────────────────────

interface VendorModel {
  name: string
  type?: string
  typeLabel?: string
  pics_url?: string
  object_types?: Record<string, number | boolean>
  pics_error?: string
}
interface Vendor { name: string; models: VendorModel[] }

const vendors = ref<Vendor[]>([])
const vendorsLoading = ref(false)

const vendorOptions = computed(() =>
  vendors.value.map(v => ({ value: v.name, label: v.name }))
)

const selectedVendor = computed(() => vendors.value.find(v => v.name === form.vendor_name))

const modelOptions = computed(() => {
  const v = selectedVendor.value
  if (!v) return []
  return v.models.map(m => ({
    value: m.name,
    label: m.typeLabel ? `${m.name} (${m.typeLabel})` : m.name,
  }))
})

// Scraped BTL catalog info (device profile, PICS datasheet, supported object
// types) for whichever vendor/model is currently selected — surfaced so the
// user can see what was scraped without leaving the form.
const selectedModel = computed(() =>
  selectedVendor.value?.models.find(m => m.name === form.model_name)
)

const selectedModelObjectTypes = computed(() => {
  const types = selectedModel.value?.object_types
  if (!types) return []
  return Object.entries(types).map(([code, count]) => ({
    code,
    label: typeof count === 'number' ? `${code} ×${count}` : code,
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
  edeFile.value = null
  edeFileName.value = ''
  const src = props.draftMode ? props.draftDevice : props.device
  if (src) {
    Object.assign(form, {
      device_instance: src.device_instance,
      name: src.name,
      description: src.description ?? '',
      vendor_name: src.vendor_name,
      model_name: src.model_name,
      enabled: !!src.enabled,
      firmware_revision: src.firmware_revision ?? 'N/A',
      protocol_revision: src.protocol_revision ?? 22,
      max_apdu_length_accepted: src.max_apdu_length_accepted ?? 1024,
      segmentation_supported: src.segmentation_supported ?? 'segmented-both',
      location_id: src.location_id ?? null,
      equipment_type: src.equipment_type ?? null,
    })
  } else {
    Object.assign(form, {
      device_instance: nextFreeInstance(), name: '', description: '', vendor_name: 'Iotistica', model_name: 'BACnet Simulator', enabled: true,
      firmware_revision: 'N/A', protocol_revision: 22, max_apdu_length_accepted: 1024, segmentation_supported: 'segmented-both',
      location_id: null,
      equipment_type: null,
    })
  }
})

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (props.draftMode) {
    emit('draft-saved', { ...form })
    emit('update:open', false)
    return
  }
  loading.value = true
  const body = { ...form, enabled: form.enabled ? 1 : 0 }
  try {
    if (props.device) {
      await api.devices.update(props.device.id, body)
      message.success('Device updated')
    } else {
      const created = await api.devices.create(body)
      message.success('Device created')
      if (edeFile.value) {
        try {
          const result = await api.devices.importEde(created.id, edeFile.value)
          message.success(`${result.objects_imported} object${result.objects_imported !== 1 ? 's' : ''} imported from EDE`)
        } catch (e: unknown) {
          message.error(`Device created, but EDE import failed: ${(e as Error).message}`)
        }
      }
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function doDelete() {
  if (!props.device) return
  const dev = props.device
  Modal.confirm({
    title: `Delete "${dev.name}"?`,
    content: 'This also deletes all its objects and cannot be undone.',
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      deleting.value = true
      try {
        await api.devices.del(dev.id)
        message.success('Device deleted')
        emit('update:open', false)
        emit('saved')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete device')
      } finally {
        deleting.value = false
      }
    },
  })
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

      <a-form-item label="Location">
        <a-tree-select
          v-model:value="form.location_id"
          :tree-data="locationTreeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Top level"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item label="Equipment Type">
        <a-select
          v-model:value="form.equipment_type"
          show-search
          allow-clear
          placeholder="Not tagged"
          :options="meta.equipment_types"
          :filter-option="filterOption"
        />
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
            <!-- a-select when vendor is in BTL list (click-to-open dropdown) -->
            <a-select
              v-if="modelOptions.length"
              v-model:value="form.model_name"
              show-search
              allow-clear
              placeholder="Select model…"
              :options="modelOptions"
              :filter-option="filterOption"
            />
            <!-- plain input for custom vendors not in list -->
            <a-input
              v-else
              v-model:value="form.model_name"
              placeholder="e.g. BACnet Simulator"
            />
          </a-form-item>
        </a-col>
      </a-row>

      <div
        v-if="selectedModel && (selectedModel.typeLabel || selectedModel.pics_url || selectedModelObjectTypes.length)"
        style="background:var(--surface-alt);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:16px;font-size:12px"
      >
        <div v-if="selectedModel.typeLabel" style="margin-bottom:4px">
          <span style="color:var(--text-muted)">Device Profile:</span> {{ selectedModel.typeLabel }}
        </div>
        <div v-if="selectedModel.pics_url" style="margin-bottom:4px">
          <div style="color:var(--text-muted);white-space:nowrap">PICS Datasheet:</div>
          <a :href="selectedModel.pics_url" target="_blank" rel="noopener noreferrer" style="word-break:break-all">{{ selectedModel.pics_url }}</a>
        </div>
        <div v-if="selectedModelObjectTypes.length">
          <span style="color:var(--text-muted)">Supported Object Types:</span>
          <a-space :size="[4, 4]" wrap style="margin-left:4px">
            <a-tag v-for="t in selectedModelObjectTypes" :key="t.code" style="margin:0">{{ t.label }}</a-tag>
          </a-space>
        </div>
        <div v-if="selectedModel.pics_error" style="color:#faad14;margin-top:4px">
          PICS parsing note: {{ selectedModel.pics_error }}
        </div>
      </div>

      <a-collapse ghost style="margin-top:16px">
        <a-collapse-panel key="device-info" header="Device Info (advanced)">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
            Informational/cosmetic Device object properties some BACnet clients check — not enforced by the simulator itself.
          </div>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Firmware Revision">
                <a-input v-model:value="form.firmware_revision" placeholder="N/A" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Protocol Revision">
                <a-input-number v-model:value="form.protocol_revision" :min="0" :max="255" style="width:100%" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Max APDU Length Accepted">
                <a-input-number v-model:value="form.max_apdu_length_accepted" :min="50" :max="1476" style="width:100%" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Segmentation Supported">
                <a-select v-model:value="form.segmentation_supported">
                  <a-select-option v-for="s in meta.segmentation_options" :key="s" :value="s">{{ s }}</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
          </a-row>
        </a-collapse-panel>
      </a-collapse>

      <a-form-item
        v-if="!device && !draftMode"
        label="Import points from EDE (optional)"
        style="margin-top:16px;margin-bottom:0"
      >
        <input ref="edeFileInput" type="file" accept=".ede,.csv,text/csv" style="display:none" @change="onEdeFileChange" />
        <a-button block @click="edeFileInput?.click()">
          <template #icon><UploadOutlined /></template>
          {{ edeFileName || 'Choose EDE file…' }}
        </a-button>
      </a-form-item>

      <a-form-item label="Enabled" style="margin-top:16px;margin-bottom:0">
        <a-switch v-model:checked="form.enabled" />
      </a-form-item>
    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a-button v-if="device && !draftMode" danger :loading="deleting" @click="doDelete">Delete</a-button>
        <div v-else />
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ device ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
