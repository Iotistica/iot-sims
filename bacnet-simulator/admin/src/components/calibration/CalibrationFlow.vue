<script setup lang="ts">
/** Select Recording -> Map Points -> Start Calibration -> Status/Results,
 * for a model that's already known (the caller picks/knows the model and
 * passes it in as `modelId`). Shared between CalibrationView.vue (the
 * standalone screen, which adds its own Select Model step first) and
 * CalibrationDrawer.vue (opened from a specific device that already has
 * an attached Simulation Model, so there's nothing to pick). */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined, CloseCircleOutlined, StopOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type {
  CalibrationJob, CalibrationMappingSuggestions, CalibrationRecordingSummary, CalibrationResult,
} from '../../types'

const props = defineProps<{
  modelId: string
  /** Pre-select a recording (e.g. opened via a recording's own "Create
   * Calibration" shortcut) instead of requiring the user to pick one. */
  initialRecordingId?: number | null
}>()

const loadingRecordings = ref(false)
const recordings = ref<CalibrationRecordingSummary[]>([])
const selectedRecordingId = ref<number | null>(props.initialRecordingId ?? null)

const loadingMapping = ref(false)
const suggestions = ref<CalibrationMappingSuggestions | null>(null)
// variable name -> chosen recording point id (or null = unmapped)
const mapping = ref<Record<string, number | null>>({})

const starting = ref(false)
const job = ref<CalibrationJob | null>(null)
const results = ref<CalibrationResult | null>(null)
const cancelling = ref(false)

async function loadRecordings() {
  loadingRecordings.value = true
  try {
    recordings.value = await api.calibration.listRecordings(props.modelId)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load recordings')
  } finally {
    loadingRecordings.value = false
  }
}
onMounted(loadRecordings)

// A variable declared unit:"boolean" (e.g. an override-enable input) needs
// a genuinely binary point -- iot-models' calibration script requires its
// whole column be constant boolean-ish text (1/true/on/... or
// 0/false/off/...), and errors out (after a full HEBO run attempt) on
// anything else, e.g. an analog point's numeric values. The suggestion
// engine has no idea about this FMI-variability distinction (it only
// scores Brick point-class/unit/name compatibility), so it's applied here
// as a point-picker filter, not a change to the scoring itself.
const BINARY_OBJECT_TYPES = new Set(['binary-input', 'binary-value', 'binary-output'])

function isBooleanVariable(v: { unit: string | null }): boolean {
  return v.unit === 'boolean'
}

function pointOptionsFor(v: { unit: string | null }) {
  if (!suggestions.value) return []
  if (!isBooleanVariable(v)) return suggestions.value.points
  const binaryPoints = suggestions.value.points.filter((p) => BINARY_OBJECT_TYPES.has(p.object_type))
  // No binary point at all in this recording -- fall back to showing
  // everything rather than an empty, dead-end dropdown; the warning below
  // still makes the requirement clear.
  return binaryPoints.length ? binaryPoints : suggestions.value.points
}

// Tried showing only "required" (= no model.json default) variables here,
// but every input in this model catalog happens to declare a default
// (needed just so the FMU can initialize standalone) -- so that filter
// degenerates to "goal output only" for RTU and most other models, silently
// falling back every real driving input (outdoor air temp, fan command,
// damper positions...) to one constant value for the whole recording
// instead of the recording's actual values. "required" is informational
// only here; every variable is shown/mappable.

async function loadMapping() {
  suggestions.value = null
  mapping.value = {}
  if (selectedRecordingId.value == null) return
  loadingMapping.value = true
  try {
    const s = await api.calibration.mappingSuggestions(selectedRecordingId.value, props.modelId)
    suggestions.value = s
    const pointsById = new Map(s.points.map((p) => [p.id, p]))
    const initial: Record<string, number | null> = {}
    for (const v of s.variables) {
      const suggestedPoint = v.suggested_point_id != null ? pointsById.get(v.suggested_point_id) : null
      // Don't pre-fill a boolean variable with a non-binary suggestion --
      // the suggestion engine can't see this constraint, so a "confident"
      // suggestion here can still be the wrong kind of point.
      initial[v.name] = isBooleanVariable(v) && suggestedPoint && !BINARY_OBJECT_TYPES.has(suggestedPoint.object_type)
        ? null
        : v.suggested_point_id
    }
    mapping.value = initial
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load mapping suggestions')
  } finally {
    loadingMapping.value = false
  }
}

