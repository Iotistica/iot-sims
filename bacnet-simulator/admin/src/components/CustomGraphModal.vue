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
import { CHART_COLORS, axisForUnits, buildChartData, buildChartOptions } from '../customGraphChart'
import type {
  CustomGraphDefinition, CustomGraphSeries, HistoryPoint, KpiDefinition, Meta, PointRef, PointRow,
  SavedGraph, TableDefinition, WidgetType,
} from '../types'
import PointPicker from './PointPicker.vue'

ChartJS.register(LinearScale, PointElement, LineElement, Tooltip, Legend)

const props = defineProps<{
  open: boolean
  meta: Meta
  initialPoint?: PointRef | null
  savedGraph?: SavedGraph | null
  /** Which dashboard a NEW widget lands on -- ignored when editing an
   * existing one (its own dashboard_id is preserved instead). Omitted
   * entirely (ObjectsPanel.vue's quick single-point graph icon has no
   * dashboard-switcher context) falls back to the backend's own "first
   * dashboard there is" default. */
  dashboardId?: number | null
  /** Where a NEW widget's grid geometry starts (SavedGraphsView.vue
   * computes "append below everything else on this dashboard"). Ignored
   * when editing -- the existing widget's own position/size is preserved. */
  defaultGridX?: number
  defaultGridY?: number
}>()
const emit = defineEmits<{ 'update:open': [boolean]; saved: [] }>()

// Only choosable when creating a NEW widget -- converting an existing
// populated chart's multi-series list into a KPI/Table's single point (or
// vice versa) is a different, lossy operation not worth supporting here;
// editing an existing widget just keeps whatever type it already is.
const widgetType = ref<WidgetType>('chart')
// Shared by both 'kpi' and 'table' modes -- both are "pick exactly one
// point," just displayed differently.
const singlePoint = ref<PointRow | null>(null)
const tableColumns = ref({ min: false, max: false, avg: false })
const tablePageSize = ref(10)

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
    axis: overrides?.axis ?? axisForUnits(series.value.map(s => s.units), units),
    visible: overrides?.visible ?? true,
    role: overrides?.role ?? 'pv',
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

// a-radio-group's change event isn't typed specifically enough for the
// template to infer `e`'s type on its own, and an inline type annotation in
// the template itself doesn't parse (see SemanticSuggestionsModal.vue's
// onSelectAllChange for the same issue on a-checkbox) -- returning the
// actual handler, fully typed here, lets the template just call
// onAxisChange(s) and stay annotation-free.
function onAxisChange(entry: GraphSeriesState) {
  return (e: { target: { value: string } }) => {
    entry.axis = e.target.value === 'right' ? 'right' : 'left'
  }
}

// Same typed-handler-returned-from-a-function pattern as onAxisChange above
// and for the identical reason -- a-segmented's change event isn't typed
// specifically enough for the template to infer on its own.
function onRoleChange(entry: GraphSeriesState) {
  return (value: string | number) => {
    entry.role = value === 'setpoint' ? 'setpoint' : 'pv'
  }
}

function onPickSingle(picked: PointRef | null) {
  if (!picked) return
  const row = allPoints.value.find(p => p.device_id === picked.device_id && p.object_id === picked.object_id)
  singlePoint.value = row ?? null
}

watch(
  () => props.open,
  async (isOpen) => {
    if (!isOpen) return
    series.value = []
    graphName.value = ''
    savedGraphId.value = null
    widgetType.value = 'chart'
    singlePoint.value = null
    tableColumns.value = { min: false, max: false, avg: false }
    tablePageSize.value = 10

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
      widgetType.value = props.savedGraph.widget_type

      if (props.savedGraph.widget_type === 'kpi' || props.savedGraph.widget_type === 'table') {
        const def = props.savedGraph.definition as KpiDefinition | TableDefinition
        const row = allPoints.value.find(p => p.device_id === def.device_id && p.object_id === def.object_id)
        if (row) {
          singlePoint.value = row
        } else {
          message.warning('The saved point no longer exists')
        }
        if (props.savedGraph.widget_type === 'table') {
          const table = props.savedGraph.definition as TableDefinition
          tableColumns.value = {
            min: table.columns?.min ?? false,
            max: table.columns?.max ?? false,
            avg: table.columns?.avg ?? false,
          }
          tablePageSize.value = table.page_size ?? 10
        }
      } else {
        let missingCount = 0
        for (const s of (props.savedGraph.definition as CustomGraphDefinition).series) {
          const row = allPoints.value.find(p => p.device_id === s.device_id && p.object_id === s.object_id)
          if (!row) {
            missingCount += 1
            continue
          }
          addSeriesFromRow(row, { color: s.color, axis: s.axis, visible: s.visible, role: s.role })
        }
        if (missingCount > 0) {
          message.warning(`${missingCount} saved point${missingCount === 1 ? '' : 's'} no longer exist and ${missingCount === 1 ? 'was' : 'were'} skipped`)
        }
      }
    } else if (props.initialPoint) {
      const row = allPoints.value.find(p => p.device_id === props.initialPoint!.device_id && p.object_id === props.initialPoint!.object_id)
      if (row) addSeriesFromRow(row)
    }
  },
  { immediate: true },
)

// ─── Chart ──────────────────────────────────────────────────────────────

const chartData = computed(() => buildChartData(series.value))

const hasRightAxisSeries = computed(() => series.value.some(s => s.visible && s.axis === 'right'))

const chartOptions = computed(() => buildChartOptions(isDark.value, hasRightAxisSeries.value))

// ─── Save ───────────────────────────────────────────────────────────────
// Name and points share this one dialog -- no separate rename popup and no
// separate "name it before saving" popup; both used to be their own modal
// (SavedGraphsView.vue's rename prompt, and this component's own
// saveModalOpen), collapsed here into a single name field alongside the
// point picker/chart/save button.

