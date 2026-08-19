<script setup lang="ts">
import type { Node } from '@vue-flow/core'
import type { FunctionalTestOperand, Meta, PointRow } from '../../types'
import PointPicker from '../PointPicker.vue'

const props = defineProps<{
  node: Node | null
  meta: Meta
  points: PointRow[]
}>()

const OPERATOR_OPTIONS = [
  { value: 'eq', label: 'Equals' },
  { value: 'neq', label: 'Not equals' },
  { value: 'gt', label: 'Greater than' },
  { value: 'gte', label: 'Greater than or equal' },
  { value: 'lt', label: 'Less than' },
  { value: 'lte', label: 'Less than or equal' },
  { value: 'within_tolerance', label: 'Within tolerance of' },
]

const END_RESULT_OPTIONS = [
  { value: 'pass', label: 'Pass' },
  { value: 'fail', label: 'Fail' },
  { value: 'inconclusive', label: 'Inconclusive' },
]

const OPERAND_KIND_OPTIONS = [
  { value: 'point', label: 'Point' },
  { value: 'constant', label: 'Constant value' },
  { value: 'variable', label: 'Captured variable' },
]

const CAPTIONABLE_TYPES = new Set(['wait', 'wait_until', 'capture', 'verify', 'compare', 'set'])

function setOperandKind(data: Record<string, any>, side: 'left' | 'right' | 'value', kind: FunctionalTestOperand['kind']) {
  if (kind === 'point') data[side] = { kind: 'point', point: null }
  else if (kind === 'constant') data[side] = { kind: 'constant', value: '' }
  else data[side] = { kind: 'variable', name: '' }
}

function ensureOperand(data: Record<string, any>, side: 'left' | 'right' | 'value') {
  if (!data[side] || typeof data[side] !== 'object') {
    data[side] = { kind: 'point', point: null }
  }
  return data[side]
}
</script>

