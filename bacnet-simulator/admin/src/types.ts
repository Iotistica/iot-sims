export interface Device {
  id: number
  protocol: string
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
  /** Server-computed: whether this device already has an entity_kind='controller' semantic entity. */
  has_controller_entity?: boolean
  /** 'simulation' (default) | 'mirror' | 'replay' — Twin mode uses internal value 'mirror' and follows the external source. */
  simulation_mode?: 'simulation' | 'mirror' | 'replay'
  /** ID of the external source device for Twin/Replay modes; null for ordinary simulated devices. */
  source_device_id?: number | null
  /** ID of the Recording driving this device's values; only set when simulation_mode==='replay'. */
  active_recording_id?: number | null
  /** Server-computed summary of the enabled explicit simulation model driving this device's output points. */
  active_simulation_model?: {
    id: number
    name: string
    provider_type: 'fmu' | 'ai' | 'classifier'
    model_type: string
    model_count?: number
  } | null
  /** Server-computed: true when this device has an explicit simulation model configured but currently disabled (stopped). */
  simulation_model_stopped?: boolean
  /** Server-computed: the recording driving this device's values, when simulation_mode==='replay'. */
  active_recording?: { id: number; name: string } | null
}

export interface Location {
  id: number
  name: string
  parent_location_id: number | null
  description: string
  kind?: string | null
  /** Server-managed: explicit sibling ordering for auto-generated Building/Level hierarchies. */
  sort_order?: number | null
}

/** Response shape for GET /locations/{id}/deletion-impact -- see
 *  LocationDrawer.vue's doDelete() for how each field drives the
 *  confirmation dialog. */
export interface LocationDeletionImpact {
  sub_location_count: number
  device_count: number
  equipment_count: number
  point_count: number
  blocked: boolean
  blocking_points: Array<{
    point_name: string
    device_name: string
    model_name: string
    variable: string
  }>
  affected_simulation_models: Array<{ id: number; name: string }>
  affected_custom_graphs: Array<{ id: number; name: string }>
}

export interface Equipment {
  id: number
  name: string
  description: string
  location_id?: number | null
  equipment_type?: string | null
  manufacturer?: string | null
  model?: string | null
}

/** A candidate object for the Equipment panel's "Assign Points" action --
 * see GET /equipment/{id}/assignable-points. current_assignment is set
 * when the object's point is already isPointOf some other (or the same)
 * equipment/location entity -- the UI must surface this, never silently
 * reassign it. */
export interface AssignablePoint {
  object_id: number
  object_type: string
  object_instance: number
  name: string
  point_type?: string | null
  device_id: number
  device_name: string
  point_entity_id: number | null
  current_assignment: { entity_id: number; name: string } | null
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
  /** Optional BACnet State_Text list for a multi-state object (see
   * migration 031) -- one entry per state, index 0 = state 1. null means
   * no state text configured (every multi-state object before this field
   * existed). Primary producer is the Fault Detection Model attach flow
   * (ClassifierTrainingFlow.vue), auto-populated from a promoted
   * classifier's class_labels. */
  state_text?: string[] | null
  /** When present, this object's value is a live pass-through of another
   * object elsewhere (same device, another simulated device, or a live
   * external one) -- kept in sync by mirror_sync_loop's per-object link
   * pass (see POST .../objects/link). Its own `behavior` stays stored but
   * inactive, same as a whole-device Mirror. */
  source_object_id?: number | null
  /** Resolved display info for source_object_id -- attached by GET
   * .../objects, null unless source_object_id is set. */
  link_source?: { device_id: number; device_name: string; name: string } | null
  /** Explicit model output owner. When present, the provider (FMU/AI model) generates this point's raw value; `behavior` is applied on top of it every tick (see SimEngine._apply_fmu_behavior). */
  simulation_output_owner?: {
    id: number
    name: string
    provider_type: 'fmu' | 'ai' | 'classifier'
    model_type: string
    variable: string
  } | null
  /** Diagnostics: the provider's raw, unmodified value, before the configured Behavior is applied. Only populated for provider-owned points. */
  raw_provider_value?: number | boolean | null
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
  controller_types: MetaOption[]
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
  entity_kind: 'equipment' | 'point' | 'location' | 'controller'
  device_id?: number | null
  object_id?: number | null
  location_id?: number | null
  equipment_id?: number | null
}

