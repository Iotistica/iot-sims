<script setup lang="ts">
/** Select Recording -> Map Points -> Start Calibration -> Status/Results,
 * for a model that's already known (the caller picks/knows the model and
 * passes it in as `modelId`). Shared between CalibrationView.vue (the
 * standalone screen, which adds its own Select Model step first) and
 * CalibrationDrawer.vue (opened from a specific device that already has
 * an attached Simulation Model, so there's nothing to pick). */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { StopOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type {
  CalibrationJob, CalibrationMappingSuggestions, CalibrationMetrics, CalibrationRecordingSummary,
  CalibrationResult, CalibrationRun,
} from '../../types'

const props = defineProps<{
  modelId: string
  /** Pre-select a recording (e.g. opened via a recording's own "Create
   * Calibration" shortcut) instead of requiring the user to pick one. */
  initialRecordingId?: number | null
  /** The device-scoped SimulationModelConfig this calibration targets --
   * only known/passed by CalibrationDrawer.vue (opened from a specific
   * device). CalibrationView.vue's model-type-scoped entry point has no
   * single config to apply to (would need its own device picker, out of
   * scope for v1 -- see the plan), so it leaves this unset and the Apply
   * button below stays hidden. */
  configId?: number | null
}>()

const loadingRecordings = ref(false)
const recordings = ref<CalibrationRecordingSummary[]>([])
const selectedRecordingId = ref<number | null>(props.initialRecordingId ?? null)
// Optional held-out validation recording. Must differ from the calibration recording;
// when set, the run gets a real held-out validation verdict instead of "not validated".
const validationRecordingId = ref<number | null>(null)

const loadingMapping = ref(false)
const suggestions = ref<CalibrationMappingSuggestions | null>(null)
// variable name -> chosen recording point id (or null = unmapped)
const mapping = ref<Record<string, number | null>>({})

const starting = ref(false)
const job = ref<CalibrationJob | null>(null)
const results = ref<CalibrationResult | null>(null)
const run = ref<CalibrationRun | null>(null)
const cancelling = ref(false)

const validationRecordingOptions = computed(() =>
  recordings.value.filter((r) => r.id !== selectedRecordingId.value),
)

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

const history = ref<CalibrationRun[]>([])
async function loadHistory() {
  try {
    history.value = props.configId != null
      ? await api.calibration.listRuns({ simulation_model_id: props.configId })
      : await api.calibration.listRuns({ model_id: props.modelId })
  } catch { /* history is a convenience panel; a failure here shouldn't disrupt the flow */ }
}

const recordingNameById = computed(() => new Map(recordings.value.map((r) => [r.id, r.name])))

onMounted(() => { loadRecordings(); loadHistory() })

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
  if (validationRecordingId.value === selectedRecordingId.value) validationRecordingId.value = null
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
  if (job.value.calibration_run_id != null) {
    try {
      run.value = await api.calibration.getRun(job.value.calibration_run_id)
    } catch { /* run row is best-effort context; the job itself is the source of truth */ }
  }
  if (TERMINAL.has(job.value.status)) {
    stopPolling()
    loadHistory()
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
      validation_recording_id: validationRecordingId.value ?? undefined,
      simulation_model_id: props.configId ?? undefined,
    })
    results.value = null
    run.value = null
    stopPolling()
    pollJob()
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
  run.value = null
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
function formatPctMetric(v: number | null | undefined): string {
  return v == null ? '—' : `${v.toFixed(1)}%`
}

// All five fit statistics run_hebo_calibration.py's own metric_pack() computes, for both
// baseline and best -- not just whichever one was the optimization objective (see
// CalibrationMetrics's own doc comment in types.ts). CV-RMSE/NMBE are percentages; MAE/RMSE are
// in the goal variable's own units; R² is unitless -- formatted accordingly below.
const FIT_STATISTIC_ROWS: { key: keyof CalibrationMetrics; label: string; percent: boolean }[] = [
  { key: 'cv_rmse', label: 'CV(RMSE)', percent: true },
  { key: 'nmbe', label: 'NMBE', percent: true },
  { key: 'rmse', label: 'RMSE', percent: false },
  { key: 'mae', label: 'MAE', percent: false },
  { key: 'r2', label: 'R²', percent: false },
]
const fitStatisticRows = computed(() => {
  if (!results.value) return []
  const best = results.value.calibration_metrics ?? results.value.objective.metrics
  const baseline = results.value.objective.baseline_metrics
  const validation = results.value.validation_metrics
  return FIT_STATISTIC_ROWS.map((row) => ({
    key: row.key,
    statistic: row.label,
    baseline: row.percent ? formatPctMetric(baseline?.[row.key]) : formatMetric(baseline?.[row.key] ?? null),
    best: row.percent ? formatPctMetric(best?.[row.key]) : formatMetric(best?.[row.key] ?? null),
    validation: validation
      ? (row.percent ? formatPctMetric(validation[row.key]) : formatMetric(validation[row.key] ?? null))
      : '—',
  }))
})

