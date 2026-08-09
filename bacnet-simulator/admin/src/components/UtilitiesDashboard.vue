<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Line, Bar } from 'vue-chartjs'
import {
  ReloadOutlined,
  ThunderboltOutlined,
  FireOutlined,
  DashboardOutlined,
  BulbOutlined,
} from '@ant-design/icons-vue'
import { authToken } from '../auth'
import { isDark } from '../theme'

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
)

interface EnergyEquipment {
  device_id: number
  device_name?: string
  model_type: string
  instance_key?: string

  power_kw: number | null
  interval_energy_kwh?: number | null
  total_energy_kwh?: number | null

  cooling_load_kw?: number | null
  effective_cop?: number | null

  thermal_output_kw?: number | null
  fuel_input_kw?: number | null
  interval_fuel_energy_kwh?: number | null
  total_fuel_energy_kwh?: number | null
  effective_efficiency?: number | null
  auxiliary_electric_power_kw?: number | null

  lighting_level_fraction?: number | null

  source: string
  confidence: string
  method: string
  warnings: string[]

  inputs?: {
    natural_gas_flow_cubic_feet_per_minute?: number | null
    natural_gas_flow_cubic_meters_per_hour?: number | null
    [key: string]: unknown
  }
}

interface EnergyHistoryRow {
  id: number
  timestamp: number
  device_id: number
  model_type: string
  instance_key?: string
  power_kw: number | null
  total_energy_kwh?: number | null
  source: string
  confidence: string
  metrics: {
    instance_key?: string
    interval_energy_kwh?: number | null
    cooling_load_kw?: number | null
    effective_cop?: number | null
    thermal_output_kw?: number | null
    fuel_input_kw?: number | null
    interval_fuel_energy_kwh?: number | null
    total_fuel_energy_kwh?: number | null
    effective_efficiency?: number | null
    lighting_level_fraction?: number | null
    [key: string]: unknown
  }
}

interface WrappedResponse<T> {
  value?: T
  Count?: number
}

interface AggregatedUtilityHistory {
  timestamp: number
  electric_kw: number
  cooling_kw: number
  boiler_fuel_kw: number
  heating_kw: number
  lighting_kw: number
}

const equipment = ref<EnergyEquipment[]>([])
const history = ref<EnergyHistoryRow[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

let liveTimer: ReturnType<typeof setInterval> | null = null
let historyTimer: ReturnType<typeof setInterval> | null = null

function unwrapArray<T>(payload: WrappedResponse<T[]> | T[]): T[] {
  if (Array.isArray(payload)) return payload
  return Array.isArray(payload?.value) ? payload.value : []
}

async function energyGet<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {}
  if (authToken.value) headers.Authorization = `Bearer ${authToken.value}`

  const response = await fetch(path, { headers })
  if (!response.ok) {
    throw new Error(`Energy API ${response.status}: ${response.statusText}`)
  }

  return await response.json() as T
}

async function loadEquipment() {
  try {
    loading.value = true
    const payload = await energyGet<WrappedResponse<EnergyEquipment[]> | EnergyEquipment[]>(
      '/energy/equipment',
    )
    equipment.value = unwrapArray(payload)
    error.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Unable to load utility data'
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  try {
    const payload = await energyGet<WrappedResponse<EnergyHistoryRow[]> | EnergyHistoryRow[]>(
      '/energy/history?hours=24',
    )
    history.value = unwrapArray(payload)
    error.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Unable to load utility history'
  }
}

async function refreshAll() {
  await Promise.allSettled([
    loadEquipment(),
    loadHistory(),
  ])
}

onMounted(async () => {
  await refreshAll()
  liveTimer = setInterval(loadEquipment, 5000)
  historyTimer = setInterval(loadHistory, 60000)
})

onUnmounted(() => {
  if (liveTimer) clearInterval(liveTimer)
  if (historyTimer) clearInterval(historyTimer)
})

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })
}

function deviceName(row: EnergyEquipment): string {
  if (row.device_name) return row.device_name
  return `${row.model_type} #${row.device_id}`
}

