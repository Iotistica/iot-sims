<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, EditOutlined, DeleteOutlined, MinusCircleOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, SimObject, Schedule, ScheduleTimeValue, ScheduleException, Calendar } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const DAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
const DAY_LABELS: Record<string, string> = {
  monday: 'Monday', tuesday: 'Tuesday', wednesday: 'Wednesday', thursday: 'Thursday',
  friday: 'Friday', saturday: 'Saturday', sunday: 'Sunday',
}
const VALUE_TYPES = [
  { label: 'Analog (real)', value: 'real' },
  { label: 'Binary (boolean)', value: 'boolean' },
  { label: 'Multi-state (unsigned)', value: 'unsigned' },
]
const TARGET_TYPES_FOR: Record<string, string[]> = {
  real: ['analog-input', 'analog-output', 'analog-value'],
  boolean: ['binary-input', 'binary-output', 'binary-value'],
  unsigned: ['multi-state-input', 'multi-state-output', 'multi-state-value'],
}

const list = ref<Schedule[]>([])
const allObjects = ref<SimObject[]>([])
const allCalendars = ref<Calendar[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<Schedule | null>(null)
const formOpen = ref(false)
const evaluating = ref<number | null>(null)

const form = reactive({
  name: '',
  description: '',
  value_type: 'real' as 'real' | 'boolean' | 'unsigned',
  schedule_default: 0 as number | boolean,
  effective_start: '',
  effective_end: '',
  priority_for_writing: 10,
  enabled: true,
  target_ids: [] as number[],
  weekly: {} as Record<string, ScheduleTimeValue[]>,
  exceptions: [] as ScheduleException[],
})

const eligibleTargets = computed(() => {
  const types = TARGET_TYPES_FOR[form.value_type] ?? []
  return allObjects.value.filter(o => types.includes(o.object_type))
})

function onValueTypeChange() {
  const eligible = new Set(eligibleTargets.value.map(o => o.id))
  form.target_ids = form.target_ids.filter(id => eligible.has(id))
  form.schedule_default = form.value_type === 'boolean' ? false : 0
}

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    const [scheds, objects, calendars] = await Promise.all([
      api.schedules.list(props.device.id),
      api.objects.list(props.device.id),
      api.calendars.list(props.device.id),
    ])
    list.value = scheds
    allObjects.value = objects
    allCalendars.value = calendars
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load schedules')
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  if (v) load()
  else { formOpen.value = false; editing.value = null }
})

function emptyWeekly(): Record<string, ScheduleTimeValue[]> {
  return Object.fromEntries(DAY_NAMES.map(d => [d, []]))
}

function resetForm() {
  Object.assign(form, {
    name: '', description: '', value_type: 'real', schedule_default: 0,
    effective_start: '', effective_end: '', priority_for_writing: 10, enabled: true,
    target_ids: [], weekly: emptyWeekly(), exceptions: [],
  })
}

function openAdd() {
  editing.value = null
  resetForm()
  formOpen.value = true
}

function openEdit(sched: Schedule) {
  editing.value = sched
  Object.assign(form, {
    name: sched.name,
    description: sched.description,
    value_type: sched.value_type,
    schedule_default: sched.schedule_default,
    effective_start: sched.effective_start ?? '',
    effective_end: sched.effective_end ?? '',
    priority_for_writing: sched.priority_for_writing,
    enabled: sched.enabled,
    target_ids: sched.targets.map(t => t.object_id),
    weekly: { ...emptyWeekly(), ...sched.weekly_schedule },
    exceptions: sched.exception_schedule.map(e => ({ ...e, entries: [...e.entries] })),
  })
  formOpen.value = true
}

function addWeeklyEntry(day: string) {
  form.weekly[day].push({ time: '08:00:00', value: form.value_type === 'boolean' ? false : 0 })
}
function removeWeeklyEntry(day: string, i: number) {
  form.weekly[day].splice(i, 1)
}

function addException() {
  form.exceptions.push({
    period: { type: 'date', date: new Date().toISOString().slice(0, 10) },
    priority: 1,
    entries: [{ time: '00:00:00', value: form.value_type === 'boolean' ? false : 0 }],
  })
}
function removeException(i: number) {
  form.exceptions.splice(i, 1)
}
function addExceptionEntry(exc: ScheduleException) {
  exc.entries.push({ time: '08:00:00', value: form.value_type === 'boolean' ? false : 0 })
}
function removeExceptionEntry(exc: ScheduleException, i: number) {
  exc.entries.splice(i, 1)
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (!form.target_ids.length) { message.error('Choose at least one target object'); return }

  saving.value = true
  try {
    const body = {
      name: form.name.trim(),
      description: form.description,
      value_type: form.value_type,
      schedule_default: form.schedule_default,
      effective_start: form.effective_start || null,
      effective_end: form.effective_end || null,
      weekly_schedule: form.weekly,
      exception_schedule: form.exceptions,
      priority_for_writing: form.priority_for_writing,
      enabled: form.enabled ? 1 : 0,
      targets: form.target_ids.map(id => ({ object_id: id, property_identifier: 'present-value' })),
    }
    if (editing.value) {
      await api.schedules.update(editing.value.id, body as any)
      message.success('Schedule updated')
    } else {
      await api.schedules.create(props.device.id, body as any)
      message.success('Schedule created')
    }
    formOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Save failed')
  } finally {
    saving.value = false
  }
}

