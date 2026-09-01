<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import {
  EditOutlined, CopyOutlined, LineChartOutlined, ApiOutlined, BulbOutlined, ExperimentOutlined,
  VideoCameraOutlined, DownOutlined, DownloadOutlined, UploadOutlined, AlertOutlined,
  CalendarOutlined, ScheduleOutlined, ThunderboltOutlined, ClusterOutlined, DeleteOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'
import { formatPresentValue } from '../format'
import { coerceValueForObjectType } from '../objectValue'
import type { Device, SimObject, Meta, PointRef, Location } from '../types'
import { buildLocationTreeOptions, flattenLocationTree } from '../locationTree'
import GridFilterToolbar from './GridFilterToolbar.vue'
import ObjectDrawer from './ObjectDrawer.vue'
import SaveTemplateModal from './SaveTemplateModal.vue'
import TemplatePickerModal from './TemplatePickerModal.vue'
import CustomGraphModal from './CustomGraphModal.vue'
import SemanticSuggestionsModal from './SemanticSuggestionsModal.vue'

const props = defineProps<{
  device: Device
  devices: Device[]
  meta: Meta
  locations: Location[]
  /** WS-driven live values for simulated devices, owned by App.vue — read
   * here, never written. External devices never touch this map; they use
   * their own locally-polled externalLiveValues instead (see below). */
  liveValues: Record<number, number | boolean>
  /** WS-driven shadow model values. On Twin devices this is intentionally
   * separate from liveValues so external/source values remain authoritative. */
  modelValues: Record<number, number | boolean>
  modelStates: Record<number, string>
}>()

// "Move to" submenu options for the external-device "More actions" menu --
// same computation LeftSideView.vue's own moveToOptions used before this
// menu moved here.
const moveToOptions = computed(() => flattenLocationTree(buildLocationTreeOptions(props.locations)))

// Fired after Suggest Semantics applies a device-level (equipment_type)
// change, so App.vue can refresh its devices list — the tree row's
// equipment-type label lives there, outside this component.
const emit = defineEmits<{
  'device-updated': []
  'external-device-seen': [deviceId: number, seenAt: string]
  'edit-device': [device: Device]
  'simulation-model': [device: Device]
  'replay-recordings': [device: Device]
  // "More actions" menu (moved here from LeftSideView.vue's sidebar "..."
  // dropdown -- same items/icons/groups, same App.vue handlers, just
  // triggered from the header instead of the tree row).
  'duplicate-device': [device: Device]
  'export-ede': [device: Device]
  'import-ede': [device: Device]
  'export-brick': [device: Device]
  'notification-classes': [device: Device]
  'event-enrollments': [device: Device]
  'trend-logs': [device: Device]
  'replay-playback': [device: Device]
  'calibration': [device: Device]
  'schedules': [device: Device]
  'calendars': [device: Device]
  'energy-model': [device: Device]
  'view-traffic': [device: Device]
  // External-device "More actions" menu (moved here from LeftSideView.vue's
  // sidebar "..." dropdown, same as above).
  'create-simulated-copy': [device: Device]
  'assign-device-location': [device: Device, locationId: number]
  'remove-external-device': [device: Device]
}>()

const isExternal = computed(() => props.device.source_type === 'external-bacnet')
const isMirror = computed(() => props.device.simulation_mode === 'mirror')
const isReplay = computed(() => props.device.simulation_mode === 'replay')
const replayLabel = computed(() =>
  props.device.active_replay_recording ? `Replay: ${props.device.active_replay_recording.name}` : 'Replay')
const isSimulationMode = computed(() => !isExternal.value && !isMirror.value && !isReplay.value)
const canConfigureSimulation = computed(() => !isExternal.value)
// Calibration needs a known FMU model to calibrate against -- gated on the
// device's own active_simulation_model (already server-computed and present
// on every device row) rather than a separate fetch just to check this.
const calibrationModel = computed(() => {
  const model = props.device.active_simulation_model
  return model && model.provider_type === 'fmu' ? model : null
})
// Strips the "{device name} " prefix that SimulationModelDrawer.vue's
// buildPayload() always names a model with (`${device.name} ${model.label}`),
// recovering just the model's own label for display on the badge -- falls
// back to the full stored name for a model renamed away from that
// convention rather than guessing at one.
const simulationModelDisplayName = computed(() => {
  const model = props.device.active_simulation_model
  if (!model) return null
  const prefix = `${props.device.name} `
  return model.name.startsWith(prefix) ? model.name.slice(prefix.length) : model.name
})
const simulationProviderLabel = computed(() => {
  const provider = props.device.active_simulation_model?.provider_type
  if (!provider) return null
  if (provider === 'fmu') return simulationModelDisplayName.value ? `FMU: ${simulationModelDisplayName.value}` : 'FMU'
  return provider
})
const simulationProviderColor = computed(() => {
  const provider = props.device.active_simulation_model?.provider_type
  if (provider === 'fmu') return 'purple'
  if (provider === 'learned') return 'cyan'
  return 'default'
})
const simulationProviderTitle = computed(() => {
  const model = props.device.active_simulation_model
  if (!model) return ''
  const suffix = model.model_count && model.model_count > 1
    ? ` plus ${model.model_count - 1} more`
    : ''
  return `${model.name} (${model.model_type})${suffix}`
})
const mirrorSourceName = computed(() => {
  const sourceId = props.device.source_device_id
  if (sourceId == null) return 'source'
  return props.devices.find(d => d.id === sourceId)?.name ?? `source device ${sourceId}`
})

function openSimulationModel() {
  emit('simulation-model', props.device)
}

function openEditDevice() {
  emit('edit-device', props.device)
}

// ─── Object list ──────────────────────────────────────────────────────────

const objects = ref<SimObject[]>([])
const loading = ref(false)
const hasDiscovered = computed(() => objects.value.length > 0)

async function loadObjects() {
  // Deliberately does NOT clear `objects` first -- this is also called for
  // a plain in-place refresh after saving/duplicating/toggling an object on
  // the SAME device (see onObjectSaved() etc. below), where clearing first
  // makes the whole table flash empty for the ~1s round-trip before
  // repopulating. The one case that genuinely needs a clear-first (avoiding
  // a flash of the PREVIOUS device's stale rows while switching devices) is
  // handled explicitly in the props.device.id watcher instead.
  loading.value = true
  try {
    objects.value = await api.objects.list(props.device.id)
  } catch { /* swallow */ } finally {
    loading.value = false
  }
}

// Exposed so App.vue can refresh this panel after an action that mutates
// this device's objects from outside it (e.g. tree-dropdown "Import EDE").
defineExpose({ reload: loadObjects })

// ─── External live values: polling only the currently-shown device,
// entirely independent of the simulation loop/WS. Starts/stops for free
// via this component's own lifecycle as `device` changes or it unmounts —
// no manual "cancel on navigate away" bookkeeping needed. ───────────────

const externalLiveValues = ref<Record<number, unknown>>({})
const externalLastUpdatedAt = ref<number | null>(null)
let externalPollTimer: ReturnType<typeof setInterval> | null = null

function stopExternalPolling() {
  if (externalPollTimer) { clearInterval(externalPollTimer); externalPollTimer = null }
}

async function pollExternalValues() {
  try {
    const result = await api.externalObjects.refresh(props.device.id)
    const map: Record<number, unknown> = {}
    for (const o of result.objects) map[o.id] = o.present_value
    externalLiveValues.value = map
    externalLastUpdatedAt.value = Date.now()
    emit('external-device-seen', props.device.id, new Date().toISOString())
  } catch { /* transient poll failure — try again next tick */ }
}

function startExternalPolling() {
  stopExternalPolling()
  externalLiveValues.value = {}
  externalLastUpdatedAt.value = null
  if (!isExternal.value) return
  pollExternalValues()
  externalPollTimer = setInterval(pollExternalValues, 3000)
}

watch(() => props.device.id, () => {
  loadObjects()
  startExternalPolling()
}, { immediate: true })

const secondsSinceUpdate = ref(0)
const tickTimer = setInterval(() => {
  secondsSinceUpdate.value = externalLastUpdatedAt.value ? Math.round((Date.now() - externalLastUpdatedAt.value) / 1000) : 0
}, 1000)

onUnmounted(() => {
  stopExternalPolling()
  clearInterval(tickTimer)
})

const refreshing = ref(false)
async function manualRefresh() {
  refreshing.value = true
  try { await pollExternalValues() } finally { refreshing.value = false }
}

async function discoverObjects() {
  loading.value = true
  try {
    const result = await api.externalObjects.discover(props.device.id)
    objects.value = result.objects
    const map: Record<number, unknown> = {}
    for (const o of result.objects) map[o.id] = o.present_value
    externalLiveValues.value = map
    externalLastUpdatedAt.value = Date.now()
    emit('external-device-seen', props.device.id, new Date().toISOString())
    message.success(result.objects.length
      ? `Discovered ${result.objects.length} object${result.objects.length !== 1 ? 's' : ''}`
      : 'No objects found')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Object discovery failed')
  } finally {
    loading.value = false
  }
}

// ─── Live value helpers — single source per column, branching once on
// isExternal rather than tracking per-object source. ─────────────────────

function liveVal(id: number): number | boolean | null {
  const v = isExternal.value ? externalLiveValues.value[id] : props.liveValues[id]
  return v !== undefined ? (v as number | boolean) : null
}
function hasLive(id: number): boolean {
  return isExternal.value ? externalLiveValues.value[id] !== undefined : props.liveValues[id] !== undefined
}
function fmtVal(obj: SimObject): string {
  return formatPresentValue(obj.object_type, liveVal(obj.id))
}
function modelVal(id: number): number | boolean | null {
  const v = props.modelValues[id]
  return v !== undefined ? v : null
}
function hasModel(id: number): boolean {
  return props.modelValues[id] !== undefined
}
function modelState(id: number): string | null {
  return props.modelStates[id] ?? null
}
function isModelWarming(obj: SimObject): boolean {
  const state = modelState(obj.id)
  return state === 'WARMING_UP'
}
function fmtModelVal(obj: SimObject): string {
  if (isModelWarming(obj)) return 'Warming up…'
  return hasModel(obj.id) ? formatPresentValue(obj.object_type, modelVal(obj.id)) : '—'
}
function deltaVal(obj: SimObject): number | null {
  const live = liveVal(obj.id)
  const model = modelVal(obj.id)
  if (typeof live !== 'number' || typeof model !== 'number') return null
  return model - live
}
function fmtDelta(obj: SimObject): string {
  if (isModelWarming(obj)) return '—'
  const delta = deltaVal(obj)
  if (delta === null) return '—'
  const prefix = delta > 0 ? '+' : ''
  return `${prefix}${delta.toFixed(2)}`
}

// ─── Table ────────────────────────────────────────────────────────────────

const BEHAVIOR_COLOR: Record<string, string> = {
  constant: 'default', sine: 'blue', noise: 'orange', random_walk: 'purple', manual: 'red',
  schedule: 'cyan', ramp: 'green', fault: 'volcano', raw: 'purple',
}

const pointTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of props.meta.point_types) map[o.value] = o.label
  return map
})

