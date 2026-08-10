<script setup lang="ts">
import type { FunctionalTestNodeType } from '../../types'

defineProps<{
  canDeleteSelected: boolean
}>()

const emit = defineEmits<{
  add: [type: FunctionalTestNodeType]
  'delete-selected': []
}>()

const BLOCKS: { type: FunctionalTestNodeType; label: string }[] = [
  { type: 'start', label: 'Start' },
  { type: 'wait', label: 'Wait' },
  { type: 'wait_until', label: 'Wait Until' },
  { type: 'capture', label: 'Capture' },
  { type: 'verify', label: 'Verify' },
  { type: 'compare', label: 'Compare' },
  { type: 'end', label: 'End' },
]
</script>

<template>
  <aside class="ft-palette">
    <div class="ft-panel-title">Blocks</div>

    <a-button
      v-for="block in BLOCKS"
      :key="block.type"
      block
      style="margin-bottom:8px"
      @click="emit('add', block.type)"
    >
      {{ block.label }}
    </a-button>

    <div class="ft-palette-divider" />

    <a-button
      block
      danger
      :disabled="!canDeleteSelected"
      @click="emit('delete-selected')"
    >
      Delete selected
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

.ft-palette-divider {
  margin: 14px 0;
  border-top: 1px solid var(--border, #d9d9d9);
}
</style>
