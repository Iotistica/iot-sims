# RTU + VAV + ThermalZone System Integration Report

Proves that the three FMUs (`models/rtu/RTU.fmu`,
`models/vav/SimpleVAVZone.fmu`, `models/zone/ThermalZone.fmu`) can operate
together as one coherent packaged VAV system, with a real physical duct-
pressure coupling between RTU and VAV (added in the second pass below --
see "Update: RTU.dpSup -> VAV.dpSup pressure coupling closed"):

```
Weather / boundary conditions
          |
          v
         RTU
          |
          | TSup             dpSup
          v                    |
         VAV <------------------
          |
          v
     ThermalZone
          |
          +---- zone temperature feedback ----> VAV
                                                  |
                                    (damper position) --> RTU.uVAVDamMax
```

`models/rtu/RTU.mo` and `models/zone/ThermalZone.mo` were **not modified**,
in either pass. `models/vav/SimpleVAVZone.mo` was **not modified** in the
first pass (the results below that predate the pressure-coupling update);
it was deliberately modified in the second pass to add the `dpSup` input --
see that section for the exact change. Their standalone test suites
(`models/rtu/tests/test_rtu.py`, `models/vav/tests/test_vav.py`, etc.) are
each maintained separately; this file is `tests/integration/
system_harness.py` + `tests/integration/test_rtu_vav_zone_system.py`.

```
pytest tests/integration -m integration -v -s   ->   6 passed in ~4m14s
(Scenarios 1-5 below predate the pressure-coupling update -- numbers shifted
slightly on re-run afterward, same conclusions; Scenario 6 is new.)
```

## Signal mapping used

Every connection below was verified directly against each FMU's own
`modelDescription.xml` (`causality`, the `<Real unit="...">` child element),
not just `model.json`'s display-unit metadata -- per the task's own warning
that RTU/VAV/ThermalZone temperatures are Kelvin at the FMU boundary while
`model.json` may declare a `c_to_k`/`k_to_c` conversion for the outward-
facing REST API layer that this harness bypasses entirely.

| From | To | FMU-level unit (both sides) |
|---|---|---|
| `RTU.TSup` (output) | `VAV.TSupAHU` (input) | K |
| `VAV.TSup` (output, discharge air) | `ThermalZone.TSup` (input) | K |
| `VAV.VSup_flow` (output) | `ThermalZone.VSup_flow` (input) | m3/s |
| `ThermalZone.TRoo` (output) | `VAV.TRoo` (input) | K |
| `ThermalZone.TRoo` (output) | `RTU.TRet` (input) | K |
| `VAV.yDam_actual` (output, actual damper position) | `RTU.uVAVDamMax` (input) | fraction, unit `"1"` |
| `RTU.dpSup` (output) | `VAV.dpSup` (input, **added** -- see update below) | Pa |

Confirmed by direct inspection (excerpted from `modelDescription.xml`):

```
VAV   TRoo         causality=input   <Real start="0.0" unit="K"/>
VAV   TSup         causality=output  <Real unit="K"/>
VAV   TSupAHU      causality=input   <Real start="0.0" unit="K"/>
VAV   VSup_flow    causality=output  <Real min="0.0" unit="m3/s"/>
VAV   yDam_actual  causality=output  <Real unit="1"/>
ZONE  TRoo         causality=output  <Real unit="K"/>
ZONE  TSup         causality=input   <Real start="0.0" unit="K"/>
ZONE  VSup_flow    causality=input   <Real start="0.0" unit="m3/s"/>
RTU   TSup         causality=output  <Real unit="K"/>  (see models/rtu/tests/README.md)
RTU   uVAVDamMax   causality=input   <Real unit="1"/>
RTU   TRet         causality=input   <Real unit="K"/>
```

No mismatched-unit connection exists anywhere in this wiring -- every
temperature link is K-to-K, every damper/valve fraction is `"1"`-to-`"1"`,
`VSup_flow` is `m3/s`-to-`m3/s`.

### Exogenous (scenario) inputs, not cross-model signals

`RTU.TSupSet`, `RTU.TOut`, `RTU.uFan`, `VAV.TRooHeaSet`, `VAV.TRooCooSet`,
`ThermalZone.TOut`, `ThermalZone.QInternal` are supervisory/schedule/weather
inputs in this repo's own architecture (matching how `RTU.mo`/`AHU.mo`
already treat `TSupSet`/`uFan` as external commands, not another model's
output) -- held constant per scenario, not produced by any of the three
models.

