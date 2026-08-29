# AHU Regression Suite

## Purpose

`test_ahu.py` protects the behavior of `models/ahu/AHU.mo` (the experimental,
physically-coupled-CHW-coil successor to `SimpleAHU.mo`) against regressions
in the model, the OpenModelica/Buildings build toolchain, or the FMU
interface the simulator depends on.

It's a direct port of the scratch `pyfmi` harness used to produce the
40-test verification report in `models/ahu/tests/readme` — same simulation
calls, same assertions, turned into permanent `pytest` tests instead of a
one-off script. It does not add new physics assertions and does not loosen
any that the verification run already passed.

## Dependencies

- `AHU.fmu`, built from `AHU.mo` via the repository's normal
  OpenModelica 1.27 / Buildings 13.0.0 build (`shared/build-fmu.sh`).
- Python: `pytest`, `pyfmi`, `numpy`.

`pyfmi` needs a compiled Sundials/Assimulo stack; it is **not** a plain
`pip install pyfmi` on a bare runner. This repo already has a working
installation in the top-level `Dockerfile` (conda: `pyfmi fastapi uvicorn
pydantic requests numpy`) — the same image `docker-compose.yaml`'s
`fmu-runtime` service runs in production. Both local runs and CI reuse that
image rather than installing `pyfmi` a second, different way.

## Running core tests locally

Build the FMU, then run the suite inside the repo's own runtime image
(from the repository root):

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/ahu/AHU.mo /build/AHU.mo && MODELICA_FILE=AHU.mo MODELICA_MODEL=AHU FMU_OUTPUT_NAME=AHU.fmu sh shared/build-fmu.sh && cp /fmu-out/AHU.fmu models/ahu/AHU.fmu"

docker build -t iot-models-runtime:local .

docker run --rm -v ${PWD}:/app -w /app iot-models-runtime:local `
  sh -c "pip install --quiet pytest && python -m pytest models/ahu/tests -m core -v"
```

## Running extended tests

Same, with `-m extended` in place of `-m core`. Extended tests include a
six-hour stability run, a warm-CHWS sweep, a full static-pressure-reset
sweep, alternate-sizing parameter checks, step-response tests, and a
regression comparison against `SimpleAHU.fmu`. They're slower and not run
on every push — see CI below.

Run everything (both markers) with no `-m` filter at all:

```
python -m pytest models/ahu/tests -v
```

## Expected FMU location

`models/ahu/AHU.fmu`, resolved relative to this file
(`Path(__file__).parent.parent`), not the working directory. If the FMU is
missing, tests that need it call `pytest.skip()` rather than erroring, so a
local `pytest` run against a stale checkout fails loudly as "skipped, FMU
not found" instead of a confusing collection error.

## Model Exchange, not Co-Simulation

Every test loads the FMU with `pyfmi.load_fmu(path, kind="ME")` and drives
it with `fmu.simulate(input=(names, trajectory), options=...)` — the same
calling convention `shared/runtime/models/manager.py` uses in production (it also
loads `kind="ME"` and replays a full input-history trajectory on every
step). An earlier ad hoc smoke test using `fmpy`'s Co-Simulation `doStep()`
loop appeared to crash the model; that traced back to the co-simulation
harness itself, not the model — it reproduced identically against the
already-shipping `SimpleAHU.fmu`. Testing in the same mode production uses
avoided chasing a phantom bug, so this suite does not use Co-Simulation.

## Why `result_handling='memory'`

`pyfmi`'s default `result_handling='file'` writes each simulation's
trajectory to a shared, model-name-derived `.mat` file on disk. Running the
same model repeatedly in one process — which this suite does, several dozen
times per run — means a later simulation silently overwrites an earlier
one's result file before the earlier `ResultObject` is read back, raising
`"the result file has been modified since the result object was created"`.
This produced five false failures during the original verification pass,
fixed by switching to `result_handling='memory'` with no change to the
model. **Do not switch this back to `'file'`** — every `simulate_ahu()` call
sets it explicitly for exactly this reason.

## Known behavior: `TChiWatRet` at zero CHW flow

