<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import DeviceDrawer from './components/DeviceDrawer.vue'
import LocationDrawer from './components/LocationDrawer.vue'
import ObjectDrawer from './components/ObjectDrawer.vue'
import ProjectsDrawer from './components/ProjectsDrawer.vue'
import TemplatePickerModal from './components/TemplatePickerModal.vue'
import SaveTemplateModal from './components/SaveTemplateModal.vue'
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

import type { Device, SimObject, Meta, Health, HistoryPoint, Location } from './types'
import { api } from './api'
import { authToken, currentUser, logout } from './auth'
import { isDark, toggleDark, themeConfig } from './theme'
import { formatPresentValue } from './format'
import { ClusterOutlined, EditOutlined, DeleteOutlined, ApiOutlined, CopyOutlined, FileAddOutlined, LineChartOutlined, PlayCircleOutlined, PauseCircleOutlined, StopOutlined, UserOutlined, LogoutOutlined, DashboardOutlined, ApartmentOutlined, EllipsisOutlined, DownloadOutlined, UploadOutlined, SearchOutlined, AlertOutlined, CalendarOutlined, ScheduleOutlined, BulbOutlined, SettingOutlined, FolderOutlined, FolderAddOutlined, PartitionOutlined } from '@ant-design/icons-vue'

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
const meta    = ref<Meta>({ object_types: [], behaviors: [], units: [], reliability_options: [], polarity_options: [], segmentation_options: [], brick_version: '', equipment_types: [], point_types: [], location_kinds: [], semantic_predicates: [], network_address: null })
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
const expandedKeys = ref<string[]>([])
watch(locations, () => { expandedKeys.value = locations.value.map(l => `location-${l.id}`) }, { immediate: true })

function onTreeSelect(_keys: unknown, info: { node: { dataRef?: SidebarTreeNode } & Partial<SidebarTreeNode> }) {
  const data = info.node.dataRef ?? (info.node as SidebarTreeNode)
  if (data.kind === 'device' && data.device) selectDevice(data.device)
}

const selectedDevice = ref<Device | null>(null)
const objects = ref<SimObject[]>([])
const liveValues = ref<Record<number, number | boolean>>({})

// Drawers
const deviceDrawerOpen  = ref(false)
const editingDevice     = ref<Device | null>(null)
const locationDrawerOpen = ref(false)
const editingLocation    = ref<Location | null>(null)
const objectDrawerOpen  = ref(false)
const editingObject     = ref<SimObject | null>(null)
const projectsDrawerOpen   = ref(false)
const templateModalOpen    = ref(false)
const saveTemplateOpen     = ref(false)

// Active project state — persisted to localStorage so a page reload (e.g.
// after a frontend rebuild) doesn't lose track of which project "Save"
// should overwrite.
const ACTIVE_PROJECT_KEY = 'bacnet-sim-active-project'
const activeProjectId   = ref<number | null>(null)
const activeProjectName = ref<string | null>(null)
const activeProjectDesc = ref<string>('')

function loadActiveProjectFromStorage() {
  try {
    const raw = localStorage.getItem(ACTIVE_PROJECT_KEY)
    if (!raw) return
    const saved = JSON.parse(raw) as { id: number; name: string; desc: string }
    activeProjectId.value = saved.id
    activeProjectName.value = saved.name
    activeProjectDesc.value = saved.desc
  } catch {
    // Malformed/stale storage — ignore and fall back to "no active project"
  }
}

watch([activeProjectId, activeProjectName, activeProjectDesc], () => {
  if (activeProjectId.value === null) {
    localStorage.removeItem(ACTIVE_PROJECT_KEY)
  } else {
    localStorage.setItem(ACTIVE_PROJECT_KEY, JSON.stringify({
      id: activeProjectId.value,
      name: activeProjectName.value,
      desc: activeProjectDesc.value,
    }))
  }
})

// Save-project modal
const saveModalOpen    = ref(false)
const saveModalName    = ref('')
const saveModalDesc    = ref('')
const saveModalLoading = ref(false)

