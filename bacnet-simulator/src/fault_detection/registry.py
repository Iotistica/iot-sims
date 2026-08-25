from __future__ import annotations

from .rules.base import FaultRule
from .rules.sensors import FrozenSensorRule

# These modules use canonical application semantics and may resolve points
# through Brick classes, project-specific semantic extensions, or aliases.
from .rules.ahu import AHU_FAULT_RULES
from .rules.rtu import RTU_FAULT_RULES


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
            if rule.definition.equipment_type in {"*", equipment_type}
        ]


def build_default_registry() -> FaultRuleRegistry:
    """
    Register the full semantic AHU + RTU FDD libraries.

    Equipment filtering still happens in for_equipment(), so registering all
    rules here does not cause AHU rules to execute on RTUs or vice versa.

    Rules whose required semantics are unavailable return evaluable=False;
    they should not be removed merely because one installation lacks a point.
    """

    rules: list[FaultRule] = [
        *(rule_class() for rule_class in AHU_FAULT_RULES),
        *(rule_class() for rule_class in RTU_FAULT_RULES),
        FrozenSensorRule(),
    ]

    return FaultRuleRegistry(rules)
