/** Maps a device's semantic equipment type (Device.equipment_type, from the
 * canonical EQUIPMENT_TYPES vocabulary in src/core/config.py) to an
 * existing @ant-design/icons-vue component -- never derived/guessed from
 * the device name. Unclassified devices (or a class with no dedicated
 * icon) fall back to a generic device/controller icon. Presentation-only:
 * does not introduce a second classification system. */
import type { Component } from 'vue'
import {
  ApiFilled,
  BoxPlotFilled,
  BulbFilled,
  CloudFilled,
  CloudServerOutlined,
  DashboardFilled,
  FireFilled,
  ReloadOutlined,
  SwapOutlined,
  SyncOutlined,
} from '@ant-design/icons-vue'

const EQUIPMENT_ICON_MAP: Record<string, Component> = {
  Air_Handling_Unit: SyncOutlined,
  Variable_Air_Volume_Box: BoxPlotFilled,
  Boiler: FireFilled,
  Chiller: CloudFilled,
  Cooling_Tower: CloudServerOutlined,
  Pump: ReloadOutlined,
  Meter: DashboardFilled,
  Lighting_Equipment: BulbFilled,
  // Supply_Fan/Return_Fan are real Brick subclasses of Fan (see config.py)
  // -- share one "airflow" icon rather than inventing a distinct shape per
  // subclass.
  Fan: SwapOutlined,
  Supply_Fan: SwapOutlined,
  Return_Fan: SwapOutlined,
}

export function getEquipmentIcon(equipmentType: string | null | undefined): Component {
  if (!equipmentType) return ApiFilled
  return EQUIPMENT_ICON_MAP[equipmentType] ?? ApiFilled
}
