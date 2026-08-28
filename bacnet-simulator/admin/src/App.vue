<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import DeviceDrawer from './components/DeviceDrawer.vue'
import LocationDrawer from './components/LocationDrawer.vue'
import EquipmentDrawer from './components/EquipmentDrawer.vue'
import EquipmentPanel from './components/EquipmentPanel.vue'
import ProjectsDrawer from './components/ProjectsDrawer.vue'
import NewProjectModal from './components/NewProjectModal.vue'
import DiscoverModal from './components/DiscoverModal.vue'
import ObjectsPanel from './components/ObjectsPanel.vue'
import CreateSimulatedCopyModal from './components/CreateSimulatedCopyModal.vue'
import ExportEdeOptionsModal from './components/ExportEdeOptionsModal.vue'
import IotisticaLogo from './components/IotisticaLogo.vue'
import LeftSideView from './components/LeftSideView.vue'
import DeviceLogPanel from './components/DeviceLogPanel.vue'
import LoginView from './components/LoginView.vue'
import AnalyticsDashboard from './components/AnalyticsDashboard.vue'
import UtilitiesDashboard from './components/UtilitiesDashboard.vue'
import AlarmsPanel from './components/AlarmsPanel.vue'
import SettingsView from './components/SettingsView.vue'
import PacketCapturePanel from './components/PacketCapturePanel.vue'
import SemanticPanel from './components/SemanticPanel.vue'
import FunctionalTestsView from './components/functional-tests/FunctionalTestsView.vue'
import SavedGraphsView from './components/SavedGraphsView.vue'
import NotificationClassDrawer from './components/NotificationClassDrawer.vue'
import EventEnrollmentDrawer from './components/EventEnrollmentDrawer.vue'
import TrendLogDrawer from './components/TrendLogDrawer.vue'
import ReplayRecordingDrawer from './components/ReplayRecordingDrawer.vue'
import ReplayPlaybackDrawer from './components/ReplayPlaybackDrawer.vue'
import CalibrationDrawer from './components/calibration/CalibrationDrawer.vue'
import ScheduleDrawer from './components/ScheduleDrawer.vue'
import CalendarDrawer from './components/CalendarDrawer.vue'
import EnergyModelDrawer from './components/EnergyModelDrawer.vue'
import SimulationModelModal from './components/SimulationModelDrawer.vue'

import type { Device, Meta, Health, Location, Equipment, Project, BACnetConnectionConfig, BACnetDiscoveryConnection, ReplayRecording } from './types'
import { api, projectDirty } from './api'
import { buildLocationTreeOptions, flattenLocationTree } from './locationTree'
import { authToken, currentUser, logout } from './auth'
import { isDark, toggleDark, themeConfig } from './theme'
import { copyDeviceAndObjects } from './deviceCopy'
import { getLocationIcon } from './locationIcons'
import { getEquipmentIcon, getControllerIcon } from './equipmentIcons'
import { ClusterOutlined, EditOutlined, ApiOutlined, CopyOutlined, FileAddOutlined, LineChartOutlined, PlayCircleOutlined, PauseCircleOutlined, StopOutlined, UserOutlined, LogoutOutlined, DashboardOutlined, ApartmentOutlined, EllipsisOutlined, DownloadOutlined, UploadOutlined, SearchOutlined, AlertOutlined, CalendarOutlined, ScheduleOutlined, BulbOutlined, SettingOutlined, FolderAddOutlined, PartitionOutlined, ThunderboltOutlined, DeleteOutlined, ExperimentOutlined, PlusOutlined, DeploymentUnitOutlined, FolderOutlined } from '@ant-design/icons-vue'

const activeView = ref<
  'devices' |
  'bacnet' |
  'alarms' |
  'packet-capture' |
  'settings' |
  'utility' |
  'semantic' |
  'tests' |
  'graphs'
>('devices')

const health  = ref<Health>({ status: 'unknown', bacnet_running: false, devices: 0, sim_state: 'stopped', elapsed_seconds: 0 })
const simActionLoading = ref(false)
const SIM_STATE_COLOR: Record<Health['sim_state'], string> = { running: '#52c41a', paused: '#faad14', stopped: '#ff4d4f' }
const SIM_STATE_LABEL: Record<Health['sim_state'], string> = { running: 'Running', paused: 'Paused', stopped: 'Stopped' }
const meta    = ref<Meta>({ object_types: [], behaviors: [], units: [], reliability_options: [], polarity_options: [], segmentation_options: [], brick_version: '', equipment_types: [], controller_types: [], point_types: [], location_kinds: [], semantic_predicates: [], energy_model_types: [], network_address: null })
const devices = ref<Device[]>([])
const locations = ref<Location[]>([])
const equipment = ref<Equipment[]>([])
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

