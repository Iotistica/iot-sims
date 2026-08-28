<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import { getEquipmentIcon } from '../equipmentIcons'
import type { Device, Location, Equipment, Meta, SemanticEntity, AssignablePoint } from '../types'

const props = defineProps<{
  equipment: Equipment
  devices: Device[]
  locations: Location[]
  meta: Meta
}>()
const emit = defineEmits<{
  edit: [Equipment]
  'select-controller': [number]
}>()

interface PointRow {
  id: number
  name: string
  brick_class: string
  object_type: string
  object_instance: number
  object_name: string
}

const loading = ref(false)
// The equipment's own entity_kind='equipment' semantic entity -- null when
// this Equipment row has no equipment_type set yet (sync_entity_from_flat_field
// only creates an entity once a Brick class is chosen), in which case there
// is nothing to hang Controlled By/Points/Sub-equipment relationships off.
const equipmentEntity = ref<SemanticEntity | null>(null)
const controllers = ref<SemanticEntity[]>([])
const points = ref<PointRow[]>([])
const subEquipment = ref<SemanticEntity[]>([])
const feeds = ref<SemanticEntity[]>([])
const fedBy = ref<SemanticEntity[]>([])

function filterOption(input: string, opt: { value?: string | number; label?: string }) {
  return (opt.label ?? '').toLowerCase().includes(input.toLowerCase())
}

const equipmentTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of props.meta.equipment_types) map[o.value] = o.label
  return map
})
const pointTypeLabel = computed(() => {
  const map: Record<string, string> = {}
  for (const o of props.meta.point_types) map[o.value] = o.label
  return map
})
const locationName = computed(() => {
  if (props.equipment.location_id == null) return null
  return props.locations.find(l => l.id === props.equipment.location_id)?.name ?? null
})

async function loadSummary() {
  loading.value = true
  try {
    const matches = await api.semanticEntities.list({ entity_kind: 'equipment', equipment_id: props.equipment.id })
    equipmentEntity.value = matches[0] ?? null

    if (!equipmentEntity.value) {
      controllers.value = []
      points.value = []
      subEquipment.value = []
      feeds.value = []
      fedBy.value = []
      return
    }

    const entityId = equipmentEntity.value.id
    const [controllersRes, pointsRes, subEquipmentRes, feedsRes, fedByRes] = await Promise.all([
      api.semanticEntities.related(entityId, 'controls', 'in'),
      api.semanticEntities.points(entityId),
      api.semanticEntities.related(entityId, 'isPartOf', 'in'),
      api.semanticEntities.related(entityId, 'feeds', 'out'),
      api.semanticEntities.related(entityId, 'feeds', 'in'),
    ])
    controllers.value = controllersRes
    points.value = pointsRes as unknown as PointRow[]
    subEquipment.value = subEquipmentRes
    feeds.value = feedsRes
    fedBy.value = fedByRes
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load equipment')
  } finally {
    loading.value = false
  }
}

watch(() => props.equipment.id, loadSummary, { immediate: true })

const pointColumns = [
  { title: 'Name', dataIndex: 'name', key: 'name' },
  { title: 'Semantic Type', dataIndex: 'brick_class', key: 'brick_class' },
]

// ── Assign Controller ───────────────────────────────────────────────────

const assignControllerOpen = ref(false)
const assignControllerLoading = ref(false)
const assignControllerDeviceId = ref<number | null>(null)
const controllerDevices = computed(() => props.devices.filter(d => d.has_controller_entity))

function openAssignController() {
  assignControllerDeviceId.value = null
  assignControllerOpen.value = true
}

async function doAssignController() {
  if (!assignControllerDeviceId.value || !equipmentEntity.value) return
  const equipmentEntityId = equipmentEntity.value.id
  assignControllerLoading.value = true
  try {
    const matches = await api.semanticEntities.list({ device_id: assignControllerDeviceId.value, entity_kind: 'controller' })
    const controllerEntity = matches[0]
    if (!controllerEntity) { message.error('That device has no Controller semantic entity'); return }
    await api.semanticRelationships.create({
      source_entity_id: controllerEntity.id,
      predicate: 'controls',
      target_entity_id: equipmentEntityId,
    })
    message.success('Controller related')
    assignControllerOpen.value = false
    await loadSummary()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to relate Controller')
  } finally {
    assignControllerLoading.value = false
  }
}

// ── Assign Points ────────────────────────────────────────────────────────

const assignPointsOpen = ref(false)
const assignPointsLoading = ref(false)
const candidates = ref<AssignablePoint[]>([])
const selectedObjectIds = ref<number[]>([])
const rowSemanticType = ref<Record<number, string | null>>({})
const rowReassignConfirmed = ref<Record<number, boolean>>({})

