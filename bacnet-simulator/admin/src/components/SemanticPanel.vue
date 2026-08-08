<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { PlusOutlined, DeleteOutlined, EditOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Device, SimObject, Location, Meta, SemanticEntity, SemanticRelationship } from '../types'

const loading = ref(false)
const activeTab = ref<'entities' | 'relationships'>('entities')

const meta = ref<Meta | null>(null)
const devices = ref<Device[]>([])
const locations = ref<Location[]>([])
const objects = ref<SimObject[]>([])
const entities = ref<SemanticEntity[]>([])
const relationships = ref<SemanticRelationship[]>([])

const deviceById = computed(() => new Map(devices.value.map(d => [d.id, d])))
const locationById = computed(() => new Map(locations.value.map(l => [l.id, l])))
const objectById = computed(() => new Map(objects.value.map(o => [o.id, o])))
const entityById = computed(() => new Map(entities.value.map(e => [e.id, e])))

async function load() {
  loading.value = true
  try {
    const [m, devs, locs, ents, rels] = await Promise.all([
      api.meta(),
      api.devices.list(),
      api.locations.list(),
      api.semanticEntities.list(),
      api.semanticRelationships.list(),
    ])
    meta.value = m
    devices.value = devs
    locations.value = locs
    entities.value = ents
    relationships.value = rels
    // Objects aren't exposed by a single global endpoint -- fetch per
    // device (modest dataset for a simulator, not a hot path).
    const perDevice = await Promise.all(devs.map(d => api.objects.list(d.id)))
    objects.value = perDevice.flat()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load semantic data')
  } finally {
    loading.value = false
  }
}

onMounted(load)

function deviceLabel(id?: number | null): string {
  if (id == null) return '—'
  const d = deviceById.value.get(id)
  return d ? d.name : `device #${id}`
}
function objectLabel(id?: number | null): string {
  if (id == null) return '—'
  const o = objectById.value.get(id)
  if (!o) return `object #${id}`
  return `${deviceLabel(o.device_id)} / ${o.name}`
}
function locationLabel(id?: number | null): string {
  if (id == null) return '—'
  const l = locationById.value.get(id)
  return l ? l.name : `location #${id}`
}
function entityLabel(id: number): string {
  const e = entityById.value.get(id)
  return e ? `${e.name} (${e.brick_class})` : `entity #${id}`
}

function linkedLabel(e: SemanticEntity): string {
  if (e.entity_kind === 'point') return objectLabel(e.object_id)
  if (e.entity_kind === 'location') {
    return e.location_id != null ? locationLabel(e.location_id) : `${deviceLabel(e.device_id)} (virtual)`
  }
  return deviceLabel(e.device_id)
}

function brickClassOptions(kind: string | null): { value: string; label: string }[] {
  if (!meta.value) return []
  if (kind === 'equipment') return meta.value.equipment_types
  if (kind === 'point') return meta.value.point_types
  if (kind === 'location') return meta.value.location_kinds
  return []
}

// ── Entity create/edit modal ────────────────────────────────────────────
const entityModalOpen = ref(false)
const editingEntity = ref<SemanticEntity | null>(null)
const entitySaving = ref(false)
const locationBackingMode = ref<'real' | 'virtual'>('real')

const entityForm = ref({
  name: '',
  entity_kind: null as 'equipment' | 'point' | 'location' | null,
  brick_class: null as string | null,
  local_slug: '',
  device_id: null as number | null,
  object_id: null as number | null,
  location_id: null as number | null,
})

const objectsForChosenDevice = computed(() =>
  objects.value.filter(o => o.device_id === entityForm.value.device_id),
)

function resetEntityForm() {
  entityForm.value = {
    name: '', entity_kind: null, brick_class: null, local_slug: '',
    device_id: null, object_id: null, location_id: null,
  }
  locationBackingMode.value = 'real'
}

function openCreateEntity() {
  editingEntity.value = null
  resetEntityForm()
  entityModalOpen.value = true
}

function openEditEntity(e: SemanticEntity) {
  editingEntity.value = e
  entityForm.value = {
    name: e.name,
    entity_kind: e.entity_kind,
    brick_class: e.brick_class,
    local_slug: e.local_slug ?? '',
    device_id: e.device_id ?? null,
    object_id: e.object_id ?? null,
    location_id: e.location_id ?? null,
  }
  locationBackingMode.value = e.location_id != null ? 'real' : 'virtual'
  entityModalOpen.value = true
}

