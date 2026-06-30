<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import DeviceDrawer from './components/DeviceDrawer.vue'
import ObjectDrawer from './components/ObjectDrawer.vue'
import type { Device, SimObject, Meta, Health } from './types'
import { api } from './api'

const apiPort = window.location.port || '47900'

const health  = ref<Health>({ status: 'unknown', bacnet_running: false, devices: 0 })
const meta    = ref<Meta>({ object_types: [], behaviors: [], units: [] })
const devices = ref<Device[]>([])
const selectedDevice = ref<Device | null>(null)
const objects = ref<SimObject[]>([])
const liveValues = ref<Record<number, number | boolean>>({})

// Drawers
const deviceDrawerOpen = ref(false)
const editingDevice    = ref<Device | null>(null)
const objectDrawerOpen = ref(false)
const editingObject    = ref<SimObject | null>(null)

// Set-value modal
const setValOpen    = ref(false)
const setValObj     = ref<SimObject | null>(null)
const setValInput   = ref(0)
const setValLoading = ref(false)

// WebSocket
let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null

function wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${proto}//${location.host}/ws`)
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data) as { devices?: { objects?: { id: number; value: number | boolean }[] }[] }
    const map: Record<number, number | boolean> = {}
    data.devices?.forEach(d => d.objects?.forEach(o => { map[o.id] = o.value }))
    liveValues.value = map
  }
  ws.onclose = () => { wsTimer = setTimeout(wsConnect, 3000) }
  ws.onerror = () => ws?.close()
}

function liveVal(id: number): number | boolean | null {
  const v = liveValues.value[id]
  return v !== undefined ? v : null
}

function hasLive(id: number): boolean {
  return liveValues.value[id] !== undefined
}

function fmtVal(obj: SimObject): string {
  const v = liveVal(obj.id)
  if (v === null) return '—'
  if (typeof v === 'boolean') return v ? 'ON' : 'OFF'
  const n = Number(v)
  return isNaN(n) ? String(v) : n.toFixed(2)
}

// Loaders
async function loadHealth() {
  try { health.value = await api.health() } catch { /* swallow */ }
}
async function loadMeta() {
  try { meta.value = await api.meta() } catch { /* swallow */ }
}
async function loadDevices() {
  try {
    devices.value = await api.devices.list()
    if (selectedDevice.value) {
      const found = devices.value.find(d => d.id === selectedDevice.value!.id)
      selectedDevice.value = found ?? null
    }
  } catch { /* swallow */ }
}
async function loadObjects() {
  if (!selectedDevice.value) return
  try { objects.value = await api.objects.list(selectedDevice.value.id) } catch { /* swallow */ }
}

function selectDevice(d: Device) {
  selectedDevice.value = d
  loadObjects()
}

// Device actions
function openAddDevice() { editingDevice.value = null; deviceDrawerOpen.value = true }
function openEditDevice(d: Device) { editingDevice.value = d; deviceDrawerOpen.value = true }
async function onDeviceSaved() { await loadDevices(); await loadHealth() }
function deleteDevice(d: Device) {
  Modal.confirm({
    title: `Delete "${d.name}"?`,
    content: 'This also deletes all its objects and cannot be undone.',
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      await api.devices.del(d.id)
      if (selectedDevice.value?.id === d.id) { selectedDevice.value = null; objects.value = [] }
      await loadDevices(); await loadHealth()
      message.success('Device deleted')
    },
  })
}

