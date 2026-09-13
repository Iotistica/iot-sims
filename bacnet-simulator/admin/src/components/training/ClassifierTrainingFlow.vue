<script setup lang="ts">
/** Choose What to Detect -> Choose Recordings -> Training Data Coverage -> Create Fault
 * Detection Model -> Review Detection Results -> Save / Use on Equipment. Internal name/props
 * shape mirrors TrainingFlow.vue (task_type="classification" instead of the default
 * "regression"), but the two are separate components -- a classifier's target picker, coverage
 * table, and accuracy/confusion-matrix results have no equivalent in the regression flow, and a
 * classifier's own recording picker/mapping only ever uses input (recorded-point) variables,
 * never output ones.
 *
 * User-facing language throughout is building-automation/FDD terminology, not ML jargon (see the
 * approved plan's "Frontend terminology mapping" table) -- every internal name (classifier_target,
 * class_labels, trajectory, provider_type, RandomForestClassifier, etc.) stays exactly as it is
 * everywhere else; only what's shown/clicked here changes. */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined, RobotOutlined, StopOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type { SimulationModelPayload } from '../../api'
import type {
  CalibrationRecordingSummary, ClassificationResult, Device, FaultType, FeatureChoice,
  PromotedModelSummary, TrainingJob, TrainingMappingSuggestions,
} from '../../types'

// Building-automation labels for the handful of Brick point classes this flow is most likely to
// show -- falls back to the raw brick_class string (still readable, just not prettified) for
// anything not in this small list, never hidden. Kept local/minimal rather than importing the
// full backend POINT_TYPES vocabulary, which this admin app doesn't otherwise mirror client-side.
const POINT_CLASS_LABEL: Record<string, string> = {
  Zone_Air_Temperature_Sensor: 'Zone Air Temperature', Room_Air_Temperature_Setpoint: 'Zone Temperature Setpoint',
  Supply_Air_Flow_Sensor: 'Supply Air Flow', Damper_Position_Command: 'Damper Position',
  Fan_Speed_Command: 'Fan Speed Command', Fan_Status: 'Fan Status',
  Supply_Air_Temperature_Sensor: 'Supply Air Temperature', Return_Air_Temperature_Sensor: 'Return Air Temperature',
  Outside_Air_Temperature_Sensor: 'Outside Air Temperature',
}
function brickLabel(pointClass: string, equipmentPath: string[] | null | undefined): string {
  const base = POINT_CLASS_LABEL[pointClass] ?? pointClass.replace(/_/g, ' ')
  const path = (equipmentPath ?? []).map((p) => p.replace(/_/g, ' ')).join(' → ')
  return path ? `${path} → ${base}` : base
}
const PORTABILITY_LABEL: Record<string, string> = {
  portable: 'Portable — works on any equivalent equipment',
  partial: 'Semi-portable — some points are named, not fully standardized',
  device_specific: 'This device only',
}
const PORTABILITY_COLOR: Record<string, string> = { portable: 'green', partial: 'gold', device_specific: 'default' }

const props = defineProps<{ modelId: string; deviceId: number; device: Device }>()

// Same tag/color convention as RecordingsDrawer.vue/TrainingFlow.vue's own
// CONDITION_TAG/FAULT_TYPE_TAG -- display only.
const CONDITION_TAG: Record<'normal' | 'degraded' | 'fault', { label: string; color: string }> = {
  normal: { label: 'Normal', color: 'green' },
  degraded: { label: 'Degraded', color: 'orange' },
  fault: { label: 'Fault', color: 'red' },
}
const FAULT_TYPE_TAG: Record<FaultType, string> = {
  sensor_bias: 'Sensor Bias', sensor_stuck: 'Sensor Stuck', actuator_stuck: 'Actuator Stuck',
  actuator_leak: 'Actuator Leak', control_unstable: 'Control Unstable', incorrect_setpoint: 'Incorrect Setpoint',
  simultaneous_heat_cool: 'Simultaneous Heat/Cool', filter_fouling: 'Filter Fouling',
  compressor_staging: 'Compressor Staging', condenser_fouling: 'Condenser Fouling', evaporator_fouling: 'Evaporator Fouling',
  refrigerant_overcharge: 'Refrigerant Overcharge', refrigerant_undercharge: 'Refrigerant Undercharge',
  refrigerant_line_restriction: 'Refrigerant Line Restriction', heat_exchanger_fouling: 'Heat Exchanger Fouling',
  water_flow_restriction: 'Water Flow Restriction', cavitation: 'Cavitation', ignition_failure: 'Ignition Failure',
  low_water_cutoff_fault: 'Low Water Cutoff Fault', belt_or_drive_fault: 'Belt/Drive Fault',
}

