<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import type { FunctionalTestOperand, Meta } from '../../types'
import type { PointLookup } from '../../pointLookup'
import { lookupPoint, pointDisplayLabel } from '../../pointLookup'

const props = defineProps<{
  type: string
  data: Record<string, any>
  meta: Meta
  pointLookup: PointLookup
  /** Vue Flow's own node-selected slot prop -- passed straight through via
   * v-bind="nodeProps" in the builder. */
  selected?: boolean
}>()

const KICKER: Record<string, string> = {
  wait: 'WAIT',
  wait_until: 'WAIT UNTIL',
  capture: 'CAPTURE',
  verify: 'VERIFY',
  compare: 'COMPARE',
  set: 'SET',
}

const OPERATOR_SYMBOL: Record<string, string> = {
  eq: '=',
  neq: '≠',
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
}

function pointLabel(pointRef: unknown): string {
  if (!pointRef) return 'Select point'
  return pointDisplayLabel(lookupPoint(props.pointLookup, pointRef as { device_id: number; object_id: number }))
}

function operandLabel(operand: unknown): string {
  const op = operand as Partial<FunctionalTestOperand> | undefined
  if (!op || !op.kind) return '?'
  if (op.kind === 'point') return pointLabel(op.point)
  if (op.kind === 'constant') return op.value === undefined || op.value === '' ? '?' : String(op.value)
  if (op.kind === 'variable') return op.name ? `{${op.name}}${op.offset ? ` +${op.offset}` : ''}` : '?'
  return '?'
}

function comparisonPhrase(operator: unknown, operand: unknown, tolerance: unknown): string {
  if (operator === 'within_tolerance') {
    return `within ${tolerance ?? '?'} of ${operandLabel(operand)}`
  }
  const symbol = OPERATOR_SYMBOL[operator as string] ?? (operator ?? '?')
  return `${symbol} ${operandLabel(operand)}`
}

const main = computed(() => {
  switch (props.type) {
    case 'wait':
      return `${props.data.seconds ?? 0} seconds`
    case 'wait_until':
    case 'capture':
    case 'set':
      return pointLabel(props.data.point)
    case 'verify':
    case 'compare':
      return operandLabel(props.data.left)
    default:
      return ''
  }
})

const detail = computed(() => {
  switch (props.type) {
    case 'wait_until': {
      let text = comparisonPhrase(props.data.operator, props.data.value, props.data.tolerance)
      if (props.data.stable_for_seconds) text += ` for ${props.data.stable_for_seconds}s`
      return `${text} (timeout ${props.data.timeout_seconds ?? '?'}s)`
    }
    case 'capture':
      return `Save as ${props.data.variable || '?'}`
    case 'verify':
    case 'compare':
      return comparisonPhrase(props.data.operator, props.data.right, props.data.tolerance)
    case 'set': {
      let text = `→ ${props.data.value ?? '?'}`
      if (props.data.priority) text += ` (priority ${props.data.priority})`
      return text
    }
    default:
      return ''
  }
})
</script>

<template>
  <div
    class="ft-node"
    :class="{ 'ft-node--branching': type === 'verify', 'ft-node--selected': selected }"
  >
    <Handle type="target" :position="Position.Top" />

    <div v-if="data.label" class="ft-node-label">{{ data.label }}</div>
    <div class="ft-node-kicker">{{ KICKER[type] }}</div>
    <div class="ft-node-main">{{ main }}</div>
    <div v-if="detail" class="ft-node-detail">{{ detail }}</div>

    <template v-if="type === 'verify'">
      <Handle id="pass" type="source" :position="Position.Bottom" style="left:35%" />
      <Handle id="fail" type="source" :position="Position.Bottom" style="left:65%" />
      <div class="ft-node-branch-labels">
        <span>PASS</span>
        <span>FAIL</span>
      </div>
    </template>
    <Handle v-else type="source" :position="Position.Bottom" />
  </div>
</template>

<style scoped>
.ft-node {
  position: relative;
  min-width: 210px;
  padding: 10px 12px;
  background: var(--surface-alt, #f5f5f5);
  border: 1px solid #faad14;
  border-radius: 8px;
  box-shadow: var(--card-shadow, 0 2px 8px rgb(0 0 0 / 6%));
  font-size: 12px;
}

.ft-node--branching {
  padding-bottom: 20px;
}

.ft-node--selected {
  outline: 3px dashed #eb2f96;
  outline-offset: 3px;
}

.ft-node-label {
  margin-bottom: 2px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary, #666);
}

.ft-node-kicker {
  margin-bottom: 4px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: #1677ff;
}

.ft-node-main {
  font-weight: 600;
}

.ft-node-detail {
  margin-top: 3px;
  color: var(--text-secondary, #666);
}

.ft-node-branch-labels {
  display: flex;
  justify-content: space-around;
  margin-top: 12px;
  font-size: 9px;
  color: var(--text-secondary, #888);
}
</style>
