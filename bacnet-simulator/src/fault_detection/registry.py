from __future__ import annotations

from .rules.ahu import SupplyAirTemperatureDeviation, SupplyFanCommandStatusMismatch
from .rules.base import FaultRule
from .rules.sensors import FrozenSensorRule


class FaultRuleRegistry:
    def __init__(self, rules: list[FaultRule] | None = None) -> None:
        self._rules: dict[str, FaultRule] = {}
        for rule in rules or []:
            self.register(rule)

    def register(self, rule: FaultRule) -> None:
        rule_id = rule.definition.rule_id
        if rule_id in self._rules:
            raise ValueError(f"Fault rule {rule_id!r} already registered")
        self._rules[rule_id] = rule

    def all(self) -> list[FaultRule]:
        return list(self._rules.values())

    def for_equipment(
    self,
    equipment_type: str | None,
    ) -> list[FaultRule]:
        if not equipment_type:
            return [
                rule
                for rule in self._rules.values()
                if rule.definition.equipment_type == "*"
            ]

        return [
            rule
            for rule in self._rules.values()
            if rule.definition.equipment_type
            in {
                "*",
                equipment_type,
            }
        ]


def build_default_registry() -> FaultRuleRegistry:
    return FaultRuleRegistry([
        SupplyFanCommandStatusMismatch(),
        SupplyAirTemperatureDeviation(),
        FrozenSensorRule(),
    ])
