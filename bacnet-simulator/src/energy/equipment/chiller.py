from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class ChillerEnergyConfig:
    """
    Accounting configuration for SimpleChillerPlant.mo.

    Toronto/Ontario defaults are simplified simulation assumptions.
    Override them with project-specific electricity rates and emissions
    factors when available.
    """

    electricity_rate_per_kwh: float = 0.15
    electricity_kg_co2e_per_kwh: float = 0.059
    currency: str = "CAD"
    region: str = "Toronto, Ontario"

    def validate(self) -> None:
        _validate_nonnegative(
            self.electricity_rate_per_kwh,
            "electricity_rate_per_kwh",
        )
        _validate_nonnegative(
            self.electricity_kg_co2e_per_kwh,
            "electricity_kg_co2e_per_kwh",
        )

        if not self.currency.strip():
            raise ValueError("currency cannot be empty")
        if not self.region.strip():
            raise ValueError("region cannot be empty")


@dataclass(frozen=True)
class ChillerSnapshot:
    """
    Direct values mapped from SimpleChillerPlant.mo.

    Required energy inputs:
      PChi1 [W] -> chiller_1_power_kw [kW]
      PChi2 [W] -> chiller_2_power_kw [kW]

    Optional diagnostics:
      COP1, COP2
      QCoolDelivered [W]
      plantPLR
      TChwSup [K]
      dpChw [Pa]

    No power is reconstructed from COP, thermal load, rated capacity,
    flow, temperatures, or run status.
    """

    chiller_1_power_kw: float
    chiller_2_power_kw: float

    chiller_1_cop: float | None = None
    chiller_2_cop: float | None = None
    cooling_delivered_kw: float | None = None
    plant_plr: float | None = None
    chilled_water_supply_temperature_k: float | None = None
    chilled_water_differential_pressure_pa: float | None = None


@dataclass(frozen=True)
class ChillerEnergyResult:
    currency: str
    region: str

    chiller_1_power_kw: float
    chiller_2_power_kw: float
    total_chiller_power_kw: float

    interval_electric_energy_kwh: float
    total_electric_energy_kwh: float

    interval_electric_cost: float
    total_electric_cost: float

    interval_electric_co2e_kg: float
    total_electric_co2e_kg: float

    chiller_1_cop: float | None
    chiller_2_cop: float | None
    cooling_delivered_kw: float | None
    plant_plr: float | None
    chilled_water_supply_temperature_k: float | None
    chilled_water_differential_pressure_pa: float | None