function onEntityKindChange() {
  entityForm.value.brick_class = null
  entityForm.value.device_id = null
  entityForm.value.object_id = null
  entityForm.value.location_id = null
  locationBackingMode.value = 'real'
}

async function saveEntity() {
  const f = entityForm.value
  if (!f.name.trim()) { message.error('Name is required'); return }
  if (!f.entity_kind) { message.error('Choose an entity kind'); return }
  if (!f.brick_class) { message.error('Choose a Brick class'); return }
  if (f.entity_kind === 'equipment' && !f.device_id) { message.error('Choose a device'); return }
  if (f.entity_kind === 'point' && !f.object_id) { message.error('Choose an object'); return }
  if (f.entity_kind === 'location') {
    if (locationBackingMode.value === 'real' && !f.location_id) { message.error('Choose a location'); return }
    if (locationBackingMode.value === 'virtual' && !f.device_id) { message.error('Choose a hosting device'); return }
  }

  const body = {
    name: f.name.trim(),
    entity_kind: f.entity_kind,
    brick_class: f.brick_class,
    local_slug: f.local_slug.trim() || null,
    device_id: f.entity_kind === 'equipment' || (f.entity_kind === 'location' && locationBackingMode.value === 'virtual')
      ? f.device_id : null,
    object_id: f.entity_kind === 'point' ? f.object_id : null,
    location_id: f.entity_kind === 'location' && locationBackingMode.value === 'real' ? f.location_id : null,
  }

  entitySaving.value = true
  try {
    if (editingEntity.value) {
      await api.semanticEntities.update(editingEntity.value.id, body)
      message.success('Semantic entity updated')
    } else {
      await api.semanticEntities.create(body)
      message.success('Semantic entity created')
    }
    entityModalOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save semantic entity')
  } finally {
    entitySaving.value = false
  }
}

function confirmDeleteEntity(e: SemanticEntity) {
  Modal.confirm({
    title: `Delete semantic entity "${e.name}"?`,
    content: 'Any relationships referencing it will be deleted too.',
    okType: 'danger',
    onOk: async () => {
      try {
        await api.semanticEntities.del(e.id)
        message.success('Deleted')
        await load()
      } catch (err: unknown) {
        message.error((err as Error).message ?? 'Failed to delete')
      }
    },
  })
}

// ── Relationship create modal ───────────────────────────────────────────
const relationshipModalOpen = ref(false)
const relationshipSaving = ref(false)
const relationshipForm = ref({
  source_entity_id: null as number | null,
  predicate: null as string | null,
  target_entity_id: null as number | null,
})

function openCreateRelationship() {
  relationshipForm.value = { source_entity_id: null, predicate: null, target_entity_id: null }
  relationshipModalOpen.value = true
}

async function saveRelationship() {
  const f = relationshipForm.value
  if (!f.source_entity_id) { message.error('Choose a source entity'); return }
  if (!f.predicate) { message.error('Choose a predicate'); return }
  if (!f.target_entity_id) { message.error('Choose a target entity'); return }
  if (f.source_entity_id === f.target_entity_id) { message.error('Source and target must differ'); return }

  relationshipSaving.value = true
  try {
    await api.semanticRelationships.create({
      source_entity_id: f.source_entity_id,
      predicate: f.predicate as SemanticRelationship['predicate'],
      target_entity_id: f.target_entity_id,
    })
    message.success('Relationship created')
    relationshipModalOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to create relationship')
  } finally {
    relationshipSaving.value = false
  }
}

function confirmDeleteRelationship(r: SemanticRelationship) {
  Modal.confirm({
    title: 'Delete this relationship?',
    okType: 'danger',
    onOk: async () => {
      try {
        await api.semanticRelationships.del(r.id)
        message.success('Deleted')
        await load()
      } catch (err: unknown) {
        message.error((err as Error).message ?? 'Failed to delete')
      }
    },
  })
}

function filterByLabel(input: string, option: { label?: string }) {
  return (option.label ?? '').toLowerCase().includes(input.toLowerCase())
}

