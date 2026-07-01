<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import type { SimObject } from '../types'

const props = defineProps<{
  open: boolean
  objects: SimObject[]
  deviceName?: string
}>()
const emit = defineEmits<{ 'update:open': [v: boolean]; saved: [] }>()

const name = ref('')
const desc = ref('')

watch(() => props.open, (v) => {
  if (!v) return
  name.value = props.deviceName ? `${props.deviceName} Template` : ''
  desc.value = ''
})

function save() {
  if (!name.value.trim()) { message.error('Name is required'); return }

  const existing = JSON.parse(localStorage.getItem('bacnet-sim-user-templates') || '[]')
  existing.push({
    key: `user-${Date.now()}`,
    label: name.value.trim(),
    desc: desc.value.trim(),
    objects: props.objects.map(o => ({
      object_type:     o.object_type,
      object_instance: o.object_instance,
      name:            o.name,
      units:           o.units,
      behavior:        o.behavior,
      behavior_params: o.behavior_params,
    })),
    createdAt: new Date().toISOString().slice(0, 10),
  })
  localStorage.setItem('bacnet-sim-user-templates', JSON.stringify(existing))

  message.success(`Template "${name.value.trim()}" saved`)
  emit('update:open', false)
  emit('saved')
}
</script>

<template>
  <a-modal
    :open="open"
    title="Save as Template"
    ok-text="Save Template"
    @ok="save"
    @cancel="emit('update:open', false)"
  >
    <a-form layout="vertical" style="margin-top:8px">
      <a-form-item label="Template Name" required>
        <a-input
          v-model:value="name"
          placeholder="e.g. My AHU Controller"
          @press-enter="save"
        />
      </a-form-item>
      <a-form-item label="Description" style="margin-bottom:8px">
        <a-input
          v-model:value="desc"
          placeholder="Optional description"
        />
      </a-form-item>
      <div style="font-size:12px;color:#8c8c8c">
        {{ objects.length }} object{{ objects.length !== 1 ? 's' : '' }} will be saved
      </div>
    </a-form>
  </a-modal>
</template>