// ── Step 1: What should the model detect? ────────────────────────────────
const classifierTarget = ref<'condition' | 'fault_type'>('condition')

// ── Step 2: Choose recordings to learn from ──────────────────────────────
const loadingRecordings = ref(false)
const allRecordings = ref<CalibrationRecordingSummary[]>([])
const selectedRecordingIds = ref<number[]>([])

async function loadRecordings() {
  loadingRecordings.value = true
  try {
    allRecordings.value = await api.training.listRecordings(props.modelId, props.deviceId)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load recordings')
  } finally {
    loadingRecordings.value = false
  }
}
onMounted(loadRecordings)

// The label a recording contributes for the CURRENTLY chosen target.
// classifierTarget === 'condition' is a direct read of the recording's own field. For
// 'fault_type', a recording only ever has fault_type set when its condition is
// Degraded/Fault (RecordingsDrawer.vue hides/clears the field entirely for Normal -- there's
// nothing to classify for a normal recording at LABELING time). But a fault-type classifier
// still needs a "normal" baseline class to actually distinguish faults from -- without this,
// a Normal recording could never be selected at all when training a fault-type model, making
// it impossible to mix one Normal + one Fault recording (exactly the gap this fixes). "normal"
// here is a synthetic training label, never written back into recordings.fault_type itself
// (that column stays exactly the closed 20-value enum it always was).
function recordingLabel(rec: CalibrationRecordingSummary, target: 'condition' | 'fault_type'): string | null {
  if (target === 'fault_type' && rec.condition === 'normal') return 'normal'
  return rec[target] ?? null
}

// Only recordings carrying a label for the CURRENTLY chosen target -- an
// unlabeled recording can't teach the model anything for this target, so
// it's excluded from the picker entirely rather than offered and later
// rejected.
const eligibleRecordings = computed(() =>
  allRecordings.value.filter((r) => recordingLabel(r, classifierTarget.value) != null),
)

function onTargetChanged() {
  selectedRecordingIds.value = selectedRecordingIds.value.filter(
    (id) => eligibleRecordings.value.some((r) => r.id === id),
  )
}

// ── Training Data Coverage ────────────────────────────────────────────────
// Client-side mirror of recording_class_distribution() -- same shape,
// computed from the already-loaded recording list rather than a second
// request, so this updates instantly as the selection changes.
const coverage = computed(() => {
  const dist: Record<string, { label: string; recordings: number; samples: number }> = {}
  for (const id of selectedRecordingIds.value) {
    const rec = allRecordings.value.find((r) => r.id === id)
    const value = rec ? recordingLabel(rec, classifierTarget.value) : null
    if (value == null) continue
    const label = String(value)
    if (!dist[label]) dist[label] = { label, recordings: 0, samples: 0 }
    dist[label].recordings += 1
    dist[label].samples += rec?.sample_count ?? 0
  }
  return Object.values(dist).sort((a, b) => a.label.localeCompare(b.label))
})
const distinctClassCount = computed(() => coverage.value.length)

function classLabel(value: string): string {
  // "normal" shows the same way regardless of which target is active -- see recordingLabel's
  // own comment for why a fault-type classifier can have a synthetic "normal" class at all.
  if (value === 'normal') return CONDITION_TAG.normal.label
  return classifierTarget.value === 'condition'
    ? (CONDITION_TAG[value as 'normal' | 'degraded' | 'fault']?.label ?? value)
    : (FAULT_TYPE_TAG[value as FaultType] ?? value)
}

// ── Readiness messaging ───────────────────────────────────────────────────
// The coverage table alone doesn't tell an HVAC technician the thing that actually matters: a
// class with only 1 recording is enough to TRAIN from (the model can still learn what that
// condition looks like), but not enough to CHECK -- nothing is held out to test accuracy
// against, since there's no second, independent occurrence of that same condition to compare
// against. These two facts are easy to conflate ("more data would help") but are actually
// separate concerns, so they get two distinct messages rather than one generic warning repeated
// per class. See the plan's own no_held_out_data branch (train_classifier.py) -- this is the UI
// surfacing of exactly that backend behavior, phrased without ML terminology.
const evaluableClasses = computed(() => coverage.value.filter((c) => c.recordings >= 2))
const unevaluableClasses = computed(() => coverage.value.filter((c) => c.recordings < 2))
const allClassesEvaluable = computed(() => distinctClassCount.value >= 2 && unevaluableClasses.value.length === 0)
const noClassesEvaluable = computed(() => evaluableClasses.value.length === 0)

