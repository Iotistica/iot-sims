import type { Device, SimObject, Meta, Health } from './types'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error((e as { detail?: string }).detail || res.statusText)
  }
  if (res.status === 204) return null as T
  return res.json() as Promise<T>
}

export const api = {
  health: () => req<Health>('/health'),
  meta:   () => req<Meta>('/meta'),

  devices: {
    list:   ()                              => req<Device[]>('/devices'),
    create: (b: Omit<Device, 'id'>)        => req<Device>('/devices', { method: 'POST', body: JSON.stringify(b) }),
    update: (id: number, b: Omit<Device, 'id'>) => req<Device>(`/devices/${id}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:    (id: number)                   => req<null>(`/devices/${id}`, { method: 'DELETE' }),
  },

  objects: {
    list:     (did: number)                        => req<SimObject[]>(`/devices/${did}/objects`),
    create:   (did: number, b: object)             => req<SimObject>(`/devices/${did}/objects`, { method: 'POST', body: JSON.stringify(b) }),
    update:   (did: number, oid: number, b: object) => req<SimObject>(`/devices/${did}/objects/${oid}`, { method: 'PUT', body: JSON.stringify(b) }),
    del:      (did: number, oid: number)           => req<null>(`/devices/${did}/objects/${oid}`, { method: 'DELETE' }),
    setValue: (did: number, oid: number, value: unknown) =>
      req(`/devices/${did}/objects/${oid}/value`, { method: 'POST', body: JSON.stringify({ value }) }),
  },
}
