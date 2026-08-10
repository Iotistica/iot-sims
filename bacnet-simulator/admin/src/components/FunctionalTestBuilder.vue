<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  VueFlow,
  Handle,
  Position,
  addEdge,
  type Connection,
  type Edge,
  type Node,
} from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'

type TestNodeType =
  | 'start'
  | 'wait'
  | 'wait_until'
  | 'capture'
  | 'verify'
  | 'end'

interface TestNodeData {
  label: string

  pointType?: string
  operator?: 'eq' | 'gt' | 'gte' | 'lt' | 'lte'
  value?: string | number | boolean

  seconds?: number

  variable?: string
  referenceVariable?: string
  offset?: number
}

const pointTypes = [
  { value: 'Boiler_Status', label: 'Boiler Status' },
  {
    value: 'Heating_Water_Supply_Temperature_Sensor',
    label: 'HW Supply Temperature',
  },
  {
    value: 'Heating_Water_Return_Temperature_Sensor',
    label: 'HW Return Temperature',
  },
  {
    value: 'Differential_Pressure_Sensor',
    label: 'Differential Pressure',
  },
]

const nodes = ref<Node<TestNodeData>[]>([
  {
    id: 'start',
    type: 'start',
    position: { x: 260, y: 40 },
    data: {
      label: 'Start',
    },
  },
  {
    id: 'wait-boiler',
    type: 'wait_until',
    position: { x: 220, y: 150 },
    data: {
      label: 'Wait Until',
      pointType: 'Boiler_Status',
      operator: 'eq',
      value: true,
    },
  },
  {
    id: 'capture-supply',
    type: 'capture',
    position: { x: 220, y: 300 },
    data: {
      label: 'Capture',
      pointType: 'Heating_Water_Supply_Temperature_Sensor',
      variable: 't1',
    },
  },
  {
    id: 'wait-5min',
    type: 'wait',
    position: { x: 240, y: 450 },
    data: {
      label: 'Wait',
      seconds: 300,
    },
  },
  {
    id: 'verify-rise',
    type: 'verify',
    position: { x: 210, y: 580 },
    data: {
      label: 'Verify',
      pointType: 'Heating_Water_Supply_Temperature_Sensor',
      operator: 'gt',
      referenceVariable: 't1',
      offset: 2,
    },
  },
  {
    id: 'end',
    type: 'end',
    position: { x: 260, y: 740 },
    data: {
      label: 'End',
    },
  },
])

const edges = ref<Edge[]>([
  { id: 'e1', source: 'start', target: 'wait-boiler' },
  { id: 'e2', source: 'wait-boiler', target: 'capture-supply' },
  { id: 'e3', source: 'capture-supply', target: 'wait-5min' },
  { id: 'e4', source: 'wait-5min', target: 'verify-rise' },
  { id: 'e5', source: 'verify-rise', target: 'end' },
])

const selectedNodeId = ref<string | null>(null)

const selectedNode = computed(() =>
  nodes.value.find(node => node.id === selectedNodeId.value) ?? null
)

function onConnect(connection: Connection) {
  edges.value = addEdge(connection, edges.value)
}

function selectNode(node: Node<TestNodeData>) {
  selectedNodeId.value = node.id
}

function addNode(type: TestNodeType) {
  const id = `${type}-${crypto.randomUUID()}`

  nodes.value.push({
    id,
    type,
    position: {
      x: 150 + Math.random() * 250,
      y: 100 + nodes.value.length * 60,
    },
    data: {
      label: nodeLabel(type),
      ...(type === 'wait' ? { seconds: 30 } : {}),
    },
  })
}

function nodeLabel(type: TestNodeType) {
  switch (type) {
    case 'wait_until':
      return 'Wait Until'
    case 'wait':
      return 'Wait'
    case 'capture':
      return 'Capture'
    case 'verify':
      return 'Verify'
    case 'start':
      return 'Start'
    case 'end':
      return 'End'
  }
}

function removeSelectedNode() {
  if (!selectedNodeId.value) return

  const id = selectedNodeId.value

  nodes.value = nodes.value.filter(node => node.id !== id)

  edges.value = edges.value.filter(
    edge => edge.source !== id && edge.target !== id
  )

  selectedNodeId.value = null
}

