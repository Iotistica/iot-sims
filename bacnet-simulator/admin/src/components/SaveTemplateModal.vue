<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import type { SimObject, Meta } from '../types'
import { api } from '../api'

const props = defineProps<{
  open: boolean
  objects: SimObject[]
  deviceId?: number
  deviceName?: string
  meta: Meta
}>()
const emit = defineEmits<{ 'update:open': [v: boolean]; saved: [] }>()

const name = ref('')
const desc = ref('')
const equipmentType = ref<string | null>(null)
const saving = ref(false)

// ── Known equipment type (this Controller's `controls` relationship) ────────
// Saving a template from an already-wired controller already knows what kind
// of equipment it controls -- asking the user to restate it would be asking
// them to repeat a fact the app already has. Only populated when there is
// exactly one controlled equipment type; a controller with none yet, or with
// several different types, still falls back to asking.
const knownEquipmentType = ref<string | null>(null)
const loadingKnownType = ref(false)

// Plain-language label for the known type (same vocabulary as the Equipment
// Type select below) -- never show the raw Brick class name in this form.
const knownEquipmentTypeLabel = computed(() =>
  props.meta.equipment_types.find(o => o.value === knownEquipmentType.value)?.label ?? knownEquipmentType.value
)

async function loadKnownEquipmentType() {
  knownEquipmentType.value = null
  if (!props.deviceId) return

  loadingKnownType.value = true
  try {
    const controllers = await api.semanticEntities.list({ device_id: props.deviceId, entity_kind: 'controller' })
    const entity = controllers[0]
    if (!entity) return

    const targets = await api.semanticEntities.related(entity.id, 'controls', 'out')
    const types = new Set(targets.map(t => t.brick_class).filter((c): c is string => !!c))
    if (types.size === 1) {
      knownEquipmentType.value = [...types][0]
    }
  } catch {
    // Fall back to asking -- same as if nothing were known yet.
  } finally {
    loadingKnownType.value = false
  }
}

watch(() => props.open, (v) => {
  if (!v) return
  name.value = props.deviceName ? `${props.deviceName} Template` : ''
  desc.value = ''
  equipmentType.value = null
  loadKnownEquipmentType()
})

async function save() {
  if (!name.value.trim()) { message.error('Name is required'); return }

  saving.value = true
  try {
    await api.templates.create({
      label: name.value.trim(),
      description: desc.value.trim(),
      objects: props.objects.map(o => ({
        object_type:     o.object_type,
        object_instance: o.object_instance,
        name:            o.name,
        units:           o.units,
        behavior:        o.behavior,
        behavior_params: o.behavior_params,
      })),
      equipment_types: knownEquipmentType.value ? [knownEquipmentType.value] : (equipmentType.value ? [equipmentType.value] : null),
    })

    message.success(`Template "${name.value.trim()}" saved`)
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save template')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title="Save as Template"
    ok-text="Save Template"
    :confirm-loading="saving"
    @ok="save"
    @cancel="emit('update:open', false)"
  >
    <a-form layout="vertical" style="margin-top:8px">
      <a-form-item label="Template Name" required>
        <a-input
          v-model:value="name"
          placeholder="e.g. My AHU Controller"
          @press-enter="save"
        />
      </a-form-item>
      <a-form-item label="Description" style="margin-bottom:8px">
        <a-input
          v-model:value="desc"
          placeholder="Optional description"
        />
      </a-form-item>
      <template v-if="!loadingKnownType">
        <div v-if="knownEquipmentType" style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
          Tagged for <strong>{{ knownEquipmentTypeLabel }}</strong> equipment -- already known from this controller.
        </div>
        <a-form-item v-else label="Equipment Type" help="Optional -- lets this template show up when adding a controller for equipment of this type." style="margin-bottom:8px">
          <a-select
            v-model:value="equipmentType"
            allow-clear
            show-search
            placeholder="Not tagged"
            :options="meta.equipment_types"
          />
        </a-form-item>
      </template>
      <div style="font-size:12px;color:var(--text-muted)">
        {{ objects.length }} object{{ objects.length !== 1 ? 's' : '' }} will be saved
      </div>
    </a-form>
  </a-modal>
</template>