function instanceLabel(instanceKey?: string): string {
  if (!instanceKey || instanceKey === 'default') return ''

  return instanceKey
    .split('-')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function equipmentLabel(row: EnergyEquipment): string {
  const instance = instanceLabel(row.instance_key)
  return instance ? `${deviceName(row)} / ${instance}` : deviceName(row)
}

// ─── Equipment groups ────────────────────────────────────────────────────────

const boilers = computed(() =>
  equipment.value.filter((row) => row.model_type === 'boiler'),
)

const chillers = computed(() =>
  equipment.value.filter((row) => row.model_type === 'chiller'),
)

const lighting = computed(() =>
  equipment.value.filter((row) => row.model_type === 'lighting'),
)

const electricalEquipment = computed(() =>
  equipment.value.filter((row) => row.model_type !== 'boiler'),
)

// ─── Electricity ──────────────────────────────────────────────────────────────

const electricDemandKw = computed(() =>
  electricalEquipment.value.reduce(
    (sum, row) => sum + (finite(row.power_kw) ? row.power_kw : 0),
    0,
  ),
)

const accumulatedElectricEnergyKwh = computed(() =>
  electricalEquipment.value.reduce(
    (sum, row) => sum + (finite(row.total_energy_kwh) ? row.total_energy_kwh : 0),
    0,
  ),
)

const electricalBreakdownRows = computed(() =>
  [...electricalEquipment.value]
    .filter((row) => finite(row.power_kw))
    .sort((a, b) => Number(b.power_kw ?? 0) - Number(a.power_kw ?? 0)),
)

const electricalBreakdownData = computed(() => ({
  labels: electricalBreakdownRows.value.map(equipmentLabel),
  datasets: [{
    label: 'Electrical Demand (kW)',
    data: electricalBreakdownRows.value.map((row) => Number(row.power_kw ?? 0)),
    backgroundColor: '#1890ff',
  }],
}))

// ─── Natural Gas ──────────────────────────────────────────────────────────────

const currentGasFlowCfm = computed(() =>
  boilers.value.reduce((sum, row) => {
    const value = row.inputs?.natural_gas_flow_cubic_feet_per_minute
    return sum + (finite(value) ? value : 0)
  }, 0),
)

const currentGasFlowM3h = computed(() =>
  boilers.value.reduce((sum, row) => {
    const direct = row.inputs?.natural_gas_flow_cubic_meters_per_hour
    if (finite(direct)) return sum + direct

    const cfm = row.inputs?.natural_gas_flow_cubic_feet_per_minute
    if (finite(cfm)) {
      return sum + (cfm * 60.0 / 35.3146667)
    }

    return sum
  }, 0),
)

const currentFuelInputKw = computed(() =>
  boilers.value.reduce(
    (sum, row) => sum + (finite(row.fuel_input_kw) ? row.fuel_input_kw : 0),
    0,
  ),
)

const accumulatedFuelEnergyKwh = computed(() =>
  boilers.value.reduce(
    (sum, row) => sum + (finite(row.total_fuel_energy_kwh) ? row.total_fuel_energy_kwh : 0),
    0,
  ),
)

// We cannot claim total gas volume yet because the backend currently accumulates
// fuel energy, not gas volume. Keep the UI honest until that accumulator exists.
const gasVolumeAvailable = computed(() => false)

// ─── Heating ──────────────────────────────────────────────────────────────────

const heatingOutputKw = computed(() =>
  boilers.value.reduce(
    (sum, row) => sum + (finite(row.thermal_output_kw) ? row.thermal_output_kw : 0),
    0,
  ),
)

const heatingEfficiencyPercent = computed(() => {
  if (currentFuelInputKw.value <= 0) return 0
  return (heatingOutputKw.value / currentFuelInputKw.value) * 100
})

// ─── Cooling ──────────────────────────────────────────────────────────────────

const coolingLoadKw = computed(() =>
  chillers.value.reduce(
    (sum, row) => sum + (finite(row.cooling_load_kw) ? row.cooling_load_kw : 0),
    0,
  ),
)

const chillerElectricalKw = computed(() =>
  chillers.value.reduce(
    (sum, row) => sum + (finite(row.power_kw) ? row.power_kw : 0),
    0,
  ),
)

const plantCop = computed(() => {
  if (chillerElectricalKw.value <= 0) return 0
  return coolingLoadKw.value / chillerElectricalKw.value
})

// ─── Lighting ─────────────────────────────────────────────────────────────────

const lightingDemandKw = computed(() =>
  lighting.value.reduce(
    (sum, row) => sum + (finite(row.power_kw) ? row.power_kw : 0),
    0,
  ),
)

const lightingEnergyKwh = computed(() =>
  lighting.value.reduce(
    (sum, row) => sum + (finite(row.total_energy_kwh) ? row.total_energy_kwh : 0),
    0,
  ),
)

const lightingBreakdownData = computed(() => ({
  labels: lighting.value.map(equipmentLabel),
  datasets: [{
    label: 'Lighting Demand (kW)',
    data: lighting.value.map((row) => Number(row.power_kw ?? 0)),
    backgroundColor: '#faad14',
  }],
}))

// ─── Utility History ──────────────────────────────────────────────────────────

const utilityHistory = computed<AggregatedUtilityHistory[]>(() => {
  const buckets = new Map<number, AggregatedUtilityHistory>()

  for (const row of history.value) {
    const timestamp = Number(row.timestamp)
    if (!Number.isFinite(timestamp)) continue

    const bucket = buckets.get(timestamp) ?? {
      timestamp,
      electric_kw: 0,
      cooling_kw: 0,
      boiler_fuel_kw: 0,
      heating_kw: 0,
      lighting_kw: 0,
    }

    if (row.model_type !== 'boiler' && finite(row.power_kw)) {
      bucket.electric_kw += row.power_kw
    }

    if (row.model_type === 'lighting' && finite(row.power_kw)) {
      bucket.lighting_kw += row.power_kw
    }

    const cooling = row.metrics?.cooling_load_kw
    if (finite(cooling)) {
      bucket.cooling_kw += cooling
    }

    const fuel = row.metrics?.fuel_input_kw
    if (finite(fuel)) {
      bucket.boiler_fuel_kw += fuel
    }

    const heating = row.metrics?.thermal_output_kw
    if (finite(heating)) {
      bucket.heating_kw += heating
    }

    buckets.set(timestamp, bucket)
  }

  return [...buckets.values()].sort(
    (a, b) => a.timestamp - b.timestamp,
  )
})

const electricalHistoryData = computed(() => ({
  labels: utilityHistory.value.map((row) => fmtTime(row.timestamp)),
  datasets: [{
    label: 'Electrical Demand (kW)',
    data: utilityHistory.value.map((row) => row.electric_kw),
    borderColor: '#1890ff',
    backgroundColor: 'rgba(24,144,255,0.10)',
    fill: true,
    tension: 0.3,
    pointRadius: 1,
  }],
}))

const gasHeatingHistoryData = computed(() => ({
  labels: utilityHistory.value.map((row) => fmtTime(row.timestamp)),
  datasets: [
    {
      label: 'Fuel Input (kW)',
      data: utilityHistory.value.map((row) => row.boiler_fuel_kw),
      borderColor: '#fa8c16',
      tension: 0.3,
      pointRadius: 1,
    },
    {
      label: 'Heating Output (kW)',
      data: utilityHistory.value.map((row) => row.heating_kw),
      borderColor: '#52c41a',
      tension: 0.3,
      pointRadius: 1,
    },
  ],
}))

const coolingHistoryData = computed(() => ({
  labels: utilityHistory.value.map((row) => fmtTime(row.timestamp)),
  datasets: [{
    label: 'Cooling Load (kW)',
    data: utilityHistory.value.map((row) => row.cooling_kw),
    borderColor: '#13c2c2',
    backgroundColor: 'rgba(19,194,194,0.08)',
    fill: true,
    tension: 0.3,
    pointRadius: 1,
  }],
}))

const lightingHistoryData = computed(() => ({
  labels: utilityHistory.value.map((row) => fmtTime(row.timestamp)),
  datasets: [{
    label: 'Lighting Demand (kW)',
    data: utilityHistory.value.map((row) => row.lighting_kw),
    borderColor: '#faad14',
    backgroundColor: 'rgba(250,173,20,0.08)',
    fill: true,
    tension: 0.3,
    pointRadius: 1,
  }],
}))

// ─── Chart options ─────────────────────────────────────────────────────────────

const chartOptions = computed(() => {
  const textColor = isDark.value
    ? 'rgba(255,255,255,0.65)'
    : 'rgba(0,0,0,0.65)'

  const gridColor = isDark.value
    ? 'rgba(255,255,255,0.08)'
    : 'rgba(0,0,0,0.06)'

  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false as const,
    plugins: {
      legend: {
        labels: { color: textColor },
      },
    },
    scales: {
      x: {
        ticks: { color: textColor },
        grid: { color: gridColor },
      },
      y: {
        beginAtZero: true,
        ticks: { color: textColor },
        grid: { color: gridColor },
      },
    },
  }
})

