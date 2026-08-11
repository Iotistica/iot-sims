<script setup lang="ts">
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { Project, ProjectSourceType, BACnetConnectionConfig, ModelingMode } from '../types'

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
const modelingMode = ref<ModelingMode>('standard')
const sourceType = ref<ProjectSourceType>('simulated')
const discoveryTarget = ref('')
const deviceInstanceLow = ref(0)
const deviceInstanceHigh = ref(4194303)
const timeoutMs = ref(5000)
const creating = ref(false)

watch(() => props.open, (v) => {
  if (!v) return
  name.value = ''
  description.value = ''
  modelingMode.value = 'standard'
  sourceType.value = 'simulated'
  discoveryTarget.value = ''
  deviceInstanceLow.value = 0
  deviceInstanceHigh.value = 4194303
  timeoutMs.value = 5000
})

async function doCreate() {
  if (!name.value.trim()) { message.error('Project name is required'); return }

  creating.value = true
  try {
    // Reset first (existing behavior, reused as-is) — creation never
    // depends on discovery succeeding; discovery only happens afterward,
    // from the project's own "Discover Devices" action.
    await props.resetProject(true)

    const connectionConfig: BACnetConnectionConfig | null =
      sourceType.value === 'external-bacnet'
        ? {
            discovery_target: discoveryTarget.value.trim() || null,
            device_instance_low: deviceInstanceLow.value,
            device_instance_high: deviceInstanceHigh.value,
            timeout_ms: timeoutMs.value,
          }
        : null

    const project = await api.projects.save(
      name.value.trim(),
      description.value.trim(),
      sourceType.value,
      connectionConfig,
      modelingMode.value,
    )
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

      <a-form-item label="Start From">
        <a-radio-group v-model:value="sourceType" style="width:100%">
          <div style="display:flex;flex-direction:column;gap:8px">
            <a-radio value="simulated">
              Blank Simulation
              <div style="font-size:12px;color:var(--text-secondary);margin-left:24px">
                Start with an empty project and add simulated devices manually.
              </div>
            </a-radio>
            <a-radio value="external-bacnet">
              External BACnet
              <div style="font-size:12px;color:var(--text-secondary);margin-left:24px">
                Connect to an existing BACnet network and discover devices.
              </div>
            </a-radio>
          </div>
        </a-radio-group>
      </a-form-item>

      <a-form-item label="Modeling Mode">
        <a-radio-group v-model:value="modelingMode" style="width:100%">
          <div style="display:flex;flex-direction:column;gap:8px">
            <a-radio value="standard">
              Standard BACnet Simulator
              <div style="font-size:12px;color:var(--text-secondary);margin-left:24px">
                Simulate BACnet devices, objects, values, alarms and network behavior.
              </div>
            </a-radio>
            <a-radio value="semantic">
              Semantic Building Model (Brick)
              <div style="font-size:12px;color:var(--text-secondary);margin-left:24px">
                Also model Controllers, Equipment and their relationships using Brick.
              </div>
            </a-radio>
          </div>
        </a-radio-group>
      </a-form-item>

      <template v-if="sourceType === 'external-bacnet'">
        <a-form-item
          label="Discovery Target"
          help="Leave empty to use normal local BACnet broadcast discovery. Enter an IP/host when broadcast is unavailable or when targeting a known BACnet host/gateway."
        >
          <a-input v-model:value="discoveryTarget" placeholder="e.g. 192.168.1.50 (optional)" />
        </a-form-item>

        <a-collapse ghost style="margin-bottom:0">
          <a-collapse-panel key="advanced" header="Advanced">
            <a-form-item label="Device Instance Range" style="margin-bottom:12px">
              <a-input-group compact>
                <a-input-number v-model:value="deviceInstanceLow" :min="0" :max="4194303" addon-before="Low" style="width:50%" />
                <a-input-number v-model:value="deviceInstanceHigh" :min="0" :max="4194303" addon-before="High" style="width:50%" />
              </a-input-group>
            </a-form-item>
            <a-form-item label="Discovery Timeout (ms)" style="margin-bottom:0">
              <a-input-number v-model:value="timeoutMs" :min="100" :max="60000" style="width:100%" />
            </a-form-item>
          </a-collapse-panel>
        </a-collapse>
      </template>
    </a-form>
  </a-modal>
</template>
