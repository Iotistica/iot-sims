<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Project } from '../types'

const props = defineProps<{
  open: boolean
  // The exact same live-state wipe App.vue's toolbar "New Project" already
  // uses — passed down and called once, not reimplemented here, so there is
  // only ever one reset implementation.
  resetProject: (silent?: boolean) => Promise<void>
}>()
const emit = defineEmits<{ 'update:open': [v: boolean]; created: [project: Project] }>()

const name = ref('')
const description = ref('')
const aboveGroundLevels = ref(0)
const belowGroundLevels = ref(0)
const creating = ref(false)

watch(() => props.open, (v) => {
  if (!v) return
  name.value = ''
  description.value = ''
  aboveGroundLevels.value = 0
  belowGroundLevels.value = 0
})

async function doCreate() {
  if (!name.value.trim()) { message.error('Project name is required'); return }

  creating.value = true
  try {
    // Reset first (existing behavior, reused as-is) — creation never
    // triggers discovery; discovery only happens afterward, from the
    // Devices toolbar's "Discover" action.
    await props.resetProject(true)

    // 0/0 is valid — no building hierarchy generated (backend returns early).
    const project = await api.projects.save(name.value.trim(), description.value.trim(), undefined, aboveGroundLevels.value, belowGroundLevels.value)
    message.success(`"${project.name}" created`)
    emit('created', project)
    emit('update:open', false)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to create project')
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <a-modal
    :open="open"
    title="New Project"
    ok-text="Create Project"
    :confirm-loading="creating"
    @ok="doCreate"
    @cancel="emit('update:open', false)"
  >
    <a-form layout="vertical" style="margin-top:8px">
      <a-form-item label="Project Name" required>
        <a-input v-model:value="name" placeholder="e.g. Building A — Floor 3" />
      </a-form-item>
      <a-form-item label="Description">
        <a-input v-model:value="description" placeholder="Optional description" />
      </a-form-item>
      <a-form-item label="Building Structure">
        <div style="display:flex;gap:8px;align-items:center">
          <div style="display:flex;flex-direction:column">
            <label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">Above ground</label>
            <a-input-number v-model:value="aboveGroundLevels" :min="0" />
          </div>
          <div style="display:flex;flex-direction:column">
            <label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">Below ground</label>
            <a-input-number v-model:value="belowGroundLevels" :min="0" />
          </div>
        </div>
      </a-form-item>
    </a-form>
  </a-modal>
</template>