function compareText(a: string | null | undefined, b: string | null | undefined): number {
  return (a ?? '').localeCompare(b ?? '', undefined, { numeric: true, sensitivity: 'base' })
}

function sortableLiveValue(obj: SimObject): number {
  const value = liveVal(obj.id)
  if (value === null) return Number.NEGATIVE_INFINITY
  if (typeof value === 'boolean') return value ? 1 : 0
  const numericValue = Number(value)
  return Number.isNaN(numericValue) ? Number.NEGATIVE_INFINITY : numericValue
}

const objectSearch = ref('')
const objectTypeFilter = ref<string | undefined>(undefined)
const objectTypeOptions = computed(() => (
  [...new Set(objects.value.map(o => o.object_type))]
    .sort((a, b) => compareText(a, b))
    .map(t => ({ value: t, label: t }))
))

const filteredObjects = computed(() => {
  const q = objectSearch.value.trim().toLowerCase()
  return objects.value.filter(o => {
    if (objectTypeFilter.value && o.object_type !== objectTypeFilter.value) return false
    if (!q) return true
    return o.name.toLowerCase().includes(q) || String(o.object_instance).includes(q)
  })
})

const objectFiltersActive = computed(() => !!objectSearch.value.trim() || objectTypeFilter.value !== undefined)
function clearObjectFilters() {
  objectSearch.value = ''
  objectTypeFilter.value = undefined
}