function joinNames(names: string[]): string {
  if (names.length <= 1) return names.join('')
  if (names.length === 2) return `${names[0]} and ${names[1]}`
  return `${names.slice(0, -1).join(', ')}, and ${names[names.length - 1]}`
}
// "Normal" reads better with "operation" appended when named alongside a fault ("Normal
// operation and Sensor Bias") -- a fault name already reads fine bare.
function occurrenceName(label: string): string {
  const name = classLabel(label)
  return label === 'normal' ? `${name} operation` : name
}

const readinessMessage = computed(() => (allClassesEvaluable.value ? 'Training data ready' : 'Ready to create a model'))
const readinessDescription = computed(() => {
  if (allClassesEvaluable.value) {
    return `The model has separate recordings available for learning and checking ${joinNames(coverage.value.map((c) => occurrenceName(c.label)))}. Model performance can be evaluated after training.`
  }
  if (noClassesEvaluable.value) {
    return `You have recordings of ${joinNames(coverage.value.map((c) => occurrenceName(c.label)))}, so the model has enough examples to learn the difference.`
  }
  return null // Partial coverage: the warning alert below carries the explanation instead.
})

const readinessWarningMessage = computed(() => {
  if (noClassesEvaluable.value) return 'Model performance cannot be checked yet'
  return `More ${joinNames(unevaluableClasses.value.map((c) => classLabel(c.label)))} data recommended`
})
const readinessWarningSentences = computed(() => {
  if (noClassesEvaluable.value) {
    return ['Each condition has only one recording. The model can be trained, but its accuracy on new operating data cannot yet be measured.']
  }
  const checked = evaluableClasses.value.map((c) => (
    c.label === 'normal'
      ? `${classLabel(c.label)} operation can now be checked using a separate recording.`
      : `${classLabel(c.label)} can now be checked using a separate recording.`
  ))
  const unchecked = unevaluableClasses.value.map((c) => (
    c.label === 'normal'
      ? `${classLabel(c.label)} has only one recording, so its baseline behavior cannot yet be checked against a separate recording.`
      : `${classLabel(c.label)} has only one recording, so its detection performance cannot yet be checked against a separate fault occurrence.`
  ))
  return [...checked, ...unchecked]
})
const readinessRecommendedNext = computed(() => {
  if (noClassesEvaluable.value) {
    return `Add ${joinNames(coverage.value.map((c) => `another ${classLabel(c.label)} recording`))}.`
  }
  return `Record ${joinNames(unevaluableClasses.value.map((c) => (
    c.label === 'normal' ? `another ${classLabel(c.label)} recording` : `another ${classLabel(c.label)} event`
  )))}.`
})

// ── Feature (input point) mapping -- reuses the same mapping-suggestions
// route the surrogate flow uses, but a classifier only ever uses INPUT
// variables as features; any output variable this device's model happens
// to declare is simply never offered here. ─────────────────────────────
const loadingMapping = ref(false)
const suggestions = ref<TrainingMappingSuggestions | null>(null)

async function loadMapping() {
  suggestions.value = null
  if (selectedRecordingIds.value.length < 2) return
  loadingMapping.value = true
  try {
    suggestions.value = await api.training.mappingSuggestions(selectedRecordingIds.value, props.modelId, props.deviceId)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load point mapping')
  } finally {
    loadingMapping.value = false
  }
}
function onRecordingsChanged() { loadMapping() }

const usableInputs = computed(() => {
  if (!suggestions.value) return []
  return suggestions.value.variables.filter(
    (v) => v.direction === 'input' && v.mapped
      && v.missing_in_recordings.length < selectedRecordingIds.value.length,
  )
})
const mappingPayload = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const v of usableInputs.value) if (v.object_id != null) out[v.name] = v.object_id
  return out
})

// ── Points Used by Model -- Brick auto-resolves first (see usableInputs' own `semantic` field
// from the mapping-suggestions response); anything unresolved needs an EXPLICIT user decision
// here before training can start. Never defaulted -- an entry only exists once the user has
// actually chosen custom or device-specific for that variable. ──────────────────────────────
const featureChoices = ref<Record<string, FeatureChoice>>({})
const customLabelDrafts = ref<Record<string, string>>({})

