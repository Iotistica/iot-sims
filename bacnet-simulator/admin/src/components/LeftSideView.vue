<script setup lang="ts">
import { computed } from 'vue'
import {
  ApiOutlined, CopyOutlined, DeleteOutlined, DeploymentUnitOutlined,
  DownloadOutlined, EditOutlined, EllipsisOutlined, FolderAddOutlined,
  FolderOutlined, LineChartOutlined, PlusOutlined, SearchOutlined,
  AlertOutlined, CalendarOutlined, ScheduleOutlined, ThunderboltOutlined,
  ExperimentOutlined, ClusterOutlined, UploadOutlined,
} from '@ant-design/icons-vue'
import type { Device, Equipment, Location, Meta } from '../types'
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
  hasRememberedConnection?: boolean
  width?: number
}>(), {
  quickDiscoverLoading: false,
  hasRememberedConnection: false,
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
  'edit-discovery': []
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
}>()

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
const openEditDevice = (v: Device) => emit('edit-device', v)
const onDiscoverClick = () => emit('discover')
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
</script>

<template>
        <!-- Sidebar: devices -->
        <a-layout-sider :width="width" style="background:var(--surface);border-right:1px solid var(--border);overflow:auto">
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
              <a-button
                size="small"
                :title="hasExternalDevices ? 'Rediscover' : 'Discover Devices'"
                :loading="quickDiscoverLoading"
                @click="onDiscoverClick"
              >
                Discover
              </a-button>
              <a-button
                v-if="hasRememberedConnection"
                size="small"
                title="Edit discovery connection"
                @click="$emit('edit-discovery')"
              >
                <template #icon><EditOutlined /></template>
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

          <div v-if="!sidebarRawCount" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
            Nothing here yet — use "+ Add" to create a Location, Equipment, or Controller
          </div>
          <div v-else-if="!sidebarFilteredCount" style="padding:24px 16px;color:var(--text-placeholder);text-align:center;font-size:13px">
            No devices match "{{ deviceSearch }}"
          </div>

          <a-tree
            v-else
            v-model:expanded-keys="expandedKeys"
            :tree-data="sidebarTree"
            :selected-keys="selectedTreeKeys"
            :field-names="{ children: 'children', title: 'key', key: 'key' }"
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
                  <div style="font-size:11px;color:var(--text-secondary)">
                    ID {{ node.device.device_instance }}
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
                        <a-menu-item key="edit" @click="openEditDevice(node.device)">
                          <EditOutlined /> Edit
                        </a-menu-item>
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
                    <a-tag v-if="node.device.simulation_mode === 'mirror'" color="blue" style="flex-shrink:0;font-size:10px;line-height:16px;padding:0 4px;margin:0">Mirror</a-tag>
                  </div>
                  <div style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-secondary)">
                    <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                      ID {{ node.device.device_instance }}<template v-if="node.device.equipment_type"> · {{ equipmentTypeLabel[node.device.equipment_type] ?? node.device.equipment_type }}</template>
                    </span>
                    <a-badge :status="node.device.enabled ? 'success' : 'default'" style="flex-shrink:0" />
                  </div>
                </div>
                <a-space :size="2" @click.stop>
                  <a-dropdown :trigger="['click']">
                    <a-button type="text" size="small" title="More">
                      <template #icon><EllipsisOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu>
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
                        <a-menu-item
                          v-if="node.device.source_type !== 'external-bacnet'"
                          key="simulation-model"
                          @click="openSimulationModel(node.device)"
                        >
                          <ExperimentOutlined /> Simulation Model
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
</template>
