<script setup lang="ts">
import { ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { DownloadOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import type { Profile } from '../types'
import { api } from '../api'

const emit = defineEmits<{
  'update:open': [val: boolean]
  loaded: [profileId: number, profileName: string, profileDesc: string]
}>()

const props = defineProps<{ open: boolean }>()

const profiles = ref<Profile[]>([])
const loading = ref(false)

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

function confirmLoad(p: Profile) {
  Modal.confirm({
    title: `Load "${p.name}"?`,
    content: 'This will replace all current devices and objects with the saved profile.',
    okType: 'danger',
    okText: 'Load',
    async onOk() {
      try {
        await api.profiles.load(p.id)
        message.success(`"${p.name}" loaded`)
        emit('loaded', p.id, p.name, p.description)
        emit('update:open', false)
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
    title: `Delete "${p.name}"?`,
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

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString()
}

watch(() => props.open, (isOpen) => {
  if (isOpen) load()
})
</script>

<template>
  <a-drawer
    :open="open"
    title="Open Profile"
    width="460"
    @close="emit('update:open', false)"
  >
    <a-spin :spinning="loading">
      <div v-if="!profiles.length && !loading" style="text-align:center;color:#bbb;padding:60px 0;font-size:14px">
        No profiles saved yet
      </div>
      <div
        v-for="p in profiles"
        :key="p.id"
        style="border:1px solid #e8e8e8;border-radius:6px;padding:12px 14px;margin-bottom:10px;background:white"
      >
        <div style="display:flex;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
              {{ p.name }}
            </div>
            <div v-if="p.description" style="font-size:12px;color:#888;margin-top:2px">{{ p.description }}</div>
            <div style="font-size:11px;color:#bbb;margin-top:4px">
              {{ p.device_count }} device{{ p.device_count !== 1 ? 's' : '' }} · {{ fmtDate(p.created_at) }}
            </div>
          </div>
          <a-space :size="4">
            <a-button size="small" type="primary" ghost @click="confirmLoad(p)">Load</a-button>
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
      <a-button @click="emit('update:open', false)">Close</a-button>
    </template>
  </a-drawer>
</template>