const saving = ref(false)

async function doSave() {
  if (widgetType.value === 'kpi' || widgetType.value === 'table') {
    if (!singlePoint.value) {
      message.error('Pick a point before saving')
      return
    }
  } else if (!series.value.length) {
    message.error('Add at least one point before saving')
    return
  }
  const name = graphName.value.trim()
  if (!name) {
    message.error('Name is required')
    return
  }
  saving.value = true
  try {
    let definition: CustomGraphDefinition | KpiDefinition | TableDefinition
    if (widgetType.value === 'kpi') {
      definition = { device_id: singlePoint.value!.device_id, object_id: singlePoint.value!.object_id }
    } else if (widgetType.value === 'table') {
      definition = {
        device_id: singlePoint.value!.device_id,
        object_id: singlePoint.value!.object_id,
        columns: tableColumns.value,
        page_size: tablePageSize.value,
      }
    } else {
      definition = {
        version: 1,
        series: series.value.map(s => ({
          device_id: s.device_id, object_id: s.object_id, color: s.color, axis: s.axis, visible: s.visible,
          role: s.role,
        })),
        time_range: 'live',
      }
    }
    // Editing keeps the widget's existing dashboard/geometry unchanged;
    // creating a new one uses the dashboard/position the caller supplied
    // (SavedGraphsView.vue computes "append below everything else" for
    // grid_x/y; ObjectsPanel.vue's quick graph icon supplies neither, and
    // the backend falls back to its own defaults).
    //
    // Default height differs by type: a KPI tile is just a big number and
    // fits comfortably in 4 grid rows (~156px); a Chart/Table needs real
    // room for its plot/rows on top of the card's own header/footer --
    // 4 rows left new charts visibly clipped/squished (confirmed live,
    // needing a manual resize every time), so those default taller.
    const defaultGridH = widgetType.value === 'kpi' ? 4 : 10
    const payload = {
      name,
      definition,
      dashboard_id: props.savedGraph?.dashboard_id ?? props.dashboardId ?? undefined,
      widget_type: widgetType.value,
      grid_x: props.savedGraph?.grid_x ?? props.defaultGridX ?? 0,
      grid_y: props.savedGraph?.grid_y ?? props.defaultGridY ?? 0,
      grid_w: props.savedGraph?.grid_w ?? 6,
      grid_h: props.savedGraph?.grid_h ?? defaultGridH,
    }
    const result = savedGraphId.value
      ? await api.customGraphs.update(savedGraphId.value, payload)
      : await api.customGraphs.create(payload)
    savedGraphId.value = result.id
    graphName.value = result.name
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
    title="Data Graph"
    :footer="null"
    width="900px"
    destroy-on-close
    @update:open="(v: boolean) => !v && close()"
  >
    <a-input
      v-model:value="graphName"
      placeholder="Graph name"
      :maxlength="100"
      style="margin-bottom:10px"
    />

    <a-segmented
      v-model:value="widgetType"
      :disabled="!!savedGraphId"
      :options="[{ label: 'Chart', value: 'chart' }, { label: 'KPI Tile', value: 'kpi' }, { label: 'Table', value: 'table' }]"
      style="margin-bottom:10px"
    />

    <template v-if="widgetType === 'kpi' || widgetType === 'table'">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <PointPicker
          :model-value="null"
          :points="allPoints"
          :meta="meta"
          placeholder="Pick Point"
          :disabled="loadingPoints"
          @update:modelValue="onPickSingle"
        />
        <a-button type="primary" :loading="saving" @click="doSave">
          <template #icon><SaveOutlined /></template>
          Save Widget
        </a-button>
      </div>
      <div v-if="!singlePoint" style="text-align:center;color:var(--text-placeholder);padding:40px 0">
        No point picked yet — use "Pick Point" above.
      </div>
      <template v-else-if="widgetType === 'kpi'">
        <div style="text-align:center;padding:40px 0">
          <div style="font-size:13px;color:var(--text-secondary)">{{ singlePoint.device_name }} / {{ singlePoint.name }}</div>
          <div style="font-size:28px;font-weight:600;margin-top:6px">{{ singlePoint.units === 'no-units' ? '' : singlePoint.units }}</div>
        </div>
      </template>
      <template v-else>
        <div style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">
          {{ singlePoint.device_name }} / {{ singlePoint.name }}
        </div>
        <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
          <span style="font-size:12.5px;color:var(--text-secondary)">Columns:</span>
          <a-checkbox v-model:checked="tableColumns.min">Min</a-checkbox>
          <a-checkbox v-model:checked="tableColumns.max">Max</a-checkbox>
          <a-checkbox v-model:checked="tableColumns.avg">Avg</a-checkbox>
          <span style="margin-left:16px;font-size:12.5px;color:var(--text-secondary)">Rows per page:</span>
          <a-input-number v-model:value="tablePageSize" :min="1" :max="720" />
        </div>
      </template>
    </template>

    <template v-else>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px">
      <PointPicker
        :model-value="null"
        :points="allPoints"
        :meta="meta"
        placeholder="+ Add Point"
        :disabled="loadingPoints"
        @update:modelValue="onPick"
      />
      <a-button type="primary" :loading="saving" @click="doSave">
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
          <a-segmented
            :value="s.role ?? 'pv'"
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
          <a-checkbox v-model:checked="s.visible" style="flex:none" title="Show/hide" />
          <a-button type="text" size="small" danger title="Remove" style="flex:none" @click="removeSeries(i)">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
      </div>
    </template>
    </template>
  </a-modal>
</template>
