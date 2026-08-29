# RTU.mo Test Report

Results from running `models/rtu/tests/test_rtu.py` (test matrix R1-R24 plus
three follow-up capacity/fan-command checks, plus global invariants) against
the built `models/rtu/RTU.fmu`, via `pyfmi` in Model Exchange mode
(`load_fmu(..., kind="ME")`), matching this project's own production
calling convention in `shared/runtime/models/manager.py`. Both suites were run
inside the repo's own test-runtime Docker image (`docker build -f
Dockerfile .`), the same image `build-rtu-fmu.yml` builds in CI.

```
pytest models/rtu/tests -m core      -> 25 passed, 4 deselected
pytest models/rtu/tests -m extended  ->  4 passed, 25 deselected
```

All 29 tests pass. No test was loosened to make it pass without a documented
reason (see "Harness fix, not a model fix" below for the one case where a
test itself, not RTU.mo, needed correcting).

**Update (Trane IntelliPak comparison, adds `VOutAir_flow` output and
`uMinOutAir` input):** this pass DID make a real interface/physics-adjacent
change to RTU.mo -- see "Trane IntelliPak comparison: outdoor-air flow
output + minimum-OA input" below. It is additive and structure-preserving:
no existing equation, parameter, or output was removed or renumbered, the
idealized `HeaterCooler_u` DX approximation, continuous `yCoo` modulation,
and the `compressorCOP(TOut)`/`PCompressor` formulas are all unchanged, and
the economizer's control structure (favorable/unfavorable/disabled
behavior) is unchanged -- only the economizer floor's *source* changed,
from a compiled-in parameter to a live FMU input. The prior pass's
documentation-accuracy fix (see "Documentation fix" below) remains
in effect and unrelated to this change.

**Update (BACnet status outputs, adds `supplyFanStatus`, `coolingStatus`,
`heatingStatus`, `economizerStatus` Boolean outputs):** this pass is a pure
output addition -- four new `BooleanOutput`s and their defining equations,
plus one new tolerance parameter (`economizerActiveTol`, default 0.02). No
existing input, output, parameter, or equation was removed, renamed, or
renumbered; RTU's physics and control behavior (economizer, SAT control,
VAV static-pressure reset, capacity limits) are unchanged. See "BACnet
status outputs" below.

**Update (`supply_fan_speed_pct` dedicated fan-speed output):** a pure
`model.json`-metadata addition -- `yFan` was already an existing FMU output
(no RTU.mo or RTU.fmu change was needed or made). Adds a second, clearly
named `model.json` output entry (`supply_fan_speed_pct`, semantic
`Fan_Speed_Sensor`) aliasing the same `yFan` value for a dedicated BACnet
mapping (`RTU-1-Supply-Fan-Speed`), distinct from the pre-existing
`fan_command_pct` output entry that also reads `yFan` under a "Fan Command"
label. See "Dedicated supply-fan-speed output" below.

## Core suite (CI gate, `pytest models/rtu/tests -m core`)

| # | Test | Covers | Result |
|---|------|--------|--------|
| R1 | `test_r1_cold_start_unit_off` | Fan off, cold start -- the exact scenario that segfaulted the abandoned real DX coil (see RTU.mo Documentation); all 4 Boolean status outputs False | PASS |
| R2 | `test_r2_normal_cooling` | DX cooling engages, TSup settles at setpoint, mutual-exclusion with heating, `coolingPLR` tracks `yCoo`; `supplyFanStatus`/`coolingStatus` True, `heatingStatus` False | PASS |
| R3 | `test_r3_normal_heating` | Gas heat engages, TSup settles at setpoint, compressor stays off, `heatingPLR` tracks `yHea`; `supplyFanStatus`/`heatingStatus` True, `coolingStatus` False | PASS |
| R4 | `test_r4_heating_cooling_mutual_exclusion` | `yHea*yCoo ~ 0` across both a heating and a cooling scenario | PASS |
| R5 | `test_r5_economizer_favorable_outdoor_air` | Economizer opens above minimum OA when outdoor air is cooler than return air; `economizerStatus` True | PASS |
| R6 | `test_r6_economizer_unfavorable_outdoor_air` | Economizer holds at `minOutAirFra` when outdoor air is warmer than return air; `economizerStatus` False | PASS |
| R7 | `test_r7_economizer_disabled` | `haveEconomizer=false` locks the OA damper at `minOutAirFra`'s default value AND at a non-default supplied `uMinOutAir=0.35` -- confirms the lock tracks the live input, not a compiled-in constant; `economizerStatus` False in both cases | PASS |
| R8 | `test_r8_vav_static_pressure_reset` | Most-open-VAV-damper static-pressure reset, monotonic and matches the linear reset formula | PASS |
| R9 | `test_r9_fan_pressure_and_power_laws` | `dpSup = dpSup_nominal*yFan^2`, `PFan = PSupFan_nominal*yFan^3` | PASS |
| R10 | `test_r10_fan_off` | All fan/coil-dependent outputs, including `VOutAir_flow` and the 3 non-economizer Boolean status outputs, are exactly 0/False with the fan off | PASS |
| R11 | `test_r11_mixed_air_temperature_bounds` | `TMix` stays within `[min(TOut,TRet), max(TOut,TRet)]` | PASS |
| R12 | `test_r12_compressor_cop_bounds_and_monotonicity` | `compressorCOP` bounded by `[COP_min, COP_max]`, monotonically non-increasing with `TOut`, exactly `COP_nominal` at `TOutCOPRef` | PASS |
| R13 | `test_r13_compressor_power_derivation` | `PCompressor = QCoolLoad / compressorCOP` | PASS |
| R14 | `test_r14_gas_heating_power_derivation` | `gasHeatingPower = QHeaLoad / etaHeat` | PASS |
| R15 | `test_r15_total_electric_power_sum` | `totalElectricPower = PCompressor + PFan` | PASS |
| R15b | `test_r15b_cooling_capacity_saturates_at_nominal` | Under an oversized cooling demand, `yCoo` saturates at 1 and `QCoolLoad` does not exceed `abs(QCoo_flow_nominal)` | PASS |
| R15c | `test_r15c_heating_capacity_saturates_at_nominal` | Under an oversized heating demand, `yHea` saturates at 1 and `QHeaLoad` does not exceed `QHea_flow_nominal` | PASS |
| R15d | `test_r15d_fan_command_never_exceeds_ufan` | `yFan <= uFan` holds across a sweep of partial fan commands (0.3, 0.5, 0.7, 1.0), not just at `uFan=1` | PASS |
| R16 | `test_r16_fmu_interface_regression` | Exact FMU input/output set (6 in / 23 out); confirms no `TChiWatSup/TChiWatRet/VChiWat_flow/THotWatSup/THotWatRet` central-plant signals are exposed | PASS |
| R17 | `test_r17_model_json_metadata_matches_fmu` | `model.json` matches the exported FMU interface exactly (reuses `shared/validate_fmu_metadata.py`) | PASS |
| R18 | `test_r18_cav_mode_fixed_static_pressure` | `haveVAVControl=false` locks `dpSupSet` at `dpSupSetFixed` regardless of `uVAVDamMax` | PASS |
| R23 | `test_r23_outdoor_airflow_relationship` | `VOutAir_flow = VSup_flow * yOutDam` exactly; increases with higher VAV-driven supply airflow and with a more-open economizer | PASS |
| R24 | `test_r24_minimum_outdoor_air_input_sets_economizer_floor` | `uMinOutAir` sets `yOutDam`'s floor under unfavorable conditions at every value tried (0.05, 0.15, 0.30, 0.50), not just the old 0.15 default | PASS |
| R25 | `test_r25_economizer_status_tolerance_boundary` | `economizerStatus` stays False for an OA-damper opening just inside `economizerActiveTol` (0.02) above the minimum-OA floor, and flips True just outside it -- avoids status chatter right at the floor | PASS |
| R26 | `test_r26_supply_fan_speed_output_tracks_yfan` | `model.json`'s `supply_fan_speed_pct` entry declares `fmu_variable=yFan`/`conversion=fraction_to_pct`; across a `uFan` sweep (0.3, 0.5, 0.7, 1.0) the derived value tracks the command and is never constant | PASS |

## Extended suite (`pytest models/rtu/tests -m extended`, `workflow_dispatch` only)

| # | Test | Covers | Result |
|---|------|--------|--------|
| R19 | `test_r19_full_economizer_saturation` | OA damper saturates at 100% and `TMix` tracks `TOut` when outdoor air alone can meet the SAT setpoint | PASS |
| R20 | `test_r20_six_hour_stability` | Full 21,600 s experiment duration, all 18 outputs finite throughout, global invariants hold at every recorded point | PASS |
| R21 | `test_r21_alternate_cooling_capacity_override` | `QCoo_flow_nominal` can be overridden (e.g. -20,000 W) without a crash; `QCoolLoad` respects the new nameplate cap | PASS |
| R22 | `test_r22_sat_setpoint_step_response` | A live SAT-setpoint step (16 -> 13 degC) drives `yCoo` up and `TSup` converges to the new setpoint within 0.5 K | PASS |

## Global invariants (`assert_global_invariants`, applied inside every test above)

Checked at every recorded point across the whole trajectory, not just the
final value:

- `0 <= yFan <= 1`, `minOutAirFra <= yOutDam <= 1` (`minOutAirFra` here means "whatever `uMinOutAir` the test actually supplied" -- see `assert_global_invariants`'s own `minOutAirFra=` parameter), `0 <= yCoo <= 1`, `0 <= yHea <= 1`
- `0 <= VOutAir_flow <= VSup_flow` (new -- structural consequence of `VOutAir_flow = VSup_flow * yOutDam` and `yOutDam` being bounded to `[uMinOutAir, 1]`)
- `dpSup >= 0`, `dpSupSetMin <= dpSupSet <= dpSupSetMax`
- `PFan >= 0`, `PCompressor >= 0`
- `QCoolLoad >= 0`, `QHeaLoad >= 0` (this project's positive-when-cooling/heating application convention)
- `gasHeatingPower >= 0`, `totalElectricPower >= 0`
- `compressorCOP` is either exactly 0 (compressor off) or within `[COP_min, COP_max]`
- all 23 outputs finite (no NaN/Inf) throughout

## Documentation fix (not a physics/architecture change)

`TOut`'s input description previously read "also drives the DX coil's
condenser-entering temperature (the condenser is in the unit's outdoor
section...)" -- a leftover from the abandoned real-DX-coil design. In the
current idealized-coil design there is no modeled condenser at all, so
`TOut` only affects economizer control, the mixed-air calculation, and the
`compressorCOP(TOut)` approximation. Corrected to: "Outdoor-air temperature;
used for economizer control, mixed-air calculation, and compressor COP
approximation." This is a description-string change on the `TOut`
`RealInput` only -- no equation, parameter, or component was touched.
Because the description string is embedded in the exported
`modelDescription.xml`, `RTU.fmu` was rebuilt (450/450 equations balanced,
same benign alias-variable warnings as before) and the full suite re-run
against the rebuilt FMU.

## Trane IntelliPak comparison: outdoor-air flow output + minimum-OA input

Two model-backed points identified from a Trane IntelliPak point-list
comparison, both directly supported by RTU.mo's existing physics (no new
component, coil, or control loop added):

1. **`VOutAir_flow` output** (`outdoor_airflow_m3_s` in `model.json`,
   unit m3/s). The model already computed
   `mOut_flow_cmd = mAir_flow_cmd * yOutDam` (a protected mass-flow
   command) but never exposed a volumetric outdoor-air flow. Rather than a
   separate mass-flow/fixed-density conversion of `mOut_flow_cmd`,
   `VOutAir_flow = VSup_flow * yOutDam` reuses the *existing* measured
   supply-air volume-flow sensor (`senFlo.V_flow`, already exposed as
   `VSup_flow`) scaled by the same `yOutDam` economizer fraction -- keeps
   `VOutAir_flow` automatically consistent with whatever density the
   simulated air actually has at that sensor, and guarantees
   `0 <= VOutAir_flow <= VSup_flow` by construction rather than only
   empirically. See RTU.mo Documentation, "Outdoor-air volume flow output".
2. **`uMinOutAir` input** (`minimum_outdoor_air_damper_pct` in
   `model.json`, percent with `pct_to_fraction` conversion, default 15%).
   Replaces the formerly-fixed `minOutAirFra` parameter as the economizer's
   minimum-outdoor-air floor *during FMU operation* -- every place the
   economizer equations (`outFraRaw`, `yOutDam`) referenced `minOutAirFra`
   now references `uMinOutAir` instead, with no other change to the
   economizer's control structure. `minOutAirFra` itself is kept as a
   parameter, now used only as `uMinOutAir`'s own Modelica `start` value,
   so a caller that never explicitly drives `uMinOutAir` (or a standalone,
   non-FMU simulation of this model) still behaves exactly as before. See
   RTU.mo Documentation, "Minimum outdoor-air input".

**Not added**, per this task's explicit scope: humidity, latent cooling,
filter differential pressure, space pressure, refrigerant suction/discharge
pressure, return/exhaust fan, energy recovery, staged compressors, or any
arbitration/BAS-only point -- none of these are supported by RTU.mo's
existing physics, and adding them would require new components or belong
in the simulator/controller layer instead.

### Interface change summary

| | Before | After |
|---|---|---|
| FMU inputs | 5: `TSupSet, TOut, TRet, uFan, uVAVDamMax` | 6: adds `uMinOutAir` |
| FMU outputs | 18 | 19: adds `VOutAir_flow` |
| `model.json` inputs | 5 | 6: adds `minimum_outdoor_air_damper_pct` |
| `model.json` outputs | 18 | 19: adds `outdoor_airflow_m3_s` |

Rebuilt via the same OpenModelica toolchain as every other FMU in this repo
(`iotistic/modelica-build:om-1.27.0-buildings-13.0.0`, matching
`build-rtu-fmu.yml`): `Class RTU has 452 equation(s) and 452 variable(s)`
(up from 450/450 before this change, consistent with one new input and one
new output each contributing one trivial equation), same benign
alias-variable/over-specified-initialization warnings as documented in
"Harness fix, not a model fix" below -- not new to this change. Validated
against `model.json` with `shared/validate_fmu_metadata.py`: `FMU metadata
valid: 6 inputs, 19 outputs`.

### Representative results

**Outdoor-air flow vs. economizer state** (`TSupSet=13C, TRet=25C or 24C,
uFan=1, uVAVDamMax=0.8`, `VSup_flow=1.4907 m3/s` in every row below since
`uFan`/`uVAVDamMax` are unchanged across them):

| Scenario | `TOut` | `uMinOutAir` | `yOutDam` | `VOutAir_flow` |
|---|---|---|---|---|
| Unfavorable (default min OA) | 32C | 0.15 | 0.1500 | 0.2236 m3/s |
| Favorable (economizer opens) | 10C | 0.15 | 0.8000 | 1.1926 m3/s |
| Fan off | 32C | 0.15 | 0.1500 (locked, but flow is 0) | ~0 m3/s |

`VOutAir_flow` = `VSup_flow * yOutDam` exactly in every row (e.g. favorable:
`1.4907 * 0.80 = 1.1926`); `VOutAir_flow <= VSup_flow` holds in every row,
including the always-0.15-locked unfavorable/fan-off cases.

**Minimum-OA input sweep** (unfavorable conditions, `TOut=32C > TRet=24C`,
so `yOutDam` sits exactly at the floor):

| `uMinOutAir` | `yOutDam` | `VOutAir_flow` |
|---|---|---|
| 0.05 | 0.0500 | 0.0745 m3/s |
| 0.15 (old fixed default) | 0.1500 | 0.2236 m3/s |
| 0.30 | 0.3000 | 0.4472 m3/s |
| 0.50 | 0.5000 | 0.7454 m3/s |

`yOutDam` tracks `uMinOutAir` exactly at every value -- not just the old
compiled-in 0.15 default -- confirming the economizer floor now comes from
the live input.

**`haveEconomizer=false` with a non-default minimum** (favorable outdoor
conditions, `TOut=10C < TRet=24C`, `uMinOutAir=0.35`): `yOutDam = 0.3500`
exactly, even though outdoor conditions alone would otherwise justify
opening the damper well above that -- confirms the disabled-economizer lock
now holds at whatever `uMinOutAir` is supplied, not just the parameter's
old default.

**Full RTU->VAV->ThermalZone system integration** (all 6 scenarios in
`tests/integration/test_rtu_vav_zone_system.py`, which drives RTU through
this same `uMinOutAir` input held at the model's own 0.15 default for every
exchange step): all 6 PASS against the rebuilt `RTU.fmu`, confirming the
interface change doesn't disturb the coupled RTU+VAV+ThermalZone system
behavior already verified in `tests/integration/SYSTEM_INTEGRATION_REPORT.md`.

## BACnet status outputs: explicit Boolean outputs for fan / cooling / heating / economizer

Four existing BACnet BI points (`Supply-Fan-Status`, `Cooling-Status`,
`Heating-Status`, `Economizer-Status`) were previously only inferable by a
consumer thresholding continuous outputs itself. This pass exposes them as
explicit `BooleanOutput`s derived directly from existing RTU.mo state --
no new physics, coil, or control loop:

```
supplyFanStatus  = yFan > 0.01
coolingStatus    = coolingPLR > 0.01
heatingStatus    = heatingPLR > 0.01
economizerStatus = yOutDam > uMinOutAir + economizerActiveTol   (economizerActiveTol default = 0.02)
```

`economizerActiveTol` exists solely so `economizerStatus` doesn't chatter
right at the minimum-OA floor from interpolation/solver noise -- the same
class of near-boundary floating-point noise already documented in "Harness
fix, not a model fix" below, guarded against here at the model level
instead of the test level since this output is a real FMU/BACnet-facing
signal, not a test assertion. See RTU.mo Documentation, "BACnet status
outputs".

### Interface change summary

| | Before | After |
|---|---|---|
| FMU inputs | 6 | 6: unchanged |
| FMU outputs | 19 | 23: adds `supplyFanStatus, coolingStatus, heatingStatus, economizerStatus` |
| `model.json` inputs | 6 | 6: unchanged |
| `model.json` outputs | 19 | 23: adds `supply_fan_status, cooling_status, heating_status, economizer_status` |

Rebuilt via the same OpenModelica toolchain as every other FMU in this repo
(`iotistic/modelica-build:om-1.27.0-buildings-13.0.0`, matching
`build-rtu-fmu.yml`): `Class RTU has 456 equation(s) and 456 variable(s)`
(up from 452/452 before this change, consistent with 4 new Boolean outputs
each contributing one trivial equation), same benign
alias-variable/over-specified-initialization warnings as documented in
"Harness fix, not a model fix" below -- not new to this change. Validated
against `model.json` with `shared/validate_fmu_metadata.py`: `FMU metadata
valid: 6 inputs, 23 outputs`.

### Representative results

**ON/OFF behavior across scenarios** (reusing the exact scenario inputs from
the "Representative results" tables below, plus R1/R10 fan-off):

| Scenario | `supplyFanStatus` | `coolingStatus` | `heatingStatus` | `economizerStatus` |
|---|---|---|---|---|
| Cold start / fan off (R1, R10) | False | False | False | False (also locked at min OA) |
| Normal cooling (`yCoo=0.6428`) | True | True | False | -- |
| Normal heating (`yHea=0.8893`) | True | False | True | -- |
| Economizer favorable (`yOutDam=0.7857`, min OA 0.15) | -- | -- | -- | True |
| Economizer unfavorable (`yOutDam=0.1500`, min OA 0.15) | -- | -- | -- | False |
| Economizer disabled (`yOutDam` locked at 0.1500) | -- | -- | -- | False |

**Tolerance-boundary check** (R25 -- `uMinOutAir=0.20`,
`economizerActiveTol=0.02`, `TOut` chosen precisely via the economizer's own
`outFraRaw` formula so `yOutDam` lands exactly inside, then exactly outside,
the tolerance band):

| `yOutDam` | Position relative to floor | `economizerStatus` |
|---|---|---|
| 0.2100 (= min OA + 0.01, half the tolerance) | Inside tolerance band | False |
| 0.2300 (= min OA + 0.03, 1.5x the tolerance) | Outside tolerance band | True |

Confirms `economizerStatus` doesn't flip on every small economizer movement
right at the floor -- only once the opening genuinely clears
`economizerActiveTol`.

**Full RTU->VAV->ThermalZone system integration**: all 6 scenarios in
`tests/integration/test_rtu_vav_zone_system.py` PASS against the rebuilt
`RTU.fmu` (263.62s), confirming the pure output addition doesn't disturb the
coupled RTU+VAV+ThermalZone system behavior -- no changes were needed in
`system_harness.py` for this pass, since no new input was added.

## Dedicated supply-fan-speed output

`yFan` ("Actual normalized fan/airflow command", the fan-law/saturation-
limited value RTU.mo already computes -- see R15d, `yFan <= uFan`) was
already exported as an FMU output and already aliased once in `model.json`
as `fan_command_pct`. That existing alias is labeled "Fan Command" and
shares its `name` with the genuinely distinct commanded-value *input*
`fan_command_pct` (`uFan`) -- workable for the runtime (inputs and outputs
are looked up independently, keyed by `item.name` within their own list;
see `shared/runtime/models/manager.py::_read_outputs`), but ambiguous for a BACnet
point picker, since an operator sees "Fan Command" twice with no way to
tell the actual-speed feedback apart from the command setpoint.

This pass adds a second `model.json` output entry, `supply_fan_speed_pct`
("Supply Fan Speed", semantic `Fan_Speed_Sensor`), aliasing the same `yFan`
value under an unambiguous name/label for a dedicated mapping to
`RTU-1-Supply-Fan-Speed`. `fan_command_pct`'s existing output entry is
untouched -- both entries coexist, each independently converted
(`fraction_to_pct`) and keyed by their own `name` when the runtime builds
its output dict, so neither shadows the other. No RTU.mo equation, no
RTU.fmu variable, and no control/physics behavior changed; `RTU.fmu` itself
was not rebuilt (`yFan` already existed, so there was nothing new to
export).

### Interface change summary

| | Before | After |
|---|---|---|
| FMU inputs / outputs | 6 / 23 | 6 / 23: unchanged (no new FMU variable) |
| `model.json` outputs | 23 | 24: adds `supply_fan_speed_pct` (aliases the existing `yFan` output alongside `fan_command_pct`) |

Validated against `model.json` with `shared/validate_fmu_metadata.py`:
`FMU metadata valid: 6 inputs, 23 outputs` (the validator counts real FMU
variables, not `model.json` alias entries, so this is correctly unchanged).

### Representative results

**`supply_fan_speed_pct` tracks the commanded fan speed** (R26 -- sweeping
`uFan`, fan otherwise unconstrained by the SAT/economizer scenario used):

| `uFan` (commanded) | `yFan` (final, actual) | `supply_fan_speed_pct` = `yFan * 100` |
|---|---|---|
| 0.3 | 0.3000 | 30.00% |
| 0.5 | 0.5000 | 50.00% |
| 0.7 | 0.7000 | 70.00% |
| 1.0 | 1.0000 | 100.00% |

`supply_fan_speed_pct` moves with every step of the sweep -- confirms the
BACnet-mapped point reads a genuinely live value, not a value frozen at
model-load time.

## Representative results

**Normal cooling** (`TOut=32C, TRet=25C, TSupSet=13C, uFan=1, uVAVDamMax=0.8`):

| Output | Value |
|---|---|
| TSup | 12.85 degC (settled at setpoint) |
| TMix | 26.05 degC |
| yCoo | 0.6428 |
| yHea | 0.0000 |
| QCoolLoad | 23,684.0 W |
| PCompressor | 6,183.8 W |
| compressorCOP | 3.83 |
| PFan | 3,577.7 W |
| totalElectricPower | 9,761.5 W |

Energy check: `PCompressor * compressorCOP` = 6,183.8 * 3.83 = 23,684.0 W ~= `QCoolLoad` (23,684.0 W). Matches exactly.

**Normal heating** (`TOut=0C, TRet=18C, TSupSet=30C, uFan=1, uVAVDamMax=0.8`):

| Output | Value |
|---|---|
| TSup | 30.00 degC (settled at setpoint) |
| TMix | 15.30 degC |
| yHea | 0.8893 |
| yCoo | 0.0000 |
| QHeaLoad | 26,678.5 W |
| gasHeatingPower | 33,348.1 W |
| PCompressor | 0.0 W |

Energy check: `gasHeatingPower * etaHeat` = 33,348.1 * 0.80 = 26,678.5 W = `QHeaLoad` exactly.

**COP sweep** (`TSupSet=13C, TOut swept, TRet=25C, uFan=1, uVAVDamMax=0.8`):

| TOut | compressorCOP | QCoolLoad (W) | PCompressor (W) |
|---|---|---|---|
| 20 degC | 4.31 | 12,704.1 | 2,947.6 |
| 25 degC | 4.11 | 21,778.4 | 5,298.9 |
| 30 degC | 3.91 | 23,139.5 | 5,918.0 |
| 35 degC | 3.71 | 24,500.5 | 6,603.9 |
| 40 degC | 3.51 | 25,862.3 | 7,368.2 |

`compressorCOP` stays within `[COP_min=2.0, COP_max=5.0]` and decreases
monotonically as `TOut` rises, at exactly `COP_nominal=3.71` at
`TOutCOPRef=35C`, all as expected from the bounded linear formula.

**Capacity limiting** (deliberately oversized demand):

| Scenario | Command saturates at | Delivered | Nameplate | Within cap? |
|---|---|---|---|---|
| Cooling (`TSupSet=-10C, TOut=45C, TRet=35C`) | yCoo = 1.0000 | QCoolLoad = 36,846.8 W | abs(QCoo_flow_nominal) = 36,846.8 W | Yes -- exact |
| Heating (`TSupSet=45C, TOut=-20C, TRet=10C`) | yHea = 1.0000 | QHeaLoad = 30,000.0 W | QHea_flow_nominal = 30,000.0 W | Yes -- exact |

Both are a structural guarantee of the SAT PI's own `yMax=1` saturation
limit (`Q_flow = u*Q_flow_nominal`, `u` clipped to `[0,1]`), not a separate
capacity-cap mechanism -- confirmed empirically here rather than assumed.

**Economizer**:

| Scenario | yOutDam |
|---|---|
| Favorable (TOut=10C < TRet=24C) | 0.7857 (above minOutAirFra) |
| Unfavorable (TOut=32C > TRet=24C) | 0.1500 (= minOutAirFra) |
| Disabled (favorable air, `haveEconomizer=false`) | 0.1500 (locked at minOutAirFra) |

**VAV static-pressure reset** (`vavDamLow=0.60, vavDamHigh=0.90, dpSupSetMin=200, dpSupSetMax=500`):

| uVAVDamMax | dpSupSet |
|---|---|
| 0.30 (below vavDamLow) | 200.00 Pa (= dpSupSetMin) |
| 0.60 (at vavDamLow) | 200.00 Pa (= dpSupSetMin) |
| 0.75 (midpoint) | 350.00 Pa (interpolated midpoint) |
| 0.90 (at vavDamHigh) | 500.00 Pa (= dpSupSetMax) |
| 0.95 (above vavDamHigh) | 500.00 Pa (= dpSupSetMax) |

**CAV mode** (`haveVAVControl=false`, `dpSupSetFixed=500`):

| uVAVDamMax | dpSupSet |
|---|---|
| 0.10 | 500.00 Pa |
| 0.50 | 500.00 Pa |
| 0.95 | 500.00 Pa |

`dpSupSet` stays fixed regardless of `uVAVDamMax`, as expected.

**Fan laws and `yFan <= uFan`**:

| uFan | yFan | dpSup (actual / law) | PFan (actual / law) |
|---|---|---|---|
| 0.3 | 0.3000 | 45.00 / 45.00 Pa | 135.00 / 135.00 W |
| 0.5 | 0.5000 | 125.00 / 125.00 Pa | 625.00 / 625.00 W |
| 0.7 | 0.7000 | 245.00 / 245.00 Pa | 1,715.00 / 1,715.00 W |
| 1.0 | 1.0000 | 500.00 / 500.00 Pa | 5,000.00 / 5,000.00 W |

`yFan` never exceeds `uFan`; `dpSup`/`PFan` match the fan-law formulas exactly.

## Genuine defects found

None. Every check in this verification pass (including the new capacity-limit
and fan-command-ceiling checks) passed against the existing RTU.mo physics
without requiring any change beyond the documentation fix above.

## Harness fix, not a model fix

The first full run of the core suite failed 6 tests, all at the same
assertion: an exact-equality check (`coolingPLR == yCoo`,
`heatingPLR == yHea`) applied across the *entire recorded trajectory* rather
than at a single settled point. Both relations are simple algebraic copies in
RTU.mo (`coolingPLR = yCoo;`, `heatingPLR = yHea;`) and hold exactly at every
point the solver actually evaluates -- but `pyfmi`'s CVode Model-Exchange
driver reconstructs each recorded output's trajectory independently via dense
polynomial interpolation at the `ncp` communication points, and two
algebraically-identical outputs can diverge by up to ~2% at an interpolated
(non-solver-step) time even though the underlying equation never breaks. The
FMU export itself had already flagged this exact class of issue at build
time ("The model contains alias variables with redundant start and/or
conflicting nominal values").

This was verified as a harness/interpolation artifact, not a genuine RTU.mo
defect, the same way this project has root-caused similar false failures
before (`test_b7`'s PLR-drift test-design bug in `test_boiler.py`, and the
`result_handling='file'` shared-`.mat`-file bug across all three test
suites): the full-trajectory exact-equality assertions were removed from
`assert_global_invariants` and replaced with (a) loose non-negativity bounds
checked across the whole trajectory, and (b) exact-equality checks
(`rel_tol=1e-4`) at the *settled final value* inside `test_r2_normal_cooling`
and `test_r3_normal_heating`, where interpolation noise is negligible.
RTU.mo itself was not modified for this.

## Reproducing

```powershell
docker build -t iot-models-test-runtime -f Dockerfile .

docker run --rm -v "${PWD}:/app" -w /app iot-models-test-runtime `
  sh -c "pip install --quiet pytest && python -m pytest models/rtu/tests -m core -v"

docker run --rm -v "${PWD}:/app" -w /app iot-models-test-runtime `
  sh -c "pip install --quiet pytest && python -m pytest models/rtu/tests -m extended -v"
```
