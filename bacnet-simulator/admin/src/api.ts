import type { Device, SimObject, Meta, Health, Profile, LogEntry, HistoryPoint, User, AuthResponse } from './types'
import { authToken, logout } from './auth'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
  const res = await fetch(path, { headers, ...init })
  if (!res.ok) {
    if (res.status === 401) logout()
    const e = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error((e as { detail?: string }).detail || res.statusText)
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
    start: () => req<{ sim_running: boolean }>('/sim/start', { method: 'POST' }),
    pause: () => req<{ sim_running: boolean }>('/sim/pause', { method: 'POST' }),
    stop:  () => req<{ sim_running: boolean; elapsed_seconds: number }>('/sim/stop', { method: 'POST' }),
  },

  devices: {
    list:   ()                              => req<Device[]>('/devices'),
    create: (b: Omit<Device, 'id'>)        => req<Device>('/devices', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Device, 'id'>) => req<Device>(`/devices/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                   => req<null>(`/devices/${id}`, { method: 'DELETE' }),
    logs:   (id: number, limit = 100)      => req<LogEntry[]>(`/devices/${id}/logs?limit=${limit}`),
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
}
