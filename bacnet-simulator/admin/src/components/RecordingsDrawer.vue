<script setup lang="ts">
import { ref, reactive, computed, watch, onUnmounted, nextTick } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, StopOutlined, DeleteOutlined, EditOutlined, PlayCircleOutlined, DownloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { SimulationModelConfig } from '../api'
import type { Device, SimObject, Recording, FaultType } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean]; 'create-replay': [recording: Recording] }>()

const list = ref<Recording[]>([])
const allObjects = ref<SimObject[]>([])
const loading = ref(false)
const saving = ref(false)
const formOpen = ref(false)
// Set while editing an existing recording (its points list is fixed --
// see update() and the "Points to Record" form-item's v-if -- so this only
// pre-fills the operational fields, never the points picker).
const editing = ref<Recording | null>(null)

// The engine tick loop (TICK_SECONDS, default 5s, adjustable in Settings)
// is what actually recomputes a simulated device's values -- sampling a
// recording faster than that just re-reads the same cached value multiple
// times (duplicate rows, not more information). Only relevant for
// non-external devices; an external-bacnet device's cadence is a real
// network poll, unrelated to this simulator's own tick rate.
const tickSeconds = ref<number | null>(null)
const isExternalDevice = computed(() => props.device?.source_type === 'external-bacnet')
const belowTickRate = computed(() =>
  !isExternalDevice.value && tickSeconds.value != null && form.sample_interval_seconds < tickSeconds.value
)

const form = reactive({
  name: '',
  description: '',
  // Structured ground-truth classification (severity) -- separate from the
  // free-text `tags` (Tags) list below. null only ever happens when opening
  // an existing pre-migration recording that was never classified (see
  // openEdit()) -- a new recording always starts on 'normal'.
  condition: 'normal' as 'normal' | 'degraded' | 'fault' | null,
  // What kind of problem, independent of condition's severity -- see
  // FaultType's own doc comment. Only shown/editable in the form below when
  // condition isn't 'normal' (nothing to classify for a normal recording),
  // but stays stored either way rather than being cleared when hidden.
  fault_type: null as FaultType | null,
  // Independent scenario/tag labels (e.g. "hot-day", "fan-test") -- purely
  // descriptive metadata (see migration 026's own docstring), not fed into
  // any training feature. Editable after the fact, unlike point_ids.
  tags: [] as string[],
  pointsMode: 'all' as 'all' | 'selected' | 'model',
  selectedPointIds: [] as number[],
  sample_interval_seconds: 5,
  maximum_samples: 1000,
  buffer_mode: 'stop' as 'overwrite' | 'stop',
})

// Inline "+ Add tag" input's own open/closed state -- separate from
// form.tags itself (the committed list). Not shown at all until clicked;
// Enter commits into form.tags and closes it, Escape/blur closes without
// committing whatever's half-typed.
const addingTag = ref(false)
const newTag = ref('')
const newTagInputRef = ref<{ focus: () => void } | null>(null)

// The device's own active Simulation Model config (mappings +
// input_exposures), fetched once per drawer-open alongside everything else
// -- feeds the "Model points" pointsMode option below. Same data
// TrainingFlow.vue's own auto-mapping already treats as authoritative
// (it's what's actually driving live output writes/input reads), reused
// here client-side rather than a new backend endpoint.
const activeModelConfig = ref<SimulationModelConfig | null>(null)