### For a single VAV, its damper position represents the RTU's "most-open" signal

Per the task's own instruction: with exactly one VAV terminal, `VAV.yDam_actual`
(the *actual*, actuator-lag-delayed position -- not `yDam`, the raw
controller command) is used directly as `RTU.uVAVDamMax`. `yDam_actual`
comes from `vavBox.y_actual` in `SimpleVAVZone.mo` (a first-order actuator
model), matching how a real static-pressure-reset sequence uses damper
*feedback*, not the raw command.

## Interface limitation discovered (RESOLVED -- see update below)

**RTU's supply airflow (`VSup_flow`) and duct pressure (`dpSup`) have no
consuming input anywhere in VAV or ThermalZone.** `VAV.mo`'s FMU inputs are
only `TRoo`, `TRooHeaSet`, `TRooCooSet`, `TSupAHU` -- there is no airflow or
pressure input at all. Reading `SimpleVAVZone.mo` confirms why: its air side
is driven by its own fixed `Boundary_pT supAir` source (`p = 101325 + dpAir`,
`dpAir` a fixed 200 Pa parameter, not RTU's actual `dpSup`), so
`VAV.VSup_flow` is entirely self-computed from the terminal's own
Guideline-36-style `RoomVAV` controller and `VAVReheatBox` physics, not from
whatever RTU actually delivers.

This did **not** prevent a physically meaningful system-level test at the
time -- the thermal (`TSup`) and control (damper position <->
`uVAVDamMax`) connections above are real and demonstrably produce correct
closed-loop behavior (see results below) -- so per the task's own
conditional instruction, this was not treated as a blocking defect
requiring a STOP. It was a genuine, worth-noting characteristic of the
signal-based (not shared fluid network) integration architecture already
documented elsewhere in this repo (`BoilerPlant.mo`'s own header makes the
identical point about its own AHU integration). The identified smallest
interface change (add a `dpSup`-derived input to `VAV.mo` that the
terminal's own airflow calculation could saturate against) was implemented
in a follow-up task -- see "Update: RTU.dpSup -> VAV.dpSup pressure
coupling closed" below.

## Update: RTU.dpSup -> VAV.dpSup pressure coupling closed

### 1. Exact `SimpleVAVZone.mo` changes

Four changes, all additive -- no existing equation, connection, or
parameter of the `RoomVAV` controller, `VAVReheatBox`, reheat valve, or
actuator dynamics was touched:

- **New FMU input** `dpSup` (`Modelica.Blocks.Interfaces.RealInput`, `unit="Pa"`, `start=dpAir`):
  ```modelica
  Modelica.Blocks.Interfaces.RealInput dpSup(
    final unit="Pa",
    start=dpAir)
    "Available upstream supply duct static pressure (e.g. from
    RTU.dpSup/AHU.dpSup). Determines airflow through the VAV damper together
    with the damper's own commanded position -- see supAir/vav below. Not
    tied to any specific upstream model; RTU and AHU both expose a
    compatible dpSup output.";
  ```
- **`supAir` boundary changed from a fixed pressure parameter to an externally driven one** (`use_p_in=true`, the same `Buildings.Fluid.Sources.Boundary_pT` conditional-input mechanism already used for `TSupAHU` via `use_T_in`):
  ```modelica
  Buildings.Fluid.Sources.Boundary_pT supAir(
    redeclare package Medium = MediumA,
    use_T_in=true,
    use_p_in=true,
    nPorts=1)
    "Upstream supply-air pressure boundary -- pressure is driven externally
    via dpSup rather than a fixed offset, so RTU/AHU fan static-pressure
    output has a real, physical effect on this terminal's airflow";
  ```
  (previously: `p=101325 + dpAir`, a fixed parameter expression, no `use_p_in`)
- **New equation**, alongside the existing `supAir.T_in = TSupAHU;`:
  ```modelica
  supAir.p_in = 101325 + max(0, dpSup);
  ```
  `max(0, ...)` guards against a negative pressure differential, which is
  not physically meaningful for this boundary and is not asserted against
  by `Boundary_pT`'s own air-medium sanity range (50,000-150,000 Pa
  absolute) without it.
- **`dpAir`'s role changed** from "the pressure difference across the
  terminal" (used directly in an equation) to "the `dpSup` input's own FMU
  `start` value for standalone use with no external driver" (used only as
  a default, never in an equation once `dpSup` is actually supplied). Kept
  at its same default (200 Pa) for backward compatibility. `Documentation`
  annotation updated accordingly (new "Upstream duct static pressure
  (dpSup)" section; `Inputs:` list now includes `dpSup`).

Reused an existing, already-verified Buildings pattern (`use_p_in`/`p_in`
mirrors `use_T_in`/`T_in`, already present in this same model for
`TSupAHU`) rather than inventing a new pressure-source mechanism. No second
airflow controller was added -- airflow is still determined entirely by
`VAVReheatBox`'s own `Buildings.Fluid.Actuators.Dampers.Exponential`
component from upstream/downstream pressure and commanded damper position,
exactly as the task specified.

### 2. Exact `model.json` changes

One new input entry added (`models/vav/model.json`), reusing the same
`Supply_Air_Static_Pressure_Sensor` point class RTU's and AHU's own
`dpSup` outputs already use in their `model.json` files (per the task's
"do not invent a new semantic convention" instruction) -- so the same
BACnet mapping logic that already understands RTU/AHU's `dpSup` output
also understands VAV's new `dpSup` input:

```json
{
  "name": "supply_duct_static_pressure_pa",
  "label": "Supply Duct Static Pressure",
  "fmu_variable": "dpSup",
  "unit": "Pa",
  "default": 200.0,
  "suggested_point_types": ["Supply_Air_Static_Pressure_Sensor", "Static_Pressure_Sensor"],
  "mapping_hints": {
    "equipment_scope": "upstream",
    "preferred_equipment_types": ["Air_Handling_Unit", "Rooftop_Unit"],
    "relationship": "feeds",
    "signal_role": "supply_duct_static_pressure"
  },
  "semantic": {"point_class": "Supply_Air_Static_Pressure_Sensor"}
}
```

`preferred_equipment_types` lists both `Air_Handling_Unit` and
`Rooftop_Unit` deliberately -- per the task's "keep the architecture
reusable" instruction, VAV is not hardcoded to RTU specifically; either
`RTU.dpSup -> VAV.dpSup` or `AHU.dpSup -> VAV.dpSup` is a valid mapping.

### 3. FMU build result

Rebuilt via the standard `shared/build-fmu.sh` toolchain: **434/434
equations balanced**, same benign pre-existing warnings as always (alias
variables, `VAVReheatBox`'s own unrelated `THeaWatInl_nominal`-family
"no value, using start value" notices -- present before this change too,
unrelated to `dpSup`). Confirmed via direct `modelDescription.xml`
inspection: exactly 5 inputs (`TRoo, TRooCooSet, TRooHeaSet, TSupAHU,
dpSup`) and 6 outputs (unchanged), `dpSup` typed `<Real start="200.0"
unit="Pa"/>`, `causality="input"`. `python shared/validate_fmu_metadata.py
--fmu models/vav/SimpleVAVZone.fmu --metadata models/vav/model.json` ->
`FMU metadata valid: 5 inputs, 6 outputs`.

### 4. Standalone VAV test results

**First standalone pytest suite for this model** (`models/vav/tests/
test_vav.py` + `README.md`) -- no prior pytest baseline existed to
preserve (the repo's only earlier VAV test artifact, `TestSimpleVAVZone.mo`,
wires to `vavZone.TOut`/`vavZone.QInternal`, which don't exist on this
model, and isn't part of any build/CI pipeline). 14/14 pass:

```
pytest models/vav/tests -v
V1  deadband_baseline                    PASSED
V2  cooling_demand                       PASSED
V3  heating_reheat                       PASSED
V4  damper_actuator_settles              PASSED
V5  fmu_interface_regression             PASSED
V5b model_json_metadata_matches_fmu      PASSED
V6  pressure_sweep_airflow_responds      PASSED
V7  zero_pressure_stable                 PASSED
V7b negative_pressure_clamped            PASSED
V8  high_pressure_stable                 PASSED
(extended) six_hour_stability            PASSED
(extended) very_high_pressure_no_blowup  PASSED
(extended) dpsup_step_response           PASSED
(extended) heating_pressure_sweep        PASSED
============================== 14 passed in 6.89s ==============================
```

### 5. Pressure sweep results (0 / 50 / 100 / 150 / 200 / 400 / 500 Pa)

Empirically probed first, before writing any assertion (fixed strong
cooling demand, `TRoo=27C, TRooCooSet=23C, TSupAHU=13C`):

| dpSup (Pa) | yDam (command) | yDam_actual | VSup_flow (m3/s) |
|---|---|---|---|
| 0 | 1.0000 | 1.0000 | 0.00000 |
| 50 | 1.0000 | 1.0000 | 0.28868 |
| 100 | 1.0000 | 1.0000 | 0.40825 |
| 150 | 0.9743 | 0.9741 | 0.49461 |
| 200 | 0.8119 | 0.8119 | 0.49975 |
| 300 | 0.7024 | 0.7024 | 0.49999 |
| 400 | 0.6500 | 0.6500 | 0.50000 |
| 500 | 0.6160 | 0.6160 | 0.50000 |
| 700 | 0.5702 | 0.5702 | 0.50000 |

**Not a naive linear relationship -- genuine, physically correct VAV
terminal-box behavior:** below ~150-200 Pa the terminal is pressure-
limited (damper pinned open at/near 1.0, unable to reach its flow
setpoint) and airflow rises with pressure; above that threshold,
`RoomVAV`'s own flow-feedback control loop (fed by the actual measured
`VDis_flow`, not open-loop) throttles the damper *closed* to hold exactly
the design flow setpoint (`mCooAir_flow_nominal/rhoAir = 0.6/1.2 = 0.5
m3/s`) rather than overflow -- a well-designed VAV controller is supposed
to become pressure-independent once it has enough authority. At `dpSup=0`:
stable, zero airflow, damper saturates open, no NaN/Inf/crash. At
`dpSup=500`: stable, airflow at exactly the design setpoint, not exploding.
A negative-`dpSup` clamp test (`-50` vs `0`) confirmed `max(0, dpSup)`
produces bit-identical results, guarding against a stale/noisy negative
point value.

### 6. RTU + VAV + ThermalZone integration test results

All 6 scenarios pass (`pytest tests/integration -m integration -v -s` ->
`6 passed in ~4m14s`). Scenarios 1-5 (re-run with the new coupling active)
show the same qualitative conclusions as the original pass, with small
(sub-1%) numeric shifts from the new pressure-dependence -- most visible
briefly during fan-startup transients (low `yFan` -> low `dpSup` ->
momentarily pressure-limited VAV), converging back to near-identical
steady-state behavior once RTU's fan ramps up and `dpSup` exceeds VAV's
own ~150-200 Pa authority threshold (which it does under essentially all
normal `uFan=1` operating conditions, since RTU's `dpSupSetMin=200 Pa`
floor already sits at that threshold). Example, Scenario 5 (six-hour), t=2100s:

| | zone.TRoo (C) | RTU.dpSupSet (Pa) | VAV.yDam_actual | VAV.VSup_flow |
|---|---|---|---|---|
| Before pressure coupling | 24.058 | 294.07 | 0.7286 | 0.4339 |
| After pressure coupling | 24.057 | 294.64 | 0.7065 | 0.4537 |

### 7. Six-hour stability result

Scenario 5 re-run with the pressure coupling active: still 72/72 exchange
steps complete, `assert_no_nan_inf` passes over every tracked output, same
bounded self-sustaining oscillation character as before (zone ~22.35-24.06C,
damper ~0.12-0.71, `dpSupSet` ~200-302 Pa) -- no divergence, no growth in
oscillation amplitude introduced by the new coupling.

### 8. Evidence that RTU.dpSup now affects VAV.VSup_flow

The direct, causal proof (`test_scenario6_pressure_response_causality`,
new): two otherwise-identical systems, same strong cooling demand
(`TRoo0=27C, QInternal=3000W`), differing only in RTU's own fan/duct-
pressure capacity (`dpSup_nominal`, the fan law's pressure ceiling at full
speed) -- 500 Pa (default) vs an artificially constrained 80 Pa:

| End of run (t=7200s) | | RTU.dpSup, final/run-max (Pa) | RTU.dpSupSet, final (Pa) | VAV.yDam_actual | VAV.VSup_flow (m3/s) |
|---|---|---|---|---|---|
| baseline (500 Pa capacity) | | 301.43 / 308.00 | 301.43 (tracks dpSup closely) | 0.7014 | **0.5000** |
| constrained (80 Pa capacity) | | 80.00 / 80.00 (capped) | 500.00 (pinned at ceiling, unreachable) | **1.0000** | **0.3651** |

With less available duct pressure, VAV's damper opens *further* (1.0000
fully open vs 0.7014 -- the flow-feedback loop tries harder to compensate)
yet delivers **27% less airflow** (0.3651 vs 0.5000 m3/s) than the
baseline. This is decisive: airflow dropped despite maximum damper
authority being applied, which can only happen if the upstream pressure
itself is genuinely the limiting factor -- proving `RTU.dpSup -> VAV.dpSup`
is a real physical link, not two signals that merely happen to move
together as demand rises.

