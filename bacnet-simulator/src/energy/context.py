from __future__ import annotations

from typing import Any

from .equipment.chiller import ChillerSnapshot
from .equipment.ahu import AHUSnapshot
from .equipment.lighting import LightingSnapshot
from .equipment.boiler import BoilerSnapshot


def build_chiller_snapshot(
    values: dict[str, Any],
) -> ChillerSnapshot:
    return ChillerSnapshot(
        # Temporarily disabled because your plant device has multiple
        # Run_Status and Power_Sensor points.
        running=None,
        measured_power_kw=None,

        measured_energy_kwh=_optional_float(
            values.get("Energy_Sensor")
        ),

        load_percent=_optional_float(
            values.get("Cooling_Load_Percentage")
        ),

        entering_water_temperature_c=_optional_float(
            values.get(
                "Entering_Chilled_Water_Temperature_Sensor"
            )
        ),

        leaving_water_temperature_c=_optional_float(
            values.get(
                "Leaving_Chilled_Water_Temperature_Sensor"
            )
        ),

        water_flow_liters_per_second=_optional_float(
            values.get("Water_Flow_Sensor")
        ),

        measured_cooling_load_kw=_optional_float(
            values.get("Cooling_Thermal_Power_Sensor")
        ),

        measured_cop=_optional_float(
            values.get(
                "Coefficient_Of_Performance_Sensor"
            )
        ),

        outdoor_air_temperature_c=_optional_float(
            values.get(
                "Outside_Air_Temperature_Sensor"
            )
        ),

        condenser_entering_temperature_c=_optional_float(
            values.get(
                "Condenser_Water_Temperature_Sensor"
            )
        ),
    )


def build_ahu_snapshot(
    values: dict[str, Any],
    fan_points: dict[str, Any] | None = None,
) -> AHUSnapshot:
    """fan_points, when given, is SemanticResolver.resolve_ahu_fans()'s
    result -- present ONLY for the fan fields whose Supply_Fan/Return_Fan
    semantic entity + point actually resolved. Each of the 4 fan fields
    below is merged per-field: use the resolved value if that specific key
    is present, else fall back to the legacy Supply_Fan_Status/
    Supply_Fan_Speed_Command/etc alias lookup, byte-for-byte unchanged from
    before Brick Core. A device with no semantic entities at all (fan_points
    empty/None) behaves exactly as before; a partially-migrated AHU (say
    only the supply fan has entities) gets entity-resolved supply values
    *and* legacy-fallback return values in the same snapshot."""
    fan_points = fan_points or {}

    def fan_field(key: str, legacy_key: str, convert: Any) -> Any:
        if key in fan_points:
            return convert(fan_points[key])
        return convert(values.get(legacy_key))

    return AHUSnapshot(
        # Optional measured AHU electrical power.
        measured_power_kw=_optional_float(
            values.get("Power_Sensor")
        ),

        # Supply fan
        supply_fan_running=fan_field(
            "supply_fan_running", "Supply_Fan_Status", _optional_bool
        ),

        supply_fan_speed_percent=fan_field(
            "supply_fan_speed_percent", "Supply_Fan_Speed_Command", _optional_float
        ),

        # Return fan
        return_fan_running=fan_field(
            "return_fan_running", "Return_Fan_Status", _optional_bool
        ),

        return_fan_speed_percent=fan_field(
            "return_fan_speed_percent", "Return_Fan_Speed_Command", _optional_float
        ),

        # Airflow
        supply_air_flow_cfm=_optional_float(
            values.get("Supply_Air_Flow_Sensor")
            or values.get("Air_Flow_Sensor")
        ),

        # Air temperatures
        supply_air_temperature_c=_optional_float(
            values.get(
                "Supply_Air_Temperature_Sensor"
            )
        ),

        return_air_temperature_c=_optional_float(
            values.get(
                "Return_Air_Temperature_Sensor"
            )
        ),

        mixed_air_temperature_c=_optional_float(
            values.get(
                "Mixed_Air_Temperature_Sensor"
            )
        ),

        outside_air_temperature_c=_optional_float(
            values.get(
                "Outside_Air_Temperature_Sensor"
            )
        ),

        # Optional coil control information.
        cooling_valve_position_percent=_optional_float(
            values.get(
                "Cooling_Valve_Position_Command"
            )
        ),

        heating_valve_position_percent=_optional_float(
            values.get(
                "Heating_Valve_Position_Command"
            )
        ),
    )

