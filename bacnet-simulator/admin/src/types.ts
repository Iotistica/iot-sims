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
}
