/** Pure, framework-free serialize/hydrate for Functional Test definitions.
 * Deliberately has no @vue-flow/core import -- the (future) backend runner
 * only ever needs nodes/edges/params; `layout` exists purely for the
 * editor to restore node positions, never for execution. */
import type { Node, Edge } from '@vue-flow/core'
import type {
  FunctionalTestDefinition,
  FunctionalTestNodeDefinition,
  FunctionalTestNodeType,
} from './types'

/** Per-type allowlist of the fields serialized into `params` -- keeps any
 * editor-only field on a node's `data` (e.g. a transient label) out of the
 * persisted definition. */
const PARAM_KEYS: Record<FunctionalTestNodeType, string[]> = {
  start: [],
  wait: ['seconds'],
  wait_until: ['point_type', 'operator', 'value', 'timeout_seconds'],
  capture: ['point_type', 'variable'],
  verify: ['left', 'operator', 'right'],
  compare: ['left', 'operator', 'right'],
  end: ['result', 'message'],
}

export function serializeFunctionalTest(
  nodes: Node[],
  edges: Edge[],
): FunctionalTestDefinition {
  const definitionNodes: FunctionalTestNodeDefinition[] = nodes.map(node => {
    const type = node.type as FunctionalTestNodeType
    const allowedKeys = PARAM_KEYS[type] ?? []
    const data = (node.data ?? {}) as Record<string, unknown>

    const params: Record<string, unknown> = {}
    for (const key of allowedKeys) {
      if (data[key] !== undefined) params[key] = data[key]
    }

    return { id: node.id, type, params } as FunctionalTestNodeDefinition
  })

  const definitionEdges = edges.map(edge => ({
    source: edge.source,
    target: edge.target,
    source_handle: (edge.sourceHandle ?? null) as 'pass' | 'fail' | null,
  }))

  const layout: Record<string, { x: number; y: number }> = {}
  for (const node of nodes) {
    layout[node.id] = { x: node.position.x, y: node.position.y }
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
  const nodes: Node[] = definition.nodes.map((node, index) => ({
    id: node.id,
    type: node.type,
    position: definition.layout?.[node.id] ?? fallbackPosition(index),
    data: { ...node.params },
  }))

  const edges: Edge[] = definition.edges.map((edge, index) => ({
    id: `e-${edge.source}-${edge.target}-${edge.source_handle ?? 'default'}-${index}`,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle ?? undefined,
  }))

  return { nodes, edges }
}
