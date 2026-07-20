<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import DeviceDrawer from './components/DeviceDrawer.vue'
import ObjectDrawer from './components/ObjectDrawer.vue'
import ProfilesDrawer from './components/ProfilesDrawer.vue'
import TemplatePickerModal from './components/TemplatePickerModal.vue'
import SaveTemplateModal from './components/SaveTemplateModal.vue'
import IotisticaLogo from './components/IotisticaLogo.vue'
import DeviceLogPanel from './components/DeviceLogPanel.vue'
import LoginView from './components/LoginView.vue'
import UsersDrawer from './components/UsersDrawer.vue'
import type { Device, SimObject, Meta, Health, HistoryPoint } from './types'
import { api } from './api'
import { authToken, currentUser, logout } from './auth'
import { EditOutlined, DeleteOutlined, ApiOutlined, CopyOutlined, FileAddOutlined, LineChartOutlined, PlayCircleOutlined, PauseCircleOutlined, StopOutlined, UserOutlined, LogoutOutlined, TeamOutlined } from '@ant-design/icons-vue'

const apiPort = window.location.port || '47900'

const health  = ref<Health>({ status: 'unknown', bacnet_running: false, devices: 0, sim_running: true, elapsed_seconds: 0 })
const simActionLoading = ref(false)
const meta    = ref<Meta>({ object_types: [], behaviors: [], units: [] })
const devices = ref<Device[]>([])
const selectedDevice = ref<Device | null>(null)
const objects = ref<SimObject[]>([])
const liveValues = ref<Record<number, number | boolean>>({})

// Drawers
const deviceDrawerOpen  = ref(false)
const editingDevice     = ref<Device | null>(null)
const objectDrawerOpen  = ref(false)
const editingObject     = ref<SimObject | null>(null)
const profilesDrawerOpen   = ref(false)
const templateModalOpen    = ref(false)
const saveTemplateOpen     = ref(false)
const usersDrawerOpen      = ref(false)

// Active profile state
const activeProfileId   = ref<number | null>(null)
const activeProfileName = ref<string | null>(null)
const activeProfileDesc = ref<string>('')

