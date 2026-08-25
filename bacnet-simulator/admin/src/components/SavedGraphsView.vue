<script setup lang="ts">
/** Dashboard view for saved Custom Graphs: every saved graph renders as a
 * fully editable card via SavedGraphCard.vue (chart, per-series legend/
 * controls, inline rename, add-point, all auto-saving) rather than a
 * table of names needing an "Open" click into a popup just to see the
 * data. "Edit" still opens CustomGraphModal.vue for a bigger/roomier
 * session; New Graph still creates through that same modal.
 */
import { onMounted, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { LineChartOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Meta, PointRow, SavedGraph } from '../types'
import CustomGraphModal from './CustomGraphModal.vue'
import SavedGraphCard from './SavedGraphCard.vue'

const meta = ref<Meta | null>(null)
const graphs = ref<SavedGraph[]>([])
const points = ref<PointRow[]>([])
const loading = ref(false)

const graphModalOpen = ref(false)
const activeGraph = ref<SavedGraph | null>(null)

async function load() {
  loading.value = true
  try {
    const [m, g, p] = await Promise.all([api.meta(), api.customGraphs.list(), api.points.list()])
    meta.value = m
    graphs.value = g
    points.value = p
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

function openNewGraph() {
  activeGraph.value = null
  graphModalOpen.value = true
}

function onGraphSaved() {
  load()
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

onMounted(load)
</script>

<template>
  <div style="height:100%;padding:20px;overflow:auto">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <h2 style="margin:0;font-size:16px">Saved Graphs</h2>
      <div style="flex:1" />
      <a-button type="primary" @click="openNewGraph">
        <template #icon><PlusOutlined /></template>
        New Graph
      </a-button>
    </div>

    <div v-if="loading" style="padding:24px;text-align:center">
      <a-spin />
    </div>
    <div v-else-if="!graphs.length" style="padding:24px;color:var(--text-placeholder);text-align:center">
      <LineChartOutlined style="font-size:20px;margin-bottom:8px;display:block" />
      No saved graphs yet — build one from any point's graph icon, then "Save Graph".
    </div>
    <div v-else class="saved-graphs-grid">
      <SavedGraphCard
        v-for="graph in graphs"
        :key="graph.id"
        :graph="graph"
        :points="points"
        :meta="meta!"
        @edit="openGraph(graph)"
        @delete="confirmDelete(graph)"
        @saved="onGraphSaved"
      />
    </div>

    <CustomGraphModal
      v-if="meta"
      v-model:open="graphModalOpen"
      :meta="meta"
      :saved-graph="activeGraph"
      @saved="onGraphSaved"
    />
  </div>
</template>

<style scoped>
.saved-graphs-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 12px;
}
</style>
