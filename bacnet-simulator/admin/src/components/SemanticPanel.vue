<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import { message } from 'ant-design-vue'
import { ReloadOutlined, AimOutlined } from '@ant-design/icons-vue'
import { VueFlow, useVueFlow, MarkerType } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import dagre from '@dagrejs/dagre'
import { api } from '../api'
import { formatPresentValue } from '../format'
import type { Device, SimObject, Location, Meta, SemanticEntity, SemanticRelationship } from '../types'
import SemanticGraphNode from './SemanticGraphNode.vue'
import SemanticFlowEdge from './SemanticFlowEdge.vue'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
// @vue-flow/background ships no separate stylesheet -- its dot/line
// pattern is plain inline SVG, styled via @vue-flow/core's own CSS vars.

// Same shape ObjectsPanel.vue already declares -- the app's one existing
// live-value stream (App.vue's wsConnect(), pushed on every simulation
// tick), reused here with no new backend endpoint.
const props = defineProps<{
  liveValues: Record<number, number | boolean>
  modelValues: Record<number, number | boolean>
  modelStates: Record<number, string>
}>()

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

// ── Graph (Vue Flow + Dagre) ────────────────────────────────────────────
// This is now the entirety of the Graph tab -- read-only visualization of
// the Brick semantic model. Manual entity/relationship editing (previously
// its own "Entities"/"Relationships" sub-tabs here) was removed; Brick
// classification is assigned from each object/location/equipment/controller's
// own drawer (mirrored into the graph automatically), and relationships are
// created through their dedicated flows (the Controller drawer's Controls
// field, the Equipment panel's Manage Points/Assign Controller actions).
//
// Vue Flow renders; Dagre only computes {x,y} positions from generic
// node/edge objects (id + width/height in, position out) -- it never sees
// predicates/entity_kind, all of that stays in buildFlowElements() below.
const graphFocusEntityId = ref<number | null>(null)
const graphDepth = ref<1 | 2 | 3>(2)
const graphShowPoints = ref(true)
const selectedGraphEntity = ref<SemanticEntity | null>(null)
// Typed `any[]` rather than Vue Flow's Node[]/Edge[] -- the same vue-tsc
// "excessively deep type instantiation" workaround FunctionalTestBuilder.vue
// already uses for Vue Flow's discriminated-union types.
const nodes = ref<any[]>([])
const edges = ref<any[]>([])
const { fitView } = useVueFlow()

// Per-kind box size -- both what Dagre lays out around AND what each Vue
// Flow node object's own width/height is set to, so the rendered
// SemanticGraphNode always exactly fills the box Dagre assumed. Equipment
// is the most prominent node, Point the most compact, per the design brief.
const NODE_DIMENSIONS: Record<string, { width: number; height: number }> = {
  // Equipment gets extra height in Phase 2 for its resolved live-value
  // summary block (SemanticGraphNode.vue's .sgn-summary, capped/scroll-
  // clipped there so an equipment with many resolved points never grows
  // the box further) -- otherwise unchanged from Phase 1.
  equipment: { width: 190, height: 104 },
  location: { width: 160, height: 60 },
  controller: { width: 160, height: 60 },
  point: { width: 118, height: 48 },
  'point-unclassified': { width: 118, height: 48 },
}

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

// ── Live value resolution (Phase 2) ────────────────────────────────────
// Deliberately small, not a second SemanticResolver: every function below
// is built from exactly two primitives -- direct isPointOf points, and one
// isPartOf hop into a named sub-equipment (e.g. Supply_Fan) then ITS direct
// isPointOf points. No per-equipment-type (AHU/RTU/VAV) special-casing, no
// point-name matching, no topology reconstruction, no traversal beyond
// that single isPartOf hop. If a real deployment needs more than this to
// resolve reliably, that's a gap to report, not a reason to build deeper
// inference here that duplicates src/semantics/resolver.py.