### 9. Evidence that VAV.yDam_actual still drives RTU's pressure reset

Unaffected by this change (RTU.mo was not modified) and re-verified in
both systems above: `RTU.dpSupSet` rises well above its 200 Pa floor once
`VAV.yDam_actual` opens substantially in both the baseline run (up to
301 Pa) and the constrained run (up to 500 Pa, RTU's reset ceiling,
because the static-pressure PI keeps commanding more in a vain attempt to
reach a setpoint its own fan can't physically deliver) -- the
`uVAVDamMax -> dpSupSet` half of the closed loop is intact and working
exactly as it did before this change.

### 10. Instability or unexpected behavior discovered

**None.** The now-genuinely-circular loop (`VAV damper -> RTU pressure ->
VAV airflow -> zone -> VAV damper`) was specifically checked across all
five re-run scenarios plus the new causality test, at the existing
`dt_s=300s` Gauss-Seidel exchange interval -- no divergence, no NaN/Inf, no
solver failure, no growth in oscillation amplitude versus the pre-coupling
baseline. No controller gain, exchange timestep, or any other tuning
parameter was changed; none was needed. The one "unexpected" finding
(RTU's static-pressure PI pinning `dpSupSet` at its 500 Pa reset ceiling
in the artificially pressure-constrained Scenario 6 case) is expected,
correct behavior for a PI controller commanded to reach an unreachable
setpoint -- not instability, and specific to a deliberately unrealistic
capacity test, not a normal operating condition.

### 11. Changes made beyond the pressure interface, and why

None beyond what's listed above (`SimpleVAVZone.mo`'s `dpSup` input/
equation/documentation, `model.json`'s new input entry, the new
`models/vav/tests/` suite, and `system_harness.py`/
`test_rtu_vav_zone_system.py`'s wiring + new Scenario 6 test). The
`RoomVAV` controller, reheat valve, damper actuator dynamics, and existing
airflow/temperature sensing were not touched, per the task's explicit
scope.

## Harness design note: full replay-from-t=0, not FMU-instance continuation

Verified empirically (not assumed) that `pyfmi`'s high-level `simulate()`
cannot be called a second time on an already-simulated `FMUModelME2`
instance -- a second `simulate(start_time=300, ...)` call after an initial
`simulate(start_time=0, final_time=300, ...)` raises
`FMUException: Failed to setup the experiment` from `setup_experiment`. This
harness therefore uses the same technique `shared/runtime/models/manager.py`
already uses in production for the identical reason: reload a fresh FMU
instance at every exchange step and replay the full accumulated input
history from `t=0`. Every model is stepped every `dt_s=300s` (Gauss-Seidel:
each step's cross-model inputs are the *previous* step's outputs, held
constant in between -- a one-step lag, standard for loosely-coupled
co-simulation and representative of a real supervisory-control scan cycle).

**A second, genuine harness bug found and fixed during this work:**
`ThermalZone.mo`'s actual initial temperature comes from its own
`TRoo_start` parameter (`initial equation TRoo = TRoo_start;`, compiled
default 295.15 K = 22C) -- completely independent of the `TRoo0_c` scenario
argument, which only seeds the *other* models' first-window boundary-
condition guesses. The first version of this harness silently started every
scenario's zone at 22C regardless of the caller's intended initial
condition -- caught because Scenario 3 (cold zone, `TRoo0_c=18`) showed the
zone starting near 21.6C instead of near 18C. Fixed by explicitly setting
`fmu.set("TRoo_start", c2k(TRoo0_c))` on every `ThermalZone.fmu` load in
`run_system()`.

## Scenario 1: normal occupied cooling

`TOut=32C, TSupSet=13C, uFan=1.0, TRooHeaSet=20C, TRooCooSet=23C,
QInternal=3000W, TRoo0=22C`, 3-hour run.

| t (s) | zone.TRoo (C) | RTU.TSup (C) | RTU.yCoo | RTU.dpSupSet (Pa) | VAV.yDam_actual | VAV.VSup_flow (m3/s) |
|---|---|---|---|---|---|---|
| 300 | 22.064 | 13.871 | 0.3548 | 200.00 | 0.0592 | 0.0002 |
| 1200 | 22.868 | 13.004 | 0.3847 | 200.00 | 0.2353 | 0.0526 |
| 2100 | 23.457 | 13.003 | 0.4034 | 200.00 | 0.3652 | 0.1108 |
| 3000 | 23.825 | 13.002 | 0.4172 | 200.00 | 0.5602 | 0.2613 |
| 3900 | 23.744 | 13.019 | 0.5045 | 290.34 | 0.7260 | 0.4316 |
| 4800 | 23.462 | 13.004 | 0.5575 | 366.67 | 0.7793 | 0.4767 |
| 5700 | 23.161 | 13.000 | 0.5657 | 394.09 | 0.7988 | 0.4910 |
| 6600 | 22.878 | 12.999 | 0.5611 | 404.82 | 0.8051 | 0.4951 |
| 7500 | 22.647 | 12.995 | 0.5352 | 382.65 | 0.7615 | 0.4615 |
| 8400 | 22.556 | 12.990 | 0.4714 | 303.22 | 0.6678 | 0.3721 |
| 9300 | 22.677 | 12.988 | 0.3852 | 200.77 | 0.5702 | 0.2644 |
| 10200 | 22.971 | 13.001 | 0.3911 | 200.00 | 0.4969 | 0.1981 |

**The full closed control chain, demonstrated exactly as expected:** zone
temperature rises above the 23C cooling setpoint (peaks 23.825C at t=3000)
-> VAV cooling demand and damper position climb (0.06 -> 0.81 at t=6600) ->
RTU sees rising `uVAVDamMax` -> `dpSupSet` rises 200 -> 405 Pa -> RTU
delivers more conditioned air (`VSup_flow` 0.0002 -> 0.495 m3/s) -> zone
temperature turns down (peak at t=3000, falling to 22.556C by t=8400) ->
demand eases, damper and `dpSupSet` retreat (a real, damped closed-loop
oscillation, not a monotonic settle -- see "control interaction" note
below).

## Scenario 2: low cooling demand

`TOut=26C, TRooCooSet=23C, QInternal=200W, TRoo0=23C` (starts at setpoint),
2-hour run.

| t (s) | zone.TRoo (C) | RTU.yCoo | RTU.dpSupSet (Pa) | VAV.yDam_actual | VAV.VSup_flow (m3/s) |
|---|---|---|---|---|---|
| 300 | 22.847 | 0.3530 | 200.00 | 0.0585 | 0.0002 |
| 1500 | 22.995 | 0.3639 | 200.00 | 0.2750 | 0.0685 |
| 3300 | 22.790 | 0.3591 | 200.00 | 0.4004 | 0.1300 |
| 4500 | 22.578 | 0.3530 | 200.00 | 0.4223 | 0.1434 |
| 5700 | 22.360 | 0.3465 | 200.00 | 0.4294 | 0.1479 |
| 6900 | 22.148 | 0.3404 | 200.00 | 0.4316 | 0.1494 |

As expected: with only a small internal gain and the zone already at
setpoint, damper position and airflow settle at a modest, roughly
steady-state level (~0.43 / 0.15 m3/s), and `dpSupSet` stays pinned at its
floor (200 Pa = `dpSupSetMin`) for the entire run -- the damper never
crosses `vavDamLow=0.60`, so RTU's static-pressure reset never engages.

## Scenario 3: heating/reheat

`TOut=5C, TSupSet=13C, TRooHeaSet=20C, QInternal=400W, TRoo0=18C`, 6-hour
run (see "genuine capacity-limited finding" below for why not the harsher
`TOut=0C, QInternal=200W` combination originally tried).

| t (s) | zone.TRoo (C) | RTU.TSup (C) | RTU.yCoo | RTU.yHea | VAV.yVal_actual |
|---|---|---|---|---|---|
| 300 | 17.827 | 13.646 | 0.0000 | 0.0111 | 0.6971 |
| 1200 | 17.772 | 13.001 | 0.0000 | 0.0000 | 1.0000 |
| 3900 | 18.077 | 13.000 | 0.0000 | 0.0000 | 1.0000 |
| 7500 | 18.679 | 13.000 | 0.0000 | 0.0000 | 1.0000 |
| 11100 | 19.173 | 13.000 | 0.0000 | 0.0000 | 1.0000 |
| 14700 | 19.565 | 13.000 | 0.0000 | 0.0000 | 1.0000 |
| 18300 | 19.878 | 13.000 | 0.0000 | 0.0000 | 1.0000 |
| 20100 | 20.009 | 13.000 | 0.0000 | 0.0000 | 1.0000 |
| 21000 | 20.069 | 13.000 | 0.0000 | 0.0000 | 1.0000 |

VAV enters reheat immediately (valve saturates at 100% open by t=1200) and
stays there; RTU's own SAT loop holds `TSup` at exactly its 13C setpoint
throughout, running neither its own DX cooling nor heating
(`yCoo=yHea=0.0000` after the initial transient) -- **RTU and VAV do not
fight each other**: RTU keeps supplying its normal cold-deck air and lets
VAV's local reheat do all the zone-level heating work, which is the
standard VAV-reheat sequence. The zone crosses the 20C heating setpoint at
t=20100s.

### Scenario 3: a genuine capacity-limited finding (not a bug)

The first attempt at this scenario used `TOut=0C, QInternal=200W`. Under
those conditions the reheat valve still saturates at 100% almost
immediately, but the zone temperature barely recovers -- still only
~18.15C after **12 simulated hours** (double the run length), having
dipped to a low of 17.42C before slowly climbing. This was verified across
three separate probes (`TOut=5C/400W`, `TOut=0C/400W`, `TOut=0C/200W`/12h)
before concluding it's real: at `TOut=0C` combined with a low internal
gain, `VAV.mo`'s reheat coil (`mHeaAir_flow_nominal=0.3 kg/s`,
`THeaWatSup=55C` fixed) genuinely cannot deliver enough heat to fully
overcome envelope losses and outdoor-air mixing within a reasonable
window at these default sizing parameters -- a real capacity/sizing
characteristic of the existing model, not a NaN, crash, or wiring defect,
and not something this task's scope covers fixing (no new physics, no
model changes). The milder `TOut=5C, QInternal=400W` scenario above was
used instead because it cleanly demonstrates the requested behavior
(directional recovery toward setpoint) without conflating it with a
separate capacity-sizing question.

