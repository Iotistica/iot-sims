<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { PlusOutlined, DeleteOutlined, EditOutlined, ReloadOutlined, AimOutlined } from '@ant-design/icons-vue'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import { api } from '../api'
import type { Device, SimObject, Location, Meta, SemanticEntity, SemanticRelationship } from '../types'
import GridFilterToolbar from './GridFilterToolbar.vue'

const loading = ref(false)
const activeTab = ref<'entities' | 'relationships' | 'graph'>('entities')

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

// Derived (never persisted) Controller -> Point hosting, one entry per
// BACnet object owned by an explicitly-marked Controller device -- see
// objects.device_id. This is a runtime fact, not user semantics, so it is
// computed here at render/traversal time rather than requiring a manually
// created isHostedBy/hosts semantic_relationships row (see brick_export.py's
// _derive_controller_hosting_triples() for the same join reused on export).
// targetId is the real point entity id when one exists, or a synthetic
// negative id (-object.id) for a BACnet object with no semantic Point
// entity yet -- negative so it can share the same `Set<number>`/graph-node
// id space as real entities without colliding (entity ids are always >= 1).
interface DerivedHostingEdge {
  controllerEntityId: number
  targetId: number
  targetName: string
  targetBrickClass: string | null
  isUnclassified: boolean
}
const derivedHostingEdges = computed<DerivedHostingEdge[]>(() => {
  const pointEntityByObjectId = new Map<number, SemanticEntity>()
  for (const e of entities.value) {
    if (e.entity_kind === 'point' && e.object_id != null) pointEntityByObjectId.set(e.object_id, e)
  }
  const edges: DerivedHostingEdge[] = []
  for (const controller of entities.value) {
    if (controller.entity_kind !== 'controller' || controller.device_id == null) continue
    for (const obj of objects.value) {
      if (obj.device_id !== controller.device_id) continue
      const pointEntity = pointEntityByObjectId.get(obj.id)
      if (pointEntity) {
        edges.push({
          controllerEntityId: controller.id,
          targetId: pointEntity.id,
          targetName: pointEntity.name,
          targetBrickClass: pointEntity.brick_class,
          isUnclassified: false,
        })
      } else {
        edges.push({
          controllerEntityId: controller.id,
          targetId: -obj.id,
          targetName: obj.name,
          targetBrickClass: null,
          isUnclassified: true,
        })
      }
    }
  }
  return edges
})

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
    // This modal only ever offers equipment/point/location as choices
    // (see the entity_kind <a-select> options below) -- 'controller'
    // entities are managed exclusively through the dedicated Controller
    // drawer flow (see DeviceDrawer.vue / POST /devices/{id}/controller),
    // never through this generic panel, so the cast here doesn't grant a
    // capability that didn't already exist.
    entity_kind: e.entity_kind as 'equipment' | 'point' | 'location',
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

// ── Entities table filters ──────────────────────────────────────────────
const entitySearch = ref('')
const entityKindFilter = ref<SemanticEntity['entity_kind'] | undefined>(undefined)

const filteredEntities = computed(() => {
  const q = entitySearch.value.trim().toLowerCase()
  return entities.value.filter(e => {
    if (entityKindFilter.value && e.entity_kind !== entityKindFilter.value) return false
    if (!q) return true
    return (
      e.name.toLowerCase().includes(q) ||
      e.brick_class.toLowerCase().includes(q) ||
      (e.local_slug ?? '').toLowerCase().includes(q)
    )
  })
})

const entityFiltersActive = computed(() => !!entitySearch.value.trim() || entityKindFilter.value !== undefined)

function clearEntityFilters() {
  entitySearch.value = ''
  entityKindFilter.value = undefined
}

// ── Relationships table filters ─────────────────────────────────────────
const relationshipSearch = ref('')
const relationshipPredicateFilter = ref<SemanticRelationship['predicate'] | undefined>(undefined)

