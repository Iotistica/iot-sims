import type {
  Device, Tag, Meta, Health, Profile, LogEntry, HistoryPoint, User, AuthResponse,
  NodeSetPreviewResponse, NodeSetImportResponse, NodeSetImportRecord,
} from './types'
import { authToken, logout } from './auth'

async function handleResponse<T>(res: Response): Promise<T> {
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
    } else if (detail && typeof detail === 'object') {
      // Some endpoints (e.g. /nodesets/import conflicts) return a structured
      // { errors, warnings } object instead of a plain string.
      const errors = (detail as { errors?: unknown }).errors
      message = Array.isArray(errors) ? errors.join('; ') : JSON.stringify(detail)
    } else {
      message = ''
    }
    throw new Error(message || res.statusText)
  }
  if (res.status === 204) return null as T
  return res.json() as Promise<T>
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
  const res = await fetch(path, { headers, ...init })
  return handleResponse<T>(res)
}

async function reqForm<T>(path: string, form: FormData): Promise<T> {
  // No Content-Type here on purpose — the browser sets multipart/form-data
  // with the correct boundary itself; setting it manually breaks the upload.
  const headers: Record<string, string> = {}
  if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
  const res = await fetch(path, { method: 'POST', headers, body: form })
  return handleResponse<T>(res)
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

  devices: {
    list:   ()                              => req<Device[]>('/devices'),
    create: (b: Omit<Device, 'id' | 'key'>)        => req<Device>('/devices', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Device, 'id' | 'key'>) => req<Device>(`/devices/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                   => req<null>(`/devices/${id}`, { method: 'DELETE' }),
    logs:   (id: number, limit = 100)      => req<LogEntry[]>(`/devices/${id}/logs?limit=${limit}`),
  },

  tags: {
    list:     (did: number)                        => req<Tag[]>(`/devices/${did}/tags`),
    create:   (did: number, b: object)             => req<Tag>(`/devices/${did}/tags`, { method: 'POST', body: JSON.stringify(b) }),
    update:   (did: number, tid: number, b: object) => req<Tag>(`/devices/${did}/tags/${tid}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:      (did: number, tid: number)           => req<null>(`/devices/${did}/tags/${tid}`, { method: 'DELETE' }),
    setValue: (did: number, tid: number, value: unknown) =>
      req(`/devices/${did}/tags/${tid}/value`, { method: 'POST', body: JSON.stringify({ value }) }),
    history: (did: number, tid: number) =>
      req<HistoryPoint[]>(`/devices/${did}/tags/${tid}/history`),
  },

  profiles: {
    list:    ()                                              => req<Profile[]>('/profiles'),
    save:    (name: string, description: string)            => req<Profile>('/profiles', { method: 'POST', body: JSON.stringify({ name, description }) }),
    update:  (id: number, name: string, description: string) => req<{ ok: boolean }>(`/profiles/${id}`, { method: 'PUT', body: JSON.stringify({ name, description }) }),
    del:     (id: number)                                   => req<null>(`/profiles/${id}`, { method: 'DELETE' }),
    load:    (id: number)                                   => req<{ ok: boolean }>(`/profiles/${id}/load`, { method: 'POST' }),
    import_: (name: string, description: string, data: object) =>
      req<Profile>('/profiles/import', { method: 'POST', body: JSON.stringify({ name, description, data }) }),
  },

  nodesets: {
    preview: (file: File) => {
      const form = new FormData()
      form.append('file', file)
      return reqForm<NodeSetPreviewResponse>('/nodesets/preview', form)
    },
    import_: (file: File, name: string, conflictStrategy: string) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', name)
      form.append('conflict_strategy', conflictStrategy)
      return reqForm<NodeSetImportResponse>('/nodesets/import', form)
    },
    imports: () => req<NodeSetImportRecord[]>('/nodesets/imports'),
    deleteImport: (id: number) =>
      req<{ deleted_device_ids: number[]; already_removed: number[] }>(`/nodesets/imports/${id}`, { method: 'DELETE' }),
  },
}
