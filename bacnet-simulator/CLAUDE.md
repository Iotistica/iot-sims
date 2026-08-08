# BMS Simulator — Claude Project Memory

## Project identity

This project started as a BACnet simulator but is evolving into a broader **Building Management System (BMS) Simulator**.

BACnet should be treated as a protocol/interface layer, not as the entire architecture.

The simulator is intended to support:

- BACnet protocol/device/object simulation
- HVAC and building-equipment simulation
- energy and utility modeling
- Brick semantic modeling
- fault injection and diagnostics
- commissioning / functional performance testing
- control-sequence experimentation
- education, training, and application testing

The long-term architecture should allow additional building protocols or integrations (for example Modbus, DALI, MQTT, ONVIF-style events) without making the core simulator protocol-specific.

## Architectural direction

```text
Building / Project Model
        |
        +-- Locations
        +-- Equipment
        +-- Semantic Model (Brick)
        |
Simulation Engine
        |
        +-- Equipment behavior
        +-- Schedules
        +-- Faults
        +-- Control sequences (future)
        |
Protocol Layer
        |
        +-- BACnet
        +-- other protocols later
        |
Applications
        |
        +-- Energy Engine
        +-- Utilities
        +-- Analytics
        +-- Fault Detection
        +-- Commissioning
        +-- Project / Scenario Comparison
```

Keep these layers separate where practical.

## Refactoring rule

The repository has been moving toward a new refactored folder/router/service architecture.

**Do not add new feature logic to `legacy.py` unless unavoidable for backward compatibility.**

Before adding a new subsystem:

1. inspect the existing scaffold,
2. use the current router/service/module organization,
3. do not create a second parallel architecture,
4. keep migrations additive and backward-compatible where practical.

# Current major subsystems

## BACnet simulator

BACnet currently provides the live building point/object simulation.

Supported concepts include:

- devices
- BACnet objects
- behaviors
- schedules
- trend logs
- alarms/events
- commandable objects / priority arrays
- reliability / faults
- packet / traffic analytics
- discovery
- locations
- project import/export

BACnet remains important, but new higher-level features should not unnecessarily depend on BACnet-specific object naming.

## Projects

Projects are effectively full simulator configurations and are very close to the concept of **energy/control scenarios**.

Do not build a separate scenario persistence architecture unless needed.

Preferred future workflow:

```text
Baseline Project
    |
Duplicate
    |
Optimized Project
    |
Run both
    |
Compare energy, demand, comfort, alarms, runtime
```

Potential future metadata:

- `project_role`: baseline / optimized / faulty / custom
- `parent_project_id`
- `comparison_group`

Projects can later support savings comparisons between legacy controls, optimized controls, Guideline 36, faulty operation, occupancy-based operation, and custom sequences.

# Semantic model / Brick

## Current state

Brick Core is implemented: `semantic_entities` + `semantic_relationships` tables (SQLite, additive, no graph DB), a `SemanticResolver` (`src/semantics/resolver.py`), canonical-only backfill, project round-trip, and Brick (TTL) export with relationships.

**Brick is the semantic source of truth.** The older lightweight tagging fields —

- `devices.equipment_type`
- `objects.point_type`
- `locations.kind`

— still exist and are still written, but only as **compatibility mirrors**, kept in lockstep automatically server-side (`src/semantics/mirror.py`) so a user assigns a classification exactly once, through whichever UI surface is convenient, and never has to maintain both systems by hand:

- **Device/Object/Location drawers** (ordinary classification): saving the drawer's Brick Class field writes the flat field *and* creates/updates/deletes the matching direct semantic entity, in the same request. This is the primary, everyday path.
- **Semantic Model panel** (sub-equipment, virtual entities, relationships): creating/updating/deleting a **point** or **location** entity there also mirrors into `objects.point_type`/`locations.kind` (unambiguous — at most one such entity can ever reference a given object/location, DB-enforced). **Equipment is deliberately one-directional** (drawer → entity only) — a `device_id` alone can't distinguish a device's own top-level entity from sub-equipment (a `Supply_Fan` also has `device_id` set) without already knowing its `isPartOf` relationships, which may not exist yet at entity-creation time. Creating a `Supply_Fan`/`Return_Fan`/`Lighting_Zone` via the Semantic panel never touches `devices.equipment_type`.
- **Startup backfill** (`backfill_semantic_entities()`) is a **one-time migration mechanism for pre-existing data**, not the steady-state workflow — it only fires when a row has a flat tag but *no* Brick entity yet, and never overwrites an existing entity with a stale flat tag.