def build_lighting_snapshot(
    *,
    objects: list[dict[str, Any]],
    simulation_engine: Any,
    instance_key: str,
    resolver: Any = None,
    device_id: int | None = None,
) -> LightingSnapshot:
    """When resolver/device_id are given, resolves the zone's points via
    its Lighting_Zone semantic entity (resolver.get_lighting_zone_entity())
    -- one field at a time, exactly like build_ahu_snapshot's fan_points
    merge. Any individual field whose point isn't isPointOf the zone yet
    falls back to the legacy Zone-A/Zone-B object-name-prefix scan below,
    unchanged. Without resolver/device_id (or with no matching zone entity)
    every field falls back to the legacy scan, exactly as before Brick Core."""
    zone_points: dict[str, Any] = {}
    if resolver is not None and device_id is not None:
        zone_entity = resolver.get_lighting_zone_entity(device_id, instance_key)
        if zone_entity is not None:
            for point in resolver.get_entity_points(zone_entity["id"]):
                zone_points[point.brick_class] = point.value

    prefix = {
        "zone-a": "Zone-A",
        "zone-b": "Zone-B",
    }[instance_key]

    def legacy_value(name: str) -> Any:
        target = f"{prefix}-{name}"

        for obj in objects:
            if obj.get("name") == target:
                return simulation_engine.get_object_value(
                    int(obj["id"])
                )

        return None

    def value(brick_class: str, legacy_name: str) -> Any:
        if brick_class in zone_points:
            return zone_points[brick_class]
        return legacy_value(legacy_name)

    return LightingSnapshot(
        measured_power_kw=_optional_float(
            value("Power_Sensor", "Power")
        ),

        measured_energy_kwh=None,

        on=_optional_bool(
            value("On_Off_Command", "Lights-On")
        ),

        occupancy=None,

        lighting_level_fraction=None,

        lighting_level_percent=_optional_float(
            value("Lighting_Level_Command", "Dim-Level")
        ),
    )

def build_boiler_snapshot(
    values: dict[str, Any],
) -> BoilerSnapshot:
    return BoilerSnapshot(
        # Temporarily do not use Run_Status because this plant has
        # BLR-1, BLR-2 and pump Run_Status points.
        running=None,

        firing_fraction=None,
        firing_percent=None,

        entering_water_temperature_c=_optional_float(
            values.get(
                "Entering_Hot_Water_Temperature_Sensor"
            )
        ),

        leaving_water_temperature_c=_optional_float(
            values.get(
                "Leaving_Hot_Water_Temperature_Sensor"
            )
        ),

        water_flow_liters_per_second=_optional_float(
            values.get("Water_Flow_Sensor")
        ),

        water_flow_kg_per_second=None,

        measured_thermal_output_kw=None,
        measured_fuel_input_kw=None,

        # Your seeded Gas-Flow point is cubic-feet-per-minute.
        natural_gas_flow_cubic_meters_per_hour=None,

        natural_gas_flow_cubic_feet_per_minute=_optional_float(
            values.get("Natural_Gas_Flow_Sensor")
        ),

        measured_auxiliary_electric_power_kw=None,
    )
def _optional_float(
    value: Any,
) -> float | None:
    if value is None:
        return None

    try:
        return float(value)

    except (TypeError, ValueError):
        return None


def _optional_bool(
    value: Any,
) -> bool | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    normalized = str(value).strip().lower()

    if normalized in {
        "1",
        "true",
        "active",
        "on",
        "running",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "inactive",
        "off",
        "stopped",
    }:
        return False

    return None