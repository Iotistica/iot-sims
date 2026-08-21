<script setup lang="ts">
import { ref, reactive, computed, watch, nextTick } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, MinusCircleOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { SimObject, Meta, NotificationClass, PriorityArrayInfo } from '../types'
import { formatPresentValue } from '../format'

const props = defineProps<{
  open: boolean
  object: SimObject | null
  deviceId: number | undefined
  meta: Meta
  draftMode?: boolean
  draftObject?: Record<string, any> | null
  existingObjects?: { object_type: string; object_instance: number }[]
  mirrorMode?: boolean
  onBeforeSaveBehaviorChange?: () => Promise<boolean>
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  saved: []
  'draft-saved': [data: Record<string, any>]
}>()

const DEFAULT_PARAMS: Record<string, any> = {
  raw:         {},
  constant:    { value: 0 },
  manual:      { value: 0 },
  sine:        { base: 20, amplitude: 5, period_hours: 24, phase_hours: 0 },
  noise:       { base: 0, noise: 1 },
  random_walk: { value: 50, step: 1, min: 0, max: 100 },
  schedule:    { default: 18, blocks: [{ start: '07:00', value: 22 }, { start: '18:00', value: 18 }] },
  ramp:        { from: 0, to: 100, duration_minutes: 60, repeat: true },
  fault:       { base_behavior: 'constant', base_params: { value: 22 }, fault_type: 'spike', fault_value: 999, mtbf_minutes: 60, fault_duration_seconds: 30 },
}

const loading = ref(false)
const deleting = ref(false)
const form = reactive({
  object_type: 'analog-input',
  object_instance: 1,
  name: '',
  units: 'no-units',
  behavior: 'constant',
  enabled: true,
  number_of_states: 2,
  reliability: 'no-fault-detected',
  polarity: 'normal',
  point_type: null as string | null,
})
const params = ref<any>({ value: 0 })

// ── Intrinsic Reporting (alarm) config — editing only, needs a real object id ──

// Instance numbers only need to be unique per (device, object_type) — matches
// the DB's UNIQUE(device_id, object_type, object_instance) constraint (GH #20).
// Suggest the next one after the highest existing instance of that same type.
function nextInstanceFor(type: string): number {
  const instances = (props.existingObjects ?? []).filter(o => o.object_type === type).map(o => o.object_instance)
  return instances.length ? Math.max(...instances) + 1 : 0
}
const isAnalog = computed(() => form.object_type.startsWith('analog'))
const isBinary = computed(() => form.object_type.startsWith('binary'))
const isMultistate = computed(() => form.object_type.startsWith('multi-state'))
const showAlarmSection = computed(() => !props.draftMode && !!props.object && (isAnalog.value || isBinary.value || isMultistate.value))
// Polarity (GH #19) only exists on Binary Input/Output in bacpypes3's schema — not Binary Value.
const showPolarity = computed(() => form.object_type === 'binary-input' || form.object_type === 'binary-output')

const ALARM_TRANSITIONS = [
  { label: 'To Off-Normal', value: 'to-offnormal' },
  { label: 'To Fault', value: 'to-fault' },
  { label: 'To Normal', value: 'to-normal' },
]

const notificationClasses = ref<NotificationClass[]>([])
const alarmCfgLoading = ref(false)
const alarmCfgSaving = ref(false)
const alarmCfgExists = ref(false)
const alarmCfg = reactive({
  notification_class_id: null as number | null,
  enabled: true,
  event_enable: ['to-offnormal', 'to-fault', 'to-normal'] as string[],
  time_delay: 0,
  time_delay_normal: 0,
  high_limit: undefined as number | undefined,
  low_limit: undefined as number | undefined,
  deadband: 0,
  alarm_value: true,
  alarm_values: [] as number[],
})

function resetAlarmCfg() {
  alarmCfgExists.value = false
  Object.assign(alarmCfg, {
    notification_class_id: null, enabled: true,
    event_enable: ['to-offnormal', 'to-fault', 'to-normal'],
    time_delay: 0, time_delay_normal: 0,
    high_limit: undefined, low_limit: undefined, deadband: 0, alarm_value: true, alarm_values: [],
  })
}

