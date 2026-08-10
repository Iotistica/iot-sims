<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import DeviceDrawer from './components/DeviceDrawer.vue'
import LocationDrawer from './components/LocationDrawer.vue'
import ProjectsDrawer from './components/ProjectsDrawer.vue'
import NewProjectModal from './components/NewProjectModal.vue'
import ObjectsPanel from './components/ObjectsPanel.vue'
import CreateSimulatedCopyModal from './components/CreateSimulatedCopyModal.vue'
import IotisticaLogo from './components/IotisticaLogo.vue'
import DeviceLogPanel from './components/DeviceLogPanel.vue'
import LoginView from './components/LoginView.vue'
import AnalyticsDashboard from './components/AnalyticsDashboard.vue'
import UtilitiesDashboard from './components/UtilitiesDashboard.vue'
import AlarmsPanel from './components/AlarmsPanel.vue'
import SettingsView from './components/SettingsView.vue'
import PacketCapturePanel from './components/PacketCapturePanel.vue'
import SemanticPanel from './components/SemanticPanel.vue'
import NotificationClassDrawer from './components/NotificationClassDrawer.vue'
import EventEnrollmentDrawer from './components/EventEnrollmentDrawer.vue'
import TrendLogDrawer from './components/TrendLogDrawer.vue'
import ScheduleDrawer from './components/ScheduleDrawer.vue'
import CalendarDrawer from './components/CalendarDrawer.vue'
import EnergyModelDrawer from './components/EnergyModelDrawer.vue'

import type { Device, Meta, Health, Location, Project, ProjectSourceType, BACnetConnectionConfig } from './types'
import { api, projectDirty } from './api'
import { authToken, currentUser, logout } from './auth'
import { isDark, toggleDark, themeConfig } from './theme'
import { copyDeviceAndObjects } from './deviceCopy'
import { ClusterOutlined, EditOutlined, ApiOutlined, CopyOutlined, FileAddOutlined, LineChartOutlined, PlayCircleOutlined, PauseCircleOutlined, StopOutlined, UserOutlined, LogoutOutlined, DashboardOutlined, ApartmentOutlined, EllipsisOutlined, DownloadOutlined, UploadOutlined, SearchOutlined, AlertOutlined, CalendarOutlined, ScheduleOutlined, BulbOutlined, SettingOutlined, FolderOutlined, FolderAddOutlined, PartitionOutlined, ThunderboltOutlined, DeleteOutlined } from '@ant-design/icons-vue'

const activeView = ref<
  'devices' |
  'bacnet' |
  'alarms' |
  'packet-capture' |
  'settings' |
  'utility' |
  'semantic'
>('devices')

const health  = ref<Health>({ status: 'unknown', bacnet_running: false, devices: 0, sim_state: 'stopped', elapsed_seconds: 0 })
const simActionLoading = ref(false)
const SIM_STATE_COLOR: Record<Health['sim_state'], string> = { running: '#52c41a', paused: '#faad14', stopped: '#ff4d4f' }
const SIM_STATE_LABEL: Record<Health['sim_state'], string> = { running: 'Running', paused: 'Paused', stopped: 'Stopped' }
const meta    = ref<Meta>({ object_types: [], behaviors: [], units: [], reliability_options: [], polarity_options: [], segmentation_options: [], brick_version: '', equipment_types: [], point_types: [], location_kinds: [], semantic_predicates: [], energy_model_types: [], network_address: null })
const devices = ref<Device[]>([])
const locations = ref<Location[]>([])
const deviceSearch = ref('')
const filteredDevices = computed(() => {
  const q = deviceSearch.value.trim().toLowerCase()
  if (!q) return devices.value
  return devices.value.filter(d =>
    d.name.toLowerCase().includes(q) ||
    String(d.device_instance).includes(q) ||
    d.vendor_name.toLowerCase().includes(q) ||
    d.model_name.toLowerCase().includes(q)
  )
})

