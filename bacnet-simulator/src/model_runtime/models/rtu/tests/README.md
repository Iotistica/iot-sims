# RTU Regression Suite

## Purpose

`test_rtu.py` protects the behavior of `models/rtu/RTU.mo` -- a standalone
packaged rooftop unit (DX cooling, gas heat, dry-bulb economizer, VAV-capable
supply fan) serving downstream VAV terminals or thermal zones directly,
without a central chiller/boiler plant:

```
Weather -> RTU -> VAV -> ThermalZone
```

This completes a second, standalone architecture alongside this repo's
existing central-plant one:

```
SimpleChillerPlant/BoilerPlant -> AHU -> VAV -> ThermalZone
```

This suite follows the exact conventions established in
`models/ahu/tests/test_ahu.py` and `models/boiler/tests/test_boiler.py` --
`pyfmi` in Model Exchange mode, `result_handling='memory'`, `core`/`extended`
pytest markers, the same helper-function shapes (`load_*_fmu`, `simulate_*`,
`get_final_value`, `assert_close`, `assert_global_invariants`). RTU has only
Real inputs (`TSupSet`, `TOut`, `TRet`, `uFan`, `uVAVDamMax`) -- no Booleans
-- so `simulate_rtu()` is simpler than `test_boiler.py`'s Real/Boolean split;
every input rides the continuous `simulate(input=...)` trajectory directly.

## Dependencies

Same as `models/ahu/tests/README.md` and `models/boiler/tests/README.md` --
`pyfmi` needs a compiled Sundials/Assimulo stack (not a plain
`pip install pyfmi`), so these tests reuse the repo's own runtime Docker
image (`Dockerfile` at the repo root, built via `docker build .`). Build
`RTU.fmu` from `RTU.mo` via the repository's normal OpenModelica 1.27 /
Buildings 13.0.0 build (`shared/build-fmu.sh`) before running these tests.

## Running core tests locally

From the repository root:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/rtu/RTU.mo /build/RTU.mo && MODELICA_FILE=RTU.mo MODELICA_MODEL=RTU FMU_OUTPUT_NAME=RTU.fmu sh shared/build-fmu.sh && cp /fmu-out/RTU.fmu models/rtu/RTU.fmu"

docker build -t iot-models-runtime:local .

docker run --rm -v ${PWD}:/app -w /app iot-models-runtime:local `
  sh -c "pip install --quiet pytest && python -m pytest models/rtu/tests -m core -v"
```

## Running extended tests

Same, with `-m extended` in place of `-m core`. Extended tests include a
full-economizer saturation check, a six-hour stability run, an alternate
cooling-capacity parameter override, and a SAT-setpoint step-response test.

## Expected FMU location

`models/rtu/RTU.fmu`, resolved relative to this file
(`Path(__file__).parent.parent`). Tests that need it call `pytest.skip()` if
it's missing rather than erroring.

## Model Exchange, not Co-Simulation

Every test loads the FMU with `pyfmi.load_fmu(path, kind="ME")` and drives it
with `fmu.simulate(input=(names, trajectory), options=...)` -- the same
calling convention `shared/runtime/models/manager.py` uses in production.

## Why `result_handling='memory'`

Same reason as `models/ahu/tests/README.md` and
`models/boiler/tests/README.md`: `pyfmi`'s default `result_handling='file'`
writes each simulation's trajectory to a shared, model-name-derived `.mat`
file, and running the same model repeatedly in one process silently
overwrites earlier runs' results before they're read back. Every
`simulate_rtu()` call sets `result_handling='memory'` explicitly. Do not
remove it.

## Known model behaviors this suite protects (not bugs)

### DX cooling is an idealized coil, not a real refrigerant-circuit component

RTU.mo does NOT use `Buildings.Fluid.DXSystems.Cooling.AirSource.SingleSpeed`.
That real DX coil was built and empirically tested first, and rejected after
two genuine, separately verified fix attempts both failed to resolve a
segfault at `uFan=0` (cold start, the first scenario every model in this
repo's own test suites exercises) -- see `RTU.mo`'s own `Documentation`
annotation, section "DX cooling component selection", for the full
investigation (both attempted fixes, the exact failure modes, and why each
was reverted). Cooling instead uses
`Buildings.Fluid.HeatExchangers.HeaterCooler_u` (the same idealized component
this model's own gas-heating section uses, and the one
`models/ahu-simple/SimpleAHU.mo` already used successfully for its own
cooling coil), with compressor power derived algebraically as
`PCompressor = QCoolLoad / compressorCOP`, where `compressorCOP` is a
bounded linear function of `TOut`. `test_r1_cold_start_unit_off` and
`test_r20_six_hour_stability` are the tests that specifically protect this
-- neither would have passed against the abandoned real DX coil.

There is no latent-cooling output and no `computeReevaporation`/
`use_mCon_flow` configuration to speak of -- those belonged only to the
abandoned DX coil and don't apply to the idealized replacement. This is not
a gap being worked around; see `RTU.mo`'s "Known simplifications" section.

### `coolingPLR`/`heatingPLR`/`totalElectricPower` are exact only at settled points, not across a full interpolated trajectory

`coolingPLR = yCoo`, `heatingPLR = yHea`, and
`totalElectricPower = PCompressor + PFan` are simple algebraic copies in
RTU.mo, exact at every point the solver actually evaluates. But `pyfmi`'s
CVode Model-Exchange driver reconstructs each recorded output's trajectory
independently via dense polynomial interpolation between communication
points, and two algebraically-identical outputs can diverge by up to ~2% at
an interpolated (non-solver-step) time -- confirmed as the cause of the
FMU build's own "alias variables with redundant start and/or conflicting
nominal values" warning, and empirically reproduced during this suite's
first run (6 failing tests, all at the same full-trajectory equality
assertion). `assert_global_invariants` only checks non-negativity bounds for
these three across the full trajectory; the exact-equality relations are
checked at settled final values instead, inside `test_r2_normal_cooling` and
`test_r3_normal_heating` (and `test_r13`/`test_r14`/`test_r15` for the
`PCompressor`/`gasHeatingPower`/`totalElectricPower` derivations). See
`TEST_REPORT.md`, "Harness fix, not a model fix", for the full account.
`RTU.mo` itself was not modified for this.

### No central-plant inputs, by design

RTU does not expose `TChiWatSup`, `TChiWatRet`, `VChiWat_flow`,
`THotWatSup`, or `THotWatRet` -- it has no chilled-water or hot-water loop to
interface with, unlike `AHU.mo`/`BoilerPlant.mo`. `test_r16_fmu_interface_regression`
asserts these are absent from both the FMU's inputs and outputs, not just
that the expected set is present.

## What CI runs automatically

`.github/workflows/build-rtu-fmu.yml` builds `RTU.fmu` on any push touching
`RTU.mo`, validates/syncs `model.json` against it, then runs
`pytest models/rtu/tests -m core` inside the repo's own runtime image before
committing the rebuilt FMU + metadata back -- a core regression failure
blocks the auto-commit. Extended tests run only via `workflow_dispatch` with
`run_extended: true`.

## Interpreting failures

Same guidance as `models/ahu/tests/README.md` and
`models/boiler/tests/README.md`: a failure means a model regression,
build/toolchain drift, or a genuinely new issue to document -- not something
to fix by loosening the assertion.
