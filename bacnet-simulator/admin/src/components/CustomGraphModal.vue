<script setup lang="ts">
/** Extends the old single-point History modal (previously inline in
 * ObjectsPanel.vue, backed by HistoryChart.vue's single-series SVG) into a
 * reusable multi-point overlay: "+ Add Point" from any device, per-series
 * toggle/remove, automatic left/right axis grouping by unit, and
 * Save/reopen via the custom_graphs backend. HistoryChart.vue itself is
 * untouched -- TrendLogDrawer.vue still uses it for persisted trend-log
 * records, a different data source than this modal's live /history calls.
 *
 * Two entry points, mutually exclusive:
 *  - `initial-point`: ObjectsPanel.vue's graph icon -- seeds one series.
 *  - `saved-graph`: SavedGraphsView.vue's "Open" -- hydrates every series
 *    from the saved definition, re-resolving device/point/unit info live
 *    against /points (never trusted from the saved blob) so a renamed
 *    point or a stale label never shows, and a deleted point is skipped
 *    with a warning instead of breaking the modal.
 */
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  Chart as ChartJS,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
} from 'chart.js'
import { Line } from 'vue-chartjs'
import { CloseOutlined, SaveOutlined } from '@ant-design/icons-vue'
import { isDark } from '../theme'
import { api } from '../api'
import type { CustomGraphDefinition, CustomGraphSeries, HistoryPoint, Meta, PointRef, PointRow, SavedGraph } from '../types'
import PointPicker from './PointPicker.vue'

ChartJS.register(LinearScale, PointElement, LineElement, Tooltip, Legend)

const CHART_COLORS = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1', '#13c2c2', '#eb2f96', '#a0d911']

const props = defineProps<{
  open: boolean
  meta: Meta
  initialPoint?: PointRef | null
  savedGraph?: SavedGraph | null
}>()
const emit = defineEmits<{ 'update:open': [boolean]; saved: [] }>()

interface GraphSeriesState extends CustomGraphSeries {
  device_name: string
  name: string
  units: string
  object_type: string
  data: HistoryPoint[]
  loading: boolean
}

const allPoints = ref<PointRow[]>([])
const loadingPoints = ref(false)
const series = ref<GraphSeriesState[]>([])
const graphName = ref('')
const savedGraphId = ref<number | null>(null)

function unitsLabel(row: PointRow): string {
  return row.units ?? 'no-units'
}

function axisForUnits(units: string): 'left' | 'right' {
  const distinctUnits: string[] = []
  for (const s of series.value) {
    if (!distinctUnits.includes(s.units)) distinctUnits.push(s.units)
  }
  if (distinctUnits.length === 0 || distinctUnits[0] === units) return 'left'
  return 'right'
}

async function fetchHistoryFor(entry: GraphSeriesState): Promise<void> {
  entry.loading = true
  try {
    entry.data = await api.objects.history(entry.device_id, entry.object_id)
  } catch {
    entry.data = []
  } finally {
    entry.loading = false
  }
}

function addSeriesFromRow(row: PointRow, overrides?: Partial<CustomGraphSeries>) {
  const units = unitsLabel(row)
  const entry: GraphSeriesState = {
    device_id: row.device_id,
    object_id: row.object_id,
    color: overrides?.color ?? CHART_COLORS[series.value.length % CHART_COLORS.length],
    axis: overrides?.axis ?? axisForUnits(units),
    visible: overrides?.visible ?? true,
    device_name: row.device_name,
    name: row.name,
    units,
    object_type: row.object_type,
    data: [],
    loading: false,
  }
  series.value.push(entry)
  // Fetch/mutate through the reactive proxy Vue wraps around the pushed
  // object, not the raw `entry` reference above -- mutating the raw
  // object bypasses the proxy's set trap, so entry.data/entry.loading
  // change in memory but never notify the chartData computed to
  // re-render (the chart then only updates on some unrelated reactive
  // trigger, e.g. toggling a series' axis).
  void fetchHistoryFor(series.value[series.value.length - 1])
}

function onPick(picked: PointRef | null) {
  if (!picked) return
  if (series.value.some(s => s.device_id === picked.device_id && s.object_id === picked.object_id)) {
    message.warning('That point is already on this graph')
    return
  }
  const row = allPoints.value.find(p => p.device_id === picked.device_id && p.object_id === picked.object_id)
  if (!row) {
    message.error('Point not found')
    return
  }
  addSeriesFromRow(row)
}

function removeSeries(index: number) {
  series.value.splice(index, 1)
}

function onAxisChange(entry: GraphSeriesState, e: { target: { value: string } }) {
  entry.axis = e.target.value === 'right' ? 'right' : 'left'
}

watch(
  () => props.open,
  async (isOpen) => {
    if (!isOpen) return
    series.value = []
    graphName.value = ''
    savedGraphId.value = null

    loadingPoints.value = true
    try {
      allPoints.value = await api.points.list()
    } catch {
      allPoints.value = []
    } finally {
      loadingPoints.value = false
    }

    if (props.savedGraph) {
      graphName.value = props.savedGraph.name
      savedGraphId.value = props.savedGraph.id
      let missingCount = 0
      for (const s of props.savedGraph.definition.series) {
        const row = allPoints.value.find(p => p.device_id === s.device_id && p.object_id === s.object_id)
        if (!row) {
          missingCount += 1
          continue
        }
        addSeriesFromRow(row, { color: s.color, axis: s.axis, visible: s.visible })
      }
      if (missingCount > 0) {
        message.warning(`${missingCount} saved point${missingCount === 1 ? '' : 's'} no longer exist and ${missingCount === 1 ? 'was' : 'were'} skipped`)
      }
    } else if (props.initialPoint) {
      const row = allPoints.value.find(p => p.device_id === props.initialPoint!.device_id && p.object_id === props.initialPoint!.object_id)
      if (row) addSeriesFromRow(row)
    }
  },
  { immediate: true },
)