When the cooling valve is fully closed (`yCooVal ≈ 0`, `VChiWat_flow ≈ 0`),
`TChiWatRet` does **not** decay toward `TChiWatSup`. It holds at whatever the
stagnant water medium's default state happens to be (~20°C —
`Buildings.Media.Water`'s own `T_default`) because there's no flow to carry
the 7°C supply temperature through the coil. `VChiWat_flow` and `QCoolLoad`
both correctly read ~0 independently, so nothing that uses those signals
together should be misled — but a dashboard showing `TChiWatRet` in
isolation while the coil is idle would show a stale, physically meaningless
number.

`test_cooling_demand_removed` asserts `yCooVal`, `VChiWat_flow`, and
`QCoolLoad` all approach zero, and deliberately does **not** assert anything
about `TChiWatRet` when flow is below `CHW_FLOW_EPS` (1e-6 m³/s). This is a
verified model behavior, not a test gap — do not "fix" it by asserting
`TChiWatRet ≈ TChiWatSup` without first changing `AHU.mo`'s cooling section,
which is out of scope for this test-infrastructure task.

## Known behavior: `mAir_flow_nominal` FMU override

Setting `mAir_flow_nominal` to anything other than its compiled default
(2.0 kg/s) through the *already-exported* FMU's parameter-set interface
fails OpenModelica's initialization — even a 5% change to 1.9 fails
identically with a division-by-zero on `cooCoi.hA.hA_1`. The structurally
similar `VChiWat_flow_nominal` and `QCoo_flow_nominal` override cleanly, so
this looks like an OpenModelica FMU-export limitation specific to how
`mAir_flow_nominal` fans out into more components' sizing chains (`mix`,
`senTMix`, `senTSup`, `senFlo`, `heaCoi`, and `cooCoi`), not a physical sizing
problem.

`test_mair_flow_nominal_override_is_a_known_limitation` asserts this
*documented failure mode* (`pytest.raises`) rather than skipping it, so the
test itself would fail loudly — which is the point — if a future
OpenModelica/Buildings upgrade changes this behavior either way. Normal CI
never depends on overriding this parameter; the model is always exercised
at its compiled default.

## Known behavior: hot "chilled" water

If `TChiWatSup` is warmer than the entering air, the physically coupled
coil can transfer heat *from* the water *into* the air, and `QCoolLoad` can
go negative — the coil inadvertently heats instead of cools.
`cooCapacityFactor` correctly clamps to 0 in this condition, but it's
diagnostic-only and doesn't gate the valve, so this is real, physically
consistent heat-exchanger behavior, not a bug. `test_chws_above_entering_air_temperature`
documents it (no crash, no absurd magnitude) without asserting a sign on
`QCoolLoad`. A CHW-availability interlock (disable cooling when CHWS isn't
sufficiently below entering-air temperature) is a plausible future control
enhancement, out of scope here.

## What CI runs automatically

`.github/workflows/build-ahu-v2-fmu.yml` builds `AHU.fmu` on any push
touching `AHU.mo`, then runs `pytest models/ahu/tests -m core` inside the
repo's own runtime image before committing the rebuilt FMU back — a core
regression failure blocks the auto-commit. Extended tests run only via
`workflow_dispatch` with `run_extended: true`, since a six-hour-simulation
sweep and a full parameter sweep aren't something every push should pay for.

## Interpreting failures

Each test targets one thing from `models/ahu/tests/readme`; the test name
and its docstring/comment say which. A failure means one of:

- **Model regression** — `AHU.mo` changed and now behaves differently than
  the verified report. Do not loosen the assertion to make it pass; that
  defeats the point of this suite. Investigate the model change instead.
- **Build/toolchain drift** — an OpenModelica or Buildings version bump
  changed solver or export behavior. Compare against the verification
  report's noted OM 1.27/Buildings 13.0.0 baseline.
- **A genuinely new, previously-undiscovered issue** — document it (see the
  "Known behavior" sections above for the template), don't silently
  relax the test, and don't fix `AHU.mo`'s physics/control logic as a side
  effect of a test-infrastructure change.
