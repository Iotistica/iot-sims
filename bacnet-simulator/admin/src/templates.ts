/** Object-template support that stays client-side even after the feature
 * moved to SQLite (src/db/migrations/registry.py's templates migration,
 * src/api/routers/templates.py) -- everything else (the template list
 * itself, both built-in and user-saved) now comes from `api.templates.*`;
 * see the `Template`/`TplObject` types in ./types.
 *
 * Two things remain here:
 * 1. BUILTIN_ICONS -- icons aren't serializable/stored in the database, so
 *    the 8 known built-in templates' icons stay a client-side lookup keyed
 *    by their stable `key` slug (not `id`, which can vary by deployment --
 *    see the migration's own docstring for why `key` is still there).
 * 2. migrateLocalStorageTemplates() -- a one-time bridge for anyone who
 *    saved a template under the old, purely-client-side localStorage
 *    behavior (including earlier in this same project's history) before
 *    this migration existed, so that data isn't silently lost.
 */
import type { Component } from 'vue'
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
} from '@ant-design/icons-vue'
import { api } from './api'
import type { TplObject } from './types'

export const BUILTIN_ICONS: Record<string, Component> = {
  ahu: ControlOutlined,
  vav: FilterOutlined,
  fcu: SyncOutlined,
  chiller: ClusterOutlined,
  boiler: FireOutlined,
  bms: DashboardOutlined,
  meter: ThunderboltOutlined,
  lighting: BulbOutlined,
}

/** Fallback for any template with no entry above -- every user template,
 * and matches what "My Templates" already used for all of them before
 * this move (a folder glyph, not a per-template icon). */
export const DEFAULT_TEMPLATE_ICON: Component = FolderOutlined

const USER_TEMPLATES_KEY = 'bacnet-sim-user-templates'

interface LegacyStoredTemplate {
  key: string
  label: string
  desc: string
  objects: TplObject[]
  createdAt: string
  equipmentTypes?: string[]
}

/** Call once at app startup. Not awaited by the caller (best-effort,
 * fire-and-forget) -- a failed migration here must never block the app
 * from loading; the localStorage key is only cleared once every entry it
 * held has actually been POSTed successfully. */
export async function migrateLocalStorageTemplates(): Promise<void> {
  let legacy: LegacyStoredTemplate[]
  try {
    legacy = JSON.parse(localStorage.getItem(USER_TEMPLATES_KEY) || '[]')
  } catch {
    return
  }
  if (!Array.isArray(legacy) || !legacy.length) return

  try {
    for (const tpl of legacy) {
      await api.templates.create({
        label: tpl.label,
        description: tpl.desc ?? '',
        objects: tpl.objects,
        equipment_types: tpl.equipmentTypes ?? null,
      })
    }
    localStorage.removeItem(USER_TEMPLATES_KEY)
  } catch {
    // Leave the localStorage key in place -- retried next app load rather
    // than losing anything that didn't make it through this time.
  }
}
