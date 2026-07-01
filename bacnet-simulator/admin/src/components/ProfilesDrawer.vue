<script setup lang="ts">
import { ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { SaveOutlined, DownloadOutlined, UploadOutlined, DeleteOutlined, BuildOutlined } from '@ant-design/icons-vue'
import type { Profile } from '../types'
import { api } from '../api'
import ProfileBuilderDrawer from './ProfileBuilderDrawer.vue'

const emit = defineEmits<{
  'update:open': [val: boolean]
  loaded: []
}>()

const props = defineProps<{ open: boolean }>()

const profiles = ref<Profile[]>([])
const loading = ref(false)

// Save form
const saveName = ref('')
const saveDesc = ref('')
const saving = ref(false)

// Import
const importLoading = ref(false)

// Builder
const builderOpen = ref(false)

async function load() {
  loading.value = true
  try {
    profiles.value = await api.profiles.list()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load profiles')
  } finally {
    loading.value = false
  }
}

async function saveProfile() {
  if (!saveName.value.trim()) return
  saving.value = true
  try {
    await api.profiles.save(saveName.value.trim(), saveDesc.value.trim())
    message.success(`Profile "${saveName.value.trim()}" saved`)
    saveName.value = ''
    saveDesc.value = ''
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to save profile')
  } finally {
    saving.value = false
  }
}

function confirmLoad(p: Profile) {
  Modal.confirm({
    title: `Load "${p.name}"?`,
    content: 'This will replace ALL current devices and objects with the saved profile. This cannot be undone.',
    okType: 'danger',
    okText: 'Load Profile',
    async onOk() {
      try {
        await api.profiles.load(p.id)
        message.success(`Profile "${p.name}" loaded`)
        emit('loaded')
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to load profile')
      }
    },
  })
}

function exportProfile(p: Profile) {
  window.open(`/profiles/${p.id}/export`, '_blank')
}

function confirmDelete(p: Profile) {
  Modal.confirm({
    title: `Delete profile "${p.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.profiles.del(p.id)
        message.success('Profile deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete profile')
      }
    },
  })
}

function importFromFile() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json,application/json'
  input.onchange = async () => {
    const file = input.files?.[0]
    if (!file) return
    importLoading.value = true
    try {
      const text = await file.text()
      const data = JSON.parse(text)
      const name = file.name.replace(/\.json$/i, '').replace(/_/g, ' ')
      await api.profiles.import_(name, '', data)
      message.success(`Imported as "${name}"`)
      await load()
    } catch (e: unknown) {
      message.error((e as Error).message ?? 'Invalid JSON file')
    } finally {
      importLoading.value = false
    }
  }
  input.click()
}

function close() {
  emit('update:open', false)
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString()
}

watch(() => props.open, (isOpen) => {
  if (isOpen) load()
})
</script>

<template>
  <ProfileBuilderDrawer
    v-model:open="builderOpen"
    @saved="load"
  />

  <a-drawer
    :open="open"
    title="Profiles"
    width="500"
    @close="close"
  >
    <!-- Build from scratch -->
    <div style="margin-bottom: 16px">
      <a-button block @click="builderOpen = true">
        <template #icon><BuildOutlined /></template>
        Build New Profile from Scratch
      </a-button>
    </div>

    <a-divider style="margin: 0 0 20px; font-size: 12px; color: #bbb">or save the current live setup</a-divider>

    <!-- Save current config -->
    <div style="margin-bottom: 24px; padding: 16px; background: #fafafa; border-radius: 6px; border: 1px solid #e8e8e8">
      <div style="font-size: 12px; font-weight: 600; color: #666; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px">
        Save Current Configuration
      </div>
      <a-input
        v-model:value="saveName"
        placeholder="Profile name"
        style="margin-bottom: 8px"
        @pressEnter="saveProfile"
      />
      <a-input
        v-model:value="saveDesc"
        placeholder="Description (optional)"
        style="margin-bottom: 10px"
      />
      <div style="display: flex; gap: 8px">
        <a-button
          type="primary"
          :loading="saving"
          :disabled="!saveName.trim()"
          @click="saveProfile"
        >
          <template #icon><SaveOutlined /></template>
          Save Profile
        </a-button>
        <a-button :loading="importLoading" @click="importFromFile">
          <template #icon><UploadOutlined /></template>
          Import JSON
        </a-button>
      </div>
    </div>

    <!-- Profile list -->
    <a-spin :spinning="loading">
      <div v-if="!profiles.length && !loading" style="text-align: center; color: #bbb; padding: 40px 0; font-size: 14px">
        No profiles saved yet
      </div>
      <div
        v-for="p in profiles"
        :key="p.id"
        style="border: 1px solid #e8e8e8; border-radius: 6px; padding: 12px 14px; margin-bottom: 10px; background: white"
      >
        <div style="display: flex; align-items: flex-start; gap: 8px">
          <div style="flex: 1; min-width: 0">
            <div style="font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">
              {{ p.name }}
            </div>
            <div v-if="p.description" style="font-size: 12px; color: #888; margin-top: 2px">{{ p.description }}</div>
            <div style="font-size: 11px; color: #bbb; margin-top: 4px">
              {{ p.device_count }} device{{ p.device_count !== 1 ? 's' : '' }} · {{ fmtDate(p.created_at) }}
            </div>
          </div>
          <a-space :size="4">
            <a-button size="small" type="primary" ghost title="Load this profile" @click="confirmLoad(p)">
              Load
            </a-button>
            <a-button size="small" title="Export as JSON" @click="exportProfile(p)">
              <template #icon><DownloadOutlined /></template>
            </a-button>
            <a-button size="small" danger title="Delete" @click="confirmDelete(p)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-space>
        </div>
      </div>
    </a-spin>

    <template #footer>
      <a-button @click="close">Close</a-button>
    </template>
  </a-drawer>
</template>
