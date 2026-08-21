import { ref } from 'vue'

import type {
  Device,
  SimObject,
  Meta,
  Health,
  Settings,
  Project,
  BackupEntry,
  LogEntry,
  HistoryPoint,
  User,
  AuthResponse,
  AnalyticsSnapshot,
  NotificationClass,
  EnergyModelConfig,
  AlarmConfig,
  AlarmLogEntry,
  EventEnrollment,
  TrendLog,
  TrendLogRecord,
  Schedule,
  ScheduleEvaluation,
  PriorityArrayInfo,
  Calendar,
  Location,
  Equipment,
  AssignablePoint,
  SemanticEntity,
  SemanticRelationship,
  PacketCaptureStatus,
  CapturedPacket,
  CapturedPacketPage,
  PacketCaptureFilters,
  BACnetConnectionConfig,
  BACnetDiscoveryConnection,
  DiscoveredDevice,
  ExternalObjectRow,
  SemanticSuggestionsResponse,
  SemanticSuggestionEntry,
  FunctionalTest,
  FunctionalTestDefinition,
  FunctionalTestResolveResponse,
  FunctionalTestRun,
  PointRow,
  CustomGraphDefinition,
  SavedGraph,
} from './types'

import { authToken, logout } from './auth'

// Tracks whether the live simulator config (devices/objects/locations/
// semantics/notification-classes/trend-logs/schedules/calendars) has
// changed since the last project save/load, so the UI (e.g. "New Project")
// can warn about losing real changes instead of unconditionally nagging
// every time regardless of whether anything actually changed.
export const projectDirty = ref(false)

// Sub-paths that mutate but represent transient/runtime state, not saved
// project configuration — writing a live value, triggering/clearing a
// trend log, or evaluating a schedule doesn't change what a project save
// would capture.
const NON_CONFIG_MUTATION = [
  /\/objects\/\d+\/value$/,
  /\/priority-array\/\d+$/,
  /\/trend-logs\/\d+\/(trigger|clear)$/,
  /\/schedules\/\d+\/evaluate$/,
  // Present-value-only refresh (ObjectsPanel's 3s external-device poll) --
  // never persisted, see sync_external_objects()'s own docstring. Without
  // this, Save would look "stuck" enabled again within a few seconds of
  // saving any project with an external device selected, since the poll
  // keeps firing on its own timer regardless of Save.
  /\/external-objects\/refresh$/,
  // Suggest Semantics generation + the per-point "Use AI" fallback are
  // both explicitly side-effect-free (suggestion only, point_type/
  // equipment_type are never written here) -- only the separate Apply
  // Selected step, which goes through the ordinary device/object PUT
  // routes above, actually changes anything.
  /\/semantic-suggestions(\/points\/\d+\/ai)?$/,
  /\/simulation\/models\/mapping-suggestions(\/ai)?$/,
  /\/simulation\/models\/\d+\/reload$/,
  /\/simulation\/models\/reconcile$/,
]

const CONFIG_PATH_PREFIXES = [
  /^\/devices(\/|$)/,
  /^\/locations(\/|$)/,
  /^\/equipment(\/|$)/,
  /^\/semantic-entities(\/|$)/,
  /^\/semantic-relationships(\/|$)/,
  /^\/notification-classes(\/|$)/,
  /^\/trend-logs(\/|$)/,
  /^\/schedules(\/|$)/,
  /^\/calendars(\/|$)/,
  /^\/event-enrollments(\/|$)/,
  /^\/simulation\/models(\/|$)/,
]

function markDirtyIfConfigMutation(path: string, method: string | undefined): void {
  if (!method || method === 'GET') return
  if (NON_CONFIG_MUTATION.some((re) => re.test(path))) return
  if (CONFIG_PATH_PREFIXES.some((re) => re.test(path))) projectDirty.value = true
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
  return headers
}

function errorMessageFromDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail

  if (Array.isArray(detail)) {
    // FastAPI/Pydantic validation errors: detail is a list of
    // { loc, msg, type } objects, not a string.
    return detail.map((d) => (d as { msg?: string })?.msg).filter(Boolean).join('; ')
  }

  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    const runtimeError = (detail as { runtime_error?: unknown }).runtime_error
    if (typeof message === 'string') {
      return typeof runtimeError === 'string' && runtimeError
        ? `${message}: ${runtimeError}`
        : message
    }
    if (typeof runtimeError === 'string') return runtimeError
  }

  return ''
}

async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const res = await fetch(path, { headers: authHeaders() })
  if (!res.ok) throw new Error(res.statusText)
  const disposition = res.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^"]+)"?/.exec(disposition)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = match?.[1] ?? fallbackName
  a.click()
  URL.revokeObjectURL(url)
}

