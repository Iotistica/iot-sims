<script setup lang="ts">
import { ref, onUnmounted } from 'vue'
import { ClearOutlined, DownOutlined, UpOutlined } from '@ant-design/icons-vue'
import type { LogEntry } from '../types'
import { api } from '../api'

const entries = ref<LogEntry[]>([])
const scrollEl = ref<HTMLElement | null>(null)
const collapsed = ref(false)

async function fetchLogs() {
  try {
    entries.value = await api.logs(200)
    if (!collapsed.value && scrollEl.value) {
      await new Promise(r => requestAnimationFrame(r))
      scrollEl.value.scrollTop = scrollEl.value.scrollHeight
    }
  } catch { /* swallow */ }
}

function clearLogs() {
  entries.value = []
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// ── Resize handle ─────────────────────────────────────────────────────────────

const panelHeight = ref(200)
const MIN_HEIGHT = 80
const MAX_HEIGHT = 800

let dragStartY = 0
let dragStartHeight = 0

function onDragStart(e: MouseEvent) {
  if (collapsed.value) return
  dragStartY = e.clientY
  dragStartHeight = panelHeight.value
  document.addEventListener('mousemove', onDragMove)
  document.addEventListener('mouseup', onDragEnd)
  e.preventDefault()
}

function onDragMove(e: MouseEvent) {
  const delta = dragStartY - e.clientY
  panelHeight.value = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, dragStartHeight + delta))
}

function onDragEnd() {
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragEnd)
}

fetchLogs()
const timer = setInterval(fetchLogs, 5000)
onUnmounted(() => {
  clearInterval(timer)
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragEnd)
})
</script>

<template>
  <div
    :class="['log-panel', { 'log-panel--collapsed': collapsed }]"
    :style="collapsed ? undefined : { height: panelHeight + 'px' }"
  >
    <div class="drag-handle" @mousedown="onDragStart">
      <div class="drag-grip" />
    </div>
    <div class="log-header" @click="collapsed = !collapsed">
      <span class="log-title">Activity Log</span>
      <div style="display:flex;align-items:center;gap:4px" @click.stop>
        <a-button type="text" size="small" title="Clear" style="color:#666" @click="clearLogs">
          <template #icon><ClearOutlined /></template>
        </a-button>
        <a-button type="text" size="small" style="color:#666" @click="collapsed = !collapsed">
          <template #icon><UpOutlined v-if="!collapsed" /><DownOutlined v-else /></template>
        </a-button>
      </div>
    </div>
    <div v-show="!collapsed" ref="scrollEl" class="log-body">
      <div v-if="!entries.length" class="log-empty">No events yet</div>
      <div v-for="(e, i) in entries" :key="i" class="log-row">
        <span class="log-ts">{{ fmtTime(e.ts) }}</span>
        <span :class="['log-level', `log-level--${e.level}`]">{{ e.level.toUpperCase() }}</span>
        <span v-if="e.device_name" class="log-device">{{ e.device_name }}</span>
        <span class="log-msg">{{ e.message }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.log-panel {
  display: flex;
  flex-direction: column;
  background: #0d1117;
  border-top: 1px solid #30363d;
  font-family: 'Consolas', 'Menlo', monospace;
  font-size: 12px;
  flex-shrink: 0;
}
.log-panel--collapsed {
  height: 32px !important;
}
.drag-handle {
  height: 6px;
  background: #161b22;
  cursor: ns-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.drag-handle:hover .drag-grip,
.drag-handle:active .drag-grip {
  background: #58a6ff;
}
.drag-grip {
  width: 32px;
  height: 2px;
  border-radius: 1px;
  background: #30363d;
  transition: background 0.15s;
}
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 6px 4px 10px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  cursor: pointer;
  user-select: none;
  flex-shrink: 0;
  height: 32px;
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
  white-space: nowrap;
}
.log-level--info  { color: #58a6ff; background: rgba(88,166,255,.12); }
.log-level--warn  { color: #e3b341; background: rgba(227,179,65,.12); }
.log-level--error { color: #f85149; background: rgba(248,81,73,.12); }
.log-device {
  color: #7ee787;
  background: rgba(126,231,135,.1);
  border-radius: 3px;
  padding: 0 4px;
  font-size: 10px;
  white-space: nowrap;
}
.log-msg { color: #cdd9e5; word-break: break-word; }
</style>
