<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, EditOutlined, DeleteOutlined, MinusCircleOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, Calendar, CalendarDateEntry } from '../types'

const props = defineProps<{ open: boolean; device: Device | null }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const MONTHS = [
  { label: 'Any', value: null }, { label: 'Jan', value: 1 }, { label: 'Feb', value: 2 }, { label: 'Mar', value: 3 },
  { label: 'Apr', value: 4 }, { label: 'May', value: 5 }, { label: 'Jun', value: 6 }, { label: 'Jul', value: 7 },
  { label: 'Aug', value: 8 }, { label: 'Sep', value: 9 }, { label: 'Oct', value: 10 }, { label: 'Nov', value: 11 },
  { label: 'Dec', value: 12 },
]
const WEEKS_OF_MONTH = [
  { label: 'Any week', value: null }, { label: 'Days 1-7', value: 1 }, { label: 'Days 8-14', value: 2 },
  { label: 'Days 15-21', value: 3 }, { label: 'Days 22-28', value: 4 }, { label: 'Days 29-31', value: 5 },
  { label: 'Last 7 days of month', value: 6 },
]
const DAYS_OF_WEEK = [
  { label: 'Any day', value: null }, { label: 'Monday', value: 1 }, { label: 'Tuesday', value: 2 },
  { label: 'Wednesday', value: 3 }, { label: 'Thursday', value: 4 }, { label: 'Friday', value: 5 },
  { label: 'Saturday', value: 6 }, { label: 'Sunday', value: 7 },
]