// External-BACnet projects: "Discover Devices"/"Rediscover" persists
// results as real Device rows (source_type='external-bacnet') via
// api.discovery.sync(), so they flow through the exact same
// devices/filteredDevices/sidebarTree path simulated devices already use —
// no separate tree structure needed. These two refs are just local button
// UI state (loading spinner, last-run error), not a device list.
const externalSyncLoading = ref(false)
const externalSyncError = ref<string | null>(null)

async function runDiscovery() {
  externalSyncLoading.value = true
  externalSyncError.value = null
  try {
    const result = await api.discovery.sync(activeProjectConnectionConfig.value ?? {
      discovery_target: null, device_instance_low: 0, device_instance_high: 4194303, timeout_ms: 5000,
    })
    message.success(result.devices.length
      ? `Discovered ${result.devices.length} device${result.devices.length !== 1 ? 's' : ''}`
      : 'No devices found')
    await loadDevices()
    await loadHealth()
  } catch (e: unknown) {
    externalSyncError.value = (e as Error).message ?? 'Discovery failed'
  } finally {
    externalSyncLoading.value = false
  }
}

interface SidebarTreeNode {
  key: string
  kind: 'location' | 'device'
  location?: Location
  device?: Device
  children?: SidebarTreeNode[]
}

// While searching, fall back to today's flat filtered list (ignoring
// locations entirely) rather than pruning empty location branches — keeps
// the existing search UX exactly as-is.
const sidebarTree = computed<SidebarTreeNode[]>(() => {
  if (deviceSearch.value.trim()) {
    return filteredDevices.value.map(d => ({ key: `device-${d.id}`, kind: 'device' as const, device: d }))
  }
  const locationNodes = new Map<number, SidebarTreeNode>()
  for (const l of locations.value) {
    locationNodes.set(l.id, { key: `location-${l.id}`, kind: 'location', location: l, children: [] })
  }
  const roots: SidebarTreeNode[] = []
  for (const l of locations.value) {
    const node = locationNodes.get(l.id)!
    const parent = l.parent_location_id != null ? locationNodes.get(l.parent_location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  for (const d of filteredDevices.value) {
    const node: SidebarTreeNode = { key: `device-${d.id}`, kind: 'device', device: d }
    const parent = d.location_id != null ? locationNodes.get(d.location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  return roots
})
const sidebarRawCount = computed(() => devices.value.length)
const sidebarFilteredCount = computed(() => filteredDevices.value.length)
const hasExternalDevices = computed(() => devices.value.some(d => d.source_type === 'external-bacnet'))

const expandedKeys = ref<string[]>([])
watch(locations, () => { expandedKeys.value = locations.value.map(l => `location-${l.id}`) }, { immediate: true })

function onTreeSelect(_keys: unknown, info: { node: { dataRef?: SidebarTreeNode } & Partial<SidebarTreeNode> }) {
  const data = info.node.dataRef ?? (info.node as SidebarTreeNode)
  if (data.kind === 'device' && data.device) selectDevice(data.device)
}

const selectedDevice = ref<Device | null>(null)
const objectsPanelRef = ref<{ reload: () => void } | null>(null)
const liveValues = ref<Record<number, number | boolean>>({})

// Drawers
const deviceDrawerOpen  = ref(false)
const editingDevice     = ref<Device | null>(null)
const locationDrawerOpen = ref(false)
const editingLocation    = ref<Location | null>(null)
const projectsDrawerOpen   = ref(false)
const createCopyModalOpen  = ref(false)
const createCopySource     = ref<Device | null>(null)

// Active project state — persisted to localStorage so a page reload (e.g.
// after a frontend rebuild) doesn't lose track of which project "Save"
// should overwrite.
const ACTIVE_PROJECT_KEY = 'bacnet-sim-active-project'
const activeProjectId   = ref<number | null>(null)
const activeProjectName = ref<string | null>(null)
const activeProjectDesc = ref<string>('')
const activeProjectSourceType = ref<ProjectSourceType>('simulated')
const activeProjectConnectionConfig = ref<BACnetConnectionConfig | null>(null)

function loadActiveProjectFromStorage() {
  try {
    const raw = localStorage.getItem(ACTIVE_PROJECT_KEY)
    if (!raw) return
    const saved = JSON.parse(raw) as {
      id: number; name: string; desc: string
      sourceType?: ProjectSourceType; connectionConfig?: BACnetConnectionConfig | null
    }
    activeProjectId.value = saved.id
    activeProjectName.value = saved.name
    activeProjectDesc.value = saved.desc
    // Older stored entries (pre-source_type) have neither field — default to
    // 'simulated' so existing projects keep behaving exactly as before.
    activeProjectSourceType.value = saved.sourceType ?? 'simulated'
    activeProjectConnectionConfig.value = saved.connectionConfig ?? null
  } catch {
    // Malformed/stale storage — ignore and fall back to "no active project"
  }
}

watch([activeProjectId, activeProjectName, activeProjectDesc, activeProjectSourceType, activeProjectConnectionConfig], () => {
  if (activeProjectId.value === null) {
    localStorage.removeItem(ACTIVE_PROJECT_KEY)
  } else {
    localStorage.setItem(ACTIVE_PROJECT_KEY, JSON.stringify({
      id: activeProjectId.value,
      name: activeProjectName.value,
      desc: activeProjectDesc.value,
      sourceType: activeProjectSourceType.value,
      connectionConfig: activeProjectConnectionConfig.value,
    }))
  }
}, { deep: true })

// Save-project modal
const saveModalOpen    = ref(false)
const saveModalName    = ref('')
const saveModalDesc    = ref('')
const saveModalLoading = ref(false)

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
async function loadLocations() {
  try { locations.value = await api.locations.list() } catch { /* swallow */ }
}

function selectDevice(d: Device) {
  selectedDevice.value = d
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
    const srcObjects = await api.objects.list(d.id)
    const { objectCount } = await copyDeviceAndObjects(d, srcObjects, { name: `${d.name} Copy`, deviceInstance: nextInstance })
    await loadDevices()
    await loadHealth()
    message.success(`Duplicated "${d.name}" with ${objectCount} object${objectCount !== 1 ? 's' : ''}`)
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
}

// External devices — "Create Simulated Copy" (tree dropdown) and "Remove
// from Project" (also available from DeviceDrawer's Edit form).
function openCreateSimulatedCopy(d: Device) {
  createCopySource.value = d
  createCopyModalOpen.value = true
}
async function onSimulatedCopyCreated() {
  await loadDevices()
  await loadHealth()
}
function removeExternalDevice(d: Device) {
  Modal.confirm({
    title: `Remove "${d.name}" from this project?`,
    content: 'Removes this device and its discovered objects from the project inventory. The physical device on the network is unaffected.',
    okType: 'danger',
    okText: 'Remove',
    onOk: async () => {
      try {
        await api.devices.del(d.id)
        if (selectedDevice.value?.id === d.id) selectedDevice.value = null
        await loadDevices()
        await loadHealth()
        message.success('Device removed from project')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to remove device')
      }
    },
  })
}
// Location actions
function openAddLocation() { editingLocation.value = null; locationDrawerOpen.value = true }
function openEditLocation(l: Location) { editingLocation.value = l; locationDrawerOpen.value = true }

async function exportDeviceEde(d: Device) {
  try {
    await api.devices.exportEde(d.id, d.name)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Export failed')
  }
}

async function exportDeviceBrick(d: Device) {
  try {
    await api.devices.exportBrick(d.id, d.name)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Export failed')
  }
}

const edeImportTarget = ref<Device | null>(null)
const edeImportInput = ref<HTMLInputElement>()

const notificationClassDrawerOpen = ref(false)
const notificationClassDevice = ref<Device | null>(null)
function openNotificationClasses(d: Device) {
  notificationClassDevice.value = d
  notificationClassDrawerOpen.value = true
}

const eventEnrollmentDrawerOpen = ref(false)
const eventEnrollmentDevice = ref<Device | null>(null)
function openEventEnrollments(d: Device) {
  eventEnrollmentDevice.value = d
  eventEnrollmentDrawerOpen.value = true
}

const trendLogDrawerOpen = ref(false)
const trendLogDevice = ref<Device | null>(null)
function openTrendLogs(d: Device) {
  trendLogDevice.value = d
  trendLogDrawerOpen.value = true
}

const scheduleDrawerOpen = ref(false)
const scheduleDevice = ref<Device | null>(null)
function openSchedules(d: Device) {
  scheduleDevice.value = d
  scheduleDrawerOpen.value = true
}

const calendarDrawerOpen = ref(false)
const calendarDevice = ref<Device | null>(null)
function openCalendars(d: Device) {
  calendarDevice.value = d
  calendarDrawerOpen.value = true
}

const energyModelDrawerOpen = ref(false)
const energyModelDevice = ref<Device | null>(null)
function openEnergyModel(d: Device) {
  energyModelDevice.value = d
  energyModelDrawerOpen.value = true
}

const packetCaptureDeviceFilter = ref<number | null>(null)
function viewTraffic(d: Device) {
  packetCaptureDeviceFilter.value = d.id
  activeView.value = 'packet-capture'
}

function importDeviceEde(d: Device) {
  Modal.confirm({
    title: `Import EDE into "${d.name}"?`,
    content: 'Points in the file that match an existing point here by object type + instance will be overwritten; others will be added. If the file covers more than one device, use project-level EDE import instead — it creates each device separately.',
    okText: 'Choose file…',
    onOk() {
      edeImportTarget.value = d
      edeImportInput.value?.click()
    },
  })
}

async function onEdeImportFileChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  const target = edeImportTarget.value
  ;(e.target as HTMLInputElement).value = ''
  if (!file || !target) return
  try {
    const result = await api.devices.importEde(target.id, file)
    message.success(`${result.objects_imported} object${result.objects_imported !== 1 ? 's' : ''} imported into "${target.name}"`)
    if (selectedDevice.value?.id === target.id) objectsPanelRef.value?.reload()
  } catch (e2: unknown) {
    message.error((e2 as Error).message ?? 'Import failed')
  }
}

// Project actions
async function resetToNewProject(silent = false) {
  // Reuses the server's own load_project() wipe sequence (semantic
  // relationships/entities, then devices, then locations, in that order --
  // see clear_live_project's docstring) instead of looping individual
  // per-row deletes. That per-row approach used to leave locations
  // (and their parents) permanently stuck: delete_location() refuses to
  // remove a location a leftover semantic entity still points at, and nothing
  // in a device-only delete loop ever touched semantic tables at all.
  await api.projects.clear()
  selectedDevice.value = null
  activeProjectId.value = null
  activeProjectName.value = null
  activeProjectDesc.value = ''
  activeProjectSourceType.value = 'simulated'
  activeProjectConnectionConfig.value = null
  projectDirty.value = false
  await loadDevices()
  await loadLocations()
  await loadHealth()
  // NewProjectModal's Create flow calls this first and shows its own,
  // more specific "<name>" created" message right after — skip the
  // generic one there to avoid two toasts for one action.
  if (!silent) message.success('Ready — add your first device')
}

const newProjectModalOpen = ref(false)

function onNewProjectClick() {
  if (!projectDirty.value) {
    // Nothing unsaved to lose — go straight to the modal.
    newProjectModalOpen.value = true
    return
  }
  Modal.confirm({
    title: 'Start a new project?',
    content: 'Save the current setup as a project first if you want to keep it.',
    okText: 'Start Fresh',
    okType: 'danger',
    onOk: () => { newProjectModalOpen.value = true },
  })
}

function onNewProjectCreated(project: Project) {
  activeProjectId.value = project.id
  activeProjectName.value = project.name
  activeProjectDesc.value = project.description
  activeProjectSourceType.value = project.source_type ?? 'simulated'
  activeProjectConnectionConfig.value = project.connection_config ?? null
}

function openSaveAs() {
  saveModalName.value = activeProjectName.value ? `${activeProjectName.value} (copy)` : ''
  saveModalDesc.value = activeProjectDesc.value
  saveModalOpen.value = true
}

async function openSave() {
  if (activeProjectId.value !== null) {
    // Overwrite existing project directly — no dialog
    try {
      await api.projects.update(activeProjectId.value, activeProjectName.value!, activeProjectDesc.value)
      projectDirty.value = false
      message.success(`"${activeProjectName.value}" saved`)
    } catch (e: unknown) {
      message.error((e as Error).message ?? 'Failed to save')
    }
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
    // Save As duplicates the current project's identity too — an External
    // BACnet project's copy should still be External BACnet with the same
    // connection config, not silently downgrade to a blank Simulated one.
    const project = await api.projects.save(
      saveModalName.value.trim(),
      saveModalDesc.value.trim(),
      activeProjectSourceType.value,
      activeProjectConnectionConfig.value,
    )
    activeProjectId.value = project.id
    activeProjectName.value = project.name
    activeProjectDesc.value = project.description
    activeProjectSourceType.value = project.source_type ?? 'simulated'
    activeProjectConnectionConfig.value = project.connection_config ?? null
    saveModalOpen.value = false
    projectDirty.value = false
    message.success(`"${project.name}" saved`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save')
  } finally {
    saveModalLoading.value = false
  }
}

async function onProjectLoaded(
  id: number, name: string, desc: string,
  sourceType: ProjectSourceType, connectionConfig: BACnetConnectionConfig | null,
) {
  activeProjectId.value = id
  activeProjectName.value = name
  activeProjectDesc.value = desc
  activeProjectSourceType.value = sourceType
  activeProjectConnectionConfig.value = connectionConfig
  projectDirty.value = false
  await loadDevices()
  await loadLocations()
  selectedDevice.value = null
  await loadHealth()
}

const equipmentTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of meta.value.equipment_types) map[o.value] = o.label
  return map
})

// Lifecycle — gated behind auth: protected endpoints 401 until logged in
let healthTimer: ReturnType<typeof setInterval>

async function startApp() {
  await Promise.all([loadMeta(), loadDevices(), loadLocations(), loadHealth()])
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
      loadActiveProjectFromStorage()
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
  <a-config-provider :theme="themeConfig">
    <LoginView v-if="!authToken" @authenticated="onAuthenticated" />
    <a-layout v-else style="height:100vh">

      <!-- Header -->
      <a-layout-header style="display:flex;align-items:center;gap:12px;padding:0 20px;height:48px;line-height:48px;background:#0a0a0a;border-bottom:1px solid rgba(255,255,255,0.08)">
        <IotisticaLogo :size="24" />
        <span style="color:rgba(255,255,255,0.85);font-size:15px;font-weight:600;letter-spacing:.3px">Iotistica</span>
        <span style="color:rgba(255,255,255,0.25);font-size:13px;font-weight:400">BACnet Simulator</span>
        

        <div style="display:flex;align-items:center;gap:2px;margin-left:12px;padding-left:12px;padding-right:12px; border-left:1px solid rgba(255,255,255,0.08);border-right:1px solid rgba(255,255,255,0.08)">
          <a-tooltip title="Start simulation clock">
            <a-button
              size="small" type="text" :disabled="health.sim_state === 'running'" :loading="simActionLoading"
              @click="simStart"
            >
              <template #icon><PlayCircleOutlined :style="{ color: health.sim_state === 'running' ? '#555' : '#52c41a' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Pause simulation clock (freezes values in place and stops responding on the network)">
            <a-button
              size="small" type="text" :disabled="health.sim_state !== 'running'" :loading="simActionLoading"
              @click="simPause"
            >
              <template #icon><PauseCircleOutlined :style="{ color: health.sim_state !== 'running' ? '#555' : '#faad14' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Stop simulation clock, rewind to t=0, and stop responding on the network">
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
          <a-radio-button value="bacnet"><ApiOutlined  /> BACnet</a-radio-button>
          <a-radio-button value="alarms"><AlertOutlined /> Alarms</a-radio-button>
          <a-radio-button value="settings"><SettingOutlined /> Settings</a-radio-button>
          <a-radio-button value="packet-capture"><ClusterOutlined /> Network</a-radio-button>
          <a-radio-button value="utility"><DashboardOutlined /> Utilities</a-radio-button>
          <a-radio-button value="semantic"><PartitionOutlined /> Semantic</a-radio-button>
        </a-radio-group>

        <div style="flex:1" />
        <a-tag v-if="activeProjectName" color="blue" style="margin:0;font-size:11px;cursor:default">{{ activeProjectName }}</a-tag>
        <a-button size="small" @click="onNewProjectClick">
          <template #icon><FileAddOutlined /></template>
          New Project
        </a-button>
        <a-button size="small" type="primary" ghost :disabled="!projectDirty" @click="openSave">Save</a-button>
        <a-button v-if="activeProjectId !== null" size="small" @click="openSaveAs">Save As</a-button>
        <a-button size="small" @click="projectsDrawerOpen = true">Open Project</a-button>

        <div style="display:flex;align-items:center;gap:4px;margin-left:12px;padding-left:12px;border-left:1px solid rgba(255,255,255,0.08)">
          <span style="color:rgba(255,255,255,0.5);font-size:12px">
            <UserOutlined /> {{ currentUser?.username }}
          </span>
          <a-tooltip :title="isDark ? 'Switch to light mode' : 'Switch to dark mode'">
            <a-button size="small" type="text" @click="toggleDark">
              <template #icon><BulbOutlined :style="{ color: isDark ? '#faad14' : 'rgba(255,255,255,0.5)' }" /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="Sign out">
            <a-button size="small" type="text" @click="doLogout">
              <template #icon><LogoutOutlined :style="{ color: 'rgba(255,255,255,0.5)' }" /></template>
            </a-button>
          </a-tooltip>
        </div>
      </a-layout-header>

      <AnalyticsDashboard v-if="activeView === 'bacnet'" />
      <AlarmsPanel v-else-if="activeView === 'alarms'" />
      <PacketCapturePanel v-else-if="activeView === 'packet-capture'" :initial-device-id="packetCaptureDeviceFilter"/>
      <SettingsView v-else-if="activeView === 'settings'" />
      <UtilitiesDashboard v-else-if="activeView === 'utility'" />
      <SemanticPanel v-else-if="activeView === 'semantic'" />


      <a-layout v-else>

        <!-- Sidebar: devices -->
        <a-layout-sider :width="320" style="background:var(--surface);border-right:1px solid var(--border);overflow:auto">
          <div style="padding:10px 12px 10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px">Items ({{ sidebarRawCount }})</span>
            <a-space :size="4">
              <a-button size="small" title="Add Location" @click="openAddLocation">
                <template #icon><FolderAddOutlined /></template>
              </a-button>
              <a-button size="small" type="primary" @click="openAddDevice">+ Add Device</a-button>
              <a-button size="small" :title="hasExternalDevices ? 'Rediscover' : 'Discover Devices'" :loading="externalSyncLoading" @click="runDiscovery">
                <template #icon><ApiOutlined /></template>
              </a-button>
            </a-space>
          </div>

          <div v-if="sidebarRawCount" style="padding:8px 12px;border-bottom:1px solid var(--border)">
            <a-input
              v-model:value="deviceSearch"
              size="small"
              allow-clear
              placeholder="Search devices…"
            >
              <template #prefix><SearchOutlined style="color:var(--text-placeholder)" /></template>
            </a-input>
          </div>

          <div v-if="!sidebarRawCount" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
            <span v-if="externalSyncError" style="color:var(--error, #ff4d4f)">{{ externalSyncError }}</span>
            <span v-else>No devices yet</span>
          </div>
          <div v-else-if="!sidebarFilteredCount" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
            No devices match "{{ deviceSearch }}"
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
              <!-- Location row -->
              <div v-if="node.kind === 'location'" style="display:flex;align-items:center;gap:6px;padding:2px 0">
                <FolderOutlined style="color:#1890ff" />
                <span style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;font-size:12.5px">
                  {{ node.location.name }}
                </span>
                <a-space :size="0" @click.stop>
                  <a-button type="text" size="small" title="Edit" @click="openEditLocation(node.location)">
                    <template #icon><EditOutlined /></template>
                  </a-button>
                </a-space>
              </div>

              <!-- External BACnet device row — project-local mutations (Edit,
                   Create Simulated Copy, Remove from Project) are allowed;
                   source mutations (BACnet writes, simulation config) never
                   are, enforced backend-side regardless of this UI. -->
              <div v-else-if="node.device.source_type === 'external-bacnet'" style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <ApiOutlined style="color:var(--text-muted)" />
                <div style="flex:1;min-width:0">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ node.device.name }}</div>
                  <div style="font-size:11px;color:var(--text-secondary)">
                    ID {{ node.device.device_instance }}<template v-if="node.device.external_host"> · {{ node.device.external_host }}</template>
                  </div>
                </div>
                <a-tag color="default" style="font-size:10px;margin:0;line-height:16px">External</a-tag>
                <a-space :size="2" @click.stop>
                  <a-dropdown :trigger="['click']">
                    <a-button type="text" size="small" title="More">
                      <template #icon><EllipsisOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu @click.stop>
                        <a-menu-item key="edit" @click="openEditDevice(node.device)">
                          <EditOutlined /> Edit
                        </a-menu-item>
                        <a-menu-item key="create-simulated-copy" @click="openCreateSimulatedCopy(node.device)">
                          <CopyOutlined /> Create Simulated Copy
                        </a-menu-item>
                        <a-menu-divider />
                        <a-menu-item key="remove" danger @click="removeExternalDevice(node.device)">
                          <DeleteOutlined /> Remove from Project
                        </a-menu-item>
                      </a-menu>
                    </template>
                  </a-dropdown>
                </a-space>
              </div>

              <!-- Device row -->
              <div v-else style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <a-badge :status="node.device.enabled ? 'success' : 'default'" />
                <div style="flex:1;min-width:0">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ node.device.name }}</div>
                  <div style="font-size:11px;color:var(--text-secondary)">
                    ID {{ node.device.device_instance }}<template v-if="node.device.equipment_type"> · {{ equipmentTypeLabel[node.device.equipment_type] ?? node.device.equipment_type }}</template>
                  </div>
                </div>
                <a-space :size="2" @click.stop>
                  <a-dropdown :trigger="['click']">
                    <a-button type="text" size="small" title="More">
                      <template #icon><EllipsisOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu @click.stop>
                        <a-menu-item key="edit" @click="openEditDevice(node.device)">
                          <EditOutlined /> Edit
                        </a-menu-item>
                        <a-menu-item key="duplicate" @click="duplicateDevice(node.device)">
                          <CopyOutlined /> Duplicate
                        </a-menu-item>
                        <a-menu-divider />
                        <a-menu-item key="export-ede" @click="exportDeviceEde(node.device)">
                          <DownloadOutlined /> Export EDE
                        </a-menu-item>
                        <a-menu-item key="import-ede" @click="importDeviceEde(node.device)">
                          <UploadOutlined /> Import EDE
                        </a-menu-item>
                        <a-menu-item key="export-brick" @click="exportDeviceBrick(node.device)">
                          <DownloadOutlined /> Export Brick Schema (.ttl)
                        </a-menu-item>
                        <a-menu-divider />
                        <a-menu-item key="notification-classes" @click="openNotificationClasses(node.device)">
                          <AlertOutlined /> Notification Classes
                        </a-menu-item>
                        <a-menu-item key="event-enrollments" @click="openEventEnrollments(node.device)">
                          <AlertOutlined /> Event Enrollments
                        </a-menu-item>
                        <a-menu-item key="trend-logs" @click="openTrendLogs(node.device)">
                          <LineChartOutlined /> Trend Logs
                        </a-menu-item>
                        <a-menu-item key="schedules" @click="openSchedules(node.device)">
                          <CalendarOutlined /> Schedules
                        </a-menu-item>
                        <a-menu-item key="calendars" @click="openCalendars(node.device)">
                          <ScheduleOutlined /> Calendars
                        </a-menu-item>
                        <a-menu-divider />
                        <a-menu-item key="energy-model" @click="openEnergyModel(node.device)">
                          <ThunderboltOutlined /> Energy Model
                        </a-menu-item>
                        <a-menu-item key="view-traffic" @click="viewTraffic(node.device)">
                          <ClusterOutlined /> View Traffic
                        </a-menu-item>
                      </a-menu>
                    </template>
                  </a-dropdown>
                </a-space>
              </div>
            </template>
          </a-tree>
        </a-layout-sider>
        <input ref="edeImportInput" type="file" accept=".ede,.csv,text/csv" style="display:none" @change="onEdeImportFileChange" />

        <!-- Content: objects + log -->
        <a-layout-content style="display:flex;flex-direction:column;overflow:hidden">
        <div style="flex:1;overflow:auto;padding:20px">

          <div v-if="!selectedDevice" style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px">
            <ApiOutlined style="font-size:48px;color:var(--icon-disabled)" />
            <span style="font-size:15px;color:var(--text-placeholder)">Select a device to manage its objects</span>
          </div>

          <ObjectsPanel
            v-else
            ref="objectsPanelRef"
            :device="selectedDevice"
            :meta="meta"
            :live-values="liveValues"
            @device-updated="loadDevices"
          />

        </div>
        <DeviceLogPanel />
        </a-layout-content>
      </a-layout>
    </a-layout>

    <!-- Device drawer -->
    <DeviceDrawer
      v-model:open="deviceDrawerOpen"
      :device="editingDevice"
      :meta="meta"
      :locations="locations"
      :existing-instances="devices.map(d => d.device_instance)"
      @saved="onDeviceSaved"
    />

    <!-- Location drawer -->
    <LocationDrawer
      v-model:open="locationDrawerOpen"
      :location="editingLocation"
      :locations="locations"
      :meta="meta"
      @saved="loadLocations"
    />

    <!-- Create Simulated Copy modal -->
    <CreateSimulatedCopyModal
      v-model:open="createCopyModalOpen"
      :source-device="createCopySource"
      :existing-instances="devices.map(d => d.device_instance)"
      @created="onSimulatedCopyCreated"
    />

    <!-- Projects drawer -->
    <ProjectsDrawer
      v-model:open="projectsDrawerOpen"
      @loaded="onProjectLoaded"
    />

    <!-- New project modal -->
    <NewProjectModal
      v-model:open="newProjectModalOpen"
      :reset-project="resetToNewProject"
      @created="onNewProjectCreated"
    />

    <!-- Notification classes drawer -->
    <NotificationClassDrawer v-model:open="notificationClassDrawerOpen" :device="notificationClassDevice" :devices="devices" />

    <!-- Event enrollments drawer -->
    <EventEnrollmentDrawer v-model:open="eventEnrollmentDrawerOpen" :device="eventEnrollmentDevice" />

    <!-- Trend logs drawer -->
    <TrendLogDrawer v-model:open="trendLogDrawerOpen" :device="trendLogDevice" />

    <!-- Schedules drawer -->
    <ScheduleDrawer v-model:open="scheduleDrawerOpen" :device="scheduleDevice" />

    <!-- Calendars drawer -->
    <CalendarDrawer v-model:open="calendarDrawerOpen" :device="calendarDevice" />

    <!-- Energy model drawer -->
    <EnergyModelDrawer v-model:open="energyModelDrawerOpen" :device="energyModelDevice" :meta="meta" />

    <!-- Save project modal -->
    <a-modal
      v-model:open="saveModalOpen"
      title="Save Project"
      ok-text="Save"
      :confirm-loading="saveModalLoading"
      :ok-button-props="{ disabled: !saveModalName.trim() }"
      @ok="doSave"
    >
      <a-form layout="vertical" style="margin-top:8px">
        <a-form-item label="Project Name" required>
          <a-input v-model:value="saveModalName" placeholder="My Project" @pressEnter="doSave" />
        </a-form-item>
        <a-form-item label="Description">
          <a-input v-model:value="saveModalDesc" placeholder="Optional description" />
        </a-form-item>
      </a-form>
    </a-modal>

  </a-config-provider>
</template>