async function loadAlarmSection() {
  if (!props.deviceId || !props.object) return
  alarmCfgLoading.value = true
  try {
    const [cfg, ncs] = await Promise.all([
      api.alarmConfig.get(props.deviceId, props.object.id),
      api.notificationClasses.list(props.deviceId),
    ])
    notificationClasses.value = ncs
    if (cfg) {
      alarmCfgExists.value = true
      Object.assign(alarmCfg, {
        notification_class_id: cfg.notification_class_id,
        enabled: cfg.enabled,
        event_enable: cfg.event_enable,
        time_delay: cfg.time_delay,
        time_delay_normal: cfg.time_delay_normal,
        high_limit: cfg.params.high_limit as number | undefined,
        low_limit: cfg.params.low_limit as number | undefined,
        deadband: (cfg.params.deadband as number | undefined) ?? 0,
        alarm_value: (cfg.params.alarm_value as boolean | undefined) ?? true,
        alarm_values: (cfg.params.alarm_values as number[] | undefined) ?? [],
      })
    } else {
      resetAlarmCfg()
    }
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load alarm config')
  } finally {
    alarmCfgLoading.value = false
  }
}

async function saveAlarmConfig() {
  if (!props.deviceId || !props.object) return
  alarmCfgSaving.value = true
  try {
    const params: Record<string, number | boolean | number[] | undefined> = isAnalog.value
      ? { high_limit: alarmCfg.high_limit, low_limit: alarmCfg.low_limit, deadband: alarmCfg.deadband }
      : isMultistate.value
      ? { alarm_values: alarmCfg.alarm_values }
      : { alarm_value: alarmCfg.alarm_value }
    await api.alarmConfig.set(props.deviceId, props.object.id, {
      notification_class_id: alarmCfg.notification_class_id,
      enabled: alarmCfg.enabled ? 1 : 0,
      event_enable: alarmCfg.event_enable,
      notify_type: 'alarm',
      time_delay: alarmCfg.time_delay,
      time_delay_normal: alarmCfg.time_delay_normal,
      params,
    })
    alarmCfgExists.value = true
    message.success('Alarm config saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save alarm config')
  } finally {
    alarmCfgSaving.value = false
  }
}

async function removeAlarmConfig() {
  if (!props.deviceId || !props.object) return
  try {
    await api.alarmConfig.del(props.deviceId, props.object.id)
    resetAlarmCfg()
    message.success('Alarm config removed')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to remove alarm config')
  }
}

// ── Priority Array (GH #17) — only the three Commandable *-output types have a real one ──

const COMMANDABLE_TYPES = ['analog-output', 'binary-output', 'multi-state-output']
const showPriorityArraySection = computed(() => !props.draftMode && !!props.object && COMMANDABLE_TYPES.includes(form.object_type))
const simulationOutputOwner = computed(() => props.object?.simulation_output_owner ?? null)
// Read-only display for the FMU/model's current raw value, wherever a
// Behavior's own "baseline" field (Base/Initial Value/From/Default/Base
// Behavior) is replaced with the live provider value instead of an
// editable one -- see SimEngine._apply_fmu_behavior() for the matching
// backend semantics this UI must stay in sync with.
const fmuBaseDisplay = computed(() => {
  if (!props.object || props.object.raw_provider_value == null) return '—'
  return formatPresentValue(props.object.object_type, props.object.raw_provider_value)
})
// "raw" (reset to the FMU/model's raw value) only means anything for a
// provider-owned point -- hidden for a normal point rather than offering a
// no-op choice with no explanation there.
const availableBehaviors = computed(() =>
  simulationOutputOwner.value ? props.meta.behaviors : props.meta.behaviors.filter((b: string) => b !== 'raw')
)

const priorityArray = ref<PriorityArrayInfo | null>(null)
const paLoading = ref(false)
const paWriting = ref<number | null>(null)
const paWriteSlot = ref(1)
const paWriteValue = ref<number>(0)
const paWriteActive = ref(true)