class ChillerEnergyModel:
    """
    Accounting-only layer matched to the current SimpleChillerPlant.mo.

    The Modelica FMU is the source of truth for chiller electrical power:
      total_chiller_power_kw = PChi1 + PChi2

    COP1, COP2, QCoolDelivered, and plantPLR are retained as diagnostics only.

    Important limitation:
    The current Modelica plant does not expose pump electrical power or
    cooling-tower fan electrical power. Therefore this model reports
    chiller-compressor electricity, not total chilled-water-plant electricity.

    There are deliberately no fallback energy estimates.
    """

    def __init__(self, config: ChillerEnergyConfig) -> None:
        config.validate()
        self.config = config
        self.reset()

    def evaluate(
        self,
        snapshot: ChillerSnapshot,
        elapsed_seconds: float,
    ) -> ChillerEnergyResult:
        if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError(
                "elapsed_seconds must be finite and non-negative"
            )

        chiller_1_power_kw = _required_nonnegative(
            snapshot.chiller_1_power_kw,
            "chiller_1_power_kw",
        )
        chiller_2_power_kw = _required_nonnegative(
            snapshot.chiller_2_power_kw,
            "chiller_2_power_kw",
        )

        total_chiller_power_kw = (
            chiller_1_power_kw + chiller_2_power_kw
        )

        chiller_1_cop = _optional_nonnegative(
            snapshot.chiller_1_cop,
            "chiller_1_cop",
        )
        chiller_2_cop = _optional_nonnegative(
            snapshot.chiller_2_cop,
            "chiller_2_cop",
        )
        cooling_delivered_kw = _optional_nonnegative(
            snapshot.cooling_delivered_kw,
            "cooling_delivered_kw",
        )
        plant_plr = _optional_fraction(
            snapshot.plant_plr,
            "plant_plr",
        )
        chilled_water_supply_temperature_k = _optional_positive(
            snapshot.chilled_water_supply_temperature_k,
            "chilled_water_supply_temperature_k",
        )
        chilled_water_differential_pressure_pa = _optional_nonnegative(
            snapshot.chilled_water_differential_pressure_pa,
            "chilled_water_differential_pressure_pa",
        )

        dt_h = elapsed_seconds / 3600.0

        interval_electric_energy_kwh = (
            total_chiller_power_kw * dt_h
        )

        interval_electric_cost = (
            interval_electric_energy_kwh
            * self.config.electricity_rate_per_kwh
        )

        interval_electric_co2e_kg = (
            interval_electric_energy_kwh
            * self.config.electricity_kg_co2e_per_kwh
        )

        self.total_electric_energy_kwh += interval_electric_energy_kwh
        self.total_electric_cost += interval_electric_cost
        self.total_electric_co2e_kg += interval_electric_co2e_kg

        return ChillerEnergyResult(
            currency=self.config.currency,
            region=self.config.region,
            chiller_1_power_kw=chiller_1_power_kw,
            chiller_2_power_kw=chiller_2_power_kw,
            total_chiller_power_kw=total_chiller_power_kw,
            interval_electric_energy_kwh=interval_electric_energy_kwh,
            total_electric_energy_kwh=self.total_electric_energy_kwh,
            interval_electric_cost=interval_electric_cost,
            total_electric_cost=self.total_electric_cost,
            interval_electric_co2e_kg=interval_electric_co2e_kg,
            total_electric_co2e_kg=self.total_electric_co2e_kg,
            chiller_1_cop=chiller_1_cop,
            chiller_2_cop=chiller_2_cop,
            cooling_delivered_kw=cooling_delivered_kw,
            plant_plr=plant_plr,
            chilled_water_supply_temperature_k=(
                chilled_water_supply_temperature_k
            ),
            chilled_water_differential_pressure_pa=(
                chilled_water_differential_pressure_pa
            ),
        )

    def reset(self) -> None:
        self.total_electric_energy_kwh = 0.0
        self.total_electric_cost = 0.0
        self.total_electric_co2e_kg = 0.0


def snapshot_from_chiller_fmu(
    *,
    PChi1_W: float,
    PChi2_W: float,
    COP1: float | None = None,
    COP2: float | None = None,
    QCoolDelivered_W: float | None = None,
    plantPLR: float | None = None,
    TChwSup_K: float | None = None,
    dpChw_Pa: float | None = None,
) -> ChillerSnapshot:
    """
    Convenience adapter using SimpleChillerPlant.mo signal names.

    Modelica powers/heat flows are W; the accounting model uses kW.
    """

    return ChillerSnapshot(
        chiller_1_power_kw=_required_nonnegative(
            PChi1_W,
            "PChi1_W",
        ) / 1000.0,
        chiller_2_power_kw=_required_nonnegative(
            PChi2_W,
            "PChi2_W",
        ) / 1000.0,
        chiller_1_cop=COP1,
        chiller_2_cop=COP2,
        cooling_delivered_kw=_watts_to_optional_kw(
            QCoolDelivered_W,
            "QCoolDelivered_W",
        ),
        plant_plr=plantPLR,
        chilled_water_supply_temperature_k=TChwSup_K,
        chilled_water_differential_pressure_pa=dpChw_Pa,
    )


def _watts_to_optional_kw(
    value: float | None,
    name: str,
) -> float | None:
    if value is None:
        return None

    return _required_nonnegative(value, name) / 1000.0


def _required_nonnegative(
    value: float,
    name: str,
) -> float:
    result = float(value)

    if not isfinite(result) or result < 0:
        raise ValueError(
            f"{name} must be finite and non-negative"
        )

    return result


def _optional_nonnegative(
    value: float | None,
    name: str,
) -> float | None:
    if value is None:
        return None

    return _required_nonnegative(value, name)


def _optional_positive(
    value: float | None,
    name: str,
) -> float | None:
    if value is None:
        return None

    result = float(value)

    if not isfinite(result) or result <= 0:
        raise ValueError(
            f"{name} must be finite and greater than zero"
        )

    return result


def _optional_fraction(
    value: float | None,
    name: str,
) -> float | None:
    if value is None:
        return None

    result = float(value)

    if not isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(
            f"{name} must be between 0 and 1"
        )

    return result


def _validate_nonnegative(
    value: float,
    name: str,
) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(
            f"{name} must be finite and non-negative"
        )