watch(objectTypeOptions, (options) => {
  if (
    objectTypeFilter.value !== undefined
    && !options.some(o => o.value === objectTypeFilter.value)
  ) {
    objectTypeFilter.value = undefined
  }
})

const columns = computed<TableColumnsType<SimObject>>(() => {
  const baseColumns: TableColumnsType<SimObject> = [
    { title: 'Name', dataIndex: 'name', key: 'name', sorter: (a, b) => compareText(a.name, b.name), sortDirections: ['ascend', 'descend'] },
    { title: 'Type', key: 'type', width: 170, sorter: (a, b) => compareText(a.object_type, b.object_type), sortDirections: ['ascend', 'descend'] },
    { title: 'Inst.', dataIndex: 'object_instance', key: 'instance', width: 65, sorter: (a, b) => a.object_instance - b.object_instance, sortDirections: ['ascend', 'descend'] },
    { title: 'Behavior', key: 'behavior', width: 120, sorter: (a, b) => compareText(a.behavior, b.behavior), sortDirections: ['ascend', 'descend'] },
    {
      title: 'Type', key: 'point_type', width: 190,
      sorter: (a, b) => compareText(
        a.point_type ? pointTypeLabel.value[a.point_type] ?? a.point_type : '',
        b.point_type ? pointTypeLabel.value[b.point_type] ?? b.point_type : '',
      ),
      sortDirections: ['ascend', 'descend'],
    },
    { title: 'Units', dataIndex: 'units', key: 'units', width: 150, sorter: (a, b) => compareText(a.units === 'no-units' ? '' : a.units, b.units === 'no-units' ? '' : b.units), sortDirections: ['ascend', 'descend'] },
    { title: isMirror.value ? 'Live' : 'Value', key: 'value', width: isMirror.value ? 95 : 110, sorter: (a, b) => sortableLiveValue(a) - sortableLiveValue(b), sortDirections: ['ascend', 'descend'] },
  ]
  if (isMirror.value) {
    baseColumns.push(
      { title: 'Model', key: 'model_value', width: 105 },
      { title: 'Δ', key: 'delta', width: 75 },
    )
  }
  baseColumns.push(
    { title: 'On', key: 'enabled', width: 50, sorter: (a, b) => Number(Boolean(a.enabled)) - Number(Boolean(b.enabled)), sortDirections: ['ascend', 'descend'] },
    { title: '', key: 'actions', width: 200 },
  )
  return baseColumns
})

