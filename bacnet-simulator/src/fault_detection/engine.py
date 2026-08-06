from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from .context import FaultContext, PointSnapshot
from .models import FaultEvaluation, FaultResult, FaultState
from .registry import FaultRuleRegistry
from .state import RuleRuntimeState

EventCallback = Callable[[int | None, str, str], None]


class FaultDetectionEngine:
    def __init__(self, *, database: Any, simulation_engine: Any, registry: FaultRuleRegistry, event_callback: EventCallback | None = None) -> None:
        self.database = database
        self.simulation_engine = simulation_engine
        self.registry = registry
        self.event_callback = event_callback
        self._runtime: dict[tuple[int, str], RuleRuntimeState] = {}

    async def evaluate_all(self) -> list[FaultEvaluation]:
        devices = await asyncio.to_thread(self.database.get_devices)
        output: list[FaultEvaluation] = []
        for device in devices:
            if device.get("enabled"):
                output.extend(await self.evaluate_device(device["id"]))
        return output
    async def _publish_alert(
        self,
        *,
        device_name: str,
        evaluation: FaultEvaluation,
    ) -> None:
        """
        Publish active and cleared FDD transitions into the existing
        simulator alarm log.

        Pending and ordinary normal evaluations are intentionally ignored.
        """
        if evaluation.state == FaultState.ACTIVE:
            from_state = "normal"
            to_state = "offnormal"
            ack_required = 1

        elif evaluation.state == FaultState.CLEARED:
            from_state = "offnormal"
            to_state = "normal"
            ack_required = 0

        else:
            return

        priority = {
            "critical": 50,
            "warning": 100,
            "info": 150,
        }.get(
            evaluation.severity.value,
            100,
        )

        evidence_data = [
            {
                "point": item.point,
                "value": item.value,
                "expected": item.expected,
            }
            for item in evaluation.evidence
        ]

        message = (
            f"FDD: {evaluation.rule_id}: "
            f"{evaluation.message}"
        )

        await asyncio.to_thread(
            self.database.log_alarm,
            {
                # FDD rules can depend on multiple points, so there may
                # not be one correct source BACnet object.
                "object_id": None,
                "device_id": evaluation.device_id,
                "object_name": evaluation.rule_id,
                "from_state": from_state,
                "to_state": to_state,
                "priority": priority,
                "value": json.dumps(evidence_data),
                "message": message,
                "ack_required": ack_required,
            },
        )

        if self.event_callback is not None:
            level = (
                "info"
                if to_state == "normal"
                else (
                    "error"
                    if evaluation.severity.value == "critical"
                    else "warn"
                )
            )

            self.event_callback(
                evaluation.device_id,
                level,
                (
                    f"FDD alert on {device_name}: "
                    f"{message}"
                ),
            )

    async def evaluate_device(self, device_id: int) -> list[FaultEvaluation]:
        context = await self._build_context(device_id)
        configs = await asyncio.to_thread(self.database.get_fault_rule_configs, device_id)
        config_by_rule = {row["rule_id"]: row for row in configs}
        evaluations: list[FaultEvaluation] = []

        for rule in self.registry.for_equipment(context.equipment_type):
            config = config_by_rule.get(rule.definition.rule_id)
            if config is not None and not bool(config.get("enabled", 1)):
                continue
            parameters = json.loads(config.get("parameters") or "{}") if config else {}
            rule_context = FaultContext(context.device_id, context.device_name, context.equipment_type, context.timestamp, context.points, parameters)
            result = rule.evaluate(rule_context)
            persistence = float(config["persistence_seconds"]) if config and config.get("persistence_seconds") is not None else rule.definition.persistence_seconds
            clear_seconds = float(config["clear_seconds"]) if config and config.get("clear_seconds") is not None else rule.definition.clear_seconds
            evaluation = self._advance_state(device_id, rule.definition.rule_id, result, context.timestamp, persistence, clear_seconds)
            evaluations.append(evaluation)
            if evaluation.state != evaluation.previous_state:
                await asyncio.to_thread(
                    self.database.record_fault_evaluation,
                    {
                        "device_id": evaluation.device_id,
                        "rule_id": evaluation.rule_id,
                        "state": evaluation.state.value,
                        "previous_state": (
                            evaluation.previous_state.value
                        ),
                        "severity": evaluation.severity.value,
                        "message": evaluation.message,
                        "evidence": json.dumps(
                            [
                                {
                                    "point": item.point,
                                    "value": item.value,
                                    "expected": item.expected,
                                }
                                for item in evaluation.evidence
                            ]
                        ),
                        "timestamp": evaluation.timestamp,
                        "activated_at": evaluation.activated_at,
                        "cleared_at": evaluation.cleared_at,
                    },
                )

                self._log_transition(
                    context.device_name,
                    evaluation,
                )

                await self._publish_alert(
                    device_name=context.device_name,
                    evaluation=evaluation,
                )
        return evaluations

    async def _build_context(
    self,
    device_id: int,
    ) -> FaultContext:
        device = await asyncio.to_thread(
            self.database.get_device,
            device_id,
        )

        if device is None:
            raise ValueError(
                f"Device {device_id} not found"
            )

        objects = await asyncio.to_thread(
            self.database.get_objects,
            device_id,
        )

        points: dict[str, PointSnapshot] = {}

        for obj in objects:
            point_type = obj.get("point_type")

            if not point_type:
                continue

            live_value = (
                self.simulation_engine.get_object_value(
                    obj["id"]
                )
            )

            points[point_type] = PointSnapshot(
                object_id=obj["id"],
                object_identifier=(
                    f"{obj['object_type']}:"
                    f"{obj['object_instance']}"
                ),
                name=obj["name"],
                point_type=point_type,
                value=live_value,
                units=obj.get("units"),
                reliability=obj.get("reliability"),
                out_of_service=bool(
                    obj.get("out_of_service", False)
                ),
            )

        return FaultContext(
            device_id=device["id"],
            device_name=device["name"],
            equipment_type=device.get(
                "equipment_type"
            ),
            timestamp=time.time(),
            points=points,
            parameters={},
        )

    def _advance_state(self, device_id: int, rule_id: str, result: FaultResult, now: float, persistence_seconds: float, clear_seconds: float) -> FaultEvaluation:
        runtime = self._runtime.setdefault((device_id, rule_id), RuleRuntimeState())
        previous = runtime.state
        if not result.evaluable:
            runtime.condition_started_at = None
            runtime.clear_started_at = None
        elif result.condition_present:
            runtime.clear_started_at = None
            runtime.cleared_at = None
            runtime.condition_started_at = runtime.condition_started_at or now
            if now - runtime.condition_started_at >= persistence_seconds:
                runtime.state = FaultState.ACTIVE
                runtime.activated_at = runtime.activated_at or now
            else:
                runtime.state = FaultState.PENDING
        else:
            runtime.condition_started_at = None
            if runtime.state in {FaultState.ACTIVE, FaultState.PENDING}:
                runtime.clear_started_at = runtime.clear_started_at or now
                if runtime.state == FaultState.PENDING:
                    runtime.state = FaultState.NORMAL
                elif now - runtime.clear_started_at >= clear_seconds:
                    runtime.state = FaultState.CLEARED
                    runtime.cleared_at = now
                    runtime.activated_at = None
            elif runtime.state == FaultState.CLEARED:
                runtime.state = FaultState.NORMAL
                runtime.clear_started_at = None
            else:
                runtime.state = FaultState.NORMAL
        runtime.last_message = result.message
        return FaultEvaluation(device_id, rule_id, runtime.state, previous, result.message, result.severity, result.evidence, now, runtime.activated_at, runtime.cleared_at)

    def _log_transition(
    self,
    device_name: str,
    evaluation: FaultEvaluation,
        ) -> None:
            if self.event_callback is None:
                return

            if evaluation.state == FaultState.ACTIVE:
                level = (
                    "error"
                    if evaluation.severity.value == "critical"
                    else "warn"
                )

            elif evaluation.state == FaultState.PENDING:
                level = "warn"

            else:
                level = "info"

            self.event_callback(
                evaluation.device_id,
                level,
                (
                    f"FDD {evaluation.rule_id}: "
                    f"{evaluation.previous_state.value} -> "
                    f"{evaluation.state.value} on "
                    f"{device_name}: "
                    f"{evaluation.message}"
                ),
            )