// Every point this device's active model reads from or writes to -- plain
// mappings' point_id, aggregate mappings' member point_ids + non-null
// weight_point_ids, and input_exposures' point_id. A model's own points
// aren't guaranteed to all live on the device it's attached to (e.g. an
// upstream sensor input mapped from a different device -- a normal,
// supported pattern), which is fine to record: a simulated device's
// recording samples via the engine's own get_object_value(source_object_id)
// (a plain global lookup, no device scoping -- see runtime.py's
// _sample_due_simulated_recordings), so a point on a different device
// samples exactly like one on this device. Only an EXTERNAL-BACnet device's
// recording is actually restricted to its own points (it samples over its
// own BACnet connection -- see POST .../recordings' own validation), so
// that's the one case this still filters to same-device. Deduplicated (a
// point could in principle serve more than one role).
const modelPointIds = computed<number[]>(() => {
  const config = activeModelConfig.value
  const deviceId = props.device?.id
  if (!config || deviceId == null) return []
  const restrictToOwnDevice = isExternalDevice.value
  const onThisDevice = (pointDeviceId: number | undefined) => !restrictToOwnDevice || pointDeviceId === deviceId
  const ids = new Set<number>()
  for (const m of config.mappings) {
    if ('point_ids' in m) {
      for (const id of m.point_ids) if (onThisDevice(m.point_metadata?.[id]?.device_id)) ids.add(id)
      for (const id of m.weight_point_ids ?? []) if (id != null && onThisDevice(m.point_metadata?.[id]?.device_id)) ids.add(id)
    } else if (onThisDevice(m.device_id)) {
      ids.add(m.point_id)
    }
  }
  for (const e of config.input_exposures ?? []) if (onThisDevice(e.device_id)) ids.add(e.point_id)
  return [...ids]
})

// True only for an external-BACnet device (the one case still restricted
// to same-device points, see modelPointIds above) whose model uses at
// least one point NOT on this device -- surfaced so "0 points" or a
// suspiciously small count doesn't read as a dead end with no explanation.
const modelPointsExcludedElsewhere = computed(() => {
  const config = activeModelConfig.value
  if (!config || !isExternalDevice.value) return false
  const allIds = new Set<number>()
  for (const m of config.mappings) {
    if ('point_ids' in m) {
      for (const id of m.point_ids) allIds.add(id)
      for (const id of m.weight_point_ids ?? []) if (id != null) allIds.add(id)
    } else {
      allIds.add(m.point_id)
    }
  }
  for (const e of config.input_exposures ?? []) allIds.add(e.point_id)
  return allIds.size > modelPointIds.value.length
})

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    const [recordings, objects, settings] = await Promise.all([
      api.recordings.list(props.device.id),
      api.objects.list(props.device.id),
      api.settings.get(),
    ])
    list.value = recordings
    allObjects.value = objects
    tickSeconds.value = settings.tick_seconds
    activeModelConfig.value = props.device.active_simulation_model
      ? await api.simulationModels.get(props.device.active_simulation_model.id)
      : null
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load recordings')
  } finally {
    loading.value = false
  }
}

// Polls the list while the drawer is open (not while the create/edit form
// is up, so a live refresh can't disrupt in-progress input) -- an active
// recording's sample_count/duration otherwise only ever updated on the
// next manual open, which reads as "stuck" for a recording running for
// several minutes.
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function refreshList() {
  if (!props.device || formOpen.value) return
  try {
    list.value = await api.recordings.list(props.device.id)
  } catch {
    // Polling failure is silent -- load() already surfaces the initial-load error.
  }
}

watch(() => props.open, (v) => {
  stopPolling()
  if (v) {
    load()
    pollTimer = setInterval(refreshList, 3000)
  } else {
    formOpen.value = false
    editing.value = null
  }
})
onUnmounted(stopPolling)

function resetForm() {
  Object.assign(form, {
    name: '', description: '', condition: 'normal', fault_type: null, tags: [], pointsMode: 'all', selectedPointIds: [],
    // Default to the simulation's own tick rate for a simulated device
    // (never below it) -- a faster default would just start every new
    // recording with wasted, duplicate samples. External devices keep the
    // plain 5s default since tick rate doesn't apply to them.
    sample_interval_seconds: !isExternalDevice.value && tickSeconds.value ? tickSeconds.value : 5,
    maximum_samples: 1000, buffer_mode: 'stop',
  })
  addingTag.value = false
  newTag.value = ''
}

function openAdd() {
  editing.value = null
  resetForm()
  formOpen.value = true
}

