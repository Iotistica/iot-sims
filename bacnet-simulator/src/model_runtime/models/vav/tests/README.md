# VAV Regression Suite

## Purpose

`test_vav.py` protects the behavior of `models/vav/SimpleVAVZone.mo` -- a
VAV terminal controller and reheat box. This is the first standalone
pytest suite for this model (the repo's only prior VAV test artifact,
`TestSimpleVAVZone.mo`, predates the current FMU interface and is not part
of any build/CI pipeline). It follows the exact conventions established in
`models/ahu/tests/test_ahu.py`, `models/boiler/tests/test_boiler.py`, and
`models/rtu/tests/test_rtu.py` -- `pyfmi` in Model Exchange mode,
`result_handling='memory'`, `core`/`extended` pytest markers.

## The `dpSup` input

`SimpleVAVZone.mo` used to drive its own upstream supply-air boundary from
a fixed parameter (`p = 101325 + dpAir`), which meant an upstream
RTU/AHU's own calculated duct static pressure (`dpSup`) had no physical
effect on this terminal's actual delivered airflow -- only the commanded
damper position mattered. `dpSup` (Pa) is now a real FMU input
(`supAir.p_in = 101325 + max(0, dpSup)`, via `Buildings.Fluid.Sources.
Boundary_pT`'s `use_p_in` conditional connector, the same pattern already
used for `TSupAHU` via `use_T_in`). See `SimpleVAVZone.mo`'s own
`Documentation` annotation, "Upstream duct static pressure (dpSup)", for
the full rationale -- this closes an interface gap found and reported
during `tests/integration`'s RTU+VAV+ThermalZone system testing.

**A real, empirically-verified control behavior, not a bug:** airflow
rises with `dpSup` only while the terminal is pressure-limited (damper
pinned open, unable to reach its flow setpoint); once `dpSup` is high
enough, `RoomVAV`'s own flow-feedback loop (fed by `VDis_flow`) throttles
the damper closed to hold exactly the design flow setpoint rather than
overflow -- a well-tuned VAV terminal is *supposed* to become pressure-
independent once it has enough authority. `test_v6_pressure_sweep_airflow_responds`
asserts monotonic non-decreasing airflow (not unbounded growth) for
exactly this reason.

## Dependencies

Same as every other suite in this repo -- `pyfmi` needs a compiled
Sundials/Assimulo stack (not a plain `pip install pyfmi`), so these tests
reuse the repo's own runtime Docker image. Build `SimpleVAVZone.fmu` from
`SimpleVAVZone.mo` via `shared/build-fmu.sh` before running these tests.

## Running core tests locally

From the repository root:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/vav/SimpleVAVZone.mo /build/SimpleVAVZone.mo && MODELICA_FILE=SimpleVAVZone.mo MODELICA_MODEL=SimpleVAVZone FMU_OUTPUT_NAME=SimpleVAVZone.fmu sh shared/build-fmu.sh && cp /fmu-out/SimpleVAVZone.fmu models/vav/SimpleVAVZone.fmu"

docker build -t iot-models-runtime:local .

docker run --rm -v ${PWD}:/app -w /app iot-models-runtime:local `
  sh -c "pip install --quiet pytest && python -m pytest models/vav/tests -m core -v"
```

## Running extended tests

Same, with `-m extended` in place of `-m core`. Extended tests include a
six-hour stability run, an extreme (2000 Pa) pressure stability check, a
`dpSup` step-response test, and a heating-mode pressure sweep.

## Interpreting failures

Same guidance as every other suite in this repo: a failure means a model
regression, build/toolchain drift, or a genuinely new issue to document --
not something to fix by loosening the assertion.
