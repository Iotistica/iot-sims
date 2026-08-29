from __future__ import annotations

from math import isfinite

from ..context import FaultContext
from ..models import FaultDefinition, FaultEvidence, FaultResult, FaultSeverity
from .base import FaultRule


# ===========================================================================
# RULE DESCRIPTION CONVENTION
# ===========================================================================
#
# Each FaultDefinition.description below explains:
#   - Target: the equipment behavior or control sequence being checked.
#   - Detection intent: the abnormal condition the rule is designed to find.
#   - Likely causes: examples of faults/signals that may produce the symptom.
#
# Descriptions are diagnostic guidance, not guaranteed root-cause statements.
# A rule should report evidence and be correlated with other rules before
# declaring a specific mechanical root cause.
# ===========================================================================


# ===========================================================================
# UPSTREAM INTEGRATION REQUIREMENTS
# ===========================================================================
#
# The rules in this module depend on CANONICAL APPLICATION SEMANTICS.
#
# The upstream semantic/mapping layer MUST:
#
# 1. Map raw FMU/BACnet/vendor points to the canonical semantic keys used here.
#
# 2. Use Brick semantics when an appropriate Brick class exists.
#
# 3. When Brick has no sufficiently accurate concept, use a project-specific
#    semantic extension rather than forcing the point into an incorrect Brick
#    class.
#
# 4. Preserve engineering units expected by these rules.
#
# 5. Return None when a required semantic cannot be resolved.
#    Missing points MUST NOT silently become 0 or False.
#
# 6. Preserve equipment association. A point resolved for RTU-1 must belong
#    to RTU-1 or one of its associated components.
#
# 7. Normalize actuator commands/positions and PLR values to 0..1 unless the
#    semantic explicitly says otherwise.
#
# 8. Convert temperatures to degC for these rules unless otherwise documented.
#
# Example raw Modelica mappings for the current gas/DX RTU model:
#
#   TSup                 -> Supply_Air_Temperature_Sensor
#   TSupSet              -> Supply_Air_Temperature_Setpoint
#   TOut                 -> Outside_Air_Temperature_Sensor
#   TRet                 -> Return_Air_Temperature_Sensor
#   TMix                 -> Mixed_Air_Temperature_Sensor
#   VSup_flow            -> Supply_Air_Flow_Sensor
#   yFan                 -> Supply_Fan_Command
#   supplyFanStatus      -> Supply_Fan_Status
#   PFan                 -> Supply_Fan_Power
#   dpSup                -> Supply_Air_Static_Pressure_Sensor
#   dpSupSet             -> Supply_Air_Static_Pressure_Setpoint
#   uVAVDamMax           -> Most_Open_VAV_Damper_Position
#   yOutDamCmd           -> Outside_Air_Damper_Command
#   yOutDam              -> Outside_Air_Damper_Position_Sensor
#   VOutAir_flow         -> Outside_Air_Flow_Sensor
#   yCoo                 -> Cooling_Command
#   coolingPLR           -> Cooling_Part_Load_Ratio
#   coolingStatus        -> Cooling_Status
#   QCoolLoad            -> Cooling_Thermal_Power_Sensor
#   PCompressor          -> Cooling_Compressor_Power
#   compressorCOP        -> Cooling_Compressor_COP
#   compressorStage      -> Cooling_Compressor_Stage
#   availableCoolingCapacity -> Available_Cooling_Capacity
#   yHea                 -> Heating_Command
#   heatingPLR           -> Heating_Part_Load_Ratio
#   heatingStatus        -> Heating_Status
#   QHeaLoad             -> Heating_Thermal_Power_Sensor
#   gasHeatingPower      -> Gas_Heating_Input_Power
#   totalElectricPower   -> Total_Electric_Power
#
# For a heat-pump RTU variant, optional project semantics may additionally map:
#
#   PHeatingCompressor   -> Heating_Compressor_Power
#   heatingCOP           -> Heating_COP
#   availableHeatingCapacity -> Available_Heating_Capacity
#
# The FDD rules intentionally do NOT depend on raw names such as:
#   PCompressor
#   yOutDam
#   BACnet analogInput:42
#   RTU1_COMP_PWR
#
# Those are resolved upstream.
# ===========================================================================


