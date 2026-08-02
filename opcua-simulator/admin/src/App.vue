<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import DeviceDrawer from './components/DeviceDrawer.vue'
import FolderDrawer from './components/FolderDrawer.vue'
import TagDrawer from './components/TagDrawer.vue'
import ProjectsDrawer from './components/ProjectsDrawer.vue'
import NodeSetImportModal from './components/NodeSetImportModal.vue'
import TemplatePickerModal from './components/TemplatePickerModal.vue'
import SaveTemplateModal from './components/SaveTemplateModal.vue'
import IotisticaLogo from './components/IotisticaLogo.vue'
import DeviceLogPanel from './components/DeviceLogPanel.vue'
import LoginView from './components/LoginView.vue'
import UsersDrawer from './components/UsersDrawer.vue'
import AnalyticsDashboard from './components/AnalyticsDashboard.vue'
import AlarmsPanel from './components/AlarmsPanel.vue'
import type { Device, Tag, Meta, Health, HistoryPoint, Folder } from './types'
import { api } from './api'
import { authToken, currentUser, logout } from './auth'
import { EditOutlined, DeleteOutlined, ApiOutlined, CopyOutlined, FileAddOutlined, LineChartOutlined, PlayCircleOutlined, PauseCircleOutlined, StopOutlined, UserOutlined, LogoutOutlined, TeamOutlined, CloudUploadOutlined, DashboardOutlined, ApartmentOutlined, AlertOutlined, FolderOutlined, FolderAddOutlined } from '@ant-design/icons-vue'

const apiPort = window.location.port || '47901'
const activeView = ref<'devices' | 'analytics' | 'alarms'>('devices')

const health  = ref<Health>({ status: 'unknown', opcua_running: false, devices: 0, sim_state: 'stopped', elapsed_seconds: 0 })
const simActionLoading = ref(false)
const SIM_STATE_COLOR: Record<Health['sim_state'], string> = { running: '#52c41a', paused: '#faad14', stopped: '#ff4d4f' }
const SIM_STATE_LABEL: Record<Health['sim_state'], string> = { running: 'Running', paused: 'Paused', stopped: 'Stopped' }
const meta    = ref<Meta>({ data_types: [], behaviors: [] })
const devices = ref<Device[]>([])
const folders = ref<Folder[]>([])
const selectedDevice = ref<Device | null>(null)
const tags = ref<Tag[]>([])
const liveValues = ref<Record<number, number | boolean>>({})

// Drawers
const deviceDrawerOpen  = ref(false)
const editingDevice     = ref<Device | null>(null)
const addDeviceFolderId = ref<number | null>(null)
const folderDrawerOpen  = ref(false)
const editingFolder     = ref<Folder | null>(null)
const tagDrawerOpen     = ref(false)
const editingTag        = ref<Tag | null>(null)
const projectsDrawerOpen   = ref(false)
const templateModalOpen    = ref(false)
const saveTemplateOpen     = ref(false)
const usersDrawerOpen      = ref(false)
const nodeSetImportOpen    = ref(false)

// Active profile state
const activeProfileId   = ref<number | null>(null)
const activeProjectName = ref<string | null>(null)
const activeProjectDesc = ref<string>('')

// Save-profile modal
const saveModalOpen    = ref(false)
const saveModalName    = ref('')
const saveModalDesc    = ref('')
const saveModalLoading = ref(false)

// Set-value modal
const setValOpen    = ref(false)
const setValTag     = ref<Tag | null>(null)
const setValInput   = ref(0)
const setValLoading = ref(false)

// WebSocket
let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null

function wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(authToken.value ?? '')}`)
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data) as { devices?: { tags?: { id: number; value: number | boolean }[] }[] }
    const map: Record<number, number | boolean> = {}
    data.devices?.forEach(d => d.tags?.forEach(t => { map[t.id] = t.value }))
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

function fmtVal(tag: Tag): string {
  const v = liveVal(tag.id)
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
async function loadFolders() {
  try { folders.value = await api.folders.list() } catch { /* swallow */ }
}

// ── Sidebar tree (folders + devices) ────────────────────────────────────────
interface TreeNode {
  key: string
  kind: 'folder' | 'device'
  folder?: Folder
  device?: Device
  children?: TreeNode[]
}

const sidebarTree = computed<TreeNode[]>(() => {
  const folderNodes = new Map<number, TreeNode>()
  for (const f of folders.value) {
    folderNodes.set(f.id, { key: `folder-${f.id}`, kind: 'folder', folder: f, children: [] })
  }
  const roots: TreeNode[] = []
  for (const f of folders.value) {
    const node = folderNodes.get(f.id)!
    const parent = f.parent_folder_id != null ? folderNodes.get(f.parent_folder_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  for (const d of devices.value) {
    const node: TreeNode = { key: `device-${d.id}`, kind: 'device', device: d }
    const parent = d.folder_id != null ? folderNodes.get(d.folder_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  return roots
})
const expandedKeys = ref<string[]>([])
watch(folders, () => { expandedKeys.value = folders.value.map(f => `folder-${f.id}`) }, { immediate: true })

function onTreeSelect(_keys: unknown, info: { node: { dataRef?: TreeNode } & Partial<TreeNode> }) {
  const data = info.node.dataRef ?? (info.node as TreeNode)
  if (data.kind === 'device' && data.device) selectDevice(data.device)
}
async function loadTags() {
  if (!selectedDevice.value) return
  try { tags.value = await api.tags.list(selectedDevice.value.id) } catch { /* swallow */ }
}

function selectDevice(d: Device) {
  selectedDevice.value = d
  loadTags()
}

// Device actions
function openAddDevice() { editingDevice.value = null; addDeviceFolderId.value = null; deviceDrawerOpen.value = true }
function openEditDevice(d: Device) { editingDevice.value = d; deviceDrawerOpen.value = true }
async function onDeviceSaved() { await loadDevices(); await loadHealth() }
async function onNodeSetImported() { await loadDevices(); await loadHealth() }

// Folder actions
function openAddFolder() { editingFolder.value = null; folderDrawerOpen.value = true }
function openEditFolder(f: Folder) { editingFolder.value = f; folderDrawerOpen.value = true }
async function onFolderSaved() { await loadFolders(); await loadDevices() }
async function toggleFolderEnabled(f: Folder, enabled: boolean) {
  try {
    await api.folders.setEnabled(f.id, enabled)
    await loadFolders()
    await loadHealth()
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
}
function deleteFolder(f: Folder) {
  Modal.confirm({
    title: `Delete "${f.name}"?`,
    content: 'A folder can only be deleted once it has no sub-folders or devices left in it.',
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      try {
        await api.folders.del(f.id)
        await loadFolders()
        message.success('Folder deleted')
      } catch (e: unknown) {
        message.error((e as Error).message)
      }
    },
  })
}
async function duplicateDevice(d: Device) {
  try {
    const created = await api.devices.create({
      name:         `${d.name} Copy`,
      description:  d.description,
      manufacturer: d.manufacturer,
      model:        d.model,
      enabled:      d.enabled,
      folder_id:    d.folder_id,
    })
    const srcTags = await api.tags.list(d.id)
    for (const t of srcTags) {
      await api.tags.create(created.id, {
        data_type:       t.data_type,
        writable:        t.writable,
        name:            t.name,
        unit:            t.unit,
        behavior:        t.behavior,
        behavior_params: t.behavior_params,
        enabled:         t.enabled,
      })
    }
    await loadDevices()
    await loadHealth()
    message.success(`Duplicated "${d.name}" with ${srcTags.length} tag${srcTags.length !== 1 ? 's' : ''}`)
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
}
function deleteDevice(d: Device) {
  Modal.confirm({
    title: `Delete "${d.name}"?`,
    content: 'This also deletes all its tags and cannot be undone.',
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      await api.devices.del(d.id)
      if (selectedDevice.value?.id === d.id) { selectedDevice.value = null; tags.value = [] }
      await loadDevices(); await loadHealth()
      message.success('Device deleted')
    },
  })
}

// Project actions
function newProject() {
  Modal.confirm({
    title: 'Start a new project?',
    content: 'Save the current setup as a project first if you want to keep it.',
    okText: 'Start Fresh',
    okType: 'danger',
    async onOk() {
      await Promise.allSettled(devices.value.map(d => api.devices.del(d.id)))
      // Folders can only be deleted once empty — devices are already gone
      // above, so repeatedly sweep leaf folders (innermost first) until
      // nothing more can be removed.
      let remaining = [...folders.value]
      while (remaining.length) {
        const results = await Promise.allSettled(remaining.map(f => api.folders.del(f.id)))
        const stillThere = remaining.filter((_, i) => results[i].status === 'rejected')
        if (stillThere.length === remaining.length) break // safety: nothing progressed
        remaining = stillThere
      }
      selectedDevice.value = null
      tags.value = []
      activeProfileId.value = null
      activeProjectName.value = null
      activeProjectDesc.value = ''
      await loadDevices()
      await loadFolders()
      await loadHealth()
      message.success('Ready — add your first device')
    },
  })
}

function openSaveAs() {
  saveModalName.value = activeProjectName.value ? `${activeProjectName.value} (copy)` : ''
  saveModalDesc.value = activeProjectDesc.value
  saveModalOpen.value = true
}

function openSave() {
  if (activeProfileId.value !== null) {
    // Overwrite existing profile directly — no dialog
    Modal.confirm({
      title: `Save to "${activeProjectName.value}"?`,
      okText: 'Save',
      async onOk() {
        try {
          await api.projects.update(activeProfileId.value!, activeProjectName.value!, activeProjectDesc.value)
          message.success(`"${activeProjectName.value}" saved`)
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
    const profile = await api.projects.save(saveModalName.value.trim(), saveModalDesc.value.trim())
    activeProfileId.value = profile.id
    activeProjectName.value = profile.name
    activeProjectDesc.value = profile.description
    saveModalOpen.value = false
    message.success(`"${profile.name}" saved`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save')
  } finally {
    saveModalLoading.value = false
  }
}

async function onProjectLoaded(id: number, name: string, desc: string) {
  activeProfileId.value = id
  activeProjectName.value = name
  activeProjectDesc.value = desc
  await loadDevices()
  selectedDevice.value = null
  tags.value = []
  await loadHealth()
}

// Tag actions
function openAddTag() { editingTag.value = null; tagDrawerOpen.value = true }
function openEditTag(tag: Tag) { editingTag.value = tag; tagDrawerOpen.value = true }
async function onTagSaved() { await loadTags() }
async function duplicateTag(tag: Tag) {
  if (!selectedDevice.value) return
  try {
    await api.tags.create(selectedDevice.value.id, {
      data_type:       tag.data_type,
      writable:        tag.writable,
      name:            `${tag.name} Copy`,
      unit:            tag.unit,
      behavior:        tag.behavior,
      behavior_params: tag.behavior_params,
      enabled:         tag.enabled,
    })
    await loadTags()
    message.success(`Duplicated "${tag.name}"`)
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
}
function deleteTag(tag: Tag) {
  Modal.confirm({
    title: `Delete "${tag.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      await api.tags.del(selectedDevice.value!.id, tag.id)
      await loadTags()
      message.success('Tag deleted')
    },
  })
}
async function toggleTagEnabled(tag: Tag) {
  const nextEnabled = tag.enabled ? 0 : 1
  try {
    await api.tags.update(selectedDevice.value!.id, tag.id, {
      data_type:       tag.data_type,
      writable:        tag.writable,
      name:            tag.name,
      unit:            tag.unit,
      behavior:        tag.behavior,
      behavior_params: tag.behavior_params,
      enabled:         nextEnabled,
    })
    tag.enabled = nextEnabled
  } catch (e) {
    message.error((e as Error).message || 'Failed to toggle tag')
  }
}

