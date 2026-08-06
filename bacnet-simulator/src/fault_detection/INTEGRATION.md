# Fault-detection scaffold integration

This is a configurable FDD framework. It does not contain copyrighted ASHRAE rule text and should not be described as ASHRAE-compliant without validation against material you are licensed to use.

## Database facade

```python
from .repositories.fault_detection import FaultDetectionRepository

class Database(
    ...,
    FaultDetectionRepository,
):
    pass
```

The repository expects your existing `Database._conn()` context manager. Run `src/db/migrations/fault_detection.sql` from your setup/migration process.

## Lifespan

```python
from .fault_detection import FaultDetectionEngine, build_default_registry

fault_detection_engine = FaultDetectionEngine(
    database=db,
    simulation_engine=engine,
    registry=build_default_registry(),
    event_callback=_log_event,
)
app.state.fault_detection_engine = fault_detection_engine
```

## Tick loop

```python
await engine.tick()
await fault_detection_engine.evaluate_all()
await broadcast_state()
```

## Router

```python
from .api.routers.fault_detection import router as fault_detection_router
api.include_router(fault_detection_router)
```

## Required semantic point types

- `supply-fan-command`
- `supply-fan-status`
- `supply-air-temperature`
- `supply-air-temperature-setpoint`

Frozen sensor configuration example:

```json
{
  "frozen_sensor_point_type": "supply-air-temperature",
  "frozen_window_seconds": 300,
  "frozen_epsilon": 0.01
}
```
