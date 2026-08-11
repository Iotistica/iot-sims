<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { api } from '../api'
import { buildLocationTreeOptions } from '../locationTree'
import type { Equipment, Location, Meta } from '../types'

const props = defineProps<{
  open: boolean
  equipment: Equipment | null
  locations: Location[]
  meta: Meta
  /** Preselects Location when opening for a fresh Add (e.g. invoked from a
   * location row's contextual "+" action) -- ignored when editing existing
   * equipment. */
  defaultLocationId?: number | null
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  saved: []
}>()

const loading = ref(false)
const deleting = ref(false)
const form = reactive({
  name: '',
  description: '',
  location_id: null as number | null,
  equipment_type: null as string | null,
})

watch(() => props.open, (v) => {
  if (!v) return
  if (props.equipment) {
    Object.assign(form, {
      name: props.equipment.name,
      description: props.equipment.description ?? '',
      location_id: props.equipment.location_id ?? null,
      equipment_type: props.equipment.equipment_type ?? null,
    })
  } else {
    Object.assign(form, { name: '', description: '', location_id: props.defaultLocationId ?? null, equipment_type: null })
  }
})

const treeOptions = computed(() => buildLocationTreeOptions(props.locations, null))

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  loading.value = true
  const body = { name: form.name, description: form.description, location_id: form.location_id, equipment_type: form.equipment_type }
  try {
    if (props.equipment) {
      await api.equipment.update(props.equipment.id, body)
      message.success('Equipment updated')
    } else {
      await api.equipment.create(body)
      message.success('Equipment created')
    }
    emit('update:open', false)
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function doDelete() {
  if (!props.equipment) return
  const eq = props.equipment
  Modal.confirm({
    title: `Delete "${eq.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      deleting.value = true
      try {
        await api.equipment.del(eq.id)
        message.success('Equipment deleted')
        emit('update:open', false)
        emit('saved')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete equipment')
      } finally {
        deleting.value = false
      }
    },
  })
}
</script>

<template>
  <a-drawer
    :title="equipment ? 'Edit Equipment' : 'Add Equipment'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Boiler 1" />
      </a-form-item>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Location">
        <a-tree-select
          v-model:value="form.location_id"
          :tree-data="treeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Unassigned"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item label="Semantic Type" help="Describes what this equipment represents in the building.">
        <a-select
          v-model:value="form.equipment_type"
          show-search
          allow-clear
          placeholder="Not classified"
          :options="meta.equipment_types"
        />
      </a-form-item>
    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a-button v-if="equipment" danger :loading="deleting" @click="doDelete">Delete</a-button>
        <div v-else />
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ equipment ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