function exportDefinition() {
  const definition = {
    version: 1,

    nodes: nodes.value.map(node => ({
      id: node.id,
      type: node.type,
      data: node.data,
    })),

    edges: edges.value.map(edge => ({
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle ?? null,
      targetHandle: edge.targetHandle ?? null,
    })),

    layout: Object.fromEntries(
      nodes.value.map(node => [
        node.id,
        {
          x: node.position.x,
          y: node.position.y,
        },
      ])
    ),
  }

  console.log(JSON.stringify(definition, null, 2))
}
</script>

<template>
  <div class="test-builder">
    <!-- Block palette -->
    <aside class="palette">
      <div class="panel-title">Blocks</div>

      <button @click="addNode('wait')">
        Wait
      </button>

      <button @click="addNode('wait_until')">
        Wait Until
      </button>

      <button @click="addNode('capture')">
        Capture
      </button>

      <button @click="addNode('verify')">
        Verify
      </button>

      <div class="palette-divider" />

      <button
        class="danger"
        :disabled="!selectedNode"
        @click="removeSelectedNode"
      >
        Delete selected
      </button>
    </aside>

    <!-- Canvas -->
    <main class="canvas">
      <VueFlow
        v-model:nodes="nodes"
        v-model:edges="edges"
        fit-view-on-init
        :min-zoom="0.4"
        :max-zoom="1.8"
        @connect="onConnect"
        @node-click="({ node }) => selectNode(node)"
      >
        <Background :gap="16" />

        <Controls />

        <!-- START -->
        <template #node-start="{ data }">
          <div class="flow-node flow-node--start">
            <div class="node-title">
              {{ data.label }}
            </div>

            <Handle
              type="source"
              :position="Position.Bottom"
            />
          </div>
        </template>

        <!-- WAIT -->
        <template #node-wait="{ data }">
          <div class="flow-node">
            <Handle
              type="target"
              :position="Position.Top"
            />

            <div class="node-kicker">
              WAIT
            </div>

            <div class="node-main">
              {{ data.seconds ?? 0 }} seconds
            </div>

            <Handle
              type="source"
              :position="Position.Bottom"
            />
          </div>
        </template>

        <!-- WAIT UNTIL -->
        <template #node-wait_until="{ data }">
          <div class="flow-node">
            <Handle
              type="target"
              :position="Position.Top"
            />

            <div class="node-kicker">
              WAIT UNTIL
            </div>

            <div class="node-main">
              {{ data.pointType || 'Select point' }}
            </div>

            <div class="node-detail">
              {{ data.operator || '?' }}
              {{ data.value ?? '?' }}
            </div>

            <Handle
              type="source"
              :position="Position.Bottom"
            />
          </div>
        </template>

        <!-- CAPTURE -->
        <template #node-capture="{ data }">
          <div class="flow-node">
            <Handle
              type="target"
              :position="Position.Top"
            />

            <div class="node-kicker">
              CAPTURE
            </div>

            <div class="node-main">
              {{ data.pointType || 'Select point' }}
            </div>

            <div class="node-detail">
              Save as {{ data.variable || '?' }}
            </div>

            <Handle
              type="source"
              :position="Position.Bottom"
            />
          </div>
        </template>

        <!-- VERIFY -->
        <template #node-verify="{ data }">
          <div class="flow-node flow-node--verify">
            <Handle
              type="target"
              :position="Position.Top"
            />

            <div class="node-kicker">
              VERIFY
            </div>

            <div class="node-main">
              {{ data.pointType || 'Select point' }}
            </div>

            <div class="node-detail">
              {{ data.operator || '?' }}
              {{ data.referenceVariable || data.value || '?' }}
              <template v-if="data.offset">
                + {{ data.offset }}
              </template>
            </div>

            <Handle
              id="pass"
              type="source"
              :position="Position.Bottom"
              style="left:35%"
            />

            <Handle
              id="fail"
              type="source"
              :position="Position.Bottom"
              style="left:65%"
            />

            <div class="branch-labels">
              <span>PASS</span>
              <span>FAIL</span>
            </div>
          </div>
        </template>

        <!-- END -->
        <template #node-end="{ data }">
          <div class="flow-node flow-node--end">
            <Handle
              type="target"
              :position="Position.Top"
            />

            <div class="node-title">
              {{ data.label }}
            </div>
          </div>
        </template>
      </VueFlow>
    </main>

    <!-- Properties -->
    <aside class="properties">
      <template v-if="selectedNode">
        <div class="panel-title">
          {{ selectedNode.data.label }}
        </div>

        <template
          v-if="
            selectedNode.type === 'wait_until' ||
            selectedNode.type === 'capture' ||
            selectedNode.type === 'verify'
          "
        >
          <label>
            Point
            <select v-model="selectedNode.data.pointType">
              <option value="">
                Select...
              </option>

              <option
                v-for="point in pointTypes"
                :key="point.value"
                :value="point.value"
              >
                {{ point.label }}
              </option>
            </select>
          </label>
        </template>

        <template v-if="selectedNode.type === 'wait'">
          <label>
            Seconds
            <input
              v-model.number="selectedNode.data.seconds"
              type="number"
              min="0"
            />
          </label>
        </template>

        <template v-if="selectedNode.type === 'wait_until'">
          <label>
            Operator
            <select v-model="selectedNode.data.operator">
              <option value="eq">Equals</option>
              <option value="gt">Greater than</option>
              <option value="gte">Greater than or equal</option>
              <option value="lt">Less than</option>
              <option value="lte">Less than or equal</option>
            </select>
          </label>

          <label>
            Value
            <input v-model="selectedNode.data.value" />
          </label>
        </template>

        <template v-if="selectedNode.type === 'capture'">
          <label>
            Variable
            <input
              v-model="selectedNode.data.variable"
              placeholder="t1"
            />
          </label>
        </template>

        <template v-if="selectedNode.type === 'verify'">
          <label>
            Operator
            <select v-model="selectedNode.data.operator">
              <option value="eq">Equals</option>
              <option value="gt">Greater than</option>
              <option value="gte">Greater than or equal</option>
              <option value="lt">Less than</option>
              <option value="lte">Less than or equal</option>
            </select>
          </label>

          <label>
            Reference variable
            <input
              v-model="selectedNode.data.referenceVariable"
              placeholder="t1"
            />
          </label>

          <label>
            Offset
            <input
              v-model.number="selectedNode.data.offset"
              type="number"
            />
          </label>
        </template>
      </template>

      <div
        v-else
        class="empty-properties"
      >
        Select a block to configure it.
      </div>

      <button
        class="export-button"
        @click="exportDefinition"
      >
        Export JSON
      </button>
    </aside>
  </div>