// External-BACnet projects: the Discover modal persists results as real
// Device rows (source_type='external-bacnet') via api.discovery.sync(), so
// they flow through the exact same devices/filteredDevices/sidebarTree path
// simulated devices already use — no separate tree structure needed.
const discoverModalOpen = ref(false)
const quickDiscoverLoading = ref(false)
function defaultConnectionName(config: BACnetConnectionConfig): string {
  return config.discovery_target?.trim() || 'Local BACnet'
}

function normalizeDiscoveryConnections(
  connections?: BACnetDiscoveryConnection[] | null,
  legacyConfig?: BACnetConnectionConfig | null,
): BACnetDiscoveryConnection[] {
  if (connections?.length) return connections
  if (!legacyConfig) return []
  return [{
    id: 1,
    name: defaultConnectionName(legacyConfig),
    target: legacyConfig.discovery_target,
    device_instance_low: legacyConfig.device_instance_low,
    device_instance_high: legacyConfig.device_instance_high,
    timeout_ms: legacyConfig.timeout_ms,
    enabled: true,
  }]
}

function connectionConfigForCompat(connections: BACnetDiscoveryConnection[]): BACnetConnectionConfig | null {
  const first = connections[0]
  if (!first) return null
  return {
    discovery_target: first.target,
    device_instance_low: first.device_instance_low,
    device_instance_high: first.device_instance_high,
    timeout_ms: first.timeout_ms,
  }
}

async function refreshDiscoveryConnections() {
  if (activeProjectId.value == null) {
    activeProjectDiscoveryConnections.value = []
    activeProjectConnectionConfig.value = null
    return
  }
  try {
    const result = await api.discovery.connections.list(activeProjectId.value)
    activeProjectDiscoveryConnections.value = result.connections
    activeProjectConnectionConfig.value = connectionConfigForCompat(result.connections)
  } catch {
    activeProjectDiscoveryConnections.value = []
    activeProjectConnectionConfig.value = null
  }
}

async function onDiscovered(savedConnections: BACnetDiscoveryConnection[] | null) {
  if (savedConnections) {
    activeProjectDiscoveryConnections.value = savedConnections
    activeProjectConnectionConfig.value = connectionConfigForCompat(savedConnections)
  }
  await loadDevices()
  await loadHealth()
}

function onDiscoverClick() {
  discoverModalOpen.value = true
}

async function discoverConnection(connection: BACnetDiscoveryConnection) {
  if (activeProjectId.value == null) {
    discoverModalOpen.value = true
    return
  }
  quickDiscoverLoading.value = true
  try {
    const result = await api.discovery.connections.sync(activeProjectId.value, connection.id)
    message.success(result.devices.length
      ? `${connection.name}: discovered ${result.devices.length} device${result.devices.length !== 1 ? 's' : ''}`
      : 'No devices found')
    await loadDevices()
    await loadHealth()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Discovery failed')
  } finally {
    quickDiscoverLoading.value = false
  }
}

async function discoverAllConnections() {
  const connections = activeProjectDiscoveryConnections.value
  if (!connections.length || activeProjectId.value == null) {
    discoverModalOpen.value = true
    return
  }
  quickDiscoverLoading.value = true
  try {
    const result = await api.discovery.connections.syncAll(activeProjectId.value)
    message.success(result.devices.length
      ? `Discovered ${result.devices.length} device${result.devices.length !== 1 ? 's' : ''} across ${result.connections} connection${result.connections !== 1 ? 's' : ''}`
      : 'No devices found')
    await loadDevices()
    await loadHealth()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Discovery failed')
  } finally {
    quickDiscoverLoading.value = false
  }
}

interface SidebarTreeNode {
  key: string
  kind: 'location' | 'device' | 'equipment' | 'discovered-group'
  location?: Location
  device?: Device
  equipment?: Equipment
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
  // Unassigned external devices are grouped into a frontend-only
  // "Discovered" node so they don't clutter the top-level root list.
  const unassignedExternal = filteredDevices.value.filter(d => d.source_type === 'external-bacnet' && (d.location_id == null))
  const unassignedIds = new Set(unassignedExternal.map(d => d.id))

