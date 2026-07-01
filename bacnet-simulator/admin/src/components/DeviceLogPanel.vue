<script setup lang="ts">
import { ref, watch, nextTick, onUnmounted } from 'vue'
import { ClearOutlined } from '@ant-design/icons-vue'
import type { LogEntry } from '../types'
import { api } from '../api'

const props = defineProps<{ deviceId: number | null }>()

const entries = ref<LogEntry[]>([])
const scrollEl = ref<HTMLElement | null>(null)

let timer: ReturnType<typeof setInterval> | null = null

async function fetchLogs() {
  if (props.deviceId === null) return
  try {
    entries.value = await api.devices.logs(props.deviceId, 100)
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  } catch { /* swallow */ }
}

function clearLogs() {
  entries.value = []
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

watch(() => props.deviceId, (id) => {
  entries.value = []
  if (timer) clearInterval(timer)
  if (id === null) return
  fetchLogs()
  timer = setInterval(fetchLogs, 5000)
}, { immediate: true })

onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="log-panel">
    <div class="log-header">
      <span class="log-title">Device Log</span>
      <a-button type="text" size="small" title="Clear" style="color:#666" @click="clearLogs">
        <template #icon><ClearOutlined /></template>
      </a-button>
    </div>
    <div ref="scrollEl" class="log-body">
      <div v-if="!deviceId" class="log-empty">Select a device to view its log</div>
      <div v-else-if="!entries.length" class="log-empty">No events yet</div>
      <div v-for="(e, i) in entries" :key="i" class="log-row">
        <span class="log-ts">{{ fmtTime(e.ts) }}</span>
        <span :class="['log-level', `log-level--${e.level}`]">{{ e.level.toUpperCase() }}</span>
        <span class="log-msg">{{ e.message }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.log-panel {
  display: flex;
  flex-direction: column;
  height: 200px;
  background: #0d1117;
  border-top: 1px solid #30363d;
  font-family: 'Consolas', 'Menlo', monospace;
  font-size: 12px;
  flex-shrink: 0;
}
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 10px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
}
.log-title {
  font-size: 11px;
  font-weight: 600;
  color: #8b949e;
  text-transform: uppercase;
  letter-spacing: .06em;
}
.log-body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.log-empty {
  padding: 12px 10px;
  color: #484f58;
  font-size: 12px;
}
.log-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2px 10px;
  line-height: 1.5;
}
.log-row:hover { background: rgba(255,255,255,.03); }
.log-ts { color: #484f58; white-space: nowrap; }
.log-level {
  font-size: 10px;
  font-weight: 700;
  min-width: 36px;
  text-align: center;
  border-radius: 2px;
  padding: 0 3px;
}
.log-level--info  { color: #58a6ff; background: rgba(88,166,255,.12); }
.log-level--warn  { color: #e3b341; background: rgba(227,179,65,.12); }
.log-level--error { color: #f85149; background: rgba(248,81,73,.12); }
.log-msg { color: #cdd9e5; word-break: break-word; }
</style>
