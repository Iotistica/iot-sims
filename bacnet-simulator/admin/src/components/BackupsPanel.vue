<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { DownloadOutlined, DeleteOutlined, UploadOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import type { BackupEntry } from '../types'
import { api } from '../api'

const backups = ref<BackupEntry[]>([])
const loading = ref(false)
const creating = ref(false)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement>()

const columns = [
  { title: 'File name', dataIndex: 'file_name', key: 'file_name' },
  { title: 'Created', key: 'created_at', width: 170 },
  { title: 'Size', key: 'size_bytes', width: 90 },
  { title: 'Checksum', key: 'checksum_sha256', width: 120 },
  { title: '', key: 'actions', width: 140 },
]

async function load() {
  loading.value = true
  try {
    backups.value = await api.backups.list()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to load backups')
  } finally {
    loading.value = false
  }
}

async function doCreate() {
  creating.value = true
  try {
    await api.backups.create()
    message.success('Backup created')
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Failed to create backup')
  } finally {
    creating.value = false
  }
}

function doDownload(b: BackupEntry) {
  api.backups.download(b.file_name).catch((e: unknown) => {
    message.error((e as Error).message ?? 'Download failed')
  })
}

function doRestore(b: BackupEntry) {
  Modal.confirm({
    title: `Restore "${b.file_name}"?`,
    content: 'This will replace the current database with this backup. A pre-restore snapshot of the current database is created automatically first, and the simulator reloads immediately afterward — no restart needed.',
    okType: 'danger',
    okText: 'Restore',
    async onOk() {
      try {
        const result = await api.backups.restore(b.file_name)
        message.success(`Restored — pre-restore snapshot saved as "${result.pre_restore_backup}"`)
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Restore failed')
      }
    },
  })
}

function doDelete(b: BackupEntry) {
  Modal.confirm({
    title: `Delete "${b.file_name}"?`,
    okType: 'danger',
    okText: 'Delete',
    async onOk() {
      try {
        await api.backups.del(b.file_name)
        message.success('Backup deleted')
        await load()
      } catch (e: unknown) {
        message.error((e as Error).message ?? 'Failed to delete backup')
      }
    },
  })
}

async function onFileChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  ;(e.target as HTMLInputElement).value = ''
  if (!file) return
  uploading.value = true
  try {
    await api.backups.upload(file)
    message.success(`"${file.name}" uploaded`)
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message ?? 'Upload failed')
  } finally {
    uploading.value = false
  }
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString()
}

onMounted(load)
</script>

<template>
  <a-spin :spinning="loading">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div style="font-size:12px;color:var(--text-muted);max-width:520px">
        Backups are stored under <code>DATA_DIR/backups/db</code>. Restoring replaces the live database — a
        pre-restore snapshot is taken automatically, and the simulator reloads on its own; no restart needed.
      </div>
      <a-space>
        <a-button size="small" @click="load">
          <template #icon><ReloadOutlined /></template>
        </a-button>
        <input ref="fileInput" type="file" accept=".db,.sqlite" style="display:none" @change="onFileChange" />
        <a-button size="small" :loading="uploading" @click="fileInput?.click()">
          <template #icon><UploadOutlined /></template>
          Upload Backup
        </a-button>
        <a-button type="primary" size="small" :loading="creating" @click="doCreate">Create Backup</a-button>
      </a-space>
    </div>

    <a-table
      :columns="columns"
      :data-source="backups"
      :pagination="false"
      size="small"
      row-key="file_name"
      :locale="{ emptyText: 'No backups yet — click Create Backup to take a snapshot of the current database.' }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'created_at'">
          {{ fmtDate((record as BackupEntry).created_at) }}
        </template>
        <template v-else-if="column.key === 'size_bytes'">
          {{ fmtSize((record as BackupEntry).size_bytes) }}
        </template>
        <template v-else-if="column.key === 'checksum_sha256'">
          <a-tooltip :title="(record as BackupEntry).checksum_sha256">
            <span class="mono-text">{{ (record as BackupEntry).checksum_sha256.slice(0, 10) }}…</span>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4">
            <a-button size="small" title="Download" @click="doDownload(record as BackupEntry)">
              <template #icon><DownloadOutlined /></template>
            </a-button>
            <a-button size="small" type="primary" ghost @click="doRestore(record as BackupEntry)">Restore</a-button>
            <a-button size="small" danger title="Delete" @click="doDelete(record as BackupEntry)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-space>
        </template>
      </template>
    </a-table>
  </a-spin>
</template>

<style scoped>
.mono-text {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
