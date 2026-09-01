<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { SimObject, Template } from '../types'
import { DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import { BUILTIN_ICONS, DEFAULT_TEMPLATE_ICON } from '../templates'

const props = defineProps<{
  open: boolean
  deviceId: number | undefined
  vendorName?: string
  modelName?: string
}>()
const emit  = defineEmits<{ 'update:open': [v: boolean]; applied: [] }>()

// ── Templates (src/api/routers/templates.py -- built-in + user, one list) ────

const templates = ref<Template[]>([])
const loadingTemplates = ref(false)

const userTemplates = computed(() => templates.value.filter(t => !t.is_builtin))
const builtinTemplates = computed(() => templates.value.filter(t => t.is_builtin))

function iconFor(tpl: Template) {
  return BUILTIN_ICONS[tpl.key] ?? DEFAULT_TEMPLATE_ICON
}

async function loadTemplates() {
  loadingTemplates.value = true
  try {
    templates.value = await api.templates.list()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load templates')
  } finally {
    loadingTemplates.value = false
  }
}

async function deleteTemplate(tpl: Template, e: MouseEvent) {
  e.stopPropagation()
  try {
    await api.templates.del(tpl.id)
    templates.value = templates.value.filter(t => t.id !== tpl.id)
    if (selected.value === tpl.key) selected.value = null
  } catch (err: unknown) {
    message.error((err as Error).message ?? 'Failed to delete template')
  }
}

// ─────────────────────────────────────────────────────────────────────────────

const selected = ref<string | null>(null)
const applying = ref(false)
const progress = ref(0)

// ── Smart suggestion based on vendor + model name ─────────────────────────────

const suggestedKey = computed<string | null>(() => {
  const text = `${props.vendorName ?? ''} ${props.modelName ?? ''}`.toLowerCase()
  if (!text.trim()) return null

  if (/\bvav\b|variable.air.vol/.test(text))                           return 'vav'
  if (/fan.coil|\bfcu\b/.test(text))                                   return 'fcu'
  if (/\bahu\b|air.handl/.test(text))                                  return 'ahu'
  if (/chiller|cooling.plant/.test(text))                              return 'chiller'
  if (/boiler|hot.water|heating.plant/.test(text))                     return 'boiler'
  if (/\bmeter\b|wattnode|power.analyz|powerscout|acurev|acuvim/.test(text)) return 'meter'
  if (/light|dimm|wavelinx|\bdali\b/.test(text))                       return 'lighting'
  if (/supervisor|workstation|\bbms\b|scada|webctrl|orcaview|pcvue|savic|enteli.?web/.test(text)) return 'bms'
  // vendor-specific hints
  if (/dent.instr|badger.meter|accuenergy|carlo.gav|watt.?node/.test(text)) return 'meter'
  if (/cooper.light|current.light|blue.ridge|bacmove|dali/.test(text)) return 'lighting'
  if (/belimo|danfoss|armstrong|condair/.test(text))                   return 'ahu'
  if (/delta.controls/.test(text) && /dvc|vav/.test(text))            return 'vav'
  // profile-type keywords that appear in many model names
  if (/\bb-bc\b|\bb-aac\b/.test(text))                                return 'bms'
  if (/\bb-ss\b/.test(text))                                           return 'meter'

  return null
})

// Auto-select suggestion when modal opens; reload templates
watch(() => props.open, async (isOpen) => {
  if (isOpen) {
    await loadTemplates()
    selected.value = suggestedKey.value
  }
})

function selectTemplate(key: string) {
  selected.value = selected.value === key ? null : key
}

function objKey(objectType: string, objectInstance: number): string {
  return `${objectType}:${objectInstance}`
}

async function apply() {
  if (!selected.value || !props.deviceId) return
  const tpl = templates.value.find(t => t.key === selected.value)
  if (!tpl) return

  let existing: SimObject[] = []
  try {
    existing = await api.objects.list(props.deviceId)
  } catch {
    // If this fails, fall through with an empty list — apply() will just
    // create everything, same as before this conflict check existed.
  }
  const existingByKey = new Map(existing.map(o => [objKey(o.object_type, o.object_instance), o]))
  const conflicts = tpl.objects.filter(o => existingByKey.has(objKey(o.object_type, o.object_instance)))

  if (conflicts.length) {
    Modal.confirm({
      title: `Overwrite ${conflicts.length} existing object${conflicts.length !== 1 ? 's' : ''}?`,
      content: `Applying this template will overwrite ${conflicts.length !== 1 ? 'them' : 'it'} with the template's settings.`,
      okText: 'Overwrite',
      okType: 'danger',
      onOk: () => applyTemplate(tpl, existingByKey),
    })
  } else {
    await applyTemplate(tpl, existingByKey)
  }
}

async function applyTemplate(tpl: Template, existingByKey: Map<string, SimObject>) {
  if (!props.deviceId) return
  applying.value = true
  progress.value = 0

  let ok = 0
  for (const obj of tpl.objects) {
    try {
      const match = existingByKey.get(objKey(obj.object_type, obj.object_instance))
      if (match) {
        await api.objects.update(props.deviceId, match.id, { ...obj, enabled: 1 })
      } else {
        await api.objects.create(props.deviceId, { ...obj, enabled: 1 })
      }
      ok++
    } catch {
      // skip genuine failures (e.g. validation errors)
    }
    progress.value = Math.round(((ok) / tpl.objects.length) * 100)
  }

  applying.value = false
  selected.value = null
  message.success(`Applied "${tpl.label}" — ${ok} object${ok !== 1 ? 's' : ''} created/updated`)
  emit('update:open', false)
  emit('applied')
}
</script>

<template>
  <a-modal
    :open="open"
    title="Load Object Template"
    :width="720"
    :footer="null"
    @cancel="emit('update:open', false)"
  >
    <a-spin :spinning="loadingTemplates">
      <div v-if="suggestedKey && (vendorName || modelName)" style="margin-bottom:10px;font-size:12px;color:#1890ff">
        Based on <strong>{{ vendorName }}{{ modelName ? ` — ${modelName}` : '' }}</strong>
      </div>

      <!-- User-saved templates -->
      <template v-if="userTemplates.length">
        <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px">My Templates</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px">
          <div
            v-for="tpl in userTemplates"
            :key="tpl.id"
            style="border:2px solid;border-radius:8px;padding:12px 14px;cursor:pointer;transition:all .15s;position:relative"
            :style="{
              borderColor: selected === tpl.key ? '#1890ff' : 'var(--border)',
              background: selected === tpl.key ? 'var(--selected-bg)' : 'var(--surface)',
            }"
            @click="selectTemplate(tpl.key)"
          >
            <a-button
              type="text"
              size="small"
              danger
              style="position:absolute;top:6px;right:6px;padding:0 4px;height:20px;font-size:12px"
              title="Delete template"
              @click="deleteTemplate(tpl, $event)"
            >
              <template #icon><DeleteOutlined style="font-size:11px" /></template>
            </a-button>
            <component
              :is="iconFor(tpl)"
              :style="{
                fontSize: '22px',
                color: selected === tpl.key ? '#1890ff' : 'var(--text-muted)',
                marginBottom: '6px',
                display: 'block',
              }"
            />
            <div style="font-weight:600;font-size:13px;margin-bottom:3px;padding-right:20px">{{ tpl.label }}</div>
            <div style="font-size:11px;color:var(--text-muted);line-height:1.4">{{ tpl.description || 'Custom template' }}</div>
            <div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">{{ tpl.objects.length }} objects · {{ tpl.created_at }}</div>
          </div>
        </div>
        <a-divider style="margin:0 0 14px" />
        <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px">Built-in Templates</div>
      </template>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px">
        <div
          v-for="tpl in builtinTemplates"
          :key="tpl.id"
          style="border:2px solid;border-radius:8px;padding:12px 14px;cursor:pointer;transition:all .15s;position:relative"
          :style="{
            borderColor: selected === tpl.key ? '#1890ff' : suggestedKey === tpl.key ? '#91caff' : 'var(--border)',
            background: selected === tpl.key ? 'var(--selected-bg)' : 'var(--surface)',
          }"
          @click="selectTemplate(tpl.key)"
        >
          <a-tag
            v-if="suggestedKey === tpl.key"
            color="blue"
            style="position:absolute;top:8px;right:8px;font-size:10px;line-height:16px;padding:0 5px"
          >Suggested</a-tag>
          <component
            :is="iconFor(tpl)"
            :style="{
              fontSize: '22px',
              color: selected === tpl.key ? '#1890ff' : suggestedKey === tpl.key ? '#4096ff' : 'var(--text-muted)',
              marginBottom: '6px',
              display: 'block',
            }"
          />
          <div style="font-weight:600;font-size:13px;margin-bottom:3px">{{ tpl.label }}</div>
          <div style="font-size:11px;color:var(--text-muted);line-height:1.4">{{ tpl.description }}</div>
          <div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">{{ tpl.objects.length }} objects</div>
        </div>
      </div>

      <a-progress v-if="applying" :percent="progress" style="margin-bottom:12px" />

      <div style="display:flex;justify-content:flex-end;gap:8px">
        <a-button @click="emit('update:open', false)">Cancel</a-button>
        <a-button
          type="primary"
          :disabled="!selected"
          :loading="applying"
          @click="apply"
        >
          Apply Template
        </a-button>
      </div>
    </a-spin>
  </a-modal>
</template>
