/** Shared Chart.js dataset/option/color helpers for the Custom Graph
 * feature -- used by both CustomGraphModal.vue (the full popup editor)
 * and SavedGraphCard.vue (the dashboard grid's inline auto-saving editor)
 * so the two don't carry two diverging copies of the same axis-grouping/
 * chart-option logic. */
import type { HistoryPoint } from './types'

export const CHART_COLORS = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1', '#13c2c2', '#eb2f96', '#a0d911']

/** First distinct units value seen goes on the left axis; anything else
 * goes right. `existingUnits` is every series' units currently on the
 * graph (in order added), `newUnits` is the units of the series being
 * added/classified. */
export function axisForUnits(existingUnits: string[], newUnits: string): 'left' | 'right' {
  const distinct: string[] = []
  for (const u of existingUnits) {
    if (!distinct.includes(u)) distinct.push(u)
  }
  if (distinct.length === 0 || distinct[0] === newUnits) return 'left'
  return 'right'
}

export interface ChartSeriesLike {
  color: string
  axis: 'left' | 'right'
  visible: boolean
  device_name: string
  name: string
  data: HistoryPoint[]
}

export function buildChartData(series: ChartSeriesLike[]) {
  return {
    datasets: series
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
  }
}

export function buildChartOptionsBase(isDark: boolean) {
  const textColor = isDark ? 'rgba(255,255,255,0.65)' : 'rgba(0,0,0,0.65)'
  const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'
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
}

export function buildChartOptions(isDark: boolean, hasRightAxisSeries: boolean) {
  const base = buildChartOptionsBase(isDark)
  if (!hasRightAxisSeries) return base
  return {
    ...base,
    scales: {
      ...base.scales,
      y1: { position: 'right' as const, ticks: base.scales.y.ticks, grid: { display: false } },
    },
  }
}
