<script setup lang="ts">
/** Custom Vue Flow edge for `feeds` relationships only (see SemanticPanel.
 * vue's buildFlowElements(), which assigns type: 'semanticFlowEdge' to
 * feeds edges exclusively -- every other predicate keeps Vue Flow's
 * default, unanimated edge). Purely a playback surface: `data.flowState`
 * is computed upstream by SemanticPanel.vue's resolveFeedsFlowState(),
 * this component has no resolution logic of its own, only rendering.
 *
 * Loosely typed props (not Vue Flow's exact EdgeProps generic) -- the same
 * vue-tsc "excessively deep type instantiation" workaround used for
 * nodes/edges in SemanticPanel.vue. */
import { computed } from 'vue'
import { BaseEdge, getBezierPath } from '@vue-flow/core'

// Loosely typed (mostly `any`) rather than Vue Flow's exact EdgeProps
// generic -- same vue-tsc "excessively deep type instantiation" workaround
// used for nodes/edges in SemanticPanel.vue; `v-bind="edgeProps"` there
// hands this component Vue Flow's real (much wider) prop types.
const props = defineProps<{
  id: string
  sourceX: number
  sourceY: number
  targetX: number
  targetY: number
  sourcePosition?: any
  targetPosition?: any
  markerEnd?: any
  label?: any
  labelStyle?: any
  labelBgStyle?: any
  labelBgPadding?: any
  style?: any
  data?: {
    predicate?: string
    flowState?: { status: 'measured' | 'qualitative' | 'off' | 'unknown'; tier: 0 | 1 | 2 } | null
  }
}>()

const pathData = computed(() =>
  getBezierPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    sourcePosition: props.sourcePosition,
    targetX: props.targetX,
    targetY: props.targetY,
    targetPosition: props.targetPosition,
  }),
)
const path = computed(() => pathData.value[0])
const labelX = computed(() => pathData.value[1])
const labelY = computed(() => pathData.value[2])

const flowState = computed(() => props.data?.flowState ?? null)

// Base line never changes color/dash by itself -- an 'unknown' flow state
// (or a feeds edge with no resolvable evidence at all) renders exactly
// like a normal labeled relationship edge. Only the animated overlay below
// carries any flow-related meaning, and only when it's actually shown.
const baseStyle = computed(() => ({ stroke: 'var(--border, #595959)', ...(props.style ?? {}) }))

const isAnimated = computed(() => {
  const status = flowState.value?.status
  return status === 'measured' || status === 'qualitative'
})
const overlayClass = computed(() => {
  const state = flowState.value
  if (!state) return ''
  const speed = state.tier === 2 ? 'sfe-flow--fast' : 'sfe-flow--normal'
  // Qualitative (Fan_Status only, no measured magnitude) renders dimmer --
  // a visibly less-certain indicator than an actual sensor reading.
  return state.status === 'qualitative' ? `${speed} sfe-flow--qualitative` : speed
})
</script>

<template>
  <BaseEdge
    :id="id"
    :path="path"
    :marker-end="markerEnd"
    :label="label"
    :label-x="labelX"
    :label-y="labelY"
    :label-style="labelStyle"
    :label-show-bg="true"
    :label-bg-style="labelBgStyle"
    :label-bg-padding="labelBgPadding"
    :style="baseStyle"
  />
  <path v-if="isAnimated" :d="path" fill="none" class="sfe-overlay" :class="overlayClass" />
</template>

<style scoped>
.sfe-overlay {
  stroke: #4096ff;
  stroke-width: 2.4;
  stroke-dasharray: 6 6;
  pointer-events: none;
}
/* stroke-dashoffset decreasing moves the dash pattern in the direction the
   path was drawn -- source (feeder) -> target (fed), matching the feeds
   relationship's own stored/displayed direction with no extra logic here. */
.sfe-overlay.sfe-flow--normal {
  animation: sfe-dash 1.1s linear infinite;
}
.sfe-overlay.sfe-flow--fast {
  animation: sfe-dash 0.5s linear infinite;
}
.sfe-overlay.sfe-flow--qualitative {
  opacity: 0.6;
}
@keyframes sfe-dash {
  to {
    stroke-dashoffset: -24;
  }
}
</style>
