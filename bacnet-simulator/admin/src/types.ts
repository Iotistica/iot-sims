export interface Device {
  id: number
  device_instance: number
  name: string
  description: string
  vendor_name: string
  model_name: string
  enabled: number
}

export interface SimObject {
  id: number
  device_id: number
  object_type: string
  object_instance: number
  name: string
  units: string
  behavior: string
  behavior_params: string
  enabled: number
  manual_value: number | null
}

export interface Meta {
  object_types: string[]
  behaviors: string[]
  units: string[]
}

export interface Health {
  status: string
  bacnet_running: boolean
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

export interface DraftObject {
  _id: string
  object_type: string
  object_instance: number
  name: string
  units: string
  behavior: string
  behavior_params: string
  enabled: boolean
}

export interface DraftDevice {
  _id: string
  device_instance: number
  name: string
  description: string
  vendor_name: string
  model_name: string
  enabled: boolean
  objects: DraftObject[]
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

// ─── Analytics ──────────────────────────────────────────────────────────────

export interface RecentRequest {
  ts: number
  service: string
  source?: string
  broadcast?: boolean
  device?: number | null
  object?: string | null
  ok: boolean
  latency_ms?: number | null
}

export interface RecentError {
  ts: number
  type: string
  service: string | null
  object: string | null
}

export interface DuplicateIdEvent {
  ts: number
  device_instance: number
  source: string
}

export interface NewDeviceEvent {
  ts: number
  device_instance: number
  source: string
}

export interface AnalyticsDeviceRow {
  id: number
  device_instance: number
  name: string
  enabled: boolean
  object_count: number
  activity: number
}

export interface AnalyticsSnapshot {
  ts: number
  overview: {
    total_devices: number
    online_devices: number
    offline_devices: number
    active_clients: number
    requests_per_sec: number
    avg_response_time_ms: number
    active_alarms: number
  }
  traffic: {
    requests_total: number
    reads_total: number
    writes_total: number
    requests_by_service: Record<string, number>
    broadcast: number
    unicast: number
    top_devices: { device_instance: number; name: string; count: number }[]
    recent_requests: RecentRequest[]
  }
  devices: {
    list: AnalyticsDeviceRow[]
    uptime_seconds: number
  }
  objects: {
    total: number
    unused: number
    top_accessed: { object: string; count: number }[]
    reads_total: number
    writes_total: number
  }
  performance: {
    avg_response_time_ms: number
    p95_response_time_ms: number
    throughput_per_sec: number
    concurrent_clients: number
    cpu_percent: number
    memory_mb: number
    error_rate_percent: number
  }
  errors: {
    total: number
    by_type: Record<string, number>
    duplicate_device_ids: DuplicateIdEvent[]
    recent: RecentError[]
  }
  discovery: {
    who_is_total: number
    devices_seen: number
    new_devices_timeline: NewDeviceEvent[]
  }
}
