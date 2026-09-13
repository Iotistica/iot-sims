"""Layer-1 domain-meaning contract for a model.json input/output.

Part of the three-layer semantic architecture (see the plan / CLAUDE.md):

    Domain semantics describe MEANING.        <- this file
    Semantic providers describe REPRESENTATION (Brick, Haystack, OPC UA info model, ...).
    Access providers describe ACCESS (BACnet, OPC UA, Modbus, MQTT, ...).
    Consumers only ask for RESOLUTION.

`SemanticContract` is exactly what a Modelica/FMU model author wrote in the `semantic`
block of `model.json` -- the ONE authoritative description of what a signal means. It is
provider-neutral: no Brick class, no BACnet object type, no OPC UA node class, no Modbus
register. There is no legacy fallback field -- a signal either has a contract (`id` set) or
it does not.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticContract:
    """A model signal's meaning. See shared/semantic_contract.py (iot-models) for the
    canonical vocabulary of `id` / `role` / `scope` / `relationship` / `aggregation`."""

    # Canonical snake_case token, e.g. "supply_air_temperature". "" == no contract.
    id: str = ""
    # sensor | setpoint | command | status | derived  (domain meaning, not a Brick suffix)
    role: str = "sensor"
    # self | upstream | downstream | any  (topology hint for candidate discovery)
    scope: str = "self"
    # feeds | isFedBy | isPartOf | None
    relationship: str | None = None
    # none | most_open | most_closed | total | average | max | min
    aggregation: str = "none"

    @property
    def is_empty(self) -> bool:
        return not self.id

    @property
    def is_aggregated(self) -> bool:
        return self.aggregation not in (None, "", "none")
