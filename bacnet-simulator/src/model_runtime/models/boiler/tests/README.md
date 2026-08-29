# Boiler Plant Regression Suite

## Purpose

`test_boiler.py` protects the behavior of `models/boiler/BoilerPlant.mo` --
a central two-boiler hot-water plant with condensing-capable efficiency,
staging/load sharing, outdoor-air supply-water reset, pump interlock, and
capacity-limited delivered heating. It's the heating-side counterpart to
`models/ahu/AHU.mo`, completing this project's central-plant architecture:

```
SimpleChillerPlant -> AHU -> VAV -> ThermalZone   (cooling)
BoilerPlant          -> AHU -> VAV -> ThermalZone   (heating, this model)
```

This suite follows the exact conventions established in
`models/ahu/tests/test_ahu.py` -- `pyfmi` in Model Exchange mode,
`result_handling='memory'`, `core`/`extended` pytest markers, the same
helper-function shapes (`load_*_fmu`, `simulate_*`, `get_final_value`,
`assert_close`, `assert_global_invariants`). One addition was required by
BoilerPlant's own interface, not AHU's: see "Boolean inputs" below.

## Dependencies

Same as `models/ahu/tests/README.md` -- `AHU.fmu`'s dependency notes on
`pyfmi` needing a compiled Sundials/Assimulo stack (not a plain
`pip install pyfmi`) and reusing the repo's own runtime Docker image apply
identically here. Build `BoilerPlant.fmu` from `BoilerPlant.mo` via the
repository's normal OpenModelica 1.27 / Buildings 13.0.0 build
(`shared/build-fmu.sh`) before running these tests.

## Running core tests locally

From the repository root:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/boiler/BoilerPlant.mo /build/BoilerPlant.mo && MODELICA_FILE=BoilerPlant.mo MODELICA_MODEL=BoilerPlant FMU_OUTPUT_NAME=BoilerPlant.fmu sh shared/build-fmu.sh && cp /fmu-out/BoilerPlant.fmu models/boiler/BoilerPlant.fmu"

docker build -t iot-models-runtime:local .

docker run --rm -v ${PWD}:/app -w /app iot-models-runtime:local `
  sh -c "pip install --quiet pytest && python -m pytest models/boiler/tests -m core -v"
```

## Running extended tests

Same, with `-m extended` in place of `-m core`. Extended tests include the
two-boiler load-sharing check, outdoor-reset and condensing-efficiency
sweeps, a boiler-staging transition, step-response tests, and a six-hour
stability run.

## Expected FMU location

`models/boiler/BoilerPlant.fmu`, resolved relative to this file
(`Path(__file__).parent.parent`). Tests that need it call `pytest.skip()`
if it's missing rather than erroring.

## Model Exchange, not Co-Simulation

Every test loads the FMU with `pyfmi.load_fmu(path, kind="ME")` and drives
it with `fmu.simulate(input=(names, trajectory), options=...)` -- the same
calling convention `shared/runtime/models/manager.py` uses in production.

## Boolean inputs: a real FMI2 constraint, not a pyfmi quirk

Unlike `AHU.fmu` (all-Real interface), `BoilerPlant.fmu` has four Boolean
inputs (`uBoi1`, `uBoi2`, `uPum1`, `uPum2`). FMI2 does not allow driving
non-Real variables through the continuous `simulate(input=...)` trajectory
once the model is past initialization -- `fmi2SetBoolean` is only legal
before entering continuous-time mode. `shared/runtime/models/manager.py` already
handles this correctly
(`_split_input_variables`/`_apply_discrete_inputs`): Real inputs ride the
trajectory, Booleans are set once via `fmu.set()` before `simulate()` and
held constant for the run.

`simulate_boiler()` in this suite follows the identical split
(`REAL_NAMES`/`BOOL_NAMES`). Every test in this suite holds its Boolean
inputs constant for the whole simulated run -- none step a Boolean
mid-simulation. `test_b14_boiler_staging_transition` (a boiler-enable step)
works around this by running two separate `simulate()` calls with different
Boolean settings rather than one continuous run with a Boolean step, which
is the correct way to approximate a discrete-input step under this FMI2
rule (not a limitation of this suite's own design).

## Why `result_handling='memory'`

Same reason as `models/ahu/tests/README.md`: `pyfmi`'s default
`result_handling='file'` writes each simulation's trajectory to a shared,
model-name-derived `.mat` file, and running the same model repeatedly in
one process silently overwrites earlier runs' results before they're read
back. Every `simulate_boiler()` call sets `result_handling='memory'`
explicitly. Do not remove it.

## Known model behaviors this suite protects (not bugs)

### Capacity limiting and the condensing bonus

`PartialBoiler` normalizes fuel consumption by `eta_nominal`, so when the
condensing bonus pushes the live `eta` above `eta_nominal` (cool return
water), firing at full rate could otherwise deliver more heat than a
boiler's nameplate `QBoi_nominal` -- physically real for a condensing
boiler, but it would break the plant-level capacity cap this model must
respect. `BoilerPlant.mo` computes a firing-rate ceiling (`yCap`) from
`THotWatRet` alone (no algebraic loop) to keep delivered heat bounded near
nameplate capacity even under favorable return conditions, while leaving
firing unrestricted under normal (non-condensing) operation. See
`BoilerPlant.mo`'s own `Documentation` annotation, section "Capacity
limiting and the condensing bonus", for the exact formula.

`test_b4_capacity_limited` asserts `QHeatDelivered` doesn't meaningfully
exceed `availableHeatingCapacity` (2% tolerance) under a deliberately
oversized demand scenario -- this was a real, empirically-found issue
during development (an 11% overshoot before `yCap` was added), not a
theoretical concern.

### "At comparable PLR" for the condensing-efficiency test

`test_b7_condensing_efficiency_vs_return_temp` deliberately disables
outdoor reset and forces a high, fixed supply setpoint with a large flow
so that required heating vastly exceeds single-boiler capacity at every
return temperature in the sweep. A naive sweep that instead lets outdoor
reset drive the setpoint (so required delta-T shrinks as return temperature
approaches the setpoint ceiling) confounds the temperature effect on
efficiency with a part-load effect, and does NOT reliably produce a
monotonic result -- this was found empirically (the first version of this
test failed) even though the underlying efficiency formula is correctly
monotonic in return temperature at a genuinely fixed part-load ratio.

### Boiler-off, pump-on: `THotWatSup` approaches `THotWatRet` directly

Unlike `AHU.mo`'s documented zero-CHW-flow `TChiWatRet` staleness (see
`models/ahu/tests/README.md`), `BoilerPlant.mo` routes flow through both
boiler legs (at zero firing) whenever a pump runs but no boiler is enabled,
so water actively passes through the plant rather than stagnating.
`test_b13_boiler_off_pump_on` asserts `THotWatSup` approaches `THotWatRet`
directly (0.5 K tolerance) -- this is expected to hold exactly, not a
documented limitation to work around.

## What CI runs automatically

`.github/workflows/build-boiler-fmu.yml` builds `BoilerPlant.fmu` on any
push touching `BoilerPlant.mo`, validates/syncs `model.json` against it,
then runs `pytest models/boiler/tests -m core` inside the repo's own
runtime image before committing the rebuilt FMU + metadata back -- a core
regression failure blocks the auto-commit. Extended tests run only via
`workflow_dispatch` with `run_extended: true`.

## Interpreting failures

Same guidance as `models/ahu/tests/README.md`: a failure means a model
regression, build/toolchain drift, or a genuinely new issue to document --
not something to fix by loosening the assertion.
