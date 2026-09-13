"""The Brick semantic provider -- the first concrete implementation of layer 2.

Everything Brick-specific lives here and in ``feature_contract.py``:

  * ``CANONICAL_TO_BRICK`` -- the Brick-only knowledge: canonical domain id -> Brick
    ``(point_class, equipment_path)``. Every ``point_class`` is a real Brick v1.4.4 class
    present in ``src/core/config.py::POINT_TYPES`` (checked by
    ``tests/test_semantic_provider_brick.py``).
  * ``BrickSemanticProvider`` -- reuses ``feature_contract.py``'s graph traversal
    (``resolve_point_semantic_identity`` / ``match_brick_identity_in_candidates``) and
    returns the neutral ``SemanticResolution``. ``ResolvedBrickPoint`` never leaves this
    module boundary.

A future OPC UA / Haystack provider is a sibling file; nothing in the generic layer or in
consumers forces it to speak Brick.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from ..contract import SemanticContract
from ..feature_contract import (
    ResolvedBrickPoint,
    match_brick_identity_in_candidates,
)
from ..resolution import (
    ResolutionCandidate,
    ResolutionContext,
    SemanticResolution,
)


@dataclass(frozen=True)
class BrickIdentityTemplate:
    point_class: str
    equipment_path: tuple[str, ...] = ()


# Canonical domain id -> Brick representation. Only ids for which a genuine Brick v1.4.4
# point class exists are templated; every other shipped id falls through to
# ``_expected_identity`` returning ``None`` -> ``"unresolved"`` -> the consumer's
# name-similarity tier (exactly today's behavior). "supply" vs "return" etc. is expressed
# via ``equipment_path`` (isPartOf a Supply_Fan/Return_Fan sub-equipment entity), never a
# per-role point-class alias.
CANONICAL_TO_BRICK: dict[str, BrickIdentityTemplate] = {
    "supply_air_temperature": BrickIdentityTemplate("Supply_Air_Temperature_Sensor"),
    "supply_air_temperature_setpoint": BrickIdentityTemplate("Supply_Air_Temperature_Setpoint"),
    "discharge_air_temperature": BrickIdentityTemplate("Supply_Air_Temperature_Sensor"),
    "outdoor_air_temperature": BrickIdentityTemplate("Outside_Air_Temperature_Sensor"),
    "return_air_temperature": BrickIdentityTemplate("Return_Air_Temperature_Sensor"),
    "mixed_air_temperature": BrickIdentityTemplate("Mixed_Air_Temperature_Sensor"),
    "zone_air_temperature": BrickIdentityTemplate("Zone_Air_Temperature_Sensor"),
    "cooling_setpoint": BrickIdentityTemplate("Cooling_Temperature_Setpoint"),
    "heating_setpoint": BrickIdentityTemplate("Heating_Temperature_Setpoint"),
    "supply_fan_command": BrickIdentityTemplate("Fan_Speed_Command", ("Supply_Fan",)),
    "supply_fan_status": BrickIdentityTemplate("Fan_Status", ("Supply_Fan",)),
    "supply_fan_electric_power": BrickIdentityTemplate("Power_Sensor", ("Supply_Fan",)),
    "cooling_compressor_electric_power": BrickIdentityTemplate("Power_Sensor"),
    "heating_compressor_electric_power": BrickIdentityTemplate("Power_Sensor"),
    "total_electric_power": BrickIdentityTemplate("Power_Sensor"),
    "supply_airflow": BrickIdentityTemplate("Supply_Air_Flow_Sensor"),
    "supply_duct_static_pressure": BrickIdentityTemplate("Static_Pressure_Sensor"),
    "supply_duct_static_pressure_setpoint": BrickIdentityTemplate("Static_Pressure_Setpoint"),
    "cooling_command": BrickIdentityTemplate("Cooling_Command"),
    "heating_command": BrickIdentityTemplate("Heating_Command"),
    "cooling_valve_command": BrickIdentityTemplate("Valve_Position_Command"),
    "heating_valve_command": BrickIdentityTemplate("Valve_Position_Command"),
    "reheat_valve_command": BrickIdentityTemplate("Valve_Position_Command"),
    "reheat_valve_position": BrickIdentityTemplate("Valve_Status"),
    "outdoor_air_damper_command": BrickIdentityTemplate("Damper_Position_Command"),
    "damper_command": BrickIdentityTemplate("Damper_Position_Command"),
    "outdoor_air_damper_position": BrickIdentityTemplate("Damper_Position_Status"),
    "damper_position": BrickIdentityTemplate("Damper_Position_Status"),
    "chilled_water_supply_temperature": BrickIdentityTemplate("Leaving_Chilled_Water_Temperature_Sensor"),
    "chilled_water_return_temperature": BrickIdentityTemplate("Entering_Chilled_Water_Temperature_Sensor"),
    "hot_water_supply_temperature": BrickIdentityTemplate("Leaving_Hot_Water_Temperature_Sensor"),
    "hot_water_return_temperature": BrickIdentityTemplate("Entering_Hot_Water_Temperature_Sensor"),
    "condenser_water_temperature": BrickIdentityTemplate("Condenser_Water_Temperature_Sensor"),
    "chilled_water_flow": BrickIdentityTemplate("Water_Flow_Sensor"),
    "hot_water_flow": BrickIdentityTemplate("Water_Flow_Sensor"),
    "chilled_water_differential_pressure": BrickIdentityTemplate("Water_Differential_Pressure_Sensor"),
    "hot_water_differential_pressure": BrickIdentityTemplate("Differential_Pressure_Sensor"),
    "outdoor_relative_humidity": BrickIdentityTemplate("Outside_Air_Humidity_Sensor"),
}

# Canonical domain ids that are legal but deliberately NOT templated: enumerated physical
# plant instances (two identical chillers can't be told apart by point class alone),
# override/enable signals, and signals whose current legacy ``point_class`` is not a real
# Brick class. These are real sensors -- just not Brick-portable by class -- so they fall
# to the consumer's name-similarity tier, same as today.
NAME_TIER_ONLY_IDS: frozenset[str] = frozenset({
    "supply_fan_speed",
    "outdoor_airflow",
    "return_airflow",
    "supply_air_mass_flow",
    "supply_air_relative_humidity",
    "minimum_outdoor_air_damper_position",
    "outdoor_air_damper_override_enable",
    "outdoor_air_damper_override_position",
    "economizer_status",
    "cooling_status",
    "heating_status",
    "cooling_thermal_load",
    "heating_thermal_load",
    "cooling_delivered",
    "plant_heating_delivered",
    "gas_heating_input_power",
    "boiler_1_enable", "boiler_2_enable",
    "boiler_1_fuel_input_power", "boiler_2_fuel_input_power",
    "boiler_1_heating_output", "boiler_2_heating_output",
    "chiller_1_power", "chiller_2_power",
    "chiller_1_run_status", "chiller_2_run_status",
    "chilled_water_pump_1_run_status", "chilled_water_pump_2_run_status",
    "cooling_tower_fan_1_run_status", "cooling_tower_fan_2_run_status",
    "hot_water_pump_1_command", "hot_water_pump_2_command",
    "hot_water_pump_1_power", "hot_water_pump_2_power",
    "hot_water_supply_temperature_setpoint",
    "equipment_heat_gain", "internal_heat_gain", "lighting_heat_gain", "people_heat_gain",
    "solar_radiation", "wind_speed",
})

# Bridge to the FDD-internal canonical-role vocabulary (PascalCase) in
# src/fault_detection/rules/{rtu,ahu}.py::CANONICAL_SEMANTICS. Same concept, different key
# casing. Follow-up: have FDD consume CANONICAL_TO_BRICK through this map instead of its
# own table. Not merged this phase (FDD test surface).
FDD_ROLE_TO_CANONICAL_ID: dict[str, str] = {
    "Supply_Air_Temperature_Sensor": "supply_air_temperature",
    "Supply_Air_Temperature_Setpoint": "supply_air_temperature_setpoint",
    "Outside_Air_Temperature_Sensor": "outdoor_air_temperature",
    "Return_Air_Temperature_Sensor": "return_air_temperature",
    "Mixed_Air_Temperature_Sensor": "mixed_air_temperature",
    "Supply_Fan_Command": "supply_fan_command",
    "Supply_Fan_Status": "supply_fan_status",
    "Supply_Fan_Power": "supply_fan_electric_power",
    "Cooling_Command": "cooling_command",
    "Heating_Command": "heating_command",
    "Cooling_Compressor_Power": "cooling_compressor_electric_power",
    "Most_Open_VAV_Damper_Position": "most_open_vav_damper_position",
    "Outside_Air_Damper_Command": "outdoor_air_damper_command",
    "Total_Electric_Power": "total_electric_power",
}


class BrickSemanticProvider:
    name = "brick"

    def resolve(
        self,
        database: Any,
        contract: SemanticContract,
        candidates: Sequence[ResolutionCandidate],
        *,
        context: ResolutionContext | None = None,
    ) -> SemanticResolution:
        target = self._expected_identity(contract)
        if target is None:
            return SemanticResolution(
                None, "unresolved", self.name,
                {"reason": f"no Brick representation for {contract.id or contract.legacy_point_class!r}"},
            )

        per = match_brick_identity_in_candidates(database, candidates, target)
        exact = [cid for cid, q in per.items() if q == "exact"]
        class_only = [cid for cid, q in per.items() if q == "class_only"]
        _where = f" under {'/'.join(target.equipment_path)}" if target.equipment_path else ""
        prov = {
            # "summary" is the neutral, consumer-facing key (a ready-made human string);
            # the Brick-specific detail keys below are opaque UI/debug extras.
            "summary": f"Semantic match: {target.point_class}{_where}",
            "point_class": target.point_class,
            "equipment_path": list(target.equipment_path),
        }

        if len(exact) == 1:
            return SemanticResolution(exact[0], "exact", self.name, prov, per_candidate=per)
        if len(exact) > 1:
            return SemanticResolution(
                None, "ambiguous", self.name,
                {**prov, "reason": "multiple points match this semantic identity"},
                tuple(sorted(exact)), per,
            )

        if not target.equipment_path:
            if len(class_only) == 1:
                return SemanticResolution(class_only[0], "class_only", self.name, prov, per_candidate=per)
            if len(class_only) > 1:
                return SemanticResolution(
                    None, "ambiguous", self.name,
                    {**prov, "reason": "multiple points match this point class"},
                    tuple(sorted(class_only)), per,
                )
            return SemanticResolution(None, "unresolved", self.name, prov, per_candidate=per)

        # target requires an equipment_path nothing could verify -> never guess from a
        # class-only candidate; leave it to the consumer's name tier.
        return SemanticResolution(
            None, "unresolved", self.name,
            {**prov, "reason": "equipment context could not be verified for any candidate"},
            per_candidate=per,
        )

    def _expected_identity(self, contract: SemanticContract) -> Optional[ResolvedBrickPoint]:
        # `semantic.id` is authoritative. The Brick provider owns id -> Brick class here,
        # and nowhere else. role-agnostic: a `derived` contract with a template resolves
        # like any other; one without a template -> None -> "unresolved".
        template = CANONICAL_TO_BRICK.get(contract.id)
        if template is not None:
            return ResolvedBrickPoint(template.point_class, template.equipment_path, None, source_verified=True)
        return None
