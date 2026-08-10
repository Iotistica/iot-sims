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
  location_id?: number | null
  equipment_type?: string | null
  /** Explicit per-device override; null means "infer from equipment_type". */
  can_receive_event_notifications?: boolean | null
  /** Server-computed: the override if set, otherwise the equipment_type-based inference. */
  effective_can_receive_event_notifications?: boolean
  /** 'simulated' (default) or 'external-bacnet' (read-only, discovered from a real device). */
  source_type?: 'simulated' | 'external-bacnet'
  external_host?: string | null
  external_port?: number | null
  external_vendor_id?: number | null
  external_last_seen_at?: string | null
}

export interface Location {
  id: number
  name: string
  parent_location_id: number | null
  description: string
  kind?: string | null
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
  point_type?: string | null
  description?: string | null
}

/** SimObject shape plus a live-read value, returned by the external-device
 * discover/refresh endpoints. present_value is transient -- never persisted,
 * always re-read fresh (see Database.sync_external_objects's docstring). */
export interface ExternalObjectRow extends SimObject {
  present_value: unknown
}

export interface MetaOption {
  value: string
  label: string
}

export interface Meta {
  object_types: string[]
  behaviors: string[]
  units: string[]
  reliability_options: string[]
  polarity_options: string[]
  segmentation_options: string[]
  brick_version: string
  equipment_types: MetaOption[]
  point_types: MetaOption[]
  location_kinds: MetaOption[]
  semantic_predicates: MetaOption[]
  energy_model_types: MetaOption[]
  network_address: string | null
}

/** Brick Core semantic entity. Exactly one of device_id/object_id/location_id
 * is set for 'equipment'/'point' kinds; 'location' allows either location_id
 * (a real locations row) or device_id (a virtual, device-hosted location
 * such as a Lighting_Zone) but not both. semantic_key is always
 * server-derived -- never settable, see src/semantics/keys.py. */
export interface SemanticEntity {
  id: number
  name: string
  local_slug?: string | null
  semantic_key?: string | null
  brick_class: string
  entity_kind: 'equipment' | 'point' | 'location'
  device_id?: number | null
  object_id?: number | null
  location_id?: number | null
}

export interface SemanticRelationship {
  id: number
  source_entity_id: number
  predicate: 'isPointOf' | 'isPartOf' | 'feeds' | 'hasLocation'
  target_entity_id: number
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

export interface Settings {
  tick_seconds: number
  device_log_maxlen: number
  global_log_maxlen: number
  metrics_errors_maxlen: number
  metrics_new_devices_maxlen: number
  metrics_duplicate_id_maxlen: number
  metrics_traffic_feed_maxlen: number
  object_history_maxlen: number
  trend_log_default_interval: number
  trend_log_default_buffer_size: number
  jwt_expire_hours: number
}

export interface NotificationRecipient {
  recipient_type: 'device' | 'address'
  device_instance?: number | null
  ip_address?: string | null
  port?: number | null
  /** Legacy rows saved before the device/address split — "ip:port" string. */
  address?: string | null
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

/** parameters is model-type-specific (see src/energy/equipment/*.py's
 * config dataclasses) -- kept loosely typed here since the shape varies
 * by model_type; EnergyModelDrawer.vue owns the per-type field lists. */
export interface EnergyModelConfig {
  id: number
  device_id: number
  model_type: 'chiller' | 'ahu' | 'lighting' | 'boiler'
  instance_key: string
  enabled: boolean
  parameters: Record<string, number | boolean | null | undefined>
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

export interface SemanticSuggestionCandidate {
  brick_class: string
  score: number
  reasons: string[]
}

/** One suggestion row (the device itself, or a single point) from
 * POST /devices/{id}/semantic-suggestions. suggested_class/confidence are
 * always suppressed (null / 'none') when existing_class is already set --
 * existing user-entered semantics are authoritative and are never shown as
 * a competing suggestion, see that route's docstring. */
export interface SemanticSuggestionEntry {
  source_kind: 'device' | 'point'
  source_id: number
  source_name: string
  suggested_class: string | null
  existing_class: string | null
  confidence: 'high' | 'medium' | 'low' | 'none'
  score: number
  reasons: string[]
  alternatives: SemanticSuggestionCandidate[]
  /** 'rule' = deterministic engine (default), 'ai' = this one row was
   * refreshed via an explicit "Use AI" click. */
  source: 'rule' | 'ai'
}

export interface SemanticSuggestionsResponse {
  device: SemanticSuggestionEntry
  points: SemanticSuggestionEntry[]
}

export type ProjectSourceType = 'simulated' | 'external-bacnet'

export interface BACnetConnectionConfig {
  discovery_target: string | null
  device_instance_low: number
  device_instance_high: number
  timeout_ms: number
}

export interface Project {
  id: number
  name: string
  description: string
  created_at: string
  device_count: number
  source_type?: ProjectSourceType
  connection_config?: BACnetConnectionConfig | null
}

export interface DiscoveredDevice {
  name: string
  fingerprint: string
  host: string
  port: number
  device_instance: number
  confidence: 'high' | 'medium'
  discovered_at: string
  validated: boolean
  metadata: Record<string, unknown>
}

export interface BackupEntry {
  file_name: string
  created_at: string
  size_bytes: number
  checksum_sha256: string
  integrity: string
  label?: string | null
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
  point_type?: string | null
}

export interface DraftDevice {
  _id: string
  device_instance: number
  name: string
  description: string
  vendor_name: string
  model_name: string
  enabled: boolean
  equipment_type?: string | null
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

export type PacketDirection = 'inbound' | 'outbound'
export type PacketCaptureState = 'running' | 'stopped'

export interface PacketCaptureStatus {
  state: PacketCaptureState
  session_id: string | null
  started_at: string | null
  stopped_at: string | null
  packets_seen: number
  packets_stored: number
  packets_dropped: number
  inbound_count: number
  outbound_count: number
  bytes_captured: number
  max_packets: number
}

export interface CapturedPacket {
  packet_id: string
  session_id: string
  timestamp: number
  timestamp_utc: string
  direction: PacketDirection