<template>
  <aside class="ft-properties">
    <template v-if="node">
      <div class="ft-panel-title">{{ node.type }}</div>

      <a-form layout="vertical" size="small">
        <a-form-item v-if="CAPTIONABLE_TYPES.has(node.type as string)" label="Label (optional)">
          <a-input v-model:value="node.data.label" placeholder="e.g. Disable Chiller 2" />
        </a-form-item>

        <template v-if="node.type === 'wait'">
          <a-form-item label="Seconds">
            <a-input-number v-model:value="node.data.seconds" :min="0" style="width:100%" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'wait_until'">
          <a-form-item label="Point">
            <PointPicker v-model="node.data.point" :points="points" :meta="meta" />
          </a-form-item>
          <a-form-item label="Operator">
            <a-select v-model:value="node.data.operator" :options="OPERATOR_OPTIONS" />
          </a-form-item>
          <a-form-item label="Value">
            <a-select
              :value="ensureOperand(node.data, 'value').kind"
              :options="OPERAND_KIND_OPTIONS"
              style="margin-bottom:8px"
              @change="(kind: any) => setOperandKind(node!.data, 'value', kind)"
            />
            <PointPicker
              v-if="node.data.value?.kind === 'point'"
              v-model="node.data.value.point"
              :points="points"
              :meta="meta"
            />
            <a-input v-else-if="node.data.value?.kind === 'constant'" v-model:value="node.data.value.value" placeholder="Value" />
            <template v-else-if="node.data.value?.kind === 'variable'">
              <a-input v-model:value="node.data.value.name" placeholder="Variable name" style="margin-bottom:8px" />
              <a-input-number v-model:value="node.data.value.offset" placeholder="Offset (optional)" style="width:100%" />
            </template>
          </a-form-item>
          <a-form-item v-if="node.data.operator === 'within_tolerance'" label="Tolerance">
            <a-input-number v-model:value="node.data.tolerance" :min="0" style="width:100%" />
          </a-form-item>
          <a-form-item label="Stable for (seconds, optional)">
            <a-input-number
              v-model:value="node.data.stable_for_seconds" :min="0" style="width:100%"
              placeholder="Condition must hold this long, not just cross once"
            />
          </a-form-item>
          <a-form-item label="Timeout (seconds)">
            <a-input-number v-model:value="node.data.timeout_seconds" :min="1" style="width:100%" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'capture'">
          <a-form-item label="Point">
            <PointPicker v-model="node.data.point" :points="points" :meta="meta" />
          </a-form-item>
          <a-form-item label="Variable name">
            <a-input v-model:value="node.data.variable" placeholder="t1" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'set'">
          <a-form-item label="Point">
            <PointPicker v-model="node.data.point" :points="points" :meta="meta" />
          </a-form-item>
          <a-form-item label="Value">
            <a-input v-model:value="node.data.value" placeholder="e.g. OFF, 0, 72.5" />
          </a-form-item>
          <a-form-item label="Priority (optional, 1-16)">
            <a-input-number v-model:value="node.data.priority" :min="1" :max="16" style="width:100%" placeholder="Defaults to 8" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'verify' || node.type === 'compare'">
          <a-form-item label="Left side">
            <a-select
              :value="ensureOperand(node.data, 'left').kind"
              :options="OPERAND_KIND_OPTIONS"
              style="margin-bottom:8px"
              @change="(kind: any) => setOperandKind(node!.data, 'left', kind)"
            />
            <PointPicker
              v-if="node.data.left?.kind === 'point'"
              v-model="node.data.left.point"
              :points="points"
              :meta="meta"
            />
            <a-input v-else-if="node.data.left?.kind === 'constant'" v-model:value="node.data.left.value" placeholder="Value" />
            <template v-else-if="node.data.left?.kind === 'variable'">
              <a-input v-model:value="node.data.left.name" placeholder="Variable name" style="margin-bottom:8px" />
              <a-input-number v-model:value="node.data.left.offset" placeholder="Offset (optional)" style="width:100%" />
            </template>
          </a-form-item>

          <a-form-item label="Operator">
            <a-select v-model:value="node.data.operator" :options="OPERATOR_OPTIONS" />
          </a-form-item>

          <a-form-item label="Right side">
            <a-select
              :value="ensureOperand(node.data, 'right').kind"
              :options="OPERAND_KIND_OPTIONS"
              style="margin-bottom:8px"
              @change="(kind: any) => setOperandKind(node!.data, 'right', kind)"
            />
            <PointPicker
              v-if="node.data.right?.kind === 'point'"
              v-model="node.data.right.point"
              :points="points"
              :meta="meta"
            />
            <a-input v-else-if="node.data.right?.kind === 'constant'" v-model:value="node.data.right.value" placeholder="Value" />
            <template v-else-if="node.data.right?.kind === 'variable'">
              <a-input v-model:value="node.data.right.name" placeholder="Variable name" style="margin-bottom:8px" />
              <a-input-number v-model:value="node.data.right.offset" placeholder="Offset (optional)" style="width:100%" />
            </template>
          </a-form-item>

          <a-form-item v-if="node.data.operator === 'within_tolerance'" label="Tolerance">
            <a-input-number v-model:value="node.data.tolerance" :min="0" style="width:100%" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'end'">
          <a-form-item label="Result">
            <a-select v-model:value="node.data.result" :options="END_RESULT_OPTIONS" />
          </a-form-item>
          <a-form-item label="Message (optional)">
            <a-input v-model:value="node.data.message" />
          </a-form-item>
        </template>

        <div v-if="node.type === 'start'" class="ft-empty-properties">
          Start has no properties.
        </div>
      </a-form>
    </template>

    <div v-else class="ft-empty-properties">
      Select a block to configure it.
    </div>
  </aside>
</template>

<style scoped>
.ft-properties {
  padding: 14px;
  background: var(--surface, #fff);
  border-left: 1px solid var(--border, #d9d9d9);
  overflow-y: auto;
}

.ft-panel-title {
  margin-bottom: 12px;
  font-weight: 600;
  font-size: 13px;
  text-transform: capitalize;
}

.ft-empty-properties {
  color: var(--text-secondary, #888);
  font-size: 13px;
}
</style>
