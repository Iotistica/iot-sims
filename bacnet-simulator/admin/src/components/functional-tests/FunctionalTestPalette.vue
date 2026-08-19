<script setup lang="ts">
import type { FunctionalTestNodeType } from '../../types'

const emit = defineEmits<{
  add: [type: FunctionalTestNodeType]
}>()

// Start/End are implicit -- not placed by hand. The block nothing points
// to is the test's entry point, and any dangling output handle
// automatically terminates the test (fail handle -> fail, everything else
// -> pass). See functionalTestSerializer.ts for how that's translated to
// and from the real Start/End nodes the backend still requires.
const BLOCKS: { type: FunctionalTestNodeType; label: string }[] = [
  { type: 'wait', label: 'Wait' },
  { type: 'wait_until', label: 'Wait Until' },
  { type: 'capture', label: 'Capture' },
  { type: 'set', label: 'Set' },
  { type: 'verify', label: 'Verify' },
  { type: 'compare', label: 'Compare' },
]

// Drag-and-drop onto the canvas (FunctionalTestBuilder.vue's onDrop reads
// this same mime type via screenToFlowCoordinate) -- click-to-add via
// @add is kept working side by side, unchanged.
function onDragStart(e: DragEvent, type: FunctionalTestNodeType) {
  if (!e.dataTransfer) return
  e.dataTransfer.setData('application/vueflow', type)
  e.dataTransfer.effectAllowed = 'move'
}
</script>

<template>
  <aside class="ft-palette">
    <div class="ft-panel-title">Blocks</div>

    <a-button
      v-for="block in BLOCKS"
      :key="block.type"
      block
      draggable="true"
      class="ft-palette-block"
      @dragstart="onDragStart($event, block.type)"
      @click="emit('add', block.type)"
    >
      {{ block.label }}
    </a-button>
  </aside>
</template>

<style scoped>
.ft-palette {
  padding: 14px;
  background: var(--surface, #fff);
  border-right: 1px solid var(--border, #d9d9d9);
  overflow-y: auto;
}

.ft-panel-title {
  margin-bottom: 12px;
  font-weight: 600;
  font-size: 13px;
}

.ft-palette-block {
  margin-bottom: 8px;
  cursor: grab;
}

.ft-palette-block:active {
  cursor: grabbing;
}
</style>
