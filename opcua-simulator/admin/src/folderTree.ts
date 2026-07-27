import type { Folder } from './types'

export interface FolderTreeNode {
  title: string
  value: number
  key: string
  children?: FolderTreeNode[]
}

/**
 * Builds <a-tree-select> tree-data from the flat folders list. When
 * excludeSubtreeOf is set (editing an existing folder), that folder and
 * everything under it is omitted so the picker can't offer a reparent that
 * would create a cycle — the backend also refuses this, but there's no
 * reason to let the UI offer an option it knows is invalid.
 */
export function buildFolderTreeOptions(folders: Folder[], excludeSubtreeOf?: number | null): FolderTreeNode[] {
  const excluded = new Set<number>()
  if (excludeSubtreeOf != null) {
    const collect = (id: number) => {
      excluded.add(id)
      for (const f of folders) if (f.parent_folder_id === id) collect(f.id)
    }
    collect(excludeSubtreeOf)
  }

  const byParent = new Map<number | null, Folder[]>()
  for (const f of folders) {
    if (excluded.has(f.id)) continue
    const list = byParent.get(f.parent_folder_id) ?? []
    list.push(f)
    byParent.set(f.parent_folder_id, list)
  }

  const build = (parentId: number | null): FolderTreeNode[] =>
    (byParent.get(parentId) ?? []).map((f) => ({
      title: f.name,
      value: f.id,
      key: String(f.id),
      children: build(f.id),
    }))

  return build(null)
}
