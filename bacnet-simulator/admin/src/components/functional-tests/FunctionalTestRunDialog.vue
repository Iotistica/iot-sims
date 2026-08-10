<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { VueFlow, type Edge, type Node } from '@vue-flow/core'
import { Background } from '@vue-flow/background'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'

import { api } from '../../api'
import type { Device, FunctionalTest, FunctionalTestResolveResponse, FunctionalTestRun, Meta } from '../../types'
import { hydrateFunctionalTest } from '../../functionalTestSerializer'
import FunctionalTestNode from './FunctionalTestNode.vue'

const props = defineProps<{
  open: boolean
  test: FunctionalTest
  meta: Meta
}>()

const emit = defineEmits<{
  'update:open': [v: boolean]
}>()

const STATUS_LABEL: Record<string, string> = {
  resolved: 'Resolved', missing: 'Missing', ambiguous: 'Ambiguous', inconsistent: 'Inconsistent',
}
const STATUS_ICON: Record<string, string> = {
  resolved: '✓', missing: '✕', ambiguous: '!', inconsistent: '✕',
}

const step = ref<'setup' | 'running'>('setup')
const devices = ref<Device[]>([])
const loadingDevices = ref(false)
const selectedDeviceId = ref<number | null>(null)
const resolution = ref<FunctionalTestResolveResponse | null>(null)
const resolving = ref(false)
const starting = ref(false)
const cancelling = ref(false)
const run = ref<FunctionalTestRun | null>(null)
let pollHandle: ReturnType<typeof setTimeout> | null = null

const { nodes, edges } = hydrateFunctionalTest(props.test.definition) as { nodes: Node[]; edges: Edge[] }

const compatibleDevices = computed(() =>
  devices.value.filter(d => d.equipment_type === props.test.equipment_type)
)

function deviceLabel(device: Device): string {
  return device.source_type === 'external-bacnet'
    ? `External BACnet · ${device.external_host ?? 'unknown host'}`
    : 'Simulated'
}

const allResolved = computed(() => {
  if (!resolution.value) return false
  return Object.values(resolution.value.points).every(p => p.status === 'resolved')
})

const isTerminal = computed(() => {
  const state = run.value?.state
  return state === 'passed' || state === 'failed' || state === 'inconclusive' || state === 'cancelled' || state === 'error'
})

type NodeRunStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped'

const nodeStatus = computed<Record<string, NodeRunStatus>>(() => {
  const map: Record<string, NodeRunStatus> = {}
  if (!run.value) return map

  for (const detail of run.value.details) {
    map[detail.node_id] = detail.outcome === 'fail' ? 'failed' : 'passed'
  }

  if (run.value.state === 'running') {
    if (run.value.details.length > 0) {
      const last = run.value.details[run.value.details.length - 1]
      const handle = last.outcome === 'pass' || last.outcome === 'fail' ? last.outcome : null
      const nextEdge = (edges as any[]).find(e => e.source === last.node_id && (e.sourceHandle ?? null) === handle)
      if (nextEdge) map[nextEdge.target] = 'running'
    } else {
      const startNode = (nodes as any[]).find(n => n.type === 'start')
      if (startNode) map[startNode.id] = 'running'
    }
  }

  return map
})

const durationSeconds = computed(() => {
  if (!run.value?.started_at || !run.value?.finished_at) return null
  const started = new Date(run.value.started_at).getTime()
  const finished = new Date(run.value.finished_at).getTime()
  return Math.max(0, Math.round((finished - started) / 1000))
})

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

async function loadDevices() {
  loadingDevices.value = true
  try {
    devices.value = await api.devices.list()
  } catch {
    message.error('Failed to load devices')
  } finally {
    loadingDevices.value = false
  }
}

async function onSelectTarget(deviceId: number) {
  selectedDeviceId.value = deviceId
  resolution.value = null
  resolving.value = true
  try {
    resolution.value = await api.functionalTests.resolve(props.test.id, deviceId)
  } catch (e: any) {
    message.error(e?.message || 'Failed to resolve points against this target')
  } finally {
    resolving.value = false
  }
}

async function startRun() {
  if (!selectedDeviceId.value) return
  starting.value = true
  try {
    run.value = await api.functionalTests.createRun(props.test.id, selectedDeviceId.value)
    step.value = 'running'
    schedulePoll()
  } catch (e: any) {
    message.error(e?.message || 'Failed to start run')
  } finally {
    starting.value = false
  }
}

function schedulePoll() {
  if (pollHandle) clearTimeout(pollHandle)
  pollHandle = setTimeout(async () => {
    if (!run.value) return
    try {
      run.value = await api.functionalTestRuns.get(run.value.id)
    } catch {
      // transient poll failure -- try again on the next tick
    }
    if (!isTerminal.value) schedulePoll()
  }, 1500)
}

async function cancelRun() {
  if (!run.value) return
  cancelling.value = true
  try {
    run.value = await api.functionalTestRuns.cancel(run.value.id)
  } catch (e: any) {
    message.error(e?.message || 'Failed to cancel run')
  } finally {
    cancelling.value = false
  }
}

function close() {
  if (pollHandle) clearTimeout(pollHandle)
  pollHandle = null
  emit('update:open', false)
}

watch(() => props.open, (isOpen) => {
  if (isOpen) {
    step.value = 'setup'
    selectedDeviceId.value = null
    resolution.value = null
    run.value = null
    loadDevices()
  } else if (pollHandle) {
    clearTimeout(pollHandle)
    pollHandle = null
  }
})
</script>

