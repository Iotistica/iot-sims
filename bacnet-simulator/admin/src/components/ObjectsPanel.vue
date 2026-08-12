<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { EditOutlined, CopyOutlined, LineChartOutlined, ApiOutlined, BulbOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import { formatPresentValue } from '../format'
import { coerceValueForObjectType } from '../objectValue'
import type { Device, SimObject, Meta, HistoryPoint } from '../types'
import GridFilterToolbar from './GridFilterToolbar.vue'
import ObjectDrawer from './ObjectDrawer.vue'
import SaveTemplateModal from './SaveTemplateModal.vue'
import TemplatePickerModal from './TemplatePickerModal.vue'
import HistoryChart from './HistoryChart.vue'
import SemanticSuggestionsModal from './SemanticSuggestionsModal.vue'

const props = defineProps<{
  device: Device
  meta: Meta
  /** WS-driven live values for simulated devices, owned by App.vue — read
   * here, never written. External devices never touch this map; they use
   * their own locally-polled externalLiveValues instead (see below). */
  liveValues: Record<number, number | boolean>
}>()

// Fired after Suggest Semantics applies a device-level (equipment_type)
// change, so App.vue can refresh its devices list — the tree row's
// equipment-type label lives there, outside this component.
const emit = defineEmits<{ 'device-updated': [] }>()

const isExternal = computed(() => props.device.source_type === 'external-bacnet')
const isMirror = computed(() => props.device.simulation_mode === 'mirror')
const canConfigureSimulation = computed(() => !isExternal.value)

// ─── Object list ──────────────────────────────────────────────────────────

const objects = ref<SimObject[]>([])
const loading = ref(false)
const hasDiscovered = computed(() => objects.value.length > 0)

async function loadObjects() {
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

// ─── Table ────────────────────────────────────────────────────────────────

const BEHAVIOR_COLOR: Record<string, string> = {
  constant: 'default', sine: 'blue', noise: 'orange', random_walk: 'purple', manual: 'red',
  schedule: 'cyan', ramp: 'green', fault: 'volcano',
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

const columns = computed<TableColumnsType<SimObject>>(() => [
  { title: 'Name', dataIndex: 'name', key: 'name', sorter: (a, b) => compareText(a.name, b.name), sortDirections: ['ascend', 'descend'] },
  { title: 'Type', key: 'type', width: 170, sorter: (a, b) => compareText(a.object_type, b.object_type), sortDirections: ['ascend', 'descend'] },
  { title: 'Inst.', dataIndex: 'object_instance', key: 'instance', width: 65, sorter: (a, b) => a.object_instance - b.object_instance, sortDirections: ['ascend', 'descend'] },
  { title: 'Behavior', key: 'behavior', width: 120, sorter: (a, b) => compareText(a.behavior, b.behavior), sortDirections: ['ascend', 'descend'] },
  {
    title: 'Semantic Type', key: 'point_type', width: 190,
    sorter: (a, b) => compareText(
      a.point_type ? pointTypeLabel.value[a.point_type] ?? a.point_type : '',
      b.point_type ? pointTypeLabel.value[b.point_type] ?? b.point_type : '',
    ),
    sortDirections: ['ascend', 'descend'],
  },
  { title: 'Units', dataIndex: 'units', key: 'units', width: 150, sorter: (a, b) => compareText(a.units === 'no-units' ? '' : a.units, b.units === 'no-units' ? '' : b.units), sortDirections: ['ascend', 'descend'] },
  { title: 'Live Value', key: 'value', width: 110, sorter: (a, b) => sortableLiveValue(a) - sortableLiveValue(b), sortDirections: ['ascend', 'descend'] },
  { title: 'On', key: 'enabled', width: 50, sorter: (a, b) => Number(Boolean(a.enabled)) - Number(Boolean(b.enabled)), sortDirections: ['ascend', 'descend'] },
  { title: '', key: 'actions', width: 200 },
])

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

// ─── History ──────────────────────────────────────────────────────────────

const histModalOpen = ref(false)
const histObj = ref<SimObject | null>(null)
const histData = ref<HistoryPoint[]>([])
const histLoading = ref(false)

async function openHistory(obj: SimObject) {
  histObj.value = obj
  histData.value = []
  histLoading.value = true
  histModalOpen.value = true
  try {
    histData.value = await api.objects.history(props.device.id, obj.id)
  } catch { /* swallow */ } finally {
    histLoading.value = false
  }
}
function histFmt(v: number, obj: SimObject | null): string {
  if (!obj) return v.toFixed(2)
  return formatPresentValue(obj.object_type, v)
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
      content: 'This device uses Mirror mode. Changing a point behavior will switch the device to Simulation mode and activate its point behaviors.',
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
      <a-tag v-if="isMirror" color="blue" style="margin-left:8px;font-weight:normal">Mirror</a-tag>
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
      </template>
    </div>
  </div>

  <div v-if="isExternal && !hasDiscovered && !loading" style="padding:40px 0;text-align:center;color:var(--text-placeholder)">
    <div style="margin-bottom:12px">No objects discovered yet.</div>
    <a-button type="primary" ghost :loading="loading" @click="discoverObjects">
      <template #icon><ApiOutlined /></template>
      Discover Objects
    </a-button>
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
        :options="meta.object_types.map(t => ({ value: t, label: t }))"
      />

      <template #actions>
        <template v-if="canConfigureSimulation">
          <template v-if="isMirror">
            <span style="font-size:11px;color:var(--text-placeholder);margin-right:4px">Mirror mode · values driven by source</span>
          </template>
          <a-button :disabled="!objects.length" @click="saveTemplateOpen = true">Save as Template</a-button>
          <a-button @click="templateModalOpen = true">From Template</a-button>
          <a-button :disabled="!objects.length" @click="suggestModalOpen = true">
            <template #icon><BulbOutlined /></template>
            Suggest Semantics
          </a-button>
          <a-button type="primary" @click="openAddObject">+ Add Object</a-button>
        </template>
        <template v-else>
          <span v-if="externalLastUpdatedAt" style="font-size:11px;color:var(--text-placeholder);margin-right:4px">
            Live · updated {{ secondsSinceUpdate }}s ago
          </span>
          <a-button :loading="refreshing" @click="manualRefresh">Refresh</a-button>
          <a-button :disabled="!objects.length" @click="suggestModalOpen = true">
            <template #icon><BulbOutlined /></template>
            Suggest Semantics
          </a-button>
          <a-button :loading="loading" @click="discoverObjects">
            <template #icon><ApiOutlined /></template>
            Rediscover Objects
          </a-button>
        </template>
      </template>
    </GridFilterToolbar>

    <a-table
      :data-source="filteredObjects"
      :columns="columns"
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
          <a-tag v-if="canConfigureSimulation && !isMirror" :color="BEHAVIOR_COLOR[(record as SimObject).behavior]">{{ (record as SimObject).behavior }}</a-tag>
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
              v-if="(record as SimObject).behavior === 'manual'"
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
    :device-name="device.name"
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

  <!-- History chart modal -->
  <a-modal
    v-model:open="histModalOpen"
    :title="histObj ? `${histObj.name} — History` : 'History'"
    :footer="null"
    width="680px"
    destroy-on-close
  >
    <template v-if="!histLoading && histObj">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
        <a-tag :color="BEHAVIOR_COLOR[histObj.behavior]">{{ histObj.behavior }}</a-tag>
        <span style="font-size:12px;color:var(--text-secondary)">{{ histObj.units === 'no-units' ? '' : histObj.units }}</span>
        <span style="font-size:12px;color:var(--text-placeholder);margin-left:auto">{{ histData.length }} samples</span>
      </div>
    </template>
    <HistoryChart
      :data="histData"
      :loading="histLoading"
      :format-value="(v: number) => histFmt(v, histObj)"
      empty-label="Not enough data yet — check back after a few ticks (5 s each)"
    />
  </a-modal>
</template>