const unresolvedInputs = computed(() => usableInputs.value.filter((v) => !v.semantic?.resolved))

function chooseCustom(variableName: string) {
  const label = (customLabelDrafts.value[variableName] ?? '').trim()
  if (!label) return
  featureChoices.value = { ...featureChoices.value, [variableName]: { kind: 'custom', custom_label: label } }
}
function chooseDeviceSpecific(variableName: string) {
  featureChoices.value = { ...featureChoices.value, [variableName]: { kind: 'device_specific' } }
}
function clearChoice(variableName: string) {
  const next = { ...featureChoices.value }
  delete next[variableName]
  featureChoices.value = next
}

const allPointsDecided = computed(() =>
  unresolvedInputs.value.every((v) => !!featureChoices.value[v.name]),
)

const canStart = computed(() =>
  !!suggestions.value && usableInputs.value.length > 0 && distinctClassCount.value >= 2
  && selectedRecordingIds.value.length >= 2 && allPointsDecided.value,
)

// ── Observation Window (history_window) -- seconds shown are display-only;
// the value actually sent is always the sample-count history_window the
// existing lag implementation expects, never a seconds value. ───────────
const historyWindow = ref(1)
const observationSeconds = computed(() => {
  const interval = allRecordings.value.find((r) => selectedRecordingIds.value.includes(r.id))?.sample_interval_seconds
  return interval ? Math.round(historyWindow.value * interval) : null
})

// ── Create Fault Detection Model ──────────────────────────────────────────
const starting = ref(false)
const job = ref<TrainingJob | null>(null)
const results = ref<ClassificationResult | null>(null)

const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED'])
let pollTimer: ReturnType<typeof setInterval> | null = null
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }

async function pollJob() {
  if (!job.value) return
  try {
    job.value = await api.training.getJob(job.value.job_id, job.value.model_id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to refresh status')
    return
  }
  if (TERMINAL.has(job.value.status)) {
    stopPolling()
    if (job.value.status === 'COMPLETED') {
      try {
        results.value = await api.training.getClassificationResults(job.value.job_id, job.value.model_id)
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to load model results')
      }
    }
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
      task_type: 'classification',
      classifier_target: classifierTarget.value,
      feature_choices: featureChoices.value,
    })
    job.value = created
    results.value = null
    stopPolling()
    pollTimer = setInterval(pollJob, 2000)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to start')
  } finally {
    starting.value = false
  }
}

