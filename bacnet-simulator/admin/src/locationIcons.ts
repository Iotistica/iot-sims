/** Maps a location's semantic kind (Location.kind, from the canonical
 * LOCATION_KINDS vocabulary in src/core/config.py) to one of the custom
 * tree icons commissioned for this vocabulary (src/assets/tree-icons/) --
 * never derived from the location's display name. Unclassified/
 * unrecognized kinds fall back to the dedicated fallback glyph. */
import { svgIcon } from './svgIcon'
import iconSite from './assets/tree-icons/icon-location-site.svg?raw'
import iconBuilding from './assets/tree-icons/icon-location-building.svg?raw'
import iconFloor from './assets/tree-icons/icon-location-floor.svg?raw'
import iconRoom from './assets/tree-icons/icon-location-room.svg?raw'
import iconZone from './assets/tree-icons/icon-location-zone.svg?raw'
import iconLightingZone from './assets/tree-icons/icon-location-lighting-zone.svg?raw'
import iconFallback from './assets/tree-icons/icon-location-fallback.svg?raw'

const LOCATION_ICON_MAP = {
  Site: svgIcon(iconSite),
  Building: svgIcon(iconBuilding),
  Floor: svgIcon(iconFloor),
  Room: svgIcon(iconRoom),
  Zone: svgIcon(iconZone),
  // Lighting_Zone is a real Brick subclass of Zone (see config.py) -- gets
  // its own distinct mark (zone shape + light-source cue) rather than
  // reusing Zone's icon.
  Lighting_Zone: svgIcon(iconLightingZone),
}

const LOCATION_ICON_FALLBACK = svgIcon(iconFallback)

export function getLocationIcon(kind: string | null | undefined) {
  if (!kind) return LOCATION_ICON_FALLBACK
  return LOCATION_ICON_MAP[kind as keyof typeof LOCATION_ICON_MAP] ?? LOCATION_ICON_FALLBACK
}