// Save-profile modal
const saveModalOpen    = ref(false)
const saveModalName    = ref('')
const saveModalDesc    = ref('')
const saveModalLoading = ref(false)

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
  ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(authToken.value ?? '')}`)
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data) as { devices?: { objects?: { id: number; value: number | boolean }[] }[] }
    const map: Record<number, number | boolean> = {}
    data.devices?.forEach(d => d.objects?.forEach(o => { map[o.id] = o.value }))
    liveValues.value = map
  }
  // Only keep retrying while still logged in — an expired/cleared token would
  // otherwise reconnect forever against a server that immediately closes it.
  ws.onclose = () => { if (authToken.value) wsTimer = setTimeout(wsConnect, 3000) }
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

async function simStart() {
  simActionLoading.value = true
  try { await api.sim.start(); await loadHealth() }
  catch (e) { message.error((e as Error).message) }
  finally { simActionLoading.value = false }
}
async function simPause() {
  simActionLoading.value = true
  try { await api.sim.pause(); await loadHealth() }
  catch (e) { message.error((e as Error).message) }
  finally { simActionLoading.value = false }
}
async function simStop() {
  simActionLoading.value = true
  try { await api.sim.stop(); await loadHealth() }
  catch (e) { message.error((e as Error).message) }
  finally { simActionLoading.value = false }
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
async function duplicateDevice(d: Device) {
  const nextInstance = devices.value.length
    ? Math.max(...devices.value.map(x => x.device_instance)) + 1
    : d.device_instance + 1
  try {
    const created = await api.devices.create({
      device_instance: nextInstance,
      name:            `${d.name} Copy`,
      description:     d.description,
      vendor_name:     d.vendor_name,
      model_name:      d.model_name,
      enabled:         d.enabled,
    })
    const srcObjects = await api.objects.list(d.id)
    for (const obj of srcObjects) {
      await api.objects.create(created.id, {
        object_type:     obj.object_type,
        object_instance: obj.object_instance,
        name:            obj.name,
        units:           obj.units,
        behavior:        obj.behavior,
        behavior_params: obj.behavior_params,
        enabled:         obj.enabled,
      })
    }
    await loadDevices()
    await loadHealth()
    message.success(`Duplicated "${d.name}" with ${srcObjects.length} object${srcObjects.length !== 1 ? 's' : ''}`)
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
}
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

// Profile actions
function newProfile() {
  Modal.confirm({
    title: 'Start a new profile?',
    content: 'Save the current setup as a profile first if you want to keep it.',
    okText: 'Start Fresh',
    okType: 'danger',
    async onOk() {
      await Promise.allSettled(devices.value.map(d => api.devices.del(d.id)))
      selectedDevice.value = null
      objects.value = []
      activeProfileId.value = null
      activeProfileName.value = null
      activeProfileDesc.value = ''
      await loadDevices()
      await loadHealth()
      message.success('Ready — add your first device')
    },
  })
}

function openSave() {
  if (activeProfileId.value !== null) {
    // Overwrite existing profile directly — no dialog
    Modal.confirm({
      title: `Save to "${activeProfileName.value}"?`,
      okText: 'Save',
      async onOk() {
        try {
          await api.profiles.update(activeProfileId.value!, activeProfileName.value!, activeProfileDesc.value)
          message.success(`"${activeProfileName.value}" saved`)
        } catch (e: unknown) {
          message.error((e as Error).message ?? 'Failed to save')
        }
      },
    })
  } else {
    saveModalName.value = ''
    saveModalDesc.value = ''
    saveModalOpen.value = true
  }
}

async function doSave() {
  if (!saveModalName.value.trim()) return
  saveModalLoading.value = true
  try {
    const profile = await api.profiles.save(saveModalName.value.trim(), saveModalDesc.value.trim())
    activeProfileId.value = profile.id
    activeProfileName.value = profile.name
    activeProfileDesc.value = profile.description
    saveModalOpen.value = false
    message.success(`"${profile.name}" saved`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save')
  } finally {
    saveModalLoading.value = false
  }
}

async function onProfileLoaded(id: number, name: string, desc: string) {
  activeProfileId.value = id
  activeProfileName.value = name
  activeProfileDesc.value = desc
  await loadDevices()
  selectedDevice.value = null
  objects.value = []
  await loadHealth()
}

// Object actions
function openAddObject() { editingObject.value = null; objectDrawerOpen.value = true }
function openEditObject(obj: SimObject) { editingObject.value = obj; objectDrawerOpen.value = true }
async function onObjectSaved() { await loadObjects() }
async function duplicateObject(obj: SimObject) {
  if (!selectedDevice.value) return
  const nextInstance = objects.value.length
    ? Math.max(...objects.value.map(o => o.object_instance)) + 1
    : obj.object_instance + 1
  try {
    await api.objects.create(selectedDevice.value.id, {
      object_type:     obj.object_type,
      object_instance: nextInstance,
      name:            `${obj.name} Copy`,
      units:           obj.units,
      behavior:        obj.behavior,
      behavior_params: obj.behavior_params,
      enabled:         obj.enabled,
    })
    await loadObjects()
    message.success(`Duplicated "${obj.name}"`)
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
}
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
async function toggleObjectEnabled(obj: SimObject) {
  const nextEnabled = obj.enabled ? 0 : 1
  try {
    await api.objects.update(selectedDevice.value!.id, obj.id, {
      object_type:     obj.object_type,
      object_instance: obj.object_instance,
      name:            obj.name,
      units:           obj.units,
      behavior:        obj.behavior,
      behavior_params: obj.behavior_params,
      enabled:         nextEnabled,
    })
    obj.enabled = nextEnabled
  } catch (e) {
    message.error((e as Error).message || 'Failed to toggle object')
  }
}

// History chart
const histModalOpen   = ref(false)
const histObj         = ref<SimObject | null>(null)
const histData        = ref<HistoryPoint[]>([])
const histLoading     = ref(false)

async function openHistory(obj: SimObject) {
  if (!selectedDevice.value) return
  histObj.value = obj
  histData.value = []
  histLoading.value = true
  histModalOpen.value = true
  try {
    histData.value = await api.objects.history(selectedDevice.value.id, obj.id)
  } catch { /* swallow */ } finally {
    histLoading.value = false
  }
}

const CHART_W = 600
const CHART_H = 192
const CHART_PAD = { top: 16, right: 12, bottom: 30, left: 52 }

function histSvgPoints(data: HistoryPoint[]): string {
  if (data.length < 2) return ''
  const vals = data.map(p => p.value)
  const tss  = data.map(p => p.ts)
  let minV = Math.min(...vals), maxV = Math.max(...vals)
  if (minV === maxV) { minV -= 1; maxV += 1 }
  const minT = tss[0], maxT = tss[tss.length - 1]
  const w = CHART_W - CHART_PAD.left - CHART_PAD.right
  const h = CHART_H - CHART_PAD.top  - CHART_PAD.bottom
  return data.map(p => {
    const x = CHART_PAD.left + ((p.ts - minT) / (maxT - minT)) * w
    const y = CHART_PAD.top  + (1 - (p.value - minV) / (maxV - minV)) * h
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

function histStats(data: HistoryPoint[]) {
  if (!data.length) return { min: 0, max: 0, avg: 0, current: 0 }
  const vals = data.map(p => p.value)
  const min = Math.min(...vals), max = Math.max(...vals)
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length
  return { min, max, avg, current: vals[vals.length - 1] }
}

function histFmt(v: number, obj: SimObject | null): string {
  if (!obj) return v.toFixed(2)
  const isBinary = obj.object_type.startsWith('binary')
  if (isBinary) return v >= 0.5 ? 'ON' : 'OFF'
  return v.toFixed(2)
}

function histYLabels(data: HistoryPoint[], obj: SimObject | null) {
  if (data.length < 2) return []
  const vals = data.map(p => p.value)
  let minV = Math.min(...vals), maxV = Math.max(...vals)
  if (minV === maxV) { minV -= 1; maxV += 1 }
  const h = CHART_H - CHART_PAD.top - CHART_PAD.bottom
  return [
    { y: CHART_PAD.top,           v: maxV },
    { y: CHART_PAD.top + h / 2,   v: (minV + maxV) / 2 },
    { y: CHART_PAD.top + h,       v: minV },
  ].map(t => ({ y: t.y, label: histFmt(t.v, obj) }))
}

function histXLabels(data: HistoryPoint[]) {
  if (data.length < 2) return []
  const tss = data.map(p => p.ts)
  const minT = tss[0], maxT = tss[tss.length - 1]
  const w = CHART_W - CHART_PAD.left - CHART_PAD.right
  const now = Date.now() / 1000
  const N = 5
  return Array.from({ length: N }, (_, i) => {
    const frac = i / (N - 1)
    const ts   = minT + frac * (maxT - minT)
    const x    = CHART_PAD.left + frac * w
    const age  = now - ts
    let label: string
    if (age < 10)        label = 'now'
    else if (age < 120)  label = `-${Math.round(age)}s`
    else if (age < 3600) label = `-${Math.round(age / 60)}m`
    else                 label = `-${Math.round(age / 3600)}h`
    return { x, label }
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
  schedule: 'cyan', ramp: 'green', fault: 'volcano',
}

const columns: TableColumnsType = [
  { title: 'Name',       dataIndex: 'name',            key: 'name' },
  { title: 'Type',       key: 'type',                  width: 170 },
  { title: 'Inst.',      dataIndex: 'object_instance', key: 'instance', width: 65 },
  { title: 'Behavior',   key: 'behavior',              width: 120 },
  { title: 'Units',      dataIndex: 'units',           key: 'units',    width: 150 },
  { title: 'Live Value', key: 'value',                 width: 110 },
  { title: 'On',         key: 'enabled',               width: 50  },
  { title: '',           key: 'actions',               width: 200 },
]

// Lifecycle — gated behind auth: protected endpoints 401 until logged in
let healthTimer: ReturnType<typeof setInterval>

async function startApp() {
  await Promise.all([loadMeta(), loadDevices(), loadHealth()])
  wsConnect()
  healthTimer = setInterval(loadHealth, 10_000)
}

function stopApp() {
  clearInterval(healthTimer)
  if (wsTimer) { clearTimeout(wsTimer); wsTimer = null }
  ws?.close()
  ws = null
}

async function onAuthenticated() {
  await startApp()
}

function doLogout() {
  logout()
  message.success('Signed out')
}

// If a session expires mid-use, api.ts clears authToken — stop polling/ws
// so the app doesn't keep hammering protected endpoints behind the login screen.
watch(authToken, (token) => { if (!token) stopApp() })

onMounted(async () => {
  if (authToken.value) {
    try {
      currentUser.value = await api.auth.me()
      await startApp()
    } catch {
      // api.ts already cleared the (invalid/expired) token on the 401; the
      // template's v-if="!authToken" will fall back to the login screen.
    }
  }
})
onUnmounted(() => {
  stopApp()
})
</script>

<template>
  <a-config-provider :theme="{ token: { colorPrimary: '#1890ff', borderRadius: 4 } }">
    <LoginView v-if="!authToken" @authenticated="onAuthenticated" />
    <a-layout v-else style="height:100vh">

      <!-- Header -->
      <a-layout-header style="display:flex;align-items:center;gap:12px;padding:0 20px;height:48px;line-height:48px;background:#0a0a0a;border-bottom:1px solid rgba(255,255,255,0.08)">
        <IotisticaLogo :size="24" />
        <span style="color:rgba(255,255,255,0.85);font-size:15px;font-weight:600;letter-spacing:.3px">Iotistica</span>
        <span style="color:rgba(255,255,255,0.25);font-size:13px;font-weight:400">BACnet Simulator</span>
        <span :style="{ fontSize:'12px', color: health.bacnet_running ? '#52c41a' : '#ff4d4f', marginLeft: '8px' }">
          {{ health.bacnet_running ? '● running' : '○ stopped' }}
        </span>
        <span style="color:#555;font-size:12px">· {{ health.devices }} device(s)</span>

        <div style="display:flex;align-items:center;gap:2px;margin-left:12px;padding-left:12px;border-left:1px solid rgba(255,255,255,0.08)">
          <a-tooltip title="Start simulation clock">
            <a-button
              size="small" type="text" :disabled="health.sim_running" :loading="simActionLoading"
              @click="simStart"
            >
              <template #icon><PlayCircleOutlined :style="{ color: health.sim_running ? '#555' : '#52c41a' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Pause simulation clock (freezes values in place)">
            <a-button
              size="small" type="text" :disabled="!health.sim_running" :loading="simActionLoading"
              @click="simPause"
            >
              <template #icon><PauseCircleOutlined :style="{ color: !health.sim_running ? '#555' : '#faad14' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Stop simulation clock and rewind to t=0">
            <a-button size="small" type="text" :loading="simActionLoading" @click="simStop">
              <template #icon><StopOutlined :style="{ color: '#ff4d4f' }" /></template>
            </a-button>
          </a-tooltip>
          <span style="color:#555;font-size:11px;margin-left:2px">
            {{ health.sim_running ? 'clock running' : 'clock paused' }}
          </span>
        </div>

        <div style="flex:1" />
        <a-tag v-if="activeProfileName" color="blue" style="margin:0;font-size:11px;cursor:default">{{ activeProfileName }}</a-tag>
        <a-button size="small" @click="newProfile">
          <template #icon><FileAddOutlined /></template>
          New
        </a-button>
        <a-button size="small" type="primary" ghost @click="openSave">Save</a-button>
        <a-button size="small" @click="profilesDrawerOpen = true">Open</a-button>
        <span style="color:#444;font-size:11px;margin-left:4px">:{{ apiPort }}</span>

        <div style="display:flex;align-items:center;gap:4px;margin-left:12px;padding-left:12px;border-left:1px solid rgba(255,255,255,0.08)">
          <span style="color:rgba(255,255,255,0.5);font-size:12px">
            <UserOutlined /> {{ currentUser?.username }}
          </span>
          <a-tooltip title="Manage users">
            <a-button size="small" type="text" @click="usersDrawerOpen = true">
              <template #icon><TeamOutlined :style="{ color: 'rgba(255,255,255,0.5)' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Sign out">
            <a-button size="small" type="text" @click="doLogout">
              <template #icon><LogoutOutlined :style="{ color: 'rgba(255,255,255,0.5)' }" /></template>
            </a-button>
          </a-tooltip>
        </div>
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
              <a-button type="text" size="small" title="Edit" @click.stop="openEditDevice(d)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button type="text" size="small" title="Duplicate" @click.stop="duplicateDevice(d)">
                <template #icon><CopyOutlined /></template>
              </a-button>
              <a-button type="text" size="small" danger title="Delete" @click.stop="deleteDevice(d)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-space>
          </div>
        </a-layout-sider>

        <!-- Content: objects + log -->
        <a-layout-content style="display:flex;flex-direction:column;overflow:hidden">
        <div style="flex:1;overflow:auto;padding:20px">

          <div v-if="!selectedDevice" style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px">
            <ApiOutlined style="font-size:48px;color:#d9d9d9" />
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
              <a-space>
                <a-button :disabled="!objects.length" @click="saveTemplateOpen = true">Save as Template</a-button>
                <a-button @click="templateModalOpen = true">From Template</a-button>
                <a-button type="primary" @click="openAddObject">+ Add Object</a-button>
              </a-space>
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
                  <a-switch
                    size="small"
                    :checked="!!(record as SimObject).enabled"
                    @change="toggleObjectEnabled(record as SimObject)"
                  />
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-space :size="2">
                    <a-button type="link" size="small" @click="openEditObject(record as SimObject)">Edit</a-button>
                    <a-button type="link" size="small" style="color:#8c8c8c" @click="duplicateObject(record as SimObject)">Copy</a-button>
                    <a-button
                      v-if="(record as SimObject).behavior === 'manual'"
                      type="link" size="small"
                      style="color:#fa8c16"
                      @click="openSetValue(record as SimObject)"
                    >Set</a-button>
                    <a-button type="link" size="small" style="color:#722ed1" @click="openHistory(record as SimObject)">
                      <template #icon><LineChartOutlined /></template>
                    </a-button>
                    <a-button type="link" size="small" danger @click="deleteObject(record as SimObject)">Del</a-button>
                  </a-space>
                </template>
              </template>
            </a-table>
          </template>

        </div>
        <DeviceLogPanel />
        </a-layout-content>
      </a-layout>
    </a-layout>

    <!-- Device drawer -->
    <DeviceDrawer
      v-model:open="deviceDrawerOpen"
      :device="editingDevice"
      :existing-instances="devices.map(d => d.device_instance)"
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

    <!-- Profiles drawer -->
    <ProfilesDrawer
      v-model:open="profilesDrawerOpen"
      @loaded="onProfileLoaded"
    />

    <!-- Users drawer -->
    <UsersDrawer v-model:open="usersDrawerOpen" />

    <!-- Save as template -->
    <SaveTemplateModal
      v-model:open="saveTemplateOpen"
      :objects="objects"
      :device-name="selectedDevice?.name"
    />

    <!-- Template picker -->
    <TemplatePickerModal
      v-model:open="templateModalOpen"
      :device-id="selectedDevice?.id"
      :vendor-name="selectedDevice?.vendor_name"
      :model-name="selectedDevice?.model_name"
      @applied="loadObjects"
    />

    <!-- Save profile modal -->
    <a-modal
      v-model:open="saveModalOpen"
      title="Save Profile"
      ok-text="Save"
      :confirm-loading="saveModalLoading"
      :ok-button-props="{ disabled: !saveModalName.trim() }"
      @ok="doSave"
    >
      <a-form layout="vertical" style="margin-top:8px">
        <a-form-item label="Profile Name" required>
          <a-input v-model:value="saveModalName" placeholder="My Profile" @pressEnter="doSave" />
        </a-form-item>
        <a-form-item label="Description">
          <a-input v-model:value="saveModalDesc" placeholder="Optional description" />
        </a-form-item>
      </a-form>
    </a-modal>

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

    <!-- History chart modal -->
    <a-modal
      v-model:open="histModalOpen"
      :title="histObj ? `${histObj.name} — History` : 'History'"
      :footer="null"
      width="680px"
      destroy-on-close
    >
      <div v-if="histLoading" style="text-align:center;padding:40px 0">
        <a-spin />
      </div>
      <template v-else-if="histObj">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <a-tag :color="BEHAVIOR_COLOR[histObj.behavior]">{{ histObj.behavior }}</a-tag>
          <span style="font-size:12px;color:#aaa">{{ histObj.units === 'no-units' ? '' : histObj.units }}</span>
          <span style="font-size:12px;color:#bbb;margin-left:auto">{{ histData.length }} samples</span>
        </div>

        <div v-if="histData.length < 2" style="text-align:center;padding:40px 0;color:#bbb;font-size:13px">
          Not enough data yet — check back after a few ticks (5 s each)
        </div>
        <template v-else>
          <!-- Chart -->
          <div style="border:1px solid #f0f0f0;border-radius:4px;background:#fafafa;overflow:hidden">
            <svg :viewBox="`0 0 ${CHART_W} ${CHART_H}`" style="width:100%;display:block">

              <!-- Y-axis grid lines + labels -->
              <template v-for="tick in histYLabels(histData, histObj)" :key="tick.y">
                <line
                  :x1="CHART_PAD.left" :y1="tick.y"
                  :x2="CHART_W - CHART_PAD.right" :y2="tick.y"
                  stroke="#efefef" stroke-width="1"
                />
                <text
                  :x="CHART_PAD.left - 6" :y="tick.y"
                  text-anchor="end" dominant-baseline="middle"
                  font-size="11" fill="#bbb" font-family="monospace"
                >{{ tick.label }}</text>
              </template>

              <!-- X-axis baseline -->
              <line
                :x1="CHART_PAD.left" :y1="CHART_H - CHART_PAD.bottom"
                :x2="CHART_W - CHART_PAD.right" :y2="CHART_H - CHART_PAD.bottom"
                stroke="#e0e0e0" stroke-width="1"
              />

              <!-- X-axis ticks + labels -->
              <template v-for="tick in histXLabels(histData)" :key="tick.x">
                <line
                  :x1="tick.x" :y1="CHART_H - CHART_PAD.bottom"
                  :x2="tick.x" :y2="CHART_H - CHART_PAD.bottom + 5"
                  stroke="#d0d0d0" stroke-width="1"
                />
                <text
                  :x="tick.x" :y="CHART_H - CHART_PAD.bottom + 17"
                  text-anchor="middle"
                  font-size="11" fill="#bbb" font-family="sans-serif"
                >{{ tick.label }}</text>
              </template>

              <!-- Fill area under line -->
              <polyline
                :points="`${CHART_PAD.left},${CHART_H - CHART_PAD.bottom} ${histSvgPoints(histData)} ${CHART_W - CHART_PAD.right},${CHART_H - CHART_PAD.bottom}`"
                fill="rgba(24,144,255,0.08)"
                stroke="none"
              />
              <!-- Data line -->
              <polyline
                :points="histSvgPoints(histData)"
                fill="none"
                stroke="#1890ff"
                stroke-width="1.5"
                stroke-linejoin="round"
              />
            </svg>
          </div>

          <!-- Stats row -->
          <div style="display:flex;gap:0;margin-top:14px;border:1px solid #f0f0f0;border-radius:4px;overflow:hidden">
            <div v-for="(stat, label) in { Min: histStats(histData).min, Max: histStats(histData).max, Avg: histStats(histData).avg, Current: histStats(histData).current }"
              :key="label"
              style="flex:1;text-align:center;padding:10px 0;border-right:1px solid #f0f0f0"
              :style="label === 'Current' ? 'border-right:none' : ''"
            >
              <div style="font-size:11px;color:#aaa;margin-bottom:2px">{{ label }}</div>
              <div style="font-size:14px;font-weight:600;font-family:monospace;color:#1890ff">
                {{ histFmt(stat, histObj) }}
              </div>
            </div>
          </div>
        </template>
      </template>
    </a-modal>

  </a-config-provider>
</template>
