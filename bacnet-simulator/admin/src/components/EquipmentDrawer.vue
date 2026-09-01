<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { api } from '../api'
import { buildLocationTreeOptions } from '../locationTree'
import type { Equipment, Location, Meta } from '../types'

const props = defineProps<{
  open: boolean
  equipment: Equipment | null
  /** Every equipment row in the project, used to build the "Feeds / Serves"
   * picker's Equipment group -- named distinctly from the singular
   * `equipment` prop above (the one item being edited). */
  equipmentList: Equipment[]
  locations: Location[]
  meta: Meta
  /** Preselects Location when opening for a fresh Add (e.g. invoked from a
   * location row's contextual "+" action) -- ignored when editing existing
   * equipment. */
  defaultLocationId?: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  /** equipmentId is the created/updated equipment's id -- omitted after a
   * delete (nothing left to focus). Lets App.vue switch tree focus to the
   * equipment that was actually just created/edited, instead of leaving
   * selection wherever it was -- same convention as DeviceDrawer's `saved`. */
  saved: [equipmentId?: number]
}>()

const loading = ref(false)
const deleting = ref(false)
const form = reactive({
  name: '',
  description: '',
  location_id: null as number | null,
  manufacturer: '',
  model: '',
  equipment_type: null as string | null,
})

// ── Feeds / Serves ──────────────────────────────────────────────────────────
// A `feeds` relationship is only persistable between two entities that
// already have a semantic_entities row (i.e. are classified -- see
// EquipmentPanel.vue's identical "no Semantic Type yet" gating). Rather than
// letting a user pick an unclassified target and having it silently fail to
// save, the picker only ever offers already-classified Equipment/Locations.
const feedsSelection = ref<string[]>([])
const initialFeedsSelection = ref<string[]>([])
const equipmentEntityId = ref<number | null>(null)
const relationshipIdByFeedKey = ref<Record<string, number>>({})
const classifiedEquipmentIds = ref<Set<number>>(new Set())
const classifiedLocationIds = ref<Set<number>>(new Set())

function filterOption(input: string, opt: { value?: string | number; label?: string }) {
  return (opt.label ?? '').toLowerCase().includes(input.toLowerCase())
}

const feedsOptionGroups = computed(() => {
  const otherEquipment = props.equipmentList
    .filter(e => e.id !== props.equipment?.id && classifiedEquipmentIds.value.has(e.id))
  const classifiedLocations = props.locations.filter(l => classifiedLocationIds.value.has(l.id))
  const groups: { label: string; options: { value: string; label: string }[] }[] = []
  if (otherEquipment.length) groups.push({ label: 'Equipment', options: otherEquipment.map(e => ({ value: `equipment:${e.id}`, label: e.name })) })
  if (classifiedLocations.length) groups.push({ label: 'Locations', options: classifiedLocations.map(l => ({ value: `location:${l.id}`, label: l.name })) })
  return groups
})

async function loadFeeds() {
  feedsSelection.value = []
  initialFeedsSelection.value = []
  equipmentEntityId.value = null
  relationshipIdByFeedKey.value = {}

  const [equipmentEntities, locationEntities] = await Promise.all([
    api.semanticEntities.list({ entity_kind: 'equipment' }),
    api.semanticEntities.list({ entity_kind: 'location' }),
  ])
  classifiedEquipmentIds.value = new Set(equipmentEntities.map(e => e.equipment_id).filter((id): id is number => id != null))
  classifiedLocationIds.value = new Set(locationEntities.map(e => e.location_id).filter((id): id is number => id != null))

  if (!props.equipment) return // Add mode: nothing persisted yet to load

  const entity = equipmentEntities.find(e => e.equipment_id === props.equipment!.id)
  if (!entity) return // this equipment isn't classified yet -- no entity to hang feeds off
  equipmentEntityId.value = entity.id

  const [targets, relationships] = await Promise.all([
    api.semanticEntities.related(entity.id, 'feeds', 'out'),
    api.semanticRelationships.list({ source_entity_id: entity.id, predicate: 'feeds' }),
  ])
  const relIdByTargetEntityId: Record<number, number> = {}
  for (const rel of relationships) relIdByTargetEntityId[rel.target_entity_id] = rel.id

  const keys: string[] = []
  const relByKey: Record<string, number> = {}
  for (const t of targets) {
    const key = t.entity_kind === 'equipment' && t.equipment_id != null ? `equipment:${t.equipment_id}`
      : t.entity_kind === 'location' && t.location_id != null ? `location:${t.location_id}` : null
    if (!key) continue
    keys.push(key)
    const relId = relIdByTargetEntityId[t.id]
    if (relId != null) relByKey[key] = relId
  }
  feedsSelection.value = keys
  initialFeedsSelection.value = [...keys]
  relationshipIdByFeedKey.value = relByKey
}

