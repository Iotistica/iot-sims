# Alarms & Conditions

Fault-behavior transitions (`FaultBehavior`, `lib/behaviors.py`) drive two
independent feeds:

1. **Dashboard feed** — `lib/analytics.py`'s `record_fault_transition()`/
   `acknowledge_alarm()`, backing the admin UI's "Alarm & Event Analytics"
   panel. Dashboard-only, not visible to any OPC UA client.
2. **Real OPC UA events** — `lib/alarms.py`'s `fire_alarm_condition()`,
   firing a real `AlarmConditionType` event any OPC UA client can subscribe
   to. This is what the rest of this document covers.

Both are driven from the same open/clear edges in `opcua_simulator.py`
(`_create_live_tag` and the tick loop) as independent call sites — a
failure in one can't affect the other.

## What's implemented

Each fault open/clear fires an `AlarmConditionType` event sourced from the
**device's** node (not the tag's — see below), with:

- `Message`, `Severity` (mapped from the same critical/warning scheme the
  dashboard uses, onto OPC UA's 1-1000 UInt16 range)
- `Retain` / `ActiveState` — true while the fault is active, false on clear
- `ConditionName` — the tag's name
- `InputNode` — the tag's own Variable node (the value the alarm is about)
- `NodeId` — a stable per-tag identifier, required for asyncua's built-in
  `ConditionRefresh` support to track/clear the right entry (see below)

## Known limitations (real, not yet built)

- **No Acknowledge/Confirm.** Every condition always reports
  `AckedState=Unacknowledged`/`ConfirmedState=Unconfirmed`. A client calling
  the standard Acknowledge/Confirm methods has nowhere to land — that would
  require `Server.link_method()`-based method handlers, not built here.
- **Event source is the device, not the tag.** `EventNotifier` (and
  therefore event sourcing) is only a valid attribute on Object/View nodes
  per spec — Variables don't have it. The tag is still referenced via the
  condition's `InputNode` property.
- **Subscribing to the Server node will NOT receive these events.**
  asyncua's event delivery (`monitored_item_service.py`'s `trigger_event`)
  matches only the *exact* node a client subscribed to — there's no
  hierarchical propagation via `HasNotifier` references, even though such a
  reference is browsable in the address space. A client must subscribe
  directly to the specific device's node to get live push notifications.
  This is a library limitation, not something fixable from this codebase.
- **`ConditionRefresh` *is* available** (asyncua implements it natively —
  `server/subscription_service.py`'s `condition_refresh()`, wired to the
  standard `ConditionType_ConditionRefresh` method node) and — unlike live
  delivery — is **not** subject to the exact-node-match limitation above: a
  client can call it from a monitored item on any node and receive every
  currently `Retain=true` condition server-wide. This is the practical way
  for a client to learn "what's active right now" regardless of which
  device node(s) it's subscribed to.

## Not touched by this feature

`iot-agent`'s OPC-UA client (`src/plugins/opcua/client.ts`) is
DataChange-subscription-only today — no Event-subscription code path
exists there. Making the agent actually consume these events (deciding how
a received Condition event becomes an agent-side alert, alongside the
existing anomaly-detection alert pipeline) is a separate, unimplemented
follow-up.
