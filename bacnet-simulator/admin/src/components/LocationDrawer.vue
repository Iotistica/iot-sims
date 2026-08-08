<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { api } from '../api'
import { buildLocationTreeOptions } from '../locationTree'
import type { Location, Meta } from '../types'

const props = defineProps<{
  open: boolean
  location: Location | null
  locations: Location[]
  meta: Meta
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
  parent_location_id: null as number | null,
  kind: null as string | null,
})

watch(() => props.open, (v) => {
  if (!v) return
  if (props.location) {
    Object.assign(form, {
      name: props.location.name,
      description: props.location.description ?? '',
      parent_location_id: props.location.parent_location_id,
      kind: props.location.kind ?? null,
    })
  } else {
    Object.assign(form, { name: '', description: '', parent_location_id: null, kind: null })
  }
})

// Editing an existing location must not offer itself or its own descendants
// as a parent — that would create a cycle (the backend refuses it too, but
// there's no reason to let the picker offer an option it knows is invalid).
const treeOptions = computed(() => buildLocationTreeOptions(props.locations, props.location?.id ?? null))

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  loading.value = true
  const body = { name: form.name, description: form.description, parent_location_id: form.parent_location_id, kind: form.kind }
  try {
    if (props.location) {
      await api.locations.update(props.location.id, body)
      message.success('Location updated')
    } else {
      await api.locations.create(body)
      message.success('Location created')
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
  if (!props.location) return
  const loc = props.location
  Modal.confirm({
    title: `Delete "${loc.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    onOk: async () => {
      deleting.value = true
      try {
        await api.locations.del(loc.id)
        message.success('Location deleted')
        emit('update:open', false)
        emit('saved')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete location')
      } finally {
        deleting.value = false
      }
    },
  })
}
</script>

<template>
  <a-drawer
    :title="location ? 'Edit Location' : 'Add Location'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Building A" />
      </a-form-item>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Parent Location">
        <a-tree-select
          v-model:value="form.parent_location_id"
          :tree-data="treeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Top level"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item label="Brick Class" help="Semantic classification (Brick is the source of truth — assigning it here automatically creates/updates this location's semantic entity, no separate step needed in the Semantic Model panel).">
        <a-select
          v-model:value="form.kind"
          show-search
          allow-clear
          placeholder="Not classified"
          :options="meta.location_kinds"
        />
      </a-form-item>
    </a-form>

    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <a-button v-if="location" danger :loading="deleting" @click="doDelete">Delete</a-button>
        <div v-else />
        <a-space>
          <a-button @click="emit('update:open', false)">Cancel</a-button>
          <a-button type="primary" :loading="loading" @click="save">
            {{ location ? 'Save' : 'Create' }}
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>
