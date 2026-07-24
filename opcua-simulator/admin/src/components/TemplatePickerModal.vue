<script setup lang="ts">
import { ref, computed, watch, type Component } from 'vue'
import { message } from 'ant-design-vue'
import {
  ControlOutlined,
  FilterOutlined,
  SyncOutlined,
  ClusterOutlined,
  FireOutlined,
  DashboardOutlined,
  ThunderboltOutlined,
  BulbOutlined,
  FolderOutlined,
  DeleteOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'

const props = defineProps<{
  open: boolean
  deviceId: number | undefined
  vendorName?: string
  modelName?: string
}>()
const emit  = defineEmits<{ 'update:open': [v: boolean]; applied: [] }>()

interface TplTag {
  data_type: string
  writable: boolean
  name: string
  unit: string
  behavior: string
  behavior_params: string
}

interface Template {
  key: string
  label: string
  desc: string
  icon: Component
  tags: TplTag[]
}

const TEMPLATES: Template[] = [
  {
    key: 'ahu',
    label: 'Air Handling Unit',
    desc: 'Supply/return fans, temps, valves, static pressure, alarms',
    icon: ControlOutlined,
    tags: [
      { data_type: 'Boolean', writable: false, name: 'SF-Run',             unit: '',      behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Boolean', writable: false, name: 'RF-Run',             unit: '',      behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'SF-Speed',           unit: 'percent', behavior: 'sine',      behavior_params: '{"base":75,"amplitude":15,"period_hours":12}' },
      { data_type: 'Double',  writable: false, name: 'RF-Speed',           unit: 'percent', behavior: 'sine',      behavior_params: '{"base":70,"amplitude":12,"period_hours":12}' },
      { data_type: 'Double',  writable: false, name: 'SAT',                unit: 'degC',    behavior: 'noise',     behavior_params: '{"base":13,"noise":0.4}' },
      { data_type: 'Double',  writable: false, name: 'RAT',                unit: 'degC',    behavior: 'sine',      behavior_params: '{"base":22,"amplitude":2,"period_hours":24}' },
      { data_type: 'Double',  writable: false, name: 'MAT',                unit: 'degC',    behavior: 'noise',     behavior_params: '{"base":16,"noise":0.8}' },
      { data_type: 'Double',  writable: false, name: 'OAT',                unit: 'degC',    behavior: 'sine',      behavior_params: '{"base":12,"amplitude":8,"period_hours":24}' },
      { data_type: 'Double',  writable: true,  name: 'OAD-Position',       unit: 'percent', behavior: 'sine',      behavior_params: '{"base":28,"amplitude":18,"period_hours":24}' },
      { data_type: 'Double',  writable: true,  name: 'CC-Valve',           unit: 'percent', behavior: 'sine',      behavior_params: '{"base":55,"amplitude":25,"period_hours":12}' },
      { data_type: 'Double',  writable: true,  name: 'HC-Valve',           unit: 'percent', behavior: 'sine',      behavior_params: '{"base":10,"amplitude":9,"period_hours":24}' },
      { data_type: 'Double',  writable: false, name: 'SA-Flow',            unit: 'CFM',     behavior: 'noise',     behavior_params: '{"base":8500,"noise":250}' },
      { data_type: 'Double',  writable: false, name: 'SA-Static-Pressure', unit: 'Pa',      behavior: 'noise',     behavior_params: '{"base":375,"noise":12}' },
      { data_type: 'Boolean', writable: false, name: 'Filter-DP-Alarm',    unit: '',      behavior: 'manual',      behavior_params: '{"value":false}' },
      { data_type: 'Boolean', writable: false, name: 'Freeze-Stat',        unit: '',      behavior: 'manual',      behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'vav',
    label: 'VAV Box',
    desc: 'Zone temp, airflow, damper, reheat valve, CO₂, occupancy',
    icon: FilterOutlined,
    tags: [
      { data_type: 'Double',  writable: false, name: 'Zone-Temp',     unit: 'degC',    behavior: 'noise',       behavior_params: '{"base":22,"noise":0.3}' },
      { data_type: 'Double',  writable: true,  name: 'Zone-Setpoint', unit: 'degC',    behavior: 'constant',    behavior_params: '{"value":22}' },
      { data_type: 'Double',  writable: false, name: 'Damper-Pos',    unit: 'percent', behavior: 'noise',       behavior_params: '{"base":55,"noise":3}' },
      { data_type: 'Double',  writable: true,  name: 'Damper-Cmd',    unit: 'percent', behavior: 'sine',        behavior_params: '{"base":55,"amplitude":14,"period_hours":8}' },
      { data_type: 'Double',  writable: false, name: 'Zone-Airflow',  unit: 'CFM',     behavior: 'noise',       behavior_params: '{"base":350,"noise":18}' },
      { data_type: 'Double',  writable: true,  name: 'Reheat-Valve',  unit: 'percent', behavior: 'sine',        behavior_params: '{"base":0,"amplitude":10,"period_hours":12}' },
      { data_type: 'Boolean', writable: false, name: 'Occupancy',     unit: '',      behavior: 'manual',        behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'Zone-CO2',      unit: 'ppm',     behavior: 'random_walk', behavior_params: '{"value":650,"step":30,"min":400,"max":1200}' },
    ],
  },
  {
    key: 'fcu',
    label: 'Fan Coil Unit',
    desc: 'Room temp, setpoint, cooling/heating valves, fan speeds',
    icon: SyncOutlined,
    tags: [
      { data_type: 'Double',  writable: false, name: 'Room-Temp',      unit: 'degC',    behavior: 'sine',     behavior_params: '{"base":23,"amplitude":1,"period_hours":24}' },
      { data_type: 'Double',  writable: true,  name: 'Room-Setpoint',  unit: 'degC',    behavior: 'constant', behavior_params: '{"value":22}' },
      { data_type: 'Double',  writable: false, name: 'Coil-Temp',      unit: 'degC',    behavior: 'noise',    behavior_params: '{"base":12,"noise":0.5}' },
      { data_type: 'Double',  writable: true,  name: 'Cooling-Valve',  unit: 'percent', behavior: 'manual',   behavior_params: '{"value":0}' },
      { data_type: 'Double',  writable: true,  name: 'Heating-Valve',  unit: 'percent', behavior: 'manual',   behavior_params: '{"value":0}' },
      { data_type: 'Boolean', writable: true,  name: 'Fan-Low-Speed',  unit: '',      behavior: 'manual',     behavior_params: '{"value":true}' },
      { data_type: 'Boolean', writable: true,  name: 'Fan-High-Speed', unit: '',      behavior: 'manual',     behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'chiller',
    label: 'Chiller Plant',
    desc: 'Dual chillers, condenser tower, CW loop flow & temps',
    icon: ClusterOutlined,
    tags: [
      { data_type: 'Boolean', writable: false, name: 'CH-1-Run',              unit: '',       behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'CH-1-kW',               unit: 'kW',     behavior: 'random_walk', behavior_params: '{"value":212,"step":8,"min":80,"max":320}' },
      { data_type: 'Double',  writable: false, name: 'CH-1-COP',              unit: '',       behavior: 'noise',       behavior_params: '{"base":5.8,"noise":0.2}' },
      { data_type: 'Boolean', writable: false, name: 'CH-2-Run',              unit: '',       behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'CH-2-kW',               unit: 'kW',     behavior: 'random_walk', behavior_params: '{"value":198,"step":8,"min":80,"max":320}' },
      { data_type: 'Double',  writable: false, name: 'CH-2-COP',              unit: '',       behavior: 'noise',       behavior_params: '{"base":5.6,"noise":0.2}' },
      { data_type: 'Double',  writable: false, name: 'CW-Supply-Temp',        unit: 'degC',   behavior: 'noise',       behavior_params: '{"base":6.5,"noise":0.2}' },
      { data_type: 'Double',  writable: false, name: 'CW-Return-Temp',        unit: 'degC',   behavior: 'noise',       behavior_params: '{"base":12.2,"noise":0.2}' },
      { data_type: 'Double',  writable: false, name: 'CW-Flow',               unit: 'L/s',    behavior: 'noise',       behavior_params: '{"base":48,"noise":1.5}' },
      { data_type: 'Double',  writable: false, name: 'CW-Diff-Pressure',      unit: 'Pa',     behavior: 'noise',       behavior_params: '{"base":225,"noise":8}' },
      { data_type: 'Boolean', writable: false, name: 'CT-Fan-1-Run',          unit: '',       behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Boolean', writable: false, name: 'CT-Fan-2-Run',          unit: '',       behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'CT-Leaving-Water-Temp', unit: 'degC',   behavior: 'noise',       behavior_params: '{"base":29.5,"noise":0.5}' },
      { data_type: 'Boolean', writable: false, name: 'CW-Pump-1-Run',         unit: '',       behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Boolean', writable: false, name: 'CW-Pump-2-Run',         unit: '',       behavior: 'manual',      behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'boiler',
    label: 'Hot Water Boiler',
    desc: 'Dual boilers, HW supply/return temps, pumps, gas flow',
    icon: FireOutlined,
    tags: [
      { data_type: 'Boolean', writable: false, name: 'BLR-1-Run',         unit: '',    behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'BLR-1-Firing-Rate', unit: 'percent', behavior: 'noise',   behavior_params: '{"base":62,"noise":5}' },
      { data_type: 'Double',  writable: false, name: 'BLR-1-Flue-Temp',   unit: 'degC',    behavior: 'noise',   behavior_params: '{"base":88,"noise":3}' },
      { data_type: 'Boolean', writable: false, name: 'BLR-2-Run',         unit: '',    behavior: 'manual',      behavior_params: '{"value":false}' },
      { data_type: 'Double',  writable: false, name: 'BLR-2-Firing-Rate', unit: 'percent', behavior: 'manual',  behavior_params: '{"value":0}' },
      { data_type: 'Double',  writable: false, name: 'HW-Supply-Temp',    unit: 'degC',    behavior: 'noise',   behavior_params: '{"base":71,"noise":0.8}' },
      { data_type: 'Double',  writable: false, name: 'HW-Return-Temp',    unit: 'degC',    behavior: 'noise',   behavior_params: '{"base":58.5,"noise":0.8}' },
      { data_type: 'Double',  writable: false, name: 'HW-Diff-Pressure',  unit: 'Pa',      behavior: 'noise',   behavior_params: '{"base":180,"noise":6}' },
      { data_type: 'Double',  writable: false, name: 'Gas-Flow',          unit: 'CFM',     behavior: 'random_walk', behavior_params: '{"value":44,"step":3,"min":10,"max":85}' },
      { data_type: 'Boolean', writable: false, name: 'HW-Pump-1-Run',     unit: '',    behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Boolean', writable: false, name: 'HW-Pump-2-Run',     unit: '',    behavior: 'manual',      behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'bms',
    label: 'BMS / Supervisor',
    desc: 'Building occupancy, alarms, energy, outside air conditions',
    icon: DashboardOutlined,
    tags: [
      { data_type: 'Boolean', writable: true,  name: 'Building-Occupied',    unit: '',    behavior: 'manual',      behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: false, name: 'Active-Alarms',        unit: '',    behavior: 'random_walk', behavior_params: '{"value":2,"step":1,"min":0,"max":8}' },
      { data_type: 'Double',  writable: false, name: 'Energy-Today-kWh',     unit: 'kWh', behavior: 'random_walk', behavior_params: '{"value":430,"step":12,"min":0,"max":2000}' },
      { data_type: 'Double',  writable: false, name: 'Peak-Demand-kW',       unit: 'kW',  behavior: 'random_walk', behavior_params: '{"value":182,"step":4,"min":50,"max":320}' },
      { data_type: 'Double',  writable: false, name: 'Outside-Air-Temp',     unit: 'degC',    behavior: 'sine',    behavior_params: '{"base":12,"amplitude":8,"period_hours":24}' },
      { data_type: 'Double',  writable: false, name: 'Outside-Air-Humidity', unit: 'percent', behavior: 'sine',    behavior_params: '{"base":55,"amplitude":15,"period_hours":24}' },
    ],
  },
  {
    key: 'meter',
    label: 'Electric Meter',
    desc: 'Active power, energy, voltage L1/L2, current, power factor',
    icon: ThunderboltOutlined,
    tags: [
      { data_type: 'Double', writable: false, name: 'Active-Power-kW', unit: 'kW',  behavior: 'noise',       behavior_params: '{"base":45,"noise":3}' },
      { data_type: 'Double', writable: false, name: 'Energy-kWh',      unit: 'kWh', behavior: 'random_walk', behavior_params: '{"value":1000,"step":0.05,"min":0,"max":999999}' },
      { data_type: 'Double', writable: false, name: 'Voltage-L1',      unit: 'V',   behavior: 'noise',       behavior_params: '{"base":230,"noise":2}' },
      { data_type: 'Double', writable: false, name: 'Voltage-L2',      unit: 'V',   behavior: 'noise',       behavior_params: '{"base":230,"noise":2}' },
      { data_type: 'Double', writable: false, name: 'Current-L1',      unit: 'A',   behavior: 'noise',       behavior_params: '{"base":65,"noise":4}' },
      { data_type: 'Double', writable: false, name: 'Power-Factor',    unit: '',    behavior: 'noise',       behavior_params: '{"base":0.92,"noise":0.03}' },
    ],
  },
  {
    key: 'lighting',
    label: 'Lighting Controller',
    desc: '3-zone dimming levels, overrides, occupancy, setpoints',
    icon: BulbOutlined,
    tags: [
      { data_type: 'Double',  writable: true, name: 'Zone-1-Level',       unit: 'percent', behavior: 'manual',   behavior_params: '{"value":100}' },
      { data_type: 'Double',  writable: true, name: 'Zone-2-Level',       unit: 'percent', behavior: 'manual',   behavior_params: '{"value":80}' },
      { data_type: 'Double',  writable: true, name: 'Zone-3-Level',       unit: 'percent', behavior: 'manual',   behavior_params: '{"value":60}' },
      { data_type: 'Boolean', writable: true, name: 'Zone-1-Override',    unit: '',    behavior: 'manual',       behavior_params: '{"value":false}' },
      { data_type: 'Boolean', writable: true, name: 'Zone-2-Override',    unit: '',    behavior: 'manual',       behavior_params: '{"value":false}' },
      { data_type: 'Boolean', writable: true, name: 'Occupancy-Status',   unit: '',    behavior: 'manual',       behavior_params: '{"value":true}' },
      { data_type: 'Double',  writable: true, name: 'Occupancy-Setpoint', unit: 'percent', behavior: 'constant', behavior_params: '{"value":100}' },
      { data_type: 'Double',  writable: true, name: 'Standby-Setpoint',   unit: 'percent', behavior: 'constant', behavior_params: '{"value":30}' },
    ],
  },
]

// ── User-saved templates (localStorage) ──────────────────────────────────────

interface StoredTemplate {
  key: string
  label: string
  desc: string
  tags: TplTag[]
  createdAt: string
}

const userTemplates = ref<StoredTemplate[]>([])

function loadUserTemplates() {
  try {
    userTemplates.value = JSON.parse(localStorage.getItem('opcua-sim-user-templates') || '[]')
  } catch {
    userTemplates.value = []
  }
}

function deleteUserTemplate(key: string, e: MouseEvent) {
  e.stopPropagation()
  const updated = userTemplates.value.filter(t => t.key !== key)
  localStorage.setItem('opcua-sim-user-templates', JSON.stringify(updated))
  userTemplates.value = updated
  if (selected.value === key) selected.value = null
}

// ─────────────────────────────────────────────────────────────────────────────

const selected = ref<string | null>(null)
const applying = ref(false)
const progress = ref(0)

// ── Smart suggestion based on manufacturer + model name ───────────────────────

const suggestedKey = computed<string | null>(() => {
  const text = `${props.vendorName ?? ''} ${props.modelName ?? ''}`.toLowerCase()
  if (!text.trim()) return null

  if (/\bvav\b|variable.air.vol/.test(text))                           return 'vav'
  if (/fan.coil|\bfcu\b/.test(text))                                   return 'fcu'
  if (/\bahu\b|air.handl/.test(text))                                  return 'ahu'
  if (/chiller|cooling.plant/.test(text))                              return 'chiller'
  if (/boiler|hot.water|heating.plant/.test(text))                     return 'boiler'
  if (/\bmeter\b|wattnode|power.analyz|powerscout|acurev|acuvim/.test(text)) return 'meter'
  if (/light|dimm|wavelinx|\bdali\b/.test(text))                       return 'lighting'
  if (/supervisor|workstation|\bbms\b|scada|webctrl|orcaview|pcvue|savic|enteli.?web/.test(text)) return 'bms'
  // manufacturer-specific hints
  if (/dent.instr|badger.meter|accuenergy|carlo.gav|watt.?node/.test(text)) return 'meter'
  if (/cooper.light|current.light|blue.ridge|bacmove|dali/.test(text)) return 'lighting'
  if (/belimo|danfoss|armstrong|condair/.test(text))                   return 'ahu'
  if (/delta.controls/.test(text) && /dvc|vav/.test(text))            return 'vav'

  return null
})

// Auto-select suggestion when modal opens; reload user templates
watch(() => props.open, (isOpen) => {
  if (isOpen) {
    loadUserTemplates()
    selected.value = suggestedKey.value
  }
})

function selectTemplate(key: string) {
  selected.value = selected.value === key ? null : key
}

async function apply() {
  if (!selected.value || !props.deviceId) return
  const tpl =
    TEMPLATES.find(t => t.key === selected.value) ??
    userTemplates.value.find(t => t.key === selected.value)
  if (!tpl) return
  applying.value = true
  progress.value = 0

  let ok = 0
  for (const tag of tpl.tags) {
    try {
      await api.tags.create(props.deviceId, { ...tag, enabled: 1 })
      ok++
    } catch {
      // skip duplicates / name conflicts
    }
    progress.value = Math.round(((ok) / tpl.tags.length) * 100)
  }

  applying.value = false
  selected.value = null
  message.success(`Applied "${tpl.label}" — ${ok} tag${ok !== 1 ? 's' : ''} created`)
  emit('update:open', false)
  emit('applied')
}
</script>

<template>
  <a-modal
    :open="open"
    title="Load Tag Template"
    :width="720"
    :footer="null"
    @cancel="emit('update:open', false)"
  >
    <div v-if="suggestedKey && (vendorName || modelName)" style="margin-bottom:10px;font-size:12px;color:#1890ff">
      Based on <strong>{{ vendorName }}{{ modelName ? ` — ${modelName}` : '' }}</strong>
    </div>

    <!-- User-saved templates -->
    <template v-if="userTemplates.length">
      <div style="font-size:11px;font-weight:700;color:#8c8c8c;text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px">My Templates</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px">
        <div
          v-for="tpl in userTemplates"
          :key="tpl.key"
          style="border:2px solid;border-radius:8px;padding:12px 14px;cursor:pointer;transition:all .15s;position:relative"
          :style="{
            borderColor: selected === tpl.key ? '#1890ff' : '#e8e8e8',
            background: selected === tpl.key ? '#e6f7ff' : 'white',
          }"
          @click="selectTemplate(tpl.key)"
        >
          <a-button
            type="text"
            size="small"
            danger
            style="position:absolute;top:6px;right:6px;padding:0 4px;height:20px;font-size:12px"
            title="Delete template"
            @click="deleteUserTemplate(tpl.key, $event)"
          >
            <template #icon><DeleteOutlined style="font-size:11px" /></template>
          </a-button>
          <FolderOutlined
            :style="{
              fontSize: '22px',
              color: selected === tpl.key ? '#1890ff' : '#8c8c8c',
              marginBottom: '6px',
              display: 'block',
            }"
          />
          <div style="font-weight:600;font-size:13px;margin-bottom:3px;padding-right:20px">{{ tpl.label }}</div>
          <div style="font-size:11px;color:#888;line-height:1.4">{{ tpl.desc || 'Custom template' }}</div>
          <div style="margin-top:6px;font-size:11px;color:#aaa">{{ tpl.tags.length }} tags · {{ tpl.createdAt }}</div>
        </div>
      </div>
      <a-divider style="margin:0 0 14px" />
      <div style="font-size:11px;font-weight:700;color:#8c8c8c;text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px">Built-in Templates</div>
    </template>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px">
      <div
        v-for="tpl in TEMPLATES"
        :key="tpl.key"
        style="border:2px solid;border-radius:8px;padding:12px 14px;cursor:pointer;transition:all .15s;position:relative"
        :style="{
          borderColor: selected === tpl.key ? '#1890ff' : suggestedKey === tpl.key ? '#91caff' : '#e8e8e8',
          background: selected === tpl.key ? '#e6f7ff' : 'white',
        }"
        @click="selectTemplate(tpl.key)"
      >
        <a-tag
          v-if="suggestedKey === tpl.key"
          color="blue"
          style="position:absolute;top:8px;right:8px;font-size:10px;line-height:16px;padding:0 5px"
        >Suggested</a-tag>
        <component
          :is="tpl.icon"
          :style="{
            fontSize: '22px',
            color: selected === tpl.key ? '#1890ff' : suggestedKey === tpl.key ? '#4096ff' : '#8c8c8c',
            marginBottom: '6px',
            display: 'block',
          }"
        />
        <div style="font-weight:600;font-size:13px;margin-bottom:3px">{{ tpl.label }}</div>
        <div style="font-size:11px;color:#888;line-height:1.4">{{ tpl.desc }}</div>
        <div style="margin-top:6px;font-size:11px;color:#aaa">{{ tpl.tags.length }} tags</div>
      </div>
    </div>

    <a-progress v-if="applying" :percent="progress" style="margin-bottom:12px" />

    <div style="display:flex;justify-content:flex-end;gap:8px">
      <a-button @click="emit('update:open', false)">Cancel</a-button>
      <a-button
        type="primary"
        :disabled="!selected"
        :loading="applying"
        @click="apply"
      >
        Apply Template
      </a-button>
    </div>
  </a-modal>
</template>
