<script setup lang="ts">
/** Read-only view of the last SAVED test (id/name/description/
 * equipment_type/definition/timestamps) as pretty-printed JSON -- exactly
 * what GET /functional-tests/{id} returns -- in the same right-hand slot
 * FunctionalTestProperties/FunctionalTestRunPanel occupy. */
import { computed } from 'vue'
import { message } from 'ant-design-vue'
import { CloseOutlined, CopyOutlined } from '@ant-design/icons-vue'
import type { FunctionalTest } from '../../types'

const props = defineProps<{
  test: FunctionalTest
}>()

const emit = defineEmits<{
  close: []
}>()

const json = computed(() => JSON.stringify(props.test, null, 2))

async function copyJson() {
  try {
    await navigator.clipboard.writeText(json.value)
    message.success('Copied JSON to clipboard')
  } catch {
    message.error('Failed to copy -- your browser may be blocking clipboard access')
  }
}
</script>

<template>
  <aside class="ft-json-panel">
    <div class="ft-json-panel-header">
      <div class="ft-panel-title" style="margin:0">Saved JSON</div>
      <div style="display:flex;gap:4px">
        <a-button type="text" size="small" @click="copyJson"><CopyOutlined /></a-button>
        <a-button type="text" size="small" @click="emit('close')"><CloseOutlined /></a-button>
      </div>
    </div>
    <pre class="ft-json-panel-body">{{ json }}</pre>
  </aside>
</template>

<style scoped>
.ft-json-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--surface, #fff);
  border-left: 1px solid var(--border, #d9d9d9);
}

.ft-json-panel-header {
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

.ft-json-panel-body {
  margin: 0;
  padding: 14px;
  overflow: auto;
  flex: 1;
  min-height: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  line-height: 1.5;
  white-space: pre;
}
</style>
