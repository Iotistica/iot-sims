from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


AIR_DENSITY_KG_PER_M3 = 1.204
AIR_SPECIFIC_HEAT_KJ_PER_KG_K = 1.006
CFM_TO_CUBIC_METERS_PER_SECOND = 0.00047194745


class AHUEnergySource(StrEnum):
    MEASURED = "measured"
    FAN_SPEED = "fan-speed"
    AIR_SIDE_THERMAL = "air-side-thermal"
    RATED_POWER = "rated-power"
    UNAVAILABLE = "unavailable"


class AHUEnergyConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class AHUEnergyConfig:
    """
    Static AHU characteristics.

    supply_fan_rated_power_kw / return_fan_rated_power_kw:
        Full-speed electrical input of each fan.

    fan_power_exponent:
        Approximation for VFD fan power. A value near 3.0 follows
        the fan affinity-law relationship.

    minimum_fan_power_fraction:
        Minimum running fraction for drive and motor losses.

    cooling_efficiency_cop:
        Optional COP used to convert estimated cooling-coil thermal
        load into upstream electrical power.

    heating_efficiency:
        Optional heating-system efficiency used to convert estimated
        heating-coil output into upstream fuel or electrical input.

    include_coil_energy:
        When False, only fan electrical power is reported as AHU power.
    """

    supply_fan_rated_power_kw: float | None = None
    return_fan_rated_power_kw: float | None = None

    fan_power_exponent: float = 3.0
    minimum_fan_power_fraction: float = 0.08

    supply_fan_running_fraction: float = 0.85
    return_fan_running_fraction: float = 0.85

    cooling_efficiency_cop: float | None = None
    heating_efficiency: float | None = None
    include_coil_energy: bool = False

    auxiliary_power_kw: float = 0.0

    def validate(self) -> None:
        for name, value in (
            (
                "supply_fan_rated_power_kw",
                self.supply_fan_rated_power_kw,
            ),
            (
                "return_fan_rated_power_kw",
                self.return_fan_rated_power_kw,
            ),
        ):
            if value is not None and value <= 0:
                raise ValueError(
                    f"{name} must be greater than zero"
                )

        if (
            self.supply_fan_rated_power_kw is None
            and self.return_fan_rated_power_kw is None
        ):
            raise ValueError(
                "Provide at least one fan rated power"
            )

        if self.fan_power_exponent <= 0:
            raise ValueError(
                "fan_power_exponent must be greater than zero"
            )

        if not 0 <= self.minimum_fan_power_fraction <= 1:
            raise ValueError(
                "minimum_fan_power_fraction must be between 0 and 1"
            )

        for name, value in (
            (
                "supply_fan_running_fraction",
                self.supply_fan_running_fraction,
            ),
            (
                "return_fan_running_fraction",
                self.return_fan_running_fraction,
            ),
        ):
            if not 0 <= value <= 1.5:
                raise ValueError(
                    f"{name} must be between 0 and 1.5"
                )

        if (
            self.cooling_efficiency_cop is not None
            and self.cooling_efficiency_cop <= 0
        ):
            raise ValueError(
                "cooling_efficiency_cop must be greater than zero"
            )

        if (
            self.heating_efficiency is not None
            and not 0 < self.heating_efficiency <= 1
        ):
            raise ValueError(
                "heating_efficiency must be greater than 0 "
                "and no greater than 1"
            )

        if self.auxiliary_power_kw < 0:
            raise ValueError(
                "auxiliary_power_kw cannot be negative"
            )


@dataclass(frozen=True)
class AHUSnapshot:
    """
    Live AHU values from Brick-tagged simulation points.
    """

    measured_power_kw: float | None = None

    supply_fan_running: bool | None = None
    return_fan_running: bool | None = None

    supply_fan_speed_fraction: float | None = None
    supply_fan_speed_percent: float | None = None

    return_fan_speed_fraction: float | None = None
    return_fan_speed_percent: float | None = None

    supply_air_flow_cubic_meters_per_second: float | None = None
    supply_air_flow_cfm: float | None = None

    supply_air_temperature_c: float | None = None
    return_air_temperature_c: float | None = None
    mixed_air_temperature_c: float | None = None
    outside_air_temperature_c: float | None = None

    cooling_valve_position_percent: float | None = None
    heating_valve_position_percent: float | None = None

    def normalized_supply_speed(self) -> float | None:
        return _normalized_fraction(
            self.supply_fan_speed_fraction,
            self.supply_fan_speed_percent,
        )

    def normalized_return_speed(self) -> float | None:
        return _normalized_fraction(
            self.return_fan_speed_fraction,
            self.return_fan_speed_percent,
        )

    def normalized_air_flow_m3_s(self) -> float | None:
        direct = _positive_or_zero(
            self.supply_air_flow_cubic_meters_per_second
        )
        if direct is not None:
            return direct

        cfm = _positive_or_zero(
            self.supply_air_flow_cfm
        )
        if cfm is not None:
            return cfm * CFM_TO_CUBIC_METERS_PER_SECOND

        return None


