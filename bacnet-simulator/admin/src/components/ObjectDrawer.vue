<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import type { SimObject, Meta } from '../types'

const props = defineProps<{
  open: boolean
  object: SimObject | null
  deviceId: number | undefined
  meta: Meta
}>()
const emit = defineEmits<{ 'update:open': [v: boolean]; saved: [] }>()

const DEFAULT_PARAMS: Record<string, Record<string, number>> = {
  constant:    { value: 0 },
  manual:      { value: 0 },
  sine:        { base: 20, amplitude: 5, period_hours: 24, phase_hours: 0 },
  noise:       { base: 0, noise: 1 },
  random_walk: { value: 50, step: 1, min: 0, max: 100 },
}

const loading = ref(false)
const form = reactive({
  object_type: 'analog-input',
  object_instance: 1,
  name: '',
  units: 'no-units',
  behavior: 'constant',
  enabled: true,
})
const params = ref<Record<string, number>>({ value: 0 })

watch(() => props.open, (v) => {
  if (!v) return
  if (props.object) {
    const o = props.object
    Object.assign(form, { ...o, enabled: !!o.enabled })
    try { params.value = { ...JSON.parse(o.behavior_params) } }
    catch { params.value = { value: 0 } }
  } else {
    Object.assign(form, {
      object_type: props.meta.object_types[0] ?? 'analog-input',
      object_instance: 1, name: '', units: 'no-units', behavior: 'constant', enabled: true,
    })
    params.value = { value: 0 }
  }
})

watch(() => form.behavior, (b) => {
  params.value = { ...(DEFAULT_PARAMS[b] ?? { value: 0 }) }
})

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  if (!props.deviceId) return
  loading.value = true
  const body = { ...form, enabled: form.enabled ? 1 : 0, behavior_params: JSON.stringify(params.value) }
  try {
    if (props.object) {
      await api.objects.update(props.deviceId, props.object.id, body)
      message.success('Object updated')
    } else {
      await api.objects.create(props.deviceId, body)
      message.success('Object created')
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <a-drawer
    :title="object ? 'Edit Object' : 'Add Object'"
    :open="open"
    width="480"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-row :gutter="12">
        <a-col :span="14">
          <a-form-item label="Object Type" required>
            <a-select v-model:value="form.object_type">
              <a-select-option v-for="t in meta.object_types" :key="t" :value="t">{{ t }}</a-select-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="10">
          <a-form-item label="Instance" required>
            <a-input-number v-model:value="form.object_instance" :min="0" style="width:100%" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Supply Temp" />
      </a-form-item>

      <a-row :gutter="12">
        <a-col :span="14">
          <a-form-item label="Units">
            <a-select v-model:value="form.units" show-search>
              <a-select-option v-for="u in meta.units" :key="u" :value="u">{{ u }}</a-select-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="10">
          <a-form-item label="Enabled">
            <a-switch v-model:checked="form.enabled" style="margin-top:5px" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="Behavior">
        <a-select v-model:value="form.behavior">
          <a-select-option v-for="b in meta.behaviors" :key="b" :value="b">{{ b }}</a-select-option>
        </a-select>
      </a-form-item>

      <!-- Behavior params -->
      <div style="background:#fafafa;border:1px solid #e8e8e8;border-radius:6px;padding:14px">
        <!-- constant -->
        <a-form-item v-if="form.behavior === 'constant'" label="Value" style="margin-bottom:0">
          <a-input-number v-model:value="params.value" style="width:100%" :step="0.1" />
        </a-form-item>

        <!-- manual -->
        <template v-else-if="form.behavior === 'manual'">
          <a-form-item label="Initial Value" style="margin-bottom:4px">
            <a-input-number v-model:value="params.value" style="width:100%" :step="0.1" />
          </a-form-item>
          <div style="font-size:11px;color:#aaa">Can be overridden at runtime via "Set Value"</div>
        </template>

        <!-- sine -->
        <template v-else-if="form.behavior === 'sine'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Base">
                <a-input-number v-model:value="params.base" style="width:100%" :step="0.1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Amplitude">
                <a-input-number v-model:value="params.amplitude" style="width:100%" :step="0.1" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Period (hours)" style="margin-bottom:0">
                <a-input-number v-model:value="params.period_hours" style="width:100%" :min="0.1" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Phase (hours)" style="margin-bottom:0">
                <a-input-number v-model:value="params.phase_hours" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>

        <!-- noise -->
        <template v-else-if="form.behavior === 'noise'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Base" style="margin-bottom:0">
                <a-input-number v-model:value="params.base" style="width:100%" :step="0.1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Noise ±" style="margin-bottom:0">
                <a-input-number v-model:value="params.noise" style="width:100%" :min="0" :step="0.1" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>

        <!-- random_walk -->
        <template v-else-if="form.behavior === 'random_walk'">
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Initial Value">
                <a-input-number v-model:value="params.value" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Step ±">
                <a-input-number v-model:value="params.step" style="width:100%" :min="0" :step="0.1" />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row :gutter="12">
            <a-col :span="12">
              <a-form-item label="Min" style="margin-bottom:0">
                <a-input-number v-model:value="params.min" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
            <a-col :span="12">
              <a-form-item label="Max" style="margin-bottom:0">
                <a-input-number v-model:value="params.max" style="width:100%" :step="1" />
              </a-form-item>
            </a-col>
          </a-row>
        </template>
      </div>
    </a-form>

    <template #footer>
      <a-space>
        <a-button @click="emit('update:open', false)">Cancel</a-button>
        <a-button type="primary" :loading="loading" @click="save">
          {{ object ? 'Save' : 'Create' }}
        </a-button>
      </a-space>
    </template>
  </a-drawer>
</template>
