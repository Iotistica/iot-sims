<script setup lang="ts">
import { ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { DownloadOutlined, DeleteOutlined, UploadOutlined } from '@ant-design/icons-vue'
import type { Project, ProjectSourceType, BACnetConnectionConfig, ModelingMode } from '../types'
import { api } from '../api'
import ImportProjectModal from './ImportProjectModal.vue'

const emit = defineEmits<{
  'update:open': [val: boolean]
  loaded: [
    projectId: number, projectName: string, projectDesc: string,
    sourceType: ProjectSourceType, connectionConfig: BACnetConnectionConfig | null,
    modelingMode: ModelingMode,
  ]
}>()

const props = defineProps<{ open: boolean }>()

const projects = ref<Project[]>([])
const loading = ref(false)
const importOpen = ref(false)

async function load() {
  loading.value = true
  try {
    projects.value = await api.projects.list()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load projects')
  } finally {
    loading.value = false
  }
}

async function loadProject(p: Project) {
  try {
    const result = await api.projects.load(p.id)
    message.success(`"${p.name}" loaded`)
    emit('loaded', p.id, p.name, p.description, result.source_type ?? 'simulated', result.connection_config ?? null, result.modeling_mode ?? 'standard')
    emit('update:open', false)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load project')
  }
}

function exportProject(p: Project) {
  window.open(`/profiles/${p.id}/export`, '_blank')
}

async function exportProjectEde(p: Project) {
  try {
    await api.projects.exportEde(p.id, p.name)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Export failed')
  }
}

async function exportProjectBrick(p: Project) {
  try {
    await api.projects.exportBrick(p.id, p.name)
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Export failed')
  }
}

function confirmDelete(p: Project) {
  Modal.confirm({
    title: `Delete "${p.name}"?`,
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.projects.del(p.id)
        message.success('Project deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete project')
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
    title="Open Project"
    width="460"
    @close="emit('update:open', false)"
  >
    <template #extra>
      <a-button size="small" @click="importOpen = true">
        <template #icon><UploadOutlined /></template>
        Import
      </a-button>
    </template>

    <a-spin :spinning="loading">
      <div v-if="!projects.length && !loading" style="text-align:center;color:var(--text-placeholder);padding:60px 0;font-size:14px">
        No projects saved yet
      </div>
      <div
        v-for="p in projects"
        :key="p.id"
        style="border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:10px;background:var(--surface-alt)"
      >
        <div style="display:flex;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text-primary)">
              {{ p.name }}
            </div>
            <div v-if="p.description" style="font-size:12px;color:var(--text-muted);margin-top:2px">{{ p.description }}</div>
            <div style="font-size:11px;color:var(--text-placeholder);margin-top:4px">
              {{ p.device_count }} device{{ p.device_count !== 1 ? 's' : '' }} · {{ fmtDate(p.created_at) }}
            </div>
          </div>
          <a-space :size="4">
            <a-button size="small" type="primary" ghost @click="loadProject(p)">Load</a-button>
            <a-button size="small" title="Export as JSON" @click="exportProject(p)">
              <template #icon><DownloadOutlined /></template>
            </a-button>
            <a-button size="small" title="Export as EDE" @click="exportProjectEde(p)">EDE</a-button>
            <a-button size="small" title="Export as Brick Schema (.ttl)" @click="exportProjectBrick(p)">Brick</a-button>
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

  <ImportProjectModal v-model:open="importOpen" @imported="load" />
</template>
