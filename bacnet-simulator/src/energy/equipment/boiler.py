from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


WATER_DENSITY_KG_PER_LITER = 0.997
WATER_SPECIFIC_HEAT_KJ_PER_KG_K = 4.186
DEFAULT_NATURAL_GAS_KWH_PER_CUBIC_METER = 10.55
CUBIC_FEET_PER_CUBIC_METER = 35.3146667


class BoilerEnergySource(StrEnum):
    MEASURED_FUEL_FLOW = "measured-fuel-flow"
    THERMAL_CALCULATION = "thermal-calculation"
    CAPACITY_FIRING_RATE = "capacity-firing-rate"
    RATED_FUEL_INPUT = "rated-fuel-input"
    UNAVAILABLE = "unavailable"


class BoilerEnergyConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class BoilerEnergyConfig:
    rated_thermal_capacity_kw: float | None = None
    thermal_efficiency: float = 0.90
    rated_fuel_input_kw: float | None = None
    auxiliary_electric_power_kw: float = 0.0
    natural_gas_kwh_per_cubic_meter: float = (
        DEFAULT_NATURAL_GAS_KWH_PER_CUBIC_METER
    )
    running_fuel_fraction: float = 0.70
    minimum_firing_fraction: float = 0.0
    maximum_firing_fraction: float = 1.0

    def validate(self) -> None:
        if not 0 < self.thermal_efficiency <= 1:
            raise ValueError(
                "thermal_efficiency must be greater than 0 "
                "and no greater than 1"
            )

        if (
            self.rated_thermal_capacity_kw is not None
            and self.rated_thermal_capacity_kw <= 0
        ):
            raise ValueError(
                "rated_thermal_capacity_kw must be greater than zero"
            )

        if (
            self.rated_fuel_input_kw is not None
            and self.rated_fuel_input_kw <= 0
        ):
            raise ValueError(
                "rated_fuel_input_kw must be greater than zero"
            )

        if self.auxiliary_electric_power_kw < 0:
            raise ValueError(
                "auxiliary_electric_power_kw cannot be negative"
            )

        if self.natural_gas_kwh_per_cubic_meter <= 0:
            raise ValueError(
                "natural_gas_kwh_per_cubic_meter must be greater than zero"
            )

        if not 0 <= self.running_fuel_fraction <= 1.5:
            raise ValueError(
                "running_fuel_fraction must be between 0 and 1.5"
            )

        if not 0 <= self.minimum_firing_fraction <= 1:
            raise ValueError(
                "minimum_firing_fraction must be between 0 and 1"
            )

        if not 0 < self.maximum_firing_fraction <= 1.5:
            raise ValueError(
                "maximum_firing_fraction must be between 0 and 1.5"
            )

        if self.minimum_firing_fraction > self.maximum_firing_fraction:
            raise ValueError(
                "minimum_firing_fraction cannot exceed "
                "maximum_firing_fraction"
            )

        if (
            self.rated_thermal_capacity_kw is None
            and self.rated_fuel_input_kw is None
        ):
            raise ValueError(
                "Provide rated_thermal_capacity_kw or rated_fuel_input_kw"
            )

    @property
    def derived_rated_fuel_input_kw(self) -> float | None:
        if self.rated_fuel_input_kw is not None:
            return float(self.rated_fuel_input_kw)

        if self.rated_thermal_capacity_kw is not None:
            return (
                float(self.rated_thermal_capacity_kw)
                / self.thermal_efficiency
            )

        return None


@dataclass(frozen=True)
class BoilerSnapshot:
    running: bool | None = None

    firing_fraction: float | None = None
    firing_percent: float | None = None

    entering_water_temperature_c: float | None = None
    leaving_water_temperature_c: float | None = None

    water_flow_liters_per_second: float | None = None
    water_flow_kg_per_second: float | None = None

    measured_thermal_output_kw: float | None = None
    measured_fuel_input_kw: float | None = None

    natural_gas_flow_cubic_meters_per_hour: float | None = None
    natural_gas_flow_cubic_feet_per_minute: float | None = None

    measured_auxiliary_electric_power_kw: float | None = None

    def normalized_firing_fraction(self) -> float | None:
        if self.firing_fraction is not None:
            return float(self.firing_fraction)

        if self.firing_percent is not None:
            return float(self.firing_percent) / 100.0

        return None