// Object actions
function openAddObject() { editingObject.value = null; objectDrawerOpen.value = true }
function openEditObject(obj: SimObject) { editingObject.value = obj; objectDrawerOpen.value = true }
async function onObjectSaved() { await loadObjects() }
function deleteObject(obj: SimObject) {
  Modal.confirm({
    title: `Delete "${obj.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      await api.objects.del(selectedDevice.value!.id, obj.id)
      await loadObjects()
      message.success('Object deleted')
    },
  })
}

// Set value
function openSetValue(obj: SimObject) {
  setValObj.value = obj
  setValInput.value = Number(liveVal(obj.id) ?? 0)
  setValOpen.value = true
}
async function doSetValue() {
  if (!setValObj.value || !selectedDevice.value) return
  setValLoading.value = true
  try {
    await api.objects.setValue(selectedDevice.value.id, setValObj.value.id, setValInput.value)
    setValOpen.value = false
    message.success('Value updated')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    setValLoading.value = false
  }
}

// Table
const BEHAVIOR_COLOR: Record<string, string> = {
  constant: 'default', sine: 'blue', noise: 'orange', random_walk: 'purple', manual: 'red',
}

const columns: TableColumnsType = [
  { title: 'Name',       dataIndex: 'name',            key: 'name' },
  { title: 'Type',       key: 'type',                  width: 170 },
  { title: 'Inst.',      dataIndex: 'object_instance', key: 'instance', width: 65 },
  { title: 'Behavior',   key: 'behavior',              width: 120 },
  { title: 'Units',      dataIndex: 'units',           key: 'units',    width: 150 },
  { title: 'Live Value', key: 'value',                 width: 110 },
  { title: 'On',         key: 'enabled',               width: 50  },
  { title: '',           key: 'actions',               width: 145 },
]

// Lifecycle
let healthTimer: ReturnType<typeof setInterval>
onMounted(async () => {
  await Promise.all([loadMeta(), loadDevices(), loadHealth()])
  wsConnect()
  healthTimer = setInterval(loadHealth, 10_000)
})
onUnmounted(() => {
  clearInterval(healthTimer)
  if (wsTimer) clearTimeout(wsTimer)
  ws?.close()
})
</script>

<template>
  <a-config-provider :theme="{ token: { colorPrimary: '#1890ff', borderRadius: 4 } }">
    <a-layout style="height:100vh">

      <!-- Header -->
      <a-layout-header style="display:flex;align-items:center;gap:14px;padding:0 20px;height:48px;line-height:48px">
        <span style="color:white;font-size:15px;font-weight:600;letter-spacing:.3px">⚙ BACnet Simulator</span>
        <span :style="{ fontSize:'12px', color: health.bacnet_running ? '#52c41a' : '#ff4d4f' }">
          {{ health.bacnet_running ? '● BACnet running' : '○ BACnet stopped' }}
        </span>
        <span style="color:#555;font-size:12px">{{ health.devices }} device(s)</span>
        <div style="flex:1" />
        <span style="color:#444;font-size:11px">:{{ apiPort }}</span>
      </a-layout-header>

      <a-layout>

        <!-- Sidebar: devices -->
        <a-layout-sider :width="260" style="background:white;border-right:1px solid #e8e8e8;overflow:auto">
          <div style="padding:10px 12px 10px 16px;border-bottom:1px solid #e8e8e8;display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.8px">Devices</span>
            <a-button size="small" type="primary" @click="openAddDevice">+ Add</a-button>
          </div>

          <div v-if="!devices.length" style="padding:24px 16px;color:#bbb;text-align:center;font-size:13px">
            No devices yet
          </div>

          <div
            v-for="d in devices" :key="d.id"
            style="padding:10px 12px 10px 14px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid #f5f5f5;transition:background .1s"
            :style="{
              background: selectedDevice?.id === d.id ? '#e6f7ff' : 'white',
              borderRight: selectedDevice?.id === d.id ? '3px solid #1890ff' : '3px solid transparent',
            }"
            @click="selectDevice(d)"
          >
            <a-badge :status="d.enabled ? 'success' : 'default'" />
            <div style="flex:1;min-width:0">
              <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ d.name }}</div>
              <div style="font-size:11px;color:#aaa">ID {{ d.device_instance }}</div>
            </div>
            <a-space :size="2">
              <a-button type="text" size="small" title="Edit" @click.stop="openEditDevice(d)">✏</a-button>
              <a-button type="text" size="small" danger title="Delete" @click.stop="deleteDevice(d)">✕</a-button>
            </a-space>
          </div>
        </a-layout-sider>

        <!-- Content: objects -->
        <a-layout-content style="overflow:auto;padding:20px">

          <div v-if="!selectedDevice" style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px;color:#ccc">
            <span style="font-size:52px">📡</span>
            <span style="font-size:15px;color:#bbb">Select a device to manage its objects</span>
          </div>

          <template v-else>
            <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px">
              <div>
                <div style="font-size:18px;font-weight:600">{{ selectedDevice.name }}</div>
                <div style="font-size:12px;color:#aaa;margin-top:3px">
                  Device {{ selectedDevice.device_instance }}
                  <template v-if="selectedDevice.description"> — {{ selectedDevice.description }}</template>
                  <template v-else> — {{ selectedDevice.model_name }}</template>
                </div>
              </div>
              <a-button type="primary" @click="openAddObject">+ Add Object</a-button>
            </div>

            <a-table
              :data-source="objects"
              :columns="columns"
              :pagination="false"
              size="small"
              row-key="id"
              :locale="{ emptyText: 'No objects yet — click Add Object' }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'type'">
                  <a-tag style="font-family:monospace;font-size:11px">{{ (record as SimObject).object_type }}</a-tag>
                </template>
                <template v-else-if="column.key === 'behavior'">
                  <a-tag :color="BEHAVIOR_COLOR[(record as SimObject).behavior]">{{ (record as SimObject).behavior }}</a-tag>
                </template>
                <template v-else-if="column.key === 'units'">
                  <span style="color:#aaa;font-size:12px">{{ (record as SimObject).units === 'no-units' ? '—' : (record as SimObject).units }}</span>
                </template>
                <template v-else-if="column.key === 'value'">
                  <span :style="{ fontFamily:'monospace', color: hasLive((record as SimObject).id) ? '#1890ff' : '#ccc' }">
                    {{ fmtVal(record as SimObject) }}
                  </span>
                </template>
                <template v-else-if="column.key === 'enabled'">
                  <a-badge :status="(record as SimObject).enabled ? 'success' : 'default'" />
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-space :size="2">
                    <a-button type="link" size="small" @click="openEditObject(record as SimObject)">Edit</a-button>
                    <a-button
                      v-if="(record as SimObject).behavior === 'manual'"
                      type="link" size="small"
                      style="color:#fa8c16"
                      @click="openSetValue(record as SimObject)"
                    >Set</a-button>
                    <a-button type="link" size="small" danger @click="deleteObject(record as SimObject)">Del</a-button>
                  </a-space>
                </template>
              </template>
            </a-table>
          </template>

        </a-layout-content>
      </a-layout>
    </a-layout>

    <!-- Device drawer -->
    <DeviceDrawer
      v-model:open="deviceDrawerOpen"
      :device="editingDevice"
      @saved="onDeviceSaved"
    />

    <!-- Object drawer -->
    <ObjectDrawer
      v-model:open="objectDrawerOpen"
      :object="editingObject"
      :device-id="selectedDevice?.id"
      :meta="meta"
      @saved="onObjectSaved"
    />

    <!-- Set value modal -->
    <a-modal
      v-model:open="setValOpen"
      :title="`Set Value — ${setValObj?.name}`"
      ok-text="Set"
      :confirm-loading="setValLoading"
      @ok="doSetValue"
    >
      <div style="padding:8px 0">
        <a-input-number
          v-model:value="setValInput"
          style="width:100%"
          :step="0.1"
          @pressEnter="doSetValue"
        />
      </div>
    </a-modal>

  </a-config-provider>
</template>
