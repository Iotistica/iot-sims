/** V1 ships this as a one-click "Load Example" action rather than a DB
 * seed row (keeps seed_default()'s device/object seeding unrelated to
 * commissioning tests). Uses only canonical classes already present in
 * POINT_TYPES -- there is no literal Boiler_Status/Heating_Water_Supply_
 * Temperature_Sensor key, so this uses the closest real entries
 * (Run_Status, Leaving_Hot_Water_Temperature_Sensor) instead of inventing
 * new vocabulary. */
import type { FunctionalTestDefinition } from './types'

export const boilerHeatingResponseExample: FunctionalTestDefinition = {
  version: 1,
  nodes: [
    { id: 'start', type: 'start', params: {} },
    {
      id: 'wait-run',
      type: 'wait_until',
      params: { point_type: 'Run_Status', operator: 'eq', value: true, timeout_seconds: 600 },
    },
    {
      id: 'capture-lwt',
      type: 'capture',
      params: { point_type: 'Leaving_Hot_Water_Temperature_Sensor', variable: 'lwt' },
    },
    { id: 'wait-5min', type: 'wait', params: { seconds: 300 } },
    {
      id: 'verify-lwt-rise',
      type: 'verify',
      params: {
        left: { kind: 'point', point_type: 'Leaving_Hot_Water_Temperature_Sensor' },
        operator: 'gt',
        right: { kind: 'variable', name: 'lwt', offset: 2 },
      },
    },
    { id: 'end-pass', type: 'end', params: { result: 'pass' } },
    { id: 'end-fail', type: 'end', params: { result: 'fail', message: 'Leaving hot water temperature did not rise' } },
  ],
  edges: [
    { source: 'start', target: 'wait-run', source_handle: null },
    { source: 'wait-run', target: 'capture-lwt', source_handle: null },
    { source: 'capture-lwt', target: 'wait-5min', source_handle: null },
    { source: 'wait-5min', target: 'verify-lwt-rise', source_handle: null },
    { source: 'verify-lwt-rise', target: 'end-pass', source_handle: 'pass' },
    { source: 'verify-lwt-rise', target: 'end-fail', source_handle: 'fail' },
  ],
  layout: {
    start: { x: 260, y: 40 },
    'wait-run': { x: 220, y: 160 },
    'capture-lwt': { x: 220, y: 300 },
    'wait-5min': { x: 240, y: 440 },
    'verify-lwt-rise': { x: 210, y: 580 },
    'end-pass': { x: 120, y: 740 },
    'end-fail': { x: 340, y: 740 },
  },
}
