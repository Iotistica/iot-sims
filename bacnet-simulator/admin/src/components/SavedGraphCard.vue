<script setup lang="ts">
/** Inline, always-editable card for one saved graph, used by
 * SavedGraphsView.vue's dashboard grid. Unlike the earlier read-only
 * version, this card carries the full per-series legend and controls
 * (color/name/units, L/R axis, show/hide, remove) live and interactive,
 * plus an inline-editable name and a compact "+" (PointPicker in icon
 * mode) to add a point on the spot -- every change auto-saves via
 * persist() immediately, since there's no open/close gesture bounding an
 * edit session on this card the way there is in CustomGraphModal.vue's
 * popup. "Edit" still opens that popup for a bigger/roomier session (e.g.
 * adding several points via its fuller search UI); this card is not a
 * replacement for it, just the fast path for routine changes.
 *
 * Series device/point labels and units are re-resolved live against the
 * `points` list passed down from the parent (same reasoning as the modal:
 * never trust a renamed/stale label baked into the saved definition).
 * Chart/color/axis helpers are shared with CustomGraphModal.vue via
 * ../customGraphChart.ts so the two don't carry diverging copies.
 */
import { ref, computed, watch } from 'vue'
import {
  Chart as ChartJS,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
} from 'chart.js'
import { Line } from 'vue-chartjs'
import { message } from 'ant-design-vue'
import { EditOutlined, DeleteOutlined, ReloadOutlined, CloseOutlined } from '@ant-design/icons-vue'
import { isDark } from '../theme'
import { api } from '../api'
import { CHART_COLORS, axisForUnits, buildChartData, buildChartOptions } from '../customGraphChart'
import type { CustomGraphDefinition, HistoryPoint, Meta, PointRef, PointRow, SavedGraph } from '../types'
import PointPicker from './PointPicker.vue'

ChartJS.register(LinearScale, PointElement, LineElement, Tooltip, Legend)

const props = defineProps<{
  graph: SavedGraph
  points: PointRow[]
  meta: Meta
}>()
const emit = defineEmits<{ edit: []; delete: []; saved: [] }>()

interface ResolvedSeries {
  device_id: number
  object_id: number
  color: string
  axis: 'left' | 'right'
  visible: boolean
  device_name: string
  name: string
  units: string
  data: HistoryPoint[]
}

const series = ref<ResolvedSeries[]>([])
const loading = ref(false)
const refreshing = ref(false)
const saving = ref(false)
const missingCount = ref(0)
const nameInput = ref(props.graph.name)

async function refreshData() {
  if (!series.value.length) return
  refreshing.value = true
  try {
    // Mutate through series.value's reactive proxy -- mutating a plain
    // object bypasses Vue's set trap, so entry.data changes in memory but
    // chartData's computed never re-runs. Same pitfall CustomGraphModal.
    // vue's addSeriesFromRow already documents.
    await Promise.all(
      series.value.map(async (entry) => {
        try {
          entry.data = await api.objects.history(entry.device_id, entry.object_id)
        } catch {
          entry.data = []
        }
      }),
    )
  } finally {
    refreshing.value = false
  }
}

function manualRefresh() {
  if (refreshing.value) return
  void refreshData()
}

async function resolveAndLoad() {
  loading.value = true
  missingCount.value = 0
  nameInput.value = props.graph.name
  const resolved: ResolvedSeries[] = []
  for (const s of props.graph.definition.series) {
    const row = props.points.find(p => p.device_id === s.device_id && p.object_id === s.object_id)
    if (!row) {
      missingCount.value += 1
      continue
    }
    resolved.push({
      device_id: s.device_id,
      object_id: s.object_id,
      color: s.color,
      axis: s.axis,
      visible: s.visible,
      device_name: row.device_name,
      name: row.name,
      units: row.units ?? 'no-units',
      data: [],
    })
  }
  series.value = resolved
  loading.value = false
  await refreshData()
}

watch(() => [props.graph.id, props.points], resolveAndLoad, { immediate: true })

// ─── Auto-save ──────────────────────────────────────────────────────────
// Every inline edit (add/remove point, axis, visibility, rename) persists
// immediately -- no separate "Save" step, since there's no open/close
// gesture bounding an edit session on this card. On failure: show an
// error and leave local state as-is (no rollback machinery); Refresh or
// the next parent reload will resync if it ever drifts.

async function persist() {
  saving.value = true
  try {
    const definition: CustomGraphDefinition = {
      version: 1,
      series: series.value.map(s => ({
        device_id: s.device_id, object_id: s.object_id, color: s.color, axis: s.axis, visible: s.visible,
      })),
      time_range: 'live',
    }
    const name = nameInput.value.trim() || props.graph.name
    await api.customGraphs.update(props.graph.id, { name, definition })
    emit('saved')
  } catch (e: unknown) {
    message.error((e as Error).message || 'Failed to save graph')
  } finally {
    saving.value = false
  }
}

function onNameBlur() {
  const trimmed = nameInput.value.trim()
  if (!trimmed) {
    nameInput.value = props.graph.name
    return
  }
  if (trimmed === props.graph.name) return
  void persist()
}

function blurActiveInput(e: Event) {
  (e.target as HTMLInputElement)?.blur()
}

function onAxisChange(entry: ResolvedSeries, e: { target: { value: string } }) {
  entry.axis = e.target.value === 'right' ? 'right' : 'left'
  void persist()
}

function onVisibleChange() {
  void persist()
}

