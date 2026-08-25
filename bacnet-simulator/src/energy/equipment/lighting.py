from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class LightingEnergyConfig:
    """
    Utility-cost and emissions factors for lighting energy accounting.

    Defaults are simplified Toronto/Ontario assumptions:
      electricity_rate_per_kwh = 0.15 CAD/kWh
      electricity_kg_co2e_per_kwh = 0.059 kg CO2e/kWh

    Override these values when project-specific tariffs or emissions factors
    are available.
    """

    electricity_rate_per_kwh: float = 0.15
    electricity_kg_co2e_per_kwh: float = 0.059
    currency: str = "CAD"
    region: str = "Toronto, Ontario"

    def validate(self) -> None:
        if not isfinite(self.electricity_rate_per_kwh) or self.electricity_rate_per_kwh < 0:
            raise ValueError("electricity_rate_per_kwh must be finite and non-negative")

        if (
            not isfinite(self.electricity_kg_co2e_per_kwh)
            or self.electricity_kg_co2e_per_kwh < 0
        ):
            raise ValueError(
                "electricity_kg_co2e_per_kwh must be finite and non-negative"
            )

        if not self.currency.strip():
            raise ValueError("currency cannot be empty")

        if not self.region.strip():
            raise ValueError("region cannot be empty")


@dataclass(frozen=True)
class LightingSnapshot:
    """
    Direct lighting power from the simulation/FMU/BAS adapter.

    power_kw is required and is the source of truth for energy accounting.

    The other values are optional reporting/diagnostic fields only. They do
    not affect the energy calculation.
    """

    power_kw: float

    lighting_level_fraction: float | None = None
    lighting_level_percent: float | None = None
    on: bool | None = None
    occupancy: bool | None = None

    def normalized_level_fraction(self) -> float | None:
        if self.lighting_level_fraction is not None:
            value = _required_finite(
                self.lighting_level_fraction,
                "lighting_level_fraction",
            )
            return _clamp(value, 0.0, 1.0)

        if self.lighting_level_percent is not None:
            value = _required_finite(
                self.lighting_level_percent,
                "lighting_level_percent",
            )
            return _clamp(value / 100.0, 0.0, 1.0)

        return None


@dataclass(frozen=True)
class LightingEnergyResult:
    currency: str
    region: str

    power_kw: float
    lighting_level_fraction: float | None
    on: bool | None
    occupancy: bool | None

    interval_energy_kwh: float
    total_energy_kwh: float

    interval_cost: float
    total_cost: float

    interval_co2e_kg: float
    total_co2e_kg: float


class LightingEnergyModel:
    """
    Accounting-only lighting energy model.

    This class does not estimate power from:
      - rated lighting power
      - dimming level
      - on/off status
      - occupancy
      - schedules

    The calling simulation/FMU/BAS integration must provide power_kw directly.
    Optional dimming, on/off, and occupancy values are retained only for
    reporting and diagnostics.
    """

    def __init__(self, config: LightingEnergyConfig) -> None:
        config.validate()
        self.config = config
        self.reset()

    def evaluate(
        self,
        snapshot: LightingSnapshot,
        elapsed_seconds: float,
    ) -> LightingEnergyResult:
        if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be finite and non-negative")

        power_kw = _required_nonnegative(snapshot.power_kw, "power_kw")
        lighting_level_fraction = snapshot.normalized_level_fraction()

        dt_h = elapsed_seconds / 3600.0

        interval_energy_kwh = power_kw * dt_h
        interval_cost = (
            interval_energy_kwh * self.config.electricity_rate_per_kwh
        )
        interval_co2e_kg = (
            interval_energy_kwh * self.config.electricity_kg_co2e_per_kwh
        )

        self.total_energy_kwh += interval_energy_kwh
        self.total_cost += interval_cost
        self.total_co2e_kg += interval_co2e_kg

        return LightingEnergyResult(
            currency=self.config.currency,
            region=self.config.region,
            power_kw=power_kw,
            lighting_level_fraction=lighting_level_fraction,
            on=snapshot.on,
            occupancy=snapshot.occupancy,
            interval_energy_kwh=interval_energy_kwh,
            total_energy_kwh=self.total_energy_kwh,
            interval_cost=interval_cost,
            total_cost=self.total_cost,
            interval_co2e_kg=interval_co2e_kg,
            total_co2e_kg=self.total_co2e_kg,
        )

    def reset(self) -> None:
        self.total_energy_kwh = 0.0
        self.total_cost = 0.0
        self.total_co2e_kg = 0.0


def _required_finite(value: float | int, name: str) -> float:
    result = float(value)

    if not isfinite(result):
        raise ValueError(f"{name} must be finite")

    return result


def _required_nonnegative(value: float | int, name: str) -> float:
    result = _required_finite(value, name)

    if result < 0:
        raise ValueError(f"{name} must be non-negative")

    return result


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