async function openAssignPoints() {
  if (!equipmentEntity.value) return
  assignPointsOpen.value = true
  selectedObjectIds.value = []
  rowSemanticType.value = {}
  rowReassignConfirmed.value = {}
  assignPointsLoading.value = true
  try {
    candidates.value = await api.equipment.assignablePoints(props.equipment.id)
    for (const c of candidates.value) {
      rowSemanticType.value[c.object_id] = c.point_type ?? null
    }
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load candidate points')
  } finally {
    assignPointsLoading.value = false
  }
}

function rowSelectable(row: AssignablePoint): boolean {
  if (!row.current_assignment) return true
  return !!rowReassignConfirmed.value[row.object_id]
}

function onSelectionChange(keys: (string | number)[]) {
  selectedObjectIds.value = keys as number[]
}
function getCheckboxProps(record: AssignablePoint) {
  return { disabled: !rowSelectable(record) }
}
function onReassignToggle(objectId: number, checked: boolean) {
  rowReassignConfirmed.value[objectId] = checked
}

async function doAssignPoints() {
  if (!equipmentEntity.value) return
  const equipmentEntityId = equipmentEntity.value.id
  const rows = candidates.value.filter(c => selectedObjectIds.value.includes(c.object_id))
  const missingType = rows.find(r => !r.point_entity_id && !rowSemanticType.value[r.object_id])
  if (missingType) { message.error(`Choose a Semantic Type for ${missingType.name} before assigning`); return }

  assignPointsLoading.value = true
  try {
    for (const row of rows) {
      let pointEntityId = row.point_entity_id
      if (!pointEntityId) {
        const created = await api.semanticEntities.create({
          name: row.name,
          brick_class: rowSemanticType.value[row.object_id]!,
          entity_kind: 'point',
          object_id: row.object_id,
        })
        pointEntityId = created.id
      }
      await api.semanticRelationships.create({
        source_entity_id: pointEntityId,
        predicate: 'isPointOf',
        target_entity_id: equipmentEntityId,
      })
    }
    message.success(`Assigned ${rows.length} point${rows.length !== 1 ? 's' : ''}`)
    assignPointsOpen.value = false
    await loadSummary()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to assign points')
  } finally {
    assignPointsLoading.value = false
  }
}
</script>