// Sensor/status classes are shown as measured state; command classes are
// included but always visibly labeled as commanded -- see
// resolveEquipmentSummary()'s isCommand handling.
const SUMMARY_SENSOR_STATUS_CLASSES = [
  'Fan_Status', 'Supply_Air_Flow_Sensor', 'Air_Flow_Sensor', 'Outside_Air_Flow_Sensor',
  'Supply_Air_Temperature_Sensor', 'Return_Air_Temperature_Sensor',
  'Mixed_Air_Temperature_Sensor', 'Outside_Air_Temperature_Sensor', 'Zone_Air_Temperature_Sensor',
  'Damper_Position_Sensor', 'Damper_Position_Status', 'Cooling_Status', 'Heating_Status',
] as const
const SUMMARY_COMMAND_CLASSES = [
  'Fan_Command', 'Fan_Speed_Command', 'Damper_Position_Command', 'Cooling_Command', 'Heating_Command',
] as const
const SUMMARY_BRICK_CLASSES = new Set<string>([...SUMMARY_SENSOR_STATUS_CLASSES, ...SUMMARY_COMMAND_CLASSES])
const COMMAND_CLASS_SET = new Set<string>(SUMMARY_COMMAND_CLASSES)

// Only these establish measured airflow -- see resolveFeedsFlowState()'s
// priority order. Damper_Position_Sensor is deliberately NOT in this list
// anywhere: it may be displayed on an Equipment summary, but must never by
// itself establish flow.
const AIRFLOW_SENSOR_CLASSES = ['Air_Flow_Sensor', 'Supply_Air_Flow_Sensor', 'Outside_Air_Flow_Sensor']
const AIRFLOW_ON_THRESHOLD = 5 // coarse "meaningfully more than zero" cut, not a calibrated value
const AIRFLOW_FAST_THRESHOLD = 500 // coarse tier boundary only, not a physical calibration

function pointLiveValue(point: SemanticEntity): number | boolean | undefined {
  return point.object_id != null ? props.liveValues[point.object_id] : undefined
}

// Direct isPointOf points on entityId whose brick_class is in the allowlist.
function findDirectPoints(entityId: number, brickClasses: Set<string> | readonly string[]): SemanticEntity[] {
  const wanted = brickClasses instanceof Set ? brickClasses : new Set(brickClasses)
  const points: SemanticEntity[] = []
  for (const r of relationships.value) {
    if (r.predicate !== 'isPointOf' || r.target_entity_id !== entityId) continue
    const point = entityById.value.get(r.source_entity_id)
    if (point && wanted.has(point.brick_class)) points.push(point)
  }
  return points
}

// One isPartOf hop into a named sub-equipment (e.g. Supply_Fan), then that
// sub-equipment's own direct isPointOf points. Never goes further than
// this single hop.
function findSubEquipmentPoints(
  entityId: number, subEquipmentBrickClass: string, brickClasses: Set<string> | readonly string[],
): SemanticEntity[] {
  const points: SemanticEntity[] = []
  for (const r of relationships.value) {
    if (r.predicate !== 'isPartOf' || r.target_entity_id !== entityId) continue
    const sub = entityById.value.get(r.source_entity_id)
    if (sub && sub.brick_class === subEquipmentBrickClass) {
      points.push(...findDirectPoints(sub.id, brickClasses))
    }
  }
  return points
}

interface SummaryLine { label: string; value: string; isCommand: boolean }

// Equipment node display block -- direct + one-Supply_Fan-hop points only,
// matched against the small allowlist above.
function resolveEquipmentSummary(entity: SemanticEntity): SummaryLine[] {
  if (entity.entity_kind !== 'equipment') return []
  const points = [
    ...findDirectPoints(entity.id, SUMMARY_BRICK_CLASSES),
    ...findSubEquipmentPoints(entity.id, 'Supply_Fan', SUMMARY_BRICK_CLASSES),
  ]

  const lines: SummaryLine[] = []
  const seen = new Set<string>()
  for (const point of points) {
    if (seen.has(point.brick_class)) continue // first match per class wins -- no duplicate lines
    const value = pointLiveValue(point)
    if (value === undefined) continue
    seen.add(point.brick_class)
    const obj = point.object_id != null ? objectById.value.get(point.object_id) : undefined
    const isCommand = COMMAND_CLASS_SET.has(point.brick_class)
    const unit = obj && obj.units !== 'no-units' ? ` ${obj.units}` : ''
    lines.push({
      label: point.brick_class.replace(/_/g, ' '),
      value: `${isCommand ? 'Cmd: ' : ''}${formatPresentValue(obj?.object_type ?? '', value)}${unit}`,
      isCommand,
    })
  }
  return lines
}

function findAirFlowSensorValue(entityId: number): number | undefined {
  for (const point of findDirectPoints(entityId, AIRFLOW_SENSOR_CLASSES)) {
    const value = pointLiveValue(point)
    if (typeof value === 'number') return value
  }
  return undefined
}

function findFanStatusOn(entityId: number): boolean | undefined {
  const points = [
    ...findDirectPoints(entityId, ['Fan_Status']),
    ...findSubEquipmentPoints(entityId, 'Supply_Fan', ['Fan_Status']),
  ]
  for (const point of points) {
    const value = pointLiveValue(point)
    if (typeof value === 'boolean') return value
    if (typeof value === 'number') return value > 0.5
  }
  return undefined
}

type FlowStatus = 'measured' | 'qualitative' | 'off' | 'unknown'
interface FlowState { status: FlowStatus; tier: 0 | 1 | 2 }

// Per-edge (branch-aware) flow resolution for a single `feeds` edge --
// deliberately NOT a copy of the source equipment's summary onto every
// outgoing edge (an RTU feeding three VAVs must not show RTU's one total
// supply airflow on all three branches). Priority order:
//   1. The EDGE'S TARGET's own Air_Flow_Sensor (branch/downstream-specific,
//      preferred whenever it exists).
//   2. The edge's SOURCE's own Air_Flow_Sensor (only when the target has
//      none of its own).
//   3. The source's Fan_Status -- qualitative evidence the system is
//      operating only, never a magnitude (no numeric tier scaling).
//   4. Unknown -- static edge, no claim either way.
// Damper_Position_Sensor is never consulted at any step.
function resolveFeedsFlowState(sourceEntityId: number, targetEntityId: number): FlowState {
  const targetFlow = findAirFlowSensorValue(targetEntityId)
  if (targetFlow !== undefined) {
    return targetFlow > AIRFLOW_ON_THRESHOLD
      ? { status: 'measured', tier: targetFlow > AIRFLOW_FAST_THRESHOLD ? 2 : 1 }
      : { status: 'off', tier: 0 }
  }

  const sourceFlow = findAirFlowSensorValue(sourceEntityId)
  if (sourceFlow !== undefined) {
    return sourceFlow > AIRFLOW_ON_THRESHOLD
      ? { status: 'measured', tier: sourceFlow > AIRFLOW_FAST_THRESHOLD ? 2 : 1 }
      : { status: 'off', tier: 0 }
  }

  const fanOn = findFanStatusOn(sourceEntityId)
  if (fanOn !== undefined) {
    return fanOn ? { status: 'qualitative', tier: 1 } : { status: 'off', tier: 0 }
  }

  return { status: 'unknown', tier: 0 }
}

// The narrow live-value reactive path -- mutates existing nodes.value[i]/
// edges.value[i] .data fields IN PLACE and never touches .position, so a
// live tick can never trigger applyDagreLayout()/renderGraph(). Completely
// separate from the structural watch below.
let previousLiveValues: Record<number, number | boolean> = {}
const justChangedTimers = new Map<number, ReturnType<typeof setTimeout>>()

function updateLiveDisplay() {
  const current = props.liveValues
  const changedObjectIds = new Set<number>()
  for (const key in current) {
    const objId = Number(key)
    if (previousLiveValues[objId] !== current[objId]) changedObjectIds.add(objId)
  }
  previousLiveValues = current

  for (const node of nodes.value) {
    if (node.data.kind === 'point' || node.data.kind === 'point-unclassified') {
      const objectId: number | null = node.data.objectId
      const obj = objectId != null ? objectById.value.get(objectId) : undefined
      const value = objectId != null ? current[objectId] : undefined
      node.data.valueLabel = obj && value !== undefined
        ? `${formatPresentValue(obj.object_type, value)}${obj.units !== 'no-units' ? ` ${obj.units}` : ''}`
        : null

      if (objectId != null && changedObjectIds.has(objectId)) {
        node.data.justChanged = true
        const existing = justChangedTimers.get(objectId)
        if (existing) clearTimeout(existing)
        justChangedTimers.set(objectId, setTimeout(() => { node.data.justChanged = false }, 900))
      }
    } else if (node.data.kind === 'equipment') {
      const entity = entityById.value.get(node.data.entityId)
      node.data.summaryLines = entity ? resolveEquipmentSummary(entity) : []
    }
  }

  for (const edge of edges.value) {
    if (edge.data?.predicate !== 'feeds') continue
    edge.data.flowState = resolveFeedsFlowState(edge.data.sourceEntityId, edge.data.targetEntityId)
  }
}

watch(() => props.liveValues, updateLiveDisplay)

// Builds plain Vue Flow node/edge objects from the current focus/depth/
// points-filtered entity set -- the direct replacement for the old
// Cytoscape `elements` array. Positions are a placeholder here; Dagre
// (applyDagreLayout) fills them in immediately after.
function buildFlowElements(): { nodes: any[]; edges: any[] } {
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

  const flowNodes: any[] = [
    ...visibleEntities.map(e => {
      const dims = NODE_DIMENSIONS[e.entity_kind] ?? NODE_DIMENSIONS.point
      return {
        id: `entity-${e.id}`,
        type: 'semanticNode',
        position: { x: 0, y: 0 },
        width: dims.width,
        height: dims.height,
        data: {
          entityId: e.id,
          kind: e.entity_kind,
          // Point nodes show just the point name -- the Brick class (often
          // a long Condenser_Water_Temperature_Sensor-style name) is
          // already shown in the inspector panel on click, and doesn't fit
          // meaningfully in the compact ellipse without crowding the name.
          label: e.name,
          brickClass: e.entity_kind === 'point' ? null : e.brick_class,
          isFocus: e.id === graphFocusEntityId.value,
          // Live-display fields (Phase 2) -- objectId lets a point node
          // look itself up in liveValues without re-deriving it later;
          // valueLabel/justChanged/summaryLines are populated by
          // updateLiveDisplay(), never by buildFlowElements() itself.
          objectId: e.entity_kind === 'point' ? e.object_id ?? null : null,
          valueLabel: null as string | null,
          justChanged: false,
          summaryLines: [] as { label: string; value: string; isCommand: boolean }[],
        },
      }
    }),
    ...visibleUnclassifiedPoints.map(e => {
      const dims = NODE_DIMENSIONS['point-unclassified']
      return {
        id: `entity-${e.targetId}`,
        type: 'semanticNode',
        position: { x: 0, y: 0 },
        width: dims.width,
        height: dims.height,
        data: {
          entityId: e.targetId,
          kind: 'point-unclassified',
          // Name only, same as classified point nodes above -- the dashed
          // border is what signals "unclassified", not a second text line.
          label: e.targetName,
          brickClass: null,
          isFocus: false,
          // targetId is -objectId for the synthetic unclassified-point id
          // space (see derivedHostingEdges' own comment above).
          objectId: -e.targetId,
          valueLabel: null as string | null,
          justChanged: false,
          summaryLines: [],
        },
      }
    }),
  ]

  const flowEdges: any[] = [
    ...visibleRelationships.map(r => {
      const edge = displayEdge(r)
      const isFeeds = edge.predicate === 'feeds'
      return {
        id: `relationship-${r.id}`,
        source: `entity-${edge.source}`,
        target: `entity-${edge.target}`,
        // Phase 2 assigns 'semanticFlowEdge' to feeds edges once
        // SemanticFlowEdge.vue exists; every other predicate keeps Vue
        // Flow's default (labeled, non-animated) edge rendering.
        type: isFeeds ? 'semanticFlowEdge' : undefined,
        label: edge.label,
        markerEnd: MarkerType.ArrowClosed,
        style: { stroke: 'var(--border, #595959)' },
        labelStyle: { fill: 'var(--text-muted, #8c8c8c)', fontSize: 9 },
        labelBgStyle: { fill: 'var(--surface, #141414)', fillOpacity: 0.9 },
        labelBgPadding: [3, 3] as [number, number],
        // sourceEntityId/targetEntityId are the raw semantic entity ids
        // (unlike .source/.target above, which are Vue Flow node id
        // strings) -- flow-state resolution reads these directly rather
        // than re-parsing the `entity-<id>` string later.
        data: { predicate: edge.predicate, sourceEntityId: edge.source, targetEntityId: edge.target, flowState: null as unknown },
      }
    }),
    // Derived (not persisted) edges -- dashed to visually distinguish
    // "known from objects.device_id" from user-created relationships.
    ...visibleHostingEdges.map(e => ({
      id: `hosts-${e.controllerEntityId}-${e.targetId}`,
      source: `entity-${e.controllerEntityId}`,
      target: `entity-${e.targetId}`,
      label: 'hosts',
      markerEnd: MarkerType.ArrowClosed,
      style: { stroke: '#d46b08', strokeDasharray: '5 3' },
      labelStyle: { fill: '#d46b08', fontSize: 9 },
      labelBgStyle: { fill: 'var(--surface, #141414)', fillOpacity: 0.9 },
      labelBgPadding: [3, 3] as [number, number],
      data: { predicate: 'hosts' },
    })),
  ]

  return { nodes: flowNodes, edges: flowEdges }
}

