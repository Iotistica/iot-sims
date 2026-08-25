from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

KWH_PER_THERM = 29.300111


@dataclass(frozen=True)
class RTUEnergyConfig:
    electricity_rate_per_kwh: float = 0.0
    gas_rate_per_therm: float = 0.0
    electricity_kg_co2e_per_kwh: float = 0.0
    gas_kg_co2e_per_therm: float = 0.0

    def validate(self) -> None:
        for name, value in {
            "electricity_rate_per_kwh": self.electricity_rate_per_kwh,
            "gas_rate_per_therm": self.gas_rate_per_therm,
            "electricity_kg_co2e_per_kwh": self.electricity_kg_co2e_per_kwh,
            "gas_kg_co2e_per_therm": self.gas_kg_co2e_per_therm,
        }.items():
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class RTUSnapshot:
    """Direct RTU FMU outputs. No fallback estimation is performed."""
    total_electric_power_kw: float
    gas_heating_input_kw: float
    compressor_power_kw: float | None = None
    supply_fan_power_kw: float | None = None
    cooling_load_kw: float | None = None
    heating_load_kw: float | None = None
    compressor_cop: float | None = None
    cooling_plr_percent: float | None = None
    heating_plr_percent: float | None = None


@dataclass(frozen=True)
class RTUEnergyResult:
    electric_power_kw: float
    gas_input_kw: float
    compressor_power_kw: float | None
    supply_fan_power_kw: float | None
    cooling_load_kw: float | None
    heating_load_kw: float | None
    compressor_cop: float | None
    cooling_plr_percent: float | None
    heating_plr_percent: float | None

    interval_electric_energy_kwh: float
    interval_gas_energy_kwh: float
    interval_gas_therms: float

    total_electric_energy_kwh: float
    total_gas_energy_kwh: float
    total_gas_therms: float

    interval_electric_cost: float
    interval_gas_cost: float
    interval_total_cost: float

    interval_electric_co2e_kg: float
    interval_gas_co2e_kg: float
    interval_total_co2e_kg: float

    total_electric_cost: float
    total_gas_cost: float
    total_cost: float

    total_electric_co2e_kg: float
    total_gas_co2e_kg: float
    total_co2e_kg: float