<template>
  <div>
    <div style="margin-bottom:16px;display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
      <div>
        <div style="font-size:18px;font-weight:600;display:flex;align-items:center;gap:8px">
          <component :is="getEquipmentIcon(equipment.equipment_type)" style="font-size:20px;color:var(--text-primary)" />
          {{ equipment.name }}
        </div>
        <div style="font-size:12px;color:var(--text-secondary);margin-top:3px">
          Equipment
          <template v-if="equipment.equipment_type"> · {{ equipmentTypeLabel[equipment.equipment_type] ?? equipment.equipment_type }}</template>
          <template v-else> · Not classified</template>
          <template v-if="locationName"> · Location: {{ locationName }}</template>
        </div>
      </div>
      <a-button @click="emit('edit', equipment)">Edit Equipment</a-button>
    </div>

    <div v-if="!loading && !equipmentEntity" style="padding:24px;text-align:center;color:var(--text-placeholder);background:var(--surface-alt);border:1px solid var(--border);border-radius:6px">
      This equipment has no Semantic Type set yet, so it isn't part of the semantic model.
      <br />Edit Equipment and choose a Semantic Type to enable Controlled By, Points, and assignment.
    </div>

    <template v-else>
      <!-- Controlled By -->
      <div style="margin-bottom:20px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <span style="font-size:13px;font-weight:600">Controlled By</span>
          <a-button size="small" :disabled="!equipmentEntity" @click="openAssignController">Assign Controller</a-button>
        </div>
        <div v-if="!controllers.length" style="color:var(--text-placeholder);font-size:13px">
          No Controller is currently related to this equipment.
        </div>
        <a-space v-else wrap>
          <a-tag v-for="c in controllers" :key="c.id" style="cursor:pointer" color="blue" @click="c.device_id && emit('select-controller', c.device_id)">
            {{ c.name }}
          </a-tag>
        </a-space>
      </div>

      <!-- Points -->
      <div style="margin-bottom:20px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <span style="font-size:13px;font-weight:600">Points</span>
          <a-tooltip :title="!controllers.length ? 'Relate a Controller to this equipment first' : undefined">
            <a-button size="small" :disabled="!controllers.length" @click="openAssignPoints">Assign Points</a-button>
          </a-tooltip>
        </div>
        <div v-if="!points.length" style="color:var(--text-placeholder);font-size:13px">
          No points are assigned to this equipment. Assign Points to build its semantic model.
        </div>
        <a-table
          v-else
          :data-source="points"
          :columns="pointColumns"
          :pagination="false"
          :show-sorter-tooltip="false"
          size="small"
          row-key="id"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'brick_class'">
              {{ pointTypeLabel[record.brick_class] ?? record.brick_class }}
            </template>
          </template>
        </a-table>
      </div>

      <!-- Sub-equipment -->
      <div style="margin-bottom:20px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">Sub-equipment</div>
        <div v-if="!subEquipment.length" style="color:var(--text-placeholder);font-size:13px">
          No sub-equipment.
        </div>
        <a-space v-else wrap>
          <a-tag v-for="s in subEquipment" :key="s.id">{{ s.name }}</a-tag>
        </a-space>
      </div>

      <!-- Feeds / Serves -->
      <div style="margin-bottom:20px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">Feeds / Serves</div>
        <div v-if="!feeds.length" style="color:var(--text-placeholder);font-size:13px">
          Feeds nothing.
        </div>
        <a-space v-else wrap>
          <a-tag v-for="f in feeds" :key="f.id" :color="f.entity_kind === 'location' ? 'purple' : undefined">{{ f.name }}</a-tag>
        </a-space>
      </div>

      <!-- Fed By -->
      <div>
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">Fed By</div>
        <div v-if="!fedBy.length" style="color:var(--text-placeholder);font-size:13px">
          Not fed by anything.
        </div>
        <a-space v-else wrap>
          <a-tag v-for="f in fedBy" :key="f.id" :color="f.entity_kind === 'location' ? 'purple' : undefined">{{ f.name }}</a-tag>
        </a-space>
      </div>
    </template>

    <!-- Assign Controller modal -->
    <a-modal
      v-model:open="assignControllerOpen"
      title="Assign Controller"
      ok-text="Assign"
      :confirm-loading="assignControllerLoading"
      :ok-button-props="{ disabled: !assignControllerDeviceId }"
      @ok="doAssignController"
    >
      <div style="padding:8px 0">
        <a-select
          v-model:value="assignControllerDeviceId"
          show-search
          placeholder="Choose a Controller"
          style="width:100%"
          :options="controllerDevices.map(d => ({ value: d.id, label: d.name }))"
          :filter-option="filterOption"
        />
        <div v-if="!controllerDevices.length" style="font-size:12px;color:var(--text-placeholder);margin-top:8px">
          No devices have been marked as a Controller yet.
        </div>
      </div>
    </a-modal>

    <!-- Assign Points modal -->
    <a-modal
      v-model:open="assignPointsOpen"
      title="Assign Points"
      width="720px"
      ok-text="Assign Selected"
      :confirm-loading="assignPointsLoading"
      :ok-button-props="{ disabled: !selectedObjectIds.length }"
      @ok="doAssignPoints"
    >
      <div v-if="!assignPointsLoading && !candidates.length" style="padding:24px 0;text-align:center;color:var(--text-placeholder)">
        No objects found on the Controller(s) related to this equipment.
      </div>
      <a-table
        v-else
        :data-source="candidates"
        :loading="assignPointsLoading"
        :pagination="false"
        :show-sorter-tooltip="false"
        size="small"
        row-key="object_id"
        :row-selection="{
          selectedRowKeys: selectedObjectIds,
          onChange: onSelectionChange,
          getCheckboxProps: getCheckboxProps,
        }"
      >
        <a-table-column title="Name" data-index="name" key="name" />
        <a-table-column title="Controller" data-index="device_name" key="device_name" />
        <a-table-column title="Semantic Type" key="semantic_type">
          <template #default="{ record }">
            <a-tag v-if="record.point_entity_id">{{ pointTypeLabel[rowSemanticType[record.object_id] ?? ''] ?? rowSemanticType[record.object_id] }}</a-tag>
            <a-select
              v-else
              v-model:value="rowSemanticType[record.object_id]"
              size="small"
              show-search
              placeholder="Choose type"
              style="width:160px"
              :options="meta.point_types"
            />
          </template>
        </a-table-column>
        <a-table-column title="Status" key="status">
          <template #default="{ record }">
            <template v-if="record.current_assignment">
              <div style="font-size:12px;color:var(--warning, #faad14)">
                Currently: {{ record.current_assignment.name }}
              </div>
              <a-checkbox
                :checked="!!rowReassignConfirmed[record.object_id]"
                @change="onReassignToggle(record.object_id, $event.target.checked)"
              >
                Reassign anyway
              </a-checkbox>
            </template>
          </template>
        </a-table-column>
      </a-table>
    </a-modal>
  </div>
</template>
