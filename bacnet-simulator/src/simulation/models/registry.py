from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from .chiller import ChillerModel, ChillerParameters
from .vav import VAVModel, VAVParameters


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    label: str
    type: str = "number"
    default: Any = None
    unit: str | None = None
    required: bool = False
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    label: str
    direction: str
    unit: str | None = None
    required: bool = True
    suggested_point_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelDefinition:
    model_type: str
    label: str
    provider_type: str
    description: str
    parameters: tuple[ParameterDefinition, ...]
    variables: tuple[VariableDefinition, ...]
    factory: Callable[[dict[str, Any]], Any]

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "model_type": self.model_type,
            "label": self.label,
            "provider_type": self.provider_type,
            "description": self.description,
            "parameters": [asdict(p) for p in self.parameters],
            "inputs": [
                asdict(v) for v in self.variables if v.direction == "input"
            ],
            "outputs": [
                asdict(v) for v in self.variables if v.direction == "output"
            ],
        }


def _make_vav(parameters: dict[str, Any]) -> VAVModel:
    return VAVModel(VAVParameters(**parameters))


def _make_chiller(parameters: dict[str, Any]) -> ChillerModel:
    return ChillerModel(ChillerParameters(**parameters))


MODEL_REGISTRY: dict[str, ModelDefinition] = {
    "vav": ModelDefinition(
        model_type="vav",
        label="VAV with Reheat",
        provider_type="system",
        description=(
            "Simplified pressure-independent VAV + thermal zone model with "
            "cooling airflow, reheat, occupancy, and CO2 response."
        ),
        parameters=(
            ParameterDefinition(
                "min_airflow_cfm", "Minimum Airflow",
                default=150.0, unit="cfm", minimum=0,
            ),
            ParameterDefinition(
                "max_airflow_cfm", "Maximum Airflow",
                default=500.0, unit="cfm", minimum=1,
            ),
            ParameterDefinition(
                "zone_thermal_time_constant_s", "Zone Thermal Response",
                default=1800.0, unit="s", minimum=1, advanced=True,
            ),
            ParameterDefinition(
                "cooling_proportional_band_c", "Cooling Proportional Band",
                default=2.0, unit="°C", minimum=0.01, advanced=True,
            ),
            ParameterDefinition(
                "heating_proportional_band_c", "Heating Proportional Band",
                default=2.0, unit="°C", minimum=0.01, advanced=True,
            ),
            ParameterDefinition(
                "max_reheat_temp_rise_c", "Maximum Reheat Temperature Rise",
                default=15.0, unit="°C", minimum=0, advanced=True,
            ),
            ParameterDefinition(
                "outdoor_co2_ppm", "Outdoor CO2",
                default=420.0, unit="ppm", minimum=0, advanced=True,
            ),
        ),
        variables=(
            VariableDefinition(
                "cooling_setpoint_c", "Cooling Setpoint", "input", "°C",
                suggested_point_types=("Cooling_Temperature_Setpoint",),
            ),
            VariableDefinition(
                "heating_setpoint_c", "Heating Setpoint", "input", "°C",
                suggested_point_types=("Heating_Temperature_Setpoint",),
            ),
            VariableDefinition(
                "supply_air_temp_c", "Supply Air Temperature", "input", "°C",
                suggested_point_types=("Supply_Air_Temperature_Sensor",),
            ),
            VariableDefinition(
                "occupied", "Occupancy", "input",
                suggested_point_types=("Occupancy_Sensor",),
            ),
            VariableDefinition(
                "zone_temp_c", "Zone Temperature", "output", "°C",
                suggested_point_types=("Zone_Air_Temperature_Sensor",),
            ),
            VariableDefinition(
                "damper_command_pct", "Damper Command", "output", "%",
                suggested_point_types=("Damper_Position_Command",),
            ),
            VariableDefinition(
                "zone_airflow_cfm", "Zone Airflow", "output", "cfm",
                suggested_point_types=("Air_Flow_Sensor",),
            ),
            VariableDefinition(
                "reheat_valve_pct", "Reheat Valve", "output", "%",
                suggested_point_types=("Valve_Position_Command",),
            ),
            VariableDefinition(
                "zone_co2_ppm", "Zone CO2", "output", "ppm",
                suggested_point_types=("CO2_Level_Sensor",),
            ),
        ),
        factory=_make_vav,
    ),
    "chiller": ModelDefinition(
        model_type="chiller",
        label="Water-Cooled Chiller",
        provider_type="system",
        description=(
            "Simplified dynamic water-cooled chiller model with capacity, "
            "part-load COP, condenser-temperature effect, and CHW response."
        ),
        parameters=(
            ParameterDefinition(
                "capacity_kw", "Cooling Capacity",
                default=500.0, unit="kW", minimum=1,
            ),
            ParameterDefinition(
                "nominal_cop", "Nominal COP",
                default=5.5, minimum=0.1,
            ),
            ParameterDefinition(
                "minimum_plr", "Minimum PLR",
                default=0.15, minimum=0.0, maximum=1.0,
            ),
            ParameterDefinition(
                "maximum_plr", "Maximum PLR",
                default=1.0, minimum=0.0, maximum=1.0, advanced=True,
            ),
            ParameterDefinition(
                "leaving_temp_time_constant_s", "Leaving Water Response",
                default=45.0, unit="s", minimum=0.01, advanced=True,
            ),
            ParameterDefinition(
                "minimum_flow_kg_s", "Minimum Flow",
                default=2.0, unit="kg/s", minimum=0, advanced=True,
            ),
        ),
        variables=(
            VariableDefinition("enable", "Enable", "input"),
            VariableDefinition(
                "chw_return_temp_c", "CHW Return Temperature", "input", "°C",
                suggested_point_types=("Entering_Chilled_Water_Temperature_Sensor",),
            ),
            VariableDefinition(
                "chw_setpoint_c", "CHW Setpoint", "input", "°C",
                suggested_point_types=("Chilled_Water_Temperature_Setpoint",),
            ),
            VariableDefinition(
                "chw_flow_kg_s", "CHW Flow", "input", "kg/s",
                suggested_point_types=("Water_Flow_Sensor",),
            ),
            VariableDefinition(
                "condenser_entering_temp_c",
                "Condenser Entering Water Temperature", "input", "°C",
            ),
            VariableDefinition(
                "run", "Run Status", "output",
                suggested_point_types=("Run_Status",),
            ),
            VariableDefinition(
                "chw_leaving_temp_c", "CHW Leaving Temperature", "output", "°C",
                suggested_point_types=("Leaving_Chilled_Water_Temperature_Sensor",),
            ),
            VariableDefinition("cooling_kw", "Cooling Output", "output", "kW"),
            VariableDefinition(
                "power_kw", "Electrical Power", "output", "kW",
                suggested_point_types=("Power_Sensor",),
            ),
            VariableDefinition("cop", "COP", "output"),
            VariableDefinition("plr", "Part Load Ratio", "output"),
        ),
        factory=_make_chiller,
    ),
}


def get_model_definition(model_type: str) -> ModelDefinition:
    try:
        return MODEL_REGISTRY[model_type]
    except KeyError as exc:
        raise ValueError(f"Unknown simulation model type: {model_type}") from exc


def get_model_catalog() -> list[dict[str, Any]]:
    return [
        definition.catalog_entry()
        for definition in MODEL_REGISTRY.values()
    ]
