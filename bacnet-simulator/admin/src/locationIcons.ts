/** Maps a location's semantic kind (Location.kind, from the canonical
 * LOCATION_KINDS vocabulary in src/core/config.py) to an existing
 * @ant-design/icons-vue component -- never derived from the location's
 * display name. Unclassified/unrecognized kinds fall back to the existing
 * generic folder icon. Filled variants (where available) are used over
 * thin outlines for stronger visual weight at tree-icon sizes. */
import type { Component } from 'vue'
import {
  AppstoreFilled,
  BankFilled,
  BlockOutlined,
  EnvironmentFilled,
  FolderFilled,
  LayoutFilled,
} from '@ant-design/icons-vue'

const LOCATION_ICON_MAP: Record<string, Component> = {
  Site: EnvironmentFilled,
  Building: BankFilled,
  Floor: LayoutFilled,
  // Distinct grid-square shape -- deliberately not another rectangle/panel
  // icon, so Room reads differently from Floor at a glance.
  Room: AppstoreFilled,
  Zone: BlockOutlined,
  // Lighting_Zone is a real Brick subclass of Zone (see config.py) -- shares
  // Zone's icon rather than inventing a distinct shape for a subclass.
  Lighting_Zone: BlockOutlined,
}

export function getLocationIcon(kind: string | null | undefined): Component {
  if (!kind) return FolderFilled
  return LOCATION_ICON_MAP[kind] ?? FolderFilled
}