CANONICAL_SEMANTICS: dict[str, dict[str, object]] = {
    # "Supply_Fan_Command"/"Supply_Fan_Status" below are FDD-internal signal-
    # role names, not Brick classes themselves -- neither string is a real
    # Brick class at the pinned BRICK_VERSION (verified directly against
    # bricksrc/command.py, status.py, and deprecations.py at v1.4.4; see
    # src/core/config.py's own verification convention). Real Brick's
    # generic Fan_Command/Fan_Speed_Command/Fan_Status point classes cover
    # any fan; "supply" vs "return" is expressed via equipment association
    # (isPointOf a Supply_Fan vs Return_Fan sub-equipment entity, isPartOf
    # the AHU/RTU), never via a distinct per-fan point-class name.
    # FaultDetectionEngine._resolve_fan_role_points() (src/fault_detection/
    # engine.py) resolves these two role keys via exactly that equipment-
    # relationship disambiguation -- the same one src/semantics/resolver.py's
    # resolve_ahu_fans() already does for the Energy Engine -- rather than
    # the flat point_type lookup FaultContext.value() otherwise does, so
    # they DO resolve correctly today despite not being literal Brick class
    # names. The "brick" field names which real class(es) that resolution
    # actually looks for, for documentation purposes only -- _value() never
    # reads this field.
    "Supply_Fan_Command": {
        "brick": "Fan_Speed_Command",
        "description": "Supply-fan normalized command or enable (Brick: Fan_Speed_Command, falling back to Fan_Command, isPointOf the Supply_Fan sub-equipment).",
    },
    "Supply_Fan_Status": {
        "brick": "Fan_Status",
        "description": "Proven supply-fan running status (Brick: Fan_Status isPointOf the Supply_Fan sub-equipment).",
    },
    "Supply_Fan_Power": {
        "brick": None,
        "description": "Supply-fan electric power.",
    },
    "Supply_Air_Flow_Sensor": {
        "brick": "Supply_Air_Flow_Sensor",
        "description": "Supply-air volumetric flow.",
    },
    "Supply_Air_Temperature_Sensor": {
        "brick": "Supply_Air_Temperature_Sensor",
        "description": "Supply-air temperature.",
    },
    "Supply_Air_Temperature_Setpoint": {
        "brick": "Supply_Air_Temperature_Setpoint",
        "description": "Supply-air temperature setpoint.",
    },
    "Outside_Air_Temperature_Sensor": {
        "brick": "Outside_Air_Temperature_Sensor",
        "description": "Outdoor-air temperature.",
    },
    "Return_Air_Temperature_Sensor": {
        "brick": "Return_Air_Temperature_Sensor",
        "description": "Return-air temperature.",
    },
    "Mixed_Air_Temperature_Sensor": {
        "brick": "Mixed_Air_Temperature_Sensor",
        "description": "Mixed-air temperature.",
    },
    "Outside_Air_Damper_Command": {
        "brick": None,
        "description": "Outdoor-air damper controller command, normalized 0..1.",
    },
    "Outside_Air_Damper_Position_Sensor": {
        "brick": None,
        "description": "Actual outdoor-air damper position, normalized 0..1.",
    },
    "Outside_Air_Flow_Sensor": {
        "brick": "Outside_Air_Flow_Sensor",
        "description": "Outdoor-air volume flow.",
    },
    "Cooling_Command": {
        "brick": None,
        "description": "Normalized mechanical cooling command.",
    },
    "Cooling_Status": {
        "brick": None,
        "description": "Mechanical cooling active status.",
    },
    "Cooling_Part_Load_Ratio": {
        "brick": None,
        "description": "Actual normalized cooling part-load ratio.",
    },
    "Cooling_Thermal_Power_Sensor": {
        "brick": None,
        "description": "Cooling delivered to the air, positive kW.",
    },
    "Cooling_Compressor_Power": {
        "brick": None,
        "description": "DX cooling compressor electric power in kW.",
    },
    "Cooling_Compressor_COP": {
        "brick": None,
        "description": "Effective cooling compressor COP.",
    },
    "Cooling_Compressor_Stage": {
        "brick": None,
        "description": "Cooling stage diagnostic: 0=off, 1=stage 1, 2=stage 2.",
    },
    "Available_Cooling_Capacity": {
        "brick": None,
        "description": "Available sensible cooling capacity in kW.",
    },
    "Heating_Command": {
        "brick": None,
        "description": "Normalized heating command.",
    },
    "Heating_Status": {
        "brick": None,
        "description": "Heating active status.",
    },
    "Heating_Part_Load_Ratio": {
        "brick": None,
        "description": "Actual normalized heating PLR.",
    },
    "Heating_Thermal_Power_Sensor": {
        "brick": None,
        "description": "Heating delivered to the air, positive kW.",
    },
    "Gas_Heating_Input_Power": {
        "brick": None,
        "description": "Gas furnace fuel input power in kW.",
    },
    "Heating_Compressor_Power": {
        "brick": None,
        "description": "Heat-pump heating compressor electric power in kW.",
    },
    "Heating_COP": {
        "brick": None,
        "description": "Heat-pump heating COP.",
    },
    "Available_Heating_Capacity": {
        "brick": None,
        "description": "Available heat-pump heating capacity in kW.",
    },
    "Total_Electric_Power": {
        "brick": None,
        "description": "Total RTU electrical power in kW.",
    },
    "Supply_Air_Static_Pressure_Sensor": {
        "brick": "Supply_Air_Static_Pressure_Sensor",
        "description": "Supply-duct static pressure in Pa.",
    },
    "Supply_Air_Static_Pressure_Setpoint": {
        "brick": "Supply_Air_Static_Pressure_Setpoint",
        "description": "Supply-duct static-pressure setpoint in Pa.",
    },
    "Most_Open_VAV_Damper_Position": {
        "brick": None,
        "description": "Maximum downstream VAV damper position, normalized 0..1.",
    },
}


