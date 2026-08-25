from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any

AIR_DENSITY_KG_PER_M3 = 1.204
AIR_SPECIFIC_HEAT_KJ_PER_KG_K = 1.006
CFM_TO_CUBIC_METERS_PER_SECOND = 0.00047194745


class RTUEnergySource(StrEnum):
    MEASURED_TOTAL_POWER = "measured-total-power"
    MEASURED_COMPONENT_POWER = "measured-component-power"
    LOAD_AND_COP = "load-and-cop"
    AIR_SIDE_ESTIMATE = "air-side-estimate"
    RATED_POWER = "rated-power"
    UNAVAILABLE = "unavailable"


class RTUEnergyConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class RTUEnergyConfig:
    supply_fan_rated_power_kw: float | None = None
    fan_power_exponent: float = 3.0
    minimum_fan_power_fraction: float = 0.0
    fan_running_fraction: float = 0.85
    cooling_cop: float | None = None
    heating_efficiency: float = 0.80
    auxiliary_electric_power_kw: float = 0.0
    include_gas_heating: bool = True

    def validate(self) -> None:
        if self.supply_fan_rated_power_kw is not None and self.supply_fan_rated_power_kw <= 0:
            raise ValueError("supply_fan_rated_power_kw must be greater than zero")
        if self.fan_power_exponent <= 0:
            raise ValueError("fan_power_exponent must be greater than zero")
        if not 0 <= self.minimum_fan_power_fraction <= 1:
            raise ValueError("minimum_fan_power_fraction must be between 0 and 1")
        if not 0 <= self.fan_running_fraction <= 1.5:
            raise ValueError("fan_running_fraction must be between 0 and 1.5")
        if self.cooling_cop is not None and self.cooling_cop <= 0:
            raise ValueError("cooling_cop must be greater than zero")
        if not 0 < self.heating_efficiency <= 1:
            raise ValueError("heating_efficiency must be greater than 0 and no greater than 1")
        if self.auxiliary_electric_power_kw < 0:
            raise ValueError("auxiliary_electric_power_kw cannot be negative")


@dataclass(frozen=True)
class RTUSnapshot:
    total_electric_power_kw: float | None = None
    compressor_power_kw: float | None = None
    supply_fan_power_kw: float | None = None
    cooling_load_kw: float | None = None
    heating_load_kw: float | None = None
    gas_heating_input_kw: float | None = None
    compressor_cop: float | None = None
    cooling_plr_percent: float | None = None
    heating_plr_percent: float | None = None
    supply_fan_running: bool | None = None
    supply_fan_speed_fraction: float | None = None
    supply_fan_speed_percent: float | None = None
    supply_air_flow_cubic_meters_per_second: float | None = None
    supply_air_flow_cfm: float | None = None
    supply_air_temperature_c: float | None = None
    mixed_air_temperature_c: float | None = None
    return_air_temperature_c: float | None = None
    outside_air_temperature_c: float | None = None

    def normalized_supply_speed(self) -> float | None:
        return _normalized_fraction(self.supply_fan_speed_fraction, self.supply_fan_speed_percent)

    def normalized_air_flow_m3_s(self) -> float | None:
        direct = _positive_or_zero(self.supply_air_flow_cubic_meters_per_second)
        if direct is not None:
            return direct
        cfm = _positive_or_zero(self.supply_air_flow_cfm)
        return cfm * CFM_TO_CUBIC_METERS_PER_SECOND if cfm is not None else None