  source_ip: string
  source_port: number
  destination_ip: string
  destination_port: number

  length: number
  raw_hex?: string

  decode_status: 'decoded' | 'partial' | 'warning' | 'error' | 'unknown'
  bvlc_function: string | null
  service_name: string | null
  decode_error: string | null

  simulator_device_id: number | null
  simulator_device_instance: number | null
  simulator_device_name: string | null
  simulator_object_id: number | null
  simulator_object_type: string | null
  simulator_object_instance: number | null
  simulator_object_name: string | null
}

export interface CapturedPacketPage {
  items: CapturedPacket[]
  total: number
  offset: number
  limit: number
}

export interface PacketCaptureFilters {
  direction?: PacketDirection
  sourceIp?: string
  destinationIp?: string
  service?: string
  deviceId?: number
  unassociated?: boolean
  offset?: number
  limit?: number
}

// ─── Functional Test Builder ────────────────────────────────────────────
// A constrained, BAS-commissioning-specific graph (NOT a general workflow
// engine) -- see admin/src/functionalTestSerializer.ts and
// admin/src/functionalTestValidation.ts. `params` per node type never
// contains free-form strings or expressions, only structured fields
// resolved against the existing meta.point_types/equipment_types
// vocabulary (same one DeviceDrawer/ObjectDrawer already use).

export type FunctionalTestOperator = 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte'

export type FunctionalTestNodeType =
  | 'start'
  | 'wait'
  | 'wait_until'
  | 'capture'
  | 'verify'
  | 'compare'
  | 'end'

export type FunctionalTestOperand =
  | { kind: 'point'; point_type: string }
  | { kind: 'constant'; value: string | number | boolean }
  | { kind: 'variable'; name: string; offset?: number }

export interface FunctionalTestNodeParams {
  start: Record<string, never>
  wait: { seconds: number }
  wait_until: {
    point_type: string
    operator: FunctionalTestOperator
    value: string | number | boolean
    timeout_seconds: number
  }
  capture: { point_type: string; variable: string }
  verify: { left: FunctionalTestOperand; operator: FunctionalTestOperator; right: FunctionalTestOperand }
  compare: { left: FunctionalTestOperand; operator: FunctionalTestOperator; right: FunctionalTestOperand }
  end: { result: 'pass' | 'fail' | 'inconclusive'; message?: string }
}

export interface FunctionalTestNodeDefinition<T extends FunctionalTestNodeType = FunctionalTestNodeType> {
  id: string
  type: T
  params: FunctionalTestNodeParams[T]
}

export interface FunctionalTestEdgeDefinition {
  source: string
  target: string
  /** Only 'verify' nodes use 'pass'/'fail' -- every other node's single
   * output edge uses null. */
  source_handle: 'pass' | 'fail' | null
}

export interface FunctionalTestDefinition {
  version: 1
  nodes: FunctionalTestNodeDefinition[]
  edges: FunctionalTestEdgeDefinition[]
  /** Editor-only node positions -- the (future) runner must never depend
   * on this, only on nodes/edges/params. */
  layout: Record<string, { x: number; y: number }>
}

export interface FunctionalTest {
  id: number
  name: string
  description: string
  equipment_type: string
  definition: FunctionalTestDefinition
  created_at: string
  updated_at: string
}

export interface FunctionalTestIssue {
  nodeId: string | null
  message: string
}

// ─── Functional Test execution (Phase 2) ────────────────────────────────
// Read-only: target selection, semantic point resolution, and run state.
// The graph itself is never sent to the run-creation endpoint -- only a
// target_device_id; the backend always loads the saved definition fresh
// and re-resolves every point itself (trust boundary).

export type ExecutionMode = 'external' | 'simulation'

export interface PointResolution {
  status: 'resolved' | 'missing' | 'ambiguous' | 'inconsistent'
  resolution_source: 'semantic_graph' | 'device_point_type'
  object: { id: number; name: string; object_type: string; object_instance: number } | null
  candidates: string[]
  message: string | null
}

export interface FunctionalTestResolveResponse {
  execution_mode: ExecutionMode
  points: Record<string, PointResolution>
}

export interface FunctionalTestRunDetail {
  node_id: string
  type: FunctionalTestNodeType
  outcome: string
  message: string | null
  started_at: string
  finished_at: string
}

export type FunctionalTestRunState =
  | 'pending' | 'running' | 'passed' | 'failed' | 'inconclusive' | 'cancelled' | 'error'

export interface FunctionalTestRun {
  id: number
  functional_test_id: number
  target_device_id: number
  execution_mode: ExecutionMode
  state: FunctionalTestRunState
  started_at: string | null
  finished_at: string | null
  result: 'pass' | 'fail' | 'inconclusive' | null
  result_message: string | null
  current_node_id: string | null
  error: string | null
  details: FunctionalTestRunDetail[]
  created_at: string
}
