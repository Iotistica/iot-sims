<script setup lang="ts">
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  VueFlow,
  Handle,
  Position,
  type Connection,
  type Edge,
  type Node,
} from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'

import { api } from '../../api'
import type { FunctionalTest, FunctionalTestIssue, FunctionalTestNodeType, Meta } from '../../types'
import { hydrateFunctionalTest, serializeFunctionalTest } from '../../functionalTestSerializer'
import { validateFunctionalTest } from '../../functionalTestValidation'
import { boilerHeatingResponseExample } from '../../functionalTestExamples'
import FunctionalTestPalette from './FunctionalTestPalette.vue'
import FunctionalTestProperties from './FunctionalTestProperties.vue'
import FunctionalTestNode from './FunctionalTestNode.vue'
import FunctionalTestRunDialog from './FunctionalTestRunDialog.vue'

const props = defineProps<{
  test: FunctionalTest | null
  meta: Meta
}>()

const emit = defineEmits<{
  saved: [FunctionalTest]
  back: []
}>()

const DEFAULT_PARAMS: Record<FunctionalTestNodeType, () => Record<string, any>> = {
  start: () => ({}),
  wait: () => ({ seconds: 30 }),
  wait_until: () => ({ point_type: '', operator: 'eq', value: '', timeout_seconds: 300 }),
  capture: () => ({ point_type: '', variable: '' }),
  verify: () => ({ left: { kind: 'point', point_type: '' }, operator: 'eq', right: { kind: 'constant', value: '' } }),
  compare: () => ({ left: { kind: 'point', point_type: '' }, operator: 'eq', right: { kind: 'constant', value: '' } }),
  end: () => ({ result: 'pass' }),
}

const testId = ref<number | null>(props.test?.id ?? null)
const name = ref(props.test?.name ?? 'New Functional Test')
const description = ref(props.test?.description ?? '')
const equipmentType = ref(props.test?.equipment_type ?? '')

// Run always executes the last SAVED definition, never the canvas's
// current in-progress edits (trust boundary: edit -> validate -> save ->
// run saved test) -- kept separate from the editable name/description/
// equipmentType/nodes/edges above.
const savedTest = ref<FunctionalTest | null>(props.test ?? null)
const runDialogOpen = ref(false)

function initialFlow(): { nodes: Node[]; edges: Edge[] } {
  if (props.test) return hydrateFunctionalTest(props.test.definition)
  return {
    nodes: [{ id: 'start', type: 'start', position: { x: 260, y: 40 }, data: {} }],
    edges: [],
  }
}

// Vue Flow's Node/Edge types are discriminated unions with function-typed
// (ClassFunc/StyleFunc) members -- combined with Vue's UnwrapRef, plain
// `ref<Node[]>`/`ref<Edge[]>` mutations (push/filter/reassign) blow up
// vue-tsc with "Type instantiation is excessively deep" (a known Vue Flow
// + vue-tsc interaction, not a real type error). `nodes`/`edges` are kept
// as `any[]` internally and only cast back to Node[]/Edge[] at the
// boundaries (VueFlow's v-model, the serializer) where real typing matters.
const initial = initialFlow()
const nodes = ref(initial.nodes as any[])
const edges = ref(initial.edges as any[])

const selectedNodeId = ref<string | null>(null)
const selectedNode = computed<Node | null>(() => {
  const found = nodes.value.find(n => n.id === selectedNodeId.value)
  return (found as Node | undefined) ?? null
})
const canDeleteSelected = computed(() => !!selectedNodeId.value)

const issues = ref<FunctionalTestIssue[]>([])
const saving = ref(false)

function onConnect(connection: Connection) {
  edges.value.push({
    id: `e-${connection.source}-${connection.target}-${connection.sourceHandle ?? 'default'}`,
    source: connection.source,
    target: connection.target,
    sourceHandle: connection.sourceHandle ?? undefined,
    targetHandle: connection.targetHandle ?? undefined,
  })
}

function selectNode(node: Node) {
  selectedNodeId.value = node.id
}

function addNode(type: FunctionalTestNodeType) {
  const id = `${type}-${crypto.randomUUID()}`
  nodes.value.push({
    id,
    type,
    position: { x: 150 + Math.random() * 250, y: 100 + nodes.value.length * 60 },
    data: DEFAULT_PARAMS[type](),
  })
  selectedNodeId.value = id
}

function removeSelectedNode() {
  if (!selectedNodeId.value) return
  const id = selectedNodeId.value
  nodes.value = nodes.value.filter((n: Node) => n.id !== id)
  edges.value = edges.value.filter((e: Edge) => e.source !== id && e.target !== id)
  selectedNodeId.value = null
}

function runValidation(): FunctionalTestIssue[] {
  const result = validateFunctionalTest(nodes.value as Node[], edges.value as Edge[], props.meta, equipmentType.value)
  issues.value = result
  return result
}

function selectIssue(issue: FunctionalTestIssue) {
  if (issue.nodeId) selectedNodeId.value = issue.nodeId
}

