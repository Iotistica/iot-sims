from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any

KW_PER_REFRIGERATION_TON = 3.5168525
WATER_DENSITY_KG_PER_LITER = 0.997
WATER_SPECIFIC_HEAT_KJ_PER_KG_K = 4.186


class EnergySource(StrEnum):
    MEASURED = "measured"
    THERMAL_CALCULATION = "thermal-calculation"
    CAPACITY_LOAD = "capacity-load"
    RATED_POWER = "rated-power"
    UNAVAILABLE = "unavailable"


class EnergyConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class ChillerEnergyConfig:
    rated_capacity_kw: float | None = None
    rated_capacity_tons: float | None = None
    full_load_cop: float | None = None
    iplv_cop: float | None = None
    full_load_kw_per_ton: float | None = None
    rated_electrical_power_kw: float | None = None
    running_power_fraction: float = 0.85
    minimum_load_fraction: float = 0.0
    maximum_load_fraction: float = 1.0
    include_auxiliary_power_kw: float = 0.0

    def validate(self) -> None:
        if self.capacity_kw is None or self.capacity_kw <= 0:
            raise ValueError("Provide a positive rated_capacity_kw or rated_capacity_tons")
        for name, value in (("full_load_cop", self.full_load_cop), ("iplv_cop", self.iplv_cop), ("full_load_kw_per_ton", self.full_load_kw_per_ton), ("rated_electrical_power_kw", self.rated_electrical_power_kw)):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if not 0 <= self.running_power_fraction <= 1.5:
            raise ValueError("running_power_fraction must be between 0 and 1.5")
        if not 0 <= self.minimum_load_fraction <= 1:
            raise ValueError("minimum_load_fraction must be between 0 and 1")
        if not 0 < self.maximum_load_fraction <= 1.5:
            raise ValueError("maximum_load_fraction must be between 0 and 1.5")
        if self.minimum_load_fraction > self.maximum_load_fraction:
            raise ValueError("minimum_load_fraction cannot exceed maximum_load_fraction")

    @property
    def capacity_kw(self) -> float | None:
        if self.rated_capacity_kw is not None:
            return float(self.rated_capacity_kw)
        if self.rated_capacity_tons is not None:
            return float(self.rated_capacity_tons) * KW_PER_REFRIGERATION_TON
        return None

    @property
    def derived_full_load_cop(self) -> float | None:
        if self.full_load_cop is not None:
            return float(self.full_load_cop)
        if self.full_load_kw_per_ton is not None:
            return KW_PER_REFRIGERATION_TON / float(self.full_load_kw_per_ton)
        if self.capacity_kw is not None and self.rated_electrical_power_kw and self.rated_electrical_power_kw > 0:
            return self.capacity_kw / float(self.rated_electrical_power_kw)
        return None


@dataclass(frozen=True)
class ChillerSnapshot:
    running: bool | None = None
    measured_power_kw: float | None = None
    measured_energy_kwh: float | None = None
    load_fraction: float | None = None
    load_percent: float | None = None
    entering_water_temperature_c: float | None = None
    leaving_water_temperature_c: float | None = None
    water_flow_liters_per_second: float | None = None
    water_flow_kg_per_second: float | None = None
    measured_cooling_load_kw: float | None = None
    measured_cop: float | None = None
    outdoor_air_temperature_c: float | None = None
    condenser_entering_temperature_c: float | None = None

    def normalized_load_fraction(self) -> float | None:
        if self.load_fraction is not None:
            return float(self.load_fraction)
        if self.load_percent is not None:
            return float(self.load_percent) / 100.0
        return None