  // Attach devices to their location nodes (skip discovered ones above)
  for (const d of filteredDevices.value) {
    if (unassignedIds.has(d.id)) continue
    const node: SidebarTreeNode = { key: `device-${d.id}`, kind: 'device', device: d }
    const parent = d.location_id != null ? locationNodes.get(d.location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }
  for (const e of equipment.value) {
    const node: SidebarTreeNode = { key: `equipment-${e.id}`, kind: 'equipment', equipment: e }
    const parent = e.location_id != null ? locationNodes.get(e.location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }

  if (unassignedExternal.length > 0) {
    const discoveredChildren: SidebarTreeNode[] = unassignedExternal.map(d => ({ key: `device-${d.id}`, kind: 'device', device: d }))
    const discoveredNode: SidebarTreeNode = { key: 'discovered-group', kind: 'discovered-group', children: discoveredChildren }
    // If there's exactly one top-level Building location, nest Discovered under it; otherwise show it at root.
    const topLevelBuildings = roots.filter(r => r.kind === 'location' && r.location?.kind === 'Building')
    if (topLevelBuildings.length === 1) {
      topLevelBuildings[0].children = topLevelBuildings[0].children ?? []
      topLevelBuildings[0].children.unshift(discoveredNode)
    } else {
      roots.unshift(discoveredNode)
    }
  }
  return roots
})
// Total sidebar item count (devices + locations + equipment) -- NOT
// devices.value.length alone, otherwise a project containing only a
// location or only equipment (zero devices) would hit the "no devices"
// empty state and never render the tree at all, even though sidebarTree
// above has nodes to show.
const sidebarRawCount = computed(() => devices.value.length + locations.value.length + equipment.value.length)
// While searching, the tree itself falls back to a flat device-only list
// (see sidebarTree's own comment) -- so the "no results" gate must match
// that same device-only scope, not the total count above.
const sidebarFilteredCount = computed(() =>
  deviceSearch.value.trim() ? filteredDevices.value.length : sidebarRawCount.value
)
const hasExternalDevices = computed(() => devices.value.some(d => d.source_type === 'external-bacnet'))

const expandedKeys = ref<string[]>([])
watch(locations, () => { expandedKeys.value = [...locations.value.map(l => `location-${l.id}`), 'discovered-group'] }, { immediate: true })

const moveToOptions = computed(() => flattenLocationTree(buildLocationTreeOptions(locations.value)))
const duplicateLocationOptions = computed(() => buildLocationTreeOptions(locations.value))

async function assignDeviceToLocation(device: Device, locationId: number) {
  const { id, ...rest } = device as any
  try {
    await api.devices.update(id, { ...rest, location_id: locationId })
    await loadDevices()
    message.success('Device assigned')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to assign device')
  }
}

function onTreeSelect(_keys: unknown, info: { node: { dataRef?: SidebarTreeNode } & Partial<SidebarTreeNode> }) {
  const data = info.node.dataRef ?? (info.node as SidebarTreeNode)
  if (data.kind === 'device' && data.device) selectDevice(data.device)
  else if (data.kind === 'equipment' && data.equipment) selectEquipment(data.equipment)
}

const selectedDevice = ref<Device | null>(null)
const selectedEquipment = ref<Equipment | null>(null)
const objectsPanelRef = ref<{ reload: () => void } | null>(null)
const liveValues = ref<Record<number, number | boolean>>({})
const unackedAlarmCount = ref(0)
const modelValues = ref<Record<number, number | boolean>>({})
const modelStates = ref<Record<number, string>>({})

// Drawers
const deviceDrawerOpen  = ref(false)
const editingDevice     = ref<Device | null>(null)
const locationDrawerOpen = ref(false)
const editingLocation    = ref<Location | null>(null)
const equipmentDrawerOpen = ref(false)
const editingEquipment    = ref<Equipment | null>(null)
// Preselected location/parent for the unified "+ Add" flow (null = top
// level / no location) -- only consulted by the drawers when adding fresh,
// never when editing (see DeviceDrawer/LocationDrawer's defaultLocationId/
// defaultParentLocationId props).
const addContextLocationId = ref<number | null>(null)
const projectsDrawerOpen   = ref(false)
const createCopyModalOpen  = ref(false)
const createCopySource     = ref<Device | null>(null)
const createCopyPreselectedRecordingId = ref<number | null>(null)
const exportEdeModalOpen   = ref(false)
const exportEdeDevice      = ref<Device | null>(null)
const duplicateModalOpen   = ref(false)
const duplicateSource      = ref<Device | null>(null)
const duplicateLoading     = ref(false)
const duplicateLocationId  = ref<number | null>(null)
const duplicateName        = ref('')
const duplicateOptions     = ref({
  semantics: true,
  simulation: true,
})

// Sidebar (LeftSideView) width — user-resizable via its own drag handle,
// persisted so the chosen width survives a page reload.
const SIDEBAR_WIDTH_KEY = 'bacnet-sim-sidebar-width'
const sidebarWidth = ref<number>((() => {
  const raw = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY))
  return Number.isFinite(raw) && raw > 0 ? raw : 320
})())
function onSidebarWidthChange(value: number) {
  sidebarWidth.value = value
  localStorage.setItem(SIDEBAR_WIDTH_KEY, String(value))
}