// History chart
const histModalOpen   = ref(false)
const histTag         = ref<Tag | null>(null)
const histData        = ref<HistoryPoint[]>([])
const histLoading     = ref(false)

async function openHistory(tag: Tag) {
  if (!selectedDevice.value) return
  histTag.value = tag
  histData.value = []
  histLoading.value = true
  histModalOpen.value = true
  try {
    histData.value = await api.tags.history(selectedDevice.value.id, tag.id)
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

function histFmt(v: number, tag: Tag | null): string {
  if (!tag) return v.toFixed(2)
  const isBoolean = tag.data_type === 'Boolean'
  if (isBoolean) return v >= 0.5 ? 'ON' : 'OFF'
  return v.toFixed(2)
}

function histYLabels(data: HistoryPoint[], tag: Tag | null) {
  if (data.length < 2) return []
  const vals = data.map(p => p.value)
  let minV = Math.min(...vals), maxV = Math.max(...vals)
  if (minV === maxV) { minV -= 1; maxV += 1 }
  const h = CHART_H - CHART_PAD.top - CHART_PAD.bottom
  return [
    { y: CHART_PAD.top,           v: maxV },
    { y: CHART_PAD.top + h / 2,   v: (minV + maxV) / 2 },
    { y: CHART_PAD.top + h,       v: minV },
  ].map(t => ({ y: t.y, label: histFmt(t.v, tag) }))
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
function openSetValue(tag: Tag) {
  setValTag.value = tag
  setValInput.value = Number(liveVal(tag.id) ?? 0)
  setValOpen.value = true
}
async function doSetValue() {
  if (!setValTag.value || !selectedDevice.value) return
  setValLoading.value = true
  try {
    await api.tags.setValue(selectedDevice.value.id, setValTag.value.id, setValInput.value)
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
  { title: 'Name',       dataIndex: 'name',      key: 'name' },
  { title: 'Data Type',  key: 'type',            width: 100 },
  { title: 'Writable',   key: 'writable',        width: 80  },
  { title: 'Behavior',   key: 'behavior',        width: 120 },
  { title: 'Unit',       dataIndex: 'unit',       key: 'unit',    width: 100 },
  { title: 'Live Value', key: 'value',           width: 110 },
  { title: 'On',         key: 'enabled',         width: 50  },
  { title: '',           key: 'actions',         width: 200 },
]

// Lifecycle — gated behind auth: protected endpoints 401 until logged in
let healthTimer: ReturnType<typeof setInterval>

async function startApp() {
  await Promise.all([loadMeta(), loadDevices(), loadFolders(), loadHealth()])
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
        <span style="color:rgba(255,255,255,0.25);font-size:13px;font-weight:400">OPC UA Simulator</span>
        <span style="color:#555;font-size:12px;margin-left:8px">{{ health.devices }} device(s)</span>

        <div style="display:flex;align-items:center;gap:2px;margin-left:12px;padding-left:12px;border-left:1px solid rgba(255,255,255,0.08)">
          <a-tooltip title="Start simulation clock">
            <a-button
              size="small" type="text" :disabled="health.sim_state === 'running'" :loading="simActionLoading"
              @click="simStart"
            >
              <template #icon><PlayCircleOutlined :style="{ color: health.sim_state === 'running' ? '#555' : '#52c41a' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Pause simulation clock (freezes values in place)">
            <a-button
              size="small" type="text" :disabled="health.sim_state !== 'running'" :loading="simActionLoading"
              @click="simPause"
            >
              <template #icon><PauseCircleOutlined :style="{ color: health.sim_state !== 'running' ? '#555' : '#faad14' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Stop simulation clock and rewind to t=0">
            <a-button
              size="small" type="text" :disabled="health.sim_state === 'stopped'" :loading="simActionLoading"
              @click="simStop"
            >
              <template #icon><StopOutlined :style="{ color: health.sim_state === 'stopped' ? '#555' : '#ff4d4f' }" /></template>
            </a-button>
          </a-tooltip>
          <span :style="{ fontSize:'11px', marginLeft:'2px', color: SIM_STATE_COLOR[health.sim_state] }">
            {{ SIM_STATE_LABEL[health.sim_state] }}
          </span>
        </div>

        <a-radio-group v-model:value="activeView" button-style="solid" size="small" style="margin-left:8px">
          <a-radio-button value="devices"><ApartmentOutlined /> Devices</a-radio-button>
          <a-radio-button value="analytics"><DashboardOutlined /> Analytics</a-radio-button>
          <a-radio-button value="alarms"><AlertOutlined /> Alarms</a-radio-button>
        </a-radio-group>

        <div style="flex:1" />
        <a-tag v-if="activeProjectName" color="blue" style="margin:0;font-size:11px;cursor:default">{{ activeProjectName }}</a-tag>
        <a-button size="small" @click="newProject">
          <template #icon><FileAddOutlined /></template>
          New Project
        </a-button>
        <a-button size="small" type="primary" ghost @click="openSave">Save</a-button>
        <a-button v-if="activeProfileId !== null" size="small" @click="openSaveAs">Save As</a-button>
        <a-button size="small" @click="projectsDrawerOpen = true">Open project</a-button>
        <a-button size="small" @click="nodeSetImportOpen = true">
          <template #icon><CloudUploadOutlined /></template>
          Import NodeSet
        </a-button>
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

      <a-layout v-if="activeView === 'devices'">

        <!-- Sidebar: folders + devices -->
        <a-layout-sider :width="280" style="background:white;border-right:1px solid #e8e8e8;overflow:auto">
          <div style="padding:10px 12px 10px 16px;border-bottom:1px solid #e8e8e8;display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.8px">Devices</span>
            <a-space :size="4">
              <a-button size="small" title="Add Folder" @click="openAddFolder">
                <template #icon><FolderAddOutlined /></template>
              </a-button>
              <a-button size="small" type="primary" @click="openAddDevice">+ Add</a-button>
            </a-space>
          </div>

          <div v-if="!devices.length && !folders.length" style="padding:24px 16px;color:#bbb;text-align:center;font-size:13px">
            No devices yet
          </div>

          <a-tree
            v-else
            v-model:expanded-keys="expandedKeys"
            :tree-data="sidebarTree"
            :selected-keys="selectedDevice ? [`device-${selectedDevice.id}`] : []"
            :field-names="{ children: 'children', title: 'key', key: 'key' }"
            block-node
            style="padding:6px 4px"
            @select="onTreeSelect"
          >
            <template #title="node">
              <!-- Folder row -->
              <div v-if="node.kind === 'folder'" style="display:flex;align-items:center;gap:6px;padding:2px 0">
                <FolderOutlined :style="{ color: node.folder.enabled ? '#1890ff' : '#ccc' }" />
                <span
                  style="flex:1;min-width:0;font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
                  :style="{ color: node.folder.enabled ? 'inherit' : '#bbb' }"
                >{{ node.folder.name }}</span>
                <a-space :size="0" @click.stop>
                  <a-switch
                    size="small"
                    :checked="!!node.folder.enabled"
                    @change="(v: boolean) => toggleFolderEnabled(node.folder, v)"
                  />
                  <a-button type="text" size="small" title="Edit" @click="openEditFolder(node.folder)">
                    <template #icon><EditOutlined /></template>
                  </a-button>
                  <a-button type="text" size="small" danger title="Delete" @click="deleteFolder(node.folder)">
                    <template #icon><DeleteOutlined /></template>
                  </a-button>
                </a-space>
              </div>

              <!-- Device row -->
              <div v-else style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <a-badge :status="node.device.enabled ? 'success' : 'default'" />
                <div style="flex:1;min-width:0">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ node.device.name }}</div>
                  <div v-if="node.device.model" style="font-size:11px;color:#aaa">{{ node.device.model }}</div>
                </div>
                <a-space :size="0" @click.stop>
                  <a-button type="text" size="small" title="Edit" @click="openEditDevice(node.device)">
                    <template #icon><EditOutlined /></template>
                  </a-button>
                  <a-button type="text" size="small" title="Duplicate" @click="duplicateDevice(node.device)">
                    <template #icon><CopyOutlined /></template>
                  </a-button>
                  <a-button type="text" size="small" danger title="Delete" @click="deleteDevice(node.device)">
                    <template #icon><DeleteOutlined /></template>
                  </a-button>
                </a-space>
              </div>
            </template>
          </a-tree>
        </a-layout-sider>

        <!-- Content: tags + log -->
        <a-layout-content style="display:flex;flex-direction:column;overflow:hidden">
        <div style="flex:1;overflow:auto;padding:20px">

          <div v-if="!selectedDevice" style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px">
            <ApiOutlined style="font-size:48px;color:#d9d9d9" />
            <span style="font-size:15px;color:#bbb">Select a device to manage its tags</span>
          </div>

          <template v-else>
            <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px">
              <div>
                <div style="font-size:18px;font-weight:600">{{ selectedDevice.name }}</div>
                <div style="font-size:12px;color:#aaa;margin-top:3px">
                  <template v-if="selectedDevice.description">{{ selectedDevice.description }}</template>
                  <template v-else-if="selectedDevice.model">{{ selectedDevice.model }}</template>
                </div>
              </div>
              <a-space>
                <a-button :disabled="!tags.length" @click="saveTemplateOpen = true">Save as Template</a-button>
                <a-button @click="templateModalOpen = true">From Template</a-button>
                <a-button type="primary" @click="openAddTag">+ Add Tag</a-button>
              </a-space>
            </div>

            <a-table
              :data-source="tags"
              :columns="columns"
              :pagination="false"
              size="small"
              row-key="id"
              :locale="{ emptyText: 'No tags yet — click Add Tag' }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'type'">
                  <a-tag style="font-family:monospace;font-size:11px">{{ (record as Tag).data_type }}</a-tag>
                </template>
                <template v-else-if="column.key === 'writable'">
                  <span style="font-size:12px;color:#aaa">{{ (record as Tag).writable ? 'Yes' : '—' }}</span>
                </template>
                <template v-else-if="column.key === 'behavior'">
                  <a-tag :color="BEHAVIOR_COLOR[(record as Tag).behavior]">{{ (record as Tag).behavior }}</a-tag>
                </template>
                <template v-else-if="column.key === 'unit'">
                  <span style="color:#aaa;font-size:12px">{{ (record as Tag).unit || '—' }}</span>
                </template>
                <template v-else-if="column.key === 'value'">
                  <span :style="{ fontFamily:'monospace', color: hasLive((record as Tag).id) ? '#1890ff' : '#ccc' }">
                    {{ fmtVal(record as Tag) }}
                  </span>
                </template>
                <template v-else-if="column.key === 'enabled'">
                  <a-switch
                    size="small"
                    :checked="!!(record as Tag).enabled"
                    @change="toggleTagEnabled(record as Tag)"
                  />
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-space :size="2">
                    <a-button type="link" size="small" @click="openEditTag(record as Tag)">Edit</a-button>
                    <a-button type="link" size="small" style="color:#8c8c8c" @click="duplicateTag(record as Tag)">Copy</a-button>
                    <a-button
                      v-if="(record as Tag).behavior === 'manual'"
                      type="link" size="small"
                      style="color:#fa8c16"
                      @click="openSetValue(record as Tag)"
                    >Set</a-button>
                    <a-button type="link" size="small" style="color:#722ed1" @click="openHistory(record as Tag)">
                      <template #icon><LineChartOutlined /></template>
                    </a-button>
                    <a-button type="link" size="small" danger @click="deleteTag(record as Tag)">Del</a-button>
                  </a-space>
                </template>
              </template>
            </a-table>
          </template>

        </div>
        <DeviceLogPanel />
        </a-layout-content>
      </a-layout>

      <AnalyticsDashboard v-else-if="activeView === 'analytics'" style="flex:auto;min-height:0" />
      <AlarmsPanel v-else-if="activeView === 'alarms'" style="flex:auto;min-height:0" />
    </a-layout>

    <!-- Device drawer -->
    <DeviceDrawer
      v-model:open="deviceDrawerOpen"
      :device="editingDevice"
      :folders="folders"
      :pre-selected-folder-id="addDeviceFolderId"
      @saved="onDeviceSaved"
    />

    <!-- Folder drawer -->
    <FolderDrawer
      v-model:open="folderDrawerOpen"
      :folder="editingFolder"
      :folders="folders"
      @saved="onFolderSaved"
    />

    <!-- Tag drawer -->
    <TagDrawer
      v-model:open="tagDrawerOpen"
      :tag="editingTag"
      :device-id="selectedDevice?.id"
      :meta="meta"
      @saved="onTagSaved"
    />

    <!-- Projects drawer -->
    <ProjectsDrawer
      v-model:open="projectsDrawerOpen"
      @loaded="onProjectLoaded"
    />

    <!-- Users drawer -->
    <UsersDrawer v-model:open="usersDrawerOpen" />

    <!-- NodeSet2 XML import -->
    <NodeSetImportModal
      v-model:open="nodeSetImportOpen"
      @imported="onNodeSetImported"
    />

    <!-- Save as template -->
    <SaveTemplateModal
      v-model:open="saveTemplateOpen"
      :tags="tags"
      :device-name="selectedDevice?.name"
    />

    <!-- Template picker -->
    <TemplatePickerModal
      v-model:open="templateModalOpen"
      :device-id="selectedDevice?.id"
      :vendor-name="selectedDevice?.manufacturer"
      :model-name="selectedDevice?.model"
      @applied="loadTags"
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
      :title="`Set Value — ${setValTag?.name}`"
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
      :title="histTag ? `${histTag.name} — History` : 'History'"
      :footer="null"
      width="680px"
      destroy-on-close
    >
      <div v-if="histLoading" style="text-align:center;padding:40px 0">
        <a-spin />
      </div>
      <template v-else-if="histTag">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <a-tag :color="BEHAVIOR_COLOR[histTag.behavior]">{{ histTag.behavior }}</a-tag>
          <span style="font-size:12px;color:#aaa">{{ histTag.unit }}</span>
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
              <template v-for="tick in histYLabels(histData, histTag)" :key="tick.y">
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
                {{ histFmt(stat, histTag) }}
              </div>
            </div>
          </div>
        </template>
      </template>
    </a-modal>

  </a-config-provider>
</template>