// Reconciles this Equipment's `feeds` edges against feedsSelection -- only
// ever touches `feeds` relationships sourced from THIS equipment's entity;
// every other predicate/entity is untouched. Every option offered by
// feedsOptionGroups is already classified, so every selected target is
// guaranteed to resolve to a real semantic entity here.
async function syncFeedsRelationships(equipmentId: number) {
  const finalKeys = feedsSelection.value
  const initialKeys = initialFeedsSelection.value
  if (finalKeys.length === 0 && initialKeys.length === 0) return

  let entityId = equipmentEntityId.value
  if (entityId == null) {
    const entities = await api.semanticEntities.list({ entity_kind: 'equipment', equipment_id: equipmentId })
    entityId = entities[0]?.id ?? null
  }
  if (entityId == null) return // still not classified -- nothing to persist

  const toAdd = finalKeys.filter(k => !initialKeys.includes(k))
  const toRemove = initialKeys.filter(k => !finalKeys.includes(k))

  for (const key of toAdd) {
    const [kind, idStr] = key.split(':')
    const id = Number(idStr)
    const filter = kind === 'equipment' ? { entity_kind: 'equipment' as const, equipment_id: id } : { entity_kind: 'location' as const, location_id: id }
    const target = (await api.semanticEntities.list(filter))[0]
    if (!target) continue // shouldn't happen -- picker only offers classified targets
    await api.semanticRelationships.create({ source_entity_id: entityId, predicate: 'feeds', target_entity_id: target.id })
  }
  for (const key of toRemove) {
    const relId = relationshipIdByFeedKey.value[key]
    if (relId != null) await api.semanticRelationships.del(relId)
  }
}

watch(() => props.open, (v) => {
  if (!v) return
  loadFeeds()
  if (props.equipment) {
    Object.assign(form, {
      name: props.equipment.name,
      description: props.equipment.description ?? '',
      location_id: props.equipment.location_id ?? null,
      manufacturer: props.equipment.manufacturer ?? '',
      model: props.equipment.model ?? '',
      equipment_type: props.equipment.equipment_type ?? null,
    })
  } else {
    Object.assign(form, { name: '', description: '', location_id: props.defaultLocationId ?? null, manufacturer: '', model: '', equipment_type: null })
  }
})

const treeOptions = computed(() => buildLocationTreeOptions(props.locations, null))

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  loading.value = true
  const body = { name: form.name, description: form.description, location_id: form.location_id, manufacturer: form.manufacturer, model: form.model, equipment_type: form.equipment_type }
  try {
    let equipmentId: number
    if (props.equipment) {
      await api.equipment.update(props.equipment.id, body)
      equipmentId = props.equipment.id
      message.success('Equipment updated')
    } else {
      const created = await api.equipment.create(body)
      equipmentId = created.id
      message.success('Equipment created')
    }
    await syncFeedsRelationships(equipmentId)
    emit('update:open', false)
    emit('saved', equipmentId)
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function doDelete() {
  if (!props.equipment) return
  const eq = props.equipment
  Modal.confirm({
    title: `Delete "${eq.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      deleting.value = true
      try {
        await api.equipment.del(eq.id)
        message.success('Equipment deleted')
        emit('update:open', false)
        emit('saved')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete equipment')
      } finally {
        deleting.value = false
      }
    },
  })
}
</script>

<template>
  <a-drawer
    :title="equipment ? 'Edit Equipment' : 'Add Equipment'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Boiler 1" />
      </a-form-item>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Location">
        <a-tree-select
          v-model:value="form.location_id"
          :tree-data="treeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Unassigned"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item label="Manufacturer">
        <a-input v-model:value="form.manufacturer" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Model">
        <a-input v-model:value="form.model" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Type" help="Describes what this equipment represents in the building.">
        <a-select
          v-model:value="form.equipment_type"
          show-search
          allow-clear
          placeholder="Not classified"
          :options="meta.equipment_types"
        />
      </a-form-item>

      <a-form-item label="Serves" help="Downstream equipment or locations this feeds or serves. Only classified equipment and locations can be selected.">
        <a-select
          v-model:value="feedsSelection"
          mode="multiple"
          show-search
          allow-clear
          :disabled="!form.equipment_type"
          :placeholder="form.equipment_type ? 'No downstream equipment or locations selected' : 'Set a Type to enable Serves'"
          :options="feedsOptionGroups"
          :filter-option="filterOption"
        />
      </a-form-item>
    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a-button v-if="equipment" danger :loading="deleting" @click="doDelete">Delete</a-button>
        <div v-else />
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ equipment ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
