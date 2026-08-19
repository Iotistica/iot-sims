<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { EditOutlined, PlusOutlined } from '@ant-design/icons-vue'
import {
  VueFlow,
  Handle,
  Position,
  useVueFlow,
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
import type { FunctionalTest, FunctionalTestIssue, FunctionalTestNodeType, Meta, PointRow } from '../../types'
import { hydrateFunctionalTest, serializeFunctionalTest } from '../../functionalTestSerializer'
import { validateFunctionalTest } from '../../functionalTestValidation'
import { buildPointLookup } from '../../pointLookup'
import FunctionalTestPalette from './FunctionalTestPalette.vue'
import FunctionalTestProperties from './FunctionalTestProperties.vue'
import FunctionalTestNode from './FunctionalTestNode.vue'
import FunctionalTestRunPanel from './FunctionalTestRunPanel.vue'
import FunctionalTestJsonPanel from './FunctionalTestJsonPanel.vue'

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
  wait_until: () => ({ point: null, operator: 'eq', value: { kind: 'constant', value: '' }, timeout_seconds: 300 }),
  capture: () => ({ point: null, variable: '' }),
  verify: () => ({ left: { kind: 'point', point: null }, operator: 'eq', right: { kind: 'constant', value: '' } }),
  compare: () => ({ left: { kind: 'point', point: null }, operator: 'eq', right: { kind: 'constant', value: '' } }),
  set: () => ({ point: null, value: '' }),
  end: () => ({ result: 'pass' }),
}

// Fetched once per builder session (not per PointPicker instance) and
// passed down to both the properties panel and every canvas node's summary
// rendering, so opening the builder does one /points request, not many.
const points = ref<PointRow[]>([])
const pointLookup = computed(() => buildPointLookup(points.value))
onMounted(async () => {
  try {
    points.value = await api.points.list()
  } catch (e: any) {
    message.error(e?.message || 'Failed to load points')
  }
})

const testId = ref<number | null>(props.test?.id ?? null)
const name = ref(props.test?.name ?? 'New Functional Test')
const description = ref(props.test?.description ?? '')
const equipmentType = ref(props.test?.equipment_type ?? '')

// Run always executes the last SAVED definition, never the canvas's
// current in-progress edits (trust boundary: edit -> validate -> save ->
// run saved test) -- kept separate from the editable name/description/
// equipmentType/nodes/edges above.
const savedTest = ref<FunctionalTest | null>(props.test ?? null)
const showRunPanel = ref(false)
const showJsonPanel = ref(false)

function startRunPanel() {
  // Validation issues are surfaced INSIDE the run panel now (right before
  // the readiness-check/Run Test step), not a separate banner -- always
  // open it and let it decide what to show.
  runValidation()
  selectedNodeId.value = null
  showJsonPanel.value = false
  showRunPanel.value = true
}

function selectIssue(issue: FunctionalTestIssue) {
  if (!issue.nodeId) return
  showRunPanel.value = false
  showJsonPanel.value = false
  selectedNodeId.value = issue.nodeId
}

function toggleJsonPanel() {
  selectedNodeId.value = null
  showRunPanel.value = false
  showJsonPanel.value = true
}

function blankFlow(): { nodes: Node[]; edges: Edge[] } {
  // Start is implicit -- an empty canvas, not a lone Start block. The
  // first block the user adds automatically becomes the entry point (see
  // functionalTestValidation.ts/functionalTestSerializer.ts).
  return { nodes: [], edges: [] }
}

function initialFlow(): { nodes: Node[]; edges: Edge[] } {
  return props.test ? hydrateFunctionalTest(props.test.definition) : blankFlow()
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
  showRunPanel.value = false
  showJsonPanel.value = false
  selectedNodeId.value = node.id
}

function addNode(type: FunctionalTestNodeType, position?: { x: number; y: number }) {
  const id = `${type}-${crypto.randomUUID()}`
  nodes.value.push({
    id,
    type,
    position: position ?? { x: 150 + Math.random() * 250, y: 100 + nodes.value.length * 60 },
    data: DEFAULT_PARAMS[type](),
  })
  selectedNodeId.value = id
}

const { screenToFlowCoordinate } = useVueFlow()

function onCanvasDragOver(e: DragEvent) {
  if (!e.dataTransfer?.types.includes('application/vueflow')) return
  e.preventDefault()
  e.dataTransfer.dropEffect = 'move'
}

function onCanvasDrop(e: DragEvent) {
  const type = e.dataTransfer?.getData('application/vueflow') as FunctionalTestNodeType | ''
  if (!type) return
  e.preventDefault()
  const pos = screenToFlowCoordinate({ x: e.clientX, y: e.clientY })
  // A node's position is its top-left corner, but the cursor drops from
  // roughly the middle of the dragged block -- without this offset the
  // block's corner lands exactly on the cursor, so the whole ~210x70px
  // block visually appears down-and-right of where it was actually
  // dropped. Pull it back by roughly half a block's size so it's centered
  // under the cursor instead.
  addNode(type, { x: pos.x - 100, y: pos.y - 30 })
}

function removeSelectedNode() {
  if (!selectedNodeId.value) return
  const id = selectedNodeId.value
  nodes.value = nodes.value.filter((n: Node) => n.id !== id)
  edges.value = edges.value.filter((e: Edge) => e.source !== id && e.target !== id)
  selectedNodeId.value = null
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable
}