function openEdit(recording: Recording) {
  editing.value = recording
  Object.assign(form, {
    name: recording.name,
    description: recording.description,
    // Left unset (not defaulted to 'normal') for a recording that predates
    // this field -- see migration 025's own docstring: unclassified/legacy
    // stays that way unless the user explicitly picks one.
    condition: recording.condition ?? null,
    fault_type: recording.fault_type ?? null,
    tags: [...(recording.tags ?? [])],
    pointsMode: 'all', // not editable here -- see the form-item's v-if
    selectedPointIds: [],
    sample_interval_seconds: recording.sample_interval_seconds,
    maximum_samples: recording.maximum_samples,
    buffer_mode: recording.buffer_mode,
  })
  addingTag.value = false
  newTag.value = ''
  formOpen.value = true
}

// Autocomplete suggestions for "+ Add tag": a small seed set of generic
// scenario examples, unioned with every distinct tag already used across
// this device's own recordings -- reuses data already loaded for the list
// above, no new endpoint. Still plain free-text underneath (typing
// anything not listed here is accepted, and duplicates of an already-added
// tag are filtered out below), just a nudge toward reusing an existing
// label instead of a typo'd near-duplicate. Normal/Faulty deliberately
// excluded -- that distinction belongs to the Condition field above.
const tagOptions = computed(() => {
  const seen = new Set<string>(['hot-day', 'cooling', 'startup', 'fan-test'])
  for (const r of list.value) for (const t of r.tags ?? []) seen.add(t)
  for (const t of form.tags) seen.delete(t) // already added -- don't suggest it again
  return [...seen].sort().map((value) => ({ value }))
})

function removeTag(tag: string, e?: Event) {
  e?.preventDefault() // a-tag's own default close behavior would hide it independently of form.tags
  form.tags = form.tags.filter((t) => t !== tag)
}

function startAddingTag() {
  addingTag.value = true
  newTag.value = ''
  nextTick(() => newTagInputRef.value?.focus())
}

function commitTag() {
  const tag = newTag.value.trim()
  // A blank entry or one already present is silently ignored (closes the
  // input without adding anything) -- not an error worth interrupting the
  // user for, same spirit as the old free-text field never validating.
  if (tag && !form.tags.includes(tag)) form.tags.push(tag)
  newTag.value = ''
  addingTag.value = false
}

function cancelAddingTag() {
  newTag.value = ''
  addingTag.value = false
}

function objectLabel(o: SimObject): string {
  return `${o.name} (${o.object_type})`
}

// Same eligibility rule CreateSimulatedCopyModal.vue's own Replay-mode
// recording picker uses -- only a completed, non-empty recording can
// drive a Replay device.
function isReplayable(recording: Recording): boolean {
  return recording.status === 'completed' && recording.sample_count > 0
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (!editing.value && form.pointsMode === 'selected' && !form.selectedPointIds.length) {
    message.error('Choose at least one point, or switch to "All points"')
    return
  }
  if (!editing.value && form.pointsMode === 'model' && !modelPointIds.value.length) {
    message.error('This device\'s active Simulation Model has no mapped points yet')
    return
  }

  saving.value = true
  try {
    if (editing.value) {
      await api.recordings.update(editing.value.id, {
        name: form.name.trim(),
        description: form.description,
        condition: form.condition,
        fault_type: form.condition === 'normal' ? null : form.fault_type,
        tags: form.tags,
        sample_interval_seconds: form.sample_interval_seconds,
        maximum_samples: form.maximum_samples,
        buffer_mode: form.buffer_mode,
      })
      message.success('Recording updated')
    } else {
      // Starts sampling immediately -- there is no separate save-then-start step.
      await api.recordings.create(props.device.id, {
        name: form.name.trim(),
        description: form.description,
        condition: form.condition,
        fault_type: form.condition === 'normal' ? null : form.fault_type,
        tags: form.tags,
        point_ids: form.pointsMode === 'all' ? null
          : form.pointsMode === 'model' ? modelPointIds.value
          : form.selectedPointIds,
        sample_interval_seconds: form.sample_interval_seconds,
        maximum_samples: form.maximum_samples,
        buffer_mode: form.buffer_mode,
      })
      message.success('Recording started')
    }
    formOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Save failed')
  } finally {
    saving.value = false
  }
}