BUILTIN_SEMANTIC_ALIASES: dict[str, tuple[str, ...]] = {
    "Cooling_Command": (
        "Cooling_Coil_Command",
        "Compressor_Command",
    ),
    "Heating_Command": (
        "Heating_Coil_Command",
        "Furnace_Command",
    ),
    "Cooling_Thermal_Power_Sensor": (
        "Cooling_Load",
        "Cooling_Capacity",
    ),
    "Heating_Thermal_Power_Sensor": (
        "Heating_Load",
        "Heating_Capacity",
    ),
    "Outside_Air_Damper_Position_Sensor": (
        "Outside_Air_Damper_Position",
    ),
    "Most_Open_VAV_Damper_Position": (
        "Maximum_VAV_Damper_Position",
        "Most_Open_Terminal_Damper_Position",
    ),
    "Gas_Heating_Input_Power": (
        "Gas_Furnace_Input_Power",
        "Natural_Gas_Heating_Power",
    ),
    "Cooling_Compressor_Power": (
        "Compressor_Power",
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _semantic_aliases(context: FaultContext, canonical_key: str) -> tuple[str, ...]:
    configured = context.parameters.get("semantic_aliases", {})
    configured_aliases: tuple[str, ...] = ()

    if isinstance(configured, dict):
        value = configured.get(canonical_key, ())
        if isinstance(value, str):
            configured_aliases = (value,)
        elif isinstance(value, (list, tuple, set)):
            configured_aliases = tuple(str(item) for item in value)

    return configured_aliases + BUILTIN_SEMANTIC_ALIASES.get(canonical_key, ())


def _value(context: FaultContext, canonical_key: str):
    value = context.value(canonical_key)
    if value is not None:
        return value

    for alias in _semantic_aliases(context, canonical_key):
        value = context.value(alias)
        if value is not None:
            return value

    return None


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


def _parameter(context: FaultContext, name: str, default):
    return context.parameters.get(name, default)


def _not_evaluable(definition: FaultDefinition, message: str):
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


def _fan_running(context: FaultContext):
    status = _value(context, "Supply_Fan_Status")
    if status is None:
        return None
    return bool(_bool_from_signal(status, 0.5))


# ---------------------------------------------------------------------------
# Supply fan / airflow
# ---------------------------------------------------------------------------

class SupplyFanCommandStatusMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.supply_fan.command_status_mismatch",
        name="RTU supply fan command/status mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: supply-fan start/proof sequence. Detects a fan that is commanded on but does not prove running. Intended to identify fan/VFD/interlock failures, status-point failures, or other conditions preventing airflow after a start command."
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
        commanded_on = bool(_bool_from_signal(command, threshold))
        status_on = bool(_bool_from_signal(status, 0.5))
        condition_present = commanded_on and not status_on

        return _result(
            self.definition,
            condition_present,
            "RTU supply fan is commanded on but status is off.",
            "RTU supply fan command and status agree.",
            [
                FaultEvidence(point="Supply_Fan_Command", value=command),
                FaultEvidence(point="Supply_Fan_Status", value=status),
            ],
        )


class SupplyFanFailedToStop(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.supply_fan.failed_to_stop",
        name="RTU supply fan failed to stop",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: supply-fan stop sequence. Detects a fan that remains proven on after its command is removed. Intended to identify stuck relays/contactors, VFD command problems, latched controls, or incorrect fan-status feedback."
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
        condition_present = (
            not bool(_bool_from_signal(command, threshold))
            and bool(_bool_from_signal(status, 0.5))
        )

        return _result(
            self.definition,
            condition_present,
            "RTU supply fan status remains on with command off.",
            "RTU supply fan stops when its command is removed.",
            [
                FaultEvidence(point="Supply_Fan_Command", value=command),
                FaultEvidence(point="Supply_Fan_Status", value=status),
            ],
        )


class SupplyFanPowerStatusMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.supply_fan.power_status_mismatch",
        name="RTU supply fan power/status mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: consistency between supply-fan status and electrical power. Flags cases where the fan is reported on with near-zero power, or reported off while meaningful fan power remains. Useful for identifying bad status points, failed current/power sensing, or abnormal fan/VFD operation."
        ),
        persistence_seconds=60.0,
        clear_seconds=30.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        power = _number(_value(context, "Supply_Fan_Power"))
        status = _value(context, "Supply_Fan_Status")

        if power is None or status is None:
            return _not_evaluable(
                self.definition,
                "Required Supply_Fan_Power or Supply_Fan_Status point is missing.",
            )

        on_min = float(_parameter(context, "fan_power_on_min_kw", 0.05))
        off_max = float(_parameter(context, "fan_power_off_max_kw", 0.02))
        status_on = bool(_bool_from_signal(status, 0.5))

        condition_present = (
            (status_on and power < on_min)
            or (not status_on and power > off_max)
        )

        return _result(
            self.definition,
            condition_present,
            "Supply-fan power is inconsistent with fan status.",
            "Supply-fan power is consistent with fan status.",
            [
                FaultEvidence(point="Supply_Fan_Status", value=status),
                FaultEvidence(
                    point="Supply_Fan_Power",
                    value=power,
                    expected=f">= {on_min} kW when on; <= {off_max} kW when off",
                ),
            ],
        )


class LowSupplyAirflow(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.supply_fan.low_airflow",
        name="RTU low supply airflow",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: air-delivery performance while the supply fan is running. Detects airflow below a configured minimum and can indicate fan degradation, blocked filters/ducts, closed dampers, belt/mechanical problems, or incorrect airflow measurement."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        airflow = _number(_value(context, "Supply_Air_Flow_Sensor"))
        fan = _fan_running(context)

        if airflow is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required Supply_Air_Flow_Sensor or Supply_Fan_Status point is missing.",
            )

        minimum = float(_parameter(context, "minimum_supply_airflow_m3_s", 0.1))
        condition_present = fan and airflow < minimum

        return _result(
            self.definition,
            condition_present,
            "RTU fan is running but supply airflow is below the minimum threshold.",
            "RTU supply airflow is acceptable.",
            [
                FaultEvidence(
                    point="Supply_Air_Flow_Sensor",
                    value=airflow,
                    expected=f">= {minimum} m3/s while fan is on",
                )
            ],
        )


# ---------------------------------------------------------------------------
# Supply-air temperature / capacity response
# ---------------------------------------------------------------------------

class SupplyAirTemperatureDeviation(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.sat.setpoint_deviation",
        name="RTU supply-air temperature deviation",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: overall supply-air-temperature control performance. Detects sustained SAT error beyond tolerance while the fan is operating. This is a symptom-level rule that should be combined with heating, cooling, economizer, and airflow evidence to determine the likely root cause."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _fan_running(context)

        if sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required SAT, SAT setpoint, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "sat_tolerance_c", 2.0))
        deviation = abs(sat - sp)
        condition_present = fan and deviation > tolerance

        return _result(
            self.definition,
            condition_present,
            f"RTU SAT differs from setpoint by {deviation:.2f} C.",
            "RTU SAT is within setpoint tolerance.",
            [
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
                FaultEvidence(
                    point="Absolute_Deviation",
                    value=deviation,
                    expected=f"<= {tolerance} C",
                ),
            ],
        )


class CoolingIneffective(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.cooling.ineffective",
        name="RTU cooling ineffective",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: DX cooling effectiveness. Detects high cooling demand while SAT remains too warm. Intended to reveal inadequate compressor capacity, refrigerant/circuit issues, airflow problems, unfavorable entering conditions, control faults, or other causes of insufficient cooling response."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Cooling_Command"))
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _fan_running(context)

        if command is None or sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required cooling command, SAT, setpoint, or fan status is missing.",
            )

        command_threshold = float(_parameter(context, "cooling_high_threshold", 0.8))
        tolerance = float(_parameter(context, "sat_tolerance_c", 2.0))

        condition_present = (
            fan
            and command >= command_threshold
            and sat > sp + tolerance
        )

        return _result(
            self.definition,
            condition_present,
            "Cooling is heavily commanded but supply air remains too warm.",
            "Cooling response is consistent with SAT demand.",
            [
                FaultEvidence(point="Cooling_Command", value=command),
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
            ],
        )


class HeatingIneffective(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.heating.ineffective",
        name="RTU heating ineffective",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: RTU heating effectiveness. Detects high heating demand while SAT remains too cold. Intended to identify burner/furnace problems in gas RTUs, heat-pump capacity shortfall in heat-pump RTUs, airflow issues, or control/actuator failures."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Heating_Command"))
        sat = _number(_value(context, "Supply_Air_Temperature_Sensor"))
        sp = _number(_value(context, "Supply_Air_Temperature_Setpoint"))
        fan = _fan_running(context)

        if command is None or sat is None or sp is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required heating command, SAT, setpoint, or fan status is missing.",
            )

        command_threshold = float(_parameter(context, "heating_high_threshold", 0.8))
        tolerance = float(_parameter(context, "sat_tolerance_c", 2.0))

        condition_present = (
            fan
            and command >= command_threshold
            and sat < sp - tolerance
        )

        return _result(
            self.definition,
            condition_present,
            "Heating is heavily commanded but supply air remains too cold.",
            "Heating response is consistent with SAT demand.",
            [
                FaultEvidence(point="Heating_Command", value=command),
                FaultEvidence(point="Supply_Air_Temperature_Sensor", value=sat),
                FaultEvidence(point="Supply_Air_Temperature_Setpoint", value=sp),
            ],
        )


