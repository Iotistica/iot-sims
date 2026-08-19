/** Pure, framework-free graph-quality validation for the Functional Test
 * builder canvas. Boundary (see plan/CLAUDE notes): this is the
 * "is this a complete, sensible, executable test graph?" half -- exactly
 * one entry point, reachability, unused/undefined capture variables,
 * friendly per-node issue navigation. The backend
 * (src/functional_tests/validation.py) owns the other half -- "is this a
 * well-formed FunctionalTestDefinition?" (node types, params shapes,
 * dangling edge references) -- and deliberately does not duplicate this
 * graph analysis.
 *
 * Start/End are implicit here, not dedicated block types the user places:
 * the entry point is whichever block has no incoming edge, and termination
 * is automatic wherever a block's output handle has no outgoing edge (see
 * functionalTestSerializer.ts, which synthesizes the real Start/End nodes
 * the backend still requires from exactly this shape) -- so there is no
 * "must have a Start block" / "must have a reachable End block" check here
 * anymore; any non-empty, single-entry, fully-connected graph is already
 * complete by construction. */
import type { Node, Edge } from '@vue-flow/core'
import type { FunctionalTestIssue, FunctionalTestOperand, Meta, PointRef } from './types'

const IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
const OPERATORS = new Set(['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'within_tolerance'])

/** A concrete PointRef, not a Brick semantic class -- see types.ts's
 * PointRef doc comment. Existence against the live device/object list is
 * NOT checked here (that's the backend readiness check, a live-DB concern);
 * this only checks shape. */
function isPointRef(value: unknown): value is PointRef {
  const ref = value as Partial<PointRef> | undefined
  return !!ref && typeof ref === 'object'
    && typeof ref.device_id === 'number' && typeof ref.object_id === 'number'
}

function toleranceIssues(nodeId: string, label: string, operator: unknown, tolerance: unknown): FunctionalTestIssue[] {
  if (operator !== 'within_tolerance') return []
  if (typeof tolerance !== 'number' || Number.isNaN(tolerance) || tolerance < 0) {
    return [{ nodeId, message: `${label}: needs a non-negative tolerance` }]
  }
  return []
}

function operandIssues(
  nodeId: string,
  label: string,
  operand: unknown,
  definedVariables: Set<string>,
): FunctionalTestIssue[] {
  const issues: FunctionalTestIssue[] = []
  const op = operand as Partial<FunctionalTestOperand> | undefined

  if (!op || typeof op !== 'object') {
    issues.push({ nodeId, message: `${label}: operand is not set` })
    return issues
  }

  if (op.kind === 'point') {
    if (!isPointRef(op.point)) {
      issues.push({ nodeId, message: `${label}: point is not set` })
    }
  } else if (op.kind === 'constant') {
    if (op.value === undefined || op.value === null || op.value === '') {
      issues.push({ nodeId, message: `${label}: constant value is not set` })
    }
  } else if (op.kind === 'variable') {
    if (!op.name || !IDENTIFIER_RE.test(op.name)) {
      issues.push({ nodeId, message: `${label}: variable name is not set` })
    } else if (!definedVariables.has(op.name)) {
      issues.push({ nodeId, message: `${label}: variable "${op.name}" is never captured upstream` })
    }
  } else {
    issues.push({ nodeId, message: `${label}: operand kind is not set` })
  }

  return issues
}

export function validateFunctionalTest(
  nodes: Node[],
  edges: Edge[],
  meta: Meta,
  equipmentType: string,
): FunctionalTestIssue[] {
  const issues: FunctionalTestIssue[] = []

  if (!equipmentType || !meta.equipment_types.some(o => o.value === equipmentType)) {
    issues.push({ nodeId: null, message: 'Choose an equipment type this test applies to' })
  }

  const nodesById = new Map(nodes.map(n => [n.id, n]))

  // Dangling edge endpoints.
  for (const edge of edges) {
    if (!nodesById.has(edge.source)) {
      issues.push({ nodeId: null, message: `An edge references a missing block: ${edge.source}` })
    }
    if (!nodesById.has(edge.target)) {
      issues.push({ nodeId: null, message: `An edge references a missing block: ${edge.target}` })
    }
  }

  // Entry point: the block nothing points to. Start is implicit now (see
  // this file's module doc comment) -- the test begins at whichever block
  // has no incoming edge, not a dedicated Start type.
  const targeted = new Set(edges.map(e => e.target))
  const entryNodes = nodes.filter(n => !targeted.has(n.id))

  if (nodes.length === 0) {
    issues.push({ nodeId: null, message: 'Add a block to build your test' })
  } else if (entryNodes.length === 0) {
    issues.push({ nodeId: null, message: 'The test has no starting block -- every block has something pointing into it (a cycle with no entry point)' })
  } else if (entryNodes.length > 1) {
    for (const n of entryNodes) {
      issues.push({ nodeId: n.id, message: 'Disconnected from the rest of the test -- connect it into the flow' })
    }
  }

  // Reachability from the entry block (breadth-first over outgoing edges) --
  // only meaningful with exactly one entry point; with zero or multiple,
  // that's already reported above and flooding every other block with a
  // redundant "not reachable" issue wouldn't help.
  const reachable = new Set<string>()
  if (entryNodes.length === 1) {
    const queue = [entryNodes[0].id]
    reachable.add(entryNodes[0].id)
    while (queue.length > 0) {
      const current = queue.shift()!
      for (const edge of edges) {
        if (edge.source === current && !reachable.has(edge.target) && nodesById.has(edge.target)) {
          reachable.add(edge.target)
          queue.push(edge.target)
        }
      }
    }

    for (const node of nodes) {
      if (!reachable.has(node.id)) {
        issues.push({ nodeId: node.id, message: 'This block is not reachable from the start of the test' })
      }
    }
  }

  // Capture variable names: unique, and available to any reachable
  // downstream node whose operand references them by name.
  const captureNodes = nodes.filter(n => n.type === 'capture')
  const seenVariableNames = new Set<string>()
  for (const node of captureNodes) {
    const variable = (node.data as { variable?: string } | undefined)?.variable
    if (!variable || !IDENTIFIER_RE.test(variable)) {
      issues.push({ nodeId: node.id, message: 'Capture needs a valid variable name' })
    } else if (seenVariableNames.has(variable)) {
      issues.push({ nodeId: node.id, message: `Variable name "${variable}" is already used by another Capture block` })
    } else {
      seenVariableNames.add(variable)
    }

    const point = (node.data as { point?: unknown } | undefined)?.point
    if (!isPointRef(point)) {
      issues.push({ nodeId: node.id, message: 'Capture needs a point selected' })
    }
  }

  for (const node of nodes) {
    const data = (node.data ?? {}) as Record<string, unknown>

    if (node.type === 'wait') {
      if (typeof data.seconds !== 'number' || data.seconds < 0) {
        issues.push({ nodeId: node.id, message: 'Wait needs a number of seconds' })
      }
    }

    if (node.type === 'wait_until') {
      if (!isPointRef(data.point)) {
        issues.push({ nodeId: node.id, message: 'Wait Until needs a point selected' })
      }
      if (!OPERATORS.has(data.operator as string)) {
        issues.push({ nodeId: node.id, message: 'Wait Until needs an operator' })
      }
      issues.push(...operandIssues(node.id, 'Wait Until value', data.value, seenVariableNames))
      issues.push(...toleranceIssues(node.id, 'Wait Until', data.operator, data.tolerance))
      if (data.stable_for_seconds !== undefined && (typeof data.stable_for_seconds !== 'number' || data.stable_for_seconds < 0)) {
        issues.push({ nodeId: node.id, message: 'Wait Until "stable for" must be a non-negative number of seconds' })
      }
      if (typeof data.timeout_seconds !== 'number' || data.timeout_seconds <= 0) {
        issues.push({ nodeId: node.id, message: 'Wait Until needs a timeout' })
      }
    }

    if (node.type === 'verify' || node.type === 'compare') {
      if (!OPERATORS.has(data.operator as string)) {
        issues.push({ nodeId: node.id, message: `${node.type === 'verify' ? 'Verify' : 'Compare'} needs an operator` })
      }
      const label = node.type === 'verify' ? 'Verify' : 'Compare'
      issues.push(...operandIssues(node.id, `${label} left side`, data.left, seenVariableNames))
      issues.push(...operandIssues(node.id, `${label} right side`, data.right, seenVariableNames))
      issues.push(...toleranceIssues(node.id, label, data.operator, data.tolerance))
    }

    if (node.type === 'set') {
      if (!isPointRef(data.point)) {
        issues.push({ nodeId: node.id, message: 'Set needs a point selected' })
      }
      if (data.value === undefined || data.value === null || data.value === '') {
        issues.push({ nodeId: node.id, message: 'Set needs a value to write' })
      }
      if (data.priority !== undefined && (typeof data.priority !== 'number' || data.priority < 1 || data.priority > 16)) {
        issues.push({ nodeId: node.id, message: 'Set priority must be between 1 and 16' })
      }
    }

    if (node.type === 'end') {
      if (!['pass', 'fail', 'inconclusive'].includes(data.result as string)) {
        issues.push({ nodeId: node.id, message: 'End needs a result' })
      }
    }
  }

  return issues
}