const entityColumns: TableColumnsType<SemanticEntity> = [
  { title: 'Name', dataIndex: 'name', key: 'name' },
  { title: 'Local Slug', dataIndex: 'local_slug', key: 'local_slug', width: 140 },
  { title: 'Brick Class', dataIndex: 'brick_class', key: 'brick_class', width: 180 },
  { title: 'Kind', dataIndex: 'entity_kind', key: 'entity_kind', width: 100 },
  { title: 'Linked To', key: 'linked', width: 220 },
  { title: '', key: 'actions', width: 90 },
]

const relationshipColumns: TableColumnsType<SemanticRelationship> = [
  { title: 'Source', key: 'source' },
  { title: 'Predicate', dataIndex: 'predicate', key: 'predicate', width: 140 },
  { title: 'Target', key: 'target' },
  { title: '', key: 'actions', width: 60 },
]
</script>

<template>
  <div style="padding:20px;overflow:auto;height:100%">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
      <h2 style="margin:0;font-size:16px">Semantic Model</h2>
      <span v-if="meta" style="font-size:12px;color:var(--text-muted)">Brick {{ meta.brick_version }}</span>
      <div style="flex:1" />
      <a-button size="small" :loading="loading" @click="load">
        <template #icon><ReloadOutlined /></template>
      </a-button>
    </div>
    <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px">
      Ordinary device/point/location classification is assigned in their own drawers (Brick Class field) — it's mirrored here automatically.
      Use this panel for sub-equipment (e.g. a Supply Fan under an AHU), virtual entities (e.g. a Lighting Zone), and relationships (isPointOf / isPartOf / feeds / hasLocation).
    </div>

    <a-tabs v-model:activeKey="activeTab">
      <a-tab-pane key="entities" :tab="`Entities (${entities.length})`">
        <div style="margin-bottom:12px">
          <a-button type="primary" size="small" @click="openCreateEntity">
            <template #icon><PlusOutlined /></template>
            New Entity
          </a-button>
        </div>
        <a-table
          :columns="entityColumns"
          :data-source="entities"
          :loading="loading"
          row-key="id"
          size="small"
          :pagination="{ pageSize: 25 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'linked'">
              {{ linkedLabel(record as SemanticEntity) }}
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-button size="small" type="text" @click="openEditEntity(record as SemanticEntity)">
                <template #icon><EditOutlined /></template>
              </a-button>
              <a-button size="small" type="text" danger @click="confirmDeleteEntity(record as SemanticEntity)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </template>
          </template>
          <template #emptyText>
            <div style="padding:24px;color:var(--text-placeholder)">No semantic entities yet</div>
          </template>
        </a-table>
      </a-tab-pane>

      <a-tab-pane key="relationships" :tab="`Relationships (${relationships.length})`">
        <div style="margin-bottom:12px">
          <a-button type="primary" size="small" :disabled="entities.length < 2" @click="openCreateRelationship">
            <template #icon><PlusOutlined /></template>
            New Relationship
          </a-button>
        </div>
        <a-table
          :columns="relationshipColumns"
          :data-source="relationships"
          :loading="loading"
          row-key="id"
          size="small"
          :pagination="{ pageSize: 25 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'source'">
              {{ entityLabel((record as SemanticRelationship).source_entity_id) }}
            </template>
            <template v-else-if="column.key === 'target'">
              {{ entityLabel((record as SemanticRelationship).target_entity_id) }}
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-button size="small" type="text" danger @click="confirmDeleteRelationship(record as SemanticRelationship)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </template>
          </template>
          <template #emptyText>
            <div style="padding:24px;color:var(--text-placeholder)">No relationships yet</div>
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>

    <!-- Entity create/edit modal -->
    <a-modal
      :open="entityModalOpen"
      :title="editingEntity ? 'Edit Semantic Entity' : 'New Semantic Entity'"
      :confirm-loading="entitySaving"
      ok-text="Save"
      @ok="saveEntity"
      @cancel="entityModalOpen = false"
    >
      <a-form layout="vertical" style="margin-top:8px">
        <a-form-item label="Name" required>
          <a-input v-model:value="entityForm.name" placeholder="e.g. AHU-1 Supply Fan" />
        </a-form-item>

        <a-form-item label="Entity Kind" required>
          <a-select
            v-model:value="entityForm.entity_kind"
            placeholder="Choose a kind"
            :options="[
              { value: 'equipment', label: 'Equipment' },
              { value: 'point', label: 'Point' },
              { value: 'location', label: 'Location' },
            ]"
            @change="onEntityKindChange"
          />
        </a-form-item>

        <a-form-item label="Brick Class" required>
          <a-select
            v-model:value="entityForm.brick_class"
            show-search
            placeholder="Choose a canonical Brick class"
            :options="brickClassOptions(entityForm.entity_kind)"
            :filter-option="filterByLabel"
            :disabled="!entityForm.entity_kind"
          />
        </a-form-item>

        <a-form-item label="Local Slug" help="Optional disambiguator when Brick Class + linked device alone aren't unique (e.g. two Lighting_Zone entities on one gateway) — e.g. zone-a, supply-fan.">
          <a-input v-model:value="entityForm.local_slug" placeholder="e.g. zone-a" />
        </a-form-item>

        <a-form-item v-if="entityForm.entity_kind === 'equipment'" label="Device" required>
          <a-select
            v-model:value="entityForm.device_id"
            show-search
            placeholder="Choose a device"
            :options="devices.map(d => ({ value: d.id, label: d.name }))"
            :filter-option="filterByLabel"
          />
        </a-form-item>

        <template v-else-if="entityForm.entity_kind === 'point'">
          <a-form-item label="Device" required>
            <a-select
              v-model:value="entityForm.device_id"
              show-search
              placeholder="Choose a device"
              :options="devices.map(d => ({ value: d.id, label: d.name }))"
              :filter-option="filterByLabel"
              @change="entityForm.object_id = null"
            />
          </a-form-item>
          <a-form-item label="Object" required>
            <a-select
              v-model:value="entityForm.object_id"
              show-search
              placeholder="Choose an object"
              :disabled="!entityForm.device_id"
              :options="objectsForChosenDevice.map(o => ({ value: o.id, label: o.name }))"
              :filter-option="filterByLabel"
            />
          </a-form-item>
        </template>

        <template v-else-if="entityForm.entity_kind === 'location'">
          <a-form-item label="Backing">
            <a-radio-group v-model:value="locationBackingMode">
              <a-radio value="real">Real location</a-radio>
              <a-radio value="virtual">Virtual (device-hosted)</a-radio>
            </a-radio-group>
          </a-form-item>
          <a-form-item v-if="locationBackingMode === 'real'" label="Location" required>
            <a-select
              v-model:value="entityForm.location_id"
              show-search
              placeholder="Choose a location"
              :options="locations.map(l => ({ value: l.id, label: l.name }))"
              :filter-option="filterByLabel"
            />
          </a-form-item>
          <a-form-item v-else label="Hosting Device" required help="The device whose points this virtual location's points resolve against.">
            <a-select
              v-model:value="entityForm.device_id"
              show-search
              placeholder="Choose a device"
              :options="devices.map(d => ({ value: d.id, label: d.name }))"
              :filter-option="filterByLabel"
            />
          </a-form-item>
        </template>
      </a-form>
    </a-modal>

    <!-- Relationship create modal -->
    <a-modal
      :open="relationshipModalOpen"
      title="New Semantic Relationship"
      :confirm-loading="relationshipSaving"
      ok-text="Create"
      @ok="saveRelationship"
      @cancel="relationshipModalOpen = false"
    >
      <a-form layout="vertical" style="margin-top:8px">
        <a-form-item label="Source Entity" required>
          <a-select
            v-model:value="relationshipForm.source_entity_id"
            show-search
            placeholder="Choose the source entity"
            :options="entities.map(e => ({ value: e.id, label: `${e.name} (${e.brick_class})` }))"
            :filter-option="filterByLabel"
          />
        </a-form-item>
        <a-form-item label="Predicate" required>
          <a-select
            v-model:value="relationshipForm.predicate"
            placeholder="Choose a predicate"
            :options="meta?.semantic_predicates ?? []"
          />
        </a-form-item>
        <a-form-item label="Target Entity" required>
          <a-select
            v-model:value="relationshipForm.target_entity_id"
            show-search
            placeholder="Choose the target entity"
            :options="entities.map(e => ({ value: e.id, label: `${e.name} (${e.brick_class})` }))"
            :filter-option="filterByLabel"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>
