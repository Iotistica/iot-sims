from __future__ import annotations

from math import isfinite

from ..context import FaultContext
from ..models import FaultDefinition, FaultEvidence, FaultResult, FaultSeverity
from .base import FaultRule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _value(context: FaultContext, point: str):
    return context.value(point)


def _number(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _bool_from_signal(value, threshold: float = 0.05):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    number = _number(value)
    if number is None:
        return bool(value)
    return number > threshold


def _parameter(
    context: FaultContext,
    name: str,
    default,
):
    return context.parameters.get(name, default)


def _not_evaluable(
    definition: FaultDefinition,
    message: str,
):
    return FaultResult(
        condition_present=False,
        evaluable=False,
        message=message,
        severity=definition.severity,
    )


def _result(
    definition: FaultDefinition,
    condition_present: bool,
    message_fault: str,
    message_ok: str,
    evidence: list[FaultEvidence],
):
    return FaultResult(
        condition_present=condition_present,
        message=message_fault if condition_present else message_ok,
        severity=definition.severity,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Supply fan
# ---------------------------------------------------------------------------

class SupplyFanCommandStatusMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.supply_fan.command_status_mismatch",
        name="Supply fan command/status mismatch",
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply fan is commanded on while status remains off."
        ),
        persistence_seconds=30.0,
        clear_seconds=15.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _value(context, "Supply_Fan_Command")
        status = _value(context, "Supply_Fan_Status")

        if command is None or status is None:
            return _not_evaluable(
                self.definition,
                "Required Supply_Fan_Command or Supply_Fan_Status point is missing.",
            )

        threshold = float(_parameter(context, "fan_command_on_threshold", 0.05))
        commanded_on = _bool_from_signal(command, threshold)
        status_on = _bool_from_signal(status, 0.5)

        condition_present = bool(commanded_on) and not bool(status_on)

        return _result(
            self.definition,
            condition_present,
            "Supply fan is commanded on but status is off.",
            "Supply fan command and status agree.",
            [
                FaultEvidence(
                    point="Supply_Fan_Command",
                    value=command,
                    expected=f"> {threshold} when commanded on",
                ),
                FaultEvidence(
                    point="Supply_Fan_Status",
                    value=status,
                    expected="True when commanded on",
                ),
            ],
        )


class SupplyFanFailedToStop(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.supply_fan.failed_to_stop",
        name="Supply fan failed to stop",
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply fan status remains on after the fan command is removed."
        ),
        persistence_seconds=45.0,
        clear_seconds=15.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _value(context, "Supply_Fan_Command")
        status = _value(context, "Supply_Fan_Status")

        if command is None or status is None:
            return _not_evaluable(
                self.definition,
                "Required Supply_Fan_Command or Supply_Fan_Status point is missing.",
            )

        threshold = float(_parameter(context, "fan_command_on_threshold", 0.05))
        commanded_on = _bool_from_signal(command, threshold)
        status_on = _bool_from_signal(status, 0.5)

        condition_present = not bool(commanded_on) and bool(status_on)

        return _result(
            self.definition,
            condition_present,
            "Supply fan status remains on while the command is off.",
            "Supply fan stops when its command is removed.",
            [
                FaultEvidence(point="Supply_Fan_Command", value=command),
                FaultEvidence(point="Supply_Fan_Status", value=status),
            ],
        )


class SupplyFanPowerStatusMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.supply_fan.power_status_mismatch",
        name="Supply fan power/status mismatch",
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply fan status and measured/calculated fan power are inconsistent."
        ),
        persistence_seconds=60.0,
        clear_seconds=30.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        status = _value(context, "Supply_Fan_Status")
        power = _number(_value(context, "Supply_Fan_Power"))

        if status is None or power is None:
            return _not_evaluable(
                self.definition,
                "Required Supply_Fan_Status or Supply_Fan_Power point is missing.",
            )

        status_on = _bool_from_signal(status, 0.5)
        on_min_kw = float(_parameter(context, "fan_power_on_min_kw", 0.05))
        off_max_kw = float(_parameter(context, "fan_power_off_max_kw", 0.02))

        condition_present = (
            (bool(status_on) and power < on_min_kw)
            or (not bool(status_on) and power > off_max_kw)
        )

        return _result(
            self.definition,
            condition_present,
            "Supply fan power is inconsistent with fan status.",
            "Supply fan power is consistent with fan status.",
            [
                FaultEvidence(point="Supply_Fan_Status", value=status),
                FaultEvidence(
                    point="Supply_Fan_Power",
                    value=power,
                    expected=(
                        f">= {on_min_kw} kW when on and <= {off_max_kw} kW when off"
                    ),
                ),
            ],
        )