function confirmDelete(sched: Schedule) {
  Modal.confirm({
    title: `Delete "${sched.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.schedules.del(sched.id)
        message.success('Deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Delete failed')
      }
    },
  })
}

async function toggleEnabled(sched: Schedule, checked: boolean) {
  try {
    if (checked) await api.schedules.enable(sched.id)
    else await api.schedules.disable(sched.id)
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to update')
  }
}

async function evaluate(sched: Schedule) {
  evaluating.value = sched.id
  try {
    const result = await api.schedules.evaluate(sched.id)
    if (result.present_value === null) {
      message.info(`${sched.name}: ${result.source}`)
    } else {
      const next = result.next_transition ? new Date(result.next_transition).toLocaleString() : 'unknown'
      message.info(`${sched.name}: ${result.present_value} (${result.source}) — next transition ${next}`)
    }
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Evaluate failed')
  } finally {
    evaluating.value = null
  }
}

function targetLabel(sched: Schedule): string {
  return sched.targets.map(t => t.object_name).join(', ') || 'no targets'
}
function objectLabel(o: SimObject): string {
  return `${o.name} (${o.object_type})`
}
</script>

<template>
  <a-drawer
    :title="device ? `Schedules — ${device.name}` : 'Schedules'"
    :open="open"
    width="620"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <div style="font-size:12px;color:#888;margin-bottom:12px">
        Writes scheduled values to one or more target properties on this device, using a weekly timetable
        plus date-based exceptions. Higher-priority operator writes (lower BACnet priority number) are
        never overridden.
      </div>
      <a-button type="primary" block :disabled="!allObjects.length" @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Add Schedule
      </a-button>
      <div v-if="!allObjects.length" style="font-size:12px;color:#faad14;margin-top:-10px;margin-bottom:16px">
        This device has no points to schedule yet.
      </div>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:#bbb;padding:40px 0;font-size:13px">
          No schedules yet
        </div>
        <div
          v-for="sched in list" :key="sched.id"
          style="border:1px solid #e8e8e8;border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px">{{ sched.name }}</div>
              <div style="font-size:11px;color:#888;margin-top:2px">
                Controls <b>{{ targetLabel(sched) }}</b> · {{ sched.value_type }} · priority {{ sched.priority_for_writing }}
              </div>
              <div style="font-size:11px;color:#aaa;margin-top:2px">
                {{ sched.effective_start || sched.effective_end
                  ? `${sched.effective_start ?? 'always'} → ${sched.effective_end ?? 'always'}`
                  : 'always effective' }}
              </div>
            </div>
            <a-space :size="4">
              <a-switch size="small" :checked="sched.enabled" @change="(c: boolean) => toggleEnabled(sched, c)" />
              <a-button size="small" title="Evaluate current value" :loading="evaluating === sched.id" @click="evaluate(sched)">
                <template #icon><ThunderboltOutlined /></template>
              </a-button>
              <a-button size="small" title="Edit" @click="openEdit(sched)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(sched)">
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
          <a-input v-model:value="form.name" placeholder="e.g. Occupied-Hours" />
        </a-form-item>
        <a-form-item label="Description">
          <a-input v-model:value="form.description" placeholder="Optional description" />
        </a-form-item>

        <a-row :gutter="12">
          <a-col :span="12">
            <a-form-item label="Value Type">
              <a-select v-model:value="form.value_type" :options="VALUE_TYPES" @change="onValueTypeChange" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="Default Value" tooltip="Used before the first entry of the day and outside the effective period">
              <a-switch v-if="form.value_type === 'boolean'" v-model:checked="form.schedule_default as any" />
              <a-input-number v-else v-model:value="form.schedule_default as any" style="width:100%" :step="form.value_type === 'unsigned' ? 1 : 0.1" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="Target Objects" required>
          <a-select v-model:value="form.target_ids" mode="multiple" placeholder="Choose one or more objects to control">
            <a-select-option v-for="o in eligibleTargets" :key="o.id" :value="o.id">{{ objectLabel(o) }}</a-select-option>
          </a-select>
        </a-form-item>

        <a-row :gutter="12">
          <a-col :span="8">
            <a-form-item label="Effective Start" tooltip="Blank = always">
              <a-input v-model:value="form.effective_start" placeholder="YYYY-MM-DD" style="font-family:monospace" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="Effective End" tooltip="Blank = always">
              <a-input v-model:value="form.effective_end" placeholder="YYYY-MM-DD" style="font-family:monospace" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="Write Priority (1-16)">
              <a-input-number v-model:value="form.priority_for_writing" :min="1" :max="16" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>

        <div style="font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px;margin:16px 0 8px">
          Weekly Schedule
        </div>
        <a-collapse>
          <a-collapse-panel v-for="day in DAY_NAMES" :key="day" :header="`${DAY_LABELS[day]} (${form.weekly[day]?.length ?? 0})`">
            <div
              v-for="(entry, i) in form.weekly[day]" :key="i"
              style="display:flex;gap:8px;align-items:center;margin-bottom:6px"
            >
              <a-input v-model:value="entry.time" placeholder="HH:MM:SS" style="width:110px;font-family:monospace" />
              <a-switch v-if="form.value_type === 'boolean'" v-model:checked="entry.value as any" />
              <a-input-number v-else v-model:value="entry.value as any" style="flex:1" :step="form.value_type === 'unsigned' ? 1 : 0.1" />
              <a-button type="text" size="small" danger @click="removeWeeklyEntry(day, i)">
                <template #icon><MinusCircleOutlined /></template>
              </a-button>
            </div>
            <a-button size="small" block @click="addWeeklyEntry(day)">
              <template #icon><PlusOutlined /></template>
              Add Time
            </a-button>
          </a-collapse-panel>
        </a-collapse>

        <div style="font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px;margin:16px 0 8px">
          Exceptions
          <span style="font-weight:400;color:#bbb;text-transform:none">
            — override the weekly schedule on specific dates, or reuse a Calendar's date list (see the device's
            "Calendars" menu entry) across multiple schedules
          </span>
        </div>
        <div
          v-for="(exc, ei) in form.exceptions" :key="ei"
          style="background:#fafafa;border:1px solid #e8e8e8;border-radius:6px;padding:10px;margin-bottom:10px"
        >
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
            <a-select v-model:value="(exc.period as any).type" style="width:150px">
              <a-select-option value="date">Single date</a-select-option>
              <a-select-option value="date-range">Date range</a-select-option>
              <a-select-option value="calendar-reference">Reference Calendar</a-select-option>
            </a-select>
            <template v-if="exc.period.type === 'date'">
              <a-input v-model:value="(exc.period as any).date" placeholder="YYYY-MM-DD" style="width:130px;font-family:monospace" />
            </template>
            <template v-else-if="exc.period.type === 'date-range'">
              <a-input v-model:value="(exc.period as any).start" placeholder="Start YYYY-MM-DD" style="width:130px;font-family:monospace" />
              <a-input v-model:value="(exc.period as any).end" placeholder="End YYYY-MM-DD" style="width:130px;font-family:monospace" />
            </template>
            <template v-else>
              <a-select
                v-model:value="(exc.period as any).calendar_name"
                placeholder="Choose a calendar"
                style="width:180px"
                :not-found-content="allCalendars.length ? undefined : 'No calendars on this device yet'"
              >
                <a-select-option v-for="cal in allCalendars" :key="cal.name" :value="cal.name">{{ cal.name }}</a-select-option>
              </a-select>
            </template>
            <a-input-number v-model:value="exc.priority" :min="1" :max="16" style="width:90px" title="Priority (1 = highest)" />
            <div style="flex:1" />
            <a-button type="text" size="small" danger @click="removeException(ei)">
              <template #icon><MinusCircleOutlined /></template>
            </a-button>
          </div>
          <div
            v-for="(entry, i) in exc.entries" :key="i"
            style="display:flex;gap:8px;align-items:center;margin-bottom:6px"
          >
            <a-input v-model:value="entry.time" placeholder="HH:MM:SS" style="width:110px;font-family:monospace" />
            <a-switch v-if="form.value_type === 'boolean'" v-model:checked="entry.value as any" />
            <a-input-number v-else v-model:value="entry.value as any" style="flex:1" :step="form.value_type === 'unsigned' ? 1 : 0.1" />
            <a-button type="text" size="small" danger @click="removeExceptionEntry(exc, i)">
              <template #icon><MinusCircleOutlined /></template>
            </a-button>
          </div>
          <a-button size="small" block @click="addExceptionEntry(exc)">
            <template #icon><PlusOutlined /></template>
            Add Time
          </a-button>
        </div>
        <a-button size="small" block @click="addException">
          <template #icon><PlusOutlined /></template>
          Add Exception
        </a-button>

        <a-form-item label="Enabled" style="margin-top:16px;margin-bottom:0">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>
      </a-form>
    </template>

    <template #footer>
      <a-space v-if="formOpen">
        <a-button @click="formOpen = false">Cancel</a-button>
        <a-button type="primary" :loading="saving" @click="save">{{ editing ? 'Save' : 'Create' }}</a-button>
      </a-space>
      <a-button v-else @click="emit('update:open', false)">Close</a-button>
    </template>
  </a-drawer>
</template>
