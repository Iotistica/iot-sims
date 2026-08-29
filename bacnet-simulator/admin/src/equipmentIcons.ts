/** Maps a device's semantic equipment type (Device.equipment_type, from the
 * canonical EQUIPMENT_TYPES vocabulary in src/core/config.py) to one of the
 * custom tree icons commissioned for this vocabulary
 * (src/assets/tree-icons/) -- never derived/guessed from the device name.
 * Unclassified devices (or a class with no dedicated icon) fall back to a
 * generic equipment/controller glyph. Presentation-only: does not
 * introduce a second classification system. */
import { svgIcon } from './svgIcon'
import iconAhu from './assets/tree-icons/icon-equipment-ahu.svg?raw'
import iconVav from './assets/tree-icons/icon-equipment-vav.svg?raw'
import iconRooftopUnit from './assets/tree-icons/icon-equipment-rooftop-unit.svg?raw'
import iconBoiler from './assets/tree-icons/icon-equipment-boiler.svg?raw'
import iconChiller from './assets/tree-icons/icon-equipment-chiller.svg?raw'
import iconCoolingTower from './assets/tree-icons/icon-equipment-cooling-tower.svg?raw'
import iconPump from './assets/tree-icons/icon-equipment-pump.svg?raw'
import iconMeter from './assets/tree-icons/icon-equipment-meter.svg?raw'
import iconLighting from './assets/tree-icons/icon-equipment-lighting.svg?raw'
import iconFan from './assets/tree-icons/icon-equipment-fan.svg?raw'
import iconEquipmentFallback from './assets/tree-icons/icon-equipment-fallback.svg?raw'
import iconControllerGeneric from './assets/tree-icons/icon-controller-generic.svg?raw'

const EQUIPMENT_ICON_MAP = {
  Air_Handling_Unit: svgIcon(iconAhu),
  Variable_Air_Volume_Box: svgIcon(iconVav),
  // Rooftop_Unit is a real Brick subclass of Air_Handling_Unit (see
  // config.py) -- gets its own distinct "packaged unit" mark rather than
  // reusing the AHU icon.
  Rooftop_Unit: svgIcon(iconRooftopUnit),
  Boiler: svgIcon(iconBoiler),
  Chiller: svgIcon(iconChiller),
  Cooling_Tower: svgIcon(iconCoolingTower),
  Pump: svgIcon(iconPump),
  Meter: svgIcon(iconMeter),
  Lighting_Equipment: svgIcon(iconLighting),
  // Supply_Fan/Return_Fan are real Brick subclasses of Fan (see config.py)
  // -- share one "airflow" icon rather than inventing a distinct shape per
  // subclass.
  Fan: svgIcon(iconFan),
  Supply_Fan: svgIcon(iconFan),
  Return_Fan: svgIcon(iconFan),
}

const EQUIPMENT_ICON_FALLBACK = svgIcon(iconEquipmentFallback)
const CONTROLLER_ICON_FALLBACK = svgIcon(iconControllerGeneric)

export function getEquipmentIcon(equipmentType: string | null | undefined) {
  if (!equipmentType) return EQUIPMENT_ICON_FALLBACK
  return EQUIPMENT_ICON_MAP[equipmentType as keyof typeof EQUIPMENT_ICON_MAP] ?? EQUIPMENT_ICON_FALLBACK
}

/** Always the dedicated controller/network glyph, regardless of the
 * device's own (legacy) equipment_type field -- a Controller row now
 * routinely sits directly beside or nested under its Equipment row in the
 * Browse tree (see LeftSideView.vue's controls-based nesting), so it must
 * stay visually distinct from that Equipment's own icon rather than
 * borrowing it, even when the device still carries a classified
 * equipment_type from before the Equipment/Controller split existed. */
export function getControllerIcon(_equipmentType: string | null | undefined) {
  return CONTROLLER_ICON_FALLBACK
}