const list = ref<Calendar[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref<Calendar | null>(null)
const formOpen = ref(false)

const form = reactive({
  name: '',
  description: '',
  enabled: true,
  date_list: [] as CalendarDateEntry[],
})

async function load() {
  if (!props.device) return
  loading.value = true
  try {
    list.value = await api.calendars.list(props.device.id)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load calendars')
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  if (v) load()
  else { formOpen.value = false; editing.value = null }
})

function resetForm() {
  Object.assign(form, { name: '', description: '', enabled: true, date_list: [] })
}

function openAdd() {
  editing.value = null
  resetForm()
  formOpen.value = true
}

function openEdit(cal: Calendar) {
  editing.value = cal
  Object.assign(form, {
    name: cal.name,
    description: cal.description,
    enabled: cal.enabled,
    date_list: cal.date_list.map(e => ({ ...e })),
  })
  formOpen.value = true
}

function addEntry() {
  form.date_list.push({ type: 'date', date: new Date().toISOString().slice(0, 10) })
}
function removeEntry(i: number) {
  form.date_list.splice(i, 1)
}

async function save() {
  if (!props.device) return
  if (!form.name.trim()) { message.error('Name is required'); return }

  saving.value = true
  try {
    const body = {
      name: form.name.trim(),
      description: form.description,
      date_list: form.date_list,
      enabled: form.enabled ? 1 : 0,
    }
    if (editing.value) {
      await api.calendars.update(editing.value.id, body as any)
      message.success('Calendar updated')
    } else {
      await api.calendars.create(props.device.id, body as any)
      message.success('Calendar created')
    }
    formOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Save failed')
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(cal: Calendar, checked: boolean) {
  try {
    await api.calendars.update(cal.id, {
      name: cal.name, description: cal.description, date_list: cal.date_list, enabled: checked ? 1 : 0,
    } as any)
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to update')
  }
}

function confirmDelete(cal: Calendar) {
  Modal.confirm({
    title: `Delete "${cal.name}"?`,
    content: 'Any Schedule exception referencing this calendar by name will fall through to its default value.',
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.calendars.del(cal.id)
        message.success('Deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Delete failed')
      }
    },
  })
}

function entrySummary(e: CalendarDateEntry): string {
  if (e.type === 'date') return e.date
  if (e.type === 'date-range') return `${e.start} → ${e.end}`
  const m = MONTHS.find(x => x.value === e.month)?.label ?? 'Any'
  const w = WEEKS_OF_MONTH.find(x => x.value === e.week_of_month)?.label ?? 'Any week'
  const d = DAYS_OF_WEEK.find(x => x.value === e.day_of_week)?.label ?? 'Any day'
  return `${m} · ${w} · ${d}`
}
</script>

<template>
  <a-drawer
    :title="device ? `Calendars — ${device.name}` : 'Calendars'"
    :open="open"
    width="560"
    @close="emit('update:open', false)"
  >
    <template v-if="!formOpen">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
        A device-scoped list of dates that a Schedule's exceptions can reference by name (calendarReference),
        instead of repeating the same dates inline in every schedule that needs them.
      </div>
      <a-button type="primary" block @click="openAdd" style="margin-bottom:16px">
        <template #icon><PlusOutlined /></template>
        Add Calendar
      </a-button>

      <a-spin :spinning="loading">
        <div v-if="!list.length && !loading" style="text-align:center;color:var(--text-placeholder);padding:40px 0;font-size:13px">
          No calendars yet
        </div>
        <div
          v-for="cal in list" :key="cal.id"
          style="border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:10px"
        >
          <div style="display:flex;align-items:flex-start;gap:8px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px;color:var(--text-primary)">{{ cal.name }}</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                {{ cal.date_list.length }} date{{ cal.date_list.length !== 1 ? 's' : '' }}
                <span v-if="cal.date_list.length"> — {{ cal.date_list.slice(0, 3).map(entrySummary).join(', ') }}{{ cal.date_list.length > 3 ? ', …' : '' }}</span>
              </div>
            </div>
            <a-space :size="4">
              <a-switch size="small" :checked="cal.enabled" @change="(c: boolean) => toggleEnabled(cal, c)" />
              <a-button size="small" title="Edit" @click="openEdit(cal)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" danger title="Delete" @click="confirmDelete(cal)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-space>
          </div>
        </div>
      </a-spin>
    </template>

    <template v-else>
      <a-form layout="vertical" :colon="false">
        <a-form-item label="Name" required tooltip="Schedules reference this calendar by name, so it must be unique on this device">
          <a-input v-model:value="form.name" placeholder="e.g. Holidays" />
        </a-form-item>
        <a-form-item label="Description">
          <a-input v-model:value="form.description" placeholder="Optional description" />
        </a-form-item>

        <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:16px 0 8px">
          Date List
        </div>
        <div
          v-for="(entry, i) in form.date_list" :key="i"
          style="background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:8px"
        >
          <div style="display:flex;gap:8px;align-items:center">
            <a-select v-model:value="(entry as any).type" style="width:140px">
              <a-select-option value="date">Single date</a-select-option>
              <a-select-option value="date-range">Date range</a-select-option>
              <a-select-option value="weekday">Weekday pattern</a-select-option>
            </a-select>
            <template v-if="entry.type === 'date'">
              <a-input v-model:value="(entry as any).date" placeholder="YYYY-MM-DD" style="width:130px;font-family:monospace" />
            </template>
            <template v-else-if="entry.type === 'date-range'">
              <a-input v-model:value="(entry as any).start" placeholder="Start YYYY-MM-DD" style="width:130px;font-family:monospace" />
              <a-input v-model:value="(entry as any).end" placeholder="End YYYY-MM-DD" style="width:130px;font-family:monospace" />
            </template>
            <template v-else>
              <a-select v-model:value="(entry as any).month" :options="MONTHS" style="width:100px" />
              <a-select v-model:value="(entry as any).week_of_month" :options="WEEKS_OF_MONTH" style="width:140px" />
              <a-select v-model:value="(entry as any).day_of_week" :options="DAYS_OF_WEEK" style="width:120px" />
            </template>
            <div style="flex:1" />
            <a-button type="text" size="small" danger @click="removeEntry(i)">
              <template #icon><MinusCircleOutlined /></template>
            </a-button>
          </div>
        </div>
        <a-button size="small" block @click="addEntry">
          <template #icon><PlusOutlined /></template>
          Add Date Entry
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
