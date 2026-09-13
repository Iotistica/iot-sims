<script setup lang="ts">
/** Inline, always-editable widget card for one dashboard widget (Chart or
 * KPI Tile -- see migration 28's docstring for why both live on the same
 * SavedGraph/custom_graphs entity), used by SavedGraphsView.vue's grid.
 * A chart widget carries the full per-series legend and controls (color/
 * name/units, PV/Setpoint role, L/R axis, show/hide, remove) live and
 * interactive, plus an inline-editable name and a compact "+" (PointPicker
 * in icon mode) to add a point on the spot -- every change auto-saves via
 * persist() immediately, since there's no open/close gesture bounding an
 * edit session on this card the way there is in CustomGraphModal.vue's
 * popup. A KPI widget just displays its one point's current value -- "Edit"
 * (CustomGraphModal.vue) is the only way to change WHICH point it shows.
 * "Edit" always opens that popup for a bigger/roomier session (e.g. adding
 * several points via its fuller search UI, or changing widget type); this
 * card is not a replacement for it, just the fast path for routine changes.
 *
 * Series device/point labels and units are re-resolved live against the
 * `points` list passed down from the parent (same reasoning as the modal:
 * never trust a renamed/stale label baked into the saved definition).
 * Chart/color/axis helpers are shared with CustomGraphModal.vue via
 * ../customGraphChart.ts so the two don't carry diverging copies.
 *
 * Grid position/size (grid_x/y/w/h) is owned by the parent's GridLayout,
 * not this card -- persist() here never touches those fields, relying on
 * Database.update_custom_graph's "omitted means unchanged" merge so a
 * content edit here can never clobber a drag/resize saved elsewhere (and
 * vice versa). See CustomGraphUpdate's own comment in schemas.py.
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
import { EditOutlined, DeleteOutlined, ReloadOutlined, CloseOutlined, HolderOutlined } from '@ant-design/icons-vue'
import { isDark } from '../theme'
import { api } from '../api'
import { CHART_COLORS, axisForUnits, buildChartData, buildChartOptions } from '../customGraphChart'
import type {
  CustomGraphDefinition, CustomGraphSeries, HistoryPoint, KpiDefinition, Meta, PointRef, PointRow, SavedGraph,
  TableDefinition,
} from '../types'
import PointPicker from './PointPicker.vue'

ChartJS.register(LinearScale, PointElement, LineElement, Tooltip, Legend)

const props = defineProps<{
  graph: SavedGraph
  points: PointRow[]
  meta: Meta
}>()
const emit = defineEmits<{ edit: []; delete: []; saved: [] }>()

const isKpi = computed(() => props.graph.widget_type === 'kpi')
const isTable = computed(() => props.graph.widget_type === 'table')

interface ResolvedSeries {
  device_id: number
  object_id: number
  color: string
  axis: 'left' | 'right'
  visible: boolean
  role: 'pv' | 'setpoint'
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

// ─── KPI ────────────────────────────────────────────────────────────────

const kpiRow = ref<PointRow | null>(null)
const kpiMissing = ref(false)

function resolveKpi() {
  const kpi = props.graph.definition as KpiDefinition
  const row = props.points.find(p => p.device_id === kpi.device_id && p.object_id === kpi.object_id)
  kpiRow.value = row ?? null
  kpiMissing.value = !row
}

function formatKpiValue(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'number') {
    const decimals = (props.graph.definition as KpiDefinition).decimals
    return decimals != null ? value.toFixed(decimals) : String(value)
  }
  return String(value)
}

async function refreshKpi() {
  refreshing.value = true
  try {
    // points is a snapshot from whenever the parent last loaded it -- refetch
    // independently here so Refresh means something even between parent loads,
    // same self-sufficiency chart series already have via fetchHistoryFor.
    const fresh = await api.points.list()
    const kpi = props.graph.definition as KpiDefinition
    const row = fresh.find(p => p.device_id === kpi.device_id && p.object_id === kpi.object_id)
    kpiRow.value = row ?? null
    kpiMissing.value = !row
  } catch {
    // leave last-known value in place
  } finally {
    refreshing.value = false
  }
}

// ─── Table ──────────────────────────────────────────────────────────────
// One point's raw /history buffer shown as a table -- the same data source
// a chart series already uses, just rendered as rows instead of a line.
// There's no server-side bucketing (see this widget's own plan Context),
// so Min/Max/Avg are a SUMMARY over the whole fetched buffer, not per-row
// columns -- a per-row column would just repeat the same aggregate value
// on every row, which is misleading, not merely simplified.

const tableRow = ref<PointRow | null>(null)
const tableMissing = ref(false)
const tableHistory = ref<HistoryPoint[]>([])

function resolveTable() {
  const table = props.graph.definition as TableDefinition
  const row = props.points.find(p => p.device_id === table.device_id && p.object_id === table.object_id)
  tableRow.value = row ?? null
  tableMissing.value = !row
}

async function refreshTableData() {
  if (!tableRow.value) return
  refreshing.value = true
  try {
    tableHistory.value = await api.objects.history(tableRow.value.device_id, tableRow.value.object_id)
  } catch {
    tableHistory.value = []
  } finally {
    refreshing.value = false
  }
}

interface TableRowState { ts: number; value: number }
const tableRows = computed<TableRowState[]>(() => tableHistory.value.map(p => ({ ts: p.ts, value: p.value })))
const tableSummary = computed(() => {
  const values = tableHistory.value.map(p => p.value).filter((v): v is number => typeof v === 'number')
  if (!values.length) return null
  return {
    min: Math.min(...values),
    max: Math.max(...values),
    avg: values.reduce((a, b) => a + b, 0) / values.length,
  }
})
const tableColumns = computed(() => (props.graph.definition as TableDefinition).columns ?? {})
const tablePageSize = computed(() => (props.graph.definition as TableDefinition).page_size ?? 10)
const TABLE_GRID_COLUMNS = [
  { title: 'Time', dataIndex: 'ts', key: 'ts', sorter: (a: TableRowState, b: TableRowState) => a.ts - b.ts },
  { title: 'Value', dataIndex: 'value', key: 'value', align: 'right', sorter: (a: TableRowState, b: TableRowState) => a.value - b.value },
]

function fmtTableTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}
function fmtTableValue(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : String(value)
}

// ─── Chart ──────────────────────────────────────────────────────────────

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
  if (isKpi.value) void refreshKpi()
  else if (isTable.value) void refreshTableData()
  else void refreshData()
}

async function resolveAndLoad() {
  nameInput.value = props.graph.name

  if (isKpi.value) {
    resolveKpi()
    return
  }

  if (isTable.value) {
    resolveTable()
    await refreshTableData()
    return
  }

  loading.value = true
  missingCount.value = 0
  const resolved: ResolvedSeries[] = []
  for (const s of (props.graph.definition as CustomGraphDefinition).series) {
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
      role: s.role ?? 'pv',
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

watch(() => [props.graph.id, props.graph.widget_type, props.points], resolveAndLoad, { immediate: true })

// ─── Auto-save ──────────────────────────────────────────────────────────
// Every inline edit (add/remove point, axis, role, visibility, rename)
// persists immediately -- no separate "Save" step, since there's no
// open/close gesture bounding an edit session on this card. On failure:
// show an error and leave local state as-is (no rollback machinery); Refresh
// or the next parent reload will resync if it ever drifts.

async function persist() {
  saving.value = true
  try {
    const definition: CustomGraphDefinition | KpiDefinition | TableDefinition = (isKpi.value || isTable.value)
      ? props.graph.definition
      : {
          version: 1,
          series: series.value.map(s => ({
            device_id: s.device_id, object_id: s.object_id, color: s.color, axis: s.axis, visible: s.visible,
            role: s.role,
          })),
          time_range: 'live',
        }
    const name = nameInput.value.trim() || props.graph.name
    // dashboard_id/widget_type/grid_* deliberately omitted -- see this
    // component's own top comment.
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

// a-radio-group's change event isn't typed specifically enough for the
// template to infer `e`'s type on its own, and an inline type annotation in
// the template itself doesn't parse (see SemanticSuggestionsModal.vue's
// onSelectAllChange for the same issue on a-checkbox) -- returning the
// actual handler, fully typed here, lets the template just call
// onAxisChange(s) and stay annotation-free.
function onAxisChange(entry: ResolvedSeries) {
  return (e: { target: { value: string } }) => {
    entry.axis = e.target.value === 'right' ? 'right' : 'left'
    void persist()
  }
}

function onRoleChange(entry: ResolvedSeries) {
  return (value: string | number) => {
    entry.role = value === 'setpoint' ? 'setpoint' : 'pv'
    void persist()
  }
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
    role: 'pv',
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
      <span class="widget-drag-handle" title="Drag to move">
        <HolderOutlined />
      </span>
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
          v-if="!isKpi && !isTable"
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

    <template v-if="isKpi">
      <div class="saved-graph-card__kpi">
        <div v-if="kpiMissing" class="saved-graph-card__state">Point no longer exists.</div>
        <template v-else>
          <div class="saved-graph-card__kpi-value">{{ formatKpiValue(kpiRow?.live_value) }}</div>
          <div class="saved-graph-card__kpi-label">
            {{ kpiRow ? `${kpiRow.device_name} / ${kpiRow.name}` : '' }}
            <span v-if="kpiRow?.units && kpiRow.units !== 'no-units'">({{ kpiRow.units }})</span>
          </div>
        </template>
      </div>
    </template>
    <template v-else-if="isTable">
      <div v-if="tableMissing" class="saved-graph-card__state">Point no longer exists.</div>
      <template v-else>
        <div class="saved-graph-card__table-label">
          {{ tableRow ? `${tableRow.device_name} / ${tableRow.name}` : '' }}
        </div>
        <div v-if="tableSummary && (tableColumns.min || tableColumns.max || tableColumns.avg)" class="saved-graph-card__table-summary">
          <span v-if="tableColumns.min">Min: {{ fmtTableValue(tableSummary.min) }}</span>
          <span v-if="tableColumns.max">Max: {{ fmtTableValue(tableSummary.max) }}</span>
          <span v-if="tableColumns.avg">Avg: {{ fmtTableValue(tableSummary.avg) }}</span>
        </div>
        <a-table
          size="small"
          :data-source="tableRows"
          :columns="TABLE_GRID_COLUMNS"
          :pagination="{ pageSize: tablePageSize, size: 'small' }"
          :show-sorter-tooltip="false"
          row-key="ts"
        >
          <template #bodyCell="{ column, record }">
            <span v-if="column.key === 'ts'" style="font-family:monospace;font-size:12px">{{ fmtTableTime(record.ts) }}</span>
            <span v-else-if="column.key === 'value'">{{ fmtTableValue(record.value) }}</span>
          </template>
        </a-table>
      </template>
    </template>
    <template v-else>
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
            <a-segmented
              :value="s.role"
              size="small"
              :options="[{ label: 'PV', value: 'pv' }, { label: 'Setpoint', value: 'setpoint' }]"
              style="flex:none"
              @change="onRoleChange(s)"
            />
            <a-radio-group
              :value="s.axis"
              size="small"
              button-style="solid"
              style="flex:none"
              @change="onAxisChange(s)"
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
    </template>

    <div class="saved-graph-card__footer">
      <span v-if="!isKpi && !isTable">{{ series.length }} point{{ series.length === 1 ? '' : 's' }}</span>
      <span v-if="!isKpi && !isTable && missingCount" class="saved-graph-card__missing">{{ missingCount }} missing</span>
      <a-spin v-if="saving" size="small" />
      <span class="saved-graph-card__updated">Updated {{ fmtDate(graph.updated_at) }}</span>
    </div>
  </a-card>
</template>

<style scoped>
.saved-graph-card {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.saved-graph-card :deep(.ant-card-body) {
  display: flex;
  flex-direction: column;
  flex: 1;
}
.saved-graph-card :deep(.ant-card-head) {
  padding-left: 8px;
}
/* Ant Design's default card-head-title CSS (white-space:nowrap +
   text-overflow:ellipsis) is meant for a plain text title -- with a drag
   handle + editable input inside it instead, that ellipsis rule was
   clipping the whole row down to just "...". Override to a flex row that
   actually fits both. */
.saved-graph-card :deep(.ant-card-head-title) {
  display: flex;
  align-items: center;
  overflow: visible;
  min-width: 0;
}
.widget-drag-handle {
  cursor: move;
  color: var(--text-placeholder);
  margin-right: 4px;
  flex: none;
}
.saved-graph-card__name-input {
  font-weight: 600;
  padding-left: 4px;
  min-width: 0;
  flex: 1;
}
.saved-graph-card__name-input :deep(input) {
  font-weight: 600;
  text-overflow: ellipsis;
}
.saved-graph-card__chart {
  height: 220px;
  flex: 1;
}
.saved-graph-card__kpi {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 16px 0;
}
.saved-graph-card__kpi-value {
  font-size: 34px;
  font-weight: 700;
  line-height: 1.1;
}
.saved-graph-card__kpi-label {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
}
.saved-graph-card__table-label {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.saved-graph-card__table-summary {
  display: flex;
  gap: 14px;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 8px;
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
