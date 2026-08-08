from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from .context import (
    build_chiller_snapshot,
    build_ahu_snapshot,
    build_lighting_snapshot,
    build_boiler_snapshot,
)

from .equipment.ahu import (
    AHUEnergyConfig,
    AHUEnergyModel,
    AHUEnergyResult,
)
from .equipment.chiller import (
    ChillerEnergyConfig,
    ChillerEnergyModel,
    ChillerEnergyResult,
)
from .equipment.lighting import (
    LightingEnergyConfig,
    LightingEnergyModel,
    LightingEnergyResult,
)

from .equipment.boiler import (
    BoilerEnergyConfig,
    BoilerEnergyModel,
    BoilerEnergyResult,
)

from ..semantics.resolver import SemanticResolver




log = logging.getLogger(__name__)

EventCallback = Callable[[int, str, str], None]
ModelKey = tuple[int, str, str]


class EnergyEngine:
    def __init__(
        self,
        *,
        database: Any,
        simulation_engine: Any,
        event_callback: EventCallback | None = None,
        history_interval_seconds: float = 60.0,
        history_retention_days: int = 7,
        audit_log_interval_seconds: float = 60.0,
        cleanup_interval_seconds: float = 3600.0,
    ) -> None:
        if history_interval_seconds <= 0:
            raise ValueError(
                "history_interval_seconds must be greater than zero"
            )

        if history_retention_days <= 0:
            raise ValueError(
                "history_retention_days must be greater than zero"
            )

        if audit_log_interval_seconds <= 0:
            raise ValueError(
                "audit_log_interval_seconds must be greater than zero"
            )

        if cleanup_interval_seconds <= 0:
            raise ValueError(
                "cleanup_interval_seconds must be greater than zero"
            )

        self.database = database
        self.simulation_engine = simulation_engine
        self.event_callback = event_callback
        self.semantic_resolver = SemanticResolver(
            database=database, simulation_engine=simulation_engine,
        )

        self.history_interval_seconds = float(
            history_interval_seconds
        )

        self.history_retention_seconds = float(
            history_retention_days * 24 * 60 * 60
        )

        self.audit_log_interval_seconds = float(
            audit_log_interval_seconds
        )

        self.cleanup_interval_seconds = float(
            cleanup_interval_seconds
        )

        # Every model is keyed by device, model type, and model instance.
        # Existing one-model-per-device equipment uses instance_key="default".
        # Lighting can use instance keys such as "zone-a" and "zone-b".
        self._models: dict[ModelKey, Any] = {}
        self._latest: dict[ModelKey, Any] = {}

        # Mirrors SimEngine.clock_state ("running"/"paused"/"stopped") --
        # kept here (not read from SimEngine directly) so EnergyEngine
        # stays usable/testable without a live SimEngine instance. Callers
        # (src/api/routers/simulation.py) call pause()/resume() alongside
        # the matching SimEngine method on every /sim/pause and /sim/start
        # request, and reset() (which also sets this) on /sim/stop.
        self.clock_state: str = "running"

        self._history_elapsed_seconds = 0.0
        self._audit_log_elapsed_seconds = 0.0
        self._last_cleanup_timestamp = 0.0

    async def evaluate_all(
        self,
        *,
        elapsed_seconds: float,
        simulation_timestamp: float | None = None,
        force_audit_log: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Evaluate every enabled energy model.

        elapsed_seconds should be simulated elapsed time when the
        simulator supports accelerated time.

        simulation_timestamp should be a Unix timestamp. If it is not
        supplied, wall-clock time is used for stored history.

        Set force_audit_log=True for a manual API evaluation that should
        appear immediately in the activity log.
        """
        if elapsed_seconds < 0:
            raise ValueError(
                "elapsed_seconds cannot be negative"
            )

        configs = await asyncio.to_thread(
            self.database.get_enabled_energy_model_configs
        )

        self._history_elapsed_seconds += elapsed_seconds
        self._audit_log_elapsed_seconds += elapsed_seconds

        should_write_history = (
            self._history_elapsed_seconds
            >= self.history_interval_seconds
        )

        should_log_audit = (
            force_audit_log
            or self._audit_log_elapsed_seconds
            >= self.audit_log_interval_seconds
        )

        results: list[dict[str, Any]] = []

        for row in configs:
            try:
                device_id = int(row["device_id"])
                model_type = str(row["model_type"])
                instance_key = str(
                    row.get("instance_key") or "default"
                )
                parameters_json = str(
                    row.get("parameters") or "{}"
                )

                if model_type == "chiller":
                    result = await self.evaluate_chiller(
                        device_id=device_id,
                        instance_key=instance_key,
                        parameters_json=parameters_json,
                        elapsed_seconds=elapsed_seconds,
                        log_event=should_log_audit,
                    )
                    results.append(result)
                    continue

                if model_type == "ahu":
                    result = await self.evaluate_ahu(
                        device_id=device_id,
                        instance_key=instance_key,
                        parameters_json=parameters_json,
                        elapsed_seconds=elapsed_seconds,
                        log_event=should_log_audit,
                    )
                    results.append(result)
                    continue

                if model_type == "lighting":
                    result = await self.evaluate_lighting(
                        device_id=device_id,
                        instance_key=instance_key,
                        parameters_json=parameters_json,
                        elapsed_seconds=elapsed_seconds,
                        log_event=should_log_audit,
                    )
                    results.append(result)
                    continue

                if model_type == "boiler":
                    result = await self.evaluate_boiler(
                        device_id=device_id,
                        instance_key=instance_key,
                        parameters_json=parameters_json,
                        elapsed_seconds=elapsed_seconds,
                        log_event=should_log_audit,
                    )

                    results.append(result)
                    continue

                log.debug(
                    "Unsupported energy model type %s "
                    "for device %s instance %s",
                    model_type,
                    device_id,
                    instance_key,
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Energy evaluation failed for "
                    "device=%r model_type=%r instance_key=%r",
                    row.get("device_id"),
                    row.get("model_type"),
                    row.get("instance_key", "default"),
                )

        timestamp = (
            float(simulation_timestamp)
            if simulation_timestamp is not None
            else time.time()
        )

        if should_write_history:
            try:
                await self._write_history(
                    results=results,
                    timestamp=timestamp,
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Failed to persist energy history"
                )

            self._history_elapsed_seconds %= (
                self.history_interval_seconds
            )

            try:
                await self._cleanup_history(
                    timestamp=timestamp,
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Failed to clean up energy history"
                )

        if should_log_audit:
            self._audit_log_elapsed_seconds %= (
                self.audit_log_interval_seconds
            )

        return results

    async def evaluate_chiller(
        self,
        *,
        device_id: int,
        instance_key: str = "default",
        parameters_json: str,
        elapsed_seconds: float,
        log_event: bool = False,
    ) -> dict[str, Any]:
        try:
            parameters = json.loads(
                parameters_json or "{}"
            )

        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid chiller energy configuration "
                f"for device {device_id}"
            ) from exc

        config = ChillerEnergyConfig(**parameters)
        key: ModelKey = (
            device_id,
            "chiller",
            instance_key,
        )

        model = self._models.get(key)

        if model is None or model.config != config:
            model = ChillerEnergyModel(config)
            self._models[key] = model

        objects = await asyncio.to_thread(
            self.database.get_objects,
            device_id,
        )

        values = (
            self.simulation_engine
            .get_device_point_values(objects)
        )

        snapshot = build_chiller_snapshot(values)

        result = model.evaluate(
            snapshot,
            elapsed_seconds,
        )

        self._latest[key] = result

        serialized = self._serialize_chiller(
            device_id=device_id,
            instance_key=instance_key,
            model=model,
            result=result,
        )

        if log_event:
            self._log_chiller_result(
                device_id=device_id,
                instance_key=instance_key,
                model=model,
                result=result,
            )

        return serialized

    async def evaluate_boiler(
    self,
    *,
    device_id: int,
    instance_key: str = "default",
    parameters_json: str,
    elapsed_seconds: float,
    log_event: bool = False,
    ) -> dict[str, Any]:
        try:
            parameters = json.loads(
                parameters_json or "{}"
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid boiler energy configuration "
                f"for device {device_id}"
            ) from exc

        config = BoilerEnergyConfig(
            **parameters
        )

        key = (
            device_id,
            "boiler",
            instance_key,
        )

        model = self._models.get(key)

        if model is None or model.config != config:
            model = BoilerEnergyModel(config)
            self._models[key] = model

        objects = await asyncio.to_thread(
            self.database.get_objects,
            device_id,
        )

        values = (
            self.simulation_engine
            .get_device_point_values(objects)
        )

        snapshot = build_boiler_snapshot(
            values
        )

        result = model.evaluate(
            snapshot,
            elapsed_seconds,
        )

        self._latest[key] = result

        serialized = self._serialize_boiler(
            device_id=device_id,
            instance_key=instance_key,
            model=model,
            result=result,
        )

        if (
            log_event
            and self.event_callback is not None
        ):
            fuel_text = (
                f"{result.fuel_input_kw:.2f} kW fuel"
                if result.fuel_input_kw is not None
                else "fuel input unavailable"
            )

            output_text = (
                f"{result.thermal_output_kw:.2f} kW heating"
                if result.thermal_output_kw is not None
                else "heating output unavailable"
            )

            self.event_callback(
                device_id,
                "info",
                (
                    f"Boiler energy calculation: "
                    f"{fuel_text}; "
                    f"{output_text}; "
                    f"source={result.source.value}; "
                    f"confidence={result.confidence.value}; "
                    f"fuel_total="
                    f"{model.total_fuel_energy_kwh:.3f} kWh"
                ),
            )

        return serialized

    async def evaluate_ahu(
        self,
        *,
        device_id: int,
        instance_key: str = "default",
        parameters_json: str,
        elapsed_seconds: float,
        log_event: bool = False,
    ) -> dict[str, Any]:
        try:
            parameters = json.loads(
                parameters_json or "{}"
            )

        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid AHU energy configuration "
                f"for device {device_id}"
            ) from exc

        config = AHUEnergyConfig(**parameters)
        key: ModelKey = (
            device_id,
            "ahu",
            instance_key,
        )

        model = self._models.get(key)

        if model is None or model.config != config:
            model = AHUEnergyModel(config)
            self._models[key] = model

        objects = await asyncio.to_thread(
            self.database.get_objects,
            device_id,
        )

        values = (
            self.simulation_engine
            .get_device_point_values(objects)
        )

        fan_points = await asyncio.to_thread(
            self.semantic_resolver.resolve_ahu_fans,
            device_id,
        )

        snapshot = build_ahu_snapshot(values, fan_points=fan_points)

        result = model.evaluate(
            snapshot,
            elapsed_seconds,
        )

        self._latest[key] = result

        serialized = self._serialize_ahu(
            device_id=device_id,
            instance_key=instance_key,
            model=model,
            result=result,
        )

        if log_event and self.event_callback is not None:
            power_text = (
                f"{result.power_kw:.2f} kW"
                if result.power_kw is not None
                else "power unavailable"
            )

            message = (
                f"AHU energy calculation"
                f" [{instance_key}]: "
                f"{power_text}; "
                f"source={result.source.value}; "
                f"confidence={result.confidence.value}; "
                f"total={model.total_energy_kwh:.3f} kWh"
            )

            self.event_callback(
                device_id,
                "info",
                message,
            )

        return serialized

    async def evaluate_lighting(
        self,
        *,
        device_id: int,
        instance_key: str,
        parameters_json: str,
        elapsed_seconds: float,
        log_event: bool = False,
    ) -> dict[str, Any]:
        try:
            parameters = json.loads(
                parameters_json or "{}"
            )

        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid lighting energy configuration "
                f"for device {device_id} instance {instance_key}"
            ) from exc

        config = LightingEnergyConfig(**parameters)
        key: ModelKey = (
            device_id,
            "lighting",
            instance_key,
        )

        model = self._models.get(key)

        if model is None or model.config != config:
            model = LightingEnergyModel(config)
            self._models[key] = model

        objects = await asyncio.to_thread(
            self.database.get_objects,
            device_id,
        )

        # build_lighting_snapshot resolves via the zone's Lighting_Zone
        # semantic entity when one exists (semantic_resolver/device_id),
        # falling back per-field to the legacy Zone-A/Zone-B object-name
        # scan otherwise -- the lighting energy model itself is unchanged.
        snapshot = build_lighting_snapshot(
            objects=objects,
            simulation_engine=self.simulation_engine,
            instance_key=instance_key,
            resolver=self.semantic_resolver,
            device_id=device_id,
        )

        result = model.evaluate(
            snapshot,
            elapsed_seconds,
        )

        self._latest[key] = result

        serialized = self._serialize_lighting(
            device_id=device_id,
            instance_key=instance_key,
            model=model,
            result=result,
        )

        if log_event and self.event_callback is not None:
            power_text = (
                f"{result.power_kw:.2f} kW"
                if result.power_kw is not None
                else "power unavailable"
            )

            message = (
                f"Lighting energy calculation "
                f"[{instance_key}]: "
                f"{power_text}; "
                f"source={result.source.value}; "
                f"confidence={result.confidence.value}; "
                f"total={model.total_energy_kwh:.3f} kWh"
            )

            self.event_callback(
                device_id,
                "info",
                message,
            )

        return serialized

    def get_latest(
        self,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []

        for (
            device_id,
            model_type,
            instance_key,
        ), result in self._latest.items():
            model = self._models[
                (
                    device_id,
                    model_type,
                    instance_key,
                )
            ]

            if model_type == "chiller":
                output.append(
                    self._serialize_chiller(
                        device_id=device_id,
                        instance_key=instance_key,
                        model=model,
                        result=result,
                    )
                )

            elif model_type == "ahu":
                output.append(
                    self._serialize_ahu(
                        device_id=device_id,
                        instance_key=instance_key,
                        model=model,
                        result=result,
                    )
                )

            elif model_type == "lighting":
                output.append(
                    self._serialize_lighting(
                        device_id=device_id,
                        instance_key=instance_key,
                        model=model,
                        result=result,
                    )
                )
            elif model_type == "boiler":
                output.append(
                    self._serialize_boiler(
                        device_id=device_id,
                        instance_key=instance_key,
                        model=model,
                        result=result,
                    )
                )

        return sorted(
            output,
            key=lambda item: (
                item["device_id"],
                item["model_type"],
                item.get("instance_key", "default"),
            ),
        )

    def pause(self) -> None:
        """Freeze energy accumulation in place -- mirrors
        SimEngine.pause(). evaluate_all() itself doesn't check this; the
        tick loop (src/legacy.py's tick_loop()) skips calling evaluate_all()
        entirely while engine.clock_state != "running", the same way it
        already skips engine.tick()'s own value recomputation. This flag
        exists so EnergyEngine's own state is consistent/introspectable
        even though the tick loop is what actually enforces it."""
        self.clock_state = "paused"

    def resume(self) -> None:
        self.clock_state = "running"

    def reset(self) -> None:
        """
        Reset live energy state.

        Persisted history is not deleted.
        """
        self.clock_state = "stopped"

        for model in self._models.values():
            model.reset()

        self._latest.clear()

        self._history_elapsed_seconds = 0.0
        self._audit_log_elapsed_seconds = 0.0
        self._last_cleanup_timestamp = 0.0

    async def _write_history(
        self,
        *,
        results: list[dict[str, Any]],
        timestamp: float,
    ) -> None:
        rows: list[dict[str, Any]] = []

        # instance_key is intentionally NOT in common_fields yet. The current
        # energy_history schema has no instance_key column, so it is preserved
        # inside metrics. If you later add energy_history.instance_key, move it
        # into common_fields and pass it as a first-class DB column.
        common_fields = {
            "device_id",
            "model_type",
            "power_kw",
            "total_energy_kwh",
            "source",
            "confidence",
            "warnings",
            "inputs",
        }

        for result in results:
            metrics = {
                key: value
                for key, value in result.items()
                if key not in common_fields
            }

            rows.append(
                {
                    "timestamp": timestamp,
                    "device_id": result["device_id"],
                    "model_type": result["model_type"],
                    "power_kw": result.get("power_kw"),
                    "total_energy_kwh": result.get(
                        "total_energy_kwh"
                    ),
                    "source": result.get("source"),
                    "confidence": result.get("confidence"),
                    "metrics": json.dumps(
                        metrics,
                        separators=(",", ":"),
                    ),
                }
            )

        if not rows:
            return

        await asyncio.to_thread(
            self.database.insert_energy_history_batch,
            rows,
        )

    async def _cleanup_history(
        self,
        *,
        timestamp: float,
    ) -> None:
        if (
            self._last_cleanup_timestamp > 0
            and timestamp
            - self._last_cleanup_timestamp
            < self.cleanup_interval_seconds
        ):
            return

        cutoff_timestamp = (
            timestamp
            - self.history_retention_seconds
        )

        deleted = await asyncio.to_thread(
            self.database.delete_energy_history_before,
            cutoff_timestamp,
        )

        self._last_cleanup_timestamp = timestamp

        if deleted:
            log.info(
                "Deleted %s expired energy history rows",
                deleted,
            )

    def _log_chiller_result(
        self,
        *,
        device_id: int,
        instance_key: str,
        model: ChillerEnergyModel,
        result: ChillerEnergyResult,
    ) -> None:
        if self.event_callback is None:
            return

        power_text = (
            f"{result.power_kw:.2f} kW"
            if result.power_kw is not None
            else "power unavailable"
        )

        cooling_text = (
            f"{result.cooling_load_kw:.2f} kW cooling"
            if result.cooling_load_kw is not None
            else "cooling load unavailable"
        )

        message = (
            f"Energy calculation [{instance_key}]: "
            f"{power_text}; "
            f"{cooling_text}; "
            f"source={result.source.value}; "
            f"confidence={result.confidence.value}; "
            f"total={model.total_energy_kwh:.3f} kWh"
        )

        if result.warnings:
            message += (
                "; warnings="
                + " | ".join(result.warnings)
            )

        try:
            self.event_callback(
                device_id,
                "info",
                message,
            )

        except Exception:
            log.exception(
                "Energy audit callback failed "
                "for device %s",
                device_id,
            )

    @staticmethod
    def _serialize_ahu(
        *,
        device_id: int,
        instance_key: str,
        model: AHUEnergyModel,
        result: AHUEnergyResult,
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "model_type": "ahu",
            "instance_key": instance_key,
            "power_kw": result.power_kw,
            "interval_energy_kwh": result.interval_energy_kwh,
            "total_energy_kwh": model.total_energy_kwh,
            "supply_fan_power_kw": result.supply_fan_power_kw,
            "return_fan_power_kw": result.return_fan_power_kw,
            "auxiliary_power_kw": result.auxiliary_power_kw,
            "cooling_load_kw": result.cooling_load_kw,
            "heating_load_kw": result.heating_load_kw,
            "cooling_input_power_kw": result.cooling_input_power_kw,
            "heating_input_power_kw": result.heating_input_power_kw,
            "source": result.source.value,
            "confidence": result.confidence.value,
            "method": result.method,
            "inputs": result.inputs,
            "warnings": result.warnings,
        }

    @staticmethod
    def _serialize_chiller(
        *,
        device_id: int,
        instance_key: str,
        model: ChillerEnergyModel,
        result: ChillerEnergyResult,
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "model_type": "chiller",
            "instance_key": instance_key,
            "power_kw": result.power_kw,
            "interval_energy_kwh": result.interval_energy_kwh,
            "total_energy_kwh": model.total_energy_kwh,
            "cooling_load_kw": result.cooling_load_kw,
            "load_fraction": result.load_fraction,
            "effective_cop": result.effective_cop,
            "source": result.source.value,
            "confidence": result.confidence.value,
            "method": result.method,
            "inputs": result.inputs,
            "warnings": result.warnings,
        }

    @staticmethod
    def _serialize_lighting(
        *,
        device_id: int,
        instance_key: str,
        model: LightingEnergyModel,
        result: LightingEnergyResult,
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "model_type": "lighting",
            "instance_key": instance_key,
            "power_kw": result.power_kw,
            "interval_energy_kwh": result.interval_energy_kwh,
            "total_energy_kwh": model.total_energy_kwh,
            "lighting_level_fraction": result.lighting_level_fraction,
            "on": result.on,
            "occupancy": result.occupancy,
            "source": result.source.value,
            "confidence": result.confidence.value,
            "method": result.method,
            "inputs": result.inputs,
            "warnings": result.warnings,
        }

    @staticmethod
    def _serialize_boiler(
        *,
        device_id: int,
        instance_key: str,
        model: BoilerEnergyModel,
        result: BoilerEnergyResult,
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "model_type": "boiler",
            "instance_key": instance_key,

            # IMPORTANT:
            # Do not put boiler fuel input into power_kw.
            # Your current dashboard treats power_kw as electrical power.
            "power_kw": result.auxiliary_electric_power_kw,

            "thermal_output_kw": result.thermal_output_kw,
            "fuel_input_kw": result.fuel_input_kw,

            "interval_fuel_energy_kwh":
                result.interval_fuel_energy_kwh,

            "total_fuel_energy_kwh":
                model.total_fuel_energy_kwh,

            "auxiliary_electric_power_kw":
                result.auxiliary_electric_power_kw,

            "interval_electric_energy_kwh":
                result.interval_electric_energy_kwh,

            "total_electric_energy_kwh":
                model.total_electric_energy_kwh,

            "firing_fraction":
                result.firing_fraction,

            "effective_efficiency":
                result.effective_efficiency,

            "source": result.source.value,
            "confidence": result.confidence.value,
            "method": result.method,
            "inputs": result.inputs,
            "warnings": result.warnings,
        }