<script setup lang="ts">
/** Inline replacement for the old FunctionalTestRunDialog modal -- renders
 * in the same right-hand slot FunctionalTestProperties normally occupies
 * (see FunctionalTestBuilder.vue), pure text, no duplicate mini-canvas.
 * Same readiness-check -> start -> poll -> terminal flow as before, just
 * without the modal chrome or the read-only Vue Flow re-render. */
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { message } from 'ant-design-vue'
import { CloseOutlined } from '@ant-design/icons-vue'
import { api } from '../../api'
import type { FunctionalTest, FunctionalTestIssue, FunctionalTestResolveResponse, FunctionalTestRun } from '../../types'

const props = defineProps<{
  test: FunctionalTest
  /** Graph-quality issues from the CURRENT (possibly unsaved) canvas --
   * all validation is surfaced here now, not a separate banner in the
   * builder, so Run is blocked and shows exactly what to fix instead of
   * silently falling through to a readiness check that can't succeed. */
  issues: FunctionalTestIssue[]
}>()

const emit = defineEmits<{
  close: []
  'select-issue': [FunctionalTestIssue]
}>()

const STATUS_LABEL: Record<string, string> = {
  ok: 'Ready', missing_device: 'Device missing', missing_object: 'Point missing', not_simulated: 'Not simulated',
}

const step = ref<'setup' | 'running'>('setup')
const readiness = ref<FunctionalTestResolveResponse | null>(null)
const checkingReadiness = ref(false)
const starting = ref(false)
const cancelling = ref(false)
const run = ref<FunctionalTestRun | null>(null)
let pollHandle: ReturnType<typeof setTimeout> | null = null

const allReady = computed(() => {
  if (!readiness.value) return false
  return readiness.value.points.every(p => p.status === 'ok')
})

