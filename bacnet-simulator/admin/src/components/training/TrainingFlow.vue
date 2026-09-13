<script setup lang="ts">
/** Select Recordings (multi) -> Map Variables -> Start Training -> Status/
 * Results, for a model that's already known. Sibling to
 * calibration/CalibrationFlow.vue, generalized from one recording to
 * several (one recording = one trajectory in iot-models' training-job
 * terms) and from "inputs + one goal output" to every declared model
 * variable. Recording listing uses api.training.listRecordings (its own
 * dedicated, stricter route) rather than calibration's own -- confirmed
 * live that combining recordings from devices running different models
 * produces near-zero point overlap; see api.ts's own comment. */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined, RobotOutlined, StopOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type { SimulationModelAggregateMapping, SimulationModelMapping, SimulationModelPayload } from '../../api'
import type {
  CalibrationRecordingSummary, Device, FaultType, PromotedModelSummary, TrainingJob,
  TrainingMappingSuggestions, TrainingResult,
} from '../../types'

const props = defineProps<{ modelId: string; deviceId: number; device: Device }>()

const loadingRecordings = ref(false)
const recordings = ref<CalibrationRecordingSummary[]>([])
const selectedRecordingIds = ref<number[]>([])

// Same label/color convention as RecordingsDrawer.vue's own CONDITION_TAG
// -- just making a recording's Normal/Degraded/Fault classification
// identifiable here too, display only (no filtering, no change to how
// recordings are actually selected/combined).
const CONDITION_TAG: Record<'normal' | 'degraded' | 'fault', { label: string; color: string }> = {
  normal: { label: 'Normal', color: 'green' },
  degraded: { label: 'Degraded', color: 'orange' },
  fault: { label: 'Fault', color: 'red' },
}
// Same idea for Fault Type -- see RecordingsDrawer.vue's own FAULT_TYPE_TAG.
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

const loadingMapping = ref(false)
const suggestions = ref<TrainingMappingSuggestions | null>(null)

const starting = ref(false)
const job = ref<TrainingJob | null>(null)
const results = ref<TrainingResult | null>(null)
const cancelling = ref(false)

async function loadRecordings() {
  loadingRecordings.value = true
  try {
    recordings.value = await api.training.listRecordings(props.modelId, props.deviceId)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load recordings')
  } finally {
    loadingRecordings.value = false
  }
}
onMounted(loadRecordings)

// Purely informational -- every training job ever run for this model
// (iot-models persists these across its own restarts too, see api.ts's own
// comment), newest first. No interactivity by design: this is a history
// list, not another way to load/inspect a specific run's results.
const loadingHistory = ref(false)
const jobHistory = ref<TrainingJob[]>([])

async function loadJobHistory() {
  loadingHistory.value = true
  try {
    jobHistory.value = await api.training.listJobs(props.modelId)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load training history')
  } finally {
    loadingHistory.value = false
  }
}
onMounted(loadJobHistory)

function formatTimestamp(ts: number | null | undefined): string {
  return ts == null ? '—' : new Date(ts * 1000).toLocaleString()
}

async function loadMapping() {
  suggestions.value = null
  if (selectedRecordingIds.value.length < 2) return
  loadingMapping.value = true
  try {
    suggestions.value = await api.training.mappingSuggestions(selectedRecordingIds.value, props.modelId, props.deviceId)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load mapping suggestions')
  } finally {
    loadingMapping.value = false
  }
}

function onRecordingsChanged() {
  loadMapping()
}

// Everything below is fully automatic -- no picker, no checkbox, nothing
// for the user to review or decide. A variable is either mapped on this
// device's own active Simulation Model config or it isn't (Training never
// suggests a point itself, see training_export.py's own docstring); a
// mapped variable is used exactly when its point was actually captured by
// at least one of the currently-selected recordings (otherwise including
// it would only drop recordings for nothing -- most visibly, a variable
// mapped to a point on a different device that a recording didn't capture).
const mappedVariables = computed(() =>
  suggestions.value ? suggestions.value.variables.filter((v) => v.mapped) : [],
)
const usableVariables = computed(() =>
  mappedVariables.value.filter((v) => v.missing_in_recordings.length < selectedRecordingIds.value.length),
)

// Per build_training_dataset's own "a recording missing ANY used variable's
// point contributes zero rows" behavior -- only a recording where every
// used variable resolves is actually going to appear in the exported
// dataset. Surfaced clearly so this is caught before submitting, not
// discovered afterward as "why is my dataset missing a trajectory".
const effectiveRecordingIds = computed(() =>
  selectedRecordingIds.value.filter((rid) =>
    usableVariables.value.every((v) => !v.missing_in_recordings.includes(rid)),
  ),
)