@dataclass(frozen=True)
class ChillerEnergyResult:
    power_kw: float | None
    interval_energy_kwh: float
    cooling_load_kw: float | None
    load_fraction: float | None
    effective_cop: float | None
    source: EnergySource
    confidence: EnergyConfidence
    method: str
    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class ChillerEnergyModel:
    def __init__(self, config: ChillerEnergyConfig) -> None:
        config.validate()
        self.config = config
        self.total_energy_kwh = 0.0

    def evaluate(self, snapshot: ChillerSnapshot, elapsed_seconds: float) -> ChillerEnergyResult:
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds cannot be negative")
        if snapshot.running is False:
            return ChillerEnergyResult(0.0, 0.0, 0.0, 0.0, None, EnergySource.RATED_POWER, EnergyConfidence.HIGH, "chiller-off", {"running": False})
        result = (self._from_measured_power(snapshot, elapsed_seconds) or self._from_thermal_load(snapshot, elapsed_seconds) or self._from_capacity_and_load(snapshot, elapsed_seconds) or self._from_rated_power(snapshot, elapsed_seconds) or self._unavailable(snapshot))
        self.total_energy_kwh += result.interval_energy_kwh
        return result

    def reset(self) -> None:
        self.total_energy_kwh = 0.0

    def _from_measured_power(self, snapshot: ChillerSnapshot, elapsed_seconds: float) -> ChillerEnergyResult | None:
        power = _positive_or_zero(snapshot.measured_power_kw)
        if power is None:
            return None
        cooling_load = self._resolve_cooling_load(snapshot)
        cop = cooling_load / power if cooling_load is not None and power > 0 else None
        return self._make_result(power, elapsed_seconds, cooling_load, self._resolve_load_fraction(snapshot, cooling_load), cop, EnergySource.MEASURED, EnergyConfidence.HIGH, "measured-electric-power", {"measured_power_kw": power})

    def _from_thermal_load(self, snapshot: ChillerSnapshot, elapsed_seconds: float) -> ChillerEnergyResult | None:
        cooling_load = self._calculate_water_side_load(snapshot)
        if cooling_load is None:
            cooling_load = _positive_or_zero(snapshot.measured_cooling_load_kw)
        if cooling_load is None:
            return None
        load_fraction = self._resolve_load_fraction(snapshot, cooling_load)
        cop = self._effective_cop(snapshot, load_fraction)
        if cop is None or cop <= 0:
            return None
        power = cooling_load / cop + self.config.include_auxiliary_power_kw
        return self._make_result(power, elapsed_seconds, cooling_load, load_fraction, cop, EnergySource.THERMAL_CALCULATION, EnergyConfidence.HIGH, "water-flow-delta-t-divided-by-cop", {"water_flow_liters_per_second": snapshot.water_flow_liters_per_second, "water_flow_kg_per_second": snapshot.water_flow_kg_per_second, "entering_water_temperature_c": snapshot.entering_water_temperature_c, "leaving_water_temperature_c": snapshot.leaving_water_temperature_c, "cooling_load_kw": cooling_load, "effective_cop": cop})

    def _from_capacity_and_load(self, snapshot: ChillerSnapshot, elapsed_seconds: float) -> ChillerEnergyResult | None:
        capacity = self.config.capacity_kw
        load_fraction = snapshot.normalized_load_fraction()
        if capacity is None or load_fraction is None:
            return None
        load_fraction = self._clamp_load(load_fraction)
        cooling_load = capacity * load_fraction
        cop = self._effective_cop(snapshot, load_fraction)
        if cop is None or cop <= 0:
            return None
        power = cooling_load / cop + self.config.include_auxiliary_power_kw
        return self._make_result(power, elapsed_seconds, cooling_load, load_fraction, cop, EnergySource.CAPACITY_LOAD, EnergyConfidence.MEDIUM, "rated-capacity-times-load-divided-by-cop", {"rated_capacity_kw": capacity, "load_fraction": load_fraction, "effective_cop": cop})

    def _from_rated_power(self, snapshot: ChillerSnapshot, elapsed_seconds: float) -> ChillerEnergyResult | None:
        rated_power = _positive_or_zero(self.config.rated_electrical_power_kw)
        if rated_power is None:
            return None
        load_fraction = snapshot.normalized_load_fraction()
        if load_fraction is None:
            running_fraction = self.config.running_power_fraction
            confidence = EnergyConfidence.LOW
            method = "rated-power-running-factor"
        else:
            running_fraction = self._clamp_load(load_fraction)
            confidence = EnergyConfidence.MEDIUM
            method = "rated-power-times-load"
        power = rated_power * running_fraction + self.config.include_auxiliary_power_kw
        return self._make_result(power, elapsed_seconds, None, load_fraction, None, EnergySource.RATED_POWER, confidence, method, {"rated_electrical_power_kw": rated_power, "running_fraction": running_fraction}, ["Power was estimated from rated electrical power."])

    def _unavailable(self, snapshot: ChillerSnapshot) -> ChillerEnergyResult:
        return ChillerEnergyResult(None, 0.0, None, snapshot.normalized_load_fraction(), None, EnergySource.UNAVAILABLE, EnergyConfidence.NONE, "insufficient-data", warnings=["No measured power, thermal inputs, load fraction, or rated electrical power were available."])

    def _calculate_water_side_load(self, snapshot: ChillerSnapshot) -> float | None:
        entering = _finite_or_none(snapshot.entering_water_temperature_c)
        leaving = _finite_or_none(snapshot.leaving_water_temperature_c)
        if entering is None or leaving is None:
            return None
        mass_flow = _positive_or_zero(snapshot.water_flow_kg_per_second)
        if mass_flow is None:
            volume_flow = _positive_or_zero(snapshot.water_flow_liters_per_second)
            if volume_flow is None:
                return None
            mass_flow = volume_flow * WATER_DENSITY_KG_PER_LITER
        return mass_flow * WATER_SPECIFIC_HEAT_KJ_PER_KG_K * abs(entering - leaving)

    def _resolve_cooling_load(self, snapshot: ChillerSnapshot) -> float | None:
        measured = _positive_or_zero(snapshot.measured_cooling_load_kw)
        if measured is not None:
            return measured
        thermal = self._calculate_water_side_load(snapshot)
        if thermal is not None:
            return thermal
        capacity = self.config.capacity_kw
        load_fraction = snapshot.normalized_load_fraction()
        if capacity is None or load_fraction is None:
            return None
        return capacity * self._clamp_load(load_fraction)

    def _resolve_load_fraction(self, snapshot: ChillerSnapshot, cooling_load_kw: float | None) -> float | None:
        provided = snapshot.normalized_load_fraction()
        if provided is not None:
            return self._clamp_load(provided)
        capacity = self.config.capacity_kw
        if cooling_load_kw is None or capacity is None or capacity <= 0:
            return None
        return self._clamp_load(cooling_load_kw / capacity)

    def _effective_cop(self, snapshot: ChillerSnapshot, load_fraction: float | None) -> float | None:
        measured = _positive_or_zero(snapshot.measured_cop)
        if measured is not None:
            return measured
        full_load = self.config.derived_full_load_cop
        if full_load is None:
            return None
        if self.config.iplv_cop is None or load_fraction is None:
            return full_load
        return self._interpolate_part_load_cop(full_load, float(self.config.iplv_cop), load_fraction)

    @staticmethod
    def _interpolate_part_load_cop(full_load_cop: float, iplv_cop: float, load_fraction: float) -> float:
        load = max(0.0, min(1.0, load_fraction))
        if load >= 0.75:
            ratio = (load - 0.75) / 0.25
            return iplv_cop + (full_load_cop - iplv_cop) * ratio
        if load >= 0.25:
            return iplv_cop
        minimum_cop = max(full_load_cop * 0.65, 0.1)
        return minimum_cop + (iplv_cop - minimum_cop) * (load / 0.25)

    def _clamp_load(self, value: float) -> float:
        return max(self.config.minimum_load_fraction, min(self.config.maximum_load_fraction, float(value)))

    @staticmethod
    def _make_result(power_kw: float, elapsed_seconds: float, cooling_load_kw: float | None, load_fraction: float | None, effective_cop: float | None, source: EnergySource, confidence: EnergyConfidence, method: str, inputs: dict[str, Any], warnings: list[str] | None = None) -> ChillerEnergyResult:
        power = max(0.0, float(power_kw))
        return ChillerEnergyResult(power, power * elapsed_seconds / 3600.0, cooling_load_kw, load_fraction, effective_cop, source, confidence, method, inputs, warnings or [])


def _finite_or_none(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if isfinite(result) else None


def _positive_or_zero(value: float | int | None) -> float | None:
    result = _finite_or_none(value)
    if result is None or result < 0:
        return None
    return result
