<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import type { FunctionalTestOperand, Meta } from '../../types'

const props = defineProps<{
  type: string
  data: Record<string, any>
  meta: Meta
  /** Optional run-mode overlay -- undefined leaves the builder's normal
   * editing appearance untouched. Only FunctionalTestRunDialog's read-only
   * canvas sets this. */
  status?: 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
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
}

function pointLabel(pointType: unknown): string {
  if (!pointType) return 'Select point'
  const option = props.meta.point_types.find(o => o.value === pointType)
  return option?.label ?? String(pointType)
}

function operandLabel(operand: unknown): string {
  const op = operand as Partial<FunctionalTestOperand> | undefined
  if (!op || !op.kind) return '?'
  if (op.kind === 'point') return pointLabel(op.point_type)
  if (op.kind === 'constant') return op.value === undefined || op.value === '' ? '?' : String(op.value)
  if (op.kind === 'variable') return op.name ? `{${op.name}}${op.offset ? ` +${op.offset}` : ''}` : '?'
  return '?'
}

const main = computed(() => {
  switch (props.type) {
    case 'wait':
      return `${props.data.seconds ?? 0} seconds`
    case 'wait_until':
    case 'capture':
      return pointLabel(props.data.point_type)
    case 'verify':
    case 'compare':
      return operandLabel(props.data.left)
    default:
      return ''
  }
})

const detail = computed(() => {
  switch (props.type) {
    case 'wait_until':
      return `${props.data.operator ?? '?'} ${props.data.value ?? '?'} (timeout ${props.data.timeout_seconds ?? '?'}s)`
    case 'capture':
      return `Save as ${props.data.variable || '?'}`
    case 'verify':
    case 'compare':
      return `${props.data.operator ?? '?'} ${operandLabel(props.data.right)}`
    default:
      return ''
  }
})
</script>

<template>
  <div
    class="ft-node"
    :class="[
      { 'ft-node--branching': type === 'verify', 'ft-node--selected': selected },
      status ? `ft-node--status-${status}` : '',
    ]"
  >
    <Handle type="target" :position="Position.Top" />

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
  background: var(--surface, #fff);
  border: 1px solid var(--border, #d9d9d9);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgb(0 0 0 / 6%);
  font-size: 12px;
}

.ft-node--branching {
  padding-bottom: 20px;
}

.ft-node--selected {
  outline: 3px dashed #eb2f96;
  outline-offset: 3px;
}

.ft-node--status-running {
  border-left: 3px solid #1677ff;
}

.ft-node--status-passed {
  border-left: 3px solid #52c41a;
}

.ft-node--status-failed {
  border-left: 3px solid #ff4d4f;
}

.ft-node--status-skipped {
  opacity: 0.5;
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
