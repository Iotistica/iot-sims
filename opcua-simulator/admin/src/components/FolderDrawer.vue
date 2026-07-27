<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '../api'
import { buildFolderTreeOptions } from '../folderTree'
import type { Folder } from '../types'

const props = defineProps<{
  open: boolean
  folder: Folder | null
  folders: Folder[]
}>()
const emit = defineEmits<{
  'update:open': [v: boolean]
  saved: []
}>()

const loading = ref(false)
const form = reactive({
  name: '',
  description: '',
  parent_folder_id: null as number | null,
})

watch(() => props.open, (v) => {
  if (!v) return
  if (props.folder) {
    Object.assign(form, {
      name: props.folder.name,
      description: props.folder.description ?? '',
      parent_folder_id: props.folder.parent_folder_id,
    })
  } else {
    Object.assign(form, { name: '', description: '', parent_folder_id: null })
  }
})

// Editing an existing folder must not offer itself or its own descendants
// as a parent — that would create a cycle (the backend refuses it too, but
// there's no reason to let the picker offer an option it knows is invalid).
const treeOptions = computed(() => buildFolderTreeOptions(props.folders, props.folder?.id ?? null))

async function save() {
  if (!form.name.trim()) { message.error('Name is required'); return }
  loading.value = true
  const body = { name: form.name, description: form.description, parent_folder_id: form.parent_folder_id }
  try {
    if (props.folder) {
      await api.folders.update(props.folder.id, body)
      message.success('Folder updated')
    } else {
      await api.folders.create(body)
      message.success('Folder created')
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
    :title="folder ? 'Edit Folder' : 'Add Folder'"
    :open="open"
    width="440"
    @close="emit('update:open', false)"
  >
    <a-form layout="vertical" :colon="false">
      <a-form-item label="Name" required>
        <a-input v-model:value="form.name" placeholder="Pumping Station" />
      </a-form-item>

      <a-form-item label="Description">
        <a-input v-model:value="form.description" placeholder="Optional" />
      </a-form-item>

      <a-form-item label="Parent Folder">
        <a-tree-select
          v-model:value="form.parent_folder_id"
          :tree-data="treeOptions"
          allow-clear
          tree-default-expand-all
          placeholder="Root (directly under Objects)"
          style="width: 100%"
        />
      </a-form-item>
    </a-form>

    <template #footer>
      <a-space>
        <a-button @click="emit('update:open', false)">Cancel</a-button>
        <a-button type="primary" :loading="loading" @click="save">
          {{ folder ? 'Save' : 'Create' }}
        </a-button>
      </a-space>
    </template>
  </a-drawer>
</template>
