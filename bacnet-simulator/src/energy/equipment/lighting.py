from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


class LightingEnergySource(StrEnum):
    MEASURED = "measured"
    DIMMING_LEVEL = "dimming-level"
    ON_OFF_STATUS = "on-off-status"
    OCCUPANCY = "occupancy"
    UNAVAILABLE = "unavailable"


class LightingEnergyConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class LightingEnergyConfig:
    """
    Static lighting characteristics.

    rated_power_kw:
        Total full-output electrical power for the fixture, zone,
        or lighting group represented by this model.

    standby_power_kw:
        Power used when lighting is off but controls/drivers remain active.

    minimum_dimmed_power_fraction:
        Minimum fraction of rated power while dimmed above zero.

    dimming_exponent:
        Controls the dimming curve. Use 1.0 for linear behavior.
        Values above 1.0 reduce power faster at low dim levels.

    occupied_default_level_percent:
        Fallback dim level when only occupancy is available.
    """

    rated_power_kw: float
    standby_power_kw: float = 0.0
    minimum_dimmed_power_fraction: float = 0.0
    dimming_exponent: float = 1.0
    occupied_default_level_percent: float = 100.0

    def validate(self) -> None:
        if self.rated_power_kw <= 0:
            raise ValueError(
                "rated_power_kw must be greater than zero"
            )

        if self.standby_power_kw < 0:
            raise ValueError(
                "standby_power_kw cannot be negative"
            )

        if not 0 <= self.minimum_dimmed_power_fraction <= 1:
            raise ValueError(
                "minimum_dimmed_power_fraction must be between 0 and 1"
            )

        if self.dimming_exponent <= 0:
            raise ValueError(
                "dimming_exponent must be greater than zero"
            )

        if not 0 <= self.occupied_default_level_percent <= 100:
            raise ValueError(
                "occupied_default_level_percent must be between 0 and 100"
            )


@dataclass(frozen=True)
class LightingSnapshot:
    """
    Live values from Brick-tagged simulation points.
    """

    measured_power_kw: float | None = None
    measured_energy_kwh: float | None = None

    on: bool | None = None
    occupancy: bool | None = None

    lighting_level_fraction: float | None = None
    lighting_level_percent: float | None = None

    def normalized_level_fraction(self) -> float | None:
        if self.lighting_level_fraction is not None:
            return _clamp(
                float(self.lighting_level_fraction),
                0.0,
                1.0,
            )

        if self.lighting_level_percent is not None:
            return _clamp(
                float(self.lighting_level_percent) / 100.0,
                0.0,
                1.0,
            )

        return None


