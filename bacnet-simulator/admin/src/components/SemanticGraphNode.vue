<script setup lang="ts">
/** Custom Vue Flow node for the Graph tab (SemanticPanel.vue) -- one
 * component for all four entity kinds (+ the synthetic "unclassified
 * point" variant), branching purely on `data.kind`, matching the exact
 * per-kind color palette the previous Cytoscape stylesheet used. Sizing
 * (width/height) is set on the Vue Flow node object itself in
 * SemanticPanel.vue's NODE_DIMENSIONS, matching what Dagre used to compute
 * this node's position -- this component only fills that box (100%/100%),
 * it never determines its own size. */
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'

const props = defineProps<{
  data: {
    entityId: number
    kind: 'equipment' | 'location' | 'controller' | 'point' | 'point-unclassified'
    label: string
    brickClass: string | null
    isFocus: boolean
    // Live-display fields (Phase 2), populated by SemanticPanel.vue's
    // updateLiveDisplay() -- this component only renders them, it never
    // resolves anything itself.
    objectId?: number | null
    valueLabel?: string | null
    justChanged?: boolean
    summaryLines?: { label: string; value: string; isCommand: boolean }[]
  }
  selected?: boolean
}>()

const kindClass = computed(() => `sgn-${props.data.kind}`)
</script>

<template>
  <div class="sgn" :class="[kindClass, { 'sgn--focus': data.isFocus || selected }]">
    <Handle type="target" :position="Position.Left" />
    <div class="sgn-label">{{ data.label }}</div>
    <div v-if="data.brickClass" class="sgn-class">{{ data.brickClass }}</div>

    <div
      v-if="(data.kind === 'point' || data.kind === 'point-unclassified') && data.valueLabel"
      class="sgn-value"
      :class="{ 'sgn-value--flash': data.justChanged }"
    >
      {{ data.valueLabel }}
    </div>

    <div v-if="data.kind === 'equipment' && data.summaryLines?.length" class="sgn-summary">
      <div v-for="line in data.summaryLines" :key="line.label" class="sgn-summary-row" :class="{ 'sgn-summary-row--command': line.isCommand }">
        <span class="sgn-summary-label">{{ line.label }}</span>
        <span class="sgn-summary-value">{{ line.value }}</span>
      </div>
    </div>

    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.sgn {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  padding: 8px;
  border-radius: 8px;
  border: 1.5px solid var(--border, #595959);
  text-align: center;
  font-size: 11px;
  color: var(--text-primary, #d9d9d9);
  background: var(--surface-alt, #262626);
  box-sizing: border-box;
  overflow: hidden;
}
.sgn-label {
  font-weight: 500;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.sgn-class {
  margin-top: 2px;
  font-size: 10px;
  opacity: 0.8;
  overflow-wrap: anywhere;
}

.sgn-value {
  margin-top: 3px;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  transition: color 0.15s ease;
}
/* Brief highlight on value change -- CSS-only, no existing convention in
   this app to reuse (confirmed no other @keyframes/flash pattern exists). */
.sgn-value--flash {
  animation: sgn-flash 0.9s ease-out;
}
@keyframes sgn-flash {
  0% { color: #ffd666; }
  100% { color: inherit; }
}

.sgn-summary {
  margin-top: 4px;
  width: 100%;
  max-height: 44px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.sgn-summary-row {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-size: 9px;
  line-height: 1.3;
  opacity: 0.9;
}
.sgn-summary-label {
  color: var(--text-muted, #8c8c8c);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sgn-summary-value {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
/* Commands are visibly distinguished from measured state -- never shown
   as if they were a sensor/status reading. */
.sgn-summary-row--command .sgn-summary-value {
  color: #d4b106;
  font-style: italic;
}

.sgn-equipment {
  border-radius: 10px;
  background: #162d4d;
  border-color: #4096ff;
  border-width: 2px;
}
.sgn-point,
.sgn-point-unclassified {
  border-radius: 999px;
  font-size: 10px;
}
.sgn-point {
  background: #173b2c;
  border-color: #49aa19;
}
.sgn-point-unclassified {
  background: #262626;
  border-color: #595959;
  border-style: dashed;
  color: var(--text-muted, #8c8c8c);
}
.sgn-location {
  border-radius: 10px;
  background: #30204d;
  border-color: #9254de;
  border-style: dashed;
  border-width: 2px;
}
.sgn-controller {
  border-radius: 10px;
  background: #4d2b0a;
  border-color: #d46b08;
  border-width: 2px;
}

.sgn--focus {
  border-color: var(--primary-color, #1677ff) !important;
  border-width: 4px !important;
}
</style>