async function uploadFile<T>(path: string, file: File, fields: Record<string, string> = {}, markDirty = false): Promise<T> {
  const body = new FormData()
  body.append('file', file)
  for (const [k, v] of Object.entries(fields)) body.append(k, v)
  const res = await fetch(path, { method: 'POST', headers: authHeaders(), body })
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = (e as { detail?: unknown }).detail
    throw new Error(errorMessageFromDetail(detail) || res.statusText)
  }
  if (markDirty) projectDirty.value = true
  return res.json() as Promise<T>
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
  const res = await fetch(path, { headers, ...init })
  if (!res.ok) {
    if (res.status === 401) logout()
    const e = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = (e as { detail?: unknown }).detail
    const message = errorMessageFromDetail(detail)
    throw new Error(message || res.statusText)
  }
  markDirtyIfConfigMutation(path, init?.method)
  if (res.status === 204) return null as T
  return res.json() as Promise<T>
}


export type SimulationProviderType =
  | 'builtin'
  | 'fmu'
  | 'learned'

export interface SimulationProviderCatalogEntry {
  provider_type: SimulationProviderType
  label: string
  available: boolean
  persistent_model_required: boolean
  description: string
}

export interface SimulationModelParameterDefinition {
  name: string
  label: string
  type: string
  default?: unknown
  unit?: string | null
  required?: boolean
  advanced?: boolean
  minimum?: number | null
  maximum?: number | null
}

export interface SimulationModelMappingHints {
  equipment_scope: 'self' | 'upstream' | 'downstream' | 'any'
  preferred_equipment_types?: string[]
  relationship?: 'feeds' | 'isPartOf' | null
  signal_role?: string | null
}

export interface SimulationModelVariableDefinition {
  name: string
  label: string
  direction: 'input' | 'output'
  unit?: string | null
  default?: unknown
  required?: boolean
  suggested_point_types?: string[]
  /** Already present in the backend catalog response (VariableDefinition's
   * mapping_hints, asdict()'d) -- this type just declares it. Used to rank
   * an Aggregate input's point picker (equipment_scope/preferred_equipment_types/
   * signal_role), never to hard-code any specific variable in the UI. */
  mapping_hints?: SimulationModelMappingHints | null
}

export interface SimulationModelCatalogEntry {
  model_type: string
  label: string
  provider_type: Exclude<SimulationProviderType, 'builtin'>
  description: string
  parameters: SimulationModelParameterDefinition[]
  inputs: SimulationModelVariableDefinition[]
  outputs: SimulationModelVariableDefinition[]
}

export interface SimulationModelMapping {
  id?: number
  model_config_id?: number
  variable: string
  direction: 'input' | 'output'
  point_id: number
  device_id?: number
  device_name?: string
  point_name?: string
  object_type?: string
  object_instance?: number
  units?: string
  point_type?: string | null
}

/** point_metadata keys are point_ids -- always strings once this crosses
 * the JSON wire, even though the backend builds the dict with int keys
 * internally (see model_store._load_aggregate_mappings). Weight points
 * (weighted_average only) share the same point_metadata dict as value
 * points -- a lookup by point_id works identically for either. */
export interface SimulationModelAggregateMapping {
  id?: number
  model_config_id?: number
  variable: string
  direction: 'input'
  operation: 'max' | 'min' | 'weighted_average'
  point_ids: number[]
  /** Positionally parallel to point_ids (index i's weight is
   * weight_point_ids[i]). Only present/meaningful for
   * operation='weighted_average' -- absent or all-null for 'max'/'min'. */
  weight_point_ids?: (number | null)[]
  point_metadata?: Record<string, {
    point_name?: string
    device_name?: string
    device_id?: number
    object_type?: string
    object_instance?: number
    units?: string
    point_type?: string | null
  }>
}

/** Mirrors an already-resolved model INPUT value (whichever of Point,
 * Aggregate, or Constant currently sources `variable`) onto a second
 * BACnet point's Present Value each step, without recomputing it. Not a
 * "mapping" in the read/write direction sense SimulationModelMapping uses
 * -- see simulation_model_input_exposures's schema comment in the
 * backend's model_store.py for why this is its own list/table. */
export interface SimulationModelInputExposure {
  id?: number
  model_config_id?: number
  variable: string
  point_id: number
  device_id?: number
  device_name?: string
  point_name?: string
  object_type?: string
  object_instance?: number
  units?: string
  point_type?: string | null
}

export interface SimulationModelConfig {
  id: number
  name: string
  provider_type: Exclude<SimulationProviderType, 'builtin'>
  model_type: string
  enabled: boolean
  parameters: Record<string, unknown>
  created_from_device_id: number | null
  /** A row is an aggregate mapping iff it has "point_ids" (plural) instead
   * of "point_id" -- the same discriminator the backend's
   * model_runtime._is_aggregate_row uses. */
  mappings: Array<SimulationModelMapping | SimulationModelAggregateMapping>
  input_exposures?: SimulationModelInputExposure[]
  runtime_id?: string
  runtime?: unknown
}

