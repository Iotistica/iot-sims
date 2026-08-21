<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  ApiOutlined, CopyOutlined, DeleteOutlined, DeploymentUnitOutlined,
  DownloadOutlined, EditOutlined, EllipsisOutlined, FolderAddOutlined,
  FolderOutlined, LineChartOutlined, PlusOutlined, SearchOutlined,
  AlertOutlined, CalendarOutlined, DownOutlined, ScheduleOutlined, ThunderboltOutlined,
  ExperimentOutlined, ClusterOutlined, UploadOutlined, ApartmentOutlined,
} from '@ant-design/icons-vue'
import type { BACnetDiscoveryConnection, Device, Equipment, Location, Meta } from '../types'
import { buildLocationTreeOptions, flattenLocationTree } from '../locationTree'
import { getLocationIcon } from '../locationIcons'
import { getEquipmentIcon, getControllerIcon } from '../equipmentIcons'

interface SidebarTreeNode {
  key: string
  kind: 'location' | 'device' | 'equipment' | 'discovered-group'
  location?: Location
  device?: Device
  equipment?: Equipment
  children?: SidebarTreeNode[]
}

const props = withDefaults(defineProps<{
  devices: Device[]
  locations: Location[]
  equipment: Equipment[]
  meta: Meta
  selectedDevice: Device | null
  selectedEquipment: Equipment | null
  search: string
  expandedKeys: string[]
  quickDiscoverLoading?: boolean
  discoveryConnections?: BACnetDiscoveryConnection[]
  width?: number
}>(), {
  quickDiscoverLoading: false,
  discoveryConnections: () => [],
  width: 320,
})

const emit = defineEmits<{
  'update:search': [value: string]
  'update:expandedKeys': [value: string[]]
  'select-device': [device: Device]
  'select-equipment': [equipment: Equipment]
  'add-location': [id: number | null]
  'add-equipment': [id: number | null]
  'add-device': [id: number | null]
  'edit-location': [location: Location]
  'edit-equipment': [equipment: Equipment]
  'edit-device': [device: Device]
  'discover': []
  'discover-all': []
  'discover-connection': [connection: BACnetDiscoveryConnection]
  'manage-discovery': []
  'assign-device-location': [device: Device, locationId: number]
  'create-simulated-copy': [device: Device]
  'remove-external-device': [device: Device]
  'duplicate-device': [device: Device]
  'export-ede': [device: Device]
  'import-ede': [device: Device]
  'export-brick': [device: Device]
  'notification-classes': [device: Device]
  'event-enrollments': [device: Device]
  'trend-logs': [device: Device]
  'schedules': [device: Device]
  'calendars': [device: Device]
  'energy-model': [device: Device]
  'simulation-model': [device: Device]
  'view-traffic': [device: Device]
  'update:width': [value: number]
}>()

// ─── Sidebar resize ─────────────────────────────────────────────────────
// Width itself is owned by the parent (v-model:width, same pattern as
// search/expandedKeys above) -- this component only owns the drag
// gesture and emits the live value as the pointer moves, so the parent
// can persist it (e.g. to localStorage) however it likes.
const MIN_SIDEBAR_WIDTH = 220
const MAX_SIDEBAR_WIDTH = 560
const resizingSidebar = ref(false)

function startSidebarResize(event: MouseEvent) {
  const startX = event.clientX
  const startWidth = props.width
  resizingSidebar.value = true

  function onMouseMove(moveEvent: MouseEvent) {
    const next = Math.min(
      MAX_SIDEBAR_WIDTH,
      Math.max(MIN_SIDEBAR_WIDTH, startWidth + (moveEvent.clientX - startX)),
    )
    emit('update:width', next)
  }
  function onMouseUp() {
    resizingSidebar.value = false
    window.removeEventListener('mousemove', onMouseMove)
    window.removeEventListener('mouseup', onMouseUp)
  }
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
  event.preventDefault()
}

const deviceSearch = computed({
  get: () => props.search,
  set: (value: string) => emit('update:search', value),
})
const expandedKeys = computed({
  get: () => props.expandedKeys,
  set: (value: string[]) => emit('update:expandedKeys', value),
})