// ─── Chart ──────────────────────────────────────────────────────────────

const chartData = computed(() => ({
  datasets: series.value
    .filter(s => s.visible)
    .map(s => ({
      label: `${s.device_name} / ${s.name}`,
      data: s.data.map(p => ({ x: p.ts * 1000, y: p.value })),
      borderColor: s.color,
      backgroundColor: s.color,
      yAxisID: s.axis === 'right' ? 'y1' : 'y',
      pointRadius: 0,
      tension: 0.3,
    })),
}))

const chartOptionsBase = computed(() => {
  const textColor = isDark.value ? 'rgba(255,255,255,0.65)' : 'rgba(0,0,0,0.65)'
  const gridColor = isDark.value ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false as const,
    plugins: { legend: { labels: { color: textColor } } },
    scales: {
      x: {
        type: 'linear' as const,
        ticks: { color: textColor, callback: (v: number) => new Date(v).toLocaleTimeString() },
        grid: { color: gridColor },
      },
      y: { ticks: { color: textColor }, grid: { color: gridColor } },
    },
  }
})

const hasRightAxisSeries = computed(() => series.value.some(s => s.visible && s.axis === 'right'))

const chartOptions = computed(() => {
  if (!hasRightAxisSeries.value) return chartOptionsBase.value
  return {
    ...chartOptionsBase.value,
    scales: {
      ...chartOptionsBase.value.scales,
      y1: { position: 'right' as const, ticks: chartOptionsBase.value.scales.y.ticks, grid: { display: false } },
    },
  }
})

// ─── Save ───────────────────────────────────────────────────────────────

const saveModalOpen = ref(false)
const saveNameInput = ref('')
const saving = ref(false)

function openSaveModal() {
  if (!series.value.length) {
    message.error('Add at least one point before saving')
    return
  }
  saveNameInput.value = graphName.value
  saveModalOpen.value = true
}

async function doSave() {
  const name = saveNameInput.value.trim()
  if (!name) {
    message.error('Name is required')
    return
  }
  saving.value = true
  try {
    const definition: CustomGraphDefinition = {
      version: 1,
      series: series.value.map(s => ({
        device_id: s.device_id, object_id: s.object_id, color: s.color, axis: s.axis, visible: s.visible,
      })),
      time_range: 'live',
    }
    const result = savedGraphId.value
      ? await api.customGraphs.update(savedGraphId.value, { name, definition })
      : await api.customGraphs.create({ name, definition })
    savedGraphId.value = result.id
    graphName.value = result.name
    saveModalOpen.value = false
    message.success('Graph saved')
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message || 'Failed to save graph')
  } finally {
    saving.value = false
  }
}

function close() {
  emit('update:open', false)
}
</script>

<template>
  <a-modal
    :open="open"
    :title="graphName ? `Data Graph — ${graphName}` : 'Data Graph'"
    :footer="null"
    width="900px"
    destroy-on-close
    @update:open="(v: boolean) => !v && close()"
  >
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px">
      <PointPicker
        :model-value="null"
        :points="allPoints"
        :meta="meta"
        placeholder="+ Add Point"
        :disabled="loadingPoints"
        @update:modelValue="onPick"
      />
      <a-button type="primary" @click="openSaveModal">
        <template #icon><SaveOutlined /></template>
        Save Graph
      </a-button>
    </div>

    <div v-if="!series.length" style="text-align:center;color:var(--text-placeholder);padding:40px 0">
      No points on this graph yet — use "+ Add Point" above.
    </div>

    <template v-else>
      <div style="height:320px">
        <Line :data="chartData" :options="chartOptions" />
      </div>

      <div style="margin-top:14px;display:flex;flex-direction:column;gap:6px">
        <div
          v-for="(s, i) in series"
          :key="`${s.device_id}-${s.object_id}`"
          style="display:flex;align-items:center;gap:10px;font-size:12.5px;padding:4px 6px;border-radius:4px"
          :style="{ background: 'var(--hover-bg, rgba(128,128,128,0.06))' }"
        >
          <span :style="{ display: 'inline-block', width: '10px', height: '10px', borderRadius: '50%', background: s.color, flex: 'none' }" />
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            {{ s.device_name }} / {{ s.name }}
          </span>
          <span style="color:var(--text-secondary);width:80px;flex:none">{{ s.units === 'no-units' ? '' : s.units }}</span>
          <a-radio-group
            :value="s.axis"
            size="small"
            button-style="solid"
            style="flex:none"
            @change="(e) => onAxisChange(s, e)"
          >
            <a-radio-button value="left">L</a-radio-button>
            <a-radio-button value="right">R</a-radio-button>
          </a-radio-group>
          <a-checkbox v-model:checked="s.visible" style="flex:none" title="Show/hide" />
          <a-button type="text" size="small" danger title="Remove" style="flex:none" @click="removeSeries(i)">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
      </div>
    </template>

    <a-modal
      v-model:open="saveModalOpen"
      title="Save Graph"
      ok-text="Save"
      :confirm-loading="saving"
      @ok="doSave"
    >
      <a-input
        v-model:value="saveNameInput"
        placeholder="Graph name"
        :maxlength="100"
        @pressEnter="doSave"
      />
    </a-modal>
  </a-modal>
</template>