// Delete key removes the selected block directly, no confirmation --
// ignored while typing in a text field/select so it doesn't hijack normal
// text editing (e.g. deleting characters in the description drawer).
function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Delete' || isEditableTarget(e.target)) return
  removeSelectedNode()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

function runValidation(): FunctionalTestIssue[] {
  const result = validateFunctionalTest(nodes.value as Node[], edges.value as Edge[], props.meta, equipmentType.value)
  issues.value = result
  return result
}

// New Test / Edit share one drawer: New starts the draft blank and, on
// Save, also resets the canvas/testId/savedTest (folding in what newTest()
// used to do in place); Edit seeds the draft from the current values and,
// on Save, only touches name/description/equipmentType -- the canvas is
// left untouched either way until Save/Close decide what to commit.
const metaDrawerOpen = ref(false)
const metaDrawerMode = ref<'new' | 'edit'>('new')
const draftName = ref('')
const draftDescription = ref('')
const draftEquipmentType = ref('')

function openNewTestDrawer() {
  metaDrawerMode.value = 'new'
  draftName.value = 'New Functional Test'
  draftDescription.value = ''
  draftEquipmentType.value = ''
  metaDrawerOpen.value = true
}

function openEditDrawer() {
  metaDrawerMode.value = 'edit'
  draftName.value = name.value
  draftDescription.value = description.value
  draftEquipmentType.value = equipmentType.value
  metaDrawerOpen.value = true
}

function saveMetaDrawer() {
  if (metaDrawerMode.value === 'new') {
    const blank = blankFlow()
    testId.value = null
    savedTest.value = null
    nodes.value = blank.nodes
    edges.value = blank.edges
    selectedNodeId.value = null
    showRunPanel.value = false
    showJsonPanel.value = false
    issues.value = []
  }
  name.value = draftName.value
  description.value = draftDescription.value
  equipmentType.value = draftEquipmentType.value
  metaDrawerOpen.value = false
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

      <div class="ft-test-title">{{ name }}</div>

      <div style="flex:1" />

      <a-button @click="openNewTestDrawer"><PlusOutlined /> New Test</a-button>
      <a-button @click="openEditDrawer"><EditOutlined /> Edit</a-button>
      <a-tooltip :title="savedTest ? 'View the last saved definition as JSON' : 'Save the test before viewing its JSON'">
        <a-button :disabled="!savedTest" @click="toggleJsonPanel">View JSON</a-button>
      </a-tooltip>
      <a-tooltip :title="savedTest ? 'Run the last saved version of this test' : 'Save the test before running it'">
        <a-button :disabled="!savedTest" @click="startRunPanel">Run</a-button>
      </a-tooltip>
      <a-divider type="vertical" style="height:24px;margin:0 2px" />
      <a-button type="primary" :loading="saving" style="width:80px" @click="save">Save</a-button>
    </div>

    <div class="ft-body">
      <FunctionalTestPalette @add="addNode" />

      <main class="ft-canvas" @dragover="onCanvasDragOver" @drop="onCanvasDrop">
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
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :point-lookup="pointLookup" />
          </template>
          <template #node-wait_until="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :point-lookup="pointLookup" />
          </template>
          <template #node-capture="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :point-lookup="pointLookup" />
          </template>
          <template #node-verify="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :point-lookup="pointLookup" />
          </template>
          <template #node-compare="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :point-lookup="pointLookup" />
          </template>
          <template #node-set="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :point-lookup="pointLookup" />
          </template>
        </VueFlow>
      </main>

      <FunctionalTestRunPanel
        v-if="showRunPanel && savedTest"
        :test="savedTest"
        :issues="issues"
        @close="showRunPanel = false"
        @select-issue="selectIssue"
      />
      <FunctionalTestJsonPanel
        v-else-if="showJsonPanel && savedTest"
        :test="savedTest"
        @close="showJsonPanel = false"
      />
      <FunctionalTestProperties v-else :node="selectedNode" :meta="meta" :points="points" />
    </div>

    <a-drawer
      :open="metaDrawerOpen"
      :title="metaDrawerMode === 'new' ? 'New Test' : 'Edit Test'"
      placement="right"
      width="360"
      @close="metaDrawerOpen = false"
    >
      <a-form layout="vertical">
        <a-form-item label="Name">
          <a-input v-model:value="draftName" placeholder="Test name" />
        </a-form-item>
        <a-form-item label="Category">
          <a-select
            v-model:value="draftEquipmentType"
            show-search
            allow-clear
            placeholder="Applies to equipment type"
            :options="meta.equipment_types"
            style="width:100%"
          />
        </a-form-item>
        <a-form-item label="Description">
          <a-textarea v-model:value="draftDescription" placeholder="Description (optional)" :rows="3" />
        </a-form-item>
      </a-form>

      <template #footer>
        <div style="display:flex;justify-content:flex-end;gap:8px">
          <a-button @click="metaDrawerOpen = false">Close</a-button>
          <a-button type="primary" :disabled="!draftName.trim()" @click="saveMetaDrawer">Save</a-button>
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.ft-builder {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.ft-test-title {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ft-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border, #d9d9d9);
  background: var(--surface, #fff);
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
  background: var(--surface-alt, #f5f5f5);
  border: 1px solid #faad14;
  border-radius: 8px;
  box-shadow: var(--card-shadow, 0 2px 8px rgb(0 0 0 / 6%));
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