async function loadPriorityArray() {
  if (!props.deviceId || !props.object) return
  paLoading.value = true
  try {
    priorityArray.value = await api.objects.priorityArray(props.deviceId, props.object.id)
  } catch (e: unknown) {
    priorityArray.value = null
    message.error((e as Error).message ?? 'Failed to load priority array')
  } finally {
    paLoading.value = false
  }
}

async function writePriority(priority: number, value: unknown) {
  if (!props.deviceId || !props.object) return
  paWriting.value = priority
  try {
    priorityArray.value = await api.objects.writePriority(props.deviceId, props.object.id, priority, value)
    message.success(value === null ? `Priority ${priority} relinquished` : `Priority ${priority} set`)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to write priority')
  } finally {
    paWriting.value = null
  }
}

function submitPriorityWrite() {
  const value = isBinary.value ? paWriteActive.value : paWriteValue.value
  writePriority(paWriteSlot.value, value)
}

function relinquishPriority(priority: number) {
  writePriority(priority, null)
}

function addScheduleBlock() {
  if (!Array.isArray(params.value.blocks)) params.value.blocks = []
  params.value = { ...params.value, blocks: [...params.value.blocks, { start: '12:00', value: 0 }] }
}
function removeScheduleBlock(i: number | string) {
  const removeIndex = Number(i)
  params.value = { ...params.value, blocks: params.value.blocks.filter((_: any, idx: number) => idx !== removeIndex) }
}
function onFaultBaseChange() {
  const defaults: Record<string, any> = {
    constant: { value: 22 },
    sine: { base: 20, amplitude: 5, period_hours: 24, phase_hours: 0 },
    noise: { base: 20, noise: 1 },
    random_walk: { value: 50, step: 1, min: 0, max: 100 },
  }
  params.value = { ...params.value, base_params: defaults[params.value.base_behavior] ?? { value: 0 } }
}

// Guard: prevents the behavior watcher from resetting params during a programmatic load.
let skipBehaviorReset = false

watch(() => form.behavior, (b) => {
  if (skipBehaviorReset) return
  params.value = { ...(DEFAULT_PARAMS[b] ?? { value: 0 }) }
})

// Re-suggest the next free instance when the type changes while adding a new
// object. Never touch it while editing an existing object (GH #20).
watch(() => form.object_type, (type) => {
  if (skipBehaviorReset || props.object || (props.draftMode && props.draftObject)) return
  form.object_instance = nextInstanceFor(type)
})