export interface SemanticRelationship {
  id: number
  source_entity_id: number
  predicate: 'isPointOf' | 'isPartOf' | 'feeds' | 'hasLocation' | 'controls' | 'isHostedBy'
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
  fmu_runtime_url: string
  fmu_runtime_timeout_s: number
  fmu_runtime_api_key: string
  azure_openai_endpoint: string
  azure_openai_api_key: string
  azure_openai_deployment: string
  azure_openai_api_version: string
  llm_provider: 'azure_openai' | 'openai' | 'openai_compatible'
  openai_api_key: string
  openai_model: string
  openai_compatible_base_url: string
  openai_compatible_api_key: string
  openai_compatible_model: string
  recording_retention_enabled: boolean
  recording_retention_days: number
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
  model_type: 'chiller' | 'ahu' | 'lighting' | 'boiler' | 'rtu'
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

export interface RecordingPoint {
  id: number
  recording_id: number
  source_object_id: number | null
  object_type: string
  object_instance: number
  object_name: string
  point_type: string | null
  units: string | null
}

/** Second axis of a recording's ground-truth classification, alongside
 * `condition` (severity) -- what KIND of problem, not how bad. Real-world
 * HVAC FDD practice splits classification exactly this way. Fixed enum,
 * not free text -- a classifier needs a small, known, closed set of
 * classes to predict from. */
export type FaultType =
  // General -- any equipment with sensors/actuators/setpoints.
  | 'sensor_bias' | 'sensor_stuck' | 'actuator_stuck' | 'actuator_leak'
  | 'control_unstable' | 'incorrect_setpoint'
  // Air-side -- AHU/RTU/VAV.
  | 'simultaneous_heat_cool' | 'filter_fouling'
  // Refrigeration cycle -- RTU/Chiller/heat-pump DX equipment.
  | 'compressor_staging' | 'condenser_fouling' | 'evaporator_fouling'
  | 'refrigerant_overcharge' | 'refrigerant_undercharge' | 'refrigerant_line_restriction'
  // Hydronic/water-side -- Boiler/Chiller/Cooling Tower/Pump.
  | 'heat_exchanger_fouling' | 'water_flow_restriction' | 'cavitation'
  // Combustion -- Boiler-specific.
  | 'ignition_failure' | 'low_water_cutoff_fault'
  // Mechanical/rotating equipment -- Fan/Pump/Cooling Tower.
  | 'belt_or_drive_fault'

export interface Recording {
  id: number
  source_device_id: number
  name: string
  description: string
  /** Structured ground-truth classification (severity) -- null means
   * unclassified/legacy (a recording made before this field existed, or
   * never classified), never guessed/backfilled. Separate from `tags`
   * (Tags) below and from `fault_type` (what kind, not how bad). */
  condition?: 'normal' | 'degraded' | 'fault' | null
  /** What kind of problem, independent of condition's severity -- see
   * FaultType's own doc comment. null means unset -- the UI only shows/
   * edits this when condition isn't 'normal' (nothing to classify for a
   * normal recording). */
  fault_type?: FaultType | null
  /** Independent scenario/tag labels (e.g. "hot-day", "fan-test") -- never
   * read by replay playback, purely descriptive metadata. Not fed into any
   * training feature (see src/simulation/training_export.py's own module
   * docstring -- an earlier single-string `category` field did feed a
   * training column; that was retired, not carried over to this list). */
  tags: string[]
  status: 'recording' | 'completed'
  sample_interval_seconds: number
  maximum_samples: number
  buffer_mode: 'overwrite' | 'stop'
  started_at: string
  ended_at: string | null
  created_at: string
  point_count: number
  sample_count: number
  sample_index_min: number | null
  sample_index_max: number | null
  points?: RecordingPoint[]
}


export interface ReplayPlaybackState {
  status: 'playing' | 'paused' | 'stopped'
  current_sample_index: number
  speed: number
  loop: boolean
  last_advance_wall_time: number | null
}

// ── Calibration ─────────────────────────────────────────────────────────
// Standalone screen (CalibrationView.vue) gluing a completed Recording to
// iot-models' calibration API. Field names mirror the backend/iot-models
// response shapes verbatim -- see src/api/routers/calibration.py.

export interface CalibrationModelSummary {
  id: string
  slug?: string
  label: string
  description?: string | null
  calibration_enabled: boolean
}

export interface CalibrationRecordingSummary {
  id: number
  source_device_id: number
  device_name: string | null
  name: string
  description: string
  /** Structured ground-truth classification -- null means unclassified/
   * legacy, see Recording's own doc comment. */
  condition?: 'normal' | 'degraded' | 'fault' | null
  /** See FaultType's own doc comment. */
  fault_type?: FaultType | null
  /** See Recording.tags's own doc comment -- metadata only. */
  tags: string[]
  status: 'recording' | 'completed'
  sample_interval_seconds: number
  point_count: number
  sample_count: number
  started_at: string
  ended_at: string | null
}

export interface CalibrationMappingAlternative {
  point_id: number
  point_name: string
  score: number
  reasons: string[]
}

export interface CalibrationMappingVariable {
  name: string
  direction: 'input' | 'output'
  unit: string | null
  required: boolean
  suggested_point_id: number | null
  confidence: 'high' | 'medium' | 'low' | 'none' | string
  reasons: string[]
  alternatives: CalibrationMappingAlternative[]
  /** How this row was resolved: which semantic representation answered ('brick' today)
   * and how confidently ('exact' | 'class_only' | 'ambiguous' | 'unresolved').
   * `suggested_point_id` is only ever set for 'exact' / 'class_only'. */
  resolution?: { provider: string; quality: string }
  /** A SEPARATE name/unit-based guess, offered only when semantics did not bind. It is
   * never auto-applied and is never a semantic match -- the user must opt in. */
  name_match?: {
    mechanism: 'name'
    point_id: number | null
    point_name: string
    confidence: string
    reasons: string[]
  } | null
}

export interface CalibrationRecordingPoint {
  id: number
  name: string
  units: string | null
  point_type: string | null
  object_type: string
}

export interface CalibrationMappingSuggestions {
  variables: CalibrationMappingVariable[]
  points: CalibrationRecordingPoint[]
}

export interface CalibrationJob {
  job_id: string
  model_id: string
  method: string
  dataset_id: string
  validation_dataset_id?: string | null
  status: 'QUEUED' | 'VALIDATING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
  experiment_id: string | null
  created_at: number
  started_at: number | null
  completed_at: number | null
  error: string | null
  /** The bacnet-simulator calibration_runs row this compute job belongs to -- present on
   * POST /calibration/jobs and every GET /calibration/jobs/{id} response. */
  calibration_run_id?: number
}

// The five fit statistics run_hebo_calibration.py's own metric_pack() computes for a candidate
// -- surfaced in full (not just whichever one was the optimization objective), since they're
// directly relevant to judging an experimentally-calibrated model independent of whether the
// ASHRAE G14 check below applies at all. Any entry can be null (metric undefined for that
// evaluation, e.g. a zero-mean goal variable, or the evaluation failed).
export interface CalibrationMetrics {
  cv_rmse: number | null
  mae: number | null
  nmbe: number | null
  r2: number | null
  rmse: number | null
}

// An OPTIONAL, complementary statistical reference check -- never the sole/universal definition
// of "this model is calibrated" (see ashrae_g14.py's own module docstring in iot-models).
// `applicable` is false (with `reason`) whenever a real PASS/FAIL can't be produced: no
// calibration.validation.interval configured in model.json, an unrecognized interval value, or
// a missing cv_rmse/nmbe -- never a guessed hourly-vs-monthly band.
export interface AshraeG14Result {
  applicable: boolean
  reason?: string
  interval?: 'hourly' | 'monthly'
  cv_rmse?: number
  cv_rmse_threshold?: number
  cv_rmse_pass?: boolean
  nmbe?: number
  nmbe_threshold?: number
  nmbe_pass?: boolean
  pass?: boolean | null
  /** The PRIMARY ashrae_g14 is held-out-validation-only: 'validation' when it was scored
   * on a held-out replay, null when there was none (or it failed) -- in which case
   * `applicable` is false and `reason` says why. Fit-period G14 is exposed separately as
   * `CalibrationResult.calibration_fit_g14` and is a diagnostic, never a trust verdict. */
  based_on?: 'validation' | null
}

export interface CalibrationResult {
  job_id: string
  experiment_id: string | null
  model_id: string
  method: string
  dataset_id: string
  objective: {
    metric: string | null
    baseline: number | null
    best: number | null
    improvement_pct: number | null
    metrics: CalibrationMetrics | null
    baseline_metrics: CalibrationMetrics | null
  }
  /** Explicit, non-conflatable names: how the model did on the data it was TUNED against
   * vs. on HELD-OUT data. `validation_metrics` is null unless a held-out replay completed. */
  calibration_metrics?: CalibrationMetrics | null
  validation_metrics?: CalibrationMetrics | null
  /** Best-effort declared tuner start values from the FMU modelDescription -- provenance
   * only, may be partial or {}. Not the baseline (which is a no-override replay). */
  fmu_defaults?: Record<string, number>
  best_parameters: Record<string, number>
  execution: {
    evaluations: number | null
    failed_evaluations: number
    duration_seconds: number
  }
  /** PRIMARY verdict -- held-out validation only. */
  ashrae_g14: AshraeG14Result
  /** Fit-period G14 -- diagnostic ONLY, never presented as a trust verdict. */
  calibration_fit_g14?: AshraeG14Result
}

/** One row of the bacnet-simulator `calibration_runs` table (migration 033) -- the
 * durable workflow/audit record for a calibration. See src/api/routers/calibration.py. */
export interface CalibrationRun {
  id: number
  schema_version: number
  runtime_job_id: string | null
  runtime_dataset_id: string | null
  runtime_validation_dataset_id: string | null
  model_id: string
  simulation_model_id: number | null
  recording_id: number | null
  validation_recording_id: number | null
  fmu_sha256: string | null
  metadata_sha256: string | null
  tuner_names: string[]
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  failure_stage: string | null
  error_summary: string | null
  mapping_json: Record<string, {
    recording_point_id: number
    semantic_id: string | null
    selection_source: 'semantic_match' | 'user_override'
    semantic_resolution: { provider: string; quality: string; alternatives: number[] } | null
  }>
  objective_metric: string | null
  baseline_metric: number | null
  calibration_metric: number | null
  improvement_pct: number | null
  validation_status: 'not_requested' | 'pending' | 'completed' | 'failed'
  validation_cv_rmse: number | null
  validation_nmbe: number | null
  validation_rmse: number | null
  validation_mae: number | null
  validation_r2: number | null
  g14_interval: string | null
  g14_based_on: 'validation' | null
  g14_applicable: number | null
  g14_pass: number | null
  best_parameters_json: Record<string, number>
  fmu_defaults_json: Record<string, number>
  created_at: string
  started_at: string | null
  completed_at: string | null
  applied_at: string | null
  applied_to_simulation_model_id: number | null
}

// ── Training export (recordings -> iot-models training-job dataset) ──────
// Sibling to the Calibration* types above, but scoped to MULTIPLE
// recordings at once (every model input AND output, not just calibration's
// narrower inputs + one goal output) -- see
// src/api/routers/training_export.py for the exact response shapes these
// mirror.

// No scoring/guessing here -- a variable is either mapped on the device's
// own active Simulation Model config or it isn't (see training_export.py's
// get_training_mapping_suggestions own docstring for why Training stopped
// guessing point mappings itself).
export interface TrainingMappingVariable {
  name: string
  direction: 'input' | 'output'
  unit: string | null
  required: boolean
  /** The model's own catalog default for this variable, if it has one --
   * what the model uses when this variable is left out of training/runtime
   * entirely. */
  default: number | string | null
  mapped: boolean
  /** How `mapped` was satisfied -- 'point' (a live BACnet point, the usual
   * case) or 'constant' (no point at all; this input's value came from
   * what a recording itself snapshotted at record time -- see
   * constant_values). null when unmapped. */
  source: 'point' | 'constant' | null
  object_id: number | null
  point_name: string | null
  point_units: string | null
  /** Only set when source === 'constant' -- recording id -> the literal
   * value THAT recording captured for this variable at record time (can
   * differ between recordings if the device's config changed in between;
   * never the device's current live value). */
  constant_values: Record<number, number> | null
  /** Only meaningful when mapped -- recording ids (from the request's own
   * recording_ids) that can't supply this variable at all: for a
   * point-sourced variable, one that never captured a point for its
   * object_id; for a constant-sourced one, a recording made before
   * constant-value snapshotting existed (or before this variable was
   * constant-sourced). Including this variable will silently drop that
   * recording's rows from the training set. */
  missing_in_recordings: number[]
  /** Auto Brick-resolution attempt for this variable's own object_id -- null when the variable
   * isn't point-sourced at all (unmapped, or constant-sourced). See
   * src/semantics/feature_contract.py's resolve_point_semantic_identity. */
  semantic: { resolved: boolean; point_class: string | null; equipment_path: string[] | null } | null
}

export interface TrainingMappingSuggestions {
  variables: TrainingMappingVariable[]
  recording_ids: number[]
}

// ── Semantic feature contract (Brick-portable classifier features) ───────
// See src/semantics/feature_contract.py. Three identity kinds, chosen per feature: "brick"
// (auto-resolved, genuinely portable), "custom" (user-named, same-device-only this phase),
// "device_specific" (raw point id, same-device-only, honest fallback -- never chosen silently).
export type FeatureIdentityKind = 'brick' | 'custom' | 'device_specific'

export interface SemanticFeatureSpec {
  feature_name: string
  identity_kind: FeatureIdentityKind
  point_class?: string | null
  equipment_path?: string[] | null
  custom_label?: string | null
  source_device_id?: number | null
  source_object_id?: number | null
  bacnet_object_type?: string | null
  bacnet_object_instance?: number | null
  object_name?: string | null
  unit?: string | null
  lag?: number
}

/** The user's explicit decision for a variable Brick couldn't auto-resolve -- see
 * CreateTrainingExportJobRequest.feature_choices. Never a default; only ever set by an explicit
 * UI action. */
export type FeatureChoice =
  | { kind: 'custom'; custom_label: string }
  | { kind: 'device_specific' }

export interface SemanticCompatibilityResult {
  ok: boolean
  portability: 'portable' | 'partial' | 'device_specific' | null
  bound: Record<string, number>
  missing: Record<string, string>
  ambiguous: Record<string, number[]>
}

/** Result of promoting a completed training job -- a real, live,
 * attachable catalog entry (ModelMetadata.summary() from iot-models),
 * always a fresh model_id (see training_export.py's own docstring for
 * why promotion never overwrites an existing one in place). */
export interface PromotedModelSummary {
  id: string
  slug: string
  label: string
  runtime_type: 'onnx' | 'fmu' | 'classifier'
  onnx_path: string | null
  onnx_exists: boolean
  /** Only set for runtime_type === 'classifier' -- the fixed, ordered set
   * of class names this model's single output predicts, and which
   * recording field it was trained against. See iot-models'
   * ModelMetadata.class_labels/classifier_target. */
  class_labels?: string[] | null
  classifier_target?: 'condition' | 'fault_type' | null
  /** Brick-portable feature identity contract this model was trained with -- see
   * src/semantics/feature_contract.py. null for a model trained before this feature existed. */
  semantic_features?: SemanticFeatureSpec[] | null
  /** Least-portable identity kind present among semantic_features -- "portable" (every feature
   * is brick, attachable to any equivalent equipment), "partial" (some custom features, this
   * device only for those), "device_specific" (this device only, full stop). */
  portability?: 'portable' | 'partial' | 'device_specific' | null
  equipment_class?: string | null
}

export interface TrainingJob {
  job_id: string
  model_id: string
  dataset_id: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
  created_at: number
  started_at: number | null
  completed_at: number | null
  error: string | null
}

export interface TrainingResult {
  job_id: string
  model_id: string
  dataset_id: string
  wall_clock_seconds: number
  rows: { raw: number; supervised: number; train: number; test: number }
  trajectories: { train: number; test: number }
  aggregate: { mean_mae: number | null; mean_r2: number | null }
  per_output_metrics: Record<string, { mae: number | null; r2: number | null; n: number }>
  worst_outputs_by_r2: { output: string; r2: number | null }[]
  typical_outputs: Record<string, number>
  // train_baseline.py's own raw payload carries a few more fields (paths,
  // configuration echo) not surfaced in this drawer -- passed through
  // rather than dropped.
  [key: string]: unknown
}

/** Result of a completed classification (task_type: "classification")
 * training job -- iot-models' train_classifier.py result.json shape,
 * distinctly shaped from TrainingResult (accuracy/confusion_matrix, not
 * RÂ²/MAE). Detectable via "class_labels" in result, same convention
 * iot-models' own promote route uses server-side. */
export interface ClassificationResult {
  job_id: string
  model_id: string
  dataset_id: string
  wall_clock_seconds: number
  classifier_target: 'condition' | 'fault_type'
  class_labels: string[]
  rows: { raw: number; supervised: number; train: number; test: number }
  trajectories: { train: number; test: number }
  /** Per-class counts by BOTH recording (trajectory) and sample count --
   * never let one recording's many ticks read as many independent
   * experiments. Keyed by class label. */
  class_distribution: Record<string, { trajectories: number; samples: number }>
  /** Classes present in training but with zero held-out test coverage
   * (always the classes with only 1 recording) -- surfaced, not hidden;
   * their accuracy/per_class_metrics entry isn't meaningful this run. */
  untested_classes: string[]
  accuracy: number
  per_class_metrics: Record<string, { precision: number; recall: number; f1: number; support: number }>
  confusion_matrix: number[][]
  /** Brick-portable feature identity contract this run trained with -- null when no
   * feature_choices/semantic resolution was submitted at all (see feature_contract.py). */
  semantic_features?: SemanticFeatureSpec[] | null
  portability?: 'portable' | 'partial' | 'device_specific' | null
  equipment_class?: string | null
  [key: string]: unknown
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

export interface BACnetConnectionConfig {
  discovery_target: string | null
  device_instance_low: number
  device_instance_high: number
  timeout_ms: number
}

export interface BACnetDiscoveryConnection {
  id: number
  name: string
  target: string | null
  device_instance_low: number
  device_instance_high: number
  timeout_ms: number
  enabled: boolean
}

export interface Project {
  id: number
  name: string
  description: string
  created_at: string
  device_count: number
  connection_config?: BACnetConnectionConfig | null
  discovery_connections?: BACnetDiscoveryConnection[] | null
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
  category?: 'audit' | 'simulation'
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
  protocol: string
  transport: string
  direction: PacketDirection
  source: { address: string; port: number | null }
  destination: { address: string; port: number | null }
  length: number
  raw_hex?: string
  decode_status: 'decoded' | 'partial' | 'warning' | 'error' | 'unknown'
  operation: string | null
  status: string | null
  summary: Record<string, unknown>
  details: Record<string, unknown>
  decode_error: string | null
  simulator: { device_id: number | null; device_name: string | null; point_id: number | null; point_name: string | null }
}

export interface CapturedPacketPage {
  items: CapturedPacket[]
  total: number
  offset: number
  limit: number
}

export interface PacketCaptureFilters {
  direction?: PacketDirection
  protocol?: string
  operation?: string
  deviceId?: number
  unassociated?: boolean
  offset?: number
  limit?: number
}

// ─── Functional Test Builder ────────────────────────────────────────────
// A constrained, BAS-commissioning-specific graph (NOT a general workflow
// engine) -- see admin/src/functionalTestSerializer.ts and
// admin/src/functionalTestValidation.ts. `params` per node type never
// contains free-form strings or expressions, only structured fields.
// Every point reference is a concrete PointRef (a real device + a real
// BACnet object, picked at authoring time via PointPicker.vue) -- Brick
// semantic class (meta.point_types) is filter/display metadata only, never
// the primary identity, and is never itself part of a node's params.

/** A concrete BACnet point: one real object on one real device. Never a
 * semantic/Brick class placeholder -- see the module comment above. */
export interface PointRef {
  device_id: number
  object_id: number
}

/** One row from GET /points -- the cross-device listing PointPicker.vue
 * searches/filters. Device/Point Name are the primary identity; semantic
 * type/object typeÂ·instance/units/live value are secondary/display-only. */
export interface PointRow {
  object_id: number
  device_id: number
  device_name: string
  object_type: string
  object_instance: number
  name: string
  units: string | null
  point_type: string | null
  live_value: unknown
  source_type: 'simulated' | 'external-bacnet'
}

export type FunctionalTestOperator =
  | 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'within_tolerance'

export type FunctionalTestNodeType =
  | 'start'
  | 'wait'
  | 'wait_until'
  | 'capture'
  | 'verify'
  | 'compare'
  | 'set'
  | 'end'

// Additive-only discriminated union: a future `kind: 'aggregate'` variant
// (min/max/avg/count over a PointRef[], for Guideline-36-style checks) must
// be addable here without migrating existing saved operand JSON -- every
// site that pattern-matches `kind` (operandLabel() below, backend
// evaluate_operand()/_validate_operand()) must stay an exhaustive switch
// with an explicit "unknown kind" rejection, never a permissive fallback,
// so adding a new kind later is the only change required.
export type FunctionalTestOperand =
  | { kind: 'point'; point: PointRef }
  | { kind: 'constant'; value: string | number | boolean }
  | { kind: 'variable'; name: string; offset?: number }

export interface FunctionalTestNodeParams {
  start: Record<string, never>
  wait: { seconds: number; label?: string }
  wait_until: {
    point: PointRef
    operator: FunctionalTestOperator
    /** Was a flat literal -- now a full operand so it can reference a
     * captured baseline (e.g. "decreases vs. baseline"). */
    value: FunctionalTestOperand
    /** Required iff operator === 'within_tolerance'. */
    tolerance?: number
    /** Condition must hold continuously for this many seconds, not just
     * cross once -- guards against a single noisy simulation tick. */
    stable_for_seconds?: number
    timeout_seconds: number
    label?: string
  }
  capture: { point: PointRef; variable: string; label?: string }
  verify: {
    left: FunctionalTestOperand
    operator: FunctionalTestOperator
    right: FunctionalTestOperand
    tolerance?: number
    label?: string
  }
  compare: {
    left: FunctionalTestOperand
    operator: FunctionalTestOperator
    right: FunctionalTestOperand
    tolerance?: number
    label?: string
  }
  set: { point: PointRef; value: string | number | boolean; priority?: number; label?: string }
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

// ─── Custom Graph ─────────────────────────────────────────────────────────

export interface CustomGraphSeries {
  device_id: number
  object_id: number
  color: string
  axis: 'left' | 'right'
  visible: boolean
  /** 'pv' (Process Variable, the default -- omitted/absent means this) or
   * 'setpoint', rendered dashed instead of solid (see customGraphChart.ts's
   * buildChartData) -- a real-controller-style trend, PV vs. its Setpoint.
   * Sourced from a live point like any other series, never a typed-in
   * constant -- see migration 28's docstring. */
  role?: 'pv' | 'setpoint'
}

export interface CustomGraphDefinition {
  version: 1
  series: CustomGraphSeries[]
  /** Only 'live' exists this iteration -- there is no server-side history
   * range query behind /history (just a ~1h ring buffer per point), so a
   * preset picker would misrepresent what's actually available. Kept as a
   * field (not omitted) so a real range control has somewhere to go later
   * without a schema migration -- see CustomGraphModal.vue. */
  time_range: 'live'
}

/** widget_type='kpi' definition -- a single point's current value, no
 * chart/series concept at all. See migration 28's docstring. */
export interface KpiDefinition {
  device_id: number
  object_id: number
  decimals?: number
}

/** widget_type='table' definition -- one point's raw history shown as a
 * table. Time/Value are always shown; Min/Max/Avg are computed
 * client-side over the same fetched /history buffer (no server-side
 * aggregation exists) and toggled via `columns`. No "quality" column --
 * nothing in this app maps to it (see the Table widget plan's Context). */
export interface TableDefinition {
  device_id: number
  object_id: number
  columns?: { time?: boolean; value?: boolean; min?: boolean; max?: boolean; avg?: boolean }
  page_size?: number
}

export type WidgetType = 'chart' | 'kpi' | 'table'

export interface SavedGraph {
  id: number
  name: string
  /** 'chart' rows use CustomGraphDefinition; 'kpi'/'table' rows use
   * KpiDefinition/TableDefinition (same one-point shape, different
   * display) -- discriminate on widget_type, not on the shape of
   * `definition` itself. */
  definition: CustomGraphDefinition | KpiDefinition | TableDefinition
  dashboard_id: number
  widget_type: WidgetType
  grid_x: number
  grid_y: number
  grid_w: number
  grid_h: number
  created_at: string
  updated_at: string
}

/** Just a name to group SavedGraph (widget) rows under -- no content of
 * its own. See migration 28's docstring for why widgets live on the
 * existing custom_graphs table/SavedGraph type rather than a new one. */
export interface Dashboard {
  id: number
  name: string
  created_at: string
  updated_at: string
}

// ─── Functional Test execution ──────────────────────────────────────────
// Read-only: point readiness and run state. The graph itself is never sent
// to the run-creation endpoint, and neither is a target device -- every
// point in the saved definition already carries its own device, so the
// backend always loads the definition fresh and re-checks readiness itself
// (trust boundary), with nothing left for the caller to supply.

export type ExecutionMode = 'external' | 'simulation'

export interface PointReadiness {
  device_id: number | null
  object_id: number | null
  status: 'ok' | 'missing_device' | 'missing_object' | 'not_simulated'
  device_name: string | null
  object_name: string | null
  message: string | null
}

export interface FunctionalTestResolveResponse {
  points: PointReadiness[]
}

export interface FunctionalTestRunDetail {
  node_id: string
  /** Every real node type, plus the synthetic "restore" entries appended
   * after the run ends (one per Set write undone). */
  type: FunctionalTestNodeType | 'restore'
  outcome: string
  message: string | null
  started_at: string
  finished_at: string
  // Structured fields, populated per node type (all optional so today's
  // flat `${type} — ${message}` rendering keeps working unmodified) --
  // enough to build a future trend/evidence view without another schema
  // change. See functional_tests/executor.py's _record() extra payloads.
  point?: PointRef
  variable?: string
  value?: string | number | boolean
  operator?: FunctionalTestOperator
  tolerance?: number
  target?: FunctionalTestOperand
  left?: FunctionalTestOperand & { resolved_value?: unknown }
  right?: FunctionalTestOperand & { resolved_value?: unknown }
  priority?: number
  previous_state?: Record<string, unknown>
  action?: 'relinquished' | 'restored_manual' | 'restored_behavior' | null
}

export type FunctionalTestRunState =
  | 'pending' | 'running' | 'passed' | 'failed' | 'inconclusive' | 'cancelled' | 'error'

export interface FunctionalTestRun {
  id: number
  functional_test_id: number
  /** No longer a per-run selection -- every point already carries its own
   * device. Historical value on runs created before this change; always
   * null on new runs. */
  target_device_id: number | null
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

/** One object a Template applies to a device on creation -- object_type/
 * object_instance/name/units/behavior/behavior_params/point_type only (no
 * id/enabled -- those are assigned/forced when the template is applied,
 * see DeviceDrawer.vue and TemplatePickerModal.vue's applyTemplate()). */
export interface TplObject {
  object_type: string
  object_instance: number
  name: string
  units: string
  behavior: string
  behavior_params: string
  point_type?: string
}

/** A saved object template -- built-in (is_builtin=true, seeded, delete-
 * protected -- see src/db/migrations/registry.py's templates migration) or
 * user-created (POST /templates from SaveTemplateModal.vue). Both are the
 * same kind of row/response shape; only is_builtin distinguishes them.
 * equipment_types (optional) is used only to filter the picker in
 * DeviceDrawer.vue when adding a controller for equipment whose type is
 * already known -- a template with none set simply never appears in that
 * filtered list, but stays visible in TemplatePickerModal.vue's own
 * unfiltered browse-all view. */
export interface Template {
  id: number
  key: string
  label: string
  description: string
  objects: TplObject[]
  equipment_types: string[] | null
  is_builtin: boolean
  created_at: string
}