</template>

<style scoped>
.test-builder {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr) 280px;
  height: 720px;
  border: 1px solid var(--border, #d9d9d9);
  border-radius: 8px;
  overflow: hidden;
}

.palette,
.properties {
  padding: 14px;
  background: var(--surface, #fff);
}

.palette {
  border-right: 1px solid var(--border, #d9d9d9);
}

.properties {
  border-left: 1px solid var(--border, #d9d9d9);
}

.panel-title {
  margin-bottom: 12px;
  font-weight: 600;
}

.palette button,
.export-button {
  width: 100%;
  margin-bottom: 8px;
  padding: 7px 10px;
}

.palette-divider {
  margin: 14px 0;
  border-top: 1px solid var(--border, #d9d9d9);
}

.canvas {
  min-width: 0;
  background: var(--surface-secondary, #fafafa);
}

.flow-node {
  position: relative;
  min-width: 210px;
  padding: 12px 14px;
  background: var(--surface, #fff);
  border: 1px solid var(--border, #d9d9d9);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgb(0 0 0 / 6%);
}

.flow-node--start,
.flow-node--end {
  min-width: 120px;
  text-align: center;
}

.flow-node--verify {
  padding-bottom: 22px;
}

.node-kicker {
  margin-bottom: 5px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: #1677ff;
}

.node-title,
.node-main {
  font-weight: 600;
}

.node-detail {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-secondary, #666);
}

.branch-labels {
  display: flex;
  justify-content: space-around;
  margin-top: 13px;
  font-size: 9px;
  color: var(--text-secondary, #888);
}

.properties label {
  display: block;
  margin-bottom: 12px;
  font-size: 12px;
}

.properties input,
.properties select {
  display: block;
  width: 100%;
  box-sizing: border-box;
  margin-top: 5px;
  padding: 6px 8px;
}

.empty-properties {
  color: var(--text-secondary, #888);
  font-size: 13px;
}

.export-button {
  margin-top: 20px;
}

.danger {
  color: #ff4d4f;
}
</style>