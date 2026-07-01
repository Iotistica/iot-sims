<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  PlusOutlined, DeleteOutlined, EditOutlined, CopyOutlined,
  AppstoreOutlined, DownloadOutlined,
} from '@ant-design/icons-vue'
import DeviceDrawer from './DeviceDrawer.vue'
import ObjectDrawer from './ObjectDrawer.vue'
import type { Meta, DraftDevice, DraftObject } from '../types'
import { api } from '../api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  'update:open': [val: boolean]
  saved: []
}>()

// ── Meta (fetched once) ───────────────────────────────────────────────────────

const meta = ref<Meta>({ object_types: [], behaviors: [], units: [] })

async function fetchMeta() {
  if (meta.value.object_types.length) return
  try { meta.value = await api.meta() } catch { /* non-fatal */ }
}

// ── Draft state ───────────────────────────────────────────────────────────────

const profileName = ref('')
const profileDesc = ref('')
const draftDevices = ref<DraftDevice[]>([])
const selectedId = ref<string | null>(null)

const selectedDevice = computed(() =>
  draftDevices.value.find(d => d._id === selectedId.value) ?? null,
)

function reset() {
  profileName.value = ''
  profileDesc.value = ''
  draftDevices.value = []
  selectedId.value = null
}

watch(() => props.open, async (isOpen) => {
  if (isOpen) {
    reset()
    await fetchMeta()
  }
})

// ── Device CRUD ───────────────────────────────────────────────────────────────

const deviceDrawerOpen = ref(false)
const editingDevice = ref<DraftDevice | null>(null)

function openAddDevice() {
  editingDevice.value = null
  deviceDrawerOpen.value = true
}

function openEditDevice(dev: DraftDevice) {
  editingDevice.value = dev
  deviceDrawerOpen.value = true
}

function onDeviceDraftSaved(data: Record<string, any>) {
  if (editingDevice.value) {
    const idx = draftDevices.value.findIndex(d => d._id === editingDevice.value!._id)
    if (idx !== -1) draftDevices.value[idx] = { ...draftDevices.value[idx], ...data }
  } else {
    const newDev: DraftDevice = { _id: crypto.randomUUID(), ...data as any, objects: [] }
    draftDevices.value.push(newDev)
    selectedId.value = newDev._id
  }
}

function duplicateDevice(dev: DraftDevice) {
  const existingInstances = draftDevices.value.map(d => d.device_instance)
  let inst = dev.device_instance + 1
  while (existingInstances.includes(inst)) inst++
  const copy: DraftDevice = {
    ...dev,
    _id: crypto.randomUUID(),
    device_instance: inst,
    name: `${dev.name} (copy)`,
    objects: dev.objects.map(o => ({ ...o, _id: crypto.randomUUID() })),
  }
  draftDevices.value.push(copy)
  selectedId.value = copy._id
  message.success('Device duplicated')
}

function removeDevice(dev: DraftDevice) {
  draftDevices.value = draftDevices.value.filter(d => d._id !== dev._id)
  if (selectedId.value === dev._id) {
    selectedId.value = draftDevices.value[0]?._id ?? null
  }
}

const existingInstances = computed(() => {
  const editing = editingDevice.value?.device_instance
  return draftDevices.value
    .map(d => d.device_instance)
    .filter(i => i !== editing)
})

// ── Object CRUD ───────────────────────────────────────────────────────────────

const objectDrawerOpen = ref(false)
const editingObject = ref<DraftObject | null>(null)

function openAddObject() {
  if (!selectedDevice.value) return
  editingObject.value = null
  objectDrawerOpen.value = true
}

function openEditObject(obj: DraftObject) {
  editingObject.value = obj
  objectDrawerOpen.value = true
}

function onObjectDraftSaved(data: Record<string, any>) {
  const dev = selectedDevice.value
  if (!dev) return
  if (editingObject.value) {
    const idx = dev.objects.findIndex(o => o._id === editingObject.value!._id)
    if (idx !== -1) dev.objects[idx] = { ...dev.objects[idx], ...data as DraftObject }
  } else {
    dev.objects.push({ _id: crypto.randomUUID(), ...(data as Omit<DraftObject, '_id'>) })
  }
}

