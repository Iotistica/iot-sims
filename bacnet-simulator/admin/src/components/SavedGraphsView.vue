<script setup lang="ts">
/** List view for saved Custom Graphs -- mirrors
 * functional-tests/FunctionalTestsView.vue's list (table + row actions,
 * Modal.confirm for delete), but "Open" launches CustomGraphModal.vue
 * directly rather than switching to a full-page builder mode: the
 * "builder" for this feature already IS that modal, shared with
 * ObjectsPanel.vue's graph-icon entry point. Rename is a lightweight
 * name-only prompt (no true inline-rename pattern exists elsewhere in
 * this app) -- it resubmits the same definition unchanged via the same
 * update endpoint, so it doesn't need the full modal.
 */
import { onMounted, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { EditOutlined, DeleteOutlined, LineChartOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Meta, SavedGraph } from '../types'
import CustomGraphModal from './CustomGraphModal.vue'

const meta = ref<Meta | null>(null)
const graphs = ref<SavedGraph[]>([])
const loading = ref(false)

const graphModalOpen = ref(false)
const activeGraph = ref<SavedGraph | null>(null)

const renameModalOpen = ref(false)
const renameTarget = ref<SavedGraph | null>(null)
const renameInput = ref('')
const renaming = ref(false)

const columns = [
  { title: 'Name', dataIndex: 'name', key: 'name' },
  { title: 'Points', key: 'points', width: 90 },
  { title: 'Updated', key: 'updated_at', width: 180 },
  { title: '', key: 'actions', width: 90 },
]

async function load() {
  loading.value = true
  try {
    const [m, g] = await Promise.all([api.meta(), api.customGraphs.list()])
    meta.value = m
    graphs.value = g
  } catch {
    message.error('Failed to load saved graphs')
  } finally {
    loading.value = false
  }
}

function openGraph(graph: SavedGraph) {
  activeGraph.value = graph
  graphModalOpen.value = true
}

function onGraphSaved() {
  load()
}

function openRename(graph: SavedGraph) {
  renameTarget.value = graph
  renameInput.value = graph.name
  renameModalOpen.value = true
}

async function doRename() {
  const target = renameTarget.value
  const name = renameInput.value.trim()
  if (!target || !name) {
    message.error('Name is required')
    return
  }
  renaming.value = true
  try {
    await api.customGraphs.update(target.id, { name, definition: target.definition })
    message.success('Graph renamed')
    renameModalOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message || 'Failed to rename graph')
  } finally {
    renaming.value = false
  }
}

function confirmDelete(graph: SavedGraph) {
  Modal.confirm({
    title: `Delete "${graph.name}"?`,
    content: 'This cannot be undone.',
    okType: 'danger',
    onOk: async () => {
      await api.customGraphs.del(graph.id)
      message.success('Graph deleted')
      await load()
    },
  })
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString()
}

onMounted(load)
</script>

<template>
  <div style="height:100%;padding:20px;overflow:auto">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <h2 style="margin:0;font-size:16px">Saved Graphs</h2>
    </div>

    <a-table
      :columns="columns"
      :data-source="graphs"
      :loading="loading"
      :show-sorter-tooltip="false"
      row-key="id"
      size="small"
      :pagination="{ pageSize: 25 }"
      :custom-row="(record: SavedGraph) => ({ onClick: () => openGraph(record), style: 'cursor:pointer' })"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'points'">
          {{ (record as SavedGraph).definition.series.length }}
        </template>
        <template v-else-if="column.key === 'updated_at'">
          {{ fmtDate((record as SavedGraph).updated_at) }}
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="2">
            <a-button size="small" type="text" title="Rename" @click.stop="openRename(record as SavedGraph)">
              <template #icon><EditOutlined /></template>
            </a-button>
            <a-button size="small" type="text" danger title="Delete" @click.stop="confirmDelete(record as SavedGraph)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-space>
        </template>
      </template>
      <template #emptyText>
        <div style="padding:24px;color:var(--text-placeholder)">
          <LineChartOutlined style="font-size:20px;margin-bottom:8px;display:block" />
          No saved graphs yet — build one from any point's graph icon, then "Save Graph".
        </div>
      </template>
    </a-table>

    <CustomGraphModal
      v-if="meta"
      v-model:open="graphModalOpen"
      :meta="meta"
      :saved-graph="activeGraph"
      @saved="onGraphSaved"
    />

    <a-modal
      v-model:open="renameModalOpen"
      title="Rename Graph"
      ok-text="Save"
      :confirm-loading="renaming"
      @ok="doRename"
    >
      <a-input
        v-model:value="renameInput"
        placeholder="Graph name"
        :maxlength="100"
        @pressEnter="doRename"
      />
    </a-modal>
  </div>
</template>
