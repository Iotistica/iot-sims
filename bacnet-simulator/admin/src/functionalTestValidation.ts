/** Pure, framework-free graph-quality validation for the Functional Test
 * builder canvas. Boundary (see plan/CLAUDE notes): this is the
 * "is this a complete, sensible, executable test graph?" half -- exactly
 * one Start, reachability, missing terminal path, unused/undefined capture
 * variables, friendly per-node issue navigation. The backend
 * (src/functional_tests/validation.py) owns the other half -- "is this a
 * well-formed FunctionalTestDefinition?" (node types, params shapes,
 * dangling edge references) -- and deliberately does not duplicate this
 * graph analysis. */
import type { Node, Edge } from '@vue-flow/core'
import type { FunctionalTestIssue, FunctionalTestOperand, Meta } from './types'

const IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
const OPERATORS = new Set(['eq', 'neq', 'gt', 'gte', 'lt', 'lte'])

function isPointType(meta: Meta, value: unknown): boolean {
  return typeof value === 'string' && meta.point_types.some(o => o.value === value)
}

function operandIssues(
  meta: Meta,
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
    if (!isPointType(meta, op.point_type)) {
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

  const startNodes = nodes.filter(n => n.type === 'start')
  if (startNodes.length === 0) {
    issues.push({ nodeId: null, message: 'Add a Start block' })
  } else if (startNodes.length > 1) {
    for (const n of startNodes) {
      issues.push({ nodeId: n.id, message: 'Only one Start block is allowed' })
    }
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

  // Reachability from Start (breadth-first over outgoing edges).
  const reachable = new Set<string>()
  if (startNodes.length > 0) {
    const queue = [startNodes[0].id]
    reachable.add(startNodes[0].id)
    while (queue.length > 0) {
      const current = queue.shift()!
      for (const edge of edges) {
        if (edge.source === current && !reachable.has(edge.target) && nodesById.has(edge.target)) {
          reachable.add(edge.target)
          queue.push(edge.target)
        }
      }
    }
  }

  for (const node of nodes) {
    if (node.type !== 'start' && !reachable.has(node.id)) {
      issues.push({ nodeId: node.id, message: 'This block is not reachable from Start' })
    }
  }

  const hasReachableEnd = nodes.some(n => n.type === 'end' && reachable.has(n.id))
  if (startNodes.length > 0 && !hasReachableEnd) {
    issues.push({ nodeId: null, message: 'No End block is reachable from Start' })
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

    const pointType = (node.data as { point_type?: string } | undefined)?.point_type
    if (!isPointType(meta, pointType)) {
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
      if (!isPointType(meta, data.point_type)) {
        issues.push({ nodeId: node.id, message: 'Wait Until needs a point selected' })
      }
      if (!OPERATORS.has(data.operator as string)) {
        issues.push({ nodeId: node.id, message: 'Wait Until needs an operator' })
      }
      if (data.value === undefined || data.value === null || data.value === '') {
        issues.push({ nodeId: node.id, message: 'Wait Until needs a value to compare against' })
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
      issues.push(...operandIssues(meta, node.id, `${label} left side`, data.left, seenVariableNames))
      issues.push(...operandIssues(meta, node.id, `${label} right side`, data.right, seenVariableNames))
    }

    if (node.type === 'end') {
      if (!['pass', 'fail', 'inconclusive'].includes(data.result as string)) {
        issues.push({ nodeId: node.id, message: 'End needs a result' })
      }
    }
  }

  return issues
}
