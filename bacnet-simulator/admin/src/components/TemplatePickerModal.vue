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

interface TplObject {
  object_type: string
  object_instance: number
  name: string
  units: string
  behavior: string
  behavior_params: string
}

interface Template {
  key: string
  label: string
  desc: string
  icon: Component
  objects: TplObject[]
}

const TEMPLATES: Template[] = [
  {
    key: 'ahu',
    label: 'Air Handling Unit',
    desc: 'Supply/return fans, temps, valves, static pressure, alarms',
    icon: ControlOutlined,
    objects: [
      { object_type: 'binary-input',  object_instance:  1, name: 'SF-Run',              units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'binary-input',  object_instance:  2, name: 'RF-Run',              units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-input',  object_instance:  3, name: 'SF-Speed',            units: 'percent',               behavior: 'sine',        behavior_params: '{"base":75,"amplitude":15,"period_hours":12}' },
      { object_type: 'analog-input',  object_instance:  4, name: 'RF-Speed',            units: 'percent',               behavior: 'sine',        behavior_params: '{"base":70,"amplitude":12,"period_hours":12}' },
      { object_type: 'analog-input',  object_instance:  5, name: 'SAT',                 units: 'degrees-celsius',       behavior: 'noise',       behavior_params: '{"base":13,"noise":0.4}' },
      { object_type: 'analog-input',  object_instance:  6, name: 'RAT',                 units: 'degrees-celsius',       behavior: 'sine',        behavior_params: '{"base":22,"amplitude":2,"period_hours":24}' },
      { object_type: 'analog-input',  object_instance:  7, name: 'MAT',                 units: 'degrees-celsius',       behavior: 'noise',       behavior_params: '{"base":16,"noise":0.8}' },
      { object_type: 'analog-input',  object_instance:  8, name: 'OAT',                 units: 'degrees-celsius',       behavior: 'sine',        behavior_params: '{"base":12,"amplitude":8,"period_hours":24}' },
      { object_type: 'analog-output', object_instance:  9, name: 'OAD-Position',        units: 'percent',               behavior: 'sine',        behavior_params: '{"base":28,"amplitude":18,"period_hours":24}' },
      { object_type: 'analog-output', object_instance: 10, name: 'CC-Valve',            units: 'percent',               behavior: 'sine',        behavior_params: '{"base":55,"amplitude":25,"period_hours":12}' },
      { object_type: 'analog-output', object_instance: 11, name: 'HC-Valve',            units: 'percent',               behavior: 'sine',        behavior_params: '{"base":10,"amplitude":9,"period_hours":24}' },
      { object_type: 'analog-input',  object_instance: 12, name: 'SA-Flow',             units: 'cubic-feet-per-minute', behavior: 'noise',       behavior_params: '{"base":8500,"noise":250}' },
      { object_type: 'analog-input',  object_instance: 13, name: 'SA-Static-Pressure',  units: 'pascals',               behavior: 'noise',       behavior_params: '{"base":375,"noise":12}' },
      { object_type: 'binary-input',  object_instance: 14, name: 'Filter-DP-Alarm',     units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":false}' },
      { object_type: 'binary-input',  object_instance: 15, name: 'Freeze-Stat',         units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'vav',
    label: 'VAV Box',
    desc: 'Zone temp, airflow, damper, reheat valve, CO₂, occupancy',
    icon: FilterOutlined,
    objects: [
      { object_type: 'analog-input',  object_instance: 1, name: 'Zone-Temp',      units: 'degrees-celsius',       behavior: 'noise',       behavior_params: '{"base":22,"noise":0.3}' },
      { object_type: 'analog-value',  object_instance: 2, name: 'Zone-Setpoint',  units: 'degrees-celsius',       behavior: 'constant',    behavior_params: '{"value":22}' },
      { object_type: 'analog-input',  object_instance: 3, name: 'Damper-Pos',     units: 'percent',               behavior: 'noise',       behavior_params: '{"base":55,"noise":3}' },
      { object_type: 'analog-output', object_instance: 4, name: 'Damper-Cmd',     units: 'percent',               behavior: 'sine',        behavior_params: '{"base":55,"amplitude":14,"period_hours":8}' },
      { object_type: 'analog-input',  object_instance: 5, name: 'Zone-Airflow',   units: 'cubic-feet-per-minute', behavior: 'noise',       behavior_params: '{"base":350,"noise":18}' },
      { object_type: 'analog-output', object_instance: 6, name: 'Reheat-Valve',   units: 'percent',               behavior: 'sine',        behavior_params: '{"base":0,"amplitude":10,"period_hours":12}' },
      { object_type: 'binary-input',  object_instance: 7, name: 'Occupancy',      units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-input',  object_instance: 8, name: 'Zone-CO2',       units: 'parts-per-million',     behavior: 'random_walk', behavior_params: '{"value":650,"step":30,"min":400,"max":1200}' },
    ],
  },
  {
    key: 'fcu',
    label: 'Fan Coil Unit',
    desc: 'Room temp, setpoint, cooling/heating valves, fan speeds',
    icon: SyncOutlined,
    objects: [
      { object_type: 'analog-input',  object_instance: 1, name: 'Room-Temp',      units: 'degrees-celsius', behavior: 'sine',     behavior_params: '{"base":23,"amplitude":1,"period_hours":24}' },
      { object_type: 'analog-value',  object_instance: 2, name: 'Room-Setpoint',  units: 'degrees-celsius', behavior: 'constant', behavior_params: '{"value":22}' },
      { object_type: 'analog-input',  object_instance: 3, name: 'Coil-Temp',      units: 'degrees-celsius', behavior: 'noise',    behavior_params: '{"base":12,"noise":0.5}' },
      { object_type: 'analog-output', object_instance: 4, name: 'Cooling-Valve',  units: 'percent',         behavior: 'manual',   behavior_params: '{"value":0}' },
      { object_type: 'analog-output', object_instance: 5, name: 'Heating-Valve',  units: 'percent',         behavior: 'manual',   behavior_params: '{"value":0}' },
      { object_type: 'binary-output', object_instance: 6, name: 'Fan-Low-Speed',  units: 'no-units',        behavior: 'manual',   behavior_params: '{"value":true}' },
      { object_type: 'binary-output', object_instance: 7, name: 'Fan-High-Speed', units: 'no-units',        behavior: 'manual',   behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'chiller',
    label: 'Chiller Plant',
    desc: 'Dual chillers, condenser tower, CW loop flow & temps',
    icon: ClusterOutlined,
    objects: [
      { object_type: 'binary-input', object_instance:  1, name: 'CH-1-Run',              units: 'no-units',          behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-input', object_instance:  2, name: 'CH-1-kW',               units: 'kilowatts',         behavior: 'random_walk', behavior_params: '{"value":212,"step":8,"min":80,"max":320}' },
      { object_type: 'analog-input', object_instance:  3, name: 'CH-1-COP',              units: 'no-units',          behavior: 'noise',       behavior_params: '{"base":5.8,"noise":0.2}' },
      { object_type: 'binary-input', object_instance:  4, name: 'CH-2-Run',              units: 'no-units',          behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-input', object_instance:  5, name: 'CH-2-kW',               units: 'kilowatts',         behavior: 'random_walk', behavior_params: '{"value":198,"step":8,"min":80,"max":320}' },
      { object_type: 'analog-input', object_instance:  6, name: 'CH-2-COP',              units: 'no-units',          behavior: 'noise',       behavior_params: '{"base":5.6,"noise":0.2}' },
      { object_type: 'analog-input', object_instance:  7, name: 'CW-Supply-Temp',        units: 'degrees-celsius',   behavior: 'noise',       behavior_params: '{"base":6.5,"noise":0.2}' },
      { object_type: 'analog-input', object_instance:  8, name: 'CW-Return-Temp',        units: 'degrees-celsius',   behavior: 'noise',       behavior_params: '{"base":12.2,"noise":0.2}' },
      { object_type: 'analog-input', object_instance:  9, name: 'CW-Flow',               units: 'liters-per-second', behavior: 'noise',       behavior_params: '{"base":48,"noise":1.5}' },
      { object_type: 'analog-input', object_instance: 10, name: 'CW-Diff-Pressure',      units: 'pascals',           behavior: 'noise',       behavior_params: '{"base":225,"noise":8}' },
      { object_type: 'binary-input', object_instance: 11, name: 'CT-Fan-1-Run',          units: 'no-units',          behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'binary-input', object_instance: 12, name: 'CT-Fan-2-Run',          units: 'no-units',          behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-input', object_instance: 13, name: 'CT-Leaving-Water-Temp', units: 'degrees-celsius',   behavior: 'noise',       behavior_params: '{"base":29.5,"noise":0.5}' },
      { object_type: 'binary-input', object_instance: 15, name: 'CW-Pump-1-Run',         units: 'no-units',          behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'binary-input', object_instance: 16, name: 'CW-Pump-2-Run',         units: 'no-units',          behavior: 'manual',      behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'boiler',
    label: 'Hot Water Boiler',
    desc: 'Dual boilers, HW supply/return temps, pumps, gas flow',
    icon: FireOutlined,
    objects: [
      { object_type: 'binary-input', object_instance:  1, name: 'BLR-1-Run',        units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-input', object_instance:  2, name: 'BLR-1-Firing-Rate', units: 'percent',               behavior: 'noise',       behavior_params: '{"base":62,"noise":5}' },
      { object_type: 'analog-input', object_instance:  3, name: 'BLR-1-Flue-Temp',  units: 'degrees-celsius',       behavior: 'noise',       behavior_params: '{"base":88,"noise":3}' },
      { object_type: 'binary-input', object_instance:  4, name: 'BLR-2-Run',        units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":false}' },
      { object_type: 'analog-input', object_instance:  5, name: 'BLR-2-Firing-Rate', units: 'percent',              behavior: 'manual',      behavior_params: '{"value":0}' },
      { object_type: 'analog-input', object_instance:  6, name: 'HW-Supply-Temp',   units: 'degrees-celsius',       behavior: 'noise',       behavior_params: '{"base":71,"noise":0.8}' },
      { object_type: 'analog-input', object_instance:  7, name: 'HW-Return-Temp',   units: 'degrees-celsius',       behavior: 'noise',       behavior_params: '{"base":58.5,"noise":0.8}' },
      { object_type: 'analog-input', object_instance:  8, name: 'HW-Diff-Pressure', units: 'pascals',               behavior: 'noise',       behavior_params: '{"base":180,"noise":6}' },
      { object_type: 'analog-input', object_instance:  9, name: 'Gas-Flow',         units: 'cubic-feet-per-minute', behavior: 'random_walk', behavior_params: '{"value":44,"step":3,"min":10,"max":85}' },
      { object_type: 'binary-input', object_instance: 10, name: 'HW-Pump-1-Run',    units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'binary-input', object_instance: 11, name: 'HW-Pump-2-Run',    units: 'no-units',              behavior: 'manual',      behavior_params: '{"value":false}' },
    ],
  },
  {
    key: 'bms',
    label: 'BMS / Supervisor',
    desc: 'Building occupancy, alarms, energy, outside air conditions',
    icon: DashboardOutlined,
    objects: [
      { object_type: 'binary-value', object_instance: 1, name: 'Building-Occupied',    units: 'no-units',      behavior: 'manual',      behavior_params: '{"value":true}' },
      { object_type: 'analog-value', object_instance: 2, name: 'Active-Alarms',        units: 'no-units',      behavior: 'random_walk', behavior_params: '{"value":2,"step":1,"min":0,"max":8}' },
      { object_type: 'analog-input', object_instance: 3, name: 'Energy-Today-kWh',     units: 'kilowatt-hours',behavior: 'random_walk', behavior_params: '{"value":430,"step":12,"min":0,"max":2000}' },
      { object_type: 'analog-input', object_instance: 4, name: 'Peak-Demand-kW',       units: 'kilowatts',     behavior: 'random_walk', behavior_params: '{"value":182,"step":4,"min":50,"max":320}' },
      { object_type: 'analog-input', object_instance: 5, name: 'Outside-Air-Temp',     units: 'degrees-celsius',behavior: 'sine',       behavior_params: '{"base":12,"amplitude":8,"period_hours":24}' },
      { object_type: 'analog-input', object_instance: 6, name: 'Outside-Air-Humidity', units: 'percent',       behavior: 'sine',        behavior_params: '{"base":55,"amplitude":15,"period_hours":24}' },
    ],
  },
  {
    key: 'meter',
    label: 'Electric Meter',
    desc: 'Active power, energy, voltage L1/L2, current, power factor',
    icon: ThunderboltOutlined,
    objects: [
      { object_type: 'analog-input', object_instance: 1, name: 'Active-Power-kW', units: 'kilowatts',     behavior: 'noise',       behavior_params: '{"base":45,"noise":3}' },
      { object_type: 'analog-input', object_instance: 2, name: 'Energy-kWh',      units: 'kilowatt-hours',behavior: 'random_walk', behavior_params: '{"value":1000,"step":0.05,"min":0,"max":999999}' },
      { object_type: 'analog-input', object_instance: 3, name: 'Voltage-L1',      units: 'volts',         behavior: 'noise',       behavior_params: '{"base":230,"noise":2}' },
      { object_type: 'analog-input', object_instance: 4, name: 'Voltage-L2',      units: 'volts',         behavior: 'noise',       behavior_params: '{"base":230,"noise":2}' },
      { object_type: 'analog-input', object_instance: 5, name: 'Current-L1',      units: 'amperes',       behavior: 'noise',       behavior_params: '{"base":65,"noise":4}' },
      { object_type: 'analog-input', object_instance: 6, name: 'Power-Factor',    units: 'no-units',      behavior: 'noise',       behavior_params: '{"base":0.92,"noise":0.03}' },
    ],
  },
  {
    key: 'lighting',
    label: 'Lighting Controller',
    desc: '3-zone dimming levels, overrides, occupancy, setpoints',
    icon: BulbOutlined,
    objects: [
      { object_type: 'analog-output', object_instance: 1, name: 'Zone-1-Level',         units: 'percent',  behavior: 'manual',   behavior_params: '{"value":100}' },
      { object_type: 'analog-output', object_instance: 2, name: 'Zone-2-Level',         units: 'percent',  behavior: 'manual',   behavior_params: '{"value":80}' },
      { object_type: 'analog-output', object_instance: 3, name: 'Zone-3-Level',         units: 'percent',  behavior: 'manual',   behavior_params: '{"value":60}' },
      { object_type: 'binary-output', object_instance: 4, name: 'Zone-1-Override',      units: 'no-units', behavior: 'manual',   behavior_params: '{"value":false}' },
      { object_type: 'binary-output', object_instance: 5, name: 'Zone-2-Override',      units: 'no-units', behavior: 'manual',   behavior_params: '{"value":false}' },
      { object_type: 'binary-value',  object_instance: 6, name: 'Occupancy-Status',     units: 'no-units', behavior: 'manual',   behavior_params: '{"value":true}' },
      { object_type: 'analog-value',  object_instance: 7, name: 'Occupancy-Setpoint',   units: 'percent',  behavior: 'constant', behavior_params: '{"value":100}' },
      { object_type: 'analog-value',  object_instance: 8, name: 'Standby-Setpoint',     units: 'percent',  behavior: 'constant', behavior_params: '{"value":30}' },
    ],
  },
]

// ── User-saved templates (localStorage) ──────────────────────────────────────

interface StoredTemplate {
  key: string
  label: string
  desc: string
  objects: TplObject[]
  createdAt: string
}

const userTemplates = ref<StoredTemplate[]>([])

function loadUserTemplates() {
  try {
    userTemplates.value = JSON.parse(localStorage.getItem('bacnet-sim-user-templates') || '[]')
  } catch {
    userTemplates.value = []
  }
}

function deleteUserTemplate(key: string, e: MouseEvent) {
  e.stopPropagation()
  const updated = userTemplates.value.filter(t => t.key !== key)
  localStorage.setItem('bacnet-sim-user-templates', JSON.stringify(updated))
  userTemplates.value = updated
  if (selected.value === key) selected.value = null
}

// ─────────────────────────────────────────────────────────────────────────────

const selected = ref<string | null>(null)
const applying = ref(false)
const progress = ref(0)

// ── Smart suggestion based on vendor + model name ─────────────────────────────

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
  // vendor-specific hints
  if (/dent.instr|badger.meter|accuenergy|carlo.gav|watt.?node/.test(text)) return 'meter'
  if (/cooper.light|current.light|blue.ridge|bacmove|dali/.test(text)) return 'lighting'
  if (/belimo|danfoss|armstrong|condair/.test(text))                   return 'ahu'
  if (/delta.controls/.test(text) && /dvc|vav/.test(text))            return 'vav'
  // profile-type keywords that appear in many model names
  if (/\bb-bc\b|\bb-aac\b/.test(text))                                return 'bms'
  if (/\bb-ss\b/.test(text))                                           return 'meter'

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
  for (const obj of tpl.objects) {
    try {
      await api.objects.create(props.deviceId, { ...obj, enabled: 1 })
      ok++
    } catch {
      // skip duplicates / instance conflicts
    }
    progress.value = Math.round(((ok) / tpl.objects.length) * 100)
  }

  applying.value = false
  selected.value = null
  message.success(`Applied "${tpl.label}" — ${ok} object${ok !== 1 ? 's' : ''} created`)
  emit('update:open', false)
  emit('applied')
}
</script>

<template>
  <a-modal
    :open="open"
    title="Load Object Template"
    width="640"
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
          <div style="margin-top:6px;font-size:11px;color:#aaa">{{ tpl.objects.length }} objects · {{ tpl.createdAt }}</div>
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
        <div style="margin-top:6px;font-size:11px;color:#aaa">{{ tpl.objects.length }} objects</div>
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