const droppedRecordingIds = computed(() =>
  selectedRecordingIds.value.filter((id) => !effectiveRecordingIds.value.includes(id)),
)

function recordingLabel(id: number): string {
  return recordings.value.find((r) => r.id === id)?.name ?? `#${id}`
}

// How many of this device's own mapped variables are captured by EVERY
// selected recording -- an early, single signal that the selected
// recordings barely overlap at all, which usually means one of them is
// from unrelated equipment.
const sharedVariableCount = computed(() =>
  mappedVariables.value.filter((v) => v.missing_in_recordings.length === 0).length,
)

// Mirrors shared/train_baseline.py's own build_supervised_pairs check
// (needs at least one input_* and one output_* column) -- surfaced here so
// "0 inputs" is visible before clicking Start, not discovered only after a
// FAILED async job. Most commonly hit with recordings made before this
// device's real inputs were ever captured (see modelPointIds's own comment
// in RecordingsDrawer.vue): everything mapped is one of this device's
// own OUTPUT points, since that's all those older recordings have.
const usableInputCount = computed(() => usableVariables.value.filter((v) => v.direction === 'input').length)
const usableOutputCount = computed(() => usableVariables.value.filter((v) => v.direction === 'output').length)
// How many of the usable inputs came from a recording's own snapshotted
// constant value rather than a live point (see training_export.py's
// _constant_sourced_inputs) -- surfaced so it's clear these won't teach the
// model any real input sensitivity, they're along for the ride so the
// dataset has a feature column for them at all.
const constantSourcedInputCount = computed(
  () => usableVariables.value.filter((v) => v.direction === 'input' && v.source === 'constant').length,
)

const canStart = computed(() =>
  !!suggestions.value && usableInputCount.value > 0 && usableOutputCount.value > 0
  && effectiveRecordingIds.value.length >= 2,
)

const mappingPayload = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const v of usableVariables.value) if (v.object_id != null) out[v.name] = v.object_id
  return out
})

const historyWindow = ref(1)