async function stopRecording(recording: Recording) {
  try {
    await api.recordings.stop(recording.id)
    message.success('Recording stopped')
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to stop recording')
  }
}

function confirmDelete(recording: Recording) {
  Modal.confirm({
    title: `Delete "${recording.name}"?`,
    content: 'This also deletes all of its recorded samples. Any Replay device driven by it will stop updating.',
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.recordings.del(recording.id)
        message.success('Deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Delete failed')
      }
    },
  })
}

// Raw samples export -- a plain read-only download of what was actually
// recorded (e.g. confirming a noise/sine/random_walk Behavior point really
// varied sample-to-sample rather than being captured at one stale value).
// Separate from the calibration flow entirely; just direct inspection.
const exportingSamples = ref<number | null>(null)

async function exportSamples(recording: Recording) {
  exportingSamples.value = recording.id
  try {
    await api.recordings.exportSamples(recording.id, recording.name)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to export samples')
  } finally {
    exportingSamples.value = null
  }
}

// SQLite's datetime('now') produces "YYYY-MM-DD HH:MM:SS" (UTC, no
// timezone suffix) -- not reliably parsed as-is by Date(), same
// normalization TrendLogDrawer.vue uses for its own ts columns.
function toUnixMillis(ts: string): number {
  const iso = ts.includes('T') ? ts : `${ts.replace(' ', 'T')}Z`
  return new Date(iso).getTime()
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

// "0 remaining" reads as "should be done" -- true for buffer_mode='stop'
// (it completes right when it hits 0), but misleading for 'overwrite',
// which sits at 0 forever while it keeps recording and discarding the
// oldest sample each cycle. Say that plainly instead of showing a number
// that looks stuck.
// "X/Y samples" reads as a completion fraction -- true for buffer_mode=
// 'stop' (it really is progress toward finishing), false for 'overwrite'
// (Y is a fixed window size, not a target -- it sits at "Y/Y" forever
// once full, which looks done when it isn't). Only 'stop' gets the slash;
// 'overwrite' says what's actually happening instead.
function sampleCountLabel(recording: Recording): string {
  const { sample_count: count, maximum_samples: max, buffer_mode: mode } = recording
  if (mode === 'overwrite') {
    return count < max
      ? `${count} of ${max} samples`
      : `${count} samples (keeps most recent ${max})`
  }
  const remaining = Math.max(0, max - count)
  return remaining > 0 ? `${count}/${max} samples · ${remaining} remaining` : `${count}/${max} samples`
}

// Shared label/color for the Condition tag -- used in the past-recordings
// list here and mirrored in TrainingFlow.vue's recording picker so the
// same value always reads the same way in both places.
const CONDITION_TAG: Record<'normal' | 'degraded' | 'fault', { label: string; color: string }> = {
  normal: { label: 'Normal', color: 'green' },
  degraded: { label: 'Degraded', color: 'orange' },
  fault: { label: 'Fault', color: 'red' },
}

// Same idea for Fault Type -- the second classification axis, alongside
// Condition. No color coding (unlike severity, no kind is "worse" than
// another), just a readable label per value. Broadened from an RTU-only
// cut to cover every equipment family this simulator models -- see
// schemas.py's own _RECORDING_FAULT_TYPES comment for the real-world
// grounding per group.
const FAULT_TYPE_TAG: Record<FaultType, string> = {
  sensor_bias: 'Sensor Bias',
  sensor_stuck: 'Sensor Stuck',
  actuator_stuck: 'Actuator Stuck',
  actuator_leak: 'Actuator Leak',
  control_unstable: 'Control Unstable',
  incorrect_setpoint: 'Incorrect Setpoint',
  simultaneous_heat_cool: 'Simultaneous Heat/Cool',
  filter_fouling: 'Filter Fouling',
  compressor_staging: 'Compressor Staging',
  condenser_fouling: 'Condenser Fouling',
  evaporator_fouling: 'Evaporator Fouling',
  refrigerant_overcharge: 'Refrigerant Overcharge',
  refrigerant_undercharge: 'Refrigerant Undercharge',
  refrigerant_line_restriction: 'Refrigerant Line Restriction',
  heat_exchanger_fouling: 'Heat Exchanger Fouling',
  water_flow_restriction: 'Water Flow Restriction',
  cavitation: 'Cavitation',
  ignition_failure: 'Ignition Failure',
  low_water_cutoff_fault: 'Low Water Cutoff Fault',
  belt_or_drive_fault: 'Belt/Drive Fault',
}
const FAULT_TYPE_OPTIONS = (Object.keys(FAULT_TYPE_TAG) as FaultType[]).map((value) => ({
  value, label: FAULT_TYPE_TAG[value],
}))

// General values apply regardless of equipment type; everything else is
// only offered for the equipment families it's actually grounded against
// (see schemas.py's own group comments) -- narrows the picker so a Boiler
// recording isn't offered refrigerant_overcharge, and vice versa. Purely a
// UI narrowing: the backend still accepts any of the 20 values regardless
// of device (same "not equipment-type-validated" choice as condition/
// tags), so this list only needs to stay a reasonable default, never a
// hard guarantee.
const GENERAL_FAULT_TYPES: FaultType[] = [
  'sensor_bias', 'sensor_stuck', 'actuator_stuck', 'actuator_leak',
  'control_unstable', 'incorrect_setpoint',
]
const REFRIGERATION_FAULT_TYPES: FaultType[] = [
  'compressor_staging', 'condenser_fouling', 'evaporator_fouling',
  'refrigerant_overcharge', 'refrigerant_undercharge', 'refrigerant_line_restriction',
]
const HYDRONIC_FAULT_TYPES: FaultType[] = ['heat_exchanger_fouling', 'water_flow_restriction']
const EQUIPMENT_FAULT_TYPES: Record<string, FaultType[]> = {
  Air_Handling_Unit: [...GENERAL_FAULT_TYPES, 'simultaneous_heat_cool', 'filter_fouling'],
  Rooftop_Unit: [...GENERAL_FAULT_TYPES, 'simultaneous_heat_cool', 'filter_fouling', ...REFRIGERATION_FAULT_TYPES],
  Variable_Air_Volume_Box: [...GENERAL_FAULT_TYPES, 'simultaneous_heat_cool'],
  Chiller: [...GENERAL_FAULT_TYPES, ...REFRIGERATION_FAULT_TYPES, ...HYDRONIC_FAULT_TYPES],
  Boiler: [...GENERAL_FAULT_TYPES, ...HYDRONIC_FAULT_TYPES, 'ignition_failure', 'low_water_cutoff_fault'],
  Cooling_Tower: [...GENERAL_FAULT_TYPES, ...HYDRONIC_FAULT_TYPES, 'belt_or_drive_fault'],
  Pump: [...GENERAL_FAULT_TYPES, ...HYDRONIC_FAULT_TYPES, 'cavitation', 'belt_or_drive_fault'],
  Supply_Fan: [...GENERAL_FAULT_TYPES, 'belt_or_drive_fault'],
  Return_Fan: [...GENERAL_FAULT_TYPES, 'belt_or_drive_fault'],
  Fan: [...GENERAL_FAULT_TYPES, 'belt_or_drive_fault'],
  Meter: GENERAL_FAULT_TYPES,
  Lighting_Equipment: GENERAL_FAULT_TYPES,
}
// Unset/unrecognized equipment_type -> the full list, not a guessed-narrow
// one -- same "don't guess" rule condition's own null handling follows.
const filteredFaultTypeOptions = computed(() => {
  const equipmentType = props.device?.equipment_type
  const allowed = equipmentType ? EQUIPMENT_FAULT_TYPES[equipmentType] : undefined
  return allowed ? FAULT_TYPE_OPTIONS.filter((o) => allowed.includes(o.value)) : FAULT_TYPE_OPTIONS
})

function duration(recording: Recording): string {
  const start = toUnixMillis(recording.started_at)
  const end = recording.ended_at ? toUnixMillis(recording.ended_at) : Date.now()
  return formatDuration(Math.max(0, (end - start) / 1000))
}
</script>

<template>
  <a-drawer
    :title="device ? `Recordings — ${device.name}` : 'Recordings'"
    :open="open"
    width="560"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
        Records this device's points on an interval, for later use in Replay playback or
        model calibration. Starts recording immediately.
      </div>
      <a-button type="primary" block @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Start Recording
      </a-button>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:var(--text-placeholder);padding:40px 0;font-size:13px">
          No recordings yet
        </div>
        <div
          v-for="r in list" :key="r.id"
          style="border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px;color:var(--text-primary)">
                {{ r.name }}
                <a-tag :color="r.status === 'recording' ? 'green' : 'default'" style="margin-left:6px;font-weight:normal">
                  {{ r.status === 'recording' ? 'Recording' : 'Completed' }}
                </a-tag>
                <a-tag v-if="r.condition" :color="CONDITION_TAG[r.condition].color" style="font-weight:normal">
                  {{ CONDITION_TAG[r.condition].label }}
                </a-tag>
                <a-tag v-if="r.fault_type" style="font-weight:normal">
                  {{ FAULT_TYPE_TAG[r.fault_type] }}
                </a-tag>
              </div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                Started {{ new Date(toUnixMillis(r.started_at)).toLocaleString() }} · Duration {{ duration(r) }}
              </div>
              <div style="font-size:11px;color:var(--text-secondary);margin-top:2px">
                Every {{ r.sample_interval_seconds }}s · {{ r.point_count }} point{{ r.point_count !== 1 ? 's' : '' }}
                · {{ sampleCountLabel(r) }}
              </div>
              <div v-if="r.tags?.length" style="margin-top:4px">
                <a-tag v-for="t in r.tags" :key="t" style="font-weight:normal;margin-bottom:2px">{{ t }}</a-tag>
              </div>
            </div>
            <a-space :size="4">
              <a-button v-if="r.status === 'recording'" size="small" title="Stop recording" @click="stopRecording(r)">
                <template #icon><StopOutlined /></template>
              </a-button>
              <a-button v-if="isReplayable(r)" size="small" title="Create Replay device from this recording" @click="emit('create-replay', r)">
                <template #icon><PlayCircleOutlined /></template>
              </a-button>
              <a-button v-if="r.sample_count > 0" size="small" title="Export raw samples (CSV)" :loading="exportingSamples === r.id" @click="exportSamples(r)">
                <template #icon><DownloadOutlined /></template>
              </a-button>
              <a-button size="small" title="Edit" @click="openEdit(r)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(r)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-space>
          </div>
        </div>
      </a-spin>
    </template>

    <template v-else>
      <a-form layout="vertical" :colon="false">
        <a-form-item label="Name" required>
          <a-input v-model:value="form.name" placeholder="e.g. Main-Meter Morning Cycle" />
        </a-form-item>

        <a-form-item label="Description">
          <a-input v-model:value="form.description" placeholder="Optional description" />
        </a-form-item>

        <a-form-item label="Condition">
          <a-radio-group v-model:value="form.condition">
            <a-radio-button value="normal">Normal</a-radio-button>
            <a-radio-button value="degraded">Degraded</a-radio-button>
            <a-radio-button value="fault">Fault</a-radio-button>
          </a-radio-group>
        </a-form-item>

        <a-form-item v-if="form.condition !== 'normal'" label="Fault Type">
          <a-select
            v-model:value="form.fault_type"
            allow-clear
            placeholder="What kind of problem (optional)"
            :options="filteredFaultTypeOptions"
          />
        </a-form-item>

        <a-form-item label="Tags">
          <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
            <a-tag v-for="t in form.tags" :key="t" closable @close="removeTag(t, $event)">{{ t }}</a-tag>
            <a-auto-complete
              v-if="addingTag"
              ref="newTagInputRef"
              v-model:value="newTag"
              :options="tagOptions"
              size="small"
              style="width:140px"
              placeholder="Tag name"
              @keydown.enter="commitTag"
              @keydown.esc="cancelAddingTag"
              @blur="commitTag"
            />
            <a-tag v-else style="cursor:pointer;border-style:dashed" @click="startAddingTag">
              <template #icon><PlusOutlined /></template>
              Add tag
            </a-tag>
          </div>
          <div style="font-size:12px;color:var(--text-placeholder);margin-top:4px">
            Optional, e.g. hot-day, cooling, startup, fan-test -- shown alongside this recording
            when picking it in Training.
          </div>
        </a-form-item>

        <a-form-item v-if="!editing" label="Points to Record">
          <a-radio-group v-model:value="form.pointsMode">
            <a-radio-button value="all">All points</a-radio-button>
            <a-radio-button value="selected">Selected points</a-radio-button>
            <a-tooltip :title="device?.active_simulation_model ? undefined : 'Attach a Simulation Model to this device first'">
              <a-radio-button value="model" :disabled="!device?.active_simulation_model">Model points</a-radio-button>
            </a-tooltip>
          </a-radio-group>
          <div v-if="form.pointsMode === 'model' && modelPointsExcludedElsewhere" style="font-size:12px;color:#d46b08;margin-top:6px">
            Some of this model's points are on other devices -- an external BACnet device can only
            record its own points, so those can't be included here.
          </div>
        </a-form-item>
        <div v-else style="font-size:12px;color:var(--text-muted);margin-bottom:16px">
          Points can't be changed after a recording starts ({{ editing.point_count }} point{{ editing.point_count !== 1 ? 's' : '' }}).
        </div>

        <a-form-item v-if="!editing && form.pointsMode === 'selected'" label="Points">
          <a-select
            v-model:value="form.selectedPointIds"
            mode="multiple"
            show-search
            placeholder="Choose points to record"
            :filter-option="(input: string, opt: any) => opt.label.toLowerCase().includes(input.toLowerCase())"
          >
            <a-select-option v-for="o in allObjects" :key="o.id" :value="o.id" :label="objectLabel(o)">{{ objectLabel(o) }}</a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="Recording Type">
          <a-input value="Polled" disabled />
        </a-form-item>

        <a-row :gutter="12">
          <a-col :span="12">
            <a-form-item label="Sample Interval (seconds)">
              <a-input-number v-model:value="form.sample_interval_seconds" :min="0.1" :step="1" style="width:100%" />
              <div v-if="belowTickRate" style="font-size:11px;color:#faad14;margin-top:4px">
                Below the simulation's tick rate ({{ tickSeconds }}s) — many samples will just repeat the same
                still-cached value instead of capturing something new.
              </div>
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="Maximum Samples">
              <a-input-number v-model:value="form.maximum_samples" :min="1" :max="100000" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="When Full" tooltip="Overwrite: oldest snapshot is discarded to make room. Stop: recording stops automatically.">
          <a-radio-group v-model:value="form.buffer_mode">
            <a-radio-button value="overwrite">Overwrite oldest</a-radio-button>
            <a-radio-button value="stop">Stop recording</a-radio-button>
          </a-radio-group>
        </a-form-item>
      </a-form>
    </template>

    <template #footer>
      <div v-if="formOpen" style="display:flex;justify-content:flex-end;width:100%">
        <a-space>
          <a-button @click="formOpen = false">Cancel</a-button>
          <a-button type="primary" :loading="saving" @click="save">{{ editing ? 'Save' : 'Start Recording' }}</a-button>
        </a-space>
      </div>
      <a-button v-else @click="emit('update:open', false)">Close</a-button>
    </template>
  </a-drawer>
</template>