@dataclass(frozen=True)
class AHUEnergyResult:
    power_kw: float | None
    interval_energy_kwh: float
    total_energy_kwh: float

    supply_fan_power_kw: float | None
    return_fan_power_kw: float | None
    auxiliary_power_kw: float

    cooling_load_kw: float | None
    heating_load_kw: float | None

    cooling_input_power_kw: float | None
    heating_input_power_kw: float | None

    source: AHUEnergySource
    confidence: AHUEnergyConfidence
    method: str

    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class AHUEnergyModel:
    def __init__(
        self,
        config: AHUEnergyConfig,
    ) -> None:
        config.validate()
        self.config = config
        self.total_energy_kwh = 0.0

    def evaluate(
        self,
        snapshot: AHUSnapshot,
        elapsed_seconds: float,
    ) -> AHUEnergyResult:
        if elapsed_seconds < 0:
            raise ValueError(
                "elapsed_seconds cannot be negative"
            )

        measured = _positive_or_zero(
            snapshot.measured_power_kw
        )

        if measured is not None:
            source = AHUEnergySource.MEASURED
            confidence = AHUEnergyConfidence.HIGH
            method = "measured-electric-power"
            supply_power = None
            return_power = None
            fan_power = measured
        else:
            supply_power = self._fan_power(
                rated_power_kw=(
                    self.config.supply_fan_rated_power_kw
                ),
                running=snapshot.supply_fan_running,
                speed_fraction=(
                    snapshot.normalized_supply_speed()
                ),
                fallback_running_fraction=(
                    self.config
                    .supply_fan_running_fraction
                ),
            )

            return_power = self._fan_power(
                rated_power_kw=(
                    self.config.return_fan_rated_power_kw
                ),
                running=snapshot.return_fan_running,
                speed_fraction=(
                    snapshot.normalized_return_speed()
                ),
                fallback_running_fraction=(
                    self.config
                    .return_fan_running_fraction
                ),
            )

            fan_values = [
                value
                for value in (
                    supply_power,
                    return_power,
                )
                if value is not None
            ]

            fan_power = (
                sum(fan_values)
                if fan_values
                else None
            )

            has_speed = (
                snapshot.normalized_supply_speed()
                is not None
                or snapshot.normalized_return_speed()
                is not None
            )

            if fan_power is None:
                source = AHUEnergySource.UNAVAILABLE
                confidence = AHUEnergyConfidence.NONE
                method = "insufficient-data"
            elif has_speed:
                source = AHUEnergySource.FAN_SPEED
                confidence = AHUEnergyConfidence.MEDIUM
                method = "rated-fan-power-times-speed-curve"
            else:
                source = AHUEnergySource.RATED_POWER
                confidence = AHUEnergyConfidence.LOW
                method = "rated-fan-power-running-factor"

        cooling_load, heating_load = (
            self._calculate_air_side_loads(snapshot)
        )

        cooling_input = None
        if (
            cooling_load is not None
            and self.config.cooling_efficiency_cop
            is not None
        ):
            cooling_input = (
                cooling_load
                / self.config.cooling_efficiency_cop
            )

        heating_input = None
        if (
            heating_load is not None
            and self.config.heating_efficiency
            is not None
        ):
            heating_input = (
                heating_load
                / self.config.heating_efficiency
            )

        auxiliary = self.config.auxiliary_power_kw

        total_power = fan_power

        if total_power is not None:
            total_power += auxiliary

            if self.config.include_coil_energy:
                total_power += cooling_input or 0.0
                total_power += heating_input or 0.0

        interval_energy = (
            total_power
            * elapsed_seconds
            / 3600.0
            if total_power is not None
            else 0.0
        )

        self.total_energy_kwh += interval_energy

        warnings: list[str] = []

        if source is AHUEnergySource.RATED_POWER:
            warnings.append(
                "Fan power was estimated from rated power "
                "and a running factor."
            )

        if (
            self.config.include_coil_energy
            and cooling_load is not None
            and cooling_input is None
        ):
            warnings.append(
                "Cooling load was estimated, but no cooling COP "
                "was configured."
            )

        if (
            self.config.include_coil_energy
            and heating_load is not None
            and heating_input is None
        ):
            warnings.append(
                "Heating load was estimated, but no heating "
                "efficiency was configured."
            )

        if total_power is None:
            warnings.append(
                "No measured power or usable fan rating was available."
            )

        return AHUEnergyResult(
            power_kw=total_power,
            interval_energy_kwh=interval_energy,
            total_energy_kwh=self.total_energy_kwh,
            supply_fan_power_kw=supply_power,
            return_fan_power_kw=return_power,
            auxiliary_power_kw=auxiliary,
            cooling_load_kw=cooling_load,
            heating_load_kw=heating_load,
            cooling_input_power_kw=cooling_input,
            heating_input_power_kw=heating_input,
            source=source,
            confidence=confidence,
            method=method,
            inputs={
                "supply_fan_speed_fraction": (
                    snapshot.normalized_supply_speed()
                ),
                "return_fan_speed_fraction": (
                    snapshot.normalized_return_speed()
                ),
                "air_flow_m3_s": (
                    snapshot.normalized_air_flow_m3_s()
                ),
                "supply_air_temperature_c": (
                    snapshot.supply_air_temperature_c
                ),
                "return_air_temperature_c": (
                    snapshot.return_air_temperature_c
                ),
                "mixed_air_temperature_c": (
                    snapshot.mixed_air_temperature_c
                ),
                "outside_air_temperature_c": (
                    snapshot.outside_air_temperature_c
                ),
                "include_coil_energy": (
                    self.config.include_coil_energy
                ),
            },
            warnings=warnings,
        )

    def reset(self) -> None:
        self.total_energy_kwh = 0.0

    def _fan_power(
        self,
        *,
        rated_power_kw: float | None,
        running: bool | None,
        speed_fraction: float | None,
        fallback_running_fraction: float,
    ) -> float | None:
        rated = _positive_or_zero(rated_power_kw)

        if rated is None:
            return None

        if running is False:
            return 0.0

        if speed_fraction is not None:
            speed = _clamp(
                speed_fraction,
                0.0,
                1.0,
            )

            if speed <= 0:
                return 0.0

            power_fraction = max(
                self.config.minimum_fan_power_fraction,
                speed
                ** self.config.fan_power_exponent,
            )

            return rated * power_fraction

        if running is True:
            return rated * fallback_running_fraction

        return None

    def _calculate_air_side_loads(
        self,
        snapshot: AHUSnapshot,
    ) -> tuple[float | None, float | None]:
        flow_m3_s = snapshot.normalized_air_flow_m3_s()

        if flow_m3_s is None:
            return None, None

        supply = _finite_or_none(
            snapshot.supply_air_temperature_c
        )

        reference = _finite_or_none(
            snapshot.mixed_air_temperature_c
        )

        if reference is None:
            reference = _finite_or_none(
                snapshot.return_air_temperature_c
            )

        if supply is None or reference is None:
            return None, None

        mass_flow = flow_m3_s * AIR_DENSITY_KG_PER_M3
        delta_t = supply - reference

        thermal_kw = (
            mass_flow
            * AIR_SPECIFIC_HEAT_KJ_PER_KG_K
            * abs(delta_t)
        )

        if delta_t < 0:
            return thermal_kw, None

        if delta_t > 0:
            return None, thermal_kw

        return 0.0, 0.0


def _normalized_fraction(
    fraction: float | None,
    percent: float | None,
) -> float | None:
    if fraction is not None:
        value = _finite_or_none(fraction)
        if value is not None:
            return _clamp(value, 0.0, 1.0)

    if percent is not None:
        value = _finite_or_none(percent)
        if value is not None:
            return _clamp(
                value / 100.0,
                0.0,
                1.0,
            )

    return None


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


def _clamp(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
    return max(minimum, min(maximum, value))