// Active project state — persisted to localStorage so a page reload (e.g.
// after a frontend rebuild) doesn't lose track of which project "Save"
// should overwrite.
const ACTIVE_PROJECT_KEY = 'bacnet-sim-active-project'
const activeProjectId   = ref<number | null>(null)
const activeProjectName = ref<string | null>(null)
const activeProjectDesc = ref<string>('')
// The project's saved BACnet discovery connections. Deliberately NOT
// persisted to localStorage (unlike id/name/desc below) — it must always
// come from an authoritative backend response (project load/create/save-as,
// or a just-completed connection save), never from a browser cache, so
// switching projects or refreshing the page can never leak a stale or
// wrong-project value into the Discover modal.
const activeProjectConnectionConfig = ref<BACnetConnectionConfig | null>(null)
const activeProjectDiscoveryConnections = ref<BACnetDiscoveryConnection[]>([])

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
}, { deep: true })

// Save-project modal
const saveModalOpen    = ref(false)
const saveModalName    = ref('')
const saveModalDesc    = ref('')
const saveModalLoading = ref(false)
// Direct "Save" toolbar button (overwrite, no dialog) -- separate from
// saveModalLoading, which only guards the "Save As" modal's own OK button.
const savingProject = ref(false)

// WebSocket
let ws: WebSocket | null = null
let wsTimer: ReturnType<typeof setTimeout> | null = null

function wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(authToken.value ?? '')}`)
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data) as {
      devices?: {
        objects?: {
          id: number
          value?: number | boolean
          model_value?: number | boolean
          model_state?: string
        }[]
      }[]
    }
    const map: Record<number, number | boolean> = {}
    const modelMap: Record<number, number | boolean> = {}
    const stateMap: Record<number, string> = {}
    data.devices?.forEach(d => d.objects?.forEach(o => {
      if (o.value !== undefined) map[o.id] = o.value
      if (o.model_value !== undefined) modelMap[o.id] = o.model_value
      if (o.model_state !== undefined) stateMap[o.id] = o.model_state
    }))
    // Merge rather than replace -- a snapshot taken mid SimEngine.reload()
    // (e.g. right after an object save, while the engine has cleared
    // self._objects and is rebuilding it) can legitimately omit objects
    // that exist a moment before and after. Replacing the whole map on
    // every message turned that transient gap into every row's Value
    // column blanking out for a second or two; merging keeps each
    // object's last-known value in place until it reappears in a later,
    // complete snapshot.
    liveValues.value = { ...liveValues.value, ...map }
    modelValues.value = { ...modelValues.value, ...modelMap }
    modelStates.value = { ...modelStates.value, ...stateMap }
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

function markExternalDeviceSeen(deviceId: number, seenAt: string) {
  devices.value = devices.value.map(d =>
    d.id === deviceId ? { ...d, external_last_seen_at: seenAt } : d
  )
  if (selectedDevice.value?.id === deviceId) {
    selectedDevice.value = { ...selectedDevice.value, external_last_seen_at: seenAt }
  }
}
async function loadLocations() {
  try { locations.value = await api.locations.list() } catch { /* swallow */ }
}
async function loadEquipment() {
  try {
    equipment.value = await api.equipment.list()
    if (selectedEquipment.value) {
      const found = equipment.value.find(e => e.id === selectedEquipment.value!.id)
      selectedEquipment.value = found ?? null
    }
  } catch { /* swallow */ }
}

function selectDevice(d: Device) {
  selectedEquipment.value = null
  selectedDevice.value = d
}

function selectEquipment(e: Equipment) {
  selectedDevice.value = null
  selectedEquipment.value = e
}

function onSelectController(deviceId: number) {
  const device = devices.value.find(d => d.id === deviceId)
  if (device) selectDevice(device)
}

// Device actions
function openAddDevice(locationId: number | null = null) { editingDevice.value = null; addContextLocationId.value = locationId; deviceDrawerOpen.value = true }
function openEditDevice(d: Device) { editingDevice.value = d; deviceDrawerOpen.value = true }
async function onDeviceSaved() { await loadDevices(); await loadHealth() }
function duplicateDevice(d: Device) {
  duplicateSource.value = d
  duplicateName.value = `${d.name} Copy`
  duplicateLocationId.value = d.location_id ?? null
  duplicateOptions.value = { semantics: true, simulation: true }
  duplicateModalOpen.value = true
}

async function confirmDuplicateDevice() {
  const source = duplicateSource.value
  if (!source) return
  const name = duplicateName.value.trim()
  if (!name) {
    message.warning('Name is required')
    return
  }
  const nextInstance = devices.value.length
    ? Math.max(...devices.value.map(x => x.device_instance)) + 1
    : source.device_instance + 1
  duplicateLoading.value = true
  try {
    const srcObjects = await api.objects.list(source.id)
    const { device, objectCount, simulationModelCount } = await copyDeviceAndObjects(source, srcObjects, {
      name,
      deviceInstance: nextInstance,
      locationId: duplicateLocationId.value,
      copySemantics: duplicateOptions.value.semantics,
      copySimulation: duplicateOptions.value.simulation,
    })
    await loadDevices()
    await loadHealth()
    const fresh = devices.value.find(d => d.id === device.id)
    selectedEquipment.value = null
    selectedDevice.value = fresh ?? device
    duplicateModalOpen.value = false
    message.success(
      `Duplicated "${source.name}" with ${objectCount} object${objectCount !== 1 ? 's' : ''}`
      + (simulationModelCount ? ` and ${simulationModelCount} simulation model${simulationModelCount !== 1 ? 's' : ''}` : '')
    )
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    duplicateLoading.value = false
  }
}

// External devices — "Create Simulated Copy" (tree dropdown) and "Remove
// from Project" (also available from DeviceDrawer's Edit form).
function openCreateSimulatedCopy(d: Device) {
  createCopySource.value = d
  createCopyPreselectedRecordingId.value = null
  createCopyModalOpen.value = true
}

// "Create Replay" action on a recording row (ReplayRecordingDrawer.vue) --
// reuses the exact same Create Simulation modal/flow as the ordinary
// device-context-menu entry point above, just preselecting Replay mode
// and this recording (see CreateSimulatedCopyModal.vue's
// preselectedRecordingId prop). Closes the Recordings drawer first so the
// two don't stack.
function openCreateReplayFromRecording(recording: ReplayRecording) {
  if (!replayRecordingDevice.value) return
  createCopySource.value = replayRecordingDevice.value
  createCopyPreselectedRecordingId.value = recording.id
  replayRecordingDrawerOpen.value = false
  createCopyModalOpen.value = true
}
async function onSimulatedCopyCreated(created: Device) {
  await loadDevices()
  const fresh = devices.value.find(d => d.id === created.id)
  selectedEquipment.value = null
  selectedDevice.value = fresh ?? created
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
function openAddLocation(parentLocationId: number | null = null) { editingLocation.value = null; addContextLocationId.value = parentLocationId; locationDrawerOpen.value = true }
function openEditLocation(l: Location) { editingLocation.value = l; locationDrawerOpen.value = true }

// Equipment actions
function openAddEquipment(locationId: number | null = null) { editingEquipment.value = null; addContextLocationId.value = locationId; equipmentDrawerOpen.value = true }
function openEditEquipment(e: Equipment) { editingEquipment.value = e; equipmentDrawerOpen.value = true }

function exportDeviceEde(d: Device) {
  exportEdeDevice.value = d
  exportEdeModalOpen.value = true
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

const replayRecordingDrawerOpen = ref(false)
const replayRecordingDevice = ref<Device | null>(null)
function openReplayRecordings(d: Device) {
  replayRecordingDevice.value = d
  replayRecordingDrawerOpen.value = true
}

const calibrationDrawerOpen = ref(false)
const calibrationDevice = ref<Device | null>(null)
function openCalibration(d: Device) {
  calibrationDevice.value = d
  calibrationDrawerOpen.value = true
}

const replayPlaybackDrawerOpen = ref(false)
const replayPlaybackDevice = ref<Device | null>(null)
function openReplayPlayback(d: Device) {
  replayPlaybackDevice.value = d
  replayPlaybackDrawerOpen.value = true
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

const simulationModelModalOpen = ref(false)
const simulationModelDevice = ref<Device | null>(null)
function openSimulationModel(d: Device) {
  simulationModelDevice.value = d
  simulationModelModalOpen.value = true
}

async function onSimulationModelSaved() {
  await loadDevices()
  objectsPanelRef.value?.reload()
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
  activeProjectConnectionConfig.value = null
  activeProjectDiscoveryConnections.value = []
  projectDirty.value = false
  await loadDevices()
  await loadLocations()
  await loadEquipment()
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

async function onNewProjectCreated(project: Project) {
  activeProjectId.value = project.id
  activeProjectName.value = project.name
  activeProjectDesc.value = project.description
  activeProjectDiscoveryConnections.value = normalizeDiscoveryConnections(project.discovery_connections, project.connection_config)
  activeProjectConnectionConfig.value = connectionConfigForCompat(activeProjectDiscoveryConnections.value)
  // resetToNewProject(true) (called before the save that created this
  // project) already reloaded locations/devices/equipment, but that
  // happened BEFORE the server generated the Above/Below ground building
  // levels the modal's own form just asked for -- reload once more so a
  // non-empty building structure actually shows up instead of only
  // appearing the next time the project is explicitly re-opened.
  await loadLocations()
  await loadDevices()
  await loadEquipment()
}

function openSaveAs() {
  saveModalName.value = activeProjectName.value ? `${activeProjectName.value} (copy)` : ''
  saveModalDesc.value = activeProjectDesc.value
  saveModalOpen.value = true
}

async function openSave() {
  if (activeProjectId.value !== null) {
    // Overwrite existing project directly — no dialog. Guard against a
    // second click landing while the first save is still in flight (the
    // button has no other disabled state to prevent it) — without this,
    // a fast double-click fires api.projects.update() twice and shows two
    // separate "saved" toasts for what the user experienced as one click.
    if (savingProject.value) return
    savingProject.value = true
    try {
      await api.projects.update(
        activeProjectId.value,
        activeProjectName.value!,
        activeProjectDesc.value,
        connectionConfigForCompat(activeProjectDiscoveryConnections.value),
        activeProjectDiscoveryConnections.value,
      )
      projectDirty.value = false
      message.success(`"${activeProjectName.value}" saved`)
    } catch (e: unknown) {
      message.error((e as Error).message ?? 'Failed to save')
    } finally {
      savingProject.value = false
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
    // Save As carries over the current project's saved discovery
    // connections too, so a copy doesn't silently lose them.
    const project = await api.projects.save(
      saveModalName.value.trim(),
      saveModalDesc.value.trim(),
      connectionConfigForCompat(activeProjectDiscoveryConnections.value),
      undefined,
      undefined,
      activeProjectDiscoveryConnections.value,
    )
    activeProjectId.value = project.id
    activeProjectName.value = project.name
    activeProjectDesc.value = project.description
    activeProjectDiscoveryConnections.value = normalizeDiscoveryConnections(project.discovery_connections, project.connection_config)
    activeProjectConnectionConfig.value = connectionConfigForCompat(activeProjectDiscoveryConnections.value)
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
  connectionConfig: BACnetConnectionConfig | null,
  discoveryConnections: BACnetDiscoveryConnection[] | null,
) {
  activeProjectId.value = id
  activeProjectName.value = name
  activeProjectDesc.value = desc
  activeProjectDiscoveryConnections.value = normalizeDiscoveryConnections(discoveryConnections, connectionConfig)
  activeProjectConnectionConfig.value = connectionConfigForCompat(activeProjectDiscoveryConnections.value)
  projectDirty.value = false
  await loadDevices()
  await loadLocations()
  await loadEquipment()
  selectedDevice.value = null
  await loadHealth()
}

const equipmentTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of meta.value.equipment_types) map[o.value] = o.label
  return map
})

const locationKindLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of meta.value.location_kinds) map[o.value] = o.label
  return map
})

// Fixed icon slot so location/equipment/generic tree rows all align their
// labels identically, regardless of which icon (and how visually "wide"
// it looks) is shown.
const TREE_ICON_SLOT_STYLE = {
  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  width: '20px', height: '20px', fontSize: '16px', flexShrink: '0',
} as const

// Lifecycle — gated behind auth: protected endpoints 401 until logged in
let healthTimer: ReturnType<typeof setInterval>
let devicesTimer: ReturnType<typeof setInterval>
let alarmsTimer: ReturnType<typeof setInterval>

async function loadUnackedAlarmCount() {
  try {
    // Same "ack_required && !acknowledged" definition AlarmsPanel.vue's own
    // unackedCount badge uses -- kept in lockstep so the nav tab and the
    // panel's in-view count never disagree. Polled independently here
    // (rather than read from AlarmsPanel) since that component is only
    // mounted while the Alarms tab itself is active (v-else-if), but the
    // nav badge needs to show a live count from any tab.
    const entries = await api.alarms.list(200, true)
    unackedAlarmCount.value = entries.filter(a => a.ack_required && !a.acknowledged).length
  } catch { /* swallow -- matches loadHealth()'s best-effort polling */ }
}

async function startApp() {
  await Promise.all([loadMeta(), loadDevices(), loadLocations(), loadEquipment(), loadHealth(), loadUnackedAlarmCount()])
  await refreshDiscoveryConnections()
  wsConnect()
  healthTimer = setInterval(loadHealth, 10_000)
  devicesTimer = setInterval(loadDevices, 30_000)
  alarmsTimer = setInterval(loadUnackedAlarmCount, 10_000)
}

function stopApp() {
  clearInterval(healthTimer)
  clearInterval(devicesTimer)
  clearInterval(alarmsTimer)
  if (wsTimer) { clearTimeout(wsTimer); wsTimer = null }
  ws?.close()
  ws = null
}

async function onAuthenticated() {
  loadActiveProjectFromStorage()
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
          <a-radio-button value="devices"><ApartmentOutlined /> Browse</a-radio-button>
          <a-radio-button value="bacnet"><ApiOutlined  /> BACnet</a-radio-button>
          <a-radio-button value="alarms"><AlertOutlined /> Alarms{{ unackedAlarmCount ? ` (${unackedAlarmCount})` : '' }}</a-radio-button>
          <a-radio-button value="settings"><SettingOutlined /> Settings</a-radio-button>
          <a-radio-button value="packet-capture"><ClusterOutlined /> Network</a-radio-button>
          <a-radio-button value="utility"><DashboardOutlined /> Utilities</a-radio-button>
          <a-radio-button value="semantic"><PartitionOutlined /> Graph</a-radio-button>
          <a-radio-button value="tests"><ExperimentOutlined /> Tests</a-radio-button>
          <a-radio-button value="graphs"><LineChartOutlined /> Data Graphs</a-radio-button>
        </a-radio-group>

        <div style="flex:1"></div>
        <a-tag v-if="activeProjectName" color="blue" style="margin:0;font-size:11px;cursor:default">{{ activeProjectName }}</a-tag>
        <a-button size="small" @click="onNewProjectClick">
          <template #icon><FileAddOutlined /></template>
          New Project
        </a-button>
        <a-button size="small" type="primary" ghost :disabled="!projectDirty" :loading="savingProject" @click="openSave">Save</a-button>
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
      <FunctionalTestsView v-else-if="activeView === 'tests'" />
      <SavedGraphsView v-else-if="activeView === 'graphs'" />
      <a-layout v-else>
        <!-- Sidebar: extracted tree / left panel -->
        <LeftSideView
          v-model:search="deviceSearch"
          v-model:expanded-keys="expandedKeys"
          :width="sidebarWidth"
          @update:width="onSidebarWidthChange"
          :devices="devices"
          :locations="locations"
          :equipment="equipment"
          :meta="meta"
          :selected-device="selectedDevice"
          :selected-equipment="selectedEquipment"
          :quick-discover-loading="quickDiscoverLoading"
          :discovery-connections="activeProjectDiscoveryConnections"
          @select-device="selectDevice"
          @select-equipment="selectEquipment"
          @add-location="openAddLocation"
          @add-equipment="openAddEquipment"
          @add-device="openAddDevice"
          @edit-location="openEditLocation"
          @edit-equipment="openEditEquipment"
          @edit-device="openEditDevice"
          @discover="onDiscoverClick"
          @discover-all="discoverAllConnections"
          @discover-connection="discoverConnection"
          @manage-discovery="discoverModalOpen = true"
          @simulation-model="openSimulationModel"
        />
        <input ref="edeImportInput" type="file" accept=".ede,.csv,text/csv" style="display:none" @change="onEdeImportFileChange" />

        <!-- Content: objects + log -->
        <a-layout-content style="display:flex;flex-direction:column;overflow:hidden">
        <div style="flex:1;overflow:auto;padding:20px">

          <EquipmentPanel
            v-if="selectedEquipment"
            :equipment="selectedEquipment"
            :devices="devices"
            :locations="locations"
            :meta="meta"
            @edit="openEditEquipment"
            @select-controller="onSelectController"
          />

          <ObjectsPanel
            v-else-if="selectedDevice"
            ref="objectsPanelRef"
            :device="selectedDevice"
            :devices="devices"
            :meta="meta"
            :locations="locations"
            :live-values="liveValues"
            :model-values="modelValues"
            :model-states="modelStates"
            @device-updated="loadDevices"
            @external-device-seen="markExternalDeviceSeen"
            @edit-device="openEditDevice"
            @simulation-model="openSimulationModel"
            @replay-recordings="openReplayRecordings"
            @calibration="openCalibration"
            @create-simulated-copy="openCreateSimulatedCopy"
            @assign-device-location="assignDeviceToLocation"
            @remove-external-device="removeExternalDevice"
            @duplicate-device="duplicateDevice"
            @export-ede="exportDeviceEde"
            @import-ede="importDeviceEde"
            @export-brick="exportDeviceBrick"
            @notification-classes="openNotificationClasses"
            @event-enrollments="openEventEnrollments"
            @trend-logs="openTrendLogs"
            @replay-playback="openReplayPlayback"
            @schedules="openSchedules"
            @calendars="openCalendars"
            @energy-model="openEnergyModel"
            @view-traffic="viewTraffic"
          />

          <div v-else style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px">
            <ApiOutlined style="font-size:48px;color:var(--icon-disabled)" />
            <span style="font-size:15px;color:var(--text-placeholder)">Select a device or equipment to get started</span>
          </div>

        </div>
        <DeviceLogPanel :device="selectedDevice" />
        </a-layout-content>
      </a-layout>
    </a-layout>

    <!-- Device drawer -->
    <DeviceDrawer
      v-model:open="deviceDrawerOpen"
      :device="editingDevice"
      :meta="meta"
      :locations="locations"
      :equipment="equipment"
      :existing-instances="devices.map(d => d.device_instance)"
      :default-location-id="addContextLocationId"
      @saved="onDeviceSaved"
      @simulation-model="openSimulationModel"
    />

    <!-- Location drawer -->
    <LocationDrawer
      v-model:open="locationDrawerOpen"
      :location="editingLocation"
      :locations="locations"
      :meta="meta"
      :default-parent-location-id="addContextLocationId"
      @saved="loadLocations"
    />

    <!-- Equipment drawer -->
    <EquipmentDrawer
      v-model:open="equipmentDrawerOpen"
      :equipment="editingEquipment"
      :equipment-list="equipment"
      :locations="locations"
      :meta="meta"
      :default-location-id="addContextLocationId"
      @saved="loadEquipment"
    />

    <!-- Create Simulation modal -->
    <CreateSimulatedCopyModal
      v-model:open="createCopyModalOpen"
      :source-device="createCopySource"
      :existing-instances="devices.map(d => d.device_instance)"
      :preselected-recording-id="createCopyPreselectedRecordingId"
      @created="onSimulatedCopyCreated"
    />

    <!-- Export EDE options modal -->
    <ExportEdeOptionsModal
      v-model:open="exportEdeModalOpen"
      :device="exportEdeDevice"
    />

    <!-- Duplicate device modal -->
    <a-modal
      v-model:open="duplicateModalOpen"
      title="Duplicate"
      ok-text="OK"
      cancel-text="Close"
      width="320px"
      :confirm-loading="duplicateLoading"
      @ok="confirmDuplicateDevice"
    >
      <a-space direction="vertical" :size="8" style="width:100%;margin-top:4px">
        <a-input
          v-model:value="duplicateName"
          placeholder="Device name"
          @press-enter="confirmDuplicateDevice"
        />
        <a-tree-select
          v-model:value="duplicateLocationId"
          :tree-data="duplicateLocationOptions"
          allow-clear
          placeholder="No location"
          style="width:100%"
          tree-default-expand-all
        />
        <a-checkbox v-model:checked="duplicateOptions.semantics">
          Semantics
        </a-checkbox>
        <a-checkbox v-model:checked="duplicateOptions.simulation">
          Simulation
        </a-checkbox>
      </a-space>
    </a-modal>

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

    <!-- Discover BACnet devices modal -->
    <DiscoverModal
      v-model:open="discoverModalOpen"
      :discovery-connections="activeProjectDiscoveryConnections"
      :active-project-id="activeProjectId"
      @discovered="onDiscovered"
    />

    <!-- Notification classes drawer -->
    <NotificationClassDrawer v-model:open="notificationClassDrawerOpen" :device="notificationClassDevice" :devices="devices" />

    <!-- Event enrollments drawer -->
    <EventEnrollmentDrawer v-model:open="eventEnrollmentDrawerOpen" :device="eventEnrollmentDevice" />

    <!-- Trend logs drawer -->
    <TrendLogDrawer v-model:open="trendLogDrawerOpen" :device="trendLogDevice" />

    <!-- Replay recordings drawer -->
    <ReplayRecordingDrawer
      v-model:open="replayRecordingDrawerOpen"
      :device="replayRecordingDevice"
      @create-replay="openCreateReplayFromRecording"
    />

    <!-- Replay playback drawer -->
    <ReplayPlaybackDrawer v-model:open="replayPlaybackDrawerOpen" :device="replayPlaybackDevice" />

    <!-- Calibration drawer -->
    <CalibrationDrawer v-model:open="calibrationDrawerOpen" :device="calibrationDevice" />

    <!-- Schedules drawer -->
    <ScheduleDrawer v-model:open="scheduleDrawerOpen" :device="scheduleDevice" />

    <!-- Calendars drawer -->
    <CalendarDrawer v-model:open="calendarDrawerOpen" :device="calendarDevice" />

    <!-- Energy model drawer -->
    <EnergyModelDrawer v-model:open="energyModelDrawerOpen" :device="energyModelDevice" :meta="meta" />

    <!-- Simulation model modal -->
    <SimulationModelModal
      v-model:open="simulationModelModalOpen"
      :device="simulationModelDevice"
      @saved="onSimulationModelSaved"
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

  </a-config-provider>
</template>