@dataclass(frozen=True)
class RTUEnergyResult:
    electric_power_kw: float | None
    interval_electric_energy_kwh: float
    total_electric_energy_kwh: float
    gas_input_kw: float | None
    interval_gas_energy_kwh: float
    total_gas_energy_kwh: float
    compressor_power_kw: float | None
    supply_fan_power_kw: float | None
    auxiliary_electric_power_kw: float
    cooling_load_kw: float | None
    heating_load_kw: float | None
    compressor_cop: float | None
    source: RTUEnergySource
    confidence: RTUEnergyConfidence
    method: str
    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class RTUEnergyModel:
    def __init__(self, config: RTUEnergyConfig) -> None:
        config.validate()
        self.config = config
        self.total_electric_energy_kwh = 0.0
        self.total_gas_energy_kwh = 0.0

    def evaluate(self, snapshot: RTUSnapshot, elapsed_seconds: float) -> RTUEnergyResult:
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds cannot be negative")

        warnings: list[str] = []
        cooling_load = _positive_or_zero(snapshot.cooling_load_kw)
        if cooling_load is None:
            cooling_load = self._estimate_air_side_cooling_load(snapshot)
        heating_load = _positive_or_zero(snapshot.heating_load_kw)

        measured_cop = _positive_or_zero(snapshot.compressor_cop)
        effective_cop = measured_cop or self.config.cooling_cop

        measured_total = _positive_or_zero(snapshot.total_electric_power_kw)
        measured_compressor = _positive_or_zero(snapshot.compressor_power_kw)
        measured_fan = _positive_or_zero(snapshot.supply_fan_power_kw)

        compressor_power = measured_compressor
        fan_power = measured_fan

        if compressor_power is None and cooling_load is not None and effective_cop:
            compressor_power = cooling_load / effective_cop
        if fan_power is None:
            fan_power = self._estimate_fan_power(snapshot)

        auxiliary = self.config.auxiliary_electric_power_kw

        if measured_total is not None:
            electric_power = measured_total
            source = RTUEnergySource.MEASURED_TOTAL_POWER
            confidence = RTUEnergyConfidence.HIGH
            method = "measured-total-electric-power"
        elif measured_compressor is not None and measured_fan is not None:
            electric_power = measured_compressor + measured_fan + auxiliary
            source = RTUEnergySource.MEASURED_COMPONENT_POWER
            confidence = RTUEnergyConfidence.HIGH
            method = "measured-compressor-plus-fan-power"
        elif compressor_power is not None and fan_power is not None:
            electric_power = compressor_power + fan_power + auxiliary
            if snapshot.cooling_load_kw is not None and effective_cop is not None:
                source = RTUEnergySource.LOAD_AND_COP
                confidence = RTUEnergyConfidence.MEDIUM
                method = "cooling-load-divided-by-cop-plus-fan"
            else:
                source = RTUEnergySource.AIR_SIDE_ESTIMATE
                confidence = RTUEnergyConfidence.LOW
                method = "air-side-load-divided-by-cop-plus-fan"
        elif fan_power is not None:
            electric_power = fan_power + auxiliary
            source = RTUEnergySource.RATED_POWER
            confidence = RTUEnergyConfidence.LOW
            method = "fan-only-electric-power"
            warnings.append("Compressor electrical power could not be resolved.")
        else:
            electric_power = None
            source = RTUEnergySource.UNAVAILABLE
            confidence = RTUEnergyConfidence.NONE
            method = "insufficient-data"
            warnings.append("No measured total power or usable fallback inputs were available.")

        gas_input = self._resolve_gas_input(snapshot, heating_load, warnings)

        interval_electric = max(0.0, electric_power) * elapsed_seconds / 3600.0 if electric_power is not None else 0.0
        interval_gas = max(0.0, gas_input) * elapsed_seconds / 3600.0 if gas_input is not None else 0.0

        self.total_electric_energy_kwh += interval_electric
        self.total_gas_energy_kwh += interval_gas

        return RTUEnergyResult(
            electric_power_kw=electric_power,
            interval_electric_energy_kwh=interval_electric,
            total_electric_energy_kwh=self.total_electric_energy_kwh,
            gas_input_kw=gas_input,
            interval_gas_energy_kwh=interval_gas,
            total_gas_energy_kwh=self.total_gas_energy_kwh,
            compressor_power_kw=compressor_power,
            supply_fan_power_kw=fan_power,
            auxiliary_electric_power_kw=auxiliary,
            cooling_load_kw=cooling_load,
            heating_load_kw=heating_load,
            compressor_cop=effective_cop,
            source=source,
            confidence=confidence,
            method=method,
            inputs={
                "total_electric_power_kw": snapshot.total_electric_power_kw,
                "compressor_power_kw": snapshot.compressor_power_kw,
                "supply_fan_power_kw": snapshot.supply_fan_power_kw,
                "cooling_load_kw": snapshot.cooling_load_kw,
                "heating_load_kw": snapshot.heating_load_kw,
                "gas_heating_input_kw": snapshot.gas_heating_input_kw,
                "compressor_cop": snapshot.compressor_cop,
                "supply_fan_speed_fraction": snapshot.normalized_supply_speed(),
                "air_flow_m3_s": snapshot.normalized_air_flow_m3_s(),
                "supply_air_temperature_c": snapshot.supply_air_temperature_c,
                "mixed_air_temperature_c": snapshot.mixed_air_temperature_c,
                "return_air_temperature_c": snapshot.return_air_temperature_c,
                "outside_air_temperature_c": snapshot.outside_air_temperature_c,
            },
            warnings=warnings,
        )

    def reset(self) -> None:
        self.total_electric_energy_kwh = 0.0
        self.total_gas_energy_kwh = 0.0

    def _estimate_fan_power(self, snapshot: RTUSnapshot) -> float | None:
        rated = _positive_or_zero(self.config.supply_fan_rated_power_kw)
        if rated is None:
            return None
        if snapshot.supply_fan_running is False:
            return 0.0
        speed = snapshot.normalized_supply_speed()
        if speed is not None:
            if speed <= 0:
                return 0.0
            power_fraction = max(
                self.config.minimum_fan_power_fraction,
                speed ** self.config.fan_power_exponent,
            )
            return rated * power_fraction
        if snapshot.supply_fan_running is True:
            return rated * self.config.fan_running_fraction
        return None

    def _estimate_air_side_cooling_load(self, snapshot: RTUSnapshot) -> float | None:
        flow_m3_s = snapshot.normalized_air_flow_m3_s()
        if flow_m3_s is None:
            return None
        supply = _finite_or_none(snapshot.supply_air_temperature_c)
        reference = _finite_or_none(snapshot.mixed_air_temperature_c)
        if reference is None:
            reference = _finite_or_none(snapshot.return_air_temperature_c)
        if supply is None or reference is None:
            return None
        delta_t = reference - supply
        if delta_t <= 0:
            return 0.0
        mass_flow = flow_m3_s * AIR_DENSITY_KG_PER_M3
        return mass_flow * AIR_SPECIFIC_HEAT_KJ_PER_KG_K * delta_t

    def _resolve_gas_input(
        self,
        snapshot: RTUSnapshot,
        heating_load_kw: float | None,
        warnings: list[str],
    ) -> float | None:
        if not self.config.include_gas_heating:
            return 0.0
        measured = _positive_or_zero(snapshot.gas_heating_input_kw)
        if measured is not None:
            return measured
        if heating_load_kw is not None:
            return heating_load_kw / self.config.heating_efficiency
        if snapshot.heating_plr_percent not in (None, 0, 0.0):
            warnings.append("Heating PLR is non-zero but gas input/heating load could not be resolved.")
        return None


def _normalized_fraction(fraction: float | None, percent: float | None) -> float | None:
    if fraction is not None:
        value = _finite_or_none(fraction)
        if value is not None:
            return _clamp(value, 0.0, 1.0)
    if percent is not None:
        value = _finite_or_none(percent)
        if value is not None:
            return _clamp(value / 100.0, 0.0, 1.0)
    return None


def _finite_or_none(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if isfinite(result) else None


def _positive_or_zero(value: float | int | None) -> float | None:
    result = _finite_or_none(value)
    return None if result is None or result < 0 else result


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