const isTerminal = computed(() => {
  const state = run.value?.state
  return state === 'passed' || state === 'failed' || state === 'inconclusive' || state === 'cancelled' || state === 'error'
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

// Start/End are implicit in the editor (see functionalTestSerializer.ts) --
// hide their run-detail entries here too rather than surfacing internal
// lifecycle plumbing the user never placed by hand.
const visibleDetails = computed(() => (run.value?.details ?? []).filter(d => d.type !== 'start' && d.type !== 'end'))

async function checkReadiness() {
  checkingReadiness.value = true
  readiness.value = null
  try {
    readiness.value = await api.functionalTests.resolve(props.test.id)
  } catch (e: any) {
    message.error(e?.message || 'Failed to check point readiness')
  } finally {
    checkingReadiness.value = false
  }
}

async function startRun() {
  starting.value = true
  try {
    run.value = await api.functionalTests.createRun(props.test.id)
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

// No point checking readiness (a network round-trip) when the graph
// itself isn't valid yet -- Run can't succeed either way.
onMounted(() => {
  if (props.issues.length === 0) checkReadiness()
})
onUnmounted(() => {
  if (pollHandle) clearTimeout(pollHandle)
  pollHandle = null
})
</script>

<template>
  <aside class="ft-run-panel">
    <div class="ft-run-panel-header">
      <div class="ft-panel-title" style="margin:0">Run: {{ test.name }}</div>
      <a-button type="text" size="small" @click="emit('close')"><CloseOutlined /></a-button>
    </div>

    <div class="ft-run-panel-body">
      <template v-if="issues.length > 0">
        <div class="ft-run-section-title" style="color:#faad14">
          {{ issues.length }} issue{{ issues.length === 1 ? '' : 's' }} to fix before running
        </div>
        <div
          v-for="(issue, idx) in issues"
          :key="idx"
          class="ft-run-issue-row"
          :class="{ 'ft-run-issue-row--clickable': !!issue.nodeId }"
          @click="issue.nodeId && emit('select-issue', issue)"
        >
          {{ issue.message }}
        </div>
      </template>

      <template v-else-if="step === 'setup'">
        <div v-if="checkingReadiness" class="ft-run-muted"><a-spin size="small" /> Checking points...</div>

        <template v-else-if="readiness">
          <div class="ft-run-section-title">Required Points</div>
          <div v-if="readiness.points.length === 0" class="ft-run-muted">
            This test doesn't reference any points.
          </div>
          <div v-for="p in readiness.points" :key="`${p.device_id}:${p.object_id}`" class="ft-run-point-row">
            <span :style="{ color: p.status === 'ok' ? '#52c41a' : '#ff4d4f', fontWeight: 600 }">
              {{ p.status === 'ok' ? '✓' : '✕' }}
            </span>
            {{ p.device_name ? `${p.device_name} / ${p.object_name ?? '?'}` : `Device ${p.device_id}` }}
            <div class="ft-run-point-detail">{{ p.message || STATUS_LABEL[p.status] }}</div>
          </div>
        </template>

        <a-button type="primary" block :disabled="!allReady" :loading="starting" style="margin-top:12px" @click="startRun">
          Run Test
        </a-button>
      </template>

      <template v-else>
        <div v-if="isTerminal" class="ft-run-result">
          Result:
          <span :style="{ color: run?.state === 'passed' ? '#52c41a' : (run?.state === 'failed' || run?.state === 'error') ? '#ff4d4f' : '#faad14' }">
            {{ (run?.state ?? '').toUpperCase() }}
          </span>
          <div v-if="durationSeconds !== null" class="ft-run-muted">Duration: {{ formatDuration(durationSeconds) }}</div>
          <div v-if="run?.result_message" class="ft-run-point-detail">{{ run.result_message }}</div>
          <div v-if="run?.error" class="ft-run-point-detail" style="color:#ff4d4f">{{ run.error }}</div>
        </div>
        <div v-else class="ft-run-muted"><a-spin size="small" /> Running...</div>

        <div v-if="visibleDetails.length" class="ft-run-progress">
          <div v-for="(d, idx) in visibleDetails" :key="`${d.node_id}-${idx}`" class="ft-run-detail-row">
            <strong>{{ d.type.toUpperCase() }}</strong> — {{ d.message }}
          </div>
        </div>

        <a-button v-if="!isTerminal" danger block :loading="cancelling" style="margin-top:12px" @click="cancelRun">
          Cancel Run
        </a-button>
        <a-button v-else block style="margin-top:12px" @click="emit('close')">Close</a-button>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.ft-run-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--surface, #fff);
  border-left: 1px solid var(--border, #d9d9d9);
}

.ft-run-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 8px 14px 14px;
  border-bottom: 1px solid var(--border, #d9d9d9);
  flex-shrink: 0;
}

.ft-panel-title {
  font-weight: 600;
  font-size: 13px;
}

.ft-run-panel-body {
  padding: 14px;
  overflow-y: auto;
  font-size: 12px;
}

.ft-run-muted {
  color: var(--text-secondary, #888);
  font-size: 12px;
}

.ft-run-section-title {
  font-weight: 600;
  margin-bottom: 8px;
}

.ft-run-issue-row {
  padding: 6px 0;
  border-bottom: 1px solid var(--border, #f0f0f0);
  color: #874d00;
}

.ft-run-issue-row--clickable {
  cursor: pointer;
}

.ft-run-issue-row--clickable:hover {
  text-decoration: underline;
}

.ft-run-point-row {
  padding: 6px 0;
  border-bottom: 1px solid var(--border, #f0f0f0);
}

.ft-run-point-detail {
  margin-left: 20px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-secondary, #888);
}

.ft-run-result {
  font-weight: 600;
  font-size: 13px;
  margin: 10px 0;
}

.ft-run-progress {
  margin-top: 12px;
  border-top: 1px solid var(--border, #f0f0f0);
  padding-top: 8px;
}

.ft-run-detail-row {
  padding: 4px 0;
  border-bottom: 1px solid var(--border, #f0f0f0);
  word-break: break-word;
}
</style>