const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED'])
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function pollJob() {
  if (!job.value) return
  try {
    job.value = await api.training.getJob(job.value.job_id, job.value.model_id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to refresh training status')
    return
  }
  if (TERMINAL.has(job.value.status)) {
    stopPolling()
    if (job.value.status === 'COMPLETED') {
      try {
        results.value = await api.training.getResults(job.value.job_id, job.value.model_id)
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to load training results')
      }
    }
    loadJobHistory()
  }
}

async function startTraining() {
  if (!canStart.value) return
  starting.value = true
  try {
    const created = await api.training.createJob({
      model_id: props.modelId,
      recording_ids: selectedRecordingIds.value,
      mapping: mappingPayload.value,
      history_window: historyWindow.value,
    })
    job.value = created
    results.value = null
    stopPolling()
    pollTimer = setInterval(pollJob, 2000)
    loadJobHistory()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to start training')
  } finally {
    starting.value = false
  }
}

async function cancelTraining() {
  if (!job.value) return
  cancelling.value = true
  try {
    job.value = await api.training.cancelJob(job.value.job_id, job.value.model_id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to cancel training')
  } finally {
    cancelling.value = false
  }
}

const usingModel = ref(false)
const promotedModel = ref<PromotedModelSummary | null>(null)
// True once useThisModel() has fully finished -- either branch (retrained
// in place, or minted+attached). Distinct from promotedModel != null so
// the button can't be double-clicked mid-flight, and so an attach failure
// (below) can leave promotedModel set without counting as "done".
const usedModel = ref(false)
// Set only for the mint-new branch, so the template can say what actually
// happened -- retraining an already-AI-attached model never touches
// attachment/mappings at all, there's nothing to describe there.
const wasNewModel = ref(false)
// Set when promotion succeeded but the attach step's own save was
// rejected -- almost always the backend's required-input-mapping check
// (see simulation.py::_validate_mapping_contract's required/missing
// block, shared with FMU since AI adopted the same Constant/Point/
// Aggregate input sourcing). Kept as a persistent banner (not just a
// toast) since it names points the user has to go fix in Simulation →
// Model before retrying, not something to glance past.
const attachError = ref<string | null>(null)

// ONE user action for what iot-models' own /promote endpoint already
// decides as one thing server-side (see its own docstring): retraining an
// already-promoted (runtime_type=='onnx') model updates it IN PLACE --
// same id, so the device is already attached, nothing else to do. Training
// a still-FMU-backed model for the first time mints a new id (the FMU
// stays the shipped reference, never overwritten) -- that case still needs
// an attach, but it happens right here in the same click, not a second
// one, and carries over whatever was already mapped rather than resetting
// to blank (the new model declares the exact same variable names, copied
// verbatim from the FMU source at promote time -- see iot-models' own
// promote_training_job).
async function useThisModel() {
  if (!job.value) return
  usingModel.value = true
  attachError.value = null
  try {
    // A retry after a failed attach must not re-promote -- the job was
    // already promoted into a real model on the first click; promoting
    // the same still-FMU-backed source again would mint a second,
    // orphaned model instead of reusing the one the user is trying to fix.
    let result = promotedModel.value
    if (!result) {
      result = await api.training.promoteJob(job.value.job_id, job.value.model_id)
      promotedModel.value = result
    }

    if (result.id === props.modelId) {
      wasNewModel.value = false
      usedModel.value = true
      message.success('Model updated — retrained in place')
      return
    }

    wasNewModel.value = true
    let mappings: SimulationModelPayload['mappings'] = []
    let aggregateMappings: SimulationModelPayload['aggregate_mappings'] = []
    let inputExposures: SimulationModelPayload['input_exposures'] = []
    if (props.device.active_simulation_model) {
      const existing = await api.simulationModels.get(props.device.active_simulation_model.id)
      mappings = existing.mappings.filter(
        (m): m is SimulationModelMapping => !('point_ids' in m),
      )
      aggregateMappings = existing.mappings.filter(
        (m): m is SimulationModelAggregateMapping => 'point_ids' in m,
      )
      inputExposures = existing.input_exposures ?? []
    }

    // Same create/update + full-mapping-replace mechanism
    // SimulationModelDrawer.vue's own persist() uses -- update() when this
    // device already has a model, create() otherwise. Carries the
    // existing mappings over instead of resetting to blank (see above).
    const payload: SimulationModelPayload = {
      name: `${props.device.name} ${result.label}`,
      provider_type: 'ai',
      model_type: result.id,
      enabled: true,
      created_from_device_id: props.device.id,
      parameters: {},
      mappings,
      aggregate_mappings: aggregateMappings,
      input_exposures: inputExposures,
    }
    try {
      if (props.device.active_simulation_model) {
        await api.simulationModels.update(props.device.active_simulation_model.id, payload, true)
      } else {
        await api.simulationModels.create(payload)
      }
    } catch (e: unknown) {
      // The model itself is already trained and saved (promotedModel is
      // set) -- only this attach save was rejected, so say exactly that
      // and let a retry skip straight back to this step.
      attachError.value =
        `${result.label} was trained and saved, but couldn't be attached: ${(e as Error).message}. ` +
        'Map the missing input(s) in Simulation → Model, then click "Use this model" again.'
      message.error(attachError.value)
      return
    }
    usedModel.value = true
    message.success(`Now using ${result.label} — points carried over from the previous model`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to use the trained model')
  } finally {
    usingModel.value = false
  }
}

function startOver() {
  stopPolling()
  job.value = null
  results.value = null
  promotedModel.value = null
  usedModel.value = false
  attachError.value = null
  wasNewModel.value = false
}

onUnmounted(stopPolling)

const STATUS_COLOR: Record<string, string> = {
  QUEUED: 'default', RUNNING: 'blue', COMPLETED: 'green', FAILED: 'red', CANCELLED: 'default',
}

function formatR2(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(3)
}
function formatMae(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(3)
}
</script>

<template>
  <a-card size="small" title="Select Recordings" style="margin-bottom:16px">
    <div style="font-size:12px;color:var(--text-placeholder);margin-bottom:8px">
      Pick at least 2 recordings -- iot-models' training pipeline needs multiple trajectories to hold one out for honest validation.
    </div>
    <a-select
      v-model:value="selectedRecordingIds"
      mode="multiple"
      placeholder="Choose completed recordings"
      style="width:100%"
      :disabled="!!job"
      :loading="loadingRecordings"
      show-search
      :filter-option="(input: string, option: any) => option.label.toLowerCase().includes(input.toLowerCase())"
      @change="onRecordingsChanged"
    >
      <a-select-option
        v-for="r in recordings" :key="r.id" :value="r.id"
        :label="`${r.name} ${r.device_name ?? ''}`"
      >
        {{ r.name }}
        <a-tag v-if="r.condition" :color="CONDITION_TAG[r.condition].color" style="margin-left:4px;font-weight:normal">
          {{ CONDITION_TAG[r.condition].label }}
        </a-tag>
        <a-tag v-if="r.fault_type" style="margin-left:4px;font-weight:normal">{{ FAULT_TYPE_TAG[r.fault_type] }}</a-tag>
        <a-tag v-for="t in r.tags" :key="t" style="margin-left:4px;font-weight:normal">{{ t }}</a-tag>
        <span style="color:var(--text-placeholder)">
          — {{ r.device_name ?? 'unknown device' }} · {{ r.point_count }} points · {{ r.sample_count }} samples
        </span>
      </a-select-option>
    </a-select>
    <div v-if="!loadingRecordings && !recordings.length" style="color:var(--text-placeholder);font-size:12px;margin-top:8px">
      No completed recordings with samples yet.
    </div>
    <div
      v-else-if="selectedRecordingIds.length === 1"
      style="color:var(--text-placeholder);font-size:12px;margin-top:8px"
    >
      Select at least 1 more recording to see the mapping table -- training needs multiple trajectories to hold one out for validation.
    </div>
  </a-card>

  <a-card
    v-if="selectedRecordingIds.length >= 2"
    size="small" title="Training Data" style="margin-bottom:16px"
  >
    <div v-if="loadingMapping" style="padding:16px;text-align:center"><a-spin /></div>
    <template v-else-if="suggestions">
      <a-alert
        v-if="mappedVariables.length && sharedVariableCount === 0"
        type="error" show-icon style="margin-bottom:12px"
        message="These recordings share none of this device's mapped variables"
        description="They're likely from unrelated devices/equipment -- combining them won't produce a usable training set. Consider selecting recordings of the same (or a matching) device instead."
      />
      <a-alert
        v-else-if="mappedVariables.length && sharedVariableCount <= 2"
        type="warning" show-icon style="margin-bottom:12px"
        :message="`Only ${sharedVariableCount} mapped variable${sharedVariableCount === 1 ? '' : 's'} shared across all selected recordings`"
        description="Double-check these recordings are actually from the same or comparable equipment."
      />
      <a-alert
        v-if="!job && droppedRecordingIds.length"
        type="warning" show-icon style="margin-bottom:12px"
        :message="`${droppedRecordingIds.length} of ${selectedRecordingIds.length} selected recordings will be excluded`"
        :description="`${droppedRecordingIds.map(recordingLabel).join(', ')} didn't capture one of the variables used here, so they won't contribute any rows.`"
      />
      <a-alert
        v-if="!job && suggestions.variables.length && (usableInputCount === 0 || usableOutputCount === 0)"
        type="error" show-icon style="margin-bottom:12px"
        message="Nothing to train on"
        description="Training needs at least one input and one output variable. Record this device with &quot;Model points&quot; mode so its real inputs are captured too, not just its outputs. If this model's inputs are constant-sourced (no live point), make a new recording so their values get snapshotted -- recordings made before that existed can't supply them. See Recordings."
      />
      <div style="font-size:12px;color:var(--text-placeholder)">
        Using {{ usableVariables.length }} of {{ suggestions.variables.length }} variables mapped on this
        device's Simulation Model configuration ({{ usableInputCount }} input{{ usableInputCount === 1 ? '' : 's' }}<span v-if="constantSourcedInputCount"> — {{ constantSourcedInputCount }} constant, from the recording{{ constantSourcedInputCount === 1 ? '' : 's' }} themselves, not a live point</span>,
        {{ usableOutputCount }} output{{ usableOutputCount === 1 ? '' : 's' }}).
      </div>

      <a-form-item label="History window" style="margin-top:12px;margin-bottom:0">
        <a-input-number v-model:value="historyWindow" :min="1" :disabled="!!job" />
        <span style="margin-left:8px;font-size:12px;color:var(--text-placeholder)">
          Trailing ticks of prior outputs fed back in as features (1 = just the previous tick).
        </span>
      </a-form-item>
    </template>
  </a-card>

  <div v-if="selectedRecordingIds.length >= 2 && suggestions && !job" style="margin-bottom:16px">
    <a-button type="primary" :disabled="!canStart" :loading="starting" @click="startTraining">
      Start Training
    </a-button>
    <span v-if="!canStart" style="margin-left:8px;font-size:12px;color:var(--text-placeholder)">
      This device's Simulation Model has no usable variable across at least 2 of these recordings --
      check Simulation → Model, or pick different recordings.
    </span>
  </div>

  <a-card v-if="job" size="small" title="Status / Results">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <a-tag :color="STATUS_COLOR[job.status]">{{ job.status }}</a-tag>
      <span v-if="job.status === 'RUNNING' || job.status === 'QUEUED'"><a-spin size="small" /></span>
      <div style="flex:1" />
      <a-button
        v-if="job.status === 'QUEUED' || job.status === 'RUNNING'"
        danger :loading="cancelling" @click="cancelTraining"
      >
        <template #icon><StopOutlined /></template>
        Cancel
      </a-button>
      <a-button
        v-if="job.status === 'COMPLETED' && !usedModel"
        type="primary" :loading="usingModel" @click="useThisModel"
      >
        <template #icon><RobotOutlined /></template>
        {{ promotedModel ? 'Retry attaching model' : 'Use this model' }}
      </a-button>
      <a-button v-if="job.status !== 'RUNNING' && job.status !== 'QUEUED'" @click="startOver">
        New Training Run
      </a-button>
    </div>

    <a-alert
      v-if="promotedModel && usedModel"
      type="success" show-icon style="margin-bottom:12px"
      :message="wasNewModel ? `Now using ${promotedModel.label}` : `${promotedModel.label} updated`"
      :description="wasNewModel
        ? 'Attached to this device -- points carried over from the previous model.'
        : 'Retrained in place -- same model, same attachment, same mappings.'"
    />

    <a-alert
      v-if="attachError"
      type="warning" show-icon style="margin-bottom:12px"
      message="Model trained, but not yet attached"
      :description="attachError"
    />

    <a-alert
      v-if="job.status === 'FAILED'"
      type="error" show-icon :message="job.error || 'Training failed'"
      style="margin-bottom:12px"
    />

    <div v-if="results">
      <a-descriptions bordered size="small" :column="2" style="margin-bottom:16px">
        <a-descriptions-item label="Mean R²">
          <CheckCircleOutlined v-if="(results.aggregate.mean_r2 ?? 0) > 0.7" style="color:#52c41a" />
          {{ formatR2(results.aggregate.mean_r2) }}
        </a-descriptions-item>
        <a-descriptions-item label="Mean MAE">{{ formatMae(results.aggregate.mean_mae) }}</a-descriptions-item>
        <a-descriptions-item label="Train trajectories">{{ results.trajectories?.train ?? '—' }}</a-descriptions-item>
        <a-descriptions-item label="Test trajectories (held out)">{{ results.trajectories?.test ?? '—' }}</a-descriptions-item>
        <a-descriptions-item label="Train rows">{{ results.rows?.train ?? '—' }}</a-descriptions-item>
        <a-descriptions-item label="Test rows">{{ results.rows?.test ?? '—' }}</a-descriptions-item>
      </a-descriptions>

      <h4 style="margin:0 0 8px">Per-Output Metrics</h4>
      <a-table
        size="small"
        :pagination="false"
        :data-source="Object.entries(results.per_output_metrics ?? {}).map(([output, m]) => ({ output, ...m }))"
        :columns="[
          { title: 'Output', dataIndex: 'output', key: 'output' },
          { title: 'R²', dataIndex: 'r2', key: 'r2', customRender: ({ text }: any) => formatR2(text) },
          { title: 'MAE', dataIndex: 'mae', key: 'mae', customRender: ({ text }: any) => formatMae(text) },
        ]"
        row-key="output"
      />
    </div>
  </a-card>

  <a-card size="small" title="Past Runs" style="margin-top:16px">
    <a-spin v-if="loadingHistory" />
    <div v-else-if="!jobHistory.length" style="color:var(--text-placeholder);font-size:12px">
      No training runs yet for this model.
    </div>
    <a-table
      v-else
      size="small"
      :pagination="false"
      :data-source="jobHistory"
      row-key="job_id"
      :columns="[
        { title: 'Job', dataIndex: 'job_id', key: 'job_id' },
        { title: 'Status', dataIndex: 'status', key: 'status' },
        { title: 'Started', dataIndex: 'created_at', key: 'created_at' },
        { title: 'Completed', dataIndex: 'completed_at', key: 'completed_at' },
      ]"
    >
      <template #bodyCell="{ column, text, record }">
        <span v-if="column.key === 'job_id'" style="font-family:monospace;font-size:12px">{{ record.job_id }}</span>
        <a-tag v-else-if="column.key === 'status'" :color="STATUS_COLOR[record.status]">{{ record.status }}</a-tag>
        <span v-else-if="column.key === 'created_at' || column.key === 'completed_at'" style="font-size:12px">
          {{ formatTimestamp(text) }}
        </span>
      </template>
    </a-table>
  </a-card>
</template>