const filteredRelationships = computed(() => {
  const q = relationshipSearch.value.trim().toLowerCase()
  return relationships.value.filter(r => {
    if (relationshipPredicateFilter.value && r.predicate !== relationshipPredicateFilter.value) return false
    if (!q) return true
    return (
      entityLabel(r.source_entity_id).toLowerCase().includes(q) ||
      entityLabel(r.target_entity_id).toLowerCase().includes(q)
    )
  })
})

const relationshipFiltersActive = computed(() => !!relationshipSearch.value.trim() || relationshipPredicateFilter.value !== undefined)

function clearRelationshipFilters() {
  relationshipSearch.value = ''
  relationshipPredicateFilter.value = undefined
}


// ── Graph tab (Cytoscape.js) ──────────────────────────────────────────────
const graphContainer = ref<HTMLDivElement | null>(null)
const graphFocusEntityId = ref<number | null>(null)
const graphDepth = ref<1 | 2 | 3>(2)
const graphShowPoints = ref(true)
const selectedGraphEntity = ref<SemanticEntity | null>(null)
let cy: Core | null = null

const graphFocusOptions = computed(() =>
  entities.value
    .slice()
    .sort((a, b) => {
      const rank = (kind: SemanticEntity['entity_kind']) =>
        kind === 'equipment' ? 0 : kind === 'location' ? 1 : 2
      return rank(a.entity_kind) - rank(b.entity_kind) || a.name.localeCompare(b.name)
    })
    .map(e => ({
      value: e.id,
      label: `${e.name} (${e.brick_class})`,
    })),
)

function chooseDefaultGraphFocus() {
  if (graphFocusEntityId.value != null && entityById.value.has(graphFocusEntityId.value)) return

  const related = new Set<number>()
  for (const r of relationships.value) {
    related.add(r.source_entity_id)
    related.add(r.target_entity_id)
  }

  const equipment = entities.value.find(e => e.entity_kind === 'equipment' && related.has(e.id))
  const anyRelated = entities.value.find(e => related.has(e.id))
  graphFocusEntityId.value = equipment?.id ?? anyRelated?.id ?? entities.value[0]?.id ?? null
}

function graphEntityIds(): Set<number> {
  const focusId = graphFocusEntityId.value
  if (focusId == null) return new Set()

  const included = new Set<number>([focusId])
  let frontier = new Set<number>([focusId])

  for (let depth = 0; depth < graphDepth.value; depth += 1) {
    const next = new Set<number>()

    for (const r of relationships.value) {
      if (frontier.has(r.source_entity_id) && !included.has(r.target_entity_id)) {
        next.add(r.target_entity_id)
      }
      if (frontier.has(r.target_entity_id) && !included.has(r.source_entity_id)) {
        next.add(r.source_entity_id)
      }
    }

    // Derived Controller -> Point hosting participates in traversal exactly
    // like a persisted relationship (e.g. focusing on Equipment can reach
    // its Controller via `controls`, then the Controller's hosted Points
    // via this derived edge, within the same depth budget).
    for (const e of derivedHostingEdges.value) {
      if (frontier.has(e.controllerEntityId) && !included.has(e.targetId)) {
        next.add(e.targetId)
      }
      if (frontier.has(e.targetId) && !included.has(e.controllerEntityId)) {
        next.add(e.controllerEntityId)
      }
    }

    for (const id of next) included.add(id)
    frontier = next
    if (frontier.size === 0) break
  }

  // Keep the focus even when it is a point, but optionally hide other point
  // nodes -- including synthetic unclassified-point ids (always negative).
  if (!graphShowPoints.value) {
    for (const id of [...included]) {
      if (id === focusId) continue
      if (id < 0) { included.delete(id); continue }
      if (entityById.value.get(id)?.entity_kind === 'point') included.delete(id)
    }
  }

  return included
}

