# IoT FMU Model Runtime

This project runs a generic FastAPI runtime for FMU-backed models.

The runtime can serve multiple models from the `models/` folder. Each model has:

- an FMU file
- a `model.json` metadata file
- optional Modelica source/test files

Adding another model should only require adding a new folder under `models/` with its FMU and `model.json`.

## Runtime Layout

```text
app.py
shared/
  runtime/
    app.py
    catalog.py
    conversions.py
    manager.py
  build-fmu.sh
models/
  vav/
    SimpleVAVZone.fmu
    SimpleVAVZone.mo
    model.json
  ahu-simple/
    SimpleAHU.fmu
    SimpleAHU.mo
    model.json
  ahu/
    AHU.fmu
    AHU.mo
    model.json
    tests/
      test_ahu.py
      README.md
  chiller/
    SimpleChillerPlant.fmu
    SimpleChillerPlant.mo
    model.json
  boiler/
    BoilerPlant.fmu
    BoilerPlant.mo
    model.json
    tests/
      test_boiler.py
      README.md
  rtu/
    RTU.fmu
    RTU.mo
    model.json
    tests/
      test_rtu.py
      TEST_REPORT.md
      README.md
  zone/
    ThermalZone.fmu
    ThermalZone.mo
    model.json
```

`models/ahu/` (`AHU.mo`, slug `AHU`) is an experimental successor to
`models/ahu-simple/` (`SimpleAHU.mo`, slug `SimpleAHU`) with a physically
coupled chilled-water cooling coil instead of an idealized algebraic one —
see `models/ahu/tests/README.md` for what's different and why. Both are
served by the runtime below; `SimpleAHU` is the one other models' defaults
and existing BACnet mappings assume unless a mapping is explicitly pointed at
`AHU` instead.

`models/boiler/` (`BoilerPlant.mo`, slug `BoilerPlant`) is the
heating-side counterpart to `AHU.mo`, completing this project's
central-plant architecture:

```text
SimpleChillerPlant -> AHU -> VAV -> ThermalZone   (cooling)
BoilerPlant          -> AHU -> VAV -> ThermalZone   (heating)
```

A central two-boiler hot-water plant with condensing-capable efficiency,
staging/load sharing, and outdoor-air supply-water reset — see
`models/boiler/tests/README.md` for details and known behaviors. Like `AHU`,
integration with `AHU`/`SimpleVAVZone` is signal-based (documented in
`BoilerPlant.mo`'s own header), not a shared Modelica fluid network across
FMUs.

`models/rtu/` (`RTU.mo`, slug `RTU`) is a second, standalone architecture
alongside the central-plant one above — a packaged rooftop unit with its own
DX cooling and gas heating, serving downstream VAV terminals or thermal
zones directly with no central chiller/boiler plant to interface with:

```text
Weather -> RTU -> VAV -> ThermalZone   (no central plant)
```

It reuses `AHU.mo`'s proven control patterns (SAT PI, fan static-pressure PI
with most-open-VAV reset, minimum-OA economizer) but not its chilled-water
coil or central-plant signal interface — RTU does not expose
`TChiWatSup/TChiWatRet/VChiWat_flow/THotWatSup/THotWatRet`. DX cooling uses
an idealized coil plus a bounded compressor-power/COP approximation rather
than a real `Buildings.Fluid.DXSystems` component — see
`models/rtu/tests/README.md` and `RTU.mo`'s own `Documentation` annotation
for why the real DX coil was tried, tested, and rejected.

## Run The Generic Runtime

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker compose up -d
```

The API will be available at:

```text
http://localhost:8002/docs
```

Useful endpoints:

```text
GET  /models
GET  /models/{model_id}/metadata
POST /models/{model_id}/initialize
POST /models/{model_id}/step
POST /models/{model_id}/terminate
GET  /health
```

`{model_id}` is each model's canonical `id` field — an opaque GUID, not a
human-readable name. GUIDs are generated once per model and are stable
across restarts (they live in `model.json`, not in-memory), but they are
*not* predictable/hardcodable — always resolve the current id from
`GET /models` rather than guessing it. Each catalog entry also carries a
`slug`: a stable, human-readable name (e.g. `RTU`, `SimpleVAVZone`) used
for display, logs, and per-model logic that needs a fixed, known-in-advance
name (see `shared/runtime/models/diagnostics.py`) — `slug` is not itself a valid
`{model_id}` path value.

The current model slugs (see each model's `model.json` for its actual id):

```text
SimpleVAVZone
SimpleAHU
AHU
SimpleChillerPlant
BoilerPlant
RTU
ThermalZone
```

Example: resolve a model's id by slug and fetch its metadata —

```bash
curl -s http://localhost:8002/models | python -c \
  "import json,sys; models=json.load(sys.stdin)['models']; \
   print(next(m['id'] for m in models if m['slug']=='RTU'))"