@dataclass(frozen=True)
class LightingEnergyResult:
    power_kw: float | None
    interval_energy_kwh: float
    total_energy_kwh: float

    lighting_level_fraction: float | None
    on: bool | None
    occupancy: bool | None

    source: LightingEnergySource
    confidence: LightingEnergyConfidence
    method: str

    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class LightingEnergyModel:
    def __init__(
        self,
        config: LightingEnergyConfig,
    ) -> None:
        config.validate()
        self.config = config
        self.total_energy_kwh = 0.0

    def evaluate(
        self,
        snapshot: LightingSnapshot,
        elapsed_seconds: float,
    ) -> LightingEnergyResult:
        if elapsed_seconds < 0:
            raise ValueError(
                "elapsed_seconds cannot be negative"
            )

        calculation = (
            self._from_measured_power(snapshot)
            or self._from_dimming_level(snapshot)
            or self._from_on_off(snapshot)
            or self._from_occupancy(snapshot)
            or self._unavailable(snapshot)
        )

        power_kw = calculation["power_kw"]

        interval_energy_kwh = (
            max(0.0, power_kw)
            * elapsed_seconds
            / 3600.0
            if power_kw is not None
            else 0.0
        )

        self.total_energy_kwh += interval_energy_kwh

        return LightingEnergyResult(
            power_kw=power_kw,
            interval_energy_kwh=interval_energy_kwh,
            total_energy_kwh=self.total_energy_kwh,
            lighting_level_fraction=(
                calculation["lighting_level_fraction"]
            ),
            on=snapshot.on,
            occupancy=snapshot.occupancy,
            source=calculation["source"],
            confidence=calculation["confidence"],
            method=calculation["method"],
            inputs=calculation["inputs"],
            warnings=calculation["warnings"],
        )

    def reset(self) -> None:
        self.total_energy_kwh = 0.0

    def _from_measured_power(
        self,
        snapshot: LightingSnapshot,
    ) -> dict[str, Any] | None:
        power_kw = _positive_or_zero(
            snapshot.measured_power_kw
        )

        if power_kw is None:
            return None

        return self._calculation(
            power_kw=power_kw,
            lighting_level_fraction=(
                snapshot.normalized_level_fraction()
            ),
            source=LightingEnergySource.MEASURED,
            confidence=LightingEnergyConfidence.HIGH,
            method="measured-electric-power",
            inputs={
                "measured_power_kw": power_kw,
            },
        )

    def _from_dimming_level(
        self,
        snapshot: LightingSnapshot,
    ) -> dict[str, Any] | None:
        level = snapshot.normalized_level_fraction()

        if level is None:
            return None

        if snapshot.on is False or level <= 0:
            power_kw = self.config.standby_power_kw
            effective_level = 0.0
        else:
            effective_level = level

            dimmed_fraction = (
                effective_level
                ** self.config.dimming_exponent
            )

            if effective_level > 0:
                dimmed_fraction = max(
                    self.config.minimum_dimmed_power_fraction,
                    dimmed_fraction,
                )

            power_kw = (
                self.config.rated_power_kw
                * dimmed_fraction
                + self.config.standby_power_kw
            )

        return self._calculation(
            power_kw=power_kw,
            lighting_level_fraction=effective_level,
            source=LightingEnergySource.DIMMING_LEVEL,
            confidence=LightingEnergyConfidence.HIGH,
            method="rated-power-times-dimming-curve",
            inputs={
                "rated_power_kw": (
                    self.config.rated_power_kw
                ),
                "lighting_level_fraction": effective_level,
                "dimming_exponent": (
                    self.config.dimming_exponent
                ),
                "minimum_dimmed_power_fraction": (
                    self.config
                    .minimum_dimmed_power_fraction
                ),
                "standby_power_kw": (
                    self.config.standby_power_kw
                ),
            },
        )

    def _from_on_off(
        self,
        snapshot: LightingSnapshot,
    ) -> dict[str, Any] | None:
        if snapshot.on is None:
            return None

        power_kw = (
            self.config.rated_power_kw
            + self.config.standby_power_kw
            if snapshot.on
            else self.config.standby_power_kw
        )

        return self._calculation(
            power_kw=power_kw,
            lighting_level_fraction=(
                1.0 if snapshot.on else 0.0
            ),
            source=LightingEnergySource.ON_OFF_STATUS,
            confidence=LightingEnergyConfidence.MEDIUM,
            method="rated-power-from-on-off-status",
            inputs={
                "on": snapshot.on,
                "rated_power_kw": (
                    self.config.rated_power_kw
                ),
                "standby_power_kw": (
                    self.config.standby_power_kw
                ),
            },
        )

    def _from_occupancy(
        self,
        snapshot: LightingSnapshot,
    ) -> dict[str, Any] | None:
        if snapshot.occupancy is None:
            return None

        if not snapshot.occupancy:
            power_kw = self.config.standby_power_kw
            level = 0.0
        else:
            level = (
                self.config
                .occupied_default_level_percent
                / 100.0
            )

            dimmed_fraction = (
                level
                ** self.config.dimming_exponent
            )

            if level > 0:
                dimmed_fraction = max(
                    self.config.minimum_dimmed_power_fraction,
                    dimmed_fraction,
                )

            power_kw = (
                self.config.rated_power_kw
                * dimmed_fraction
                + self.config.standby_power_kw
            )

        return self._calculation(
            power_kw=power_kw,
            lighting_level_fraction=level,
            source=LightingEnergySource.OCCUPANCY,
            confidence=LightingEnergyConfidence.LOW,
            method="occupancy-default-lighting-level",
            inputs={
                "occupancy": snapshot.occupancy,
                "occupied_default_level_percent": (
                    self.config
                    .occupied_default_level_percent
                ),
                "rated_power_kw": (
                    self.config.rated_power_kw
                ),
            },
            warnings=[
                "Lighting power was inferred from occupancy."
            ],
        )

    def _unavailable(
        self,
        snapshot: LightingSnapshot,
    ) -> dict[str, Any]:
        return self._calculation(
            power_kw=None,
            lighting_level_fraction=(
                snapshot.normalized_level_fraction()
            ),
            source=LightingEnergySource.UNAVAILABLE,
            confidence=LightingEnergyConfidence.NONE,
            method="insufficient-data",
            inputs={},
            warnings=[
                "No measured power, dimming level, on/off status, "
                "or occupancy value was available."
            ],
        )

    @staticmethod
    def _calculation(
        *,
        power_kw: float | None,
        lighting_level_fraction: float | None,
        source: LightingEnergySource,
        confidence: LightingEnergyConfidence,
        method: str,
        inputs: dict[str, Any],
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "power_kw": power_kw,
            "lighting_level_fraction": (
                lighting_level_fraction
            ),
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


def _clamp(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
    return max(minimum, min(maximum, value))