const cancelling = ref(false)
async function cancelTraining() {
  if (!job.value) return
  cancelling.value = true
  try {
    job.value = await api.training.cancelJob(job.value.job_id, job.value.model_id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to cancel')
  } finally {
    cancelling.value = false
  }
}

// ── Detection Results ──────────────────────────────────────────────────
const showAdvanced = ref(false)
function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v * 100)}%`
}
// Iterates class_labels, not per_class_metrics -- a class with NO held-out data at all (e.g.
// every class has only 1 recording so far) has no per_class_metrics entry whatsoever, and would
// otherwise silently vanish from this table instead of showing as "no test data yet".
const detectionRows = computed(() => {
  if (!results.value) return []
  return results.value.class_labels.map((label) => {
    const m = results.value!.per_class_metrics[label]
    return {
      label: classLabel(label), recall: m?.recall, precision: m?.precision, f1: m?.f1, support: m?.support,
      untested: results.value!.untested_classes.includes(label),
    }
  })
})

// ── Save to Model Library / Use Model on Equipment ────────────────────────
const usingModel = ref(false)
const promotedModel = ref<PromotedModelSummary | null>(null)
const usedModel = ref(false)
const attachError = ref<string | null>(null)

async function useThisModel() {
  if (!job.value) return
  usingModel.value = true
  attachError.value = null
  try {
    let promoted = promotedModel.value
    if (!promoted) {
      promoted = await api.training.promoteJob(job.value.job_id, job.value.model_id)
      promotedModel.value = promoted
    }
    const classLabels = promoted.class_labels ?? []

    // "Equipment Compatibility" -- resolves the model's semantic feature contract against THIS
    // device before wiring anything up. This phase's UI only ever attaches to the device the
    // model was trained on, so this is realistically always satisfiable here -- but it's the
    // exact same check a future different-target-device attach flow would reuse unchanged (see
    // resolve_semantic_contract_against_device's own docstring), and it still catches a real
    // problem: a point deleted/reclassified since training.
    const compatibility = await api.training.checkSemanticCompatibility(promoted.id, props.deviceId)
    if (!compatibility.ok) {
      const problems = [
        ...Object.entries(compatibility.missing).map(([name, reason]) => `${name}: ${reason}`),
        ...Object.keys(compatibility.ambiguous).map((name) => `${name}: more than one matching point found`),
      ]
      attachError.value = `Cannot use this model on this equipment -- ${problems.join('; ')}.`
      message.error(attachError.value)
      return
    }

    // Find an already-mapped multi-state point for this classifier's own
    // output variable (classifier_target) on this device's OTHER
    // configs -- unlikely on a first attach, but avoids creating a
    // second Detection Point if one already exists from a previous run.
    // Otherwise auto-create one, sized/labeled from class_labels -- the
    // whole reason State_Text exists (see migration 031): a raw integer
    // present-value would force every viewer to memorize what each
    // number means.
    const detectionPoint = await api.objects.create(props.deviceId, {
      object_type: 'multi-state-value',
      object_instance: await nextMultiStateInstance(),
      name: `${promoted.label} Detection Status`,
      units: 'no-units',
      behavior: 'raw',
      behavior_params: '{"value":0}',
      enabled: 1,
      number_of_states: Math.max(1, classLabels.length),
      reliability: 'no-fault-detected',
      polarity: 'normal',
      state_text: classLabels,
    })

    // A classifier is a SEPARATE model from whatever surrogate/FMU model
    // this device may already be running -- always a NEW
    // simulation_model_configs row (never update the device's existing
    // active_simulation_model, which is a different model entirely), so
    // both can run side by side. zero_based_to_multistate is the exact
    // existing conversion an FMU zero-based compressor-stage output
    // already uses for the identical 0-based -> 1-based BACnet shift
    // (see mapping/conversions.py) -- the classifier's own
    // prediction_index is unwrapped to this same shape at the provider
    // boundary (see providers/fmu.py's _convert_output_value).
    const payload: SimulationModelPayload = {
      name: `${props.device.name} ${promoted.label}`,
      provider_type: 'classifier',
      model_type: promoted.id,
      enabled: true,
      created_from_device_id: props.device.id,
      parameters: {},
      mappings: [
        ...Object.entries(mappingPayload.value).map(([variable, point_id]) => ({
          variable, direction: 'input' as const, point_id,
        })),
        {
          variable: promoted.classifier_target ?? classifierTarget.value,
          direction: 'output' as const,
          point_id: detectionPoint.id,
          conversion: 'zero_based_to_multistate',
        },
      ],
      aggregate_mappings: [],
      input_exposures: [],
    }
    await api.simulationModels.create(payload)
    usedModel.value = true
    message.success(`${promoted.label} is now watching this equipment live.`)
  } catch (e: unknown) {
    attachError.value = (e as Error).message ?? 'Failed to use this model on the equipment'
    message.error(attachError.value)
  } finally {
    usingModel.value = false
  }
}

// Picks a free object_instance for the auto-created Detection Point --
// same approach ObjectDrawer.vue's own nextInstanceFor() uses client-side,
// scoped to this one device's already-loaded object list isn't available
// here, so this asks the API for the device's current objects once.
async function nextMultiStateInstance(): Promise<number> {
  const objects = await api.objects.list(props.deviceId)
  const used = new Set(objects.filter((o) => o.object_type === 'multi-state-value').map((o) => o.object_instance))
  let instance = 1
  while (used.has(instance)) instance += 1
  return instance
}

function startOver() {
  stopPolling()
  job.value = null
  results.value = null
  promotedModel.value = null
  usedModel.value = false
  attachError.value = null
}

onUnmounted(stopPolling)

const STATUS_COLOR: Record<string, string> = {
  QUEUED: 'default', RUNNING: 'blue', COMPLETED: 'green', FAILED: 'red', CANCELLED: 'default',
}
</script>

<template>
  <a-card size="small" title="What should the model detect?" style="margin-bottom:16px">
    <a-radio-group v-model:value="classifierTarget" :disabled="!!job" @change="onTargetChanged" style="width:100%">
      <a-radio-button value="condition" style="width:50%;text-align:center">Equipment Condition</a-radio-button>
      <a-radio-button value="fault_type" style="width:50%;text-align:center">Fault Type</a-radio-button>
    </a-radio-group>
    <div style="font-size:12px;color:var(--text-placeholder);margin-top:8px">
      <template v-if="classifierTarget === 'condition'">
        Recognize whether equipment is operating normally or showing degraded/fault behavior (Normal · Degraded · Fault).
      </template>
      <template v-else>
        Recognize which specific type of equipment or sensor fault is occurring.
      </template>
    </div>
  </a-card>

  <a-card size="small" title="Choose recordings to learn from" style="margin-bottom:16px">
    <div style="font-size:12px;color:var(--text-placeholder);margin-bottom:8px">
      Only recordings already labeled with {{ classifierTarget === 'condition' ? 'an Equipment Condition' : 'a Fault Type' }} are shown -- label a recording first if it's missing here.
    </div>
    <a-select
      v-model:value="selectedRecordingIds"
      mode="multiple"
      placeholder="Choose labeled recordings"
      style="width:100%"
      :disabled="!!job"
      :loading="loadingRecordings"
      show-search
      :filter-option="(input: string, option: any) => option.label.toLowerCase().includes(input.toLowerCase())"
      @change="onRecordingsChanged"
    >
      <a-select-option
        v-for="r in eligibleRecordings" :key="r.id" :value="r.id"
        :label="`${r.name} ${r.device_name ?? ''}`"
      >
        {{ r.name }}
        <a-tag v-if="classifierTarget === 'condition'" :color="CONDITION_TAG[r.condition as 'normal'|'degraded'|'fault'].color" style="margin-left:4px;font-weight:normal">
          {{ CONDITION_TAG[r.condition as 'normal'|'degraded'|'fault'].label }}
        </a-tag>
        <!-- fault_type target: a Normal recording has no fault_type at all (nothing to
             classify at labeling time) -- shown as its own Condition tag instead, since
             recordingLabel() is what actually contributes the synthetic "normal" class here. -->
        <a-tag v-else-if="r.condition === 'normal'" :color="CONDITION_TAG.normal.color" style="margin-left:4px;font-weight:normal">
          {{ CONDITION_TAG.normal.label }}
        </a-tag>
        <a-tag v-else style="margin-left:4px;font-weight:normal">{{ FAULT_TYPE_TAG[r.fault_type as FaultType] }}</a-tag>
        <span style="color:var(--text-placeholder)">— {{ r.device_name ?? 'unknown device' }} · {{ r.sample_count }} samples</span>
      </a-select-option>
    </a-select>
    <div v-if="!loadingRecordings && !eligibleRecordings.length" style="color:var(--text-placeholder);font-size:12px;margin-top:8px">
      No recordings labeled for this yet. Open a recording and set its {{ classifierTarget === 'condition' ? 'Equipment Condition' : 'Fault Type' }} first.
    </div>
    <div
      v-else-if="selectedRecordingIds.length === 1"
      style="color:var(--text-placeholder);font-size:12px;margin-top:8px"
    >
      Select at least 1 more recording to continue -- the model needs multiple recordings to hold one out and check its own accuracy honestly.
    </div>
  </a-card>

  <a-card v-if="selectedRecordingIds.length >= 2" size="small" title="Training Data Coverage" style="margin-bottom:16px">
    <a-table
      size="small" :pagination="false" :data-source="coverage" row-key="label"
      :columns="[
        { title: classifierTarget === 'condition' ? 'Condition' : 'Fault', dataIndex: 'label', key: 'label' },
        { title: 'Recordings', dataIndex: 'recordings', key: 'recordings' },
        { title: 'Data Samples', dataIndex: 'samples', key: 'samples' },
      ]"
    >
      <template #bodyCell="{ column, text, record }">
        <span v-if="column.key === 'label'">{{ classLabel(record.label) }}</span>
        <span v-else>{{ text }}</span>
      </template>
    </a-table>
    <a-alert
      v-if="distinctClassCount < 2"
      type="error" show-icon style="margin-top:12px"
      message="Need at least 2 different conditions/faults"
      :description="`Only ${distinctClassCount} is represented across the selected recordings. Select recordings covering more than one condition or fault.`"
    />
    <template v-else>
      <a-alert
        type="success" show-icon style="margin-top:12px"
        :message="readinessMessage"
        :description="readinessDescription ?? undefined"
      />
      <a-alert
        v-if="!allClassesEvaluable"
        type="warning" show-icon style="margin-top:8px"
        :message="readinessWarningMessage"
      >
        <template #description>
          <p v-for="(sentence, i) in readinessWarningSentences" :key="i" style="margin:0 0 4px">{{ sentence }}</p>
          <p style="margin:8px 0 0"><strong>Recommended next:</strong> {{ readinessRecommendedNext }}</p>
        </template>
      </a-alert>
    </template>
    <a-alert
      v-if="!job && suggestions && usableInputs.length === 0"
      type="error" show-icon style="margin-top:12px"
      message="No points to learn from"
      description="This device's Simulation Model has no usable input point captured by these recordings. Check Simulation → Model, or pick different recordings."
    />

    <a-form-item label="Observation Window" style="margin-top:16px;margin-bottom:0">
      <a-input-number v-model:value="historyWindow" :min="1" :disabled="!!job" />
      <span style="margin-left:8px;font-size:12px;color:var(--text-placeholder)">
        How much recent equipment behavior the model considers when detecting a fault
        <template v-if="observationSeconds">— current values plus the previous {{ observationSeconds }}s of recorded point behavior.</template>
      </span>
    </a-form-item>
  </a-card>

  <a-card
    v-if="selectedRecordingIds.length >= 2 && suggestions && usableInputs.length"
    size="small" title="Points Used by Model" style="margin-bottom:16px"
  >
    <!-- The common case: every point is already Brick-mapped, so this is a one-glance
         confirmation, not a task -- no separate point-mapping step to work through. -->
    <div v-for="v in usableInputs" :key="v.name" style="padding:6px 0;border-bottom:1px solid var(--border-color-split, #f0f0f0)">
      <template v-if="v.semantic?.resolved">
        <CheckCircleOutlined style="color:#52c41a;margin-right:6px" />
        {{ brickLabel(v.semantic.point_class!, v.semantic.equipment_path) }}
      </template>
      <template v-else-if="featureChoices[v.name]">
        <CheckCircleOutlined style="color:#52c41a;margin-right:6px" />
        <span v-if="featureChoices[v.name].kind === 'custom'">{{ (featureChoices[v.name] as any).custom_label }} (custom)</span>
        <span v-else>{{ v.point_name ?? v.name }} (this device only)</span>
        <a-button type="link" size="small" @click="clearChoice(v.name)">Change</a-button>
      </template>
      <template v-else>
        <div style="color:#faad14">
          <strong>{{ v.point_name ?? v.name }}</strong> is not mapped to a recognized equipment meaning yet.
        </div>
        <div style="margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <a-input
            v-model:value="customLabelDrafts[v.name]" placeholder="Give it a name (e.g. Zone_CO2_Level)"
            style="width:220px" size="small"
          />
          <a-button size="small" :disabled="!customLabelDrafts[v.name]?.trim()" @click="chooseCustom(v.name)">Use this name</a-button>
          <a-button size="small" @click="chooseDeviceSpecific(v.name)">Use this exact point only</a-button>
        </div>
      </template>
    </div>
    <a-alert
      v-if="unresolvedInputs.length && !allPointsDecided"
      type="warning" show-icon style="margin-top:12px"
      message="Semantic mapping required"
      description="Some recorded points do not yet have a recognized equipment meaning. Map them in the Semantic Model panel first, or choose a name/device-specific option above, before creating this fault detection model."
    />
  </a-card>

  <div v-if="selectedRecordingIds.length >= 2 && suggestions && !job" style="margin-bottom:16px">
    <a-button type="primary" :disabled="!canStart" :loading="starting" @click="startTraining">
      Create Fault Detection Model
    </a-button>
    <span v-if="starting" style="margin-left:8px;font-size:12px;color:var(--text-placeholder)">Learning from your recordings…</span>
  </div>

  <a-card v-if="job" size="small" title="Model Results">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <a-tag :color="STATUS_COLOR[job.status]">{{ job.status }}</a-tag>
      <span v-if="job.status === 'RUNNING' || job.status === 'QUEUED'"><a-spin size="small" /> Learning from your recordings…</span>
      <div style="flex:1" />
      <a-button v-if="job.status === 'QUEUED' || job.status === 'RUNNING'" danger :loading="cancelling" @click="cancelTraining">
        <template #icon><StopOutlined /></template>
        Cancel
      </a-button>
      <a-button v-if="job.status === 'COMPLETED' && !usedModel" type="primary" :loading="usingModel" @click="useThisModel">
        <template #icon><RobotOutlined /></template>
        {{ promotedModel ? 'Retry' : 'Save to Model Library' }}
      </a-button>
      <a-button v-if="job.status !== 'RUNNING' && job.status !== 'QUEUED'" @click="startOver">Start Over</a-button>
    </div>

    <div v-if="usingModel" style="margin-bottom:12px;color:var(--text-placeholder);font-size:12px">
      <a-spin size="small" /> Checking that this equipment provides all points required by the model…
    </div>

    <a-alert
      v-if="usedModel"
      type="success" show-icon style="margin-bottom:12px"
      message="Your fault detection model is ready and watching this equipment"
      :description="`BACnet Detection Point created with states: ${(promotedModel?.class_labels ?? []).map((l, i) => `${i + 1} ${classLabel(l)}`).join(', ')}`"
    />
    <a-alert v-if="attachError" type="warning" show-icon style="margin-bottom:12px" message="Model saved, but not yet in use" :description="attachError" />
    <a-alert v-if="job.status === 'FAILED'" type="error" show-icon :message="job.error || 'Something went wrong while learning from your recordings'" style="margin-bottom:12px" />

    <a-tag
      v-if="promotedModel?.portability || results?.portability"
      :color="PORTABILITY_COLOR[(promotedModel?.portability ?? results?.portability)!]"
      style="margin-bottom:12px"
    >
      {{ PORTABILITY_LABEL[(promotedModel?.portability ?? results?.portability)!] }}
    </a-tag>

    <a-alert
      v-if="results && results.accuracy == null"
      type="info" show-icon style="margin-bottom:12px"
      message="Accuracy not yet measured"
      description="Every condition/fault only has 1 recording so far, so there's nothing held out to test against. The model is trained and ready, but add a second recording for at least one condition/fault to see real accuracy numbers."
    />

    <template v-if="results">
      <a-descriptions v-if="results.accuracy != null" bordered size="small" :column="1" style="margin-bottom:16px">
        <a-descriptions-item label="Overall Accuracy">
          <CheckCircleOutlined v-if="results.accuracy > 0.85" style="color:#52c41a" />
          {{ pct(results.accuracy) }}
          <span style="margin-left:8px;font-size:12px;color:var(--text-placeholder)">
            The model correctly identified the recorded condition in {{ pct(results.accuracy) }} of the test data.
          </span>
        </a-descriptions-item>
      </a-descriptions>

      <h4 style="margin:0 0 8px">Detection Results</h4>
      <a-table
        size="small" :pagination="false" :data-source="detectionRows" row-key="label"
        :columns="[
          { title: classifierTarget === 'condition' ? 'Condition' : 'Fault', dataIndex: 'label', key: 'label' },
          { title: 'Correctly Identified', dataIndex: 'recall', key: 'recall' },
        ]"
      >
        <template #bodyCell="{ column, text, record }">
          <span v-if="column.key === 'recall'">
            {{ record.untested ? 'No test data this run' : pct(text) }}
          </span>
          <span v-else>{{ text }}</span>
        </template>
      </a-table>

      <a-collapse style="margin-top:16px" ghost>
        <a-collapse-panel key="advanced" header="View Advanced Model Metrics">
          <a-descriptions bordered size="small" :column="2" style="margin-bottom:12px">
            <a-descriptions-item label="Train recordings">{{ results.trajectories.train }}</a-descriptions-item>
            <a-descriptions-item label="Test recordings (held out)">{{ results.trajectories.test }}</a-descriptions-item>
            <a-descriptions-item label="Train rows">{{ results.rows.train }}</a-descriptions-item>
            <a-descriptions-item label="Test rows">{{ results.rows.test }}</a-descriptions-item>
          </a-descriptions>
          <a-table
            size="small" :pagination="false"
            :data-source="Object.entries(results.per_class_metrics).map(([label, m]) => ({ label: classLabel(label), ...m }))"
            row-key="label"
            :columns="[
              { title: classifierTarget === 'condition' ? 'Condition' : 'Fault', dataIndex: 'label', key: 'label' },
              { title: 'Precision', dataIndex: 'precision', key: 'precision', customRender: ({ text }: any) => pct(text) },
              { title: 'Recall', dataIndex: 'recall', key: 'recall', customRender: ({ text }: any) => pct(text) },
              { title: 'F1', dataIndex: 'f1', key: 'f1', customRender: ({ text }: any) => (text == null ? '—' : text.toFixed(3)) },
              { title: 'Support', dataIndex: 'support', key: 'support' },
            ]"
          />
        </a-collapse-panel>
      </a-collapse>
    </template>
  </a-card>
</template>