`/meta` exposes the shared vocabulary (same dicts back both the Brick `brick_class` values and the flat-field values — there's only one vocabulary, not two). Examples already used include `Chiller`, `Boiler`, `Air_Handling_Unit`, `Lighting_Equipment`, `Variable_Air_Volume_Box`, `Supply_Fan`, `Return_Fan`, `Fan`, and (a Brick *location* class, not equipment) `Lighting_Zone`.

The configured Brick version is **1.4.4**, verified against the real pinned source (not guessed by naming convention).

**Why the flat fields aren't removed yet:** Energy, Fault Detection, Commissioning, and `SimEngine.get_device_point_values()` still read them directly and have not been migrated to `SemanticResolver` — only AHU supply/return fans and DALI lighting zones currently resolve Brick-first-with-fallback. New consumers should use `SemanticResolver`, not read the flat fields directly. The flat fields can be dropped once every consumer is migrated.

## The collapsing-dict problem this solved

`SimEngine.get_device_point_values()`/`FaultContext.point()`/`.value()` collapse duplicate semantic classes on one device via `dict[point_type, value]` — e.g. `Lighting Zone A -> Power_Sensor` and `Lighting Zone B -> Power_Sensor` on the same DALI gateway, keyed only by class, one silently overwrites the other. Both are deliberately **left unchanged** (guaranteed-backward-compatible fallback for anything not yet Brick-aware) — `SemanticResolver.resolve_device_points()`/`FaultContext.values()` are the additive, non-collapsing replacements, and `points_by_type`-style list access is available wherever the old dict access still is.

## Brick Core structure

- Canonical Brick classes only — `EQUIPMENT_TYPES`/`POINT_TYPES`/`LOCATION_KINDS` in `src/core/config.py`, each verified against the real pinned v1.4.4 source, never invented by naming convention.
- `semantic_entities` (`entity_kind`: equipment/point/location) + `semantic_relationships` (`isPointOf`, `isPartOf`, `feeds`, `hasLocation`, each with a real verified Brick inverse).
- Multiple points of the same class, and equipment subcomponents (`Supply_Fan`/`Return_Fan` `isPartOf` an `Air_Handling_Unit`), and virtual/device-hosted locations (`Lighting_Zone`, no physical `locations` row) — all supported without inventing new vocabulary.
- `semantic_key` is a *derived*, never-client-settable value embedding live surrogate ids — recomputed on every write (including project reload, which reassigns ids); `local_slug` is the portable, ID-independent disambiguator that survives reload.
- SQLite remains the persistence layer — Brick does not require a graph database.

# Energy Engine

The energy engine is intentionally separate from the BACnet simulation engine.

BACnet/simulation supplies live point values.

The Energy Engine:

1. resolves relevant equipment inputs,
2. builds an equipment snapshot,
3. evaluates an engineering model,
4. accumulates energy,
5. exposes current results,
6. writes periodic history,
7. emits activity-log messages.

## Model identity

Energy models now support multiple instances per device.

Model key:

```python
(device_id, model_type, instance_key)
```

Examples:

```text
(306, "chiller", "default")
(307, "boiler", "default")
(308, "ahu", "default")
(318, "lighting", "zone-a")
(318, "lighting", "zone-b")
```

The database table `energy_model_configs` therefore uses `device_id`, `model_type`, `instance_key`, `enabled`, and `parameters`, with uniqueness:

```text
UNIQUE(device_id, model_type, instance_key)
```

Existing single-instance equipment uses:

```text
instance_key = "default"
```

## Current energy models

### Chiller

Current high-confidence path:

```text
Chilled water flow
+
Entering CHW temperature
+
Leaving CHW temperature
    |
Cooling thermal load
    |
COP
    |
Electrical kW
```

Typical result fields include `power_kw`, `cooling_load_kw`, `load_fraction`, `effective_cop`, `interval_energy_kwh`, `total_energy_kwh`, source, confidence, and method.

### AHU

Current AHU electrical model uses fan speeds and rated fan data.

Typical result fields include `power_kw`, `supply_fan_power_kw`, `return_fan_power_kw`, `auxiliary_power_kw`, `cooling_load_kw`, source, confidence, and method.

Supply and return fan distinction currently contains some compatibility tagging that should eventually be replaced by Brick relationships.

### Lighting

The lighting model represents **one logical lighting zone per energy-model instance**.

Example:

```text
device 318 / lighting / zone-a
device 318 / lighting / zone-b
```

Do not rewrite the lighting calculation into one giant multi-zone model.

Current priority:

```text
Measured electrical power
    |
Dimming estimate
    |
On/off estimate
    |
Occupancy estimate
```

Current DALI power points are simulated independently, so measured power can remain high even if a light command is turned off. A future linked/expression simulation behavior can make power dependent on On/Off + dim level.

Temporary lighting context may resolve points by BACnet object name because duplicate semantic tags exist. Brick Core should replace this with relationship traversal.

### Boiler

The boiler model tracks **fuel and electrical auxiliary energy separately**.

Important result fields include `fuel_input_kw`, `thermal_output_kw`, `effective_efficiency`, `interval_fuel_energy_kwh`, `total_fuel_energy_kwh`, `auxiliary_electric_power_kw`, electric energy totals, source, confidence, and method.

Current strong path:

```text
Natural gas flow
    |
Fuel input kW
    |
Configured boiler efficiency
    |
Heating output kW
```

The model also supports a water-side calculation when hot-water flow exists:

```text
Water flow
x
(Supply temperature - Return temperature)
x
water specific heat
    |
Thermal output
```

Current boiler example uses:

```text
Gas-Flow -> Natural_Gas_Flow_Sensor
```

with units such as cubic feet per minute.

**Do not add boiler fuel input to the electrical building-power KPI.**

Fuel power and electrical power are different utility streams.

# Energy history

Energy results are periodically written to SQLite history with a retention policy.

The history system was designed around roughly 60-second persistence with configurable retention (for example 7 days).

`instance_key` needs to survive history so multiple lighting zones remain distinguishable.

Where possible, long-term analytics-friendly dimensions such as `instance_key` should be first-class columns rather than hidden only in JSON metrics.

# Dashboards

## BACnet dashboard

The former general Analytics view has been renamed/repositioned as a **BACnet** dashboard.

It focuses on protocol/system information such as BACnet traffic, requests, device analytics, object analytics, discovery, errors, performance, and live protocol metrics.

Energy/utility charts were removed from this dashboard.

A protocol/network-style icon such as Ant Design `ApiOutlined` is preferred for this view.

## Utilities dashboard

A separate `UtilitiesDashboard.vue` was introduced.

Purpose:

> What utilities is the building consuming and producing?

Current logical sections:

### Electricity

- current electrical demand
- accumulated electrical energy
- equipment breakdown
- 24h electrical-demand history

Boiler fuel input must not be included in electrical demand.

### Natural Gas

- current gas flow in CFM
- current gas flow in m³/h
- fuel input kW
- accumulated fuel energy kWh

Future:

- accumulated gas volume in m³
- utility cost
- carbon emissions

The backend does not yet accumulate physical gas volume, so the UI should not pretend that it does.

### Heating

- boiler heating output
- boiler efficiency
- fuel input vs heating output history

### Cooling

- cooling load
- chiller electrical input
- plant COP
- cooling-load history

### Lighting

- lighting demand
- accumulated lighting energy
- lighting zones
- zone breakdown
- history

Utilities and equipment analytics are conceptually different:

- Equipment analytics: **How is this equipment performing?**
- Utilities: **What is the building consuming?**

# Commissioning / Functional Testing

A commissioning scaffold already exists.

Conceptual architecture:

```text
Commissioning Tests
        |
Commissioning Engine
        |
Semantic / Brick Resolver
        |
Building Points
        |
Simulation Engine
```

The first scaffolded AHU test is read-only baseline verification.

The semantic resolver is intentionally a compatibility boundary:

- today: resolve using current point tags/names,
- after Brick Core: use graph traversal,
- commissioning tests should not need major rewrites.

Future functional tests may include AHU fan/supply-air/damper/coil/filter tests, chiller start/stop/capacity/COP/flow tests, boiler start/stop/heating/efficiency tests, lighting on/off/dimming/power/fault tests, and VAV damper/airflow/reheat/zone-temperature tests.

Functional tests should generally:

1. capture baseline,
2. apply command(s),
3. wait for simulator response,
4. capture measurements,
5. compare against expected behavior/tolerance,
6. restore original values even when a test fails.

Commissioning is essentially a collection of functional tests with pass/fail/warning reporting.

# Control sequences

A control sequence means the operational logic telling equipment how to behave.

It is conceptually separate from BACnet/Modbus.

Protocols communicate values; control logic determines what values should be commanded.

Future architecture:

```text
Control Sequence
    |
reads simulated sensors
    |
writes simulated commands
    |
Equipment responds
    |
Energy changes
    |
Commissioning verifies behavior
```

Potential future sequences include AHU occupied/unoccupied, supply-air-temperature reset, economizer, static-pressure reset, hot-water reset, chilled-water reset, chiller staging, boiler staging, and lighting occupancy/daylight control.

Potential control modes:

- Legacy
- ASHRAE Guideline 36
- Faulty
- Custom

Do not emulate vendor-specific controller firmware unless there is a strong reason.

A generic Control Engine is preferred.

# Simulation behaviors

Current behaviors include things such as constant, sine, noise, random_walk, manual, schedule, ramp, and fault.

A useful future behavior would be a generic **expression/linked behavior**.

Example concept:

```text
Lighting power =
if lights_on:
    rated_power * dim_level
else:
    standby_power
```

This allows realistic cause-and-effect without adding a custom behavior for every equipment type.

Possible future use cases:

- fan power from fan speed
- pump power from VFD speed
- lighting power from dim level
- airflow from damper position
- gas use from firing rate
- valve/flow relationships

Prefer a generic expression/dependency engine over many equipment-specific behaviors.

# Broader BMS scope

The long-term product can model more than HVAC.

Potential future systems include access control, doors/fobs/badge events, security events, camera/VMS events, fire alarm integration, elevators, water, meters, and additional lighting protocols.

Do not attempt to simulate video streams.

For cameras/security, simulate **events and system interactions**, for example:

```text
Motion detected
    |
Occupancy becomes true
    |
Lighting turns on
    |
HVAC enters occupied mode
```

Access-control functional examples:

```text
Valid credential
-> unlock
-> door opens
-> door closes
-> relock
```

```text
Door contact opens without unlock
-> forced-door alarm
```

These fit naturally into Functional Testing / Commissioning and Control Sequences.

# Project comparison / energy-savings scenarios

Use Projects as scenario containers.

A future comparison engine/dashboard should allow:

```text
Baseline Project
vs
Optimized Project
```

Compare:

- electrical kWh
- gas/fuel kWh
- gas volume
- peak electrical demand
- heating output/energy
- cooling energy
- equipment runtime
- comfort violations
- alarms/faults
- estimated cost
- emissions

Important principle:

**Do not present energy savings without comfort/operational context.**

An energy-saving strategy that simply reduces comfort is not necessarily a successful optimization.

# Key engineering principles

1. **Keep protocols separate from building logic.**
   BACnet is an interface, not the semantic or control architecture.

2. **Brick is optional/additive.**
   The simulator must still function without semantic metadata.

3. **Use canonical semantics.**
   Do not invent Brick class names to solve identity/relationship problems.

4. **Use relationships for context.**
   Supply vs return, Zone A vs Zone B, fan ownership, etc. belong in the graph.

5. **Allow duplicate semantic classes.**
   Never assume one point per Brick class per device.

6. **Keep equipment energy models independent of lookup mechanics.**
   Semantic/name resolution belongs in context/resolver layers.

7. **Preserve utility types.**
   Electricity, gas/fuel, heating, and cooling are not interchangeable.

8. **Keep migrations additive/backward-compatible.**

9. **Reuse the new refactored scaffold.**
   Avoid adding more unrelated logic to `legacy.py`.

10. **Prefer reusable engines over one-off hacks.**
    Examples: semantic resolver, expression behavior engine, control engine, commissioning test framework.

# Near-term priorities

Suggested order:

1. Finish/validate current energy models and history.
2. Complete Brick Core migration.
3. Update Energy/Commissioning resolvers to use Brick relationships.
4. Finish Commissioning / Functional Testing framework.
5. Add generic Control Sequence engine.
6. Add linked/expression behavior for realistic point dependencies.
7. Add project comparison / energy-savings scenarios.
8. Expand BMS domains (access/security/fire/etc.) only after the common architecture is stable.

# Important temporary compatibility notes

Some current semantic names around AHU supply/return fans were introduced as temporary aliases because the old flat `point_type -> value` lookup could not distinguish duplicate canonical point classes.

Do not proliferate this pattern.

After Brick Core, represent:

```text
AHU
  hasPart -> Supply Fan
  hasPart -> Return Fan

Supply Fan
  hasPoint -> canonical speed/status points

Return Fan
  hasPoint -> canonical speed/status points
```

Similarly, lighting zones should be modeled as semantic entities/relationships, while the Energy Engine continues to use separate model instances such as:

```text
lighting / zone-a
lighting / zone-b
```

# Summary

Treat the project as an **open BMS / smart-building simulator** with BACnet as its first protocol.

The strongest product direction is the combination of:

```text
Building model
+ equipment simulation
+ BACnet
+ Brick
+ energy/utilities
+ fault injection
+ control sequences
+ functional testing
+ commissioning
+ scenario/project comparison
```

When implementing new work, favor architecture that supports this broader BMS direction rather than narrowly optimizing for BACnet-only use cases.
