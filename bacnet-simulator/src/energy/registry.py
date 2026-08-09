"""Central registry of energy-model-config dataclasses, keyed by the
`energy_model_configs.model_type` string. Used by the config CRUD API
(src/api/routers/devices.py's energy-models routes, src/api/routers/
energy.py's PUT/DELETE) to validate an incoming model_type and to parse +
validate its `parameters` JSON via the dataclass's own `.validate()` --
not to re-implement range checks that already exist.

EnergyEngine.evaluate_all() itself is not changed to use this registry --
its existing if/elif dispatch is left exactly as-is, per this task's
"do not change current calculations" constraint. This registry exists
purely for the new config-management layer.
"""
from __future__ import annotations

import json

from .equipment.ahu import AHUEnergyConfig
from .equipment.boiler import BoilerEnergyConfig
from .equipment.chiller import ChillerEnergyConfig
from .equipment.lighting import LightingEnergyConfig

MODEL_CONFIG_CLASSES: dict[str, type] = {
    "chiller": ChillerEnergyConfig,
    "ahu": AHUEnergyConfig,
    "lighting": LightingEnergyConfig,
    "boiler": BoilerEnergyConfig,
}

MODEL_TYPE_LABELS: dict[str, str] = {
    "chiller": "Chiller",
    "ahu": "AHU",
    "lighting": "Lighting",
    "boiler": "Boiler",
}

# Every model type allows multiple named instances per device (e.g.
# scenario-comparison chiller configs "Baseline"/"Efficient"/"Degraded",
# or multiple lighting zones). instance_key is the composite-identity
# disambiguator for all of them -- there is currently no cardinality
# restriction by model type. How multiple enabled instances of the SAME
# model type on the SAME device should aggregate (sum vs. pick one
# "active" scenario) is an explicitly separate, not-yet-designed follow-up
# -- see UtilitiesDashboard.vue's KPI aggregation, which currently sums
# every enabled instance's power/energy unconditionally.


def energy_model_config_to_api(row: dict) -> dict:
    """Shared response shape for both the devices.py list/create routes and
    energy.py's row-id update/delete routes -- parameters comes back as a
    parsed object, not the raw JSON-in-TEXT-column string."""
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "model_type": row["model_type"],
        "instance_key": row["instance_key"],
        "enabled": bool(row["enabled"]),
        "parameters": json.loads(row["parameters"] or "{}"),
    }


def validate_energy_model_parameters(model_type: str, parameters: dict) -> None:
    """Raises ValueError (unsupported model_type or invalid parameters) --
    callers convert to HTTPException(400). Reuses each equipment config
    dataclass's own .validate(), never reimplements the range checks."""
    config_class = MODEL_CONFIG_CLASSES.get(model_type)
    if config_class is None:
        raise ValueError(
            f"Unsupported model_type {model_type!r} -- must be one of "
            f"{sorted(MODEL_CONFIG_CLASSES)}"
        )
    try:
        config = config_class(**parameters)
    except TypeError as e:
        raise ValueError(f"Invalid parameters for {model_type}: {e}") from e
    config.validate()