function displayEdge(r: SemanticRelationship) {
  // Store canonical Brick direction in the DB, but draw common inverse labels
  // so an equipment-centred graph reads naturally from parent -> child.
  if (r.predicate === 'isPartOf') {
    return {
      source: r.target_entity_id,
      target: r.source_entity_id,
      label: 'has part',
      predicate: r.predicate,
    }
  }
  if (r.predicate === 'isPointOf') {
    return {
      source: r.target_entity_id,
      target: r.source_entity_id,
      label: 'has point',
      predicate: r.predicate,
    }
  }
  if (r.predicate === 'hasLocation') {
    return {
      source: r.source_entity_id,
      target: r.target_entity_id,
      label: 'located in',
      predicate: r.predicate,
    }
  }
  return {
    source: r.source_entity_id,
    target: r.target_entity_id,
    label: r.predicate,
    predicate: r.predicate,
  }
}

function cssColor(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function renderGraph() {
  if (activeTab.value !== 'graph' || !graphContainer.value) return

  chooseDefaultGraphFocus()

  const ids = graphEntityIds()
  const visibleEntities = entities.value.filter(e => ids.has(e.id))
  const visibleRelationships = relationships.value.filter(
    r => ids.has(r.source_entity_id) && ids.has(r.target_entity_id),
  )
  // Derived Controller-hosted Point edges/nodes -- never persisted, computed
  // fresh from objects.device_id every render (see derivedHostingEdges).
  const visibleHostingEdges = derivedHostingEdges.value.filter(
    e => ids.has(e.controllerEntityId) && ids.has(e.targetId),
  )
  const visibleUnclassifiedPoints = visibleHostingEdges.filter(e => e.isUnclassified)

  const elements: ElementDefinition[] = [
    ...visibleEntities.map(e => ({
      group: 'nodes' as const,
      data: {
        id: `entity-${e.id}`,
        entityId: e.id,
        // Point nodes show just the point name -- the Brick class (often a
        // long Condenser_Water_Temperature_Sensor-style name) is already
        // shown in the inspector panel on click, and doesn't fit
        // meaningfully in the small ellipse without crowding out the name.
        label: e.entity_kind === 'point' ? e.name : `${e.name}\n${e.brick_class}`,
        kind: e.entity_kind,
      },
      classes: [
        `kind-${e.entity_kind}`,
        e.id === graphFocusEntityId.value ? 'graph-focus' : '',
      ].filter(Boolean).join(' '),
    })),
    ...visibleUnclassifiedPoints.map(e => ({
      group: 'nodes' as const,
      data: {
        id: `entity-${e.targetId}`,
        entityId: e.targetId,
        // Name only, same as classified point nodes above -- the dashed
        // border is what signals "unclassified", not a second text line.
        label: e.targetName,
        kind: 'point-unclassified',
      },
      classes: 'kind-point-unclassified',
    })),
    ...visibleRelationships.map(r => {
      const edge = displayEdge(r)
      return {
        group: 'edges' as const,
        data: {
          id: `relationship-${r.id}`,
          source: `entity-${edge.source}`,
          target: `entity-${edge.target}`,
          label: edge.label,
          predicate: edge.predicate,
        },
      }
    }),
    ...visibleHostingEdges.map(e => ({
      group: 'edges' as const,
      data: {
        id: `hosts-${e.controllerEntityId}-${e.targetId}`,
        source: `entity-${e.controllerEntityId}`,
        target: `entity-${e.targetId}`,
        label: 'hosts',
        predicate: 'hosts',
      },
    })),
  ]

  cy?.destroy()

  cy = cytoscape({
    container: graphContainer.value,
    elements,
    wheelSensitivity: 0.18,
    minZoom: 0.25,
    maxZoom: 2.5,
    style: [
      {
        selector: 'node',
        style: {
          'label': 'data(label)',
          'text-wrap': 'wrap',
          'text-max-width': '150px',
          'text-valign': 'center',
          'text-halign': 'center',
          'font-size': 11,
          'color': cssColor('--text-color', '#d9d9d9'),
          'background-color': cssColor('--component-background', '#262626'),
          'border-color': cssColor('--border-color-base', '#595959'),
          'border-width': 1.5,
          'width': 150,
          'height': 58,
          'padding': '8px',
        },
      },
     {
  selector: '.kind-equipment',
  style: {
    'shape': 'round-rectangle',
    'background-color': '#162d4d',
    'border-color': '#4096ff',
    'border-width': 2,
  },
},
{
  selector: '.kind-point',
  style: {
    'shape': 'ellipse',
    'background-color': '#173b2c',
    'border-color': '#49aa19',
    'width': 118,
    'height': 48,
    'font-size': 10,
  },
},
{
  selector: '.kind-location',
  style: {
    'shape': 'round-rectangle',
    'background-color': '#30204d',
    'border-color': '#9254de',
    'border-style': 'dashed',
    'border-width': 2,
  },
},
{
  selector: '.kind-controller',
  style: {
    'shape': 'round-rectangle',
    'background-color': '#4d2b0a',
    'border-color': '#d46b08',
    'border-width': 2,
  },
},
{
  // Derived, unclassified BACnet object (no semantic Point entity yet) --
  // dashed/muted variant of the ordinary point node so it visually reads
  // as "known to exist, not yet semantically described".
  selector: '.kind-point-unclassified',
  style: {
    'shape': 'ellipse',
    'background-color': '#262626',
    'border-color': '#595959',
    'border-style': 'dashed',
    'width': 118,
    'height': 48,
    'font-size': 10,
    'color': cssColor('--text-muted', '#8c8c8c'),
  },
},
      {
        selector: '.graph-focus',
        style: {
          'border-color': cssColor('--primary-color', '#1677ff'),
          'border-width': 4,
        },
      },
      {
        selector: 'edge',
        style: {
          'curve-style': 'bezier',
          'width': 1.6,
          'line-color': cssColor('--border-color-base', '#595959'),
          'target-arrow-color': cssColor('--border-color-base', '#595959'),
          'target-arrow-shape': 'triangle',
          'arrow-scale': 0.8,
          'label': 'data(label)',
          'font-size': 9,
          'color': cssColor('--text-muted', '#8c8c8c'),
          'text-background-color': cssColor('--component-background', '#141414'),
          'text-background-opacity': 0.9,
          'text-background-padding': '3px',
          'text-rotation': 'autorotate',
        },
      },
      {
        // Derived (not persisted) edges -- dashed to visually distinguish
        // "known from objects.device_id" from user-created relationships.
        selector: 'edge[predicate = "hosts"]',
        style: {
          'line-style': 'dashed',
          'line-color': '#d46b08',
          'target-arrow-color': '#d46b08',
        },
      },
      {
        selector: 'node:selected',
        style: {
          'border-color': cssColor('--primary-color', '#1677ff'),
          'border-width': 4,
        },
      },
    ],
    layout: {
      name: 'breadthfirst',
      directed: false,
      direction: 'downward',
      roots: graphFocusEntityId.value != null
      ? [`entity-${graphFocusEntityId.value}`]
      : undefined,
      fit: true,
      padding: 48,
      spacingFactor: 1.35,
      avoidOverlap: true,
      nodeDimensionsIncludeLabels: true,
      animate: false,
    },
  })

  cy.on('tap', 'node', evt => {
    const entityId = Number(evt.target.data('entityId'))
    selectedGraphEntity.value = entityById.value.get(entityId) ?? null
  })

  cy.on('dbltap', 'node', evt => {
    graphFocusEntityId.value = Number(evt.target.data('entityId'))
  })
}

function fitGraph() {
  cy?.fit(undefined, 48)
}

function relayoutGraph() {
  if (!cy) return
  cy.layout({
    name: 'breadthfirst',
    directed: false,
    direction: 'downward',
    roots: graphFocusEntityId.value != null
    ? [`entity-${graphFocusEntityId.value}`]
    : undefined,
    fit: true,
    padding: 48,
    spacingFactor: 1.35,
    avoidOverlap: true,
    nodeDimensionsIncludeLabels: true,
    animate: true,
    animationDuration: 250,
  }).run()
}

watch(
  [activeTab, graphFocusEntityId, graphDepth, graphShowPoints, entities, relationships],
  async () => {
    if (activeTab.value !== 'graph') return
    await nextTick()
    renderGraph()
  },
  { deep: true },
)

onBeforeUnmount(() => {
  cy?.destroy()
  cy = null
})

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
        <GridFilterToolbar
          v-model:search="entitySearch"
          search-placeholder="Search name, class, slug…"
          :can-clear="entityFiltersActive"
          @clear="clearEntityFilters"
        >
          <a-select
            v-model:value="entityKindFilter"
            allow-clear
            size="small"
            placeholder="Kind"
            style="width:130px"
            :options="[
              { value: 'equipment', label: 'Equipment' },
              { value: 'point', label: 'Point' },
              { value: 'location', label: 'Location' },
            ]"
          />

          <template #actions>
            <a-button type="primary" @click="openCreateEntity">
              <template #icon><PlusOutlined /></template>
              New Entity
            </a-button>
          </template>
        </GridFilterToolbar>
        <a-table
          :columns="entityColumns"
          :data-source="filteredEntities"
          :loading="loading"
          :show-sorter-tooltip="false"
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
            <div v-if="entities.length" style="padding:24px;color:var(--text-placeholder)">
              No entities match your filters —
              <a @click="clearEntityFilters">clear filters</a>
            </div>
            <div v-else style="padding:24px;color:var(--text-placeholder)">No semantic entities yet</div>
          </template>
        </a-table>
      </a-tab-pane>

      <a-tab-pane key="relationships" :tab="`Relationships (${relationships.length})`">
        <GridFilterToolbar
          v-model:search="relationshipSearch"
          search-placeholder="Search source or target…"
          :can-clear="relationshipFiltersActive"
          @clear="clearRelationshipFilters"
        >
          <a-select
            v-model:value="relationshipPredicateFilter"
            allow-clear
            size="small"
            placeholder="Predicate"
            style="width:140px"
            :options="meta?.semantic_predicates ?? []"
          />

          <template #actions>
            <a-button type="primary" :disabled="entities.length < 2" @click="openCreateRelationship">
              <template #icon><PlusOutlined /></template>
              New Relationship
            </a-button>
          </template>
        </GridFilterToolbar>
        <a-table
          :columns="relationshipColumns"
          :data-source="filteredRelationships"
          :loading="loading"
          :show-sorter-tooltip="false"
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
            <div v-if="relationships.length" style="padding:24px;color:var(--text-placeholder)">
              No relationships match your filters —
              <a @click="clearRelationshipFilters">clear filters</a>
            </div>
            <div v-else style="padding:24px;color:var(--text-placeholder)">No relationships yet</div>
          </template>
        </a-table>
      </a-tab-pane>

      <a-tab-pane key="graph" tab="Graph">
        <div class="semantic-graph-toolbar">
          <div class="semantic-graph-control semantic-graph-focus">
            <span class="semantic-graph-label">Focus</span>
            <a-select
              v-model:value="graphFocusEntityId"
              show-search
              placeholder="Choose an entity"
              :options="graphFocusOptions"
              :filter-option="filterByLabel"
            />
          </div>

          <div class="semantic-graph-control">
            <span class="semantic-graph-label">Depth</span>
            <a-select
              v-model:value="graphDepth"
              style="width:90px"
              :options="[
                { value: 1, label: '1 hop' },
                { value: 2, label: '2 hops' },
                { value: 3, label: '3 hops' },
              ]"
            />
          </div>

          <label class="semantic-graph-switch">
            <span class="semantic-graph-label">Points</span>
            <a-switch v-model:checked="graphShowPoints" size="small" />
          </label>

          <div style="flex:1" />

          <a-button size="small" @click="relayoutGraph">
            <template #icon><ReloadOutlined /></template>
            Layout
          </a-button>
          <a-button size="small" @click="fitGraph">
            <template #icon><AimOutlined /></template>
            Fit
          </a-button>
        </div>

        <div class="semantic-graph-shell">
          <div
            ref="graphContainer"
            class="semantic-graph-canvas"
            aria-label="Brick semantic relationship graph"
          />

          <div v-if="selectedGraphEntity" class="semantic-graph-inspector">
            <div class="semantic-graph-inspector-title">{{ selectedGraphEntity.name }}</div>
            <div class="semantic-graph-inspector-row">
              <span>Kind</span>
              <strong>{{ selectedGraphEntity.entity_kind }}</strong>
            </div>
            <div class="semantic-graph-inspector-row">
              <span>Brick Class</span>
              <strong>{{ selectedGraphEntity.brick_class }}</strong>
            </div>
            <div class="semantic-graph-inspector-row">
              <span>Linked To</span>
              <strong>{{ linkedLabel(selectedGraphEntity) }}</strong>
            </div>
            <div v-if="selectedGraphEntity.local_slug" class="semantic-graph-inspector-row">
              <span>Local Slug</span>
              <strong>{{ selectedGraphEntity.local_slug }}</strong>
            </div>
            <div style="margin-top:12px">
              <a-button size="small" @click="graphFocusEntityId = selectedGraphEntity.id">
                Focus here
              </a-button>
            </div>
          </div>
        </div>

        <div class="semantic-graph-help">
          Double-click a node to focus it. The graph shows the selected entity and relationships
          up to the chosen depth. Canonical <code>isPartOf</code> / <code>isPointOf</code>
          relationships are displayed as “has part” / “has point” so equipment-centred graphs
          read naturally.
        </div>
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