function duplicateObject(obj: DraftObject) {
  const dev = selectedDevice.value
  if (!dev) return
  const used = new Set(dev.objects.filter(o => o.object_type === obj.object_type).map(o => o.object_instance))
  let inst = obj.object_instance + 1
  while (used.has(inst)) inst++
  dev.objects.push({ ...obj, _id: crypto.randomUUID(), object_instance: inst, name: `${obj.name} (copy)` })
}

function removeObject(obj: DraftObject) {
  const dev = selectedDevice.value
  if (!dev) return
  dev.objects = dev.objects.filter(o => o._id !== obj._id)
}

// ── Built-in templates ────────────────────────────────────────────────────────

interface BuiltInTemplate {
  label: string
  desc: string
  objects: Omit<DraftObject, '_id'>[]
}

const TEMPLATES: BuiltInTemplate[] = [
  {
    label: 'AHU',
    desc: 'Air Handling Unit (fans, valves, temps)',
    objects: [
      { object_type: 'analog-input',  object_instance: 0, name: 'Supply Air Temp',    units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 14, amplitude: 2, period_hours: 24, phase_hours: 0 }), enabled: true },
      { object_type: 'analog-input',  object_instance: 1, name: 'Return Air Temp',    units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 22, amplitude: 1, period_hours: 24, phase_hours: 4 }), enabled: true },
      { object_type: 'analog-output', object_instance: 0, name: 'Supply Fan Speed',   units: 'percent',         behavior: 'manual', behavior_params: JSON.stringify({ value: 75 }), enabled: true },
      { object_type: 'analog-output', object_instance: 1, name: 'Cooling Valve',      units: 'percent',         behavior: 'manual', behavior_params: JSON.stringify({ value: 0 }),  enabled: true },
      { object_type: 'analog-output', object_instance: 2, name: 'Heating Valve',      units: 'percent',         behavior: 'manual', behavior_params: JSON.stringify({ value: 0 }),  enabled: true },
      { object_type: 'binary-input',  object_instance: 0, name: 'Supply Fan Status',  units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
      { object_type: 'binary-output', object_instance: 0, name: 'Supply Fan Command', units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
    ],
  },
  {
    label: 'VAV',
    desc: 'Variable Air Volume box (zone, airflow, damper)',
    objects: [
      { object_type: 'analog-input',  object_instance: 0, name: 'Zone Temp',      units: 'degrees-celsius',         behavior: 'sine',     behavior_params: JSON.stringify({ base: 22, amplitude: 1, period_hours: 24, phase_hours: 6 }), enabled: true },
      { object_type: 'analog-input',  object_instance: 1, name: 'Airflow',        units: 'cubic-feet-per-minute',   behavior: 'noise',    behavior_params: JSON.stringify({ base: 300, noise: 20 }), enabled: true },
      { object_type: 'analog-output', object_instance: 0, name: 'Damper Position',units: 'percent',                 behavior: 'manual',   behavior_params: JSON.stringify({ value: 50 }), enabled: true },
      { object_type: 'analog-value',  object_instance: 0, name: 'Temp Setpoint',  units: 'degrees-celsius',         behavior: 'constant', behavior_params: JSON.stringify({ value: 21 }), enabled: true },
      { object_type: 'binary-input',  object_instance: 0, name: 'Occupancy',      units: 'no-units',                behavior: 'schedule', behavior_params: JSON.stringify({ default: 0, blocks: [{ start: '08:00', value: 1 }, { start: '18:00', value: 0 }] }), enabled: true },
    ],
  },
  {
    label: 'FCU',
    desc: 'Fan Coil Unit (room temp, fan, valves)',
    objects: [
      { object_type: 'analog-input',  object_instance: 0, name: 'Room Temp',       units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 21, amplitude: 1.5, period_hours: 24, phase_hours: 8 }), enabled: true },
      { object_type: 'analog-output', object_instance: 0, name: 'Fan Speed',        units: 'percent',         behavior: 'manual', behavior_params: JSON.stringify({ value: 50 }), enabled: true },
      { object_type: 'analog-output', object_instance: 1, name: 'Cooling Coil',     units: 'percent',         behavior: 'manual', behavior_params: JSON.stringify({ value: 0 }),  enabled: true },
      { object_type: 'analog-output', object_instance: 2, name: 'Heating Coil',     units: 'percent',         behavior: 'manual', behavior_params: JSON.stringify({ value: 0 }),  enabled: true },
      { object_type: 'analog-value',  object_instance: 0, name: 'Setpoint',         units: 'degrees-celsius', behavior: 'constant', behavior_params: JSON.stringify({ value: 21 }), enabled: true },
      { object_type: 'binary-output', object_instance: 0, name: 'Fan On/Off',       units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
    ],
  },
  {
    label: 'Chiller',
    desc: 'Chiller plant (chilled water temps, compressor)',
    objects: [
      { object_type: 'analog-input',  object_instance: 0, name: 'Leaving CHW Temp',  units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 7,  amplitude: 0.5, period_hours: 4, phase_hours: 0 }), enabled: true },
      { object_type: 'analog-input',  object_instance: 1, name: 'Entering CHW Temp', units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 12, amplitude: 0.5, period_hours: 4, phase_hours: 2 }), enabled: true },
      { object_type: 'analog-input',  object_instance: 2, name: 'Compressor Current',units: 'amperes',         behavior: 'noise',  behavior_params: JSON.stringify({ base: 85, noise: 5 }), enabled: true },
      { object_type: 'analog-output', object_instance: 0, name: 'CHW Setpoint',      units: 'degrees-celsius', behavior: 'constant', behavior_params: JSON.stringify({ value: 7 }), enabled: true },
      { object_type: 'analog-value',  object_instance: 0, name: 'Cooling Capacity',  units: 'kilowatts',       behavior: 'noise',  behavior_params: JSON.stringify({ base: 350, noise: 20 }), enabled: true },
      { object_type: 'binary-input',  object_instance: 0, name: 'Chiller Running',   units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
      { object_type: 'binary-output', object_instance: 0, name: 'Chiller Enable',    units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
    ],
  },
  {
    label: 'Boiler',
    desc: 'Heating boiler (HW temps, burner, gas)',
    objects: [
      { object_type: 'analog-input',  object_instance: 0, name: 'Supply HW Temp',   units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 80, amplitude: 5, period_hours: 24, phase_hours: 0 }), enabled: true },
      { object_type: 'analog-input',  object_instance: 1, name: 'Return HW Temp',   units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 65, amplitude: 3, period_hours: 24, phase_hours: 0 }), enabled: true },
      { object_type: 'analog-input',  object_instance: 2, name: 'Gas Consumption',  units: 'therms',          behavior: 'noise',  behavior_params: JSON.stringify({ base: 2.5, noise: 0.3 }), enabled: true },
      { object_type: 'analog-output', object_instance: 0, name: 'HW Setpoint',      units: 'degrees-celsius', behavior: 'constant', behavior_params: JSON.stringify({ value: 80 }), enabled: true },
      { object_type: 'binary-input',  object_instance: 0, name: 'Burner Status',    units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
      { object_type: 'binary-output', object_instance: 0, name: 'Boiler Enable',    units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }), enabled: true },
    ],
  },
  {
    label: 'BMS Gateway',
    desc: 'Top-level BMS controller (outside air, alarms, energy)',
    objects: [
      { object_type: 'analog-input',  object_instance: 0, name: 'Outside Air Temp',  units: 'degrees-celsius', behavior: 'sine',   behavior_params: JSON.stringify({ base: 10, amplitude: 8, period_hours: 24, phase_hours: 14 }), enabled: true },
      { object_type: 'analog-value',  object_instance: 0, name: 'Building kW',       units: 'kilowatts',       behavior: 'noise',  behavior_params: JSON.stringify({ base: 120, noise: 15 }), enabled: true },
      { object_type: 'analog-value',  object_instance: 1, name: 'Active Alarms',     units: 'no-units',        behavior: 'constant', behavior_params: JSON.stringify({ value: 0 }), enabled: true },
      { object_type: 'binary-value',  object_instance: 0, name: 'Building Occupied', units: 'no-units',        behavior: 'schedule', behavior_params: JSON.stringify({ default: 0, blocks: [{ start: '07:00', value: 1 }, { start: '19:00', value: 0 }] }), enabled: true },
    ],
  },
  {
    label: 'Energy Meter',
    desc: 'Electrical meter (kW, kVA, power factor, energy total)',
    objects: [
      { object_type: 'analog-input', object_instance: 0, name: 'Active Power',    units: 'kilowatts',          behavior: 'noise', behavior_params: JSON.stringify({ base: 50,   noise: 5 }),    enabled: true },
      { object_type: 'analog-input', object_instance: 1, name: 'Apparent Power',  units: 'kilovolt-amperes',   behavior: 'noise', behavior_params: JSON.stringify({ base: 55,   noise: 5 }),    enabled: true },
      { object_type: 'analog-input', object_instance: 2, name: 'Power Factor',    units: 'no-units',           behavior: 'noise', behavior_params: JSON.stringify({ base: 0.92, noise: 0.02 }), enabled: true },
      { object_type: 'analog-value', object_instance: 0, name: 'Total kWh',       units: 'kilowatt-hours',     behavior: 'ramp',  behavior_params: JSON.stringify({ from: 0, to: 9999999, duration_minutes: 99999999, repeat: false }), enabled: true },
    ],
  },
  {
    label: 'Lighting Zone',
    desc: 'Lighting controller (level, occupancy, scene)',
    objects: [
      { object_type: 'analog-output', object_instance: 0, name: 'Dimmer Level',    units: 'percent',  behavior: 'manual',   behavior_params: JSON.stringify({ value: 80 }),  enabled: true },
      { object_type: 'analog-value',  object_instance: 0, name: 'Lux Level',       units: 'lux',      behavior: 'noise',    behavior_params: JSON.stringify({ base: 500, noise: 50 }), enabled: true },
      { object_type: 'binary-input',  object_instance: 0, name: 'Occupancy',       units: 'no-units', behavior: 'schedule', behavior_params: JSON.stringify({ default: 0, blocks: [{ start: '08:00', value: 1 }, { start: '18:00', value: 0 }] }), enabled: true },
      { object_type: 'binary-output', object_instance: 0, name: 'Lights On/Off',   units: 'no-units', behavior: 'constant', behavior_params: JSON.stringify({ value: 1 }),   enabled: true },
    ],
  },
]

