<script setup lang="ts">
/** Dashboard view: a named Dashboard holds several widgets (Chart or KPI
 * Tile -- see migration 28's docstring for why widgets live on the
 * existing SavedGraph/custom_graphs entity rather than a new one) arranged
 * on a user-draggable/resizable grid (grid-layout-plus). Every widget
 * renders via SavedGraphCard.vue (chart or kpi body, fully editable,
 * auto-saving). "Edit" still opens CustomGraphModal.vue for a bigger/
 * roomier session; New Widget still creates through that same modal.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { GridLayout, GridItem } from 'grid-layout-plus'
import { LineChartOutlined, PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import type { Dashboard, Meta, PointRow, SavedGraph } from '../types'
import CustomGraphModal from './CustomGraphModal.vue'
import SavedGraphCard from './SavedGraphCard.vue'

const GRID_COL_NUM = 12
const GRID_ROW_HEIGHT = 30

const meta = ref<Meta | null>(null)
const points = ref<PointRow[]>([])
const dashboards = ref<Dashboard[]>([])
const activeDashboardId = ref<number | null>(null)
const graphs = ref<SavedGraph[]>([])
const loading = ref(false)

const graphModalOpen = ref(false)
const activeGraph = ref<SavedGraph | null>(null)

interface LayoutItem { i: number; x: number; y: number; w: number; h: number }
const layout = ref<LayoutItem[]>([])

function syncLayoutFromGraphs() {
  layout.value = graphs.value.map(g => ({ i: g.id, x: g.grid_x, y: g.grid_y, w: g.grid_w, h: g.grid_h }))
}

async function loadGraphs() {
  if (activeDashboardId.value == null) {
    graphs.value = []
    syncLayoutFromGraphs()
    return
  }
  loading.value = true
  try {
    graphs.value = await api.customGraphs.list(activeDashboardId.value)
    syncLayoutFromGraphs()
  } catch {
    message.error('Failed to load widgets')
  } finally {
    loading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    const [m, d, p] = await Promise.all([api.meta(), api.dashboards.list(), api.points.list()])
    meta.value = m
    dashboards.value = d
    points.value = p
    if (activeDashboardId.value == null || !d.some(x => x.id === activeDashboardId.value)) {
      activeDashboardId.value = d[0]?.id ?? null
    }
  } catch {
    message.error('Failed to load dashboards')
  } finally {
    loading.value = false
  }
  await loadGraphs()
}

const dashboardOptions = computed(() => dashboards.value.map(d => ({ label: d.name, value: d.id })))

// ─── Dashboard create/rename/delete ────────────────────────────────────
// One small modal covers both create (editingDashboard=null) and rename
// (editingDashboard set) -- same "one dialog, two purposes" shape
// CustomGraphModal.vue already uses for its own name field.

const dashboardModalOpen = ref(false)
const editingDashboard = ref<Dashboard | null>(null)
const dashboardNameInput = ref('')
const savingDashboard = ref(false)

function openNewDashboard() {
  editingDashboard.value = null
  dashboardNameInput.value = ''
  dashboardModalOpen.value = true
}

function openRenameDashboard() {
  const current = dashboards.value.find(d => d.id === activeDashboardId.value)
  if (!current) return
  editingDashboard.value = current
  dashboardNameInput.value = current.name
  dashboardModalOpen.value = true
}

async function saveDashboard() {
  const name = dashboardNameInput.value.trim()
  if (!name) {
    message.error('Name is required')
    return
  }
  savingDashboard.value = true
  try {
    if (editingDashboard.value) {
      await api.dashboards.update(editingDashboard.value.id, { name })
    } else {
      const created = await api.dashboards.create({ name })
      activeDashboardId.value = created.id
    }
    dashboardModalOpen.value = false
    await load()
  } catch (e: unknown) {
    message.error((e as Error).message || 'Failed to save dashboard')
  } finally {
    savingDashboard.value = false
  }
}

function confirmDeleteDashboard() {
  const current = dashboards.value.find(d => d.id === activeDashboardId.value)
  if (!current) return
  Modal.confirm({
    title: `Delete "${current.name}"?`,
    content: `This also deletes every widget on it. This cannot be undone.`,
    okType: 'danger',
    onOk: async () => {
      await api.dashboards.del(current.id)
      message.success('Dashboard deleted')
      activeDashboardId.value = null
      await load()
    },
  })
}

// ─── Widgets ────────────────────────────────────────────────────────────

function openGraph(graph: SavedGraph) {
  activeGraph.value = graph
  graphModalOpen.value = true
}

// A new widget's default position -- appended below everything already on
// this dashboard (grid-layout-plus's own vertical compaction then settles
// it into place even if this overestimates), same idea as iot-dash's own
// `y: Infinity` convention.
const nextGridY = computed(() => layout.value.reduce((max, item) => Math.max(max, item.y + item.h), 0))

function openNewGraph() {
  activeGraph.value = null
  graphModalOpen.value = true
}

function onGraphSaved() {
  loadGraphs()
}

function confirmDelete(graph: SavedGraph) {
  Modal.confirm({
    title: `Delete "${graph.name}"?`,
    content: 'This cannot be undone.',
    okType: 'danger',
    onOk: async () => {
      await api.customGraphs.del(graph.id)
      message.success('Widget deleted')
      await loadGraphs()
    },
  })
}

// ─── Layout persistence ─────────────────────────────────────────────────
// `moved`/`resized` fire once, at the end of a drag/resize gesture (unlike
// `move`/`resize`, fired continuously mid-gesture) -- no debounce needed,
// this is already a "gesture finished" signal. name/definition/
// dashboard_id/widget_type are resent verbatim from the graph's own
// current state (CustomGraphUpdate's own comment: omitting them would mean
// "unchanged", but resending them is just as correct and self-documenting
// here since this handler already has the full object in hand).
function persistGeometry(graphId: number, x: number, y: number, w: number, h: number) {
  const graph = graphs.value.find(g => g.id === graphId)
  if (!graph) return
  void api.customGraphs.update(graphId, {
    name: graph.name,
    definition: graph.definition,
    dashboard_id: graph.dashboard_id,
    widget_type: graph.widget_type,
    grid_x: x, grid_y: y, grid_w: w, grid_h: h,
  }).catch(() => message.error('Failed to save widget position'))
}

function onItemMoved(i: number | string, x: number, y: number) {
  const item = layout.value.find(it => it.i === i)
  persistGeometry(Number(i), x, y, item?.w ?? 6, item?.h ?? 4)
}

// grid-layout-plus's own "resized" event args are (i, h, w, heightPx,
// widthPx) -- NOT x/y (a resize never moves the item's own origin, only
// its size) -- confirmed directly against its source
// (grid-item.vue.mjs: `M("resized", i.i, a.h, a.w, ...)`). x/y for the
// persisted geometry come from this card's own last-known layout entry.
function onItemResized(i: number | string, h: number, w: number) {
  const item = layout.value.find(it => it.i === i)
  persistGeometry(Number(i), item?.x ?? 0, item?.y ?? 0, w, h)
}

watch(activeDashboardId, loadGraphs)

onMounted(load)
</script>

<template>
  <div style="height:100%;padding:20px;overflow:auto">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <h2 style="margin:0;font-size:16px">Dashboards</h2>
      <a-select
        v-model:value="activeDashboardId"
        :options="dashboardOptions"
        style="width:220px"
        placeholder="No dashboards yet"
      />
      <a-button size="small" title="Rename dashboard" :disabled="!activeDashboardId" @click="openRenameDashboard">
        <template #icon><EditOutlined /></template>
      </a-button>
      <a-button size="small" danger title="Delete dashboard" :disabled="!activeDashboardId" @click="confirmDeleteDashboard">
        <template #icon><DeleteOutlined /></template>
      </a-button>
      <a-button size="small" @click="openNewDashboard">
        <template #icon><PlusOutlined /></template>
        New Dashboard
      </a-button>
      <div style="flex:1" />
      <a-button type="primary" :disabled="!activeDashboardId" @click="openNewGraph">
        <template #icon><PlusOutlined /></template>
        New Widget
      </a-button>
    </div>

    <div v-if="loading" style="padding:24px;text-align:center">
      <a-spin />
    </div>
    <div v-else-if="!dashboards.length" style="padding:24px;color:var(--text-placeholder);text-align:center">
      <LineChartOutlined style="font-size:20px;margin-bottom:8px;display:block" />
      No dashboards yet — click "New Dashboard" to create one.
    </div>
    <div v-else-if="!graphs.length" style="padding:24px;color:var(--text-placeholder);text-align:center">
      <LineChartOutlined style="font-size:20px;margin-bottom:8px;display:block" />
      No widgets on this dashboard yet — click "New Widget" above.
    </div>
    <GridLayout
      v-else
      v-model:layout="layout"
      :col-num="GRID_COL_NUM"
      :row-height="GRID_ROW_HEIGHT"
      :margin="[12, 12]"
      :is-draggable="true"
      :is-resizable="true"
      :vertical-compact="true"
      :use-css-transforms="true"
    >
      <GridItem
        v-for="item in layout"
        :key="item.i"
        :x="item.x"
        :y="item.y"
        :w="item.w"
        :h="item.h"
        :i="item.i"
        drag-allow-from=".widget-drag-handle"
        @moved="onItemMoved"
        @resized="onItemResized"
      >
        <SavedGraphCard
          v-if="graphs.find(g => g.id === item.i)"
          :graph="graphs.find(g => g.id === item.i)!"
          :points="points"
          :meta="meta!"
          @edit="openGraph(graphs.find(g => g.id === item.i)!)"
          @delete="confirmDelete(graphs.find(g => g.id === item.i)!)"
          @saved="onGraphSaved"
        />
      </GridItem>
    </GridLayout>

    <CustomGraphModal
      v-if="meta"
      v-model:open="graphModalOpen"
      :meta="meta"
      :saved-graph="activeGraph"
      :dashboard-id="activeDashboardId"
      :default-grid-x="0"
      :default-grid-y="nextGridY"
      @saved="onGraphSaved"
    />

    <a-modal
      :open="dashboardModalOpen"
      :title="editingDashboard ? 'Rename Dashboard' : 'New Dashboard'"
      :confirm-loading="savingDashboard"
      @ok="saveDashboard"
      @cancel="dashboardModalOpen = false"
    >
      <a-input
        v-model:value="dashboardNameInput"
        placeholder="Dashboard name"
        :maxlength="100"
        @pressEnter="saveDashboard"
      />
    </a-modal>
  </div>
</template>