// ─── Object drawer / actions (simulated devices only — the actions column
// is empty for external, so these are unreachable there, but the guards
// stay explicit rather than relying on that alone). ───────────────────────

const objectDrawerOpen = ref(false)
const editingObject = ref<SimObject | null>(null)
function openAddObject() { editingObject.value = null; objectDrawerOpen.value = true }
function openEditObject(obj: SimObject) { editingObject.value = obj; objectDrawerOpen.value = true }
async function onObjectSaved() { await loadObjects() }

async function duplicateObject(obj: SimObject) {
  const nextInstance = objects.value.length
    ? Math.max(...objects.value.map(o => o.object_instance)) + 1
    : obj.object_instance + 1
  try {
    await api.objects.create(props.device.id, {
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

async function toggleObjectEnabled(obj: SimObject) {
  const nextEnabled = obj.enabled ? 0 : 1
  try {
    await api.objects.update(props.device.id, obj.id, {
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

// ─── Custom Graph (History) ─────────────────────────────────────────────

const graphModalOpen = ref(false)
const graphInitialPoint = ref<PointRef | null>(null)

function openHistory(obj: SimObject) {
  graphInitialPoint.value = { device_id: props.device.id, object_id: obj.id }
  graphModalOpen.value = true
}

// ─── Set value ────────────────────────────────────────────────────────────

const setValOpen = ref(false)
const setValObj = ref<SimObject | null>(null)
const setValInput = ref(0)
const setValActive = ref(true)
const setValLoading = ref(false)
const setValIsBinary = computed(() => setValObj.value?.object_type.startsWith('binary') ?? false)

function openSetValue(obj: SimObject) {
  setValObj.value = obj
  const current = liveVal(obj.id)
  if (obj.object_type.startsWith('binary')) {
    setValActive.value = coerceValueForObjectType(obj.object_type, current) as boolean
  } else {
    setValInput.value = coerceValueForObjectType(obj.object_type, current, obj.number_of_states) as number
  }
  setValOpen.value = true
}
async function doSetValue() {
  if (!setValObj.value) return
  setValLoading.value = true
  try {
    const value = setValIsBinary.value ? setValActive.value : setValInput.value
    await api.objects.setValue(props.device.id, setValObj.value.id, value)
    setValOpen.value = false
    message.success('Value updated')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    setValLoading.value = false
  }
}

// ─── Templates (simulated devices only) ───────────────────────────────────

const templateModalOpen = ref(false)
const saveTemplateOpen = ref(false)

// ─── Suggest Semantics — available regardless of source_type ──────────────

const suggestModalOpen = ref(false)
function onSuggestionsApplied() {
  loadObjects()
  emit('device-updated')
}

async function mirrorBehaviorGuard(): Promise<boolean> {
  return new Promise((resolve) => {
    Modal.confirm({
      title: 'Switch to Simulation mode?',
      content: 'This device uses Twin mode. Changing a point behavior will switch the device to Simulation mode and activate its point behaviors.',
      okText: 'Switch to Simulation',
      cancelText: 'Cancel',
      onOk: async () => {
        try {
          const { id, ...rest } = props.device as any
          await api.devices.update(id, { ...rest, simulation_mode: 'simulation' })
          emit('device-updated')
          resolve(true)
        } catch (e: unknown) {
          message.error((e as Error).message ?? 'Failed to switch to Simulation mode')
          resolve(false)
        }
      },
      onCancel: () => resolve(false),
    })
  })
}
</script>

<template>
  <div style="margin-bottom:16px">
    <div style="font-size:18px;font-weight:600">
      {{ device.name }}
      <a-tag v-if="isExternal" color="default" style="margin-left:8px;font-weight:normal">Read Only</a-tag>
      <a-tag v-if="isMirror" color="blue" style="margin-left:8px;font-weight:normal">Twin</a-tag>
      <a-tag v-if="isReplay" color="purple" style="margin-left:8px;font-weight:normal">{{ replayLabel }}</a-tag>
      <a-tooltip v-if="isSimulationMode && device.simulation_model_stopped" title="Simulation stopped — model disabled">
        <a-tag color="default" style="margin-left:8px;font-weight:normal">Sim</a-tag>
      </a-tooltip>
      <a-tag v-else-if="isSimulationMode" color="green" style="margin-left:8px;font-weight:normal">Sim</a-tag>
      <a-tooltip v-if="simulationProviderLabel" :title="simulationProviderTitle">
        <a-tag :color="simulationProviderColor" style="margin-left:8px;font-weight:normal">{{ simulationProviderLabel }}</a-tag>
      </a-tooltip>
    </div>
    <div style="font-size:12px;color:var(--text-secondary);margin-top:3px">
      <template v-if="isExternal">
        Instance: {{ device.device_instance }}
        <template v-if="device.external_host"> · Host: {{ device.external_host }}</template>
        <template v-if="device.vendor_name && device.vendor_name !== 'Unknown'"> · Vendor: {{ device.vendor_name }}</template>
      </template>
      <template v-else>
        Device {{ device.device_instance }}
        <template v-if="device.description"> — {{ device.description }}</template>
        <template v-else> — {{ device.model_name }}</template>
        <template v-if="isMirror"> · Source: {{ mirrorSourceName }}</template>
      </template>
    </div>
  </div>

  <div v-if="isExternal && !hasDiscovered && !loading" style="padding:40px 0;text-align:center;color:var(--text-placeholder)">
    <div style="margin-bottom:12px">No objects discovered yet.</div>
    <a-space>
      <a-button type="primary" ghost :loading="loading" @click="discoverObjects">
        <template #icon><ApiOutlined /></template>
        Discover Objects
      </a-button>
      <a-button @click="emit('create-simulated-copy', device)">
        <template #icon><CopyOutlined /></template>
        Create Simulation
      </a-button>
    </a-space>
  </div>

  <template v-else>
    <GridFilterToolbar
      v-model:search="objectSearch"
      search-placeholder="Search objects…"
      :can-clear="objectFiltersActive"
      @clear="clearObjectFilters"
    >
      <a-select
        v-model:value="objectTypeFilter"
        allow-clear
        size="small"
        placeholder="Object Type"
        style="width:170px"
        :options="objectTypeOptions"
      />

      <template #actions>
        <template v-if="canConfigureSimulation">
          <a-button @click="openEditDevice">
            <template #icon><EditOutlined /></template>
            Edit
          </a-button>
          <a-button @click="openSimulationModel">
            <template #icon><ExperimentOutlined /></template>
            Simulation Model
          </a-button>
          <a-button :disabled="!objects.length" @click="saveTemplateOpen = true">Save as Template</a-button>
          <a-button @click="templateModalOpen = true">From Template</a-button>
          <a-button :disabled="!objects.length" @click="suggestModalOpen = true">
            <template #icon><BulbOutlined /></template>
            Suggest Semantics
          </a-button>
          <a-dropdown :trigger="['click']">
            <a-button>
              More actions
              <template #icon><DownOutlined /></template>
            </a-button>
            <template #overlay>
              <a-menu>
                <a-menu-item key="duplicate" @click="emit('duplicate-device', device)">
                  <CopyOutlined /> Duplicate
                </a-menu-item>
                <a-menu-divider />
                <a-menu-item key="export-ede" @click="emit('export-ede', device)">
                  <DownloadOutlined /> Export EDE
                </a-menu-item>
                <a-menu-item key="import-ede" @click="emit('import-ede', device)">
                  <UploadOutlined /> Import EDE
                </a-menu-item>
                <a-menu-item key="export-brick" @click="emit('export-brick', device)">
                  <DownloadOutlined /> Export Brick Schema (.ttl)
                </a-menu-item>
                <a-menu-divider />
                <a-menu-item key="notification-classes" @click="emit('notification-classes', device)">
                  <AlertOutlined /> Notification Classes
                </a-menu-item>
                <a-menu-item key="event-enrollments" @click="emit('event-enrollments', device)">
                  <AlertOutlined /> Event Enrollments
                </a-menu-item>
                <a-menu-item key="trend-logs" @click="emit('trend-logs', device)">
                  <LineChartOutlined /> Trend Logs
                </a-menu-item>
                <a-menu-item key="recordings" @click="emit('replay-recordings', device)">
                  <VideoCameraOutlined /> Record
                </a-menu-item>
                <a-menu-item
                  key="calibration"
                  :disabled="!calibrationModel"
                  @click="calibrationModel && emit('calibration', device)"
                >
                  <a-tooltip :title="calibrationModel ? undefined : 'Attach a Simulation Model first'" placement="left">
                    <ExperimentOutlined /> Calibration
                  </a-tooltip>
                </a-menu-item>
                <a-menu-item v-if="isReplay" key="replay-playback" @click="emit('replay-playback', device)">
                  <VideoCameraOutlined /> Replay Playback
                </a-menu-item>
                <a-menu-item key="schedules" @click="emit('schedules', device)">
                  <CalendarOutlined /> Schedules
                </a-menu-item>
                <a-menu-item key="calendars" @click="emit('calendars', device)">
                  <ScheduleOutlined /> Calendars
                </a-menu-item>
                <a-menu-divider />
                <a-menu-item key="energy-model" @click="emit('energy-model', device)">
                  <ThunderboltOutlined /> Energy Model
                </a-menu-item>
                <a-menu-item key="view-traffic" @click="emit('view-traffic', device)">
                  <ClusterOutlined /> View Traffic
                </a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
          <a-button type="primary" @click="openAddObject">+ Add Object</a-button>
        </template>
        <template v-else>
          <span v-if="externalLastUpdatedAt" style="font-size:11px;color:var(--text-placeholder);margin-right:4px">
            Live · updated {{ secondsSinceUpdate }}s ago
          </span>
          <a-button @click="openEditDevice">
            <template #icon><EditOutlined /></template>
            Edit
          </a-button>
          <a-button :loading="refreshing" @click="manualRefresh">Refresh</a-button>
          <a-button @click="emit('create-simulated-copy', device)">
            <template #icon><CopyOutlined /></template>
            Create Simulation
          </a-button>
          <a-button :disabled="!objects.length" @click="suggestModalOpen = true">
            <template #icon><BulbOutlined /></template>
            Suggest Semantics
          </a-button>
          <a-button :loading="loading" @click="discoverObjects">
            <template #icon><ApiOutlined /></template>
            Rediscover Objects
          </a-button>
          <a-dropdown :trigger="['click']">
            <a-button>
              More actions
              <template #icon><DownOutlined /></template>
            </a-button>
            <template #overlay>
              <a-menu>
                <a-menu-item key="recordings" @click="emit('replay-recordings', device)">
                  <VideoCameraOutlined /> Record
                </a-menu-item>
                <a-menu-divider />
                <a-menu-item-group key="move-to" title="Move to">
                  <template v-for="locationOpt in moveToOptions" :key="'move-' + locationOpt.id">
                    <a-menu-item @click.stop.prevent="emit('assign-device-location', device, locationOpt.id)">
                      <span :style="{ paddingLeft: (8 * locationOpt.depth) + 'px' }">{{ locationOpt.label }}</span>
                    </a-menu-item>
                  </template>
                </a-menu-item-group>
                <a-menu-divider />
                <a-menu-item key="remove" danger @click="emit('remove-external-device', device)">
                  <DeleteOutlined /> Remove
                </a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </template>
      </template>
    </GridFilterToolbar>

    <a-table
      :data-source="filteredObjects"
      :columns="columns"
      :loading="loading"
      :pagination="false"
      :show-sorter-tooltip="false"
      size="small"
      row-key="id"
    >
      <template #emptyText>
        <div v-if="objects.length" style="padding:24px;color:var(--text-placeholder)">
          No objects match your filters —
          <a @click="clearObjectFilters">clear filters</a>
        </div>
        <div v-else style="padding:24px;color:var(--text-placeholder)">
          {{ canConfigureSimulation ? 'No objects yet — click Add Object' : 'No objects discovered yet — click Rediscover Objects' }}
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'type'">
          <a-tag style="font-family:monospace;font-size:11px">{{ (record as SimObject).object_type }}</a-tag>
        </template>
        <template v-else-if="column.key === 'behavior'">
          <a-tooltip v-if="(record as SimObject).simulation_output_owner" :title="`Driven by ${(record as SimObject).simulation_output_owner!.name} / ${(record as SimObject).simulation_output_owner!.variable}`">
            <a-tag :color="BEHAVIOR_COLOR[(record as SimObject).behavior]">{{ (record as SimObject).behavior }}</a-tag>
          </a-tooltip>
          <a-tag v-else-if="canConfigureSimulation && !isMirror" :color="BEHAVIOR_COLOR[(record as SimObject).behavior]">{{ (record as SimObject).behavior }}</a-tag>
          <span v-else style="color:var(--text-disabled)">—</span>
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
        <template v-else-if="column.key === 'model_value'">
          <span
            :style="{
              fontFamily:'monospace',
              color: isModelWarming(record as SimObject)
                ? '#faad14'
                : hasModel((record as SimObject).id)
                  ? '#722ed1'
                  : 'var(--text-disabled)'
            }"
          >
            {{ fmtModelVal(record as SimObject) }}
          </span>
        </template>
        <template v-else-if="column.key === 'delta'">
          <span :style="{ fontFamily:'monospace', color: deltaVal(record as SimObject) === null ? 'var(--text-disabled)' : 'var(--text-secondary)' }">
            {{ fmtDelta(record as SimObject) }}
          </span>
        </template>
        <template v-else-if="column.key === 'enabled'">
          <a-switch
            v-if="canConfigureSimulation"
            size="small"
            :checked="!!(record as SimObject).enabled"
            @change="toggleObjectEnabled(record as SimObject)"
          />
          <span v-else style="color:var(--text-disabled)">—</span>
        </template>
        <template v-else-if="column.key === 'actions' && canConfigureSimulation">
          <a-space :size="2">
            <a-button type="text" size="small" title="Edit" @click.stop="openEditObject(record as SimObject)">
              <template #icon><EditOutlined /></template>
            </a-button>
            <a-button type="text" size="small" title="Duplicate" @click.stop="duplicateObject(record as SimObject)">
              <template #icon><CopyOutlined /></template>
            </a-button>
            <a-button
              v-if="!(record as SimObject).simulation_output_owner && (record as SimObject).behavior === 'manual'"
              type="link" size="small"
              style="color:#fa8c16"
              @click="openSetValue(record as SimObject)"
            >Set</a-button>
            <a-button type="link" size="small" style="color:#722ed1" @click="openHistory(record as SimObject)">
              <template #icon><LineChartOutlined /></template>
            </a-button>
          </a-space>
        </template>
      </template>
    </a-table>
  </template>

  <!-- Object drawer -->
  <ObjectDrawer
    v-model:open="objectDrawerOpen"
    :object="editingObject"
    :device-id="device.id"
    :meta="meta"
    :existing-objects="objects"
    :mirror-mode="isMirror"
    :on-before-save-behavior-change="isMirror ? mirrorBehaviorGuard : undefined"
    @saved="onObjectSaved"
  />

  <!-- Save as template -->
  <SaveTemplateModal
    v-model:open="saveTemplateOpen"
    :objects="objects"
    :device-id="device.id"
    :device-name="device.name"
    :meta="meta"
  />

  <!-- Template picker -->
  <TemplatePickerModal
    v-model:open="templateModalOpen"
    :device-id="device.id"
    :vendor-name="device.vendor_name"
    :model-name="device.model_name"
    @applied="loadObjects"
  />

  <!-- Suggest Semantics -->
  <SemanticSuggestionsModal
    v-model:open="suggestModalOpen"
    :device="device"
    :objects="objects"
    :meta="meta"
    @applied="onSuggestionsApplied"
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

  <!-- Custom Graph (History) modal -->
  <CustomGraphModal
    v-model:open="graphModalOpen"
    :meta="props.meta"
    :initial-point="graphInitialPoint"
  />
</template>