function onRecordingSelected() {
  loadMapping()
}
if (selectedRecordingId.value != null) loadMapping()

const canStart = computed(() => {
  if (!suggestions.value) return false
  return suggestions.value.variables
    .filter((v) => v.required)
    .every((v) => mapping.value[v.name] != null)
})

const mappingPayload = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const [name, pointId] of Object.entries(mapping.value)) {
    if (pointId != null) out[name] = pointId
  }
  return out
})

const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED'])
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function pollJob() {
  if (!job.value) return
  try {
    job.value = await api.calibration.getJob(job.value.job_id, job.value.model_id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to refresh calibration status')
    return
  }
  if (TERMINAL.has(job.value.status)) {
    stopPolling()
    if (job.value.status === 'COMPLETED') {
      try {
        results.value = await api.calibration.getResults(job.value.job_id, job.value.model_id)
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to load calibration results')
      }
    }
  }
}

async function startCalibration() {
  if (!selectedRecordingId.value || !canStart.value) return
  starting.value = true
  try {
    job.value = await api.calibration.createJob({
      recording_id: selectedRecordingId.value,
      model_id: props.modelId,
      mapping: mappingPayload.value,
    })
    results.value = null
    stopPolling()
    pollTimer = setInterval(pollJob, 2000)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to start calibration')
  } finally {
    starting.value = false
  }
}

async function cancelCalibration() {
  if (!job.value) return
  cancelling.value = true
  try {
    job.value = await api.calibration.cancelJob(job.value.job_id, job.value.model_id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to cancel calibration')
  } finally {
    cancelling.value = false
  }
}

function startOver() {
  stopPolling()
  job.value = null
  results.value = null
}

onUnmounted(stopPolling)

const STATUS_COLOR: Record<string, string> = {
  QUEUED: 'default', VALIDATING: 'blue', RUNNING: 'blue',
  COMPLETED: 'green', FAILED: 'red', CANCELLED: 'default',
}

function formatPct(v: number | null): string {
  return v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`
}
function formatMetric(v: number | null): string {
  return v == null ? '—' : v.toFixed(4)
}
</script>

<template>
  <a-card size="small" title="Select Recording" style="margin-bottom:16px">
    <a-select
      v-model:value="selectedRecordingId"
      placeholder="Choose a completed recording"
      style="width:100%"
      :disabled="!!job"
      :loading="loadingRecordings"
      show-search
      :filter-option="(input: string, option: any) => option.label.toLowerCase().includes(input.toLowerCase())"
      @change="onRecordingSelected"
    >
      <a-select-option
        v-for="r in recordings" :key="r.id" :value="r.id"
        :label="`${r.name} ${r.device_name ?? ''}`"
      >
        {{ r.name }}
        <span style="color:var(--text-placeholder)">
          — {{ r.device_name ?? 'unknown device' }} · {{ r.point_count }} points · {{ r.sample_count }} samples
        </span>
      </a-select-option>
    </a-select>
    <div v-if="!loadingRecordings && !recordings.length" style="color:var(--text-placeholder);font-size:12px;margin-top:8px">
      No completed recordings with samples yet.
    </div>
  </a-card>

  <a-card
    v-if="selectedRecordingId"
    size="small" title="Map Points" style="margin-bottom:16px"
  >
    <div style="font-size:12px;color:var(--text-placeholder);margin-bottom:12px">
      Leaving an optional input unmapped uses the model's own default for the whole run instead of this recording's actual values.
    </div>
    <div v-if="loadingMapping" style="padding:16px;text-align:center"><a-spin /></div>
    <div v-else-if="suggestions">
      <div
        v-for="v in suggestions.variables" :key="v.name"
        style="padding:10px 0;border-bottom:1px solid var(--border-color, #f0f0f0)"
      >
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:6px">
          <div>
            <span>{{ v.name }}</span>
            <a-tag v-if="v.required" color="orange" style="margin-left:4px">required</a-tag>
            <div style="font-size:11px;color:var(--text-placeholder)">
              {{ v.direction }}<template v-if="v.unit"> · {{ v.unit }}</template>
            </div>
          </div>
          <div style="display:flex;gap:6px;flex-shrink:0;padding-top:2px">
            <a-tag v-if="mapping[v.name] === v.suggested_point_id && v.suggested_point_id != null" :color="
              v.confidence === 'high' ? 'green' : v.confidence === 'medium' ? 'blue' : 'default'
            ">
              suggested ({{ v.confidence }})
            </a-tag>
            <a-tooltip
              v-if="isBooleanVariable(v)"
              title="This input must stay constant on/off across the whole recording -- pick a binary point, not a continuous one."
            >
              <a-tag color="purple">boolean</a-tag>
            </a-tooltip>
          </div>
        </div>
        <a-select
          v-model:value="mapping[v.name]"
          placeholder="Select a point"
          allow-clear
          :disabled="!!job"
          style="width:100%"
        >
          <a-select-option v-for="p in pointOptionsFor(v)" :key="p.id" :value="p.id">
            {{ p.name }}<span v-if="p.units" style="color:var(--text-placeholder)"> ({{ p.units }})</span>
          </a-select-option>
        </a-select>
      </div>
    </div>
  </a-card>

  <div v-if="selectedRecordingId && suggestions && !job" style="margin-bottom:16px">
    <a-button type="primary" :disabled="!canStart" :loading="starting" @click="startCalibration">
      Start Calibration
    </a-button>
    <span v-if="!canStart" style="margin-left:8px;font-size:12px;color:var(--text-placeholder)">
      Map every required variable to start.
    </span>
  </div>

  <a-card v-if="job" size="small" title="Status / Results">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <a-tag :color="STATUS_COLOR[job.status]">{{ job.status }}</a-tag>
      <span v-if="job.status === 'RUNNING' || job.status === 'QUEUED' || job.status === 'VALIDATING'">
        <a-spin size="small" />
      </span>
      <div style="flex:1" />
      <a-button
        v-if="job.status === 'QUEUED' || job.status === 'VALIDATING' || job.status === 'RUNNING'"
        danger :loading="cancelling" @click="cancelCalibration"
      >
        <template #icon><StopOutlined /></template>
        Cancel
      </a-button>
      <a-button v-if="job.status !== 'RUNNING' && job.status !== 'QUEUED' && job.status !== 'VALIDATING'" @click="startOver">
        New Calibration
      </a-button>
    </div>

    <a-alert
      v-if="job.status === 'FAILED'"
      type="error" show-icon :message="job.error || 'Calibration failed'"
      style="margin-bottom:12px"
    />

    <div v-if="results">
      <a-descriptions bordered size="small" :column="2" style="margin-bottom:16px">
        <a-descriptions-item label="Metric">{{ results.objective.metric ?? '—' }}</a-descriptions-item>
        <a-descriptions-item label="Improvement">
          <CheckCircleOutlined v-if="(results.objective.improvement_pct ?? 0) > 0" style="color:#52c41a" />
          <CloseCircleOutlined v-else-if="(results.objective.improvement_pct ?? 0) < 0" style="color:#ff4d4f" />
          {{ formatPct(results.objective.improvement_pct) }}
        </a-descriptions-item>
        <a-descriptions-item label="Baseline">{{ formatMetric(results.objective.baseline) }}</a-descriptions-item>
        <a-descriptions-item label="Calibrated (best)">{{ formatMetric(results.objective.best) }}</a-descriptions-item>
        <a-descriptions-item label="Evaluations">{{ results.execution.evaluations ?? '—' }}</a-descriptions-item>
        <a-descriptions-item label="Failed evaluations">{{ results.execution.failed_evaluations }}</a-descriptions-item>
      </a-descriptions>

      <h4 style="margin:0 0 8px">Best Parameters</h4>
      <a-table
        size="small"
        :pagination="false"
        :data-source="Object.entries(results.best_parameters).map(([parameter, value]) => ({ parameter, value }))"
        :columns="[
          { title: 'Parameter', dataIndex: 'parameter', key: 'parameter' },
          { title: 'Value', dataIndex: 'value', key: 'value' },
        ]"
        row-key="parameter"
      />
    </div>
  </a-card>
</template>