// Set-value modal
const setValOpen    = ref(false)
const setValObj     = ref<SimObject | null>(null)
const setValInput   = ref(0)
const setValActive  = ref(true)
const setValLoading = ref(false)
const setValIsBinary = computed(() => setValObj.value?.object_type.startsWith('binary') ?? false)

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
  return formatPresentValue(obj.object_type, liveVal(obj.id))
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
      firmware_revision:        d.firmware_revision,
      protocol_revision:        d.protocol_revision,
      max_apdu_length_accepted: d.max_apdu_length_accepted,
      segmentation_supported:   d.segmentation_supported,
      location_id:              d.location_id,
    })
    const srcObjects = await api.objects.list(d.id)
    for (const obj of srcObjects) {
      await api.objects.create(created.id, {
        object_type:      obj.object_type,
        object_instance:  obj.object_instance,
        name:             obj.name,
        units:            obj.units,
        behavior:         obj.behavior,
        behavior_params:  obj.behavior_params,
        enabled:          obj.enabled,
        number_of_states: obj.number_of_states,
        reliability:      obj.reliability,
        polarity:         obj.polarity,
      })
    }
    await loadDevices()
    await loadHealth()
    message.success(`Duplicated "${d.name}" with ${srcObjects.length} object${srcObjects.length !== 1 ? 's' : ''}`)
  } catch (e: unknown) {
    message.error((e as Error).message)
  }
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
    if (selectedDevice.value?.id === target.id) await loadObjects()
  } catch (e2: unknown) {
    message.error((e2 as Error).message ?? 'Import failed')
  }
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
      selectedDevice.value = null
      objects.value = []
      activeProjectId.value = null
      activeProjectName.value = null
      activeProjectDesc.value = ''
      await loadDevices()
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

async function openSave() {
  if (activeProjectId.value !== null) {
    // Overwrite existing project directly — no dialog
    try {
      await api.projects.update(activeProjectId.value, activeProjectName.value!, activeProjectDesc.value)
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
    const project = await api.projects.save(saveModalName.value.trim(), saveModalDesc.value.trim())
    activeProjectId.value = project.id
    activeProjectName.value = project.name
    activeProjectDesc.value = project.description
    saveModalOpen.value = false
    message.success(`"${project.name}" saved`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save')
  } finally {
    saveModalLoading.value = false
  }
}

async function onProjectLoaded(id: number, name: string, desc: string) {
  activeProjectId.value = id
  activeProjectName.value = name
  activeProjectDesc.value = desc
  await loadDevices()
  await loadLocations()
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
  return formatPresentValue(obj.object_type, v)
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
  const current = liveVal(obj.id)
  if (obj.object_type.startsWith('binary')) {
    setValActive.value = typeof current === 'boolean' ? current : Number(current ?? 0) >= 0.5
  } else {
    setValInput.value = Number(current ?? 0)
  }
  setValOpen.value = true
}
async function doSetValue() {
  if (!setValObj.value || !selectedDevice.value) return
  setValLoading.value = true
  try {
    const value = setValIsBinary.value ? setValActive.value : setValInput.value
    await api.objects.setValue(selectedDevice.value.id, setValObj.value.id, value)
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

// Point Type stores the raw Brick class (e.g. "Supply_Air_Temperature_Sensor")
// — look up its friendly display label from /meta rather than showing the
// underscored class name directly.
const pointTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of meta.value.point_types) map[o.value] = o.label
  return map
})

const equipmentTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of meta.value.equipment_types) map[o.value] = o.label
  return map
})

function compareText(
  a: string | null | undefined,
  b: string | null | undefined,
): number {
  return (a ?? '').localeCompare(b ?? '', undefined, {
    numeric: true,
    sensitivity: 'base',
  })
}

function sortableLiveValue(obj: SimObject): number {
  const value = liveVal(obj.id)

  // Put objects without a live value at the beginning in ascending order.
  if (value === null) return Number.NEGATIVE_INFINITY
  if (typeof value === 'boolean') return value ? 1 : 0

  const numericValue = Number(value)
  return Number.isNaN(numericValue)
    ? Number.NEGATIVE_INFINITY
    : numericValue
}