<template>
  <a-modal
    :open="open"
    title="Run Functional Test"
    :width="720"
    :footer="null"
    @update:open="(v: boolean) => (v ? emit('update:open', true) : close())"
  >
    <div style="margin-bottom:16px">
      <div style="font-weight:600;font-size:15px">{{ test.name }}</div>
    </div>

    <template v-if="step === 'setup'">
      <a-form-item label="Target">
        <a-select
          v-model:value="selectedDeviceId"
          :loading="loadingDevices"
          placeholder="Select a compatible device"
          style="width:100%"
          @change="(v: any) => onSelectTarget(v)"
        >
          <a-select-option v-for="d in compatibleDevices" :key="d.id" :value="d.id">
            {{ d.name }} — {{ deviceLabel(d) }}
          </a-select-option>
        </a-select>
        <div v-if="!loadingDevices && compatibleDevices.length === 0" style="color:var(--text-secondary,#888);font-size:12px;margin-top:6px">
          No devices with equipment type "{{ test.equipment_type }}" found.
        </div>
      </a-form-item>

      <div v-if="resolving" style="padding:12px 0"><a-spin size="small" /> Resolving points...</div>

      <div v-else-if="resolution" style="margin-top:8px">
        <div style="font-weight:600;margin-bottom:8px">Required Points</div>
        <div
          v-for="(pointResolution, pointType) in resolution.points"
          :key="pointType"
          style="padding:6px 0;border-bottom:1px solid var(--border,#f0f0f0)"
        >
          <span :style="{ color: pointResolution.status === 'resolved' ? '#52c41a' : '#ff4d4f', fontWeight: 600, marginRight: '8px' }">
            {{ STATUS_ICON[pointResolution.status] }}
          </span>
          <span>{{ meta.point_types.find(p => p.value === pointType)?.label ?? pointType }}</span>
          <div style="margin-left:20px;font-size:12px;color:var(--text-secondary,#888)">
            <template v-if="pointResolution.status === 'resolved'">
              &rarr; {{ pointResolution.object?.name }}
            </template>
            <template v-else-if="pointResolution.status === 'ambiguous'">
              {{ STATUS_LABEL[pointResolution.status] }} — matches: {{ pointResolution.candidates.join(', ') }}
            </template>
            <template v-else>
              {{ pointResolution.message || STATUS_LABEL[pointResolution.status] }}
            </template>
          </div>
        </div>
      </div>

      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:20px">
        <a-button @click="close">Cancel</a-button>
        <a-button type="primary" :disabled="!allResolved" :loading="starting" @click="startRun">
          Run Test
        </a-button>
      </div>
    </template>

    <template v-else>
      <div style="margin-bottom:12px;font-size:13px;color:var(--text-secondary,#666)">
        Target: {{ compatibleDevices.find(d => d.id === selectedDeviceId)?.name }}
        &middot;
        Mode: {{ run?.execution_mode === 'external' ? 'External BACnet · Read Only' : 'Simulation' }}
      </div>

      <div style="height:360px;border:1px solid var(--border,#d9d9d9);border-radius:8px;overflow:hidden;margin-bottom:16px">
        <VueFlow
          :nodes="nodes"
          :edges="edges"
          fit-view-on-init
          :nodes-draggable="false"
          :nodes-connectable="false"
          :elements-selectable="false"
        >
          <Background :gap="16" />
          <template v-for="t in ['wait', 'wait_until', 'capture', 'verify', 'compare']" :key="t" #[`node-${t}`]="nodeProps">
            <FunctionalTestNode v-bind="nodeProps" :meta="meta" :status="nodeStatus[nodeProps.id]" />
          </template>
          <template #node-start="{ id }">
            <div class="ft-run-endpoint" :class="{ 'ft-run-endpoint--running': nodeStatus[id] === 'running' }">Start</div>
          </template>
          <template #node-end="{ id, data }">
            <div class="ft-run-endpoint" :class="{ 'ft-run-endpoint--running': nodeStatus[id] === 'running' }">
              End ({{ data.result ?? 'pass' }})
            </div>
          </template>
        </VueFlow>
      </div>

      <div v-if="isTerminal" style="margin-bottom:16px">
        <div style="font-weight:600;font-size:14px;margin-bottom:6px">
          Result:
          <span :style="{ color: run?.state === 'passed' ? '#52c41a' : (run?.state === 'failed' || run?.state === 'error') ? '#ff4d4f' : '#faad14' }">
            {{ (run?.state ?? '').toUpperCase() }}
          </span>
        </div>
        <div v-if="durationSeconds !== null" style="font-size:12px;color:var(--text-secondary,#888)">
          Duration: {{ formatDuration(durationSeconds) }}
        </div>
        <div v-if="run?.result_message" style="font-size:12px;margin-top:4px">{{ run.result_message }}</div>
        <div v-if="run?.error" style="font-size:12px;margin-top:4px;color:#ff4d4f">{{ run.error }}</div>

        <div v-if="run?.details?.length" style="margin-top:12px">
          <div v-for="d in run.details" :key="d.node_id" style="padding:4px 0;font-size:12px;border-bottom:1px solid var(--border,#f0f0f0)">
            <strong>{{ d.type.toUpperCase() }}</strong> — {{ d.message }}
          </div>
        </div>
      </div>

      <div style="display:flex;justify-content:flex-end;gap:8px">
        <a-button v-if="!isTerminal" danger :loading="cancelling" @click="cancelRun">Cancel Run</a-button>
        <a-button v-if="isTerminal" type="primary" @click="close">Close</a-button>
      </div>
    </template>
  </a-modal>
</template>

<style scoped>
.ft-run-endpoint {
  min-width: 100px;
  padding: 8px 12px;
  text-align: center;
  font-size: 12px;
  font-weight: 600;
  background: var(--surface, #fff);
  border: 1px solid var(--border, #d9d9d9);
  border-radius: 8px;
}

.ft-run-endpoint--running {
  border-left: 3px solid #1677ff;
}
</style>