class SimultaneousHeatingCooling(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.heating_cooling.simultaneous",
        name="RTU simultaneous heating and cooling",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: conflicting thermal commands. Detects heating and cooling active at the same time, which usually indicates control conflict, sequence error, sensor/setpoint problems, or unnecessary energy use."
        ),
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
            "RTU heating and cooling commands are simultaneously active.",
            "RTU heating and cooling are not simultaneously active.",
            [
                FaultEvidence(point="Heating_Command", value=heating),
                FaultEvidence(point="Cooling_Command", value=cooling),
            ],
        )


# ---------------------------------------------------------------------------
# DX compressor / cooling diagnostics
# ---------------------------------------------------------------------------

class CoolingCommandNoCompressorPower(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.cooling.command_no_compressor_power",
        name="Cooling command with no compressor power",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: DX compressor response to cooling demand. Detects cooling command without corresponding compressor electrical power. Intended to reveal compressor/VFD/contactor failure, safeties/lockouts, control output failure, or incorrect compressor-power sensing."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Cooling_Command"))
        power = _number(_value(context, "Cooling_Compressor_Power"))

        if command is None or power is None:
            return _not_evaluable(
                self.definition,
                "Required Cooling_Command or Cooling_Compressor_Power point is missing.",
            )

        command_threshold = float(_parameter(context, "cooling_active_threshold", 0.2))
        minimum_power = float(_parameter(context, "minimum_compressor_power_kw", 0.1))

        condition_present = (
            command > command_threshold
            and power < minimum_power
        )

        return _result(
            self.definition,
            condition_present,
            "Cooling is active but compressor power is near zero.",
            "Compressor power is consistent with cooling demand.",
            [
                FaultEvidence(point="Cooling_Command", value=command),
                FaultEvidence(
                    point="Cooling_Compressor_Power",
                    value=power,
                    expected=f">= {minimum_power} kW when cooling is active",
                ),
            ],
        )


class CompressorPowerWithoutCooling(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.cooling.compressor_power_without_cooling",
        name="Compressor power without cooling",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: compressor shutdown/off-state behavior. Detects compressor electrical power while cooling command and cooling load are near zero. Intended to identify stuck contactors, sequencing errors, false power measurements, or unwanted compressor operation."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Cooling_Command"))
        power = _number(_value(context, "Cooling_Compressor_Power"))
        load = _number(_value(context, "Cooling_Thermal_Power_Sensor"))

        if command is None or power is None:
            return _not_evaluable(
                self.definition,
                "Required Cooling_Command or Cooling_Compressor_Power point is missing.",
            )

        off_threshold = float(_parameter(context, "cooling_off_threshold", 0.05))
        power_threshold = float(_parameter(context, "compressor_off_power_max_kw", 0.1))
        load_threshold = float(_parameter(context, "cooling_off_load_max_kw", 0.5))

        load_inactive = load is None or load <= load_threshold
        condition_present = (
            command <= off_threshold
            and power > power_threshold
            and load_inactive
        )

        evidence = [
            FaultEvidence(point="Cooling_Command", value=command),
            FaultEvidence(point="Cooling_Compressor_Power", value=power),
        ]
        if load is not None:
            evidence.append(FaultEvidence(point="Cooling_Thermal_Power_Sensor", value=load))

        return _result(
            self.definition,
            condition_present,
            "DX compressor consumes power while cooling is inactive.",
            "DX compressor power is consistent with cooling state.",
            evidence,
        )


