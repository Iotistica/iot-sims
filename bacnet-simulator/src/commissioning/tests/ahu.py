from __future__ import annotations

from typing import Any

from ..models import CommissioningCheck, CommissioningResult, CommissioningStatus


def _availability_check(
    *,
    name: str,
    point: dict[str, Any] | None,
) -> CommissioningCheck:
    if point is None:
        return CommissioningCheck(
            name=name,
            status=CommissioningStatus.FAILED,
            message="Required semantic point was not found.",
        )

    value = point.get("value")
    return CommissioningCheck(
        name=name,
        status=(
            CommissioningStatus.PASSED
            if value is not None
            else CommissioningStatus.FAILED
        ),
        message=(
            "Point is available."
            if value is not None
            else "Point exists but has no current value."
        ),
        actual=value,
    )


async def run_ahu_baseline_test(
    *,
    device_id: int,
    resolver: Any,
) -> CommissioningResult:
    """Read-only AHU baseline commissioning test."""

    result = CommissioningResult(
        test_id="ahu-baseline",
        device_id=device_id,
        equipment_type="Air_Handling_Unit",
        status=CommissioningStatus.RUNNING,
    )

    sat = resolver.first_by_semantic(
        device_id,
        "Supply_Air_Temperature_Sensor",
    )

    airflow = (
        resolver.first_by_semantic(device_id, "Supply_Air_Flow_Sensor")
        or resolver.first_by_semantic(device_id, "Air_Flow_Sensor")
    )

    static_pressure = resolver.first_by_semantic(
        device_id,
        "Static_Pressure_Sensor",
    )

    result.checks.extend(
        [
            _availability_check(
                name="Supply air temperature available",
                point=sat,
            ),
            _availability_check(
                name="Supply airflow available",
                point=airflow,
            ),
            _availability_check(
                name="Static pressure available",
                point=static_pressure,
            ),
        ]
    )

    for key, point in (
        ("supply_air_temperature", sat),
        ("airflow", airflow),
        ("static_pressure", static_pressure),
    ):
        if point is not None:
            result.measurements[key] = {
                "value": point.get("value"),
                "units": point.get("units"),
                "object_name": point.get("name"),
            }

    result.status = (
        CommissioningStatus.FAILED
        if any(
            check.status == CommissioningStatus.FAILED
            for check in result.checks
        )
        else CommissioningStatus.PASSED
    )

    return result