<style scoped>
.semantic-graph-toolbar {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.semantic-graph-control {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.semantic-graph-focus {
  min-width: 320px;
  flex: 1;
  max-width: 520px;
}

.semantic-graph-label {
  font-size: 12px;
  color: var(--text-muted);
}

.semantic-graph-switch {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding-bottom: 4px;
}

.semantic-graph-shell {
  position: relative;
  height: 620px;
  min-height: 420px;
  border: 1px solid var(--border-color-base, #434343);
  border-radius: 8px;
  overflow: hidden;
  background: var(--component-background, #141414);
}

.semantic-graph-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.semantic-graph-inspector {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 260px;
  padding: 12px;
  border: 1px solid var(--border-color-base, #434343);
  border-radius: 8px;
  background: color-mix(in srgb, var(--component-background, #141414) 94%, transparent);
  backdrop-filter: blur(6px);
}

.semantic-graph-inspector-title {
  font-weight: 600;
  margin-bottom: 10px;
}

.semantic-graph-inspector-row {
  display: grid;
  grid-template-columns: 82px 1fr;
  gap: 8px;
  margin-top: 6px;
  font-size: 12px;
}

.semantic-graph-inspector-row > span {
  color: var(--text-muted);
}

.semantic-graph-inspector-row > strong {
  font-weight: 500;
  overflow-wrap: anywhere;
}

.semantic-graph-help {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-muted);
}

@media (max-width: 760px) {
  .semantic-graph-shell {
    height: 520px;
  }

  .semantic-graph-focus {
    min-width: 100%;
  }

  .semantic-graph-inspector {
    position: absolute;
    left: 10px;
    right: 10px;
    top: auto;
    bottom: 10px;
    width: auto;
  }

  
}


</style>