@dataclass(frozen=True)
class BoilerEnergyResult:
    thermal_output_kw: float | None
    fuel_input_kw: float | None
    auxiliary_electric_power_kw: float

    interval_fuel_energy_kwh: float
    total_fuel_energy_kwh: float

    interval_electric_energy_kwh: float
    total_electric_energy_kwh: float

    firing_fraction: float | None
    effective_efficiency: float | None

    source: BoilerEnergySource
    confidence: BoilerEnergyConfidence
    method: str

    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class BoilerEnergyModel:
    def __init__(
        self,
        config: BoilerEnergyConfig,
    ) -> None:
        config.validate()
        self.config = config
        self.total_fuel_energy_kwh = 0.0
        self.total_electric_energy_kwh = 0.0

    def evaluate(
        self,
        snapshot: BoilerSnapshot,
        elapsed_seconds: float,
    ) -> BoilerEnergyResult:
        if elapsed_seconds < 0:
            raise ValueError(
                "elapsed_seconds cannot be negative"
            )

        if snapshot.running is False:
            return BoilerEnergyResult(
                thermal_output_kw=0.0,
                fuel_input_kw=0.0,
                auxiliary_electric_power_kw=0.0,
                interval_fuel_energy_kwh=0.0,
                total_fuel_energy_kwh=self.total_fuel_energy_kwh,
                interval_electric_energy_kwh=0.0,
                total_electric_energy_kwh=(
                    self.total_electric_energy_kwh
                ),
                firing_fraction=0.0,
                effective_efficiency=None,
                source=BoilerEnergySource.RATED_FUEL_INPUT,
                confidence=BoilerEnergyConfidence.HIGH,
                method="boiler-off",
                inputs={"running": False},
            )

        calc = (
            self._from_measured_fuel_flow(snapshot)
            or self._from_water_side_thermal_load(snapshot)
            or self._from_capacity_and_firing_rate(snapshot)
            or self._from_rated_fuel_input(snapshot)
            or self._unavailable(snapshot)
        )

        auxiliary_power = self._resolve_auxiliary_power(snapshot)
        fuel_input_kw = calc["fuel_input_kw"]
        thermal_output_kw = calc["thermal_output_kw"]

        interval_fuel_energy_kwh = (
            max(0.0, fuel_input_kw) * elapsed_seconds / 3600.0
            if fuel_input_kw is not None
            else 0.0
        )

        interval_electric_energy_kwh = (
            max(0.0, auxiliary_power) * elapsed_seconds / 3600.0
        )

        self.total_fuel_energy_kwh += interval_fuel_energy_kwh
        self.total_electric_energy_kwh += (
            interval_electric_energy_kwh
        )

        efficiency = None
        if (
            fuel_input_kw is not None
            and fuel_input_kw > 0
            and thermal_output_kw is not None
        ):
            efficiency = thermal_output_kw / fuel_input_kw

        return BoilerEnergyResult(
            thermal_output_kw=thermal_output_kw,
            fuel_input_kw=fuel_input_kw,
            auxiliary_electric_power_kw=auxiliary_power,
            interval_fuel_energy_kwh=interval_fuel_energy_kwh,
            total_fuel_energy_kwh=self.total_fuel_energy_kwh,
            interval_electric_energy_kwh=(
                interval_electric_energy_kwh
            ),
            total_electric_energy_kwh=(
                self.total_electric_energy_kwh
            ),
            firing_fraction=calc["firing_fraction"],
            effective_efficiency=efficiency,
            source=calc["source"],
            confidence=calc["confidence"],
            method=calc["method"],
            inputs=calc["inputs"],
            warnings=calc["warnings"],
        )

    def reset(self) -> None:
        self.total_fuel_energy_kwh = 0.0
        self.total_electric_energy_kwh = 0.0

    def _from_measured_fuel_flow(
        self,
        snapshot: BoilerSnapshot,
    ) -> dict[str, Any] | None:
        measured_fuel_kw = _positive_or_zero(
            snapshot.measured_fuel_input_kw
        )

        if measured_fuel_kw is None:
            measured_fuel_kw = self._fuel_power_from_gas_flow(
                snapshot
            )

        if measured_fuel_kw is None:
            return None

        measured_thermal = _positive_or_zero(
            snapshot.measured_thermal_output_kw
        )

        if measured_thermal is None:
            measured_thermal = (
                measured_fuel_kw
                * self.config.thermal_efficiency
            )

        return self._calculation(
            thermal_output_kw=measured_thermal,
            fuel_input_kw=measured_fuel_kw,
            firing_fraction=self._resolve_firing_fraction(
                snapshot
            ),
            source=BoilerEnergySource.MEASURED_FUEL_FLOW,
            confidence=BoilerEnergyConfidence.HIGH,
            method="measured-fuel-flow",
            inputs={
                "measured_fuel_input_kw": (
                    snapshot.measured_fuel_input_kw
                ),
                "natural_gas_flow_cubic_meters_per_hour": (
                    snapshot.natural_gas_flow_cubic_meters_per_hour
                ),
                "natural_gas_flow_cubic_feet_per_minute": (
                    snapshot.natural_gas_flow_cubic_feet_per_minute
                ),
                "fuel_energy_content_kwh_per_m3": (
                    self.config
                    .natural_gas_kwh_per_cubic_meter
                ),
            },
        )

    def _from_water_side_thermal_load(
        self,
        snapshot: BoilerSnapshot,
    ) -> dict[str, Any] | None:
        thermal_output = self._calculate_water_side_load(
            snapshot
        )

        if thermal_output is None:
            thermal_output = _positive_or_zero(
                snapshot.measured_thermal_output_kw
            )

        if thermal_output is None:
            return None

        fuel_input = (
            thermal_output
            / self.config.thermal_efficiency
        )

        return self._calculation(
            thermal_output_kw=thermal_output,
            fuel_input_kw=fuel_input,
            firing_fraction=self._resolve_firing_fraction(
                snapshot
            ),
            source=BoilerEnergySource.THERMAL_CALCULATION,
            confidence=BoilerEnergyConfidence.HIGH,
            method="water-flow-delta-t-divided-by-efficiency",
            inputs={
                "water_flow_liters_per_second": (
                    snapshot.water_flow_liters_per_second
                ),
                "water_flow_kg_per_second": (
                    snapshot.water_flow_kg_per_second
                ),
                "entering_water_temperature_c": (
                    snapshot.entering_water_temperature_c
                ),
                "leaving_water_temperature_c": (
                    snapshot.leaving_water_temperature_c
                ),
                "thermal_output_kw": thermal_output,
                "thermal_efficiency": (
                    self.config.thermal_efficiency
                ),
            },
        )

    def _from_capacity_and_firing_rate(
        self,
        snapshot: BoilerSnapshot,
    ) -> dict[str, Any] | None:
        capacity = _positive_or_zero(
            self.config.rated_thermal_capacity_kw
        )
        firing_fraction = self._resolve_firing_fraction(
            snapshot
        )

        if capacity is None or firing_fraction is None:
            return None

        thermal_output = capacity * firing_fraction
        fuel_input = (
            thermal_output
            / self.config.thermal_efficiency
        )

        return self._calculation(
            thermal_output_kw=thermal_output,
            fuel_input_kw=fuel_input,
            firing_fraction=firing_fraction,
            source=BoilerEnergySource.CAPACITY_FIRING_RATE,
            confidence=BoilerEnergyConfidence.MEDIUM,
            method="rated-capacity-times-firing-rate",
            inputs={
                "rated_thermal_capacity_kw": capacity,
                "firing_fraction": firing_fraction,
                "thermal_efficiency": (
                    self.config.thermal_efficiency
                ),
            },
        )

    def _from_rated_fuel_input(
        self,
        snapshot: BoilerSnapshot,
    ) -> dict[str, Any] | None:
        rated_fuel_input = _positive_or_zero(
            self.config.derived_rated_fuel_input_kw
        )

        if rated_fuel_input is None:
            return None

        firing_fraction = self._resolve_firing_fraction(
            snapshot
        )

        if firing_fraction is None:
            firing_fraction = (
                self.config.running_fuel_fraction
            )
            confidence = BoilerEnergyConfidence.LOW
            method = "rated-fuel-input-running-factor"
        else:
            confidence = BoilerEnergyConfidence.MEDIUM
            method = "rated-fuel-input-times-firing-rate"

        fuel_input = rated_fuel_input * firing_fraction
        thermal_output = (
            fuel_input
            * self.config.thermal_efficiency
        )

        return self._calculation(
            thermal_output_kw=thermal_output,
            fuel_input_kw=fuel_input,
            firing_fraction=firing_fraction,
            source=BoilerEnergySource.RATED_FUEL_INPUT,
            confidence=confidence,
            method=method,
            inputs={
                "rated_fuel_input_kw": rated_fuel_input,
                "firing_fraction": firing_fraction,
                "thermal_efficiency": (
                    self.config.thermal_efficiency
                ),
            },
            warnings=[
                "Fuel use was estimated from rated boiler data."
            ],
        )

    def _unavailable(
        self,
        snapshot: BoilerSnapshot,
    ) -> dict[str, Any]:
        return self._calculation(
            thermal_output_kw=None,
            fuel_input_kw=None,
            firing_fraction=self._resolve_firing_fraction(
                snapshot
            ),
            source=BoilerEnergySource.UNAVAILABLE,
            confidence=BoilerEnergyConfidence.NONE,
            method="insufficient-data",
            inputs={},
            warnings=[
                "No measured fuel flow, water-side thermal inputs, "
                "firing-rate data, or rated fuel input were available."
            ],
        )

    def _calculate_water_side_load(
        self,
        snapshot: BoilerSnapshot,
    ) -> float | None:
        entering = _finite_or_none(
            snapshot.entering_water_temperature_c
        )
        leaving = _finite_or_none(
            snapshot.leaving_water_temperature_c
        )

        if entering is None or leaving is None:
            return None

        mass_flow = _positive_or_zero(
            snapshot.water_flow_kg_per_second
        )

        if mass_flow is None:
            volume_flow = _positive_or_zero(
                snapshot.water_flow_liters_per_second
            )

            if volume_flow is None:
                return None

            mass_flow = (
                volume_flow
                * WATER_DENSITY_KG_PER_LITER
            )

        delta_t = abs(leaving - entering)

        return (
            mass_flow
            * WATER_SPECIFIC_HEAT_KJ_PER_KG_K
            * delta_t
        )

    def _fuel_power_from_gas_flow(
        self,
        snapshot: BoilerSnapshot,
    ) -> float | None:
        cubic_meters_per_hour = _positive_or_zero(
            snapshot.natural_gas_flow_cubic_meters_per_hour
        )

        if cubic_meters_per_hour is None:
            cubic_feet_per_minute = _positive_or_zero(
                snapshot
                .natural_gas_flow_cubic_feet_per_minute
            )

            if cubic_feet_per_minute is None:
                return None

            cubic_meters_per_hour = (
                cubic_feet_per_minute
                * 60.0
                / CUBIC_FEET_PER_CUBIC_METER
            )

        return (
            cubic_meters_per_hour
            * self.config.natural_gas_kwh_per_cubic_meter
        )

    def _resolve_firing_fraction(
        self,
        snapshot: BoilerSnapshot,
    ) -> float | None:
        value = snapshot.normalized_firing_fraction()

        if value is None:
            return None

        return max(
            self.config.minimum_firing_fraction,
            min(
                self.config.maximum_firing_fraction,
                float(value),
            ),
        )

    def _resolve_auxiliary_power(
        self,
        snapshot: BoilerSnapshot,
    ) -> float:
        measured = _positive_or_zero(
            snapshot.measured_auxiliary_electric_power_kw
        )

        if measured is not None:
            return measured

        return max(
            0.0,
            float(
                self.config.auxiliary_electric_power_kw
            ),
        )

    @staticmethod
    def _calculation(
        *,
        thermal_output_kw: float | None,
        fuel_input_kw: float | None,
        firing_fraction: float | None,
        source: BoilerEnergySource,
        confidence: BoilerEnergyConfidence,
        method: str,
        inputs: dict[str, Any],
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "thermal_output_kw": thermal_output_kw,
            "fuel_input_kw": fuel_input_kw,
            "firing_fraction": firing_fraction,
            "source": source,
            "confidence": confidence,
            "method": method,
            "inputs": inputs,
            "warnings": warnings or [],
        }


def _finite_or_none(
    value: float | int | None,
) -> float | None:
    if value is None:
        return None

    result = float(value)

    if not isfinite(result):
        return None

    return result


def _positive_or_zero(
    value: float | int | None,
) -> float | None:
    result = _finite_or_none(value)

    if result is None or result < 0:
        return None

    return result