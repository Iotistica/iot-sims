/** Pure, framework-free serialize/hydrate for Functional Test definitions.
 * Deliberately has no @vue-flow/core import beyond types -- the backend
 * runner only ever needs nodes/edges/params; `layout` exists purely for the
 * editor to restore node positions, never for execution.
 *
 * Start/End are implicit in the EDITOR (no palette entry, nothing to wire
 * by hand) but still explicit, real nodes in the persisted
 * FunctionalTestDefinition -- the backend schema/executor are unchanged and
 * still require exactly one 'start' node and every path to terminate at an
 * 'end' node (see src/functional_tests/executor.py). serializeFunctionalTest
 * synthesizes those nodes from the graph's shape (the one block nothing
 * points to is the entry; any output handle nothing is wired to is a
 * terminal -- 'fail' handle -> End(fail), everything else -> End(pass));
 * hydrateFunctionalTest reverses that, so old saved tests with real
 * hand-placed Start/End nodes still load and display correctly (falling
 * back to showing an explicit End node only when one doesn't fit the
 * implicit pass/fail-on-dangling-handle shape -- e.g. 'inconclusive', a
 * custom message, or a handle/result pairing the implicit model can't
 * represent -- so nothing is ever silently dropped on round-trip). */
import type { Node, Edge } from '@vue-flow/core'
import type {
  FunctionalTestDefinition,
  FunctionalTestEdgeDefinition,
  FunctionalTestNodeDefinition,
  FunctionalTestNodeType,
} from './types'

/** Per-type allowlist of the fields serialized into `params` -- keeps any
 * truly editor-only field on a node's `data` out of the persisted
 * definition. `label` (the optional human-authored caption, e.g. "Disable
 * Chiller 2") IS persisted -- it's part of the authored test, not
 * transient UI state. */
const PARAM_KEYS: Record<FunctionalTestNodeType, string[]> = {
  start: [],
  wait: ['seconds', 'label'],
  wait_until: ['point', 'operator', 'value', 'tolerance', 'stable_for_seconds', 'timeout_seconds', 'label'],
  capture: ['point', 'variable', 'label'],
  verify: ['left', 'operator', 'right', 'tolerance', 'label'],
  compare: ['left', 'operator', 'right', 'tolerance', 'label'],
  set: ['point', 'value', 'priority', 'label'],
  end: ['result', 'message'],
}

/** Ids that can never collide with a user-created block id (those are
 * `${type}-${crypto.randomUUID()}`). */
const SYNTH_START_ID = '__start__'
function synthEndId(nodeId: string, handle: 'pass' | 'fail' | null): string {
  return `__end_${handle ?? 'next'}__${nodeId}__`
}

/** Handles a node type can branch on -- 'verify' produces two logical
 * branches (pass/fail); every other type (including 'compare', which
 * evaluates a comparison but never branches) has one, unlabeled output. */
function outputHandles(type: FunctionalTestNodeType): (string | null)[] {
  return type === 'verify' ? ['pass', 'fail'] : [null]
}

export function serializeFunctionalTest(
  nodes: Node[],
  edges: Edge[],
): FunctionalTestDefinition {
  const definitionNodes: FunctionalTestNodeDefinition[] = []
  const definitionEdges: FunctionalTestEdgeDefinition[] = []
  const layout: Record<string, { x: number; y: number }> = {}

  for (const node of nodes) {
    const type = node.type as FunctionalTestNodeType
    const allowedKeys = PARAM_KEYS[type] ?? []
    const data = (node.data ?? {}) as Record<string, unknown>

    const params: Record<string, unknown> = {}
    for (const key of allowedKeys) {
      if (data[key] !== undefined) params[key] = data[key]
    }

    definitionNodes.push({ id: node.id, type, params } as FunctionalTestNodeDefinition)
    layout[node.id] = { x: node.position.x, y: node.position.y }
  }

  for (const edge of edges) {
    definitionEdges.push({
      source: edge.source,
      target: edge.target,
      source_handle: (edge.sourceHandle ?? null) as 'pass' | 'fail' | null,
    })
  }

  // Entry: the block nothing points to. A validated graph has exactly one;
  // defensively fall back to the first node rather than emitting a
  // definition with no entry point at all if that's somehow not the case.
  const targeted = new Set(definitionEdges.map(e => e.target))
  const entry = definitionNodes.find(n => n.type !== 'end' && !targeted.has(n.id)) ?? definitionNodes[0]

  if (entry) {
    definitionNodes.push({ id: SYNTH_START_ID, type: 'start', params: {} } as FunctionalTestNodeDefinition)
    definitionEdges.push({ source: SYNTH_START_ID, target: entry.id, source_handle: null })
    const entryPos = layout[entry.id] ?? { x: 150, y: 80 }
    layout[SYNTH_START_ID] = { x: entryPos.x, y: entryPos.y - 120 }
  }

  // Terminals: any output handle of a non-'end' node with no outgoing edge
  // yet (an explicit 'end' node surviving from hydrateFunctionalTest's
  // backward-compat fallback is already a terminal and needs nothing
  // synthesized for it).
  const outgoingHandles = new Map<string, Set<string | null>>()
  for (const edge of definitionEdges) {
    if (!outgoingHandles.has(edge.source)) outgoingHandles.set(edge.source, new Set())
    outgoingHandles.get(edge.source)!.add(edge.source_handle)
  }

  for (const node of [...definitionNodes]) {
    if (node.type === 'start' || node.type === 'end') continue
    const used = outgoingHandles.get(node.id) ?? new Set()

    for (const handle of outputHandles(node.type)) {
      if (used.has(handle)) continue

      const endId = synthEndId(node.id, handle as 'pass' | 'fail' | null)
      const result = handle === 'fail' ? 'fail' : 'pass'
      definitionNodes.push({ id: endId, type: 'end', params: { result } } as FunctionalTestNodeDefinition)
      definitionEdges.push({ source: node.id, target: endId, source_handle: handle as 'pass' | 'fail' | null })

      const from = layout[node.id] ?? { x: 150, y: 80 }
      const dx = handle === 'fail' ? 120 : handle === 'pass' ? -120 : 0
      layout[endId] = { x: from.x + dx, y: from.y + 140 }
    }
  }

  return { version: 1, nodes: definitionNodes, edges: definitionEdges, layout }
}

/** Default cascade position for a node with no layout entry (e.g. a
 * hand-built or older definition) so it still renders somewhere sane. */
function fallbackPosition(index: number): { x: number; y: number } {
  return { x: 150 + (index % 4) * 40, y: 80 + index * 120 }
}

export function hydrateFunctionalTest(
  definition: FunctionalTestDefinition,
): { nodes: Node[]; edges: Edge[] } {
  const removedNodeIds = new Set<string>()
  const removedEdgeIndexes = new Set<number>()

  // Start is always safely implicit: strip the node and its one outgoing
  // edge -- whatever it targeted becomes the entry (nothing else points to
  // it), exactly matching serializeFunctionalTest's own definition of
  // "entry".
  const startNode = definition.nodes.find(n => n.type === 'start')
  if (startNode) {
    removedNodeIds.add(startNode.id)
    definition.edges.forEach((edge, index) => {
      if (edge.source === startNode.id) removedEdgeIndexes.add(index)
    })
  }

  // End nodes are stripped only when EVERY incoming edge fits the implicit
  // pattern ('fail' handle <-> result 'fail', 'pass'/null handle <-> result
  // 'pass') and there's no custom message -- an all-or-nothing check per
  // node, so a shared End reached by several branches is only ever
  // stripped as a whole. Anything that doesn't fit (inconclusive, a
  // mismatched handle/result pairing, a message) is left as an explicit
  // node so older/unusual saved tests still display and round-trip
  // faithfully instead of silently losing information.
  for (const node of definition.nodes) {
    if (node.type !== 'end') continue
    const params = node.params as { result?: string; message?: string }
    if (params.message) continue

    const incoming = definition.edges
      .map((edge, index) => ({ edge, index }))
      .filter(({ edge }) => edge.target === node.id)
    if (incoming.length === 0) continue

    const allImplicit = incoming.every(({ edge }) =>
      edge.source_handle === 'fail' ? params.result === 'fail' : params.result === 'pass'
    )
    if (!allImplicit) continue

    removedNodeIds.add(node.id)
    for (const { index } of incoming) removedEdgeIndexes.add(index)
  }

  const keptNodes = definition.nodes.filter(n => !removedNodeIds.has(n.id))
  const keptEdges = definition.edges.filter((_, index) => !removedEdgeIndexes.has(index))

  const nodes: Node[] = keptNodes.map((node, index) => ({
    id: node.id,
    type: node.type,
    position: definition.layout?.[node.id] ?? fallbackPosition(index),
    data: { ...node.params },
  }))

  const edges: Edge[] = keptEdges.map((edge, index) => ({
    id: `e-${edge.source}-${edge.target}-${edge.source_handle ?? 'default'}-${index}`,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle ?? undefined,
  }))

  return { nodes, edges }
}