class LowSupplyAirflow(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.supply_fan.low_airflow",
        name="Low supply airflow",
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply fan is running but measured supply airflow remains too low."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        status = _value(context, "Supply_Fan_Status")
        airflow = _number(_value(context, "Supply_Air_Flow_Sensor"))

        if status is None or airflow is None:
            return _not_evaluable(
                self.definition,
                "Required Supply_Fan_Status or Supply_Air_Flow_Sensor point is missing.",
            )

        minimum_flow = float(_parameter(context, "minimum_supply_airflow_m3_s", 0.1))
        status_on = _bool_from_signal(status, 0.5)
        condition_present = bool(status_on) and airflow < minimum_flow

        return _result(
            self.definition,
            condition_present,
            "Supply fan is running but supply airflow is below the minimum threshold.",
            "Supply airflow is acceptable for the fan operating state.",
            [
                FaultEvidence(point="Supply_Fan_Status", value=status),
                FaultEvidence(
                    point="Supply_Air_Flow_Sensor",
                    value=airflow,
                    expected=f">= {minimum_flow} m3/s when fan is on",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Supply-air temperature / heating / cooling
# ---------------------------------------------------------------------------

class SupplyAirTemperatureDeviation(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.sat.setpoint_deviation",
        name="Supply-air temperature deviation",
        equipment_type="Air_Handling_Unit",
        description=(
            "Supply-air temperature remains outside its setpoint tolerance."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        temperature = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        setpoint = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan_status = _value(context, "Supply_Fan_Status")

        if temperature is None or setpoint is None or fan_status is None:
            return _not_evaluable(
                self.definition,
                "Required supply-air temperature, setpoint, or fan-status point is missing.",
            )

        tolerance = float(_parameter(context, "sat_tolerance", 2.0))
        deviation = abs(temperature - setpoint)
        condition_present = (
            bool(_bool_from_signal(fan_status, 0.5))
            and deviation > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            f"Supply-air temperature differs from setpoint by {deviation:.2f}.",
            f"Supply-air temperature is within {tolerance:.2f} of setpoint.",
            [
                FaultEvidence(
                    point="Supply_Air_Temperature_Sensor",
                    value=temperature,
                ),
                FaultEvidence(
                    point="Supply_Air_Temperature_Setpoint",
                    value=setpoint,
                ),
                FaultEvidence(
                    point="Absolute_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance}",
                ),
            ],
        )


class SupplyAirTemperatureHigh(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.sat.high",
        name="Supply-air temperature too high",
        equipment_type="Air_Handling_Unit",
        description="Supply-air temperature remains above its setpoint tolerance.",
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _value(context, "Supply_Fan_Status")

        if sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required SAT, SAT setpoint, or fan-status point is missing.",
            )

        tolerance = float(_parameter(context, "sat_tolerance", 2.0))
        deviation = sat - sp
        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and deviation > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            f"Supply-air temperature is {deviation:.2f} above setpoint.",
            "Supply-air temperature is not excessively high.",
            [
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
                FaultEvidence(
                    point="SAT_High_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance}",
                ),
            ],
        )


class SupplyAirTemperatureLow(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.sat.low",
        name="Supply-air temperature too low",
        equipment_type="Air_Handling_Unit",
        description="Supply-air temperature remains below its setpoint tolerance.",
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _value(context, "Supply_Fan_Status")

        if sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required SAT, SAT setpoint, or fan-status point is missing.",
            )

        tolerance = float(_parameter(context, "sat_tolerance", 2.0))
        deviation = sp - sat
        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and deviation > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            f"Supply-air temperature is {deviation:.2f} below setpoint.",
            "Supply-air temperature is not excessively low.",
            [
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
                FaultEvidence(
                    point="SAT_Low_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance}",
                ),
            ],
        )


class CoolingIneffective(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.cooling.ineffective",
        name="Cooling ineffective",
        equipment_type="Air_Handling_Unit",
        description=(
            "Cooling command is high while supply-air temperature remains above setpoint."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cooling = _number(_value(context, "Cooling_Command"))
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _value(context, "Supply_Fan_Status")

        if cooling is None or sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required cooling command, SAT, SAT setpoint, or fan status is missing.",
            )

        command_threshold = float(_parameter(context, "cooling_high_threshold", 0.8))
        tolerance = float(_parameter(context, "sat_tolerance", 2.0))

        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and cooling >= command_threshold
            and sat > sp + tolerance
        )

        return _result(
            self.definition,
            condition_present,
            "Cooling is heavily commanded but supply-air temperature remains too warm.",
            "Cooling response is consistent with the current SAT condition.",
            [
                FaultEvidence(
                    point="Cooling_Command",
                    value=cooling,
                    expected=f"< {command_threshold} unless cooling is needed",
                ),
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
            ],
        )


class HeatingIneffective(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.heating.ineffective",
        name="Heating ineffective",
        equipment_type="Air_Handling_Unit",
        description=(
            "Heating command is high while supply-air temperature remains below setpoint."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        heating = _number(_value(context, "Heating_Command"))
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _value(context, "Supply_Fan_Status")

        if heating is None or sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required heating command, SAT, SAT setpoint, or fan status is missing.",
            )

        command_threshold = float(_parameter(context, "heating_high_threshold", 0.8))
        tolerance = float(_parameter(context, "sat_tolerance", 2.0))

        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and heating >= command_threshold
            and sat < sp - tolerance
        )

        return _result(
            self.definition,
            condition_present,
            "Heating is heavily commanded but supply-air temperature remains too cold.",
            "Heating response is consistent with the current SAT condition.",
            [
                FaultEvidence(point="Heating_Command", value=heating),
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
            ],
        )


class SimultaneousHeatingCooling(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.heating_cooling.simultaneous",
        name="Simultaneous heating and cooling",
        equipment_type="Air_Handling_Unit",
        description="Heating and cooling commands are active at the same time.",
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        heating = _number(_value(context, "Heating_Command"))
        cooling = _number(_value(context, "Cooling_Command"))

        if heating is None or cooling is None:
            return _not_evaluable(
                self.definition,
                "Required Heating_Command or Cooling_Command point is missing.",
            )

        threshold = float(_parameter(context, "simultaneous_heat_cool_threshold", 0.1))
        condition_present = heating > threshold and cooling > threshold

        return _result(
            self.definition,
            condition_present,
            "Heating and cooling are simultaneously active.",
            "Heating and cooling are not simultaneously active.",
            [
                FaultEvidence(
                    point="Heating_Command",
                    value=heating,
                    expected=f"<= {threshold} when cooling is active",
                ),
                FaultEvidence(
                    point="Cooling_Command",
                    value=cooling,
                    expected=f"<= {threshold} when heating is active",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Chilled-water / cooling coil
# ---------------------------------------------------------------------------

class CoolingCommandNoChilledWaterFlow(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.cooling.no_chilled_water_flow",
        name="Cooling command with no chilled-water flow",
        equipment_type="Air_Handling_Unit",
        description=(
            "Cooling command is active but chilled-water flow through the coil is near zero."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cooling = _number(_value(context, "Cooling_Command"))
        flow = _number(_value(context, "Chilled_Water_Flow_Sensor"))

        if cooling is None or flow is None:
            return _not_evaluable(
                self.definition,
                "Required Cooling_Command or Chilled_Water_Flow_Sensor point is missing.",
            )

        command_threshold = float(_parameter(context, "cooling_flow_command_threshold", 0.5))
        minimum_flow = float(_parameter(context, "minimum_chilled_water_flow_m3_s", 0.00005))

        condition_present = (
            cooling >= command_threshold
            and flow < minimum_flow
        )

        return _result(
            self.definition,
            condition_present,
            "Cooling is commanded but chilled-water flow is insufficient.",
            "Chilled-water flow is consistent with cooling demand.",
            [
                FaultEvidence(point="Cooling_Command", value=cooling),
                FaultEvidence(
                    point="Chilled_Water_Flow_Sensor",
                    value=flow,
                    expected=f">= {minimum_flow} m3/s when cooling command >= {command_threshold}",
                ),
            ],
        )


class CoolingValveLeakage(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.cooling.valve_leakage",
        name="Cooling valve leakage",
        equipment_type="Air_Handling_Unit",
        description=(
            "Cooling command is near zero while chilled-water flow or cooling load remains substantial."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cooling = _number(_value(context, "Cooling_Command"))
        flow = _number(_value(context, "Chilled_Water_Flow_Sensor"))
        load = _number(_value(context, "Cooling_Thermal_Power_Sensor"))

        if cooling is None or (flow is None and load is None):
            return _not_evaluable(
                self.definition,
                "Required Cooling_Command and at least one chilled-water flow/cooling-load point are missing.",
            )

        off_threshold = float(_parameter(context, "cooling_off_threshold", 0.05))
        leakage_flow = float(_parameter(context, "chilled_water_leakage_flow_m3_s", 0.00005))
        leakage_load_kw = float(_parameter(context, "cooling_leakage_load_kw", 0.5))

        flow_fault = flow is not None and flow > leakage_flow
        load_fault = load is not None and load > leakage_load_kw
        condition_present = cooling <= off_threshold and (flow_fault or load_fault)

        evidence = [
            FaultEvidence(
                point="Cooling_Command",
                value=cooling,
                expected=f"<= {off_threshold} for closed cooling valve",
            )
        ]
        if flow is not None:
            evidence.append(
                FaultEvidence(
                    point="Chilled_Water_Flow_Sensor",
                    value=flow,
                    expected=f"<= {leakage_flow} m3/s when cooling is off",
                )
            )
        if load is not None:
            evidence.append(
                FaultEvidence(
                    point="Cooling_Thermal_Power_Sensor",
                    value=load,
                    expected=f"<= {leakage_load_kw} kW when cooling is off",
                )
            )

        return _result(
            self.definition,
            condition_present,
            "Cooling appears to persist while the cooling command is off.",
            "No significant cooling-valve leakage is indicated.",
            evidence,
        )


class ChilledWaterSupplyTooWarm(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.cooling.chw_supply_too_warm",
        name="Chilled-water supply too warm",
        equipment_type="Air_Handling_Unit",
        description=(
            "Cooling is requested but chilled-water supply temperature is too warm."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cooling = _number(_value(context, "Cooling_Command"))
        chw_temp = _number(_value(context, "Chilled_Water_Supply_Temperature"))

        if cooling is None or chw_temp is None:
            return _not_evaluable(
                self.definition,
                "Required Cooling_Command or Chilled_Water_Supply_Temperature point is missing.",
            )

        command_threshold = float(_parameter(context, "cooling_active_threshold", 0.2))
        max_chw_c = float(_parameter(context, "maximum_chilled_water_supply_temperature_c", 10.0))

        condition_present = cooling > command_threshold and chw_temp > max_chw_c

        return _result(
            self.definition,
            condition_present,
            "Cooling is active but chilled-water supply temperature is too warm.",
            "Chilled-water supply temperature is acceptable for the current cooling state.",
            [
                FaultEvidence(point="Cooling_Command", value=cooling),
                FaultEvidence(
                    point="Chilled_Water_Supply_Temperature",
                    value=chw_temp,
                    expected=f"<= {max_chw_c} C when cooling is active",
                ),
            ],
        )


class PoorChilledWaterDeltaT(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.cooling.low_chw_delta_t",
        name="Low chilled-water delta-T",
        equipment_type="Air_Handling_Unit",
        description=(
            "Cooling is active but chilled-water return-to-supply temperature difference is too small."
        ),
        persistence_seconds=600.0,
        clear_seconds=180.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cooling = _number(_value(context, "Cooling_Command"))
        chw_sup = _number(_value(context, "Chilled_Water_Supply_Temperature"))
        chw_ret = _number(_value(context, "Chilled_Water_Return_Temperature"))
        flow = _number(_value(context, "Chilled_Water_Flow_Sensor"))

        if cooling is None or chw_sup is None or chw_ret is None:
            return _not_evaluable(
                self.definition,
                "Required cooling command and chilled-water supply/return temperatures are missing.",
            )

        command_threshold = float(_parameter(context, "cooling_active_threshold", 0.2))
        min_delta_t = float(_parameter(context, "minimum_chilled_water_delta_t_c", 2.0))
        min_flow = float(_parameter(context, "minimum_chilled_water_flow_m3_s", 0.00005))

        delta_t = chw_ret - chw_sup
        flow_ok = flow is None or flow >= min_flow
        condition_present = (
            cooling > command_threshold
            and flow_ok
            and delta_t < min_delta_t
        )

        evidence = [
            FaultEvidence(point="Cooling_Command", value=cooling),
            FaultEvidence(point="Chilled_Water_Supply_Temperature", value=chw_sup),
            FaultEvidence(point="Chilled_Water_Return_Temperature", value=chw_ret),
            FaultEvidence(
                point="Chilled_Water_Delta_T",
                value=delta_t,
                expected=f">= {min_delta_t} C during cooling",
            ),
        ]
        if flow is not None:
            evidence.append(FaultEvidence(point="Chilled_Water_Flow_Sensor", value=flow))

        return _result(
            self.definition,
            condition_present,
            "Chilled-water delta-T is low while cooling is active.",
            "Chilled-water delta-T is acceptable for the current cooling state.",
            evidence,
        )


# ---------------------------------------------------------------------------
# Economizer / mixing
# ---------------------------------------------------------------------------

class EconomizerNotUsingFreeCooling(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.economizer.not_using_free_cooling",
        name="Economizer not using available free cooling",
        equipment_type="Air_Handling_Unit",
        description=(
            "Outdoor air is favorable for economizing, cooling is needed, but the outdoor-air damper remains near minimum."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        out_temp = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        ret_temp = _number(_value(context, "Return_Air_Temperature_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        cooling = _number(_value(context, "Cooling_Command"))
        fan = _value(context, "Supply_Fan_Status")

        if (
            out_temp is None
            or ret_temp is None
            or damper is None
            or cooling is None
            or fan is None
        ):
            return _not_evaluable(
                self.definition,
                "Required OA/RA temperatures, OA damper, cooling command, or fan status is missing.",
            )

        diff = float(_parameter(context, "economizer_temperature_differential_c", 1.0))
        min_oa = float(_parameter(context, "minimum_outdoor_air_fraction", 0.15))
        damper_tol = float(_parameter(context, "economizer_damper_tolerance", 0.05))
        cooling_threshold = float(_parameter(context, "cooling_active_threshold", 0.2))

        favorable = out_temp < ret_temp - diff
        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and favorable
            and cooling > cooling_threshold
            and damper <= min_oa + damper_tol
        )

        return _result(
            self.definition,
            condition_present,
            "Free cooling is available but the outdoor-air damper remains near minimum.",
            "Economizer operation is consistent with outdoor/return-air conditions.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=out_temp),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=ret_temp),
                FaultEvidence(point="Outside_Air_Damper_Position_Sensor", value=damper),
                FaultEvidence(point="Cooling_Command", value=cooling),
            ],
        )


class EconomizerExcessiveOutdoorAir(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.economizer.excessive_outdoor_air",
        name="Excessive outdoor air",
        equipment_type="Air_Handling_Unit",
        description=(
            "Outdoor-air damper remains well above minimum when outdoor conditions are unfavorable."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        out_temp = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        ret_temp = _number(_value(context, "Return_Air_Temperature_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        fan = _value(context, "Supply_Fan_Status")

        if out_temp is None or ret_temp is None or damper is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required OA/RA temperatures, OA damper position, or fan status is missing.",
            )

        diff = float(_parameter(context, "economizer_temperature_differential_c", 1.0))
        min_oa = float(_parameter(context, "minimum_outdoor_air_fraction", 0.15))
        excess = float(_parameter(context, "excess_outdoor_air_margin", 0.15))

        unfavorable = out_temp >= ret_temp - diff
        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and unfavorable
            and damper > min_oa + excess
        )

        return _result(
            self.definition,
            condition_present,
            "Outdoor-air damper is excessively open under unfavorable conditions.",
            "Outdoor-air damper position is reasonable for current conditions.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=out_temp),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=ret_temp),
                FaultEvidence(
                    point="Outside_Air_Damper_Position_Sensor",
                    value=damper,
                    expected=f"<= {min_oa + excess:.2f} when economizing is unfavorable",
                ),
            ],
        )


class MixedAirTemperatureOutOfRange(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.economizer.mixed_air_temperature_out_of_range",
        name="Mixed-air temperature out of physical range",
        equipment_type="Air_Handling_Unit",
        description=(
            "Mixed-air temperature is outside the plausible range bounded by outdoor and return-air temperatures."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        out_temp = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        ret_temp = _number(_value(context, "Return_Air_Temperature_Sensor"))
        mix_temp = _number(_value(context, "Mixed_Air_Temperature_Sensor"))
        fan = _value(context, "Supply_Fan_Status")

        if out_temp is None or ret_temp is None or mix_temp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required OA, RA, mixed-air temperature, or fan-status point is missing.",
            )

        tolerance = float(_parameter(context, "mixed_air_physical_tolerance_c", 1.0))
        lower = min(out_temp, ret_temp) - tolerance
        upper = max(out_temp, ret_temp) + tolerance

        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and not (lower <= mix_temp <= upper)
        )

        return _result(
            self.definition,
            condition_present,
            "Mixed-air temperature lies outside the physical OA/RA mixing range.",
            "Mixed-air temperature is physically plausible.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=out_temp),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=ret_temp),
                FaultEvidence(
                    point="Mixed_Air_Temperature_Sensor",
                    value=mix_temp,
                    expected=f"between {lower:.2f} and {upper:.2f}",
                ),
            ],
        )


class MixedAirTemperatureMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.economizer.mixed_air_temperature_mismatch",
        name="Mixed-air temperature mismatch",
        equipment_type="Air_Handling_Unit",
        description=(
            "Measured mixed-air temperature differs materially from the temperature expected from OA/RA mixing."
        ),
        persistence_seconds=180.0,
        clear_seconds=90.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        out_temp = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        ret_temp = _number(_value(context, "Return_Air_Temperature_Sensor"))
        mix_temp = _number(_value(context, "Mixed_Air_Temperature_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        fan = _value(context, "Supply_Fan_Status")

        if (
            out_temp is None
            or ret_temp is None
            or mix_temp is None
            or damper is None
            or fan is None
        ):
            return _not_evaluable(
                self.definition,
                "Required OA/RA/mixed-air temperatures, OA damper, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "mixed_air_temperature_tolerance_c", 2.0))
        oa_fraction = max(0.0, min(1.0, damper))
        expected = oa_fraction * out_temp + (1.0 - oa_fraction) * ret_temp
        deviation = abs(mix_temp - expected)

        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and deviation > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            f"Mixed-air temperature differs from expected mixing temperature by {deviation:.2f} C.",
            "Mixed-air temperature agrees with the expected OA/RA mixing relationship.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=out_temp),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=ret_temp),
                FaultEvidence(point="Outside_Air_Damper_Position_Sensor", value=damper),
                FaultEvidence(point="Mixed_Air_Temperature_Sensor", value=mix_temp),
                FaultEvidence(
                    point="Expected_Mixed_Air_Temperature",
                    value=expected,
                    expected=f"within +/- {tolerance} C",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Duct static pressure / VAV reset
# ---------------------------------------------------------------------------

class SupplyStaticPressureHigh(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.static_pressure.high",
        name="Supply static pressure high",
        equipment_type="Air_Handling_Unit",
        description="Supply duct static pressure remains above its setpoint tolerance.",
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        pressure = _number(_value(context, "Supply_Air_Static_Pressure_Sensor"))
        setpoint = _number(_value(context, "Supply_Air_Static_Pressure_Setpoint"))
        fan = _value(context, "Supply_Fan_Status")

        if pressure is None or setpoint is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required static-pressure sensor, setpoint, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "static_pressure_tolerance_pa", 75.0))
        deviation = pressure - setpoint

        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and deviation > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            f"Supply static pressure is {deviation:.1f} Pa above setpoint.",
            "Supply static pressure is not excessively high.",
            [
                FaultEvidence(point="Supply_Air_Static_Pressure_Sensor", value=pressure),
                FaultEvidence(point="Supply_Air_Static_Pressure_Setpoint", value=setpoint),
                FaultEvidence(
                    point="Static_Pressure_High_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance} Pa",
                ),
            ],
        )


class SupplyStaticPressureLow(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.static_pressure.low",
        name="Supply static pressure low",
        equipment_type="Air_Handling_Unit",
        description="Supply duct static pressure remains below its setpoint tolerance.",
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        pressure = _number(_value(context, "Supply_Air_Static_Pressure_Sensor"))
        setpoint = _number(_value(context, "Supply_Air_Static_Pressure_Setpoint"))
        fan = _value(context, "Supply_Fan_Status")

        if pressure is None or setpoint is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required static-pressure sensor, setpoint, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "static_pressure_tolerance_pa", 75.0))
        deviation = setpoint - pressure

        condition_present = (
            bool(_bool_from_signal(fan, 0.5))
            and deviation > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            f"Supply static pressure is {deviation:.1f} Pa below setpoint.",
            "Supply static pressure is not excessively low.",
            [
                FaultEvidence(point="Supply_Air_Static_Pressure_Sensor", value=pressure),
                FaultEvidence(point="Supply_Air_Static_Pressure_Setpoint", value=setpoint),
                FaultEvidence(
                    point="Static_Pressure_Low_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance} Pa",
                ),
            ],
        )


class StaticPressureResetMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.static_pressure.reset_mismatch",
        name="Static-pressure reset mismatch",
        equipment_type="Air_Handling_Unit",
        description=(
            "Most-open VAV damper position and AHU static-pressure setpoint are inconsistent."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        damper = _number(_value(context, "Most_Open_VAV_Damper_Position"))
        setpoint = _number(_value(context, "Supply_Air_Static_Pressure_Setpoint"))

        if damper is None or setpoint is None:
            return _not_evaluable(
                self.definition,
                "Required most-open VAV damper position or static-pressure setpoint is missing.",
            )

        damper_low = float(_parameter(context, "vav_damper_low_fraction", 0.60))
        damper_high = float(_parameter(context, "vav_damper_high_fraction", 0.90))
        sp_min = float(_parameter(context, "static_pressure_setpoint_min_pa", 200.0))
        sp_max = float(_parameter(context, "static_pressure_setpoint_max_pa", 500.0))
        sp_tolerance = float(_parameter(context, "static_pressure_reset_tolerance_pa", 50.0))

        expected_fraction = max(
            0.0,
            min(
                1.0,
                (damper - damper_low) / max(0.01, damper_high - damper_low),
            ),
        )
        expected_sp = sp_min + (sp_max - sp_min) * expected_fraction
        deviation = abs(setpoint - expected_sp)
        condition_present = deviation > sp_tolerance

        return _result(
            self.definition,
            condition_present,
            f"Static-pressure setpoint differs from VAV-reset expectation by {deviation:.1f} Pa.",
            "Static-pressure setpoint is consistent with the most-open VAV reset.",
            [
                FaultEvidence(point="Most_Open_VAV_Damper_Position", value=damper),
                FaultEvidence(point="Supply_Air_Static_Pressure_Setpoint", value=setpoint),
                FaultEvidence(
                    point="Expected_Static_Pressure_Setpoint",
                    value=expected_sp,
                    expected=f"within +/- {sp_tolerance} Pa",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Optional command/feedback diagnostic
# ---------------------------------------------------------------------------

class OutdoorAirDamperCommandPositionMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="ahu.economizer.damper_command_position_mismatch",
        name="Outdoor-air damper command/position mismatch",
        equipment_type="Air_Handling_Unit",
        description=(
            "Outdoor-air damper position does not track its command."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Outside_Air_Damper_Command"))
        position = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))

        if command is None or position is None:
            return _not_evaluable(
                self.definition,
                "Required outside-air damper command or position point is missing.",
            )

        tolerance = float(_parameter(context, "damper_command_position_tolerance", 0.10))
        deviation = abs(command - position)
        condition_present = deviation > tolerance

        return _result(
            self.definition,
            condition_present,
            f"Outdoor-air damper position differs from command by {deviation:.2f}.",
            "Outdoor-air damper position tracks its command.",
            [
                FaultEvidence(point="Outside_Air_Damper_Command", value=command),
                FaultEvidence(point="Outside_Air_Damper_Position_Sensor", value=position),
                FaultEvidence(
                    point="Damper_Command_Position_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance}",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

AHU_FAULT_RULES: tuple[type[FaultRule], ...] = (
    SupplyFanCommandStatusMismatch,
    SupplyFanFailedToStop,
    SupplyFanPowerStatusMismatch,
    LowSupplyAirflow,
    SupplyAirTemperatureDeviation,
    SupplyAirTemperatureHigh,
    SupplyAirTemperatureLow,
    CoolingIneffective,
    HeatingIneffective,
    SimultaneousHeatingCooling,
    CoolingCommandNoChilledWaterFlow,
    CoolingValveLeakage,
    ChilledWaterSupplyTooWarm,
    PoorChilledWaterDeltaT,
    EconomizerNotUsingFreeCooling,
    EconomizerExcessiveOutdoorAir,
    MixedAirTemperatureOutOfRange,
    MixedAirTemperatureMismatch,
    SupplyStaticPressureHigh,
    SupplyStaticPressureLow,
    StaticPressureResetMismatch,
    OutdoorAirDamperCommandPositionMismatch,
)
