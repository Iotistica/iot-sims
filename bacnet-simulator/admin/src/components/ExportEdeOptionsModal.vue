<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Device } from '../types'

const props = defineProps<{
  open: boolean
  device: Device | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
}>()

const mode = ref<'defaults' | 'live'>('defaults')
const exporting = ref(false)

watch(() => props.open, (v) => {
  if (v) mode.value = 'defaults'
})

async function doExport() {
  if (!props.device) return
  exporting.value = true
  try {
    await api.devices.exportEde(props.device.id, props.device.name, mode.value)
    emit('update:open', false)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Export failed')
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title="Export EDE"
    ok-text="Export"
    cancel-text="Close"
    :confirm-loading="exporting"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="doExport"
  >
    <a-radio-group v-model:value="mode" style="display:flex;flex-direction:column;gap:8px;margin-top:4px">
      <a-radio value="defaults">
        <span style="font-weight:500">Configuration defaults</span>
        <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;margin-left:24px">Export configured/default object values.</div>
      </a-radio>
      <a-radio value="live">
        <span style="font-weight:500">Current live values</span>
        <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;margin-left:24px">Export current BACnet Present_Value values.</div>
      </a-radio>
    </a-radio-group>
  </a-modal>
</template>
