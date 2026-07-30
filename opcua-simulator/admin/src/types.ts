export interface Folder {
  id: number
  key: string
  name: string
  parent_folder_id: number | null
  description: string
  enabled: number
}

export interface Device {
  id: number
  key: string
  name: string
  description: string
  manufacturer: string
  model: string
  enabled: number
  folder_id: number | null
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

export interface Project {
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

// ── Analytics dashboard (see lib/analytics.py build_metrics_snapshot) ──────

export interface AnalyticsRecentRequest {
  ts: number
  service: string
  peer: string
  ok: boolean
  latency_ms: number
}

export interface AnalyticsNodeRef {
  node: string
  device: string | null
  tag: string
}

export interface AnalyticsSessionEvent {
  ts: number
  event: 'created' | 'activated' | 'closed' | 'auth_failed'
  session_id: string
  name?: [string, number]
  auth_method?: string | null
  duration_s?: number | null
  status?: string
}

export interface AnalyticsSessionRow {
  session_id: string
  peer: string
  state: 'Created' | 'Activated' | 'Closed'
  user_role: string | null
  timeout_s: number | null
}

export interface AnalyticsSubscriptionRow {
  subscription_id: number
  session_id: string
  publishing_interval_ms: number
  monitored_item_count: number
  avg_queue_size: number
}

export interface AnalyticsRecentError {
  ts: number
  service: string
  peer: string | null
  status: string
  node?: string
}

export interface AnalyticsAlarm {
  tag_id: number
  tag_name: string
  device_name: string
  fault_type: string
  severity: 'warning' | 'critical'
  opened_ts: number
  acknowledged: boolean
  acknowledged_ts: number | null
  cleared_ts: number | null
}

export interface AnalyticsAlarmEvent extends AnalyticsAlarm {
  ts: number
  event: 'open' | 'ack' | 'clear'
}

export interface AnalyticsSnapshot {
  ts: number
  overview: {
    total_servers: number
    active_sessions: number
    connected_clients: number
    subscriptions: number
    monitored_items: number
    requests_per_sec: number
    active_alarms: number
  }
  traffic: {
    requests_total: number
    requests_ok: number
    requests_failed: number
    requests_by_service: Record<string, number>
    reads_total: number
    writes_total: number
    browse_total: number
    call_total: number
    top_clients: { client: string; count: number }[]
    top_nodes: (AnalyticsNodeRef & { count: number })[]
    recent_requests: AnalyticsRecentRequest[]
  }
  sessions: {
    active: number
    created_total: number
    closed_total: number
    avg_duration_s: number
    list: AnalyticsSessionRow[]
    recent_events: AnalyticsSessionEvent[]
  }
  nodes: {
    top_read: (AnalyticsNodeRef & { count: number })[]
    top_written: (AnalyticsNodeRef & { count: number })[]
    reads_total: number
    writes_total: number
    distinct_nodes_accessed: number
  }
  subscriptions: {
    active: number
    monitored_items: number
    monitored_items_created_total: number
    monitored_items_deleted_total: number
    dropped_notifications: number
    list: AnalyticsSubscriptionRow[]
  }
  performance: {
    avg_response_time_ms: number
    p95_response_time_ms: number
    p99_response_time_ms: number
    requests_per_sec: number
    concurrent_clients: number
    cpu_percent: number
    memory_mb: number
    error_rate_percent: number
  }
  errors: {
    total: number
    by_type: Record<string, number>
    recent: AnalyticsRecentError[]
  }
  security: {
    policy: string
    anonymous_allowed: boolean
    secure_channel_opens: number
    auth_failures: number
    rejected_connections: number
  }
  alarms: {
    active: number
    active_list: AnalyticsAlarm[]
    avg_ack_time_s: number
    recent_events: AnalyticsAlarmEvent[]
  }
}
