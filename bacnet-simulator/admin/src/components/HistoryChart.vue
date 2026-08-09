<script setup lang="ts">
// Reusable SVG line chart + Min/Max/Avg/Current stats row, extracted from
// App.vue's point History modal so the Trend Log Records modal's Chart tab
// can reuse the exact same rendering against a different data source
// (stored trend log records instead of the point's recent-history buffer).
// No chart library involved -- same hand-rolled inline SVG as before, just
// moved into its own component.
import type { HistoryPoint } from '../types'

const props = withDefaults(defineProps<{
  data: HistoryPoint[]
  loading?: boolean
  formatValue?: (v: number) => string
  emptyLabel?: string
}>(), {
  loading: false,
  formatValue: (v: number) => v.toFixed(2),
  emptyLabel: 'Not enough data yet',
})

const CHART_W = 600
const CHART_H = 192
const CHART_PAD = { top: 16, right: 12, bottom: 30, left: 52 }

function svgPoints(data: HistoryPoint[]): string {
  if (data.length < 2) return ''
  const vals = data.map(p => p.value)
  const tss  = data.map(p => p.ts)
  let minV = Math.min(...vals), maxV = Math.max(...vals)
  if (minV === maxV) { minV -= 1; maxV += 1 }
  const minT = tss[0], maxT = tss[tss.length - 1]
  const w = CHART_W - CHART_PAD.left - CHART_PAD.right
  const h = CHART_H - CHART_PAD.top  - CHART_PAD.bottom
  return data.map(p => {
    const x = CHART_PAD.left + ((p.ts - minT) / (maxT - minT)) * w
    const y = CHART_PAD.top  + (1 - (p.value - minV) / (maxV - minV)) * h
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

function stats(data: HistoryPoint[]) {
  if (!data.length) return { min: 0, max: 0, avg: 0, current: 0 }
  const vals = data.map(p => p.value)
  const min = Math.min(...vals), max = Math.max(...vals)
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length
  return { min, max, avg, current: vals[vals.length - 1] }
}

function yLabels(data: HistoryPoint[]) {
  if (data.length < 2) return []
  const vals = data.map(p => p.value)
  let minV = Math.min(...vals), maxV = Math.max(...vals)
  if (minV === maxV) { minV -= 1; maxV += 1 }
  const h = CHART_H - CHART_PAD.top - CHART_PAD.bottom
  return [
    { y: CHART_PAD.top,           v: maxV },
    { y: CHART_PAD.top + h / 2,   v: (minV + maxV) / 2 },
    { y: CHART_PAD.top + h,       v: minV },
  ].map(t => ({ y: t.y, label: props.formatValue(t.v) }))
}

function xLabels(data: HistoryPoint[]) {
  if (data.length < 2) return []
  const tss = data.map(p => p.ts)
  const minT = tss[0], maxT = tss[tss.length - 1]
  const w = CHART_W - CHART_PAD.left - CHART_PAD.right
  const now = Date.now() / 1000
  const N = 5
  return Array.from({ length: N }, (_, i) => {
    const frac = i / (N - 1)
    const ts   = minT + frac * (maxT - minT)
    const x    = CHART_PAD.left + frac * w
    const age  = now - ts
    let label: string
    if (age < 10)        label = 'now'
    else if (age < 120)  label = `-${Math.round(age)}s`
    else if (age < 3600) label = `-${Math.round(age / 60)}m`
    else                 label = `-${Math.round(age / 3600)}h`
    return { x, label }
  })
}
</script>

<template>
  <div v-if="loading" style="text-align:center;padding:40px 0">
    <a-spin />
  </div>
  <div v-else-if="data.length < 2" style="text-align:center;padding:40px 0;color:var(--text-placeholder);font-size:13px">
    {{ emptyLabel }}
  </div>
  <template v-else>
    <!-- Chart -->
    <div style="border:1px solid var(--border-subtle);border-radius:4px;background:var(--panel-bg);overflow:hidden">
      <svg :viewBox="`0 0 ${CHART_W} ${CHART_H}`" style="width:100%;display:block">

        <!-- Y-axis grid lines + labels -->
        <template v-for="tick in yLabels(data)" :key="tick.y">
          <line
            :x1="CHART_PAD.left" :y1="tick.y"
            :x2="CHART_W - CHART_PAD.right" :y2="tick.y"
            stroke="var(--border-subtle)" stroke-width="1"
          />
          <text
            :x="CHART_PAD.left - 6" :y="tick.y"
            text-anchor="end" dominant-baseline="middle"
            font-size="11" fill="var(--text-placeholder)" font-family="monospace"
          >{{ tick.label }}</text>
        </template>

        <!-- X-axis baseline -->
        <line
          :x1="CHART_PAD.left" :y1="CHART_H - CHART_PAD.bottom"
          :x2="CHART_W - CHART_PAD.right" :y2="CHART_H - CHART_PAD.bottom"
          stroke="var(--border)" stroke-width="1"
        />

        <!-- X-axis ticks + labels -->
        <template v-for="tick in xLabels(data)" :key="tick.x">
          <line
            :x1="tick.x" :y1="CHART_H - CHART_PAD.bottom"
            :x2="tick.x" :y2="CHART_H - CHART_PAD.bottom + 5"
            stroke="var(--text-disabled)" stroke-width="1"
          />
          <text
            :x="tick.x" :y="CHART_H - CHART_PAD.bottom + 17"
            text-anchor="middle"
            font-size="11" fill="var(--text-placeholder)" font-family="sans-serif"
          >{{ tick.label }}</text>
        </template>

        <!-- Fill area under line -->
        <polyline
          :points="`${CHART_PAD.left},${CHART_H - CHART_PAD.bottom} ${svgPoints(data)} ${CHART_W - CHART_PAD.right},${CHART_H - CHART_PAD.bottom}`"
          fill="rgba(24,144,255,0.08)"
          stroke="none"
        />
        <!-- Data line -->
        <polyline
          :points="svgPoints(data)"
          fill="none"
          stroke="#1890ff"
          stroke-width="1.5"
          stroke-linejoin="round"
        />
      </svg>
    </div>

    <!-- Stats row -->
    <div style="display:flex;gap:0;margin-top:14px;border:1px solid var(--border-subtle);border-radius:4px;overflow:hidden">
      <div
        v-for="(stat, label) in { Min: stats(data).min, Max: stats(data).max, Avg: stats(data).avg, Current: stats(data).current }"
        :key="label"
        style="flex:1;text-align:center;padding:10px 0;border-right:1px solid var(--border-subtle)"
        :style="label === 'Current' ? 'border-right:none' : ''"
      >
        <div style="font-size:11px;color:var(--text-secondary);margin-bottom:2px">{{ label }}</div>
        <div style="font-size:14px;font-weight:600;font-family:monospace;color:#1890ff">
          {{ formatValue(stat) }}
        </div>
      </div>
    </div>
  </template>
</template>
