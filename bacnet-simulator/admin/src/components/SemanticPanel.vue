<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { message } from 'ant-design-vue'
import { ReloadOutlined, AimOutlined } from '@ant-design/icons-vue'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import { api } from '../api'
import type { Device, SimObject, Location, Meta, SemanticEntity, SemanticRelationship } from '../types'

const loading = ref(false)

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

function linkedLabel(e: SemanticEntity): string {
  if (e.entity_kind === 'point') return objectLabel(e.object_id)
  if (e.entity_kind === 'location') {
    return e.location_id != null ? locationLabel(e.location_id) : `${deviceLabel(e.device_id)} (virtual)`
  }
  return deviceLabel(e.device_id)
}

function filterByLabel(input: string, option: { label?: string }) {
  return (option.label ?? '').toLowerCase().includes(input.toLowerCase())
}

// ── Graph (Cytoscape.js) ──────────────────────────────────────────────────
// This is now the entirety of the Graph tab -- read-only visualization of
// the Brick semantic model. Manual entity/relationship editing (previously
// its own "Entities"/"Relationships" sub-tabs here) was removed; Brick
// classification is assigned from each object/location/equipment/controller's
// own drawer (mirrored into the graph automatically), and relationships are
// created through their dedicated flows (the Controller drawer's Controls
// field, the Equipment panel's Manage Points/Assign Controller actions).
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
  if (!graphContainer.value) return

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
  [graphFocusEntityId, graphDepth, graphShowPoints, entities, relationships],
  async () => {
    await nextTick()
    renderGraph()
  },
  { deep: true },
)

onBeforeUnmount(() => {
  cy?.destroy()
  cy = null
})
</script>

<template>
  <div style="padding:20px;overflow:auto;height:100%">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
      <h2 style="margin:0;font-size:16px">Graph</h2>
      <span v-if="meta" style="font-size:12px;color:var(--text-muted)">Brick Schema {{ meta.brick_version }}</span>
      <div style="flex:1" />
      <a-button size="small" :loading="loading" @click="load">
        <template #icon><ReloadOutlined /></template>
      </a-button>
    </div>
    <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px">
      Read-only view of the Brick semantic model. Brick classification is set from each Equipment/Controller/Object/Location's own drawer (mirrored here automatically); relationships are created through their dedicated actions (the Controller drawer's Controls field, the Equipment panel's Manage Points/Assign Controller) — Controller → Point hosting shown here is always derived live from BACnet device ownership, never manually assigned.
    </div>

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
      read naturally. Dashed orange edges/nodes are derived from BACnet ownership
      (<code>objects.device_id</code>), not stored relationships.
    </div>
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
