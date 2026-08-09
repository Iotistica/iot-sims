<script setup lang="ts">
import { SearchOutlined } from '@ant-design/icons-vue'

withDefaults(
  defineProps<{
    search: string
    searchPlaceholder?: string
    /** Whether the Clear button should be enabled. Defaults to true (no dead-button tracking). */
    canClear?: boolean
  }>(),
  { canClear: true },
)

const emit = defineEmits<{
  'update:search': [string]
  clear: []
}>()
</script>

<template>
  <div class="grid-filter-toolbar">
    <a-input
      :value="search"
      size="small"
      allow-clear
      :placeholder="searchPlaceholder ?? 'Search…'"
      style="width:220px"
      @update:value="(v: string) => emit('update:search', v)"
    >
      <template #prefix><SearchOutlined style="color:var(--text-placeholder)" /></template>
    </a-input>
    <slot />
    <a-button size="small" :disabled="!canClear" @click="emit('clear')">Clear</a-button>
    <div style="flex:1" />
    <slot name="actions" />
  </div>
</template>

<style scoped>
.grid-filter-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
</style>
