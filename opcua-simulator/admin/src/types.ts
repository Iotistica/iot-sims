export interface Device {
  id: number
  key: string
  name: string
  description: string
  manufacturer: string
  model: string
  enabled: number
}

export interface Tag {
  id: number
  device_id: number
  name: string
  data_type: string
  writable: number
  unit: string
  behavior: string
  behavior_params: string
  enabled: number
  manual_value: number | null
}

export interface Meta {
  data_types: string[]
  behaviors: string[]
}

export interface Health {
  status: string
  opcua_running: boolean
  devices: number
  sim_state: 'running' | 'paused' | 'stopped'
  elapsed_seconds: number
}

export interface Profile {
  id: number
  name: string
  description: string
  created_at: string
  device_count: number
}

export interface LogEntry {
  ts: number
  level: 'info' | 'warn' | 'error'
  device_id?: number
  device_name?: string
  message: string
}

export interface HistoryPoint {
  ts: number
  value: number
}

export interface User {
  id: number
  username: string
  created_at: string
  last_login_at: string | null
}

export interface AuthResponse {
  access_token: string
  user: User
}

// ── NodeSet2 XML import (see docs/nodeset-import.md) ──────────────────────

export interface NodeSetParseReport {
  valid: boolean
  namespaces: number
  nodes_total: number
  objects: number
  variables: number
  methods: number
  types: number
  max_depth: number
  warnings: string[]
  errors: string[]
  unresolved_references: string[]
  unsupported_features: string[]
  duplicate_node_ids: string[]
}

export interface NodeSetPlanTag {
  name: string
  data_type: string
  writable: boolean
}

export interface NodeSetPlanDevice {
  name: string
  description: string
  tag_count: number
  tags: NodeSetPlanTag[]
}

export interface NodeSetPlanSummary {
  devices: NodeSetPlanDevice[]
  device_count: number
  tag_count: number
}

export interface NodeSetPreviewResponse {
  report: NodeSetParseReport
  plan: NodeSetPlanSummary
}

export interface NodeSetImportResponse {
  import_id: number | null
  devices_created: { id: number; name: string; tag_count: number }[]
  tags_created: number
  devices_skipped: string[]
  parse_report: NodeSetParseReport
  warnings: string[]
}

export interface NodeSetImportRecord {
  id: number
  source_filename: string
  device_ids: number[]
  device_count: number
  tag_count: number
  warning_count: number
  imported_at: string
}
