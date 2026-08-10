<script setup lang="ts">
import type { Node } from '@vue-flow/core'
import type { FunctionalTestOperand, Meta } from '../../types'

const props = defineProps<{
  node: Node | null
  meta: Meta
}>()

const OPERATOR_OPTIONS = [
  { value: 'eq', label: 'Equals' },
  { value: 'neq', label: 'Not equals' },
  { value: 'gt', label: 'Greater than' },
  { value: 'gte', label: 'Greater than or equal' },
  { value: 'lt', label: 'Less than' },
  { value: 'lte', label: 'Less than or equal' },
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

function setOperandKind(operand: FunctionalTestOperand | undefined, data: Record<string, any>, side: 'left' | 'right', kind: FunctionalTestOperand['kind']) {
  if (kind === 'point') data[side] = { kind: 'point', point_type: '' }
  else if (kind === 'constant') data[side] = { kind: 'constant', value: '' }
  else data[side] = { kind: 'variable', name: '' }
}

function ensureOperand(data: Record<string, any>, side: 'left' | 'right') {
  if (!data[side] || typeof data[side] !== 'object') {
    data[side] = { kind: 'point', point_type: '' }
  }
  return data[side]
}
</script>

<template>
  <aside class="ft-properties">
    <template v-if="node">
      <div class="ft-panel-title">{{ node.type }}</div>

      <a-form layout="vertical" size="small">
        <template v-if="node.type === 'wait'">
          <a-form-item label="Seconds">
            <a-input-number v-model:value="node.data.seconds" :min="0" style="width:100%" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'wait_until'">
          <a-form-item label="Point">
            <a-select
              v-model:value="node.data.point_type"
              show-search
              allow-clear
              placeholder="Select a point"
              :options="meta.point_types"
            />
          </a-form-item>
          <a-form-item label="Operator">
            <a-select v-model:value="node.data.operator" :options="OPERATOR_OPTIONS" />
          </a-form-item>
          <a-form-item label="Value">
            <a-input v-model:value="node.data.value" />
          </a-form-item>
          <a-form-item label="Timeout (seconds)">
            <a-input-number v-model:value="node.data.timeout_seconds" :min="1" style="width:100%" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'capture'">
          <a-form-item label="Point">
            <a-select
              v-model:value="node.data.point_type"
              show-search
              allow-clear
              placeholder="Select a point"
              :options="meta.point_types"
            />
          </a-form-item>
          <a-form-item label="Variable name">
            <a-input v-model:value="node.data.variable" placeholder="t1" />
          </a-form-item>
        </template>

        <template v-if="node.type === 'verify' || node.type === 'compare'">
          <a-form-item label="Left side">
            <a-select
              :value="ensureOperand(node.data, 'left').kind"
              :options="OPERAND_KIND_OPTIONS"
              style="margin-bottom:8px"
              @change="(kind: any) => setOperandKind(node!.data.left, node!.data, 'left', kind)"
            />
            <a-select
              v-if="node.data.left?.kind === 'point'"
              v-model:value="node.data.left.point_type"
              show-search
              allow-clear
              placeholder="Select a point"
              :options="meta.point_types"
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
              @change="(kind: any) => setOperandKind(node!.data.right, node!.data, 'right', kind)"
            />
            <a-select
              v-if="node.data.right?.kind === 'point'"
              v-model:value="node.data.right.point_type"
              show-search
              allow-clear
              placeholder="Select a point"
              :options="meta.point_types"
            />
            <a-input v-else-if="node.data.right?.kind === 'constant'" v-model:value="node.data.right.value" placeholder="Value" />
            <template v-else-if="node.data.right?.kind === 'variable'">
              <a-input v-model:value="node.data.right.name" placeholder="Variable name" style="margin-bottom:8px" />
              <a-input-number v-model:value="node.data.right.offset" placeholder="Offset (optional)" style="width:100%" />
            </template>
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