function removeSeries(index: number) {
  series.value.splice(index, 1)
  void persist()
}

function onPick(picked: PointRef | null) {
  if (!picked) return
  if (series.value.some(s => s.device_id === picked.device_id && s.object_id === picked.object_id)) {
    message.warning('That point is already on this graph')
    return
  }
  const row = props.points.find(p => p.device_id === picked.device_id && p.object_id === picked.object_id)
  if (!row) {
    message.error('Point not found')
    return
  }
  const units = row.units ?? 'no-units'
  series.value.push({
    device_id: row.device_id,
    object_id: row.object_id,
    color: CHART_COLORS[series.value.length % CHART_COLORS.length],
    axis: axisForUnits(series.value.map(s => s.units), units),
    visible: true,
    device_name: row.device_name,
    name: row.name,
    units,
    data: [],
  })
  // Fetch through series.value's reactive proxy, not the object literal
  // pushed above -- same reactive-proxy pitfall as refreshData/resolveAndLoad.
  const added = series.value[series.value.length - 1]
  void (async () => {
    try {
      added.data = await api.objects.history(added.device_id, added.object_id)
    } catch {
      added.data = []
    }
    void persist()
  })()
}

const visibleSeries = computed(() => series.value.filter(s => s.visible))
const chartData = computed(() => buildChartData(series.value))
const hasRightAxisSeries = computed(() => series.value.some(s => s.visible && s.axis === 'right'))
const chartOptions = computed(() => buildChartOptions(isDark.value, hasRightAxisSeries.value))

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString()
}
</script>

<template>
  <a-card size="small" class="saved-graph-card">
    <template #title>
      <a-input
        v-model:value="nameInput"
        class="saved-graph-card__name-input"
        :bordered="false"
        :maxlength="100"
        @blur="onNameBlur"
        @pressEnter="blurActiveInput"
      />
    </template>
    <template #extra>
      <a-space :size="2">
        <a-button size="small" type="text" title="Refresh" :loading="refreshing" @click="manualRefresh">
          <template #icon><ReloadOutlined /></template>
        </a-button>
        <PointPicker
          trigger="icon"
          :model-value="null"
          :points="points"
          :meta="meta"
          placeholder="Add Point"
          @update:modelValue="onPick"
        />
        <a-button size="small" type="text" title="Edit" @click="emit('edit')">
          <template #icon><EditOutlined /></template>
        </a-button>
        <a-button size="small" type="text" danger title="Delete" @click="emit('delete')">
          <template #icon><DeleteOutlined /></template>
        </a-button>
      </a-space>
    </template>

    <div v-if="loading" class="saved-graph-card__state">
      <a-spin size="small" />
    </div>
    <template v-else>
      <div v-if="!visibleSeries.length" class="saved-graph-card__state">
        {{ series.length ? 'All points hidden.' : 'No points yet — use the + button above.' }}
      </div>
      <div v-else class="saved-graph-card__chart"><Line :data="chartData" :options="chartOptions" /></div>

      <div v-if="series.length" class="saved-graph-card__rows">
        <div v-for="(s, i) in series" :key="`${s.device_id}-${s.object_id}`" class="saved-graph-card__row">
          <span class="saved-graph-card__dot" :style="{ background: s.color }" />
          <span class="saved-graph-card__row-name">{{ s.device_name }} / {{ s.name }}</span>
          <span class="saved-graph-card__row-units">{{ s.units === 'no-units' ? '' : s.units }}</span>
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
          <a-checkbox v-model:checked="s.visible" style="flex:none" title="Show/hide" @change="onVisibleChange" />
          <a-button type="text" size="small" danger title="Remove" style="flex:none" @click="removeSeries(i)">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
      </div>
    </template>

    <div class="saved-graph-card__footer">
      <span>{{ series.length }} point{{ series.length === 1 ? '' : 's' }}</span>
      <span v-if="missingCount" class="saved-graph-card__missing">{{ missingCount }} missing</span>
      <a-spin v-if="saving" size="small" />
      <span class="saved-graph-card__updated">Updated {{ fmtDate(graph.updated_at) }}</span>
    </div>
  </a-card>
</template>

<style scoped>
.saved-graph-card {
  display: flex;
  flex-direction: column;
}
.saved-graph-card :deep(.ant-card-body) {
  display: flex;
  flex-direction: column;
  flex: 1;
}
.saved-graph-card :deep(.ant-card-head) {
  padding-left: 8px;
}
.saved-graph-card__name-input {
  font-weight: 600;
  padding-left: 4px;
}
.saved-graph-card__name-input :deep(input) {
  font-weight: 600;
}
.saved-graph-card__chart {
  height: 220px;
}
.saved-graph-card__state {
  height: 220px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-placeholder);
  font-size: 12.5px;
  text-align: center;
  padding: 0 16px;
}
.saved-graph-card__rows {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.saved-graph-card__row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  padding: 4px 6px;
  border-radius: 4px;
  background: var(--hover-bg, rgba(128, 128, 128, 0.06));
}
.saved-graph-card__dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex: none;
}
.saved-graph-card__row-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.saved-graph-card__row-units {
  color: var(--text-secondary);
  width: 70px;
  flex: none;
}
.saved-graph-card__footer {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  font-size: 11.5px;
  color: var(--text-secondary);
}
.saved-graph-card__missing {
  color: #faad14;
}
.saved-graph-card__updated {
  margin-left: auto;
}
</style>