const columns: TableColumnsType<SimObject> = [
  {
    title: 'Name',
    dataIndex: 'name',
    key: 'name',
    sorter: (a, b) => compareText(a.name, b.name),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Type',
    key: 'type',
    width: 170,
    sorter: (a, b) => compareText(a.object_type, b.object_type),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Inst.',
    dataIndex: 'object_instance',
    key: 'instance',
    width: 65,
    sorter: (a, b) => a.object_instance - b.object_instance,
    sortDirections: ['ascend', 'descend'],
    // Optional initial sorting:
    // defaultSortOrder: 'ascend',
  },
  {
    title: 'Behavior',
    key: 'behavior',
    width: 120,
    sorter: (a, b) => compareText(a.behavior, b.behavior),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Point Type',
    key: 'point_type',
    width: 190,
    sorter: (a, b) => {
      const aLabel = a.point_type
        ? pointTypeLabel.value[a.point_type] ?? a.point_type
        : ''

      const bLabel = b.point_type
        ? pointTypeLabel.value[b.point_type] ?? b.point_type
        : ''

      return compareText(aLabel, bLabel)
    },
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Units',
    dataIndex: 'units',
    key: 'units',
    width: 150,
    sorter: (a, b) =>
      compareText(
        a.units === 'no-units' ? '' : a.units,
        b.units === 'no-units' ? '' : b.units,
      ),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'Live Value',
    key: 'value',
    width: 110,
    sorter: (a, b) => sortableLiveValue(a) - sortableLiveValue(b),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: 'On',
    key: 'enabled',
    width: 50,
    sorter: (a, b) => Number(Boolean(a.enabled)) - Number(Boolean(b.enabled)),
    sortDirections: ['ascend', 'descend'],
  },
  {
    title: '',
    key: 'actions',
    width: 200,
  },
]

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
          <a-tooltip title="Pause simulation clock (freezes values in place — still responds on the network)">
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
        <a-button size="small" @click="newProject">
          <template #icon><FileAddOutlined /></template>
          New Project
        </a-button>
        <a-button size="small" type="primary" ghost @click="openSave">Save</a-button>
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
      <PacketCapturePanel v-else-if="activeView === 'packet-capture'"/>
      <SettingsView v-else-if="activeView === 'settings'" />
      <UtilitiesDashboard v-else-if="activeView === 'utility'" />
      <SemanticPanel v-else-if="activeView === 'semantic'" />


      <a-layout v-else>

        <!-- Sidebar: devices -->
        <a-layout-sider :width="320" style="background:var(--surface);border-right:1px solid var(--border);overflow:auto">
          <div style="padding:10px 12px 10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px">Devices ({{ health.devices }})</span>
            <a-space :size="4">
              <a-button size="small" title="Add Location" @click="openAddLocation">
                <template #icon><FolderAddOutlined /></template>
              </a-button>
              <a-button size="small" type="primary" @click="openAddDevice">+ Add Device</a-button>
            </a-space>
          </div>

          <div v-if="devices.length" style="padding:8px 12px;border-bottom:1px solid var(--border)">
            <a-input
              v-model:value="deviceSearch"
              size="small"
              allow-clear
              placeholder="Search devices…"
            >
              <template #prefix><SearchOutlined style="color:var(--text-placeholder)" /></template>
            </a-input>
          </div>

          <div v-if="!devices.length" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
            No devices yet
          </div>
          <div v-else-if="!filteredDevices.length" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
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

          <template v-else>
            <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px">
              <div>
                <div style="font-size:18px;font-weight:600">{{ selectedDevice.name }}</div>
                <div style="font-size:12px;color:var(--text-secondary);margin-top:3px">
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
                <template v-else-if="column.key === 'point_type'">
                  <a-tag v-if="(record as SimObject).point_type">{{ pointTypeLabel[(record as SimObject).point_type!] ?? (record as SimObject).point_type }}</a-tag>
                  <span v-else style="color:var(--text-disabled)">—</span>
                </template>
                <template v-else-if="column.key === 'units'">
                  <span style="color:var(--text-secondary);font-size:12px">{{ (record as SimObject).units === 'no-units' ? '—' : (record as SimObject).units }}</span>
                </template>
                <template v-else-if="column.key === 'value'">
                  <span :style="{ fontFamily:'monospace', color: hasLive((record as SimObject).id) ? '#1890ff' : 'var(--text-disabled)' }">
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
                     <a-button type="text" size="small" title="Edit" @click.stop="openEditObject(record as SimObject)">
                <template #icon><EditOutlined /></template>
              </a-button>
                    <a-button type="text" size="small" title="Duplicate" @click.stop="duplicateObject(record as SimObject)">
                <template #icon><CopyOutlined /></template>
              </a-button>
                   
                    <a-button
                      v-if="(record as SimObject).behavior === 'manual'"
                      type="link" size="small"
                      style="color:#fa8c16"
                      @click="openSetValue(record as SimObject)"
                    >Set</a-button>
                    <a-button type="link" size="small" style="color:#722ed1" @click="openHistory(record as SimObject)">
                      <template #icon><LineChartOutlined /></template>
                    </a-button>
                     <a-button type="text" size="small" danger title="Delete" @click.stop="deleteObject(record as SimObject)">
                      <template #icon><DeleteOutlined /></template>
                    </a-button>
                    
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

    <!-- Object drawer -->
    <ObjectDrawer
      v-model:open="objectDrawerOpen"
      :object="editingObject"
      :device-id="selectedDevice?.id"
      :meta="meta"
      :existing-objects="objects"
      @saved="onObjectSaved"
    />

    <!-- Projects drawer -->
    <ProjectsDrawer
      v-model:open="projectsDrawerOpen"
      @loaded="onProjectLoaded"
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

    <!-- Set value modal -->
    <a-modal
      v-model:open="setValOpen"
      :title="`Set Value — ${setValObj?.name}`"
      ok-text="Set"
      :confirm-loading="setValLoading"
      @ok="doSetValue"
    >
      <div style="padding:8px 0">
        <a-radio-group v-if="setValIsBinary" v-model:value="setValActive" style="width:100%">
          <a-radio-button :value="true" style="width:50%;text-align:center">ON</a-radio-button>
          <a-radio-button :value="false" style="width:50%;text-align:center">OFF</a-radio-button>
        </a-radio-group>
        <a-input-number
          v-else
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
          <span style="font-size:12px;color:var(--text-secondary)">{{ histObj.units === 'no-units' ? '' : histObj.units }}</span>
          <span style="font-size:12px;color:var(--text-placeholder);margin-left:auto">{{ histData.length }} samples</span>
        </div>

        <div v-if="histData.length < 2" style="text-align:center;padding:40px 0;color:var(--text-placeholder);font-size:13px">
          Not enough data yet — check back after a few ticks (5 s each)
        </div>
        <template v-else>
          <!-- Chart -->
          <div style="border:1px solid var(--border-subtle);border-radius:4px;background:var(--panel-bg);overflow:hidden">
            <svg :viewBox="`0 0 ${CHART_W} ${CHART_H}`" style="width:100%;display:block">

              <!-- Y-axis grid lines + labels -->
              <template v-for="tick in histYLabels(histData, histObj)" :key="tick.y">
                <line
                  :x1="CHART_PAD.left" :y1="tick.y"
                  :x2="CHART_W - CHART_PAD.right" :y2="tick.y"
                  stroke="var(--border-subtle)" stroke-width="1"
                />
                <text
                  :x="CHART_PAD.left - 6" :y="tick.y"
                  text-anchor="end" dominant-baseline="middle"
                  font-size="11" fill="var(--text-placeholder)" font-family="monospace"
                >{{ tick.label }}</text>
              </template>

              <!-- X-axis baseline -->
              <line
                :x1="CHART_PAD.left" :y1="CHART_H - CHART_PAD.bottom"
                :x2="CHART_W - CHART_PAD.right" :y2="CHART_H - CHART_PAD.bottom"
                stroke="var(--border)" stroke-width="1"
              />

              <!-- X-axis ticks + labels -->
              <template v-for="tick in histXLabels(histData)" :key="tick.x">
                <line
                  :x1="tick.x" :y1="CHART_H - CHART_PAD.bottom"
                  :x2="tick.x" :y2="CHART_H - CHART_PAD.bottom + 5"
                  stroke="var(--text-disabled)" stroke-width="1"
                />
                <text
                  :x="tick.x" :y="CHART_H - CHART_PAD.bottom + 17"
                  text-anchor="middle"
                  font-size="11" fill="var(--text-placeholder)" font-family="sans-serif"
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
          <div style="display:flex;gap:0;margin-top:14px;border:1px solid var(--border-subtle);border-radius:4px;overflow:hidden">
            <div v-for="(stat, label) in { Min: histStats(histData).min, Max: histStats(histData).max, Avg: histStats(histData).avg, Current: histStats(histData).current }"
              :key="label"
              style="flex:1;text-align:center;padding:10px 0;border-right:1px solid var(--border-subtle)"
              :style="label === 'Current' ? 'border-right:none' : ''"
            >
              <div style="font-size:11px;color:var(--text-secondary);margin-bottom:2px">{{ label }}</div>
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