class CoolingCOPOutOfRange(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.cooling.cop_out_of_range",
        name="Cooling COP out of expected range",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: cooling-system efficiency plausibility. Detects effective compressor COP outside a configured expected range while cooling is active. Useful for identifying degraded performance, bad load/power measurements, refrigerant problems, or model/calibration errors."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cop = _number(_value(context, "Cooling_Compressor_COP"))
        cooling = _number(_value(context, "Cooling_Part_Load_Ratio"))

        if cop is None or cooling is None:
            return _not_evaluable(
                self.definition,
                "Required Cooling_Compressor_COP or Cooling_Part_Load_Ratio point is missing.",
            )

        active_threshold = float(_parameter(context, "cooling_active_threshold", 0.05))
        min_cop = float(_parameter(context, "minimum_cooling_cop", 1.5))
        max_cop = float(_parameter(context, "maximum_cooling_cop", 8.0))

        condition_present = (
            cooling > active_threshold
            and not (min_cop <= cop <= max_cop)
        )

        return _result(
            self.definition,
            condition_present,
            f"Cooling COP {cop:.2f} is outside expected range.",
            "Cooling COP is within the expected range.",
            [
                FaultEvidence(point="Cooling_Compressor_COP", value=cop),
                FaultEvidence(
                    point="Expected_COP_Range",
                    value=f"{min_cop}..{max_cop}",
                ),
            ],
        )


class CoolingCapacityShortfall(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.cooling.capacity_shortfall",
        name="Cooling capacity shortfall",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: delivered-versus-available cooling capacity. Detects high cooling demand when delivered cooling remains materially below the model's available capacity. Intended to identify compressor/coil degradation, control limitation, airflow issues, or inconsistent capacity/load signals."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Cooling_Command"))
        delivered = _number(_value(context, "Cooling_Thermal_Power_Sensor"))
        available = _number(_value(context, "Available_Cooling_Capacity"))

        if command is None or delivered is None or available is None:
            return _not_evaluable(
                self.definition,
                "Required cooling command, delivered cooling, or available capacity point is missing.",
            )

        command_threshold = float(_parameter(context, "cooling_high_threshold", 0.8))
        minimum_fraction = float(_parameter(context, "minimum_delivered_capacity_fraction", 0.65))

        expected_min = available * minimum_fraction
        condition_present = (
            command >= command_threshold
            and available > 0.1
            and delivered < expected_min
        )

        return _result(
            self.definition,
            condition_present,
            "Delivered cooling is low relative to available capacity under high demand.",
            "Delivered cooling is reasonable relative to available capacity.",
            [
                FaultEvidence(point="Cooling_Command", value=command),
                FaultEvidence(point="Cooling_Thermal_Power_Sensor", value=delivered),
                FaultEvidence(
                    point="Available_Cooling_Capacity",
                    value=available,
                    expected=f"delivered >= {minimum_fraction:.2f} of available under high demand",
                ),
            ],
        )


class CompressorStageMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.cooling.stage_mismatch",
        name="Cooling compressor stage mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: staged-compressor sequencing. Detects disagreement between reported compressor stage and cooling part-load state. Intended for two-stage RTUs and useful for finding staging logic errors, failed stage outputs, or inconsistent diagnostic points."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        stage = _number(_value(context, "Cooling_Compressor_Stage"))
        plr = _number(_value(context, "Cooling_Part_Load_Ratio"))

        if stage is None or plr is None:
            return _not_evaluable(
                self.definition,
                "Required Cooling_Compressor_Stage or Cooling_Part_Load_Ratio point is missing.",
            )

        off_threshold = float(_parameter(context, "cooling_off_threshold", 0.05))
        active = plr > off_threshold
        stage_active = stage >= 1.0

        condition_present = active != stage_active

        return _result(
            self.definition,
            condition_present,
            "Cooling stage is inconsistent with cooling part-load ratio.",
            "Cooling stage is consistent with cooling part-load ratio.",
            [
                FaultEvidence(point="Cooling_Compressor_Stage", value=stage),
                FaultEvidence(point="Cooling_Part_Load_Ratio", value=plr),
            ],
        )


# ---------------------------------------------------------------------------
# Gas heating diagnostics
# ---------------------------------------------------------------------------

class GasHeatingCommandNoFuelInput(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.gas_heating.command_no_fuel_input",
        name="Gas heating command with no fuel input",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: gas-furnace response to heating demand. Detects heating command without meaningful gas input. Intended to identify ignition failure, gas-valve/safety lockout, burner failure, fuel-supply interruption, or incorrect gas-power/fuel-flow measurement."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Heating_Command"))
        gas_power = _number(_value(context, "Gas_Heating_Input_Power"))

        if command is None or gas_power is None:
            return _not_evaluable(
                self.definition,
                "Required Heating_Command or Gas_Heating_Input_Power point is missing.",
            )

        command_threshold = float(_parameter(context, "heating_active_threshold", 0.2))
        minimum_power = float(_parameter(context, "minimum_gas_heating_input_kw", 0.1))

        condition_present = (
            command > command_threshold
            and gas_power < minimum_power
        )

        return _result(
            self.definition,
            condition_present,
            "Gas heating is commanded but fuel input is near zero.",
            "Gas heating input is consistent with heating demand.",
            [
                FaultEvidence(point="Heating_Command", value=command),
                FaultEvidence(
                    point="Gas_Heating_Input_Power",
                    value=gas_power,
                    expected=f">= {minimum_power} kW when heating is active",
                ),
            ],
        )


class GasInputWithoutHeating(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.gas_heating.fuel_input_without_heating",
        name="Gas input without heating",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: gas-furnace off-state safety/energy consistency. Detects gas input while heating command and heating load are inactive. Intended to reveal valve leakage, control/sequence errors, incorrect fuel measurement, or unsafe/unwanted burner operation."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Heating_Command"))
        gas_power = _number(_value(context, "Gas_Heating_Input_Power"))
        load = _number(_value(context, "Heating_Thermal_Power_Sensor"))

        if command is None or gas_power is None:
            return _not_evaluable(
                self.definition,
                "Required Heating_Command or Gas_Heating_Input_Power point is missing.",
            )

        off_threshold = float(_parameter(context, "heating_off_threshold", 0.05))
        gas_threshold = float(_parameter(context, "gas_heating_off_power_max_kw", 0.1))
        load_threshold = float(_parameter(context, "heating_off_load_max_kw", 0.5))

        load_inactive = load is None or load <= load_threshold
        condition_present = (
            command <= off_threshold
            and gas_power > gas_threshold
            and load_inactive
        )

        evidence = [
            FaultEvidence(point="Heating_Command", value=command),
            FaultEvidence(point="Gas_Heating_Input_Power", value=gas_power),
        ]
        if load is not None:
            evidence.append(FaultEvidence(point="Heating_Thermal_Power_Sensor", value=load))

        return _result(
            self.definition,
            condition_present,
            "Gas input is present while heating is inactive.",
            "Gas input is consistent with heating state.",
            evidence,
        )


class GasHeatingEfficiencyMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.gas_heating.efficiency_mismatch",
        name="Gas heating efficiency mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: gas-heating energy conversion consistency. Compares delivered heating to gas input and flags an efficiency outside the configured range. Useful for degraded combustion/heat transfer, incorrect efficiency assumptions, sensor errors, or inconsistent energy signals."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        delivered = _number(_value(context, "Heating_Thermal_Power_Sensor"))
        gas_power = _number(_value(context, "Gas_Heating_Input_Power"))

        if delivered is None or gas_power is None:
            return _not_evaluable(
                self.definition,
                "Required Heating_Thermal_Power_Sensor or Gas_Heating_Input_Power point is missing.",
            )

        minimum_input = float(_parameter(context, "minimum_gas_efficiency_test_input_kw", 1.0))
        min_eff = float(_parameter(context, "minimum_gas_heating_efficiency", 0.65))
        max_eff = float(_parameter(context, "maximum_gas_heating_efficiency", 1.0))

        efficiency = delivered / gas_power if gas_power > 0 else 0.0
        condition_present = (
            gas_power >= minimum_input
            and not (min_eff <= efficiency <= max_eff)
        )

        return _result(
            self.definition,
            condition_present,
            f"Gas heating efficiency {efficiency:.3f} is outside expected range.",
            "Gas heating efficiency is within the expected range.",
            [
                FaultEvidence(point="Heating_Thermal_Power_Sensor", value=delivered),
                FaultEvidence(point="Gas_Heating_Input_Power", value=gas_power),
                FaultEvidence(
                    point="Calculated_Heating_Efficiency",
                    value=efficiency,
                    expected=f"{min_eff}..{max_eff}",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Optional heat-pump heating diagnostics
# ---------------------------------------------------------------------------

class HeatPumpCommandNoHeatingCompressorPower(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.heat_pump.command_no_heating_compressor_power",
        name="Heat-pump command with no heating compressor power",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: heat-pump compressor response in heating mode. Detects heating demand without heating-compressor electrical power. Intended to identify compressor/reversing-cycle lockout, controls failure, safeties, contactor/VFD failure, or incorrect power sensing."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.CRITICAL,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        command = _number(_value(context, "Heating_Command"))
        power = _number(_value(context, "Heating_Compressor_Power"))

        if command is None or power is None:
            return _not_evaluable(
                self.definition,
                "Heat-pump rule requires Heating_Command and Heating_Compressor_Power.",
            )

        command_threshold = float(_parameter(context, "heating_active_threshold", 0.2))
        minimum_power = float(_parameter(context, "minimum_heating_compressor_power_kw", 0.1))

        condition_present = (
            command > command_threshold
            and power < minimum_power
        )

        return _result(
            self.definition,
            condition_present,
            "Heat-pump heating is commanded but compressor power is near zero.",
            "Heat-pump compressor power is consistent with heating demand.",
            [
                FaultEvidence(point="Heating_Command", value=command),
                FaultEvidence(point="Heating_Compressor_Power", value=power),
            ],
        )


class HeatingCOPOutOfRange(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.heat_pump.heating_cop_out_of_range",
        name="Heat-pump heating COP out of range",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: heat-pump heating efficiency plausibility. Detects heating COP outside the expected range while heating is active. Useful for degraded heat-pump performance, defrost/low-ambient issues, sensor errors, or model/calibration problems."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        cop = _number(_value(context, "Heating_COP"))
        plr = _number(_value(context, "Heating_Part_Load_Ratio"))

        if cop is None or plr is None:
            return _not_evaluable(
                self.definition,
                "Heat-pump rule requires Heating_COP and Heating_Part_Load_Ratio.",
            )

        active_threshold = float(_parameter(context, "heating_active_threshold", 0.05))
        min_cop = float(_parameter(context, "minimum_heating_cop", 1.0))
        max_cop = float(_parameter(context, "maximum_heating_cop", 6.0))

        condition_present = (
            plr > active_threshold
            and not (min_cop <= cop <= max_cop)
        )

        return _result(
            self.definition,
            condition_present,
            f"Heat-pump heating COP {cop:.2f} is outside expected range.",
            "Heat-pump heating COP is within expected range.",
            [
                FaultEvidence(point="Heating_COP", value=cop),
                FaultEvidence(point="Heating_Part_Load_Ratio", value=plr),
            ],
        )


# ---------------------------------------------------------------------------
# Economizer / mixing / ventilation
# ---------------------------------------------------------------------------

class OutdoorAirDamperCommandPositionMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.economizer.damper_command_position_mismatch",
        name="RTU outdoor-air damper command/position mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: outdoor-air damper actuator tracking. Detects a sustained difference between economizer command and actual damper position. Intended to identify stuck/binding dampers, failed actuators/linkages, override conditions, or incorrect position feedback."
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
            "Outdoor-air damper position tracks command.",
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


class MixedAirTemperatureOutOfRange(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.economizer.mixed_air_temperature_out_of_range",
        name="RTU mixed-air temperature out of physical range",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: physical plausibility of mixed-air temperature. Detects TMix outside the range bounded by outdoor- and return-air temperatures while the fan is operating. Strongly suggests sensor error, incorrect point association, or invalid mixing/airflow behavior."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        t_out = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        t_ret = _number(_value(context, "Return_Air_Temperature_Sensor"))
        t_mix = _number(_value(context, "Mixed_Air_Temperature_Sensor"))
        fan = _fan_running(context)

        if t_out is None or t_ret is None or t_mix is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required OA/RA/mixed-air temperatures or fan status is missing.",
            )

        tolerance = float(_parameter(context, "mixed_air_physical_tolerance_c", 1.0))
        lower = min(t_out, t_ret) - tolerance
        upper = max(t_out, t_ret) + tolerance

        condition_present = fan and not (lower <= t_mix <= upper)

        return _result(
            self.definition,
            condition_present,
            "Mixed-air temperature lies outside the physical OA/RA mixing range.",
            "Mixed-air temperature is physically plausible.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=t_out),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=t_ret),
                FaultEvidence(
                    point="Mixed_Air_Temperature_Sensor",
                    value=t_mix,
                    expected=f"{lower:.2f}..{upper:.2f} C",
                ),
            ],
        )


class MixedAirTemperatureMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.economizer.mixed_air_temperature_mismatch",
        name="RTU mixed-air temperature mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: economizer/mixing consistency. Compares measured mixed-air temperature with the temperature predicted from outdoor-air fraction, outdoor temperature, and return temperature. Intended to identify damper-position errors, poor mixing, sensor bias, or mapping problems."
        ),
        persistence_seconds=180.0,
        clear_seconds=90.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        t_out = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        t_ret = _number(_value(context, "Return_Air_Temperature_Sensor"))
        t_mix = _number(_value(context, "Mixed_Air_Temperature_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        fan = _fan_running(context)

        if (
            t_out is None
            or t_ret is None
            or t_mix is None
            or damper is None
            or fan is None
        ):
            return _not_evaluable(
                self.definition,
                "Required OA/RA/mixed-air temperatures, OA damper position, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "mixed_air_temperature_tolerance_c", 2.0))
        oa_fraction = max(0.0, min(1.0, damper))
        expected = oa_fraction * t_out + (1.0 - oa_fraction) * t_ret
        deviation = abs(t_mix - expected)

        condition_present = fan and deviation > tolerance

        return _result(
            self.definition,
            condition_present,
            f"Mixed-air temperature differs from expected mixing temperature by {deviation:.2f} C.",
            "Mixed-air temperature agrees with expected OA/RA mixing.",
            [
                FaultEvidence(point="Mixed_Air_Temperature_Sensor", value=t_mix),
                FaultEvidence(
                    point="Expected_Mixed_Air_Temperature",
                    value=expected,
                    expected=f"within +/- {tolerance} C",
                ),
            ],
        )


class EconomizerNotUsingFreeCooling(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.economizer.not_using_free_cooling",
        name="RTU economizer not using free cooling",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: economizer free-cooling utilization. Detects favorable outdoor conditions and cooling demand while the outdoor-air damper stays near minimum. Intended to identify disabled/stuck economizer operation, bad control logic, actuator faults, or incorrect outdoor-air sensing."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        t_out = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        t_ret = _number(_value(context, "Return_Air_Temperature_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        cooling = _number(_value(context, "Cooling_Command"))
        fan = _fan_running(context)

        if (
            t_out is None
            or t_ret is None
            or damper is None
            or cooling is None
            or fan is None
        ):
            return _not_evaluable(
                self.definition,
                "Required OA/RA temperatures, damper position, cooling command, or fan status is missing.",
            )

        diff = float(_parameter(context, "economizer_temperature_differential_c", 1.0))
        min_oa = float(_parameter(context, "minimum_outdoor_air_fraction", 0.15))
        damper_tol = float(_parameter(context, "economizer_damper_tolerance", 0.05))
        cooling_threshold = float(_parameter(context, "cooling_active_threshold", 0.2))

        favorable = t_out < t_ret - diff
        condition_present = (
            fan
            and favorable
            and cooling > cooling_threshold
            and damper <= min_oa + damper_tol
        )

        return _result(
            self.definition,
            condition_present,
            "Free cooling is available but OA damper remains near minimum.",
            "Economizer operation is consistent with current conditions.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=t_out),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=t_ret),
                FaultEvidence(point="Outside_Air_Damper_Position_Sensor", value=damper),
                FaultEvidence(point="Cooling_Command", value=cooling),
            ],
        )


class EconomizerExcessiveOutdoorAir(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.economizer.excessive_outdoor_air",
        name="RTU excessive outdoor air",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: unnecessary outdoor-air intake. Detects the outdoor-air damper substantially above minimum when economizer conditions are unfavorable. Intended to identify stuck/overridden dampers, control errors, or excess ventilation causing avoidable heating/cooling energy use."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        t_out = _number(_value(context, "Outside_Air_Temperature_Sensor"))
        t_ret = _number(_value(context, "Return_Air_Temperature_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        fan = _fan_running(context)

        if t_out is None or t_ret is None or damper is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required OA/RA temperatures, OA damper position, or fan status is missing.",
            )

        diff = float(_parameter(context, "economizer_temperature_differential_c", 1.0))
        min_oa = float(_parameter(context, "minimum_outdoor_air_fraction", 0.15))
        excess = float(_parameter(context, "excess_outdoor_air_margin", 0.15))

        unfavorable = t_out >= t_ret - diff
        condition_present = (
            fan
            and unfavorable
            and damper > min_oa + excess
        )

        return _result(
            self.definition,
            condition_present,
            "Outdoor-air damper is excessively open under unfavorable conditions.",
            "Outdoor-air damper position is reasonable for current conditions.",
            [
                FaultEvidence(point="Outside_Air_Temperature_Sensor", value=t_out),
                FaultEvidence(point="Return_Air_Temperature_Sensor", value=t_ret),
                FaultEvidence(point="Outside_Air_Damper_Position_Sensor", value=damper),
            ],
        )


class OutdoorAirflowDamperMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.ventilation.outdoor_airflow_damper_mismatch",
        name="RTU outdoor-airflow/damper mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: consistency between outdoor-airflow measurement and damper/supply-airflow state. Detects measured outdoor airflow that does not agree with the expected fraction of supply flow. Useful for identifying airflow-sensor bias, damper/actuator issues, leakage, or mapping errors."
        ),
        persistence_seconds=180.0,
        clear_seconds=90.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        out_flow = _number(_value(context, "Outside_Air_Flow_Sensor"))
        sup_flow = _number(_value(context, "Supply_Air_Flow_Sensor"))
        damper = _number(_value(context, "Outside_Air_Damper_Position_Sensor"))
        fan = _fan_running(context)

        if out_flow is None or sup_flow is None or damper is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required OA flow, supply flow, damper position, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "outdoor_airflow_fraction_tolerance", 0.10))
        expected = max(0.0, sup_flow) * max(0.0, min(1.0, damper))
        scale = max(0.05, sup_flow)
        normalized_error = abs(out_flow - expected) / scale

        condition_present = (
            fan
            and normalized_error > tolerance
        )

        return _result(
            self.definition,
            condition_present,
            "Outdoor-air flow is inconsistent with supply flow and damper position.",
            "Outdoor-air flow is consistent with supply flow and damper position.",
            [
                FaultEvidence(point="Outside_Air_Flow_Sensor", value=out_flow),
                FaultEvidence(point="Supply_Air_Flow_Sensor", value=sup_flow),
                FaultEvidence(point="Outside_Air_Damper_Position_Sensor", value=damper),
                FaultEvidence(
                    point="Normalized_Outdoor_Airflow_Error",
                    value=normalized_error,
                    expected=f"<= {tolerance}",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Static pressure / VAV reset
# ---------------------------------------------------------------------------

class SupplyStaticPressureHigh(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.static_pressure.high",
        name="RTU supply static pressure high",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: supply-duct static-pressure control. Detects pressure persistently above setpoint. Intended to identify fan-control tuning issues, excessive fan speed, blocked/closed downstream paths, reset problems, or sensor/setpoint errors that can waste fan energy."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        pressure = _number(_value(context, "Supply_Air_Static_Pressure_Sensor"))
        setpoint = _number(_value(context, "Supply_Air_Static_Pressure_Setpoint"))
        fan = _fan_running(context)

        if pressure is None or setpoint is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required static pressure, setpoint, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "static_pressure_tolerance_pa", 75.0))
        deviation = pressure - setpoint
        condition_present = fan and deviation > tolerance

        return _result(
            self.definition,
            condition_present,
            f"RTU static pressure is {deviation:.1f} Pa above setpoint.",
            "RTU static pressure is not excessively high.",
            [
                FaultEvidence(point="Supply_Air_Static_Pressure_Sensor", value=pressure),
                FaultEvidence(point="Supply_Air_Static_Pressure_Setpoint", value=setpoint),
            ],
        )


class SupplyStaticPressureLow(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.static_pressure.low",
        name="RTU supply static pressure low",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: supply-duct static-pressure control. Detects pressure persistently below setpoint while the fan is running. Intended to identify insufficient fan capacity/speed, duct leakage, open dampers/high demand, filter/air-path restrictions, or static-pressure sensing problems."
        ),
        persistence_seconds=300.0,
        clear_seconds=120.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        pressure = _number(_value(context, "Supply_Air_Static_Pressure_Sensor"))
        setpoint = _number(_value(context, "Supply_Air_Static_Pressure_Setpoint"))
        fan = _fan_running(context)

        if pressure is None or setpoint is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required static pressure, setpoint, or fan status is missing.",
            )

        tolerance = float(_parameter(context, "static_pressure_tolerance_pa", 75.0))
        deviation = setpoint - pressure
        condition_present = fan and deviation > tolerance

        return _result(
            self.definition,
            condition_present,
            f"RTU static pressure is {deviation:.1f} Pa below setpoint.",
            "RTU static pressure is not excessively low.",
            [
                FaultEvidence(point="Supply_Air_Static_Pressure_Sensor", value=pressure),
                FaultEvidence(point="Supply_Air_Static_Pressure_Setpoint", value=setpoint),
            ],
        )


class StaticPressureResetMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.static_pressure.reset_mismatch",
        name="RTU static-pressure reset mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: VAV static-pressure reset sequence. Compares the most-open downstream VAV damper with the expected RTU static-pressure setpoint. Intended to identify reset-sequence faults, incorrect VAV aggregation, bad setpoint mapping, or unnecessarily high/low fan pressure targets."
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
        tolerance = float(_parameter(context, "static_pressure_reset_tolerance_pa", 50.0))

        expected_fraction = max(
            0.0,
            min(
                1.0,
                (damper - damper_low) / max(0.01, damper_high - damper_low),
            ),
        )
        expected_sp = sp_min + (sp_max - sp_min) * expected_fraction
        deviation = abs(setpoint - expected_sp)
        condition_present = deviation > tolerance

        return _result(
            self.definition,
            condition_present,
            f"RTU static-pressure setpoint differs from VAV reset expectation by {deviation:.1f} Pa.",
            "RTU static-pressure setpoint is consistent with VAV reset.",
            [
                FaultEvidence(point="Most_Open_VAV_Damper_Position", value=damper),
                FaultEvidence(point="Supply_Air_Static_Pressure_Setpoint", value=setpoint),
                FaultEvidence(
                    point="Expected_Static_Pressure_Setpoint",
                    value=expected_sp,
                    expected=f"within +/- {tolerance} Pa",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Whole-unit power consistency
# ---------------------------------------------------------------------------

class TotalElectricPowerComponentMismatch(FaultRule):
    definition = FaultDefinition(
        rule_id="rtu.energy.total_power_component_mismatch",
        name="RTU total electric power/component mismatch",
        equipment_type="Rooftop_Unit",
        description=(
            "Target: RTU electrical-energy signal consistency. Compares reported total electric power with the sum of available component powers such as supply fan and cooling/heating compressors. Useful for detecting missing loads, double counting, unit/mapping errors, or bad power signals."
        ),
        persistence_seconds=120.0,
        clear_seconds=60.0,
        severity=FaultSeverity.WARNING,
    )

    def evaluate(self, context: FaultContext) -> FaultResult:
        total = _number(_value(context, "Total_Electric_Power"))
        fan = _number(_value(context, "Supply_Fan_Power"))
        cooling = _number(_value(context, "Cooling_Compressor_Power"))
        heating = _number(_value(context, "Heating_Compressor_Power"))

        if total is None or fan is None:
            return _not_evaluable(
                self.definition,
                "Required Total_Electric_Power and Supply_Fan_Power points are missing.",
            )

        components = fan
        used = ["Supply_Fan_Power"]

        if cooling is not None:
            components += cooling
            used.append("Cooling_Compressor_Power")

        if heating is not None:
            components += heating
            used.append("Heating_Compressor_Power")

        tolerance_kw = float(_parameter(context, "total_power_component_tolerance_kw", 0.25))
        tolerance_fraction = float(_parameter(context, "total_power_component_tolerance_fraction", 0.10))
        allowed = max(tolerance_kw, max(total, components) * tolerance_fraction)
        deviation = abs(total - components)

        condition_present = deviation > allowed

        evidence = [
            FaultEvidence(point="Total_Electric_Power", value=total),
            FaultEvidence(
                point="Calculated_Component_Power_Sum",
                value=components,
                expected=f"within +/- {allowed:.3f} kW",
            ),
        ]
        for name in used:
            evidence.append(FaultEvidence(point=name, value=_value(context, name)))

        return _result(
            self.definition,
            condition_present,
            f"RTU total electric power differs from component sum by {deviation:.3f} kW.",
            "RTU total electric power is consistent with available component powers.",
            evidence,
        )


# ---------------------------------------------------------------------------
# Semantic coverage / integration helpers
# ---------------------------------------------------------------------------

def semantic_resolution_report(
    context: FaultContext,
) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}

    for canonical_key, metadata in CANONICAL_SEMANTICS.items():
        resolved_key = None
        value = context.value(canonical_key)

        if value is not None:
            resolved_key = canonical_key
        else:
            for alias in _semantic_aliases(context, canonical_key):
                alias_value = context.value(alias)
                if alias_value is not None:
                    resolved_key = alias
                    value = alias_value
                    break

        report[canonical_key] = {
            "resolved": resolved_key is not None,
            "resolved_key": resolved_key,
            "value": value,
            "brick": metadata.get("brick"),
            "description": metadata.get("description"),
        }

    return report


def semantic_coverage_fraction(context: FaultContext) -> float:
    report = semantic_resolution_report(context)

    if not report:
        return 1.0

    resolved = sum(
        1
        for item in report.values()
        if bool(item["resolved"])
    )
    return resolved / len(report)


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

# Core rules that apply to the current gas/DX RTU model.
RTU_GAS_FAULT_RULES: tuple[type[FaultRule], ...] = (
    SupplyFanCommandStatusMismatch,
    SupplyFanFailedToStop,
    SupplyFanPowerStatusMismatch,
    LowSupplyAirflow,
    SupplyAirTemperatureDeviation,
    CoolingIneffective,
    HeatingIneffective,
    SimultaneousHeatingCooling,
    CoolingCommandNoCompressorPower,
    CompressorPowerWithoutCooling,
    CoolingCOPOutOfRange,
    CoolingCapacityShortfall,
    CompressorStageMismatch,
    GasHeatingCommandNoFuelInput,
    GasInputWithoutHeating,
    GasHeatingEfficiencyMismatch,
    OutdoorAirDamperCommandPositionMismatch,
    MixedAirTemperatureOutOfRange,
    MixedAirTemperatureMismatch,
    EconomizerNotUsingFreeCooling,
    EconomizerExcessiveOutdoorAir,
    OutdoorAirflowDamperMismatch,
    SupplyStaticPressureHigh,
    SupplyStaticPressureLow,
    StaticPressureResetMismatch,
    TotalElectricPowerComponentMismatch,
)

# Optional heat-pump-specific rules. Register these only for a heat-pump RTU
# or leave them registered and allow them to return evaluable=False when the
# required heat-pump semantics are absent.
RTU_HEAT_PUMP_FAULT_RULES: tuple[type[FaultRule], ...] = (
    HeatPumpCommandNoHeatingCompressorPower,
    HeatingCOPOutOfRange,
)

# Generic union if your engine prefers one registry and handles evaluability.
RTU_FAULT_RULES: tuple[type[FaultRule], ...] = (
    *RTU_GAS_FAULT_RULES,
    *RTU_HEAT_PUMP_FAULT_RULES,
)