## Scenario 4: economizer

Two runs, identical zone/internal conditions (`TRooCooSet=23C,
QInternal=1500W, TRoo0=25C`), only `TOut` differs.

**Favorable OA (TOut=12C):**

| t (s) | zone.TRoo (C) | RTU.yCoo | RTU.dpSupSet (Pa) | VAV.yDam_actual |
|---|---|---|---|---|
| 300 | 24.754 | 0.0000 | 200.00 | 0.1735 |
| 1500 | 24.104 | 0.0000 | 213.22 | 0.6749 |
| 2700 | 22.755 | 0.0000 | 362.14 | 0.7756 |
| 4500 | 21.363 | 0.0000 | 200.00 | 0.5176 |
| 6900 | 20.670 | 0.0000 | 200.00 | 0.4399 |

**Unfavorable OA (TOut=32C):**

| t (s) | zone.TRoo (C) | RTU.yCoo | RTU.dpSupSet (Pa) | VAV.yDam_actual |
|---|---|---|---|---|
| 300 | 24.908 | 0.4444 | 200.00 | 0.1735 |
| 1500 | 24.920 | 0.4731 | 215.26 | 0.6732 |
| 2700 | 24.100 | 0.5843 | 362.44 | 0.7756 |
| 4500 | 22.915 | 0.5661 | 404.24 | 0.8064 |
| 6900 | 22.192 | 0.3707 | 200.00 | 0.5174 |

At `TOut=12C` (below the zone's return-air temperature the whole run),
RTU's economizer opens well above `minOutAirFra` and **`rtu.yCoo` stays
exactly 0.0000 for the entire run** -- the DX compressor never engages at
all, because free outdoor-air cooling alone satisfies the SAT setpoint. At
`TOut=32C` (identical zone/internal conditions, unfavorable OA), the
compressor engages substantially (`yCoo` up to 0.5843). Compressor cooling
demand doesn't just decrease under favorable conditions here -- it's fully
eliminated. VAV/zone response is stable and directionally identical in
both cases (temperature falls from ~24.8-24.9C as cooling removes the
load); no NaN/Inf, no instability.

## Scenario 5: six-hour integrated stability

`TOut=32C, TSupSet=13C, TRooHeaSet=20C, TRooCooSet=23C, QInternal=2000W,
TRoo0=24C`, full 21,600s (72 exchange steps).

| t (s) | zone.TRoo (C) | RTU.TSup (C) | RTU.yCoo | RTU.dpSupSet (Pa) | VAV.yDam_actual | VAV.VSup_flow (m3/s) |
|---|---|---|---|---|---|---|
| 300 | 23.960 | 13.935 | 0.4139 | 200.00 | 0.1191 | 0.0101 |
| 2100 | 24.058 | 13.019 | 0.5216 | 294.07 | 0.7286 | 0.4339 |
| 3900 | 23.094 | 13.000 | 0.5657 | 394.68 | 0.7993 | 0.4913 |
| 5700 | 22.352 | 12.989 | 0.4878 | 331.62 | 0.6886 | 0.3933 |
| 7500 | 22.527 | 13.001 | 0.3790 | 200.00 | 0.4742 | 0.1796 |
| 9300 | 23.035 | 13.001 | 0.3938 | 200.00 | 0.4393 | 0.1545 |
| 11100 | 23.424 | 13.001 | 0.4069 | 200.00 | 0.5403 | 0.2393 |
| 12900 | 23.104 | 13.008 | 0.4905 | 298.98 | 0.7189 | 0.4261 |
| 14700 | 22.498 | 12.993 | 0.4761 | 309.28 | 0.6803 | 0.3854 |
| 16500 | 22.603 | 13.001 | 0.3815 | 200.00 | 0.4816 | 0.1857 |
| 18300 | 23.093 | 13.001 | 0.3956 | 200.00 | 0.4412 | 0.1557 |
| 20100 | 23.429 | 13.000 | 0.4075 | 200.00 | 0.5641 | 0.2618 |

No NaN/Inf, no FMU crash, no solver failure across the full 6-hour, 72-step
run (verified via `assert_no_nan_inf` over every tracked output, not just
the final point). The system settles into a bounded, self-sustaining
oscillation (zone ~22.15-24.06C, damper ~0.12-0.80, `dpSupSet` ~200-395 Pa)
rather than diverging or drifting -- the closed loop is stable at this
6-hour design-day condition.

## Control interaction problems discovered

**None that indicate a defect.** The one behavior worth calling out
explicitly: Scenario 1 and Scenario 5 both show a **damped oscillation**
rather than a monotonic settle to setpoint -- the zone overshoots the
cooling setpoint, the VAV/RTU response pulls it back down, demand eases,
and the cycle repeats at a smaller amplitude. This is expected, physically
normal closed-loop behavior for a PI-controlled VAV terminal reacting to a
step-like internal-gain disturbance with a Gauss-Seidel one-step-lagged
exchange interval (`dt_s=300s`) -- not evidence of instability (the
oscillation is bounded and consistent across both the 3-hour and 6-hour
runs, never diverging) and not something this task's scope covers tuning.
It did require a test-design fix (checking `max()` reached during the run
rather than a naive start-vs-end comparison for `dpSupSet` in Scenario 1)
-- documented in `system_harness.py`/`test_rtu_vav_zone_system.py`.

RTU and VAV controls were verified NOT to fight each other in the heating
scenario (RTU's own `yCoo`/`yHea` stay at ~0 while VAV reheats
independently) and in the cooling scenario (VAV never commands reheat
while RTU cooling is active -- `vav.yVal_actual` is 0.0000 throughout
Scenarios 1, 2, 4, and 5).

## Interface limitations discovered (summary)

1. **No airflow/pressure pass-through from RTU to VAV/ThermalZone** (see
   "Interface limitation discovered" above) -- `VAV.mo` computes its own
   airflow from a fixed internal `dpAir=200 Pa` boundary condition, not
   from RTU's actual `VSup_flow`/`dpSup`. Does not prevent meaningful
   system-level testing; the thermal and damper-position/pressure-setpoint
   coupling is real and demonstrated above.
2. **`ThermalZone.mo`'s initial condition is a compiled parameter
   (`TRoo_start`), not something implicit in any input** -- any harness
   driving this FMU externally must set it explicitly per scenario, or
   every run silently starts at the same 22C default regardless of
   intended initial condition. Documented in `system_harness.py`.

## Reproducing

```powershell
docker build -t iot-models-test-runtime -f Dockerfile .

docker run --rm -v "${PWD}:/app" -w /app iot-models-test-runtime `
  sh -c "pip install --quiet pytest && python -m pytest tests/integration -m integration -v -s"
```