class RTUEnergyModel:
    """
    Accounting-only layer for the gas/DX RTU FMU.

    Expected mappings:
      totalElectricPower -> total_electric_power_kw (W -> kW)
      gasHeatingPower    -> gas_heating_input_kw   (W -> kW)
      PCompressor        -> compressor_power_kw    (optional, W -> kW)
      PFan               -> supply_fan_power_kw    (optional, W -> kW)
      QCoolLoad          -> cooling_load_kw        (optional, W -> kW)
      QHeaLoad           -> heating_load_kw        (optional, W -> kW)
      compressorCOP      -> compressor_cop         (optional)
      coolingPLR         -> cooling_plr_percent    (optional, fraction -> %)
      heatingPLR         -> heating_plr_percent    (optional, fraction -> %)

    This class never estimates missing power from loads, COP, airflow,
    temperatures, fan speed, rated power, or heating efficiency.
    """

    def __init__(self, config: RTUEnergyConfig) -> None:
        config.validate()
        self.config = config
        self.reset()

    def evaluate(self, snapshot: RTUSnapshot, elapsed_seconds: float) -> RTUEnergyResult:
        if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be finite and non-negative")

        electric_power_kw = _required_nonnegative(
            snapshot.total_electric_power_kw, "total_electric_power_kw"
        )
        gas_input_kw = _required_nonnegative(
            snapshot.gas_heating_input_kw, "gas_heating_input_kw"
        )

        compressor_power_kw = _optional_nonnegative(
            snapshot.compressor_power_kw, "compressor_power_kw"
        )
        supply_fan_power_kw = _optional_nonnegative(
            snapshot.supply_fan_power_kw, "supply_fan_power_kw"
        )
        cooling_load_kw = _optional_nonnegative(
            snapshot.cooling_load_kw, "cooling_load_kw"
        )
        heating_load_kw = _optional_nonnegative(
            snapshot.heating_load_kw, "heating_load_kw"
        )
        compressor_cop = _optional_positive(snapshot.compressor_cop, "compressor_cop")
        cooling_plr_percent = _optional_percent(
            snapshot.cooling_plr_percent, "cooling_plr_percent"
        )
        heating_plr_percent = _optional_percent(
            snapshot.heating_plr_percent, "heating_plr_percent"
        )

        dt_h = elapsed_seconds / 3600.0
        interval_electric_energy_kwh = electric_power_kw * dt_h
        interval_gas_energy_kwh = gas_input_kw * dt_h
        interval_gas_therms = interval_gas_energy_kwh / KWH_PER_THERM

        interval_electric_cost = (
            interval_electric_energy_kwh * self.config.electricity_rate_per_kwh
        )
        interval_gas_cost = interval_gas_therms * self.config.gas_rate_per_therm
        interval_total_cost = interval_electric_cost + interval_gas_cost

        interval_electric_co2e_kg = (
            interval_electric_energy_kwh * self.config.electricity_kg_co2e_per_kwh
        )
        interval_gas_co2e_kg = (
            interval_gas_therms * self.config.gas_kg_co2e_per_therm
        )
        interval_total_co2e_kg = interval_electric_co2e_kg + interval_gas_co2e_kg

        self.total_electric_energy_kwh += interval_electric_energy_kwh
        self.total_gas_energy_kwh += interval_gas_energy_kwh
        self.total_gas_therms += interval_gas_therms

        self.total_electric_cost += interval_electric_cost
        self.total_gas_cost += interval_gas_cost
        self.total_cost += interval_total_cost

        self.total_electric_co2e_kg += interval_electric_co2e_kg
        self.total_gas_co2e_kg += interval_gas_co2e_kg
        self.total_co2e_kg += interval_total_co2e_kg

        return RTUEnergyResult(
            electric_power_kw=electric_power_kw,
            gas_input_kw=gas_input_kw,
            compressor_power_kw=compressor_power_kw,
            supply_fan_power_kw=supply_fan_power_kw,
            cooling_load_kw=cooling_load_kw,
            heating_load_kw=heating_load_kw,
            compressor_cop=compressor_cop,
            cooling_plr_percent=cooling_plr_percent,
            heating_plr_percent=heating_plr_percent,
            interval_electric_energy_kwh=interval_electric_energy_kwh,
            interval_gas_energy_kwh=interval_gas_energy_kwh,
            interval_gas_therms=interval_gas_therms,
            total_electric_energy_kwh=self.total_electric_energy_kwh,
            total_gas_energy_kwh=self.total_gas_energy_kwh,
            total_gas_therms=self.total_gas_therms,
            interval_electric_cost=interval_electric_cost,
            interval_gas_cost=interval_gas_cost,
            interval_total_cost=interval_total_cost,
            interval_electric_co2e_kg=interval_electric_co2e_kg,
            interval_gas_co2e_kg=interval_gas_co2e_kg,
            interval_total_co2e_kg=interval_total_co2e_kg,
            total_electric_cost=self.total_electric_cost,
            total_gas_cost=self.total_gas_cost,
            total_cost=self.total_cost,
            total_electric_co2e_kg=self.total_electric_co2e_kg,
            total_gas_co2e_kg=self.total_gas_co2e_kg,
            total_co2e_kg=self.total_co2e_kg,
        )

    def reset(self) -> None:
        self.total_electric_energy_kwh = 0.0
        self.total_gas_energy_kwh = 0.0
        self.total_gas_therms = 0.0
        self.total_electric_cost = 0.0
        self.total_gas_cost = 0.0
        self.total_cost = 0.0
        self.total_electric_co2e_kg = 0.0
        self.total_gas_co2e_kg = 0.0
        self.total_co2e_kg = 0.0


def _required_nonnegative(value: float, name: str) -> float:
    result = float(value)
    if not isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _optional_nonnegative(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    return _required_nonnegative(value, name)


def _optional_positive(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return result


def _optional_percent(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not isfinite(result) or not 0.0 <= result <= 100.0:
        raise ValueError(f"{name} must be between 0 and 100")
    return result