const horizontalBarOptions = computed(() => ({
  ...chartOptions.value,
  indexAxis: 'y' as const,
}))
</script>

<template>
  <div class="utilities-root" :class="{ dark: isDark }">
    <div class="utilities-toolbar">
      <div>
        <h2>Utilities</h2>
        <div class="subtitle">
          Building-level electricity, natural gas, heating, cooling, and lighting.
        </div>
      </div>

      <a-button :loading="loading" @click="refreshAll">
        <template #icon>
          <ReloadOutlined />
        </template>
        Refresh
      </a-button>
    </div>

    <a-alert
      v-if="error"
      type="warning"
      show-icon
      :message="error"
      class="utility-alert"
    />

    <a-alert
      v-else-if="!loading && !equipment.length"
      type="info"
      show-icon
      message="No energy models configured."
      description="Configure an energy model on a Chiller, Boiler, AHU, or Lighting device (device menu → Energy Model) to begin energy evaluation."
      class="utility-alert"
    />

    <!-- Electricity -->
    <section>
      <div class="section-title">
        <ThunderboltOutlined />
        <span>Electricity</span>
      </div>

      <div class="kpi-grid">
        <a-card size="small">
          <a-statistic
            title="Current Demand"
            :value="electricDemandKw"
            :precision="2"
            suffix="kW"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Accumulated Electric Energy"
            :value="accumulatedElectricEnergyKwh"
            :precision="3"
            suffix="kWh"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Electrical Consumers"
            :value="electricalEquipment.length"
          />
        </a-card>
      </div>

      <div class="chart-row">
        <a-card size="small" title="Electrical Breakdown" class="chart-card">
          <div class="chart-box">
            <Bar :data="electricalBreakdownData" :options="horizontalBarOptions" />
          </div>
        </a-card>

        <a-card size="small" title="Electrical Demand — Last 24 Hours" class="chart-card wide">
          <div class="chart-box">
            <Line :data="electricalHistoryData" :options="chartOptions" />
          </div>
        </a-card>
      </div>
    </section>

    <!-- Natural Gas -->
    <section>
      <div class="section-title">
        <FireOutlined />
        <span>Natural Gas</span>
      </div>

      <div class="kpi-grid">
        <a-card size="small">
          <a-statistic
            title="Current Gas Flow"
            :value="currentGasFlowCfm"
            :precision="2"
            suffix="CFM"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Current Gas Flow"
            :value="currentGasFlowM3h"
            :precision="2"
            suffix="m³/h"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Fuel Input"
            :value="currentFuelInputKw"
            :precision="2"
            suffix="kW"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Accumulated Fuel Energy"
            :value="accumulatedFuelEnergyKwh"
            :precision="3"
            suffix="kWh"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Gas Consumed"
            :value="0"
            suffix="m³"
            :value-style="{ color: '#8c8c8c' }"
          />
          <div class="future-note">
            Requires gas-volume accumulator
          </div>
        </a-card>
      </div>

      <a-alert
        v-if="!gasVolumeAvailable"
        type="info"
        show-icon
        message="Gas volume is not accumulated yet. Current flow and fuel energy are available now; total m³ can be added in the boiler model later."
        class="utility-alert"
      />
    </section>

    <!-- Heating -->
    <section>
      <div class="section-title">
        <DashboardOutlined />
        <span>Heating</span>
      </div>

      <div class="kpi-grid">
        <a-card size="small">
          <a-statistic
            title="Heat Output"
            :value="heatingOutputKw"
            :precision="2"
            suffix="kW"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Boiler Efficiency"
            :value="heatingEfficiencyPercent"
            :precision="1"
            suffix="%"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Active Boiler Models"
            :value="boilers.length"
          />
        </a-card>
      </div>

      <div class="chart-row">
        <a-card size="small" title="Fuel Input vs Heating Output — Last 24 Hours" class="chart-card wide">
          <div class="chart-box">
            <Line :data="gasHeatingHistoryData" :options="chartOptions" />
          </div>
        </a-card>
      </div>
    </section>

    <!-- Cooling -->
    <section>
      <div class="section-title">
        <DashboardOutlined />
        <span>Cooling</span>
      </div>

      <div class="kpi-grid">
        <a-card size="small">
          <a-statistic
            title="Cooling Load"
            :value="coolingLoadKw"
            :precision="2"
            suffix="kW"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Chiller Electrical Input"
            :value="chillerElectricalKw"
            :precision="2"
            suffix="kW"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Plant COP"
            :value="plantCop"
            :precision="2"
          />
        </a-card>
      </div>

      <div class="chart-row">
        <a-card size="small" title="Cooling Load — Last 24 Hours" class="chart-card wide">
          <div class="chart-box">
            <Line :data="coolingHistoryData" :options="chartOptions" />
          </div>
        </a-card>
      </div>
    </section>

    <!-- Lighting -->
    <section>
      <div class="section-title">
        <BulbOutlined />
        <span>Lighting</span>
      </div>

      <div class="kpi-grid">
        <a-card size="small">
          <a-statistic
            title="Lighting Demand"
            :value="lightingDemandKw"
            :precision="2"
            suffix="kW"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Accumulated Lighting Energy"
            :value="lightingEnergyKwh"
            :precision="3"
            suffix="kWh"
          />
        </a-card>

        <a-card size="small">
          <a-statistic
            title="Lighting Zones"
            :value="lighting.length"
          />
        </a-card>
      </div>

      <div class="chart-row">
        <a-card size="small" title="Lighting by Zone" class="chart-card">
          <div class="chart-box">
            <Bar :data="lightingBreakdownData" :options="horizontalBarOptions" />
          </div>
        </a-card>

        <a-card size="small" title="Lighting Demand — Last 24 Hours" class="chart-card wide">
          <div class="chart-box">
            <Line :data="lightingHistoryData" :options="chartOptions" />
          </div>
        </a-card>
      </div>
    </section>

    <div class="disclaimer">
      Current values are derived from the simulator's energy models. Accumulated
      values are model totals since the current energy-engine state began; they
      are not yet calendar-day utility billing totals.
    </div>
  </div>