const filteredDevices = computed(() => {
  const q = deviceSearch.value.trim().toLowerCase()
  if (!q) return props.devices
  return props.devices.filter(d =>
    d.name.toLowerCase().includes(q) ||
    String(d.device_instance).includes(q) ||
    d.vendor_name.toLowerCase().includes(q) ||
    d.model_name.toLowerCase().includes(q)
  )
})

const sidebarTree = computed<SidebarTreeNode[]>(() => {
  if (deviceSearch.value.trim()) {
    return filteredDevices.value.map(d => ({ key: `device-${d.id}`, kind: 'device' as const, device: d }))
  }

  const locationNodes = new Map<number, SidebarTreeNode>()
  for (const l of props.locations) {
    locationNodes.set(l.id, { key: `location-${l.id}`, kind: 'location', location: l, children: [] })
  }

  const roots: SidebarTreeNode[] = []
  for (const l of props.locations) {
    const node = locationNodes.get(l.id)!
    const parent = l.parent_location_id != null ? locationNodes.get(l.parent_location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }

  const unassignedExternal = filteredDevices.value.filter(
    d => d.source_type === 'external-bacnet' && d.location_id == null
  )
  const unassignedIds = new Set(unassignedExternal.map(d => d.id))

  for (const d of filteredDevices.value) {
    if (unassignedIds.has(d.id)) continue
    const node: SidebarTreeNode = { key: `device-${d.id}`, kind: 'device', device: d }
    const parent = d.location_id != null ? locationNodes.get(d.location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }

  for (const e of props.equipment) {
    const node: SidebarTreeNode = { key: `equipment-${e.id}`, kind: 'equipment', equipment: e }
    const parent = e.location_id != null ? locationNodes.get(e.location_id) : undefined
    if (parent) parent.children!.push(node)
    else roots.push(node)
  }

  if (unassignedExternal.length) {
    const discoveredNode: SidebarTreeNode = {
      key: 'discovered-group',
      kind: 'discovered-group',
      children: unassignedExternal.map(d => ({ key: `device-${d.id}`, kind: 'device', device: d })),
    }
    const buildings = roots.filter(r => r.kind === 'location' && r.location?.kind === 'Building')
    if (buildings.length === 1) {
      buildings[0].children ??= []
      buildings[0].children!.unshift(discoveredNode)
    } else {
      roots.unshift(discoveredNode)
    }
  }
  return roots
})

const sidebarRawCount = computed(() => props.devices.length + props.locations.length + props.equipment.length)
const sidebarFilteredCount = computed(() => deviceSearch.value.trim() ? filteredDevices.value.length : sidebarRawCount.value)
const hasExternalDevices = computed(() => props.devices.some(d => d.source_type === 'external-bacnet'))
const moveToOptions = computed(() => flattenLocationTree(buildLocationTreeOptions(props.locations)))
const selectedTreeKeys = computed<string[]>(() => {
  if (props.selectedDevice) return ['device-' + props.selectedDevice.id]
  if (props.selectedEquipment) return ['equipment-' + props.selectedEquipment.id]
  return []
})
const expandableTreeKeys = computed<string[]>(() => {
  const keys: string[] = []
  const walk = (nodes: SidebarTreeNode[]) => {
    for (const node of nodes) {
      if (node.children?.length) {
        keys.push(node.key)
        walk(node.children)
      }
    }
  }
  walk(sidebarTree.value)
  return keys
})
const isTreeFullyExpanded = computed(() =>
  expandableTreeKeys.value.length > 0 &&
  expandableTreeKeys.value.every(key => expandedKeys.value.includes(key))
)

const equipmentTypeLabel = computed(() => {
  const map: Record<string,string> = {}
  for (const o of props.meta.equipment_types) map[o.value] = o.label
  return map
})
const locationKindLabel = computed(() => {
  const map: Record<string,string> = {}
  for (const o of props.meta.location_kinds) map[o.value] = o.label
  return map
})
const TREE_ICON_SLOT_STYLE = {
  display:'inline-flex', alignItems:'center', justifyContent:'center',
  width:'20px', height:'20px', fontSize:'16px', flexShrink:'0',
} as const
const EXTERNAL_HEALTH_STALE_MS = 2 * 60 * 1000
const now = ref(Date.now())
let healthClock: ReturnType<typeof setInterval> | null = null

function externalLastSeenMs(device: Device): number | null {
  if (!device.external_last_seen_at) return null
  const value = Date.parse(device.external_last_seen_at)
  return Number.isNaN(value) ? null : value
}

function formatRelativeAge(ageMs: number): string {
  const seconds = Math.max(0, Math.round(ageMs / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  return `${hours}h ago`
}

function deviceHealthStatus(device: Device): 'success' | 'warning' | 'default' {
  if (!device.enabled) return 'default'
  if (device.source_type !== 'external-bacnet') {
    return device.simulation_model_stopped ? 'default' : 'success'
  }
  const lastSeen = externalLastSeenMs(device)
  if (lastSeen === null) return 'default'
  return now.value - lastSeen <= EXTERNAL_HEALTH_STALE_MS ? 'success' : 'warning'
}

function deviceHealthTitle(device: Device): string {
  if (!device.enabled) return 'Device disabled'
  if (device.source_type !== 'external-bacnet') {
    return device.simulation_model_stopped ? 'Simulation stopped — model disabled' : 'Simulation enabled'
  }
  const lastSeen = externalLastSeenMs(device)
  if (lastSeen === null) return 'External device has not been seen yet'
  const age = now.value - lastSeen
  const seen = formatRelativeAge(age)
  return age <= EXTERNAL_HEALTH_STALE_MS
    ? `External device last seen ${seen}`
    : `External device stale; last seen ${seen}`
}

function simulationProviderLabel(device: Device): string | null {
  const provider = device.active_simulation_model?.provider_type
  if (!provider) return null
  if (provider === 'fmu') return 'FMU'
  if (provider === 'learned') return 'Learned'
  return provider
}

function simulationProviderColor(device: Device): string {
  const provider = device.active_simulation_model?.provider_type
  if (provider === 'fmu') return 'purple'
  if (provider === 'learned') return 'cyan'
  return 'default'
}

function simulationProviderTitle(device: Device): string {
  const model = device.active_simulation_model
  if (!model) return ''
  const suffix = model.model_count && model.model_count > 1
    ? ` plus ${model.model_count - 1} more`
    : ''
  return `${model.name} (${model.model_type})${suffix}`
}

onMounted(() => {
  healthClock = setInterval(() => { now.value = Date.now() }, 30_000)
})
const hasDiscoveryConnections = computed(() => props.discoveryConnections.length > 0)
onUnmounted(() => {
  if (healthClock) clearInterval(healthClock)
})

function onTreeSelect(_keys: unknown, info: { node: { dataRef?: SidebarTreeNode } & Partial<SidebarTreeNode> }) {
  const data = info.node.dataRef ?? (info.node as SidebarTreeNode)
  if (data.kind === 'device' && data.device) emit('select-device', data.device)
  else if (data.kind === 'equipment' && data.equipment) emit('select-equipment', data.equipment)
}

const openAddLocation = (id: number | null = null) => emit('add-location', id)
const openAddEquipment = (id: number | null = null) => emit('add-equipment', id)
const openAddDevice = (id: number | null = null) => emit('add-device', id)
const openEditLocation = (v: Location) => emit('edit-location', v)
const openEditEquipment = (v: Equipment) => emit('edit-equipment', v)
const onDiscoverClick = () => emit('discover')
const onDiscoverAllClick = () => emit('discover-all')
const onDiscoverConnectionClick = (connection: BACnetDiscoveryConnection) => emit('discover-connection', connection)
const onManageDiscoveryClick = () => emit('manage-discovery')
const assignDeviceToLocation = (d: Device, id: number) => emit('assign-device-location', d, id)
const openCreateSimulatedCopy = (d: Device) => emit('create-simulated-copy', d)
const removeExternalDevice = (d: Device) => emit('remove-external-device', d)
const duplicateDevice = (d: Device) => emit('duplicate-device', d)
const exportDeviceEde = (d: Device) => emit('export-ede', d)
const importDeviceEde = (d: Device) => emit('import-ede', d)
const exportDeviceBrick = (d: Device) => emit('export-brick', d)
const openNotificationClasses = (d: Device) => emit('notification-classes', d)
const openEventEnrollments = (d: Device) => emit('event-enrollments', d)
const openTrendLogs = (d: Device) => emit('trend-logs', d)
const openSchedules = (d: Device) => emit('schedules', d)
const openCalendars = (d: Device) => emit('calendars', d)
const openEnergyModel = (d: Device) => emit('energy-model', d)
const openSimulationModel = (d: Device) => emit('simulation-model', d)
const viewTraffic = (d: Device) => emit('view-traffic', d)
const toggleTreeExpansion = () => {
  expandedKeys.value = isTreeFullyExpanded.value ? [] : expandableTreeKeys.value
}
</script>

<template>
        <!-- Sidebar: devices -->
        <a-layout-sider
          class="inventory-sider"
          :width="width"
          style="background:var(--surface);border-right:1px solid var(--border)"
        >
          <div style="padding:10px 12px 10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px">Items ({{ sidebarRawCount }})</span>
            <a-space :size="4">
              <a-dropdown :trigger="['click']">
                <a-button size="small" type="primary">+ Add</a-button>
                <template #overlay>
                  <a-menu>
                    <a-menu-item key="location" @click="openAddLocation()">
                      <FolderAddOutlined /> Location
                    </a-menu-item>
                    <a-menu-item key="equipment" @click="openAddEquipment()">
                      <DeploymentUnitOutlined /> Equipment
                    </a-menu-item>
                    <a-menu-item key="controller" @click="openAddDevice()">
                      <ApiOutlined /> Controller
                    </a-menu-item>
                  </a-menu>
                </template>
              </a-dropdown>
              <a-dropdown v-if="hasDiscoveryConnections" :trigger="['click']">
                <a-button
                  size="small"
                  :title="hasExternalDevices ? 'Rediscover' : 'Discover Devices'"
                  :loading="quickDiscoverLoading"
                >
                  Discover <DownOutlined />
                </a-button>
                <template #overlay>
                  <a-menu>
                    <a-menu-item key="all" @click="onDiscoverAllClick">
                      All Connections
                    </a-menu-item>
                    <a-menu-divider />
                    <a-menu-item
                      v-for="connection in discoveryConnections"
                      :key="connection.id"
                      @click="onDiscoverConnectionClick(connection)"
                    >
                      {{ connection.name }}
                    </a-menu-item>
                    <a-menu-divider />
                    <a-menu-item key="manage" @click="onManageDiscoveryClick">
                      Manage connections...
                    </a-menu-item>
                  </a-menu>
                </template>
              </a-dropdown>
              <a-button
                v-else
                size="small"
                :title="hasExternalDevices ? 'Rediscover' : 'Discover Devices'"
                :loading="quickDiscoverLoading"
                @click="onDiscoverClick"
              >
                Discover
              </a-button>
            </a-space>
          </div>

          <div v-if="sidebarRawCount" style="padding:8px 12px;border-bottom:1px solid var(--border)">
            <a-input
              v-model:value="deviceSearch"
              size="small"
              allow-clear
              placeholder="Search…"
            >
              <template #prefix><SearchOutlined style="color:var(--text-placeholder)" /></template>
            </a-input>
          </div>
          <div
            v-if="sidebarRawCount"
            style="height:32px;padding:4px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:flex-start"
          >
            <a-button
              size="small"
              :type="isTreeFullyExpanded ? 'primary' : 'text'"
              :ghost="isTreeFullyExpanded"
              :title="isTreeFullyExpanded ? 'Collapse tree' : 'Expand tree'"
              @click="toggleTreeExpansion"
            >
              <template #icon><ApartmentOutlined /></template>
            </a-button>
          </div>

          <div class="sidebar-tree-pane">
            <div v-if="!sidebarRawCount" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
              Nothing here yet — use "+ Add" to create a Location, Equipment, or Controller
            </div>
            <div v-else-if="!sidebarFilteredCount" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
              No devices match "{{ deviceSearch }}"
            </div>

            <a-tree
              v-else
              class="sidebar-tree"
              v-model:expanded-keys="expandedKeys"
              :tree-data="sidebarTree"
              :selected-keys="selectedTreeKeys"
              :field-names="{ children: 'children', title: 'key', key: 'key' }"
              :show-line="{ showLeafIcon: false }"
              block-node
              style="padding:6px 4px"
              @select="onTreeSelect"
            >
            <template #title="node">
              <!-- Location row -->
              <div v-if="node.kind === 'location'" style="display:flex;align-items:center;gap:6px;padding:2px 0">
                <a-tooltip :title="node.location.kind ? (locationKindLabel[node.location.kind] ?? node.location.kind) : 'Unclassified'">
                  <component :is="getLocationIcon(node.location.kind)" :style="[TREE_ICON_SLOT_STYLE, { color: '#1890ff' }]" />
                </a-tooltip>
                <span style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;font-size:12.5px">
                  {{ node.location.name }}
                </span>
                <a-space :size="0" @click.stop>
                  <a-button type="text" size="small" title="Edit" @click="openEditLocation(node.location)">
                    <template #icon><EditOutlined /></template>
                  </a-button>
                  <a-dropdown :trigger="['click']">
                    <a-button type="text" size="small" title="Add">
                      <template #icon><PlusOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu>
                        <a-menu-item key="location" @click="openAddLocation(node.location.id)">
                          <FolderAddOutlined /> Location
                        </a-menu-item>
                        <a-menu-item key="equipment" @click="openAddEquipment(node.location.id)">
                          <DeploymentUnitOutlined /> Equipment
                        </a-menu-item>
                        <a-menu-item key="controller" @click="openAddDevice(node.location.id)">
                          <ApiOutlined /> Controller
                        </a-menu-item>
                      </a-menu>
                    </template>
                  </a-dropdown>
                </a-space>
              </div>

              <!-- External BACnet device row — project-local mutations (Edit,
                   Create Simulation, Remove from Project) are allowed;
                   source mutations (BACnet writes, simulation config) never
                   are, enforced backend-side regardless of this UI. -->
                  <div v-else-if="node.kind === 'device' && node.device.source_type === 'external-bacnet'" style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <a-tooltip :title="node.device.equipment_type ? (equipmentTypeLabel[node.device.equipment_type] ?? node.device.equipment_type) : 'Unclassified device'">
                  <component :is="getControllerIcon(node.device.equipment_type)" :style="[TREE_ICON_SLOT_STYLE, { color: 'var(--text-primary)' }]" />
                </a-tooltip>
                <div style="flex:1;min-width:0">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ node.device.name }}</div>
                  <div style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-secondary)">
                    <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                      ID {{ node.device.device_instance }}
                    </span>
                    <a-tooltip :title="deviceHealthTitle(node.device)">
                      <a-badge :status="deviceHealthStatus(node.device)" style="flex-shrink:0" />
                    </a-tooltip>
                  </div>
                </div>
                <a-tag color="default" style="font-size:10px;margin:0;line-height:16px">External</a-tag>
                <a-space :size="2" @click.stop>
                  <a-dropdown :trigger="['click']">
                    <a-button type="text" size="small" title="More">
                      <template #icon><EllipsisOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu>
                        <a-menu-item-group key="move-to" title="Move to">
                          <template v-for="locationOpt in moveToOptions" :key="'move-' + locationOpt.id">
                            <a-menu-item @click.stop.prevent="assignDeviceToLocation(node.device, locationOpt.id)">
                              <span :style="{ paddingLeft: (8 * locationOpt.depth) + 'px' }">{{ locationOpt.label }}</span>
                            </a-menu-item>
                          </template>
                        </a-menu-item-group>
                        <a-menu-item key="create-simulated-copy" @click="openCreateSimulatedCopy(node.device)">
                          <CopyOutlined /> Create Simulation
                        </a-menu-item>
                        <a-menu-divider />
                        <a-menu-item key="remove" danger @click="removeExternalDevice(node.device)">
                          <DeleteOutlined /> Remove
                        </a-menu-item>
                      </a-menu>
                    </template>
                  </a-dropdown>
                </a-space>
              </div>

              <!-- Discovered group (frontend-only synthetic node) -->
              <div v-else-if="node.kind === 'discovered-group'" style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <FolderOutlined :style="[TREE_ICON_SLOT_STYLE, { color: '#faad14' }]" />
                <span style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;font-size:12.5px">Unassigned ({{ node.children?.length ?? 0 }})</span>
              </div>

              <!-- Equipment row -->
              <div v-else-if="node.kind === 'equipment'" style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <a-tooltip :title="node.equipment.equipment_type ? (equipmentTypeLabel[node.equipment.equipment_type] ?? node.equipment.equipment_type) : 'Unclassified equipment'">
                  <component :is="getEquipmentIcon(node.equipment.equipment_type)" :style="[TREE_ICON_SLOT_STYLE, { color: 'var(--text-primary)' }]" />
                </a-tooltip>
                <div style="flex:1;min-width:0">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ node.equipment.name }}</div>
                  <div style="font-size:11px;color:var(--text-secondary)">
                    {{ node.equipment.equipment_type ? (equipmentTypeLabel[node.equipment.equipment_type] ?? node.equipment.equipment_type) : 'Unclassified' }}
                  </div>
                </div>
                <a-space :size="2" @click.stop>
                  <a-button type="text" size="small" title="Edit" @click="openEditEquipment(node.equipment)">
                    <template #icon><EditOutlined /></template>
                  </a-button>
                </a-space>
              </div>

              <!-- Device row -->
              <div v-else style="display:flex;align-items:center;gap:8px;padding:2px 0">
                <a-tooltip :title="node.device.equipment_type ? (equipmentTypeLabel[node.device.equipment_type] ?? node.device.equipment_type) : 'Unclassified device'">
                  <component :is="getControllerIcon(node.device.equipment_type)" :style="[TREE_ICON_SLOT_STYLE, { color: 'var(--text-primary)' }]" />
                </a-tooltip>
                <div style="flex:1;min-width:0">
                  <div style="display:flex;align-items:center;gap:4px;overflow:hidden">
                    <span style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ node.device.name }}</span>
                    <a-tooltip v-if="node.device.simulation_mode !== 'mirror' && node.device.simulation_model_stopped" title="Simulation stopped — model disabled">
                      <a-tag color="default" style="flex-shrink:0;font-size:10px;line-height:16px;padding:0 4px;margin:0">Sim</a-tag>
                    </a-tooltip>
                    <a-tag v-else-if="node.device.simulation_mode !== 'mirror'" color="green" style="flex-shrink:0;font-size:10px;line-height:16px;padding:0 4px;margin:0">Sim</a-tag>
                    <a-tag v-if="node.device.simulation_mode === 'mirror'" color="blue" style="flex-shrink:0;font-size:10px;line-height:16px;padding:0 4px;margin:0">Twin</a-tag>
                    <a-tooltip v-if="simulationProviderLabel(node.device)" :title="simulationProviderTitle(node.device)">
                      <a-tag :color="simulationProviderColor(node.device)" style="flex-shrink:0;font-size:10px;line-height:16px;padding:0 4px;margin:0">
                        {{ simulationProviderLabel(node.device) }}
                      </a-tag>
                    </a-tooltip>
                  </div>
                  <div style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-secondary)">
                    <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                      ID {{ node.device.device_instance }}<template v-if="node.device.equipment_type"> · {{ equipmentTypeLabel[node.device.equipment_type] ?? node.device.equipment_type }}</template>
                    </span>
                    <a-tooltip :title="deviceHealthTitle(node.device)">
                      <a-badge :status="deviceHealthStatus(node.device)" style="flex-shrink:0" />
                    </a-tooltip>
                  </div>
                </div>
                <a-space :size="2" @click.stop>
                  <a-dropdown :trigger="['click']">
                    <a-button type="text" size="small" title="More">
                      <template #icon><EllipsisOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu>
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
          </div>
        </a-layout-sider>
        <div
          class="sidebar-resize-handle"
          :class="{ 'is-resizing': resizingSidebar }"
          title="Drag to resize"
          @mousedown="startSidebarResize"
        ></div>
</template>

<style scoped>
.inventory-sider {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.inventory-sider :deep(.ant-layout-sider-children) {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.sidebar-tree-pane {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: #141414;
}

.sidebar-tree {
  background: transparent;
}

.sidebar-tree :deep(.ant-tree-indent-unit::before) {
  border-color: var(--border);
}

.sidebar-tree :deep(.ant-tree-switcher-leaf-line::before),
.sidebar-tree :deep(.ant-tree-switcher-leaf-line::after) {
  border-color: var(--border);
}

.sidebar-resize-handle {
  flex-shrink: 0;
  width: 5px;
  margin-left: -3px;
  cursor: col-resize;
  background: transparent;
  position: relative;
  z-index: 1;
}

.sidebar-resize-handle::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 1px;
  background: transparent;
}

.sidebar-resize-handle:hover::after,
.sidebar-resize-handle.is-resizing::after {
  background: #1890ff;
}
</style>
