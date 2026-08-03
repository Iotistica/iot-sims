import type { Device, SimObject, Meta, Health, Settings, Project, BackupEntry, LogEntry, HistoryPoint, User, AuthResponse, AnalyticsSnapshot, NotificationClass, AlarmConfig, AlarmLogEntry, EventEnrollment, TrendLog, TrendLogRecord, Schedule, ScheduleEvaluation, PriorityArrayInfo, Calendar, Location } from './types'
import { authToken, logout } from './auth'

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
  return headers
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

async function uploadFile<T>(path: string, file: File, fields: Record<string, string> = {}): Promise<T> {
  const body = new FormData()
  body.append('file', file)
  for (const [k, v] of Object.entries(fields)) body.append(k, v)
  const res = await fetch(path, { method: 'POST', headers: authHeaders(), body })
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = (e as { detail?: unknown }).detail
    throw new Error(typeof detail === 'string' ? detail : res.statusText)
  }
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
    let message: string
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      // FastAPI/Pydantic validation errors: detail is a list of
      // { loc, msg, type } objects, not a string.
      message = detail.map((d) => (d as { msg?: string })?.msg).filter(Boolean).join('; ')
    } else {
      message = ''
    }
    throw new Error(message || res.statusText)
  }
  if (res.status === 204) return null as T
  return res.json() as Promise<T>
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
      uploadFile<{ ok: boolean; objects_imported: number }>(`/devices/${id}/import/ede`, file),
  },

  locations: {
    list:   ()                                    => req<Location[]>('/locations'),
    create: (b: Omit<Location, 'id'>)             => req<Location>('/locations', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Location, 'id'>) => req<Location>(`/locations/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                          => req<null>(`/locations/${id}`, { method: 'DELETE' }),
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
    save:    (name: string, description: string)            => req<Project>('/profiles', { method: 'POST', body: JSON.stringify({ name, description }) }),
    update:  (id: number, name: string, description: string) => req<{ ok: boolean }>(`/profiles/${id}`, { method: 'PUT', body: JSON.stringify({ name, description }) }),
    del:     (id: number)                                   => req<null>(`/profiles/${id}`, { method: 'DELETE' }),
    load:    (id: number)                                   => req<{ ok: boolean }>(`/profiles/${id}/load`, { method: 'POST' }),
    import_: (name: string, description: string, data: object) =>
      req<Project>('/profiles/import', { method: 'POST', body: JSON.stringify({ name, description, data }) }),
    exportEde: (id: number, name: string) => downloadFile(`/profiles/${id}/export/ede`, `${name}.ede`),
    importEde: (name: string, description: string, deviceName: string, file: File) =>
      uploadFile<Project>('/profiles/import/ede', file, { name, description, device_name: deviceName }),
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
