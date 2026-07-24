export interface Device {
  id: number
  key: string
  name: string
  description: string
  manufacturer: string
  model: string
  enabled: number
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