# -> e.g. ac5e7188-4b72-43e0-9124-912007232705
curl -s http://localhost:8002/models/ac5e7188-4b72-43e0-9124-912007232705/metadata
```

## Important: Runtime Build Does Not Build FMUs

`docker compose build` for this project builds the shared Python runtime image only.

It does not regenerate `SimpleVAVZone.fmu`, `SimpleAHU.fmu`, `AHU.fmu`, `SimpleChillerPlant.fmu`, `BoilerPlant.fmu`, or `RTU.fmu`.

The runtime mounts the local models folder:

```yaml
volumes:
  - ./models:/app/models
```

So any FMU already present under `models/` is available to the runtime.

## Build Only The AHU (Simple) FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/ahu-simple/SimpleAHU.mo /build/SimpleAHU.mo && MODELICA_FILE=SimpleAHU.mo MODELICA_MODEL=SimpleAHU FMU_OUTPUT_NAME=SimpleAHU.fmu sh shared/build-fmu.sh && cp /fmu-out/SimpleAHU.fmu models/ahu-simple/SimpleAHU.fmu"
```

Output:

```text
models/ahu-simple/SimpleAHU.fmu
```

## Build Only The AHU FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/ahu/AHU.mo /build/AHU.mo && MODELICA_FILE=AHU.mo MODELICA_MODEL=AHU FMU_OUTPUT_NAME=AHU.fmu sh shared/build-fmu.sh && cp /fmu-out/AHU.fmu models/ahu/AHU.fmu"
```

Output:

```text
models/ahu/AHU.fmu
```

Regression tests: `models/ahu/tests/README.md` (`pytest models/ahu/tests -m core`).

## Build Only The VAV FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/vav/SimpleVAVZone.mo /build/SimpleVAVZone.mo && MODELICA_FILE=SimpleVAVZone.mo MODELICA_MODEL=SimpleVAVZone FMU_OUTPUT_NAME=SimpleVAVZone.fmu sh shared/build-fmu.sh && cp /fmu-out/SimpleVAVZone.fmu models/vav/SimpleVAVZone.fmu"
```

Output:

```text
models/vav/SimpleVAVZone.fmu
```

## Build Only The Chiller FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/chiller/SimpleChillerPlant.mo /build/SimpleChillerPlant.mo && MODELICA_FILE=SimpleChillerPlant.mo MODELICA_MODEL=SimpleChillerPlant FMU_OUTPUT_NAME=SimpleChillerPlant.fmu sh shared/build-fmu.sh && cp /fmu-out/SimpleChillerPlant.fmu models/chiller/SimpleChillerPlant.fmu"
```

Output:

```text
models/chiller/SimpleChillerPlant.fmu
```

## Build Only The Boiler Plant FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/boiler/BoilerPlant.mo /build/BoilerPlant.mo && MODELICA_FILE=BoilerPlant.mo MODELICA_MODEL=BoilerPlant FMU_OUTPUT_NAME=BoilerPlant.fmu sh shared/build-fmu.sh && cp /fmu-out/BoilerPlant.fmu models/boiler/BoilerPlant.fmu"
```

Output:

```text
models/boiler/BoilerPlant.fmu
```

Regression tests: `models/boiler/tests/README.md` (`pytest models/boiler/tests -m core`).

## Build Only The RTU FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/rtu/RTU.mo /build/RTU.mo && MODELICA_FILE=RTU.mo MODELICA_MODEL=RTU FMU_OUTPUT_NAME=RTU.fmu sh shared/build-fmu.sh && cp /fmu-out/RTU.fmu models/rtu/RTU.fmu"
```

Output:

```text
models/rtu/RTU.fmu
```

Regression tests: `models/rtu/tests/README.md` (`pytest models/rtu/tests -m core`).

## Build Only The Zone FMU

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
docker run --rm `
  -v ${PWD}:/work `
  -w /work `
  iotistic/modelica-build:om-1.27.0-buildings-13.0.0 `
  sh -c "mkdir -p /fmu-out && cp models/zone/ThermalZone.mo /build/ThermalZone.mo && MODELICA_FILE=ThermalZone.mo MODELICA_MODEL=ThermalZone FMU_OUTPUT_NAME=ThermalZone.fmu sh shared/build-fmu.sh && cp /fmu-out/ThermalZone.fmu models/zone/ThermalZone.fmu"
```

Output:

```text
models/zone/ThermalZone.fmu
```

## Smoke Tests

The tests use a fake FMU loader, so they verify the API shape, model catalog, conversions, and independent sessions without running the real solvers.

From `C:\Users\Dan\iot-sims\bacnet-simulator\src\model_runtime`:

```powershell
py -m pytest tests\test_runtime_api.py
```