const hasValidation = computed(() => !!results.value?.validation_metrics)
const validationStatusLabel = computed(() => {
  const s = run.value?.validation_status
  if (s === 'completed') return 'Validated on held-out data'
  if (s === 'failed') return 'Validation replay failed'
  if (s === 'pending') return 'Validation pending'
  return 'Not validated — no held-out recording was provided'
})
const appliedLabel = computed(() => {
  if (!run.value?.applied_at) return null
  const target = run.value.applied_to_simulation_model_id
  return `Applied ${run.value.applied_at}${target != null ? ` to model #${target}` : ''}`
})

const applying = ref(false)

// Push the run's best parameters onto the target simulation model via the calibration
// run itself (server merges into parameters.parameter_overrides and reloads). Requires a
// completed run row -- which only exists once configId was passed at Start time.
async function applyToModel() {
  const runId = job.value?.calibration_run_id
  if (!props.configId || runId == null) return
  applying.value = true
  try {
    const res = await api.calibration.applyRun(runId, props.configId)
    run.value = res.calibration_run
    loadHistory()
    message.success('Calibration results applied to the Simulation Model')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to apply calibration results')
  } finally {
    applying.value = false
  }
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

    <div v-if="selectedRecordingId" style="margin-top:12px">
      <div style="font-size:12px;color:var(--text-secondary, #888);margin-bottom:4px">
        Validation recording <span style="color:var(--text-placeholder)">(optional, held-out)</span>
      </div>
      <a-select
        v-model:value="validationRecordingId"
        placeholder="None — result will be marked “not validated”"
        style="width:100%"
        allow-clear
        :disabled="!!job"
        show-search
        :filter-option="(input: string, option: any) => option.label.toLowerCase().includes(input.toLowerCase())"
      >
        <a-select-option
          v-for="r in validationRecordingOptions" :key="r.id" :value="r.id"
          :label="`${r.name} ${r.device_name ?? ''}`"
        >
          {{ r.name }}
          <span style="color:var(--text-placeholder)">
            — {{ r.device_name ?? 'unknown device' }} · {{ r.sample_count }} samples
          </span>
        </a-select-option>
      </a-select>
      <div style="font-size:11px;color:var(--text-placeholder);margin-top:4px">
        The best parameters are replayed once against this recording after tuning. ASHRAE G14 is
        only evaluated when a validation recording is provided.
      </div>
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
              semantic match ({{ v.confidence }})
            </a-tag>
            <a-tag v-else-if="v.resolution?.quality === 'ambiguous'" color="gold">
              ambiguous — pick one
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
        <!-- Name-based guess: a SEPARATE mechanism, never a semantic match, never
             pre-filled. Only shown when semantics didn't bind. -->
        <div
          v-if="v.suggested_point_id == null && v.name_match?.point_id != null"
          style="font-size:11px;color:var(--text-placeholder);margin-top:4px"
        >
          Name-based guess (not a semantic match): {{ v.name_match.point_name }}
          <a v-if="!job" style="margin-left:6px" @click="mapping[v.name] = v.name_match!.point_id">Use</a>
        </div>
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
      <!-- ASHRAE G14 — the trust verdict. Held-out validation ONLY. -->
      <a-alert
        style="margin-bottom:16px"
        :type="results.ashrae_g14.applicable ? (results.ashrae_g14.pass ? 'success' : 'error') : 'info'"
        show-icon
      >
        <template #message>
          <template v-if="results.ashrae_g14.applicable">
            ASHRAE G14 — {{ results.ashrae_g14.interval }}:
            <strong>{{ results.ashrae_g14.pass ? 'PASS' : 'FAIL' }}</strong>
            <span style="color:var(--text-placeholder)"> · based on held-out validation</span>
          </template>
          <template v-else>
            ASHRAE G14 not evaluated — held-out validation was not provided
          </template>
        </template>
        <template v-if="results.ashrae_g14.applicable" #description>
          CV(RMSE) {{ formatPctMetric(results.ashrae_g14.cv_rmse) }} (≤ {{ results.ashrae_g14.cv_rmse_threshold }}%)
          · NMBE {{ formatPctMetric(results.ashrae_g14.nmbe) }} (|·| ≤ {{ results.ashrae_g14.nmbe_threshold }}%)
        </template>
      </a-alert>

      <h4 style="margin:0 0 8px">Calibration fit</h4>
      <a-descriptions bordered size="small" :column="2" style="margin-bottom:8px">
        <a-descriptions-item label="Objective metric">{{ results.objective.metric ?? '—' }}</a-descriptions-item>
        <a-descriptions-item label="Evaluations">{{ results.execution.evaluations ?? '—' }} ({{ results.execution.failed_evaluations }} failed)</a-descriptions-item>
        <a-descriptions-item label="Unmodified FMU (baseline)">{{ formatMetric(results.objective.baseline) }}</a-descriptions-item>
        <a-descriptions-item label="Calibrated (best)">{{ formatMetric(results.objective.best) }}</a-descriptions-item>
      </a-descriptions>
      <div style="font-size:11px;color:var(--text-placeholder);margin-bottom:12px">
        Optimizer improvement vs the unmodified FMU: {{ formatPct(results.objective.improvement_pct) }}
        — a tuning-progress figure, not a trust signal.
      </div>

      <a-table
        size="small"
        :pagination="false"
        :data-source="fitStatisticRows"
        :columns="[
          { title: 'Statistic', dataIndex: 'statistic', key: 'statistic' },
          { title: 'Baseline (unmodified)', dataIndex: 'baseline', key: 'baseline' },
          { title: 'Calibration fit', dataIndex: 'best', key: 'best' },
          { title: 'Held-out validation', dataIndex: 'validation', key: 'validation' },
        ]"
        row-key="key"
        style="margin-bottom:12px"
      />

      <h4 style="margin:0 0 8px">Validation</h4>
      <div style="margin-bottom:16px">
        <a-tag :color="hasValidation ? 'green' : (run?.validation_status === 'failed' ? 'red' : 'default')">
          {{ validationStatusLabel }}
        </a-tag>
        <span
          v-if="results.calibration_fit_g14"
          style="font-size:11px;color:var(--text-placeholder);margin-left:8px"
        >
          (fit-period G14 diagnostic:
          <template v-if="results.calibration_fit_g14.applicable">
            {{ results.calibration_fit_g14.pass ? 'would pass' : 'would fail' }} — not a validation result
          </template>
          <template v-else>n/a</template>)
        </span>
      </div>

      <h4 style="margin:0 0 8px">Best Parameters</h4>
      <a-table
        size="small"
        :pagination="false"
        :data-source="Object.entries(results.best_parameters).map(([parameter, value]) => ({
          parameter, value, fmu_default: results!.fmu_defaults?.[parameter] ?? '—',
        }))"
        :columns="[
          { title: 'Parameter', dataIndex: 'parameter', key: 'parameter' },
          { title: 'FMU default', dataIndex: 'fmu_default', key: 'fmu_default' },
          { title: 'Calibrated value', dataIndex: 'value', key: 'value' },
        ]"
        row-key="parameter"
      />

      <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
        <a-button
          v-if="configId && job?.calibration_run_id != null"
          type="primary" ghost :loading="applying"
          @click="applyToModel"
        >
          Apply to Model
        </a-button>
        <a-tag v-if="appliedLabel" color="green">{{ appliedLabel }}</a-tag>
        <span v-else-if="configId" style="font-size:12px;color:var(--text-placeholder)">Not applied</span>
      </div>
    </div>
  </a-card>

  <a-card v-if="history.length" size="small" title="Calibration History" style="margin-top:16px">
    <a-table
      size="small"
      :pagination="false"
      :data-source="history"
      :columns="[
        { title: 'Date', dataIndex: 'created_at', key: 'created_at' },
        { title: 'Recording', key: 'recording' },
        { title: 'Status', key: 'status' },
        { title: 'Validation', key: 'validation' },
        { title: 'ASHRAE G14', key: 'g14' },
        { title: 'Applied', key: 'applied' },
      ]"
      row-key="id"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'recording'">
          {{ recordingNameById.get(record.recording_id) ?? `#${record.recording_id ?? '—'}` }}
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag :color="record.status === 'completed' ? 'green' : record.status === 'failed' ? 'red' : 'default'">
            {{ record.status }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'validation'">
          <a-tag :color="record.validation_status === 'completed' ? 'green' : record.validation_status === 'failed' ? 'red' : 'default'">
            {{ record.validation_status }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'g14'">
          <template v-if="record.g14_based_on === 'validation' && record.g14_applicable">
            {{ record.g14_pass ? 'PASS' : 'FAIL' }}
          </template>
          <span v-else style="color:var(--text-placeholder)">—</span>
        </template>
        <template v-else-if="column.key === 'applied'">
          <a-tag v-if="record.applied_at" color="green">yes</a-tag>
          <span v-else style="color:var(--text-placeholder)">no</span>
        </template>
      </template>
    </a-table>
  </a-card>
</template>