export interface SimulationModelPayload {
  name: string
  provider_type: Exclude<SimulationProviderType, 'builtin'>
  model_type: string
  enabled: boolean
  parameters: Record<string, unknown>
  created_from_device_id?: number | null
  mappings: Array<Pick<SimulationModelMapping, 'variable' | 'direction' | 'point_id'>>
  aggregate_mappings: Array<Pick<SimulationModelAggregateMapping, 'variable' | 'direction' | 'operation' | 'point_ids' | 'weight_point_ids'>>
  input_exposures: Array<Pick<SimulationModelInputExposure, 'variable' | 'point_id'>>
}

export interface SimulationModelPointOption {
  id: number
  name: string
  device_id: number
  device_name?: string
  object_type?: string
  object_instance?: number
  units?: string
  behavior?: string
  behavior_params?: string
  manual_value?: unknown
  point_type?: string | null
  configured_value?: unknown
}

export interface MappingSuggestionAlternative {
  point_id: number
  point_name: string
  score: number
  reasons: string[]
}

/** One suggestion row (one model input/output variable) from
 * POST /simulation/models/mapping-suggestions. Cross-device suggestions
 * are expected and valid -- equipment_scope_used/related_equipment_name
 * explain *why* a point on a different device was chosen (see
 * MappingHints on the backend's VariableDefinition). */
export interface MappingSuggestionEntry {
  variable: string
  direction: 'input' | 'output'
  suggested_point_id: number | null
  suggested_point_name: string | null
  confidence: 'high' | 'medium' | 'low' | 'none'
  score: number
  reasons: string[]
  alternatives: MappingSuggestionAlternative[]
  /** 'rule' = deterministic engine (default), 'ai' = this row was
   * refreshed via an explicit "Use AI" click. */
  source: 'rule' | 'ai'
  equipment_scope_used: 'self' | 'upstream' | 'downstream' | 'any' | 'fallback'
  related_equipment_name: string | null
}

export interface MappingSuggestionsResponse {
  variables: MappingSuggestionEntry[]
}

/** One ranked candidate from POST /simulation/models/variable-candidates --
 * mirrors the backend's PointCandidate dataclass exactly (mapping_suggestions.py). */
export interface VariableCandidateEntry {
  id: number
  name: string
  description: string | null
  units: string | null
  object_type: string | null
  device_id: number
  device_name: string
  point_type: string | null
  equipment_class: string | null
  location_match: boolean
}

export interface VariableCandidatesResponse {
  variable: string
  candidates: VariableCandidateEntry[]
  equipment_scope_used: 'self' | 'upstream' | 'downstream' | 'any' | 'fallback'
  related_equipment_name: string | null
}