// Watch both open and object so the form reinitialises whenever either changes —
// this handles the case where the user clicks Edit on a second object while the
// drawer is already open (open stays true, object prop changes).
watch([() => props.open, () => props.object, () => props.draftObject], ([open]) => {
  if (!open) return
  skipBehaviorReset = true
  const src = props.draftMode ? props.draftObject : props.object
  if (src) {
    Object.assign(form, {
      object_type: src.object_type ?? 'analog-input',
      object_instance: src.object_instance ?? 1,
      name: src.name ?? '',
      units: src.units ?? 'no-units',
      behavior: src.behavior ?? 'constant',
      enabled: !!src.enabled,
      number_of_states: src.number_of_states ?? 2,
      reliability: src.reliability ?? 'no-fault-detected',
      polarity: src.polarity ?? 'normal',
      point_type: src.point_type ?? null,
    })
    try {
      const raw = src.behavior_params
      params.value = { ...(typeof raw === 'string' ? JSON.parse(raw) : raw) }
    } catch { params.value = { value: 0 } }
  } else {
    const type = props.meta.object_types[0] ?? 'analog-input'
    Object.assign(form, {
      object_type: type,
      object_instance: nextInstanceFor(type), name: '', units: 'no-units', behavior: 'constant', enabled: true,
      number_of_states: 2, reliability: 'no-fault-detected', polarity: 'normal',
      point_type: null,
    })
    params.value = { value: 0 }
  }
  nextTick(() => { skipBehaviorReset = false })

  resetAlarmCfg()
  if (!props.draftMode && props.object) loadAlarmSection()

  priorityArray.value = null
  paWriteSlot.value = 1
  paWriteValue.value = 0
  paWriteActive.value = true
  if (showPriorityArraySection.value) loadPriorityArray()
})

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (props.draftMode) {
    emit('draft-saved', { ...form, behavior_params: JSON.stringify(params.value) })
    emit('update:open', false)
    return
  }
  if (!props.deviceId) return
  if (props.mirrorMode && props.object && form.behavior !== props.object.behavior) {
    if (props.onBeforeSaveBehaviorChange) {
      const proceed = await props.onBeforeSaveBehaviorChange()
      if (!proceed) return
    }
  }
  loading.value = true
  const body = { ...form, enabled: form.enabled ? 1 : 0, behavior_params: JSON.stringify(params.value) }
  try {
    if (props.object) {
      await api.objects.update(props.deviceId, props.object.id, body)
      message.success('Object updated')
    } else {
      await api.objects.create(props.deviceId, body)
      message.success('Object created')
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function doDelete() {
  if (!props.object || !props.deviceId) return
  const obj = props.object
  const deviceId = props.deviceId
  Modal.confirm({
    title: `Delete "${obj.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      deleting.value = true
      try {
        await api.objects.del(deviceId, obj.id)
        message.success('Object deleted')
        emit('update:open', false)
        emit('saved')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete object')
      } finally {
        deleting.value = false
      }
    },
  })
}
</script>

<template>
  <a-drawer
    :title="object ? 'Edit Object' : 'Add Object'"
    :open="open"
    width="480"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-row :gutter="12">
        <a-col :span="14">
          <a-form-item label="Object Type" required>
            <a-select v-model:value="form.object_type">
              <a-select-option v-for="t in meta.object_types" :key="t" :value="t">{{ t }}</a-select-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="10">
          <a-form-item label="Object Instance" required>
            <a-input-number v-model:value="form.object_instance" :min="0" style="width:100%" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Supply Temp" />
      </a-form-item>

      <a-form-item label="Semantic Type" help="Describes what this point represents in the building.">
        <a-select
          v-model:value="form.point_type"
          show-search
          allow-clear
          placeholder="Not classified"
          :options="meta.point_types"
        />
      </a-form-item>

      <a-form-item v-if="isMultistate" label="Number of States" required>
        <a-input-number v-model:value="form.number_of_states" :min="1" :max="254" style="width:100%" />
      </a-form-item>
      <a-form-item v-else label="Units">
        <a-select v-model:value="form.units" show-search>
          <a-select-option v-for="u in meta.units" :key="u" :value="u">{{ u }}</a-select-option>
        </a-select>
      </a-form-item>

      <a-form-item v-if="simulationOutputOwner" label="Provider">
        <a-tag :color="simulationOutputOwner?.provider_type === 'fmu' ? 'purple' : 'cyan'">
          {{ simulationOutputOwner?.provider_type === 'fmu' ? 'FMU' : 'Learned' }}
        </a-tag>
        <span style="font-size:12px;color:var(--text-secondary)">
          {{ simulationOutputOwner?.name }} / {{ simulationOutputOwner?.variable }}
        </span>
      </a-form-item>

      <a-form-item
        label="Behavior"
        :help="simulationOutputOwner ? 'Applied on top of the model\'s live value every tick.' : undefined"
      >
        <a-select v-model:value="form.behavior">
          <a-select-option v-for="b in availableBehaviors" :key="b" :value="b">{{ b }}</a-select-option>
        </a-select>
      </a-form-item>

      <!-- Behavior params -->
      <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:14px">
        <!-- raw (FMU-owned points only) -->
        <div v-if="form.behavior === 'raw'" style="font-size:12px;color:var(--text-secondary)">
          No perturbation or override — Present Value tracks the model's live output directly. Select this to reset out of any other configured Behavior.
        </div>

        <!-- constant -->
        <template v-else-if="form.behavior === 'constant'">
          <div v-if="simulationOutputOwner" style="font-size:12px;color:var(--text-secondary)">
            No transformation — Present Value follows the model's live output directly.
          </div>
          <a-form-item v-else label="Value" style="margin-bottom:0">
            <a-input-number v-model:value="params.value" style="width:100%" :step="0.1" />
          </a-form-item>
        </template>

        <!-- manual -->
        <template v-else-if="form.behavior === 'manual'">
          <a-form-item :label="simulationOutputOwner ? 'Override Value' : 'Initial Value'" style="margin-bottom:4px">
            <a-input-number v-model:value="params.value" style="width:100%" :step="0.1" />
          </a-form-item>
          <div style="font-size:11px;color:var(--text-secondary)">
            {{ simulationOutputOwner
              ? 'Overrides the model\'s live value while this Behavior is set; changing Behavior returns the point to the model value.'
              : 'Can be overridden at runtime via "Set Value"' }}
          </div>
        </template>

        <!-- sine -->
        <template v-else-if="form.behavior === 'sine'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item v-if="simulationOutputOwner" label="Base (FMU)">
                <a-input :value="fmuBaseDisplay" disabled style="width:100%;font-family:monospace" />
              </a-form-item>
              <a-form-item v-else label="Base">
                <a-input-number v-model:value="params.base" style="width:100%" :step="0.1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Amplitude">
                <a-input-number v-model:value="params.amplitude" style="width:100%" :step="0.1" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Period (hours)" style="margin-bottom:0">
                <a-input-number v-model:value="params.period_hours" style="width:100%" :min="0.1" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Phase (hours)" style="margin-bottom:0">
                <a-input-number v-model:value="params.phase_hours" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>

        <!-- noise -->
        <template v-else-if="form.behavior === 'noise'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item v-if="simulationOutputOwner" label="Base (FMU)" style="margin-bottom:0">
                <a-input :value="fmuBaseDisplay" disabled style="width:100%;font-family:monospace" />
              </a-form-item>
              <a-form-item v-else label="Base" style="margin-bottom:0">
                <a-input-number v-model:value="params.base" style="width:100%" :step="0.1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Noise ±" style="margin-bottom:0">
                <a-input-number v-model:value="params.noise" style="width:100%" :min="0" :step="0.1" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>

        <!-- random_walk -->
        <template v-else-if="form.behavior === 'random_walk'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item v-if="simulationOutputOwner" label="Base (FMU)">
                <a-input :value="fmuBaseDisplay" disabled style="width:100%;font-family:monospace" />
              </a-form-item>
              <a-form-item v-else label="Initial Value">
                <a-input-number v-model:value="params.value" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Step ±">
                <a-input-number v-model:value="params.step" style="width:100%" :min="0" :step="0.1" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item :label="simulationOutputOwner ? 'Min Offset' : 'Min'" style="margin-bottom:0">
                <a-input-number v-model:value="params.min" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item :label="simulationOutputOwner ? 'Max Offset' : 'Max'" style="margin-bottom:0">
                <a-input-number v-model:value="params.max" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>

        <!-- schedule -->
        <template v-else-if="form.behavior === 'schedule'">
          <a-form-item v-if="simulationOutputOwner" label="Default" style="margin-bottom:10px">
            <a-input value="FMU / Model Value" disabled style="width:100%" />
          </a-form-item>
          <a-form-item v-else label="Default value (before first block)" style="margin-bottom:10px">
            <a-input-number v-model:value="params.default" style="width:100%" :step="1" />
          </a-form-item>
          <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:6px">Time Blocks</div>
          <div
            v-for="(block, i) in params.blocks" :key="i"
            style="display:flex;gap:8px;align-items:center;margin-bottom:6px"
          >
            <a-input v-model:value="block.start" placeholder="HH:MM" style="width:86px;font-family:monospace" />
            <a-input-number v-model:value="block.value" :step="0.5" style="flex:1" placeholder="Value" />
            <a-button type="text" size="small" danger @click="removeScheduleBlock(Number(i))">
              <template #icon><MinusCircleOutlined /></template>
            </a-button>
          </div>
          <a-button size="small" block @click="addScheduleBlock" style="margin-top:2px">
            <template #icon><PlusOutlined /></template>
            Add Time Block
          </a-button>
          <div style="font-size:11px;color:var(--text-secondary);margin-top:6px">
            Blocks fire at the given wall-clock time; value before the first block uses the default.
          </div>
        </template>

        <!-- ramp -->
        <template v-else-if="form.behavior === 'ramp'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item v-if="simulationOutputOwner" label="Base (FMU)">
                <a-input :value="fmuBaseDisplay" disabled style="width:100%;font-family:monospace" />
              </a-form-item>
              <a-form-item v-else label="From">
                <a-input-number v-model:value="params.from" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item :label="simulationOutputOwner ? 'Offset To' : 'To'">
                <a-input-number v-model:value="params.to" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="14">
              <a-form-item label="Duration (minutes)" style="margin-bottom:0">
                <a-input-number v-model:value="params.duration_minutes" :min="0.1" :step="5" style="width:100%" />
              </a-form-item>
            </a-col>
            <a-col :span="10">
              <a-form-item label="Repeat" style="margin-bottom:0">
                <a-switch v-model:checked="params.repeat" style="margin-top:5px" />
              </a-form-item>
            </a-col>
          </a-row>
          <div v-if="simulationOutputOwner" style="font-size:11px;color:var(--text-secondary);margin-top:6px">
            Drifts from the model's live value toward model + Offset To over the configured duration.
          </div>
        </template>

        <!-- fault -->
        <template v-else-if="form.behavior === 'fault'">
          <a-row :gutter="12" style="margin-bottom:2px">
            <a-col :span="12" v-if="simulationOutputOwner">
              <a-form-item label="Base">
                <a-input value="FMU / Model Value" disabled style="width:100%" />
              </a-form-item>
            </a-col>
            <a-col :span="12" v-else>
              <a-form-item label="Base Behavior">
                <a-select v-model:value="params.base_behavior" @change="onFaultBaseChange">
                  <a-select-option value="constant">constant</a-select-option>
                  <a-select-option value="sine">sine</a-select-option>
                  <a-select-option value="noise">noise</a-select-option>
                  <a-select-option value="random_walk">random_walk</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Fault Type">
                <a-select v-model:value="params.fault_type">
                  <a-select-option value="spike">spike</a-select-option>
                  <a-select-option value="stuck">stuck</a-select-option>
                  <a-select-option value="offline">offline (→ 0)</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
          </a-row>

          <div v-if="!simulationOutputOwner" style="background:var(--surface-alt);border-radius:4px;padding:10px;margin-bottom:10px">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;font-weight:600">Base behavior params</div>
            <template v-if="params.base_behavior === 'constant'">
              <a-form-item label="Value" style="margin-bottom:0">
                <a-input-number v-model:value="params.base_params.value" style="width:100%" :step="0.5" />
              </a-form-item>
            </template>
            <template v-else-if="params.base_behavior === 'sine'">
              <a-row :gutter="8">
                <a-col :span="12">
                  <a-form-item label="Base" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.base" style="width:100%" :step="0.5" />
                  </a-form-item>
                </a-col>
                <a-col :span="12">
                  <a-form-item label="Amplitude" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.amplitude" style="width:100%" :step="0.5" />
                  </a-form-item>
                </a-col>
              </a-row>
            </template>
            <template v-else-if="params.base_behavior === 'noise'">
              <a-row :gutter="8">
                <a-col :span="12">
                  <a-form-item label="Base" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.base" style="width:100%" :step="0.5" />
                  </a-form-item>
                </a-col>
                <a-col :span="12">
                  <a-form-item label="Noise ±" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.noise" :min="0" style="width:100%" :step="0.1" />
                  </a-form-item>
                </a-col>
              </a-row>
            </template>
            <template v-else-if="params.base_behavior === 'random_walk'">
              <a-row :gutter="8">
                <a-col :span="8">
                  <a-form-item label="Start" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.value" style="width:100%" :step="1" />
                  </a-form-item>
                </a-col>
                <a-col :span="8">
                  <a-form-item label="Min" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.min" style="width:100%" :step="1" />
                  </a-form-item>
                </a-col>
                <a-col :span="8">
                  <a-form-item label="Max" style="margin-bottom:0">
                    <a-input-number v-model:value="params.base_params.max" style="width:100%" :step="1" />
                  </a-form-item>
                </a-col>
              </a-row>
            </template>
          </div>

          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Fault Value" style="margin-bottom:0">
                <a-input-number v-model:value="params.fault_value" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item
                label="MTBF (minutes)"
                style="margin-bottom:0"
                tooltip="Mean Time Between Failures — the average number of minutes between fault injections. Each is random (not fixed-interval), so actual gaps vary; lower values mean more frequent faults."
              >
                <a-input-number v-model:value="params.mtbf_minutes" :min="0.1" :step="5" style="width:100%" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-form-item v-if="params.fault_type !== 'spike'" label="Fault Duration (seconds)" style="margin-top:10px;margin-bottom:0">
            <a-input-number v-model:value="params.fault_duration_seconds" :min="1" :step="5" style="width:100%" />
          </a-form-item>
          <div style="font-size:11px;color:var(--text-secondary);margin-top:6px">
            spike = one bad reading; stuck = freeze at fault value; offline = drop to zero
          </div>
          <div v-if="simulationOutputOwner" style="font-size:11px;color:var(--text-secondary);margin-top:2px">
            Outside a fault window, Present Value follows the model's live output directly.
          </div>
        </template>
      </div>

      <a-form-item
        label="Reliability"
        style="margin-top:16px;margin-bottom:0"
        tooltip="Simulates a BACnet fault condition on this object. Non-default values set statusFlags.fault so client software can be tested against fault handling."
      >
        <a-select v-model:value="form.reliability">
          <a-select-option v-for="r in meta.reliability_options" :key="r" :value="r">{{ r }}</a-select-option>
        </a-select>
      </a-form-item>

      <a-form-item
        v-if="showPolarity"
        label="Polarity"
        style="margin-top:16px;margin-bottom:0"
        tooltip="Reverse polarity means the physical/electrical state is inverted relative to the logical active/inactive presentValue — informational for BACnet clients, purely cosmetic in this simulator."
      >
        <a-select v-model:value="form.polarity">
          <a-select-option v-for="p in meta.polarity_options" :key="p" :value="p">{{ p }}</a-select-option>
        </a-select>
      </a-form-item>

      <template v-if="showAlarmSection">
        <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:20px 0 10px">
          Intrinsic Reporting (Alarms)
        </div>
        <a-spin :spinning="alarmCfgLoading">
          <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:14px">
            <a-form-item label="Enable alarming for this object" style="margin-bottom:12px">
              <a-switch v-model:checked="alarmCfg.enabled" />
            </a-form-item>

            <a-form-item label="Notification Class" style="margin-bottom:12px">
              <a-select v-model:value="alarmCfg.notification_class_id" allow-clear placeholder="None — logged locally only, no routed notification">
                <a-select-option v-for="nc in notificationClasses" :key="nc.id" :value="nc.id">{{ nc.name }}</a-select-option>
              </a-select>
            </a-form-item>

            <template v-if="isAnalog">
              <a-row :gutter="12">
                <a-col :span="8">
                  <a-form-item label="High Limit">
                    <a-input-number v-model:value="alarmCfg.high_limit" style="width:100%" :step="0.5" placeholder="none" />
                  </a-form-item>
                </a-col>
                <a-col :span="8">
                  <a-form-item label="Low Limit">
                    <a-input-number v-model:value="alarmCfg.low_limit" style="width:100%" :step="0.5" placeholder="none" />
                  </a-form-item>
                </a-col>
                <a-col :span="8">
                  <a-form-item label="Deadband">
                    <a-input-number v-model:value="alarmCfg.deadband" :min="0" style="width:100%" :step="0.5" />
                  </a-form-item>
                </a-col>
              </a-row>
            </template>
            <template v-else-if="isMultistate">
              <a-form-item label="Alarm states" :tooltip="`Which of the ${form.number_of_states} state(s) count as off-normal`">
                <a-checkbox-group v-model:value="alarmCfg.alarm_values">
                  <a-checkbox v-for="s in form.number_of_states" :key="s" :value="s">{{ s }}</a-checkbox>
                </a-checkbox-group>
              </a-form-item>
            </template>
            <template v-else>
              <a-form-item label="Alarm when value is">
                <a-radio-group v-model:value="alarmCfg.alarm_value">
                  <a-radio-button :value="true">Active</a-radio-button>
                  <a-radio-button :value="false">Inactive</a-radio-button>
                </a-radio-group>
              </a-form-item>
            </template>

            <a-row :gutter="12">
              <a-col :span="12">
                <a-form-item label="Time Delay (s)" tooltip="How long the condition must persist before an off-normal/fault transition is confirmed">
                  <a-input-number v-model:value="alarmCfg.time_delay" :min="0" :step="5" style="width:100%" />
                </a-form-item>
              </a-col>
              <a-col :span="12">
                <a-form-item label="Time Delay Normal (s)" tooltip="Same, for the return-to-normal transition">
                  <a-input-number v-model:value="alarmCfg.time_delay_normal" :min="0" :step="5" style="width:100%" />
                </a-form-item>
              </a-col>
            </a-row>

            <a-form-item label="Generate notifications for" style="margin-bottom:0">
              <a-checkbox-group v-model:value="alarmCfg.event_enable" :options="ALARM_TRANSITIONS" />
            </a-form-item>
          </div>

          <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px">
            <a-button v-if="alarmCfgExists" danger size="small" @click="removeAlarmConfig">Remove Alarm Config</a-button>
            <a-button type="primary" size="small" :loading="alarmCfgSaving" @click="saveAlarmConfig">Save Alarm Config</a-button>
          </div>
        </a-spin>
      </template>

      <template v-if="showPriorityArraySection">
        <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:20px 0 10px">
          Priority Array
        </div>
        <a-spin :spinning="paLoading">
          <div style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:14px">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
              The real BACnet 16-slot priority array. The lowest-numbered non-null slot wins; the sim's own value
              always occupies priority 16 and is overwritten every tick — write a higher priority (1–15) here to
              simulate another client asserting control.
            </div>

            <div v-if="priorityArray" style="max-height:260px;overflow:auto;margin-bottom:12px">
              <div
                v-for="slot in 16" :key="slot"
                style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border-subtle);font-size:12px"
              >
                <span style="width:70px;color:var(--text-muted)">Priority {{ slot }}</span>
                <span style="flex:1;font-family:monospace">
                  {{ formatPresentValue(form.object_type, priorityArray.priority_array[slot - 1]) }}
                </span>
                <a-tag v-if="priorityArray.current_command_priority === slot" color="processing" style="margin-right:0">In control</a-tag>
                <a-button
                  v-if="priorityArray.priority_array[slot - 1] !== null"
                  size="small" type="text" danger
                  :loading="paWriting === slot"
                  @click="relinquishPriority(slot)"
                >
                  Relinquish
                </a-button>
              </div>
              <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
                <span style="width:70px;color:var(--text-muted)">Default</span>
                <span style="flex:1;font-family:monospace">{{ formatPresentValue(form.object_type, priorityArray.relinquish_default) }}</span>
                <span style="font-size:11px;color:var(--text-placeholder)">used when all 16 slots are relinquished</span>
              </div>
            </div>

            <a-row :gutter="8" align="middle">
              <a-col :span="7">
                <a-input-number v-model:value="paWriteSlot" :min="1" :max="15" style="width:100%" addon-before="Priority" />
              </a-col>
              <a-col :span="isBinary ? 9 : 11">
                <a-input-number v-if="!isBinary" v-model:value="paWriteValue" style="width:100%" :step="isMultistate ? 1 : 0.1" placeholder="Value" />
                <a-radio-group v-else v-model:value="paWriteActive" style="width:100%">
                  <a-radio-button :value="true" style="width:50%;text-align:center">Active</a-radio-button>
                  <a-radio-button :value="false" style="width:50%;text-align:center">Inactive</a-radio-button>
                </a-radio-group>
              </a-col>
              <a-col :span="isBinary ? 8 : 6">
                <a-button block :loading="paWriting !== null" @click="submitPriorityWrite">Write</a-button>
              </a-col>
            </a-row>
          </div>
        </a-spin>
      </template>
    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a-button v-if="object && !draftMode" danger :loading="deleting" @click="doDelete">Delete</a-button>
        <div v-else />
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ object ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
