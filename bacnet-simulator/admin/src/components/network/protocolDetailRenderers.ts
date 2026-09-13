import type { Component } from 'vue'
import BacnetTrafficDetails from './BacnetTrafficDetails.vue'

export const protocolDetailRenderers: Record<string, Component> = { bacnet: BacnetTrafficDetails }