function applyTemplate(template: BuiltInTemplate) {
  const dev = selectedDevice.value
  if (!dev) return

  const usedByType: Record<string, Set<number>> = {}
  for (const o of dev.objects) {
    if (!usedByType[o.object_type]) usedByType[o.object_type] = new Set()
    usedByType[o.object_type].add(o.object_instance)
  }

  for (const tpl of template.objects) {
    const used = usedByType[tpl.object_type] ?? new Set<number>()
    let inst = tpl.object_instance
    while (used.has(inst)) inst++
    used.add(inst)
    usedByType[tpl.object_type] = used
    dev.objects.push({ _id: crypto.randomUUID(), ...tpl, object_instance: inst })
  }
  message.success(`Applied ${template.label} template — ${template.objects.length} objects added`)
}

// ── Copy from live ────────────────────────────────────────────────────────────

const copyingLive = ref(false)

async function copyFromLive() {
  copyingLive.value = true
  try {
    const devices = await api.devices.list()
    const newDrafts: DraftDevice[] = await Promise.all(
      devices.map(async (dev) => {
        const objects = await api.objects.list(dev.id)
        return {
          _id: crypto.randomUUID(),
          device_instance: dev.device_instance,
          name: dev.name,
          description: dev.description,
          vendor_name: dev.vendor_name,
          model_name: dev.model_name,
          enabled: !!dev.enabled,
          objects: objects.map(o => ({
            _id: crypto.randomUUID(),
            object_type: o.object_type,
            object_instance: o.object_instance,
            name: o.name,
            units: o.units,
            behavior: o.behavior,
            behavior_params: o.behavior_params,
            enabled: !!o.enabled,
          })),
        }
      }),
    )
    draftDevices.value = newDrafts
    selectedId.value = newDrafts[0]?._id ?? null
    message.success(`Copied ${newDrafts.length} device${newDrafts.length !== 1 ? 's' : ''} from live stack`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to copy from live')
  } finally {
    copyingLive.value = false
  }
}

// ── Save profile ──────────────────────────────────────────────────────────────

const saving = ref(false)

const totalObjects = computed(() =>
  draftDevices.value.reduce((n, d) => n + d.objects.length, 0),
)

async function saveProfile() {
  if (!profileName.value.trim()) { message.error('Profile name is required'); return }
  if (!draftDevices.value.length) { message.error('Add at least one device'); return }

  saving.value = true
  try {
    const data = {
      devices: draftDevices.value.map(dev => ({
        device_instance: dev.device_instance,
        name: dev.name,
        description: dev.description,
        vendor_name: dev.vendor_name,
        model_name: dev.model_name,
        enabled: dev.enabled ? 1 : 0,
        objects: dev.objects.map(o => ({
          object_type: o.object_type,
          object_instance: o.object_instance,
          name: o.name,
          units: o.units,
          behavior: o.behavior,
          behavior_params: o.behavior_params,
          enabled: o.enabled ? 1 : 0,
          manual_value: null,
        })),
      })),
    }
    await api.profiles.import_(profileName.value.trim(), profileDesc.value.trim(), data)
    message.success(`Profile "${profileName.value.trim()}" created`)
    emit('saved')
    emit('update:open', false)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save profile')
  } finally {
    saving.value = false
  }
}

// ── Table columns ─────────────────────────────────────────────────────────────

const objectColumns = [
  { title: 'Type',     key: 'object_type',     width: 140 },
  { title: 'Inst',     key: 'object_instance',  width: 55  },
  { title: 'Name',     key: 'name',             ellipsis: true },
  { title: 'Units',    key: 'units',            width: 120 },
  { title: 'Behavior', key: 'behavior',         width: 110 },
  { title: '',         key: 'actions',          width: 88, fixed: 'right' as const },
]

const TYPE_COLORS: Record<string, string> = {
  'analog-input':   'blue',
  'analog-output':  'cyan',
  'analog-value':   'geekblue',
  'binary-input':   'purple',
  'binary-output':  'magenta',
  'binary-value':   'volcano',
}
</script>

<template>
  <a-drawer
    :open="open"
    title="Build New Profile"
    :width="1020"
    :body-style="{ padding: 0, display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }"
    @close="emit('update:open', false)"
  >
    <!-- ── Profile meta bar ─────────────────────────────────────────────────── -->
    <div class="meta-bar">
      <a-row :gutter="16" style="width: 100%">
        <a-col :span="9">
          <a-form-item label="Profile Name" :colon="false" style="margin-bottom: 0" required>
            <a-input
              v-model:value="profileName"
              placeholder="e.g. Office Building — Normal Operations"
              allow-clear
            />
          </a-form-item>
        </a-col>
        <a-col :span="11">
          <a-form-item label="Description" :colon="false" style="margin-bottom: 0">
            <a-input v-model:value="profileDesc" placeholder="Optional description" allow-clear />
          </a-form-item>
        </a-col>
        <a-col :span="4" style="display: flex; align-items: flex-end">
          <a-button :loading="copyingLive" style="width: 100%" @click="copyFromLive">
            <template #icon><DownloadOutlined /></template>
            Copy from Live
          </a-button>
        </a-col>
      </a-row>
    </div>

    <!-- ── Main split ───────────────────────────────────────────────────────── -->
    <div class="split-body">

      <!-- Device panel -->
      <div class="device-panel">
        <div class="panel-header">
          <span class="panel-label">Devices</span>
          <a-tag v-if="draftDevices.length" size="small">{{ draftDevices.length }}</a-tag>
        </div>

        <div class="device-list">
          <div
            v-for="dev in draftDevices"
            :key="dev._id"
            :class="['device-item', { 'device-item--active': selectedId === dev._id }]"
            @click="selectedId = dev._id"
          >
            <div class="device-item-body">
              <div class="device-name">{{ dev.name }}</div>
              <div class="device-meta">#{{ dev.device_instance }} · {{ dev.objects.length }} obj</div>
            </div>
            <a-space :size="2" class="device-actions">
              <a-tooltip title="Edit device">
                <a-button type="text" size="small" @click.stop="openEditDevice(dev)">
                  <template #icon><EditOutlined /></template>
                </a-button>
              </a-tooltip>
              <a-tooltip title="Duplicate">
                <a-button type="text" size="small" @click.stop="duplicateDevice(dev)">
                  <template #icon><CopyOutlined /></template>
                </a-button>
              </a-tooltip>
              <a-tooltip title="Remove">
                <a-button type="text" size="small" danger @click.stop="removeDevice(dev)">
                  <template #icon><DeleteOutlined /></template>
                </a-button>
              </a-tooltip>
            </a-space>
          </div>

          <a-empty
            v-if="!draftDevices.length"
            description="No devices yet"
            :image-style="{ height: '40px' }"
            style="margin: 32px 0"
          />
        </div>

        <div class="device-footer">
          <a-button type="dashed" block @click="openAddDevice">
            <template #icon><PlusOutlined /></template>
            Add Device
          </a-button>
        </div>
      </div>

      <!-- Object panel -->
      <div class="object-panel">
        <!-- Object panel header -->
        <div class="panel-header" style="padding: 0 16px">
          <div style="display: flex; align-items: center; gap: 10px; min-width: 0">
            <span v-if="selectedDevice" class="panel-label" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis">
              {{ selectedDevice.name }}
            </span>
            <span v-else class="panel-label" style="color: #bbb; font-weight: 400">Select a device</span>
            <a-tag v-if="selectedDevice" size="small">{{ selectedDevice.objects.length }}</a-tag>
          </div>

          <a-space v-if="selectedDevice" :size="8">
            <!-- Template dropdown -->
            <a-dropdown :trigger="['click']">
              <a-button size="small">
                <template #icon><AppstoreOutlined /></template>
                Template
              </a-button>
              <template #overlay>
                <a-menu>
                  <a-menu-item
                    v-for="tpl in TEMPLATES"
                    :key="tpl.label"
                    @click="applyTemplate(tpl)"
                  >
                    <div>
                      <div style="font-weight: 500">{{ tpl.label }}</div>
                      <div style="font-size: 11px; color: #888">{{ tpl.desc }}</div>
                    </div>
                  </a-menu-item>
                </a-menu>
              </template>
            </a-dropdown>

            <a-button size="small" type="primary" @click="openAddObject">
              <template #icon><PlusOutlined /></template>
              Add Object
            </a-button>
          </a-space>
        </div>

        <!-- Object table -->
        <div style="flex: 1; overflow-y: auto; padding: 16px">
          <template v-if="selectedDevice">
            <a-table
              :data-source="selectedDevice.objects"
              :columns="objectColumns"
              :pagination="false"
              :scroll="{ x: 'max-content' }"
              row-key="_id"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'object_type'">
                  <a-tag :color="TYPE_COLORS[record.object_type] ?? 'default'" style="font-size: 11px">
                    {{ record.object_type }}
                  </a-tag>
                </template>
                <template v-else-if="column.key === 'units'">
                  <span style="font-size: 12px; color: #888">{{ record.units === 'no-units' ? '—' : record.units }}</span>
                </template>
                <template v-else-if="column.key === 'behavior'">
                  <span style="font-size: 12px; color: #666">{{ record.behavior }}</span>
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-space :size="4">
                    <a-button size="small" @click="openEditObject(record)">
                      <template #icon><EditOutlined /></template>
                    </a-button>
                    <a-button size="small" @click="duplicateObject(record)">
                      <template #icon><CopyOutlined /></template>
                    </a-button>
                    <a-button size="small" danger @click="removeObject(record)">
                      <template #icon><DeleteOutlined /></template>
                    </a-button>
                  </a-space>
                </template>
              </template>

              <template #emptyText>
                <div style="padding: 32px 0; text-align: center; color: #bbb; font-size: 13px">
                  No objects — click <strong>Add Object</strong> or pick a <strong>Template</strong>
                </div>
              </template>
            </a-table>
          </template>

          <div v-else style="display: flex; align-items: center; justify-content: center; height: 200px; color: #bbb; font-size: 14px">
            Select a device on the left to add objects
          </div>
        </div>
      </div>
    </div>

    <!-- ── Footer ───────────────────────────────────────────────────────────── -->
    <template #footer>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span style="font-size: 12px; color: #888">
          {{ draftDevices.length }} device{{ draftDevices.length !== 1 ? 's' : '' }} ·
          {{ totalObjects }} object{{ totalObjects !== 1 ? 's' : '' }}
        </span>
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button
            type="primary"
            :loading="saving"
            :disabled="!profileName.trim() || !draftDevices.length"
            @click="saveProfile"
          >
            Save Profile
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>

  <!-- Child drawers (draft mode) -->
  <DeviceDrawer
    v-model:open="deviceDrawerOpen"
    :device="null"
    :draft-mode="true"
    :draft-device="editingDevice"
    :existing-instances="existingInstances"
    @draft-saved="onDeviceDraftSaved"
  />

  <ObjectDrawer
    v-model:open="objectDrawerOpen"
    :object="null"
    :device-id="undefined"
    :draft-mode="true"
    :draft-object="editingObject"
    :meta="meta"
    @draft-saved="onObjectDraftSaved"
  />
</template>

<style scoped>
.meta-bar {
  flex-shrink: 0;
  padding: 16px 24px;
  border-bottom: 1px solid #e8e8e8;
  background: #fafafa;
  display: flex;
  align-items: flex-end;
}

.split-body {
  flex: 1;
  display: flex;
  overflow: hidden;
}

/* ── Device panel ──────────────────────────────────────────────────────────── */

.device-panel {
  width: 230px;
  flex-shrink: 0;
  border-right: 1px solid #e8e8e8;
  display: flex;
  flex-direction: column;
}

.panel-header {
  height: 44px;
  padding: 0 12px;
  border-bottom: 1px solid #f0f0f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}

.panel-label {
  font-size: 12px;
  font-weight: 600;
  color: #555;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.device-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.device-item {
  display: flex;
  align-items: center;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 2px;
  transition: background 0.12s;
}
.device-item:hover { background: #f5f5f5; }
.device-item--active { background: #e6f4ff; }

.device-item-body {
  flex: 1;
  min-width: 0;
}

.device-name {
  font-size: 13px;
  font-weight: 500;
  color: #222;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.device-meta {
  font-size: 11px;
  color: #aaa;
  margin-top: 1px;
}

.device-actions {
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.12s;
}
.device-item:hover .device-actions,
.device-item--active .device-actions {
  opacity: 1;
}

.device-footer {
  flex-shrink: 0;
  padding: 10px;
  border-top: 1px solid #f0f0f0;
}

/* ── Object panel ──────────────────────────────────────────────────────────── */

.object-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}
</style>