</template>

<style scoped>
.utilities-root {
  height: 100%;
  overflow: auto;
  padding: 16px 20px 40px;
  background: #f5f5f5;
}

.utilities-root.dark {
  background: #141414;
  color: rgba(255, 255, 255, 0.85);
}

.utilities-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

.utilities-toolbar h2 {
  margin: 0;
  font-size: 20px;
}

.subtitle {
  margin-top: 2px;
  font-size: 12px;
  color: #888;
}

.utilities-root.dark .subtitle {
  color: rgba(255, 255, 255, 0.45);
}

section {
  margin-bottom: 28px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 10px;
  font-size: 13px;
  font-weight: 700;
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

.utilities-root.dark .section-title {
  color: rgba(255, 255, 255, 0.45);
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.chart-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}

.chart-card.wide {
  grid-column: span 2;
}

.chart-box {
  height: 240px;
  position: relative;
}

.utility-alert {
  margin-bottom: 12px;
}

.future-note {
  margin-top: 4px;
  color: #8c8c8c;
  font-size: 11px;
}

.disclaimer {
  margin-top: 8px;
  color: #888;
  font-size: 12px;
}

.utilities-root.dark .disclaimer {
  color: rgba(255, 255, 255, 0.45);
}

@media (max-width: 700px) {
  .chart-card.wide {
    grid-column: span 1;
  }

  .utilities-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