// Pure function: generic node/edge objects (id + width/height) in, {x,y}
// positions out. No semantic/predicate logic lives here at all -- Dagre
// only ever sees ids and dimensions, never entity_kind/brick_class/
// predicate, satisfying "Dagre is not responsible for semantic traversal
// or relationship logic."
function applyDagreLayout(flowNodes: any[], flowEdges: any[]): any[] {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 32, ranksep: 90 })
  g.setDefaultEdgeLabel(() => ({}))

  for (const n of flowNodes) {
    g.setNode(n.id, { width: n.width, height: n.height })
  }
  for (const e of flowEdges) {
    g.setEdge(e.source, e.target)
  }

  dagre.layout(g)

  // Dagre positions are node centers; Vue Flow positions are top-left.
  return flowNodes.map(n => {
    const pos = g.node(n.id)
    return { ...n, position: { x: pos.x - n.width / 2, y: pos.y - n.height / 2 } }
  })
}

function renderGraph() {
  chooseDefaultGraphFocus()
  const built = buildFlowElements()
  nodes.value = applyDagreLayout(built.nodes, built.edges)
  edges.value = built.edges
  // Structural rebuilds replace nodes.value/edges.value wholesale, wiping
  // any previously-applied live-data annotations -- reapply once so newly
  // rendered elements don't sit blank until the next WS tick. This is a
  // one-shot read of currently-known values, not a second reactive path;
  // it doesn't touch .position and isn't itself triggered by a live tick.
  updateLiveDisplay()
}

function fitGraph() {
  fitView({ padding: 0.15 })
}

function relayoutGraph() {
  nodes.value = applyDagreLayout(nodes.value, edges.value)
  nextTick(() => fitView({ padding: 0.15 }))
}

function onNodeClick({ node }: { node: any }) {
  const entityId = Number(node.data?.entityId)
  selectedGraphEntity.value = entityById.value.get(entityId) ?? null
}

function onNodeDoubleClick({ node }: { node: any }) {
  graphFocusEntityId.value = Number(node.data?.entityId)
}

watch(
  [graphFocusEntityId, graphDepth, graphShowPoints, entities, relationships],
  async () => {
    await nextTick()
    renderGraph()
  },
  { deep: true },
)
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
      <VueFlow
        v-model:nodes="nodes"
        v-model:edges="edges"
        class="semantic-graph-canvas"
        fit-view-on-init
        :min-zoom="0.25"
        :max-zoom="2.5"
        aria-label="Brick semantic relationship graph"
        @node-click="onNodeClick"
        @node-double-click="onNodeDoubleClick"
      >
        <Background :gap="16" />
        <Controls />
        <template #node-semanticNode="nodeProps">
          <SemanticGraphNode v-bind="nodeProps" />
        </template>
        <template #edge-semanticFlowEdge="edgeProps">
          <SemanticFlowEdge v-bind="edgeProps" />
        </template>
      </VueFlow>

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
