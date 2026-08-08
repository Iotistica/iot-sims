# Commissioning scaffold

This scaffold starts with a read-only AHU baseline test.

## Wire it into the app

Import:

```python
from .commissioning import CommissioningEngine
from .api.routers.commissioning import (
    router as commissioning_router,
    configure_commissioning_router,
)
```

After `db` and the simulation engine exist:

```python
commissioning_engine = CommissioningEngine(
    database=db,
    simulation_engine=engine,
)

configure_commissioning_router(commissioning_engine)
```

Register the router using the project's existing refactored router composition:

```python
api.include_router(commissioning_router)
```

## Test AHU-1

For the current seeded AHU-1 database id 308:

```http
POST /commissioning/devices/308/tests/ahu-baseline
```

Also available:

```http
GET /commissioning/tests
GET /commissioning/results
GET /commissioning/devices/308/tests/ahu-baseline/result
POST /commissioning/reset
```

## Brick migration

Do not hard-code more point names into commissioning tests.

`CommissioningPointResolver` is intentionally the compatibility boundary.
Today it uses `objects.point_type`. After Brick Core is implemented, change
the resolver internals to traverse canonical Brick entities/relationships.
The commissioning test code should remain largely unchanged.

## Next test

After Brick Core, add `ahu-fan-speed-response`:

1. capture baseline fan speed, airflow, static pressure, and power;
2. command staged fan speeds;
3. wait for simulator response;
4. verify airflow/static-pressure/power response;
5. restore original values even when the test fails.
