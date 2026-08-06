from __future__ import annotations

from ..context import FaultContext
from ..models import FaultDefinition, FaultEvidence, FaultResult, FaultSeverity
from .base import FaultRule


class SupplyFanCommandStatusMismatch(
    FaultRule
):
    definition = FaultDefinition(
        rule_id=(
            "ahu.supply_fan."
            "command_status_mismatch"
        ),
        name=(
            "Supply fan command/status mismatch"
        ),
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply fan is commanded on while "
            "status remains off."
        ),
        persistence_seconds=0.0,
        clear_seconds=15.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(
        self,
        context: FaultContext,
    ) -> FaultResult:
        command = context.value(
            "Supply_Fan_Command"
        )

        status = context.value(
            "Supply_Fan_Status"
        )

        if command is None or status is None:
            return FaultResult(
                condition_present=False,
                evaluable=False,
                message=(
                    "Required Supply_Fan_Command or "
                    "Supply_Fan_Status point is missing."
                ),
                severity=self.definition.severity,
            )

        condition_present = (
            bool(command)
            and not bool(status)
        )

        return FaultResult(
            condition_present=condition_present,
            message=(
                "Supply fan is commanded on but "
                "status is off."
                if condition_present
                else (
                    "Supply fan command and status "
                    "agree."
                )
            ),
            severity=self.definition.severity,
            evidence=[
                FaultEvidence(
                    point="Supply_Fan_Command",
                    value=command,
                    expected=(
                        "False when the fan is off"
                    ),
                ),
                FaultEvidence(
                    point="Supply_Fan_Status",
                    value=status,
                    expected=(
                        "True when commanded on"
                    ),
                ),
            ],
        )


class SupplyAirTemperatureDeviation(
    FaultRule
):
    definition = FaultDefinition(
        rule_id="ahu.sat.setpoint_deviation",
        name=(
            "Supply-air temperature deviation"
        ),
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply-air temperature remains "
            "outside its setpoint tolerance."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(
        self,
        context: FaultContext,
    ) -> FaultResult:
        temperature = context.value(
            "Supply_Air_Temperature_Sensor"
        )

        setpoint = context.value(
            "Supply_Air_Temperature_Setpoint"
        )

        fan_status = context.value(
            "Supply_Fan_Status"
        )

        if (
            temperature is None
            or setpoint is None
            or fan_status is None
        ):
            return FaultResult(
                condition_present=False,
                evaluable=False,
                message=(
                    "Required Brick points are "
                    "unavailable."
                ),
                severity=self.definition.severity,
            )

        tolerance = float(
            context.parameters.get(
                "sat_tolerance",
                2.0,
            )
        )

        deviation = abs(
            float(temperature)
            - float(setpoint)
        )

        condition_present = (
            bool(fan_status)
            and deviation > tolerance
        )

        return FaultResult(
            condition_present=condition_present,
            message=(
                "Supply-air temperature differs "
                f"from setpoint by {deviation:.2f}."
            ),
            severity=self.definition.severity,
            evidence=[
                FaultEvidence(
                    point=(
                        "Supply_Air_"
                        "Temperature_Sensor"
                    ),
                    value=temperature,
                ),
                FaultEvidence(
                    point=(
                        "Supply_Air_"
                        "Temperature_Setpoint"
                    ),
                    value=setpoint,
                ),
                FaultEvidence(
                    point="Absolute_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance}",
                ),
            ],
        )