function loadExample() {
  const hydrated = hydrateFunctionalTest(boilerHeatingResponseExample)
  nodes.value = hydrated.nodes
  edges.value = hydrated.edges
  name.value = 'Boiler Heating Response'
  equipmentType.value = 'Boiler'
  selectedNodeId.value = null
  issues.value = []
}

async function save() {
  const result = runValidation()
  if (result.length > 0) {
    message.warning(`Fix ${result.length} issue${result.length === 1 ? '' : 's'} before saving`)
    return
  }

  saving.value = true
  try {
    const definition = serializeFunctionalTest(nodes.value as Node[], edges.value as Edge[])
    const body = { name: name.value, description: description.value, equipment_type: equipmentType.value, definition }
    const saved = testId.value
      ? await api.functionalTests.update(testId.value, body)
      : await api.functionalTests.create(body)
    testId.value = saved.id
    savedTest.value = saved
    message.success('Functional test saved')
    emit('saved', saved)
  } catch (e: any) {
    message.error(e?.message || 'Failed to save functional test')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="ft-builder">
    <div class="ft-header">
      <a-button @click="emit('back')">
        &larr; Back
      </a-button>

      <a-input v-model:value="name" placeholder="Test name" style="width:260px" />

      <a-select
        v-model:value="equipmentType"
        show-search
        allow-clear
        placeholder="Applies to equipment type"
        :options="meta.equipment_types"
        style="width:220px"
      />

      <a-input v-model:value="description" placeholder="Description (optional)" style="flex:1;min-width:160px" />

      <a-button @click="loadExample">Load Example</a-button>
      <a-button @click="runValidation">Validate</a-button>
      <a-tooltip :title="savedTest ? 'Run the last saved version of this test' : 'Save the test before running it'">
        <a-button :disabled="!savedTest" @click="runDialogOpen = true">Run</a-button>
      </a-tooltip>
      <a-button type="primary" :loading="saving" @click="save">Save</a-button>
    </div>

    <div v-if="issues.length > 0" class="ft-issues">
      <div class="ft-issues-title">{{ issues.length }} issue{{ issues.length === 1 ? '' : 's' }} found</div>
      <div
        v-for="(issue, idx) in issues"
        :key="idx"
        class="ft-issue"
        :class="{ 'ft-issue--clickable': !!issue.nodeId }"
        @click="selectIssue(issue)"
      >
        {{ issue.message }}
      </div>
    </div>

    <div class="ft-body">
      <FunctionalTestPalette
        :can-delete-selected="canDeleteSelected"
        @add="addNode"
        @delete-selected="removeSelectedNode"
      />

      <main class="ft-canvas">
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          fit-view-on-init
          :min-zoom="0.3"
          :max-zoom="1.8"
          @connect="onConnect"
          @node-click="({ node }) => selectNode(node)"
        >
          <Background :gap="16" />
          <Controls />

          <template #node-start="{ selected }">
            <div class="ft-node ft-node--endpoint" :class="{ 'ft-node--selected': selected }">
              <div class="ft-node-title">Start</div>
              <Handle type="source" :position="Position.Bottom" />
            </div>
          </template>

          <template #node-end="{ data, selected }">
            <div class="ft-node ft-node--endpoint" :class="{ 'ft-node--selected': selected }">
              <Handle type="target" :position="Position.Top" />
              <div class="ft-node-title">End</div>
              <div class="ft-node-detail">{{ data.result ?? 'pass' }}</div>
            </div>
          </template>

          <template #node-wait="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" />
          </template>
          <template #node-wait_until="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" />
          </template>
          <template #node-capture="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" />
          </template>
          <template #node-verify="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" />
          </template>
          <template #node-compare="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" />
          </template>
        </VueFlow>
      </main>

      <FunctionalTestProperties :node="selectedNode" :meta="meta" />
    </div>

    <FunctionalTestRunDialog
      v-if="savedTest"
      v-model:open="runDialogOpen"
      :test="savedTest"
      :meta="meta"
    />
  </div>
</template>

<style scoped>
.ft-builder {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.ft-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border, #d9d9d9);
  background: var(--surface, #fff);
}

.ft-issues {
  padding: 8px 16px;
  background: #fffbe6;
  border-bottom: 1px solid #ffe58f;
  font-size: 12px;
  max-height: 140px;
  overflow-y: auto;
}

.ft-issues-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.ft-issue {
  padding: 2px 0;
  color: #874d00;
}

.ft-issue--clickable {
  cursor: pointer;
}

.ft-issue--clickable:hover {
  text-decoration: underline;
}

.ft-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr) 300px;
}

.ft-canvas {
  min-width: 0;
  background: var(--surface-secondary, #fafafa);
}

.ft-node--endpoint {
  position: relative;
  min-width: 120px;
  padding: 10px 14px;
  text-align: center;
  background: var(--surface, #fff);
  border: 1px solid var(--border, #d9d9d9);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgb(0 0 0 / 6%);
}

.ft-node--selected {
  outline: 3px dashed #eb2f96;
  outline-offset: 3px;
}

.ft-node-title {
  font-weight: 600;
  font-size: 12px;
}

.ft-node-detail {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-secondary, #666);
}
</style>