export const api = {
  health: () => req<Health>('/health'),
  meta:   () => req<Meta>('/meta'),
  logs:   (limit = 200) => req<LogEntry[]>(`/logs?limit=${limit}`),

  auth: {
    setupRequired: () => req<{ setup_required: boolean }>('/auth/setup-required'),
    setup: (username: string, password: string) =>
      req<AuthResponse>('/auth/setup', { method: 'POST', body: JSON.stringify({ username, password }) }),
    login: (username: string, password: string) =>
      req<AuthResponse>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
    me: () => req<User>('/auth/me'),
  },

  users: {
    list:          ()                         => req<User[]>('/users'),
    create:        (username: string, password: string) =>
      req<User>('/users', { method: 'POST', body: JSON.stringify({ username, password }) }),
    resetPassword: (id: number, password: string) =>
      req<{ ok: boolean }>(`/users/${id}/password`, { method: 'POST', body: JSON.stringify({ password }) }),
    del:           (id: number)               => req<null>(`/users/${id}`, { method: 'DELETE' }),
  },

  sim: {
    start: () => req<{ sim_state: Health['sim_state'] }>('/sim/start', { method: 'POST' }),
    pause: () => req<{ sim_state: Health['sim_state'] }>('/sim/pause', { method: 'POST' }),
    stop:  () => req<{ sim_state: Health['sim_state']; elapsed_seconds: number }>('/sim/stop', { method: 'POST' }),
  },

  simulationProviders: {
    catalog: () =>
      req<SimulationProviderCatalogEntry[]>('/simulation/providers/catalog'),

    runtime: () =>
      req<Record<string, unknown>>('/simulation/providers/runtime'),
  },

  simulationModels: {
    catalog: () => req<SimulationModelCatalogEntry[]>('/simulation/models/catalog'),
    list: (createdFromDeviceId?: number) => {
      const q = createdFromDeviceId != null
        ? `?created_from_device_id=${encodeURIComponent(createdFromDeviceId)}`
        : ''
      return req<SimulationModelConfig[]>(`/simulation/models${q}`)
    },
    get: (id: number) => req<SimulationModelConfig>(`/simulation/models/${id}`),
    create: (body: SimulationModelPayload) => req<SimulationModelConfig>('/simulation/models', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
    // apply=false (default): persist configuration only, never touches the
    // runtime engine. apply=true: also push the saved configuration to the
    // runtime. Either way the model's enabled/disabled state is whatever
    // body.enabled says -- this never flips it, see setEnabled() below.
    update: (id: number, body: SimulationModelPayload, apply = false) =>
      req<SimulationModelConfig>(`/simulation/models/${id}${apply ? '?apply=true' : ''}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      }),
    // Dedicated ON/OFF SimEngine-participation control -- never touches
    // mappings, unlike update() above (see SimulationModelEnabledPayload's
    // docstring in the backend router).
    setEnabled: (id: number, enabled: boolean) => req<SimulationModelConfig>(`/simulation/models/${id}/enabled`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
    del: (id: number) => req<{ ok: boolean; model_id: number; runtime_id: string }>(`/simulation/models/${id}`, { method: 'DELETE' }),
    pointOptions: () => req<SimulationModelPointOption[]>('/simulation/points/options'),
    mappingSuggestions: (body: { model_type: string; provider_type: string; created_from_device_id: number | null; current_model_id?: number | null }) =>
      req<MappingSuggestionsResponse>('/simulation/models/mapping-suggestions', { method: 'POST', body: JSON.stringify(body) }),
    mappingSuggestionAi: (body: { model_type: string; variable: string; created_from_device_id: number | null; current_model_id?: number | null }) =>
      req<MappingSuggestionEntry>('/simulation/models/mapping-suggestions/ai', { method: 'POST', body: JSON.stringify(body) }),
    variableCandidates: (body: { model_type: string; variable: string; created_from_device_id: number | null; current_model_id?: number | null; limit?: number }) =>
      req<VariableCandidatesResponse>('/simulation/models/variable-candidates', { method: 'POST', body: JSON.stringify(body) }),
    reconcile: () => req<Record<string, unknown>>('/simulation/models/reconcile', { method: 'POST' }),
  },

  settings: {
    get:    ()                  => req<Settings>('/settings'),
    update: (body: Settings)    => req<Settings>('/settings', { method: 'PUT', body: JSON.stringify(body) }),
  },

  devices: {
    list:   ()                              => req<Device[]>('/devices'),
    create: (b: Omit<Device, 'id'>)        => req<Device>('/devices', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Device, 'id'>) => req<Device>(`/devices/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                   => req<null>(`/devices/${id}`, { method: 'DELETE' }),
    logs:   (id: number, limit = 100)      => req<LogEntry[]>(`/devices/${id}/logs?limit=${limit}`),
    exportEde: (id: number, name: string)  => downloadFile(`/devices/${id}/export/ede`, `${name}.ede`),
    importEde: (id: number, file: File)    =>
      uploadFile<{ ok: boolean; objects_imported: number }>(`/devices/${id}/import/ede`, file, {}, true),
    exportBrick: (id: number, name: string) => downloadFile(`/devices/${id}/export/brick`, `${name}.ttl`),
    markAsController: (id: number) => req<SemanticEntity>(`/devices/${id}/controller`, { method: 'POST' }),
  },

  locations: {
    list:   ()                                    => req<Location[]>('/locations'),
    create: (b: Omit<Location, 'id'>)             => req<Location>('/locations', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Location, 'id'>) => req<Location>(`/locations/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                          => req<null>(`/locations/${id}`, { method: 'DELETE' }),
  },

  equipment: {
    list:   ()                                     => req<Equipment[]>('/equipment'),
    create: (b: Omit<Equipment, 'id'>)             => req<Equipment>('/equipment', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Equipment, 'id'>) => req<Equipment>(`/equipment/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                           => req<null>(`/equipment/${id}`, { method: 'DELETE' }),
    assignablePoints: (id: number)                 => req<AssignablePoint[]>(`/equipment/${id}/assignable-points`),
  },

  semanticEntities: {
    list: (filters?: {
      entity_kind?: string
      device_id?: number
      object_id?: number
      location_id?: number
      equipment_id?: number
      brick_class?: string
    }) => {
      const qs = filters
        ? Object.entries(filters)
            .filter(([, v]) => v !== undefined && v !== null)
            .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
            .join('&')
        : ''
      return req<SemanticEntity[]>(`/semantic-entities${qs ? `?${qs}` : ''}`)
    },
    create: (b: Omit<SemanticEntity, 'id' | 'semantic_key'>) =>
      req<SemanticEntity>('/semantic-entities', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<SemanticEntity, 'id' | 'semantic_key'>) =>
      req<SemanticEntity>(`/semantic-entities/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                                  => req<null>(`/semantic-entities/${id}`, { method: 'DELETE' }),
    points: (entityId: number, brickClass?: string) =>
      req<Record<string, unknown>[]>(`/semantic-entities/${entityId}/points${brickClass ? `?brick_class=${encodeURIComponent(brickClass)}` : ''}`),
    related: (entityId: number, predicate: string, direction: 'out' | 'in' = 'out') =>
      req<SemanticEntity[]>(`/semantic-entities/${entityId}/related?predicate=${encodeURIComponent(predicate)}&direction=${direction}`),
  },

  semanticRelationships: {
    list: (filters?: { source_entity_id?: number; target_entity_id?: number; predicate?: string }) => {
      const qs = filters
        ? Object.entries(filters)
            .filter(([, v]) => v !== undefined && v !== null)
            .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
            .join('&')
        : ''
      return req<SemanticRelationship[]>(`/semantic-relationships${qs ? `?${qs}` : ''}`)
    },
    create: (b: Omit<SemanticRelationship, 'id'>)                 =>
      req<SemanticRelationship>('/semantic-relationships', { method: 'POST', body: JSON.stringify(b) }),
    del:    (id: number)                                          => req<null>(`/semantic-relationships/${id}`, { method: 'DELETE' }),
  },

  functionalTests: {
    list:   ()                                                     => req<FunctionalTest[]>('/functional-tests'),
    get:    (id: number)                                           => req<FunctionalTest>(`/functional-tests/${id}`),
    create: (b: { name: string; description: string; equipment_type: string; definition: FunctionalTestDefinition }) =>
      req<FunctionalTest>('/functional-tests', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: { name: string; description: string; equipment_type: string; definition: FunctionalTestDefinition }) =>
      req<FunctionalTest>(`/functional-tests/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                                           => req<null>(`/functional-tests/${id}`, { method: 'DELETE' }),
    // Bodyless -- every point in the saved definition already carries its
    // own device, so there is no per-run target left to supply.
    resolve: (id: number) =>
      req<FunctionalTestResolveResponse>(`/functional-tests/${id}/resolve`, { method: 'POST' }),
    createRun: (id: number) =>
      req<FunctionalTestRun>(`/functional-tests/${id}/runs`, { method: 'POST' }),
  },

  functionalTestRuns: {
    get:    (runId: number) => req<FunctionalTestRun>(`/functional-test-runs/${runId}`),
    cancel: (runId: number) => req<FunctionalTestRun>(`/functional-test-runs/${runId}/cancel`, { method: 'POST' }),
  },

  customGraphs: {
    list:   ()                                                     => req<SavedGraph[]>('/custom-graphs'),
    get:    (id: number)                                           => req<SavedGraph>(`/custom-graphs/${id}`),
    create: (b: { name: string; definition: CustomGraphDefinition }) =>
      req<SavedGraph>('/custom-graphs', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: { name: string; definition: CustomGraphDefinition }) =>
      req<SavedGraph>(`/custom-graphs/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                                           => req<null>(`/custom-graphs/${id}`, { method: 'DELETE' }),
  },

  points: {
    list: (filters?: { search?: string; objectType?: string; deviceId?: number; pointType?: string }) => {
      const params = new URLSearchParams()
      if (filters?.search) params.set('search', filters.search)
      if (filters?.objectType) params.set('object_type', filters.objectType)
      if (filters?.deviceId != null) params.set('device_id', String(filters.deviceId))
      if (filters?.pointType) params.set('point_type', filters.pointType)
      const qs = params.toString()
      return req<PointRow[]>(`/points${qs ? `?${qs}` : ''}`)
    },
  },

  objects: {
    list:     (did: number)                        => req<SimObject[]>(`/devices/${did}/objects`),
    create:   (did: number, b: object)             => req<SimObject>(`/devices/${did}/objects`, { method: 'POST', body: JSON.stringify(b) }),
    update:   (did: number, oid: number, b: object) => req<SimObject>(`/devices/${did}/objects/${oid}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:      (did: number, oid: number)           => req<null>(`/devices/${did}/objects/${oid}`, { method: 'DELETE' }),
    setValue: (did: number, oid: number, value: unknown) =>
      req(`/devices/${did}/objects/${oid}/value`, { method: 'POST', body: JSON.stringify({ value }) }),
    history: (did: number, oid: number) =>
      req<HistoryPoint[]>(`/devices/${did}/objects/${oid}/history`),
    priorityArray: (did: number, oid: number) =>
      req<PriorityArrayInfo>(`/devices/${did}/objects/${oid}/priority-array`),
    writePriority: (did: number, oid: number, priority: number, value: unknown) =>
      req<PriorityArrayInfo>(`/devices/${did}/objects/${oid}/priority-array/${priority}`, {
        method: 'PUT', body: JSON.stringify({ value }),
      }),
  },

  // Backend routes stay under /profiles (unchanged URL space) — only the
  // frontend-facing naming here was renamed to "project" to match the UI.
  projects: {
    list:    ()                                              => req<Project[]>('/profiles'),
    save:    (name: string, description: string, connectionConfig?: BACnetConnectionConfig | null, aboveGroundLevels?: number, belowGroundLevels?: number, discoveryConnections?: BACnetDiscoveryConnection[] | null) =>
      req<Project>('/profiles', { method: 'POST', body: JSON.stringify({ name, description, connection_config: connectionConfig, discovery_connections: discoveryConnections, above_ground_levels: aboveGroundLevels ?? 0, below_ground_levels: belowGroundLevels ?? 0 }) }),
    update:  (id: number, name: string, description: string, connectionConfig?: BACnetConnectionConfig | null, discoveryConnections?: BACnetDiscoveryConnection[] | null) =>
      req<{ ok: boolean }>(`/profiles/${id}`, { method: 'PUT', body: JSON.stringify({ name, description, connection_config: connectionConfig, discovery_connections: discoveryConnections }) }),
    del:     (id: number)                                   => req<null>(`/profiles/${id}`, { method: 'DELETE' }),
    load:    (id: number)                                   => req<{ ok: boolean; connection_config: BACnetConnectionConfig | null; discovery_connections: BACnetDiscoveryConnection[] | null }>(`/profiles/${id}/load`, { method: 'POST' }),
    // Wipes live project state back to blank (devices/locations/semantic
    // data) for "New Project" — reuses the server's own load_project()
    // wipe sequence rather than looping individual per-row deletes, which
    // could leave locations permanently stuck behind a leftover semantic
    // entity (see clear_live_project's docstring).
    clear:   ()                                              => req<{ ok: boolean }>('/profiles/clear', { method: 'POST' }),
    import_: (name: string, description: string, data: object) =>
      req<Project>('/profiles/import', { method: 'POST', body: JSON.stringify({ name, description, data }) }),
    exportEde: (id: number, name: string) => downloadFile(`/profiles/${id}/export/ede`, `${name}.ede`),
    importEde: (name: string, description: string, deviceName: string, file: File) =>
      uploadFile<Project>('/profiles/import/ede', file, { name, description, device_name: deviceName }),
    exportBrick: (id: number, name: string) => downloadFile(`/profiles/${id}/export/brick`, `${name}.ttl`),
  },

  discovery: {
    // Pure ephemeral dry-run -- does not touch the devices table. Kept for
    // reference/diagnostic use; the admin UI's "Discover Devices"/
    // "Rediscover" action uses sync() below instead.
    run: (opts: BACnetConnectionConfig) =>
      req<{ devices: DiscoveredDevice[] }>('/bacnet-discovery/run', {
        method: 'POST',
        body: JSON.stringify({
          discovery_target: opts.discovery_target,
          device_instance_low: opts.device_instance_low,
          device_instance_high: opts.device_instance_high,
          timeout_ms: opts.timeout_ms,
        }),
      }),
    // Runs discovery and persists results as external-BACnet project
    // devices (upsert by device_instance, never deletes).
    sync: (opts: BACnetConnectionConfig) =>
      req<{ devices: Device[] }>('/bacnet-discovery/sync', {
        method: 'POST',
        body: JSON.stringify({
          discovery_target: opts.discovery_target,
          device_instance_low: opts.device_instance_low,
          device_instance_high: opts.device_instance_high,
          timeout_ms: opts.timeout_ms,
        }),
      }),
    connections: {
      list: (projectId: number) =>
        req<{ connections: BACnetDiscoveryConnection[] }>(`/bacnet-discovery/connections/${projectId}`),
      create: (projectId: number, connection: Omit<BACnetDiscoveryConnection, 'id'>) =>
        req<BACnetDiscoveryConnection>(`/bacnet-discovery/connections/${projectId}`, {
          method: 'POST',
          body: JSON.stringify(connection),
        }),
      update: (projectId: number, connectionId: number, connection: Omit<BACnetDiscoveryConnection, 'id'>) =>
        req<BACnetDiscoveryConnection>(`/bacnet-discovery/connections/${projectId}/${connectionId}`, {
          method: 'PUT',
          body: JSON.stringify(connection),
        }),
      delete: (projectId: number, connectionId: number) =>
        req<null>(`/bacnet-discovery/connections/${projectId}/${connectionId}`, { method: 'DELETE' }),
      sync: (projectId: number, connectionId: number) =>
        req<{ devices: Device[] }>(`/bacnet-discovery/connections/${projectId}/${connectionId}/sync`, { method: 'POST' }),
      syncAll: (projectId: number) =>
        req<{ devices: Device[]; connections: number }>(`/bacnet-discovery/connections/${projectId}/sync-enabled`, { method: 'POST' }),
    },
  },

  externalObjects: {
    discover: (deviceId: number) =>
      req<{ objects: ExternalObjectRow[] }>(`/devices/${deviceId}/external-objects/discover`, { method: 'POST' }),
    refresh: (deviceId: number) =>
      req<{ objects: ExternalObjectRow[] }>(`/devices/${deviceId}/external-objects/refresh`, { method: 'POST' }),
  },

  semanticSuggestions: {
    forDevice: (deviceId: number) =>
      req<SemanticSuggestionsResponse>(`/devices/${deviceId}/semantic-suggestions`, { method: 'POST' }),
    aiForPoint: (deviceId: number, objectId: number) =>
      req<SemanticSuggestionEntry>(`/devices/${deviceId}/semantic-suggestions/points/${objectId}/ai`, { method: 'POST' }),
  },

  // Whole-database snapshot/restore — distinct from `projects` above (which
  // save/restore just the device/object topology, not the full SQLite file).
  backups: {
    list:    ()                       => req<BackupEntry[]>('/backups'),
    create:  ()                       => req<BackupEntry>('/backups', { method: 'POST' }),
    restore: (fileName: string)       => req<{ ok: boolean; pre_restore_backup: string }>(`/backups/${encodeURIComponent(fileName)}/restore`, { method: 'POST' }),
    del:     (fileName: string)       => req<null>(`/backups/${encodeURIComponent(fileName)}`, { method: 'DELETE' }),
    download: (fileName: string)      => downloadFile(`/backups/${encodeURIComponent(fileName)}/download`, fileName),
    upload:  (file: File)             => uploadFile<BackupEntry>('/backups/upload', file),
  },

  energyModels: {
    list:   (deviceId: number)                           => req<EnergyModelConfig[]>(`/devices/${deviceId}/energy-models`),
    create: (deviceId: number, b: Omit<EnergyModelConfig, 'id' | 'device_id'>) =>
      req<EnergyModelConfig>(`/devices/${deviceId}/energy-models`, { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<EnergyModelConfig, 'id' | 'device_id'>) =>
      req<EnergyModelConfig>(`/energy/models/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                                 => req<null>(`/energy/models/${id}`, { method: 'DELETE' }),
  },

  notificationClasses: {
    list:   (deviceId: number)                             => req<NotificationClass[]>(`/devices/${deviceId}/notification-classes`),
    create: (deviceId: number, b: Omit<NotificationClass, 'id' | 'device_id'>) =>
      req<NotificationClass>(`/devices/${deviceId}/notification-classes`, { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<NotificationClass, 'id' | 'device_id'>) =>
      req<NotificationClass>(`/notification-classes/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                                   => req<null>(`/notification-classes/${id}`, { method: 'DELETE' }),
  },

  alarmConfig: {
    get: (deviceId: number, objId: number)             => req<AlarmConfig | null>(`/devices/${deviceId}/objects/${objId}/alarm-config`),
    set: (deviceId: number, objId: number, b: object)  =>
      req<AlarmConfig>(`/devices/${deviceId}/objects/${objId}/alarm-config`, { method: 'PUT', body: JSON.stringify(b) }),
    del: (deviceId: number, objId: number)             => req<null>(`/devices/${deviceId}/objects/${objId}/alarm-config`, { method: 'DELETE' }),
  },

  alarms: {
    list: (limit = 200, unackedOnly = false) =>
      req<AlarmLogEntry[]>(`/alarms?limit=${limit}&unacked_only=${unackedOnly}`),
    ack:  (id: number, ackBy?: string) =>
      req<AlarmLogEntry>(`/alarms/${id}/ack`, { method: 'POST', body: JSON.stringify({ ack_by: ackBy }) }),
  },

  eventEnrollments: {
    list:   (deviceId: number)                          => req<EventEnrollment[]>(`/devices/${deviceId}/event-enrollments`),
    create: (deviceId: number, b: Omit<EventEnrollment, 'id' | 'device_id'>) =>
      req<EventEnrollment>(`/devices/${deviceId}/event-enrollments`, { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<EventEnrollment, 'id' | 'device_id'>) =>
      req<EventEnrollment>(`/event-enrollments/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                                => req<null>(`/event-enrollments/${id}`, { method: 'DELETE' }),
  },

  trendLogs: {
    list:   (deviceId: number)                     => req<TrendLog[]>(`/devices/${deviceId}/trend-logs`),
    create: (deviceId: number, b: Omit<TrendLog, 'id' | 'device_id' | 'record_count' | 'total_record_count' | 'last_sampled_at'>) =>
      req<TrendLog>(`/devices/${deviceId}/trend-logs`, { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<TrendLog, 'id' | 'device_id' | 'record_count' | 'total_record_count' | 'last_sampled_at'>) =>
      req<TrendLog>(`/trend-logs/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:     (id: number) => req<null>(`/trend-logs/${id}`, { method: 'DELETE' }),
    records: (id: number, opts: { from?: string; to?: string; startSequence?: number; limit?: number; order?: 'asc' | 'desc' } = {}) => {
      const params = new URLSearchParams()
      if (opts.from) params.set('from', opts.from)
      if (opts.to) params.set('to', opts.to)
      if (opts.startSequence !== undefined) params.set('start_sequence', String(opts.startSequence))
      if (opts.limit !== undefined) params.set('limit', String(opts.limit))
      if (opts.order) params.set('order', opts.order)
      const qs = params.toString()
      return req<TrendLogRecord[]>(`/trend-logs/${id}/records${qs ? `?${qs}` : ''}`)
    },
    trigger: (id: number) => req<{ ok: boolean; sequence_number: number; value: unknown }>(`/trend-logs/${id}/trigger`, { method: 'POST' }),
    clear:   (id: number) => req<{ ok: boolean }>(`/trend-logs/${id}/clear`, { method: 'POST' }),
  },

  schedules: {
    list:   (deviceId: number)                          => req<Schedule[]>(`/devices/${deviceId}/schedules`),
    create: (deviceId: number, b: Omit<Schedule, 'id' | 'device_id' | 'enabled'> & { enabled: number }) =>
      req<Schedule>(`/devices/${deviceId}/schedules`, { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Schedule, 'id' | 'device_id' | 'enabled'> & { enabled: number }) =>
      req<Schedule>(`/schedules/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:      (id: number) => req<null>(`/schedules/${id}`, { method: 'DELETE' }),
    enable:   (id: number) => req<{ ok: boolean }>(`/schedules/${id}/enable`, { method: 'POST' }),
    disable:  (id: number) => req<{ ok: boolean }>(`/schedules/${id}/disable`, { method: 'POST' }),
    evaluate: (id: number) => req<ScheduleEvaluation>(`/schedules/${id}/evaluate`, { method: 'POST' }),
  },

  calendars: {
    list:   (deviceId: number)                          => req<Calendar[]>(`/devices/${deviceId}/calendars`),
    create: (deviceId: number, b: Omit<Calendar, 'id' | 'device_id' | 'enabled'> & { enabled: number }) =>
      req<Calendar>(`/devices/${deviceId}/calendars`, { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Calendar, 'id' | 'device_id' | 'enabled'> & { enabled: number }) =>
      req<Calendar>(`/calendars/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del: (id: number) => req<null>(`/calendars/${id}`, { method: 'DELETE' }),
  },

  packetCapture: {
  status: () =>
    req<PacketCaptureStatus>('/packet-capture/status'),

  start: () =>
    req<PacketCaptureStatus>('/packet-capture/start', {
      method: 'POST',
    }),

  stop: () =>
    req<PacketCaptureStatus>('/packet-capture/stop', {
      method: 'POST',
    }),

  clear: () =>
    req<PacketCaptureStatus>('/packet-capture/clear', {
      method: 'POST',
    }),

  list: (filters: PacketCaptureFilters = {}) => {
    const params = new URLSearchParams()

    if (filters.direction) {
      params.set('direction', filters.direction)
    }

    if (filters.sourceIp) {
      params.set('source_ip', filters.sourceIp)
    }

    if (filters.destinationIp) {
      params.set('destination_ip', filters.destinationIp)
    }

    if (filters.service) {
      params.set('service', filters.service)
    }

    if (filters.deviceId !== undefined) {
      params.set('device_id', String(filters.deviceId))
    }

    if (filters.unassociated) {
      params.set('unassociated', 'true')
    }

    if (filters.offset !== undefined) {
      params.set('offset', String(filters.offset))
    }

    if (filters.limit !== undefined) {
      params.set('limit', String(filters.limit))
    }

    const query = params.toString()

    return req<CapturedPacketPage>(
      `/packet-capture/packets${query ? `?${query}` : ''}`,
    )
  },

  get: (packetId: string) =>
    req<CapturedPacket>(
      `/packet-capture/packets/${encodeURIComponent(packetId)}`,
    ),

  exportPcap: () =>
    downloadFile(
      '/packet-capture/export',
      `bacnet-capture-${new Date()
        .toISOString()
        .replace(/:/g, '-')
        .replace(/\.\d{3}Z$/, 'Z')}.pcap`,
    ),
},

  analytics: {
    snapshot: () => req<AnalyticsSnapshot>('/analytics/snapshot'),
    // File download, not JSON — needs the Bearer auth header, so a plain
    // <a href> link (no custom headers on navigation) won't work here.
    export: async (format: 'csv' | 'json') => {
      const headers: Record<string, string> = {}
      if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
      const res = await fetch(`/analytics/export?format=${format}`, { headers })
      if (!res.ok) throw new Error(res.statusText)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `analytics_${Date.now()}.${format}`
      a.click()
      URL.revokeObjectURL(url)
    },
  },
}
