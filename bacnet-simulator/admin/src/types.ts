export interface Device {
  id: number
  device_instance: number
  name: string
  description: string
  vendor_name: string
  model_name: string
  enabled: number
  firmware_revision: string
  protocol_revision: number
  max_apdu_length_accepted: number
  segmentation_supported: string
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
  number_of_states: number
  reliability: string
  polarity: string
}

export interface Meta {
  object_types: string[]
  behaviors: string[]
  units: string[]
  reliability_options: string[]
  polarity_options: string[]
  segmentation_options: string[]
}

export interface PriorityArrayInfo {
  priority_array: (number | boolean | null)[]  // 16 slots, index 0 = priority 1
  relinquish_default: number | boolean
  current_command_priority: number | null
}

export interface Health {
  status: string
  bacnet_running: boolean
  devices: number
  sim_state: 'running' | 'paused' | 'stopped'
  elapsed_seconds: number
}

export interface NotificationRecipient {
  address?: string
  confirmed: boolean
  process_identifier: number
}

export interface NotificationClass {
  id: number
  device_id: number
  name: string
  priority_to_offnormal: number
  priority_to_fault: number
  priority_to_normal: number
  ack_required_transitions: string[]
  recipients: NotificationRecipient[]
}

export interface AlarmConfig {
  object_id: number
  notification_class_id: number | null
  enabled: boolean
  event_enable: string[]
  notify_type: string
  time_delay: number
  time_delay_normal: number
  params: Record<string, number | boolean | number[] | undefined>
}

export interface AlarmLogEntry {
  id: number
  object_id: number | null
  device_id: number
  object_name: string
  from_state: string
  to_state: string
  priority: number
  value: string
  message: string
  ts: string
  ack_required: number
  acknowledged: number
  ack_ts: string | null
  ack_by: string | null
}

export interface EventEnrollment {
  id: number
  device_id: number
  name: string
  monitored_object_id: number
  algorithm: string
  event_parameters: Record<string, number | boolean | number[] | undefined>
  notification_class_id: number | null
  enabled: boolean
  event_enable: string[]
  notify_type: string
  time_delay: number
  time_delay_normal: number
}

export interface TrendLog {
  id: number
  device_id: number
  name: string
  description: string
  monitored_object_id: number
  logging_type: string
  log_interval: number
  cov_increment: number
  buffer_size: number
  stop_when_full: number
  enabled: number
  record_count: number
  total_record_count: number
  last_sampled_at: number | null
}

export interface TrendLogRecord {
  id: number
  trend_log_id: number
  sequence_number: number
  ts: string
  value: string
  status_flags: string
}

export interface ScheduleTimeValue {
  time: string
  value: number | boolean
}

export interface ScheduleException {
  period:
    | { type: 'date'; date: string }
    | { type: 'date-range'; start: string; end: string }
    | { type: 'calendar-reference'; calendar_name: string }
  priority: number
  entries: ScheduleTimeValue[]
}

export interface ScheduleTargetInput {
  object_id: number
  property_identifier: string
}

export interface ScheduleTarget extends ScheduleTargetInput {
  object_name: string
  object_type: string
  object_instance: number
}

export interface Schedule {
  id: number
  device_id: number
  name: string
  description: string
  value_type: 'real' | 'boolean' | 'unsigned'
  schedule_default: number | boolean
  effective_start: string | null
  effective_end: string | null
  weekly_schedule: Record<string, ScheduleTimeValue[]>
  exception_schedule: ScheduleException[]
  priority_for_writing: number
  enabled: boolean
  targets: ScheduleTarget[]
}

export interface ScheduleEvaluation {
  present_value: number | boolean | null
  source: string
  matching_exception: unknown
  next_transition: string | null
}

export type CalendarDateEntry =
  | { type: 'date'; date: string }
  | { type: 'date-range'; start: string; end: string }
  | { type: 'weekday'; month: number | null; week_of_month: number | null; day_of_week: number | null }

export interface Calendar {
  id: number
  device_id: number
  name: string
  description: string
  date_list: CalendarDateEntry[]
  enabled: boolean
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
  number_of_states?: number
  reliability?: string
  polarity?: string
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
