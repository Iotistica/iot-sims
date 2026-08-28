<script setup lang="ts">
import { ref, reactive, computed, watch, onUnmounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, StopOutlined, DeleteOutlined, EditOutlined, PlayCircleOutlined, DownloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, SimObject, ReplayRecording } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean]; 'create-replay': [recording: ReplayRecording] }>()

const list = ref<ReplayRecording[]>([])
const allObjects = ref<SimObject[]>([])
const loading = ref(false)
const saving = ref(false)
const formOpen = ref(false)
// Set while editing an existing recording (its points list is fixed --
// see update() and the "Points to Record" form-item's v-if -- so this only
// pre-fills the operational fields, never the points picker).
const editing = ref<ReplayRecording | null>(null)

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
  pointsMode: 'all' as 'all' | 'selected',
  selectedPointIds: [] as number[],
  sample_interval_seconds: 5,
  maximum_samples: 1000,
  buffer_mode: 'stop' as 'overwrite' | 'stop',
})

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    const [recordings, objects, settings] = await Promise.all([
      api.replayRecordings.list(props.device.id),
      api.objects.list(props.device.id),
      api.settings.get(),
    ])
    list.value = recordings
    allObjects.value = objects
    tickSeconds.value = settings.tick_seconds
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
    list.value = await api.replayRecordings.list(props.device.id)
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
    name: '', description: '', pointsMode: 'all', selectedPointIds: [],
    // Default to the simulation's own tick rate for a simulated device
    // (never below it) -- a faster default would just start every new
    // recording with wasted, duplicate samples. External devices keep the
    // plain 5s default since tick rate doesn't apply to them.
    sample_interval_seconds: !isExternalDevice.value && tickSeconds.value ? tickSeconds.value : 5,
    maximum_samples: 1000, buffer_mode: 'stop',
  })
}

function openAdd() {
  editing.value = null
  resetForm()
  formOpen.value = true
}

function openEdit(recording: ReplayRecording) {
  editing.value = recording
  Object.assign(form, {
    name: recording.name,
    description: recording.description,
    pointsMode: 'all', // not editable here -- see the form-item's v-if
    selectedPointIds: [],
    sample_interval_seconds: recording.sample_interval_seconds,
    maximum_samples: recording.maximum_samples,
    buffer_mode: recording.buffer_mode,
  })
  formOpen.value = true
}

function objectLabel(o: SimObject): string {
  return `${o.name} (${o.object_type})`
}

// Same eligibility rule CreateSimulatedCopyModal.vue's own Replay-mode
// recording picker uses -- only a completed, non-empty recording can
// drive a Replay device.
function isReplayable(recording: ReplayRecording): boolean {
  return recording.status === 'completed' && recording.sample_count > 0
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (!editing.value && form.pointsMode === 'selected' && !form.selectedPointIds.length) {
    message.error('Choose at least one point, or switch to "All device points"')
    return
  }

  saving.value = true
  try {
    if (editing.value) {
      await api.replayRecordings.update(editing.value.id, {
        name: form.name.trim(),
        description: form.description,
        sample_interval_seconds: form.sample_interval_seconds,
        maximum_samples: form.maximum_samples,
        buffer_mode: form.buffer_mode,
      })
      message.success('Recording updated')
    } else {
      // Starts sampling immediately -- there is no separate save-then-start step.
      await api.replayRecordings.create(props.device.id, {
        name: form.name.trim(),
        description: form.description,
        point_ids: form.pointsMode === 'all' ? null : form.selectedPointIds,
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

async function stopRecording(recording: ReplayRecording) {
  try {
    await api.replayRecordings.stop(recording.id)
    message.success('Recording stopped')
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to stop recording')
  }
}

function confirmDelete(recording: ReplayRecording) {
  Modal.confirm({
    title: `Delete "${recording.name}"?`,
    content: 'This also deletes all of its recorded samples. Any Replay device driven by it will stop updating.',
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.replayRecordings.del(recording.id)
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

async function exportSamples(recording: ReplayRecording) {
  exportingSamples.value = recording.id
  try {
    await api.replayRecordings.exportSamples(recording.id, recording.name)
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
function sampleCountLabel(recording: ReplayRecording): string {
  const { sample_count: count, maximum_samples: max, buffer_mode: mode } = recording
  if (mode === 'overwrite') {
    return count < max
      ? `${count} of ${max} samples`
      : `${count} samples (keeps most recent ${max})`
  }
  const remaining = Math.max(0, max - count)
  return remaining > 0 ? `${count}/${max} samples · ${remaining} remaining` : `${count}/${max} samples`
}

function duration(recording: ReplayRecording): string {
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
              </div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                Started {{ new Date(toUnixMillis(r.started_at)).toLocaleString() }} · Duration {{ duration(r) }}
              </div>
              <div style="font-size:11px;color:var(--text-secondary);margin-top:2px">
                Every {{ r.sample_interval_seconds }}s · {{ r.point_count }} point{{ r.point_count !== 1 ? 's' : '' }}
                · {{ sampleCountLabel(r) }}
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

        <a-form-item v-if="!editing" label="Points to Record">
          <a-radio-group v-model:value="form.pointsMode">
            <a-radio-button value="all">All device points</a-radio-button>
            <a-radio-button value="selected">Selected points</a-radio-button>
          </a-radio-group>
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
