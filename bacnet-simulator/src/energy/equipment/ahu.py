from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class AHUEnergyConfig:
    """
    Accounting configuration for AHU.mo.

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
class AHUSnapshot:
    """
    Direct values mapped from AHU.mo.

    Required energy input:
      AHU.PSupFan [W] -> supply_fan_power_kw [kW]

    Optional thermal/plant diagnostics:
      AHU.QCoolLoad     [W]    -> cooling_load_kw [kW]
      AHU.QHeaLoad      [W]    -> heating_load_kw [kW]
      AHU.VChiWat_flow  [m3/s] -> chilled_water_flow_m3_s
      AHU.TChiWatRet    [K]    -> chilled_water_return_temperature_k
      AHU input TChiWatSup [K] -> chilled_water_supply_temperature_k

    Optional AHU operating diagnostics:
      AHU.yFan, yCooVal, yHeaVal, cooCapacityFactor.

    No power is reconstructed from fan speed, airflow, thermal load, COP,
    valve position, or equipment ratings.
    """

    supply_fan_power_kw: float

    cooling_load_kw: float | None = None
    heating_load_kw: float | None = None

    chilled_water_flow_m3_s: float | None = None
    chilled_water_supply_temperature_k: float | None = None
    chilled_water_return_temperature_k: float | None = None

    fan_command_fraction: float | None = None
    cooling_valve_fraction: float | None = None
    heating_valve_fraction: float | None = None
    cooling_capacity_factor: float | None = None


@dataclass(frozen=True)
class AHUEnergyResult:
    currency: str
    region: str

    # Electrical accounting owned by this AHU model.
    electric_power_kw: float
    supply_fan_power_kw: float

    interval_electric_energy_kwh: float
    total_electric_energy_kwh: float

    interval_electric_cost: float
    total_electric_cost: float

    interval_electric_co2e_kg: float
    total_electric_co2e_kg: float

    # Thermal/plant diagnostics only. These are NOT converted to AHU
    # electricity or fuel consumption in this accounting layer.
    cooling_load_kw: float | None
    heating_load_kw: float | None

    chilled_water_flow_m3_s: float | None
    chilled_water_supply_temperature_k: float | None
    chilled_water_return_temperature_k: float | None

    fan_command_fraction: float | None
    cooling_valve_fraction: float | None
    heating_valve_fraction: float | None
    cooling_capacity_factor: float | None


class AHUEnergyModel:
    """
    Energy-accounting layer matched to the current AHU.mo.

    AHU.mo directly exposes supply-fan electrical power as PSupFan.
    Therefore this model uses that value as the sole AHU electrical-power
    source of truth.

    QCoolLoad and QHeaLoad are thermal loads. They are retained for reporting
    but are not converted into electricity or fuel here:

      * Chilled-water production/pumping energy belongs to the plant/chiller
        accounting model.
      * The current AHU.mo does not define the upstream energy source for its
        idealized heating coil, so heating fuel/electricity must be accounted
        for by the applicable upstream heating-system model.

    There are deliberately no fallback power estimates.
    """

    def __init__(self, config: AHUEnergyConfig) -> None:
        config.validate()
        self.config = config
        self.reset()

    def evaluate(
        self,
        snapshot: AHUSnapshot,
        elapsed_seconds: float,
    ) -> AHUEnergyResult:
        if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError(
                "elapsed_seconds must be finite and non-negative"
            )

        supply_fan_power_kw = _required_nonnegative(
            snapshot.supply_fan_power_kw,
            "supply_fan_power_kw",
        )

        # For the current AHU.mo, PSupFan is the AHU's only explicitly
        # modeled electrical-power output.
        electric_power_kw = supply_fan_power_kw

        cooling_load_kw = _optional_nonnegative(
            snapshot.cooling_load_kw,
            "cooling_load_kw",
        )
        heating_load_kw = _optional_nonnegative(
            snapshot.heating_load_kw,
            "heating_load_kw",
        )

        chilled_water_flow_m3_s = _optional_nonnegative(
            snapshot.chilled_water_flow_m3_s,
            "chilled_water_flow_m3_s",
        )
        chilled_water_supply_temperature_k = _optional_positive(
            snapshot.chilled_water_supply_temperature_k,
            "chilled_water_supply_temperature_k",
        )
        chilled_water_return_temperature_k = _optional_positive(
            snapshot.chilled_water_return_temperature_k,
            "chilled_water_return_temperature_k",
        )

        fan_command_fraction = _optional_fraction(
            snapshot.fan_command_fraction,
            "fan_command_fraction",
        )
        cooling_valve_fraction = _optional_fraction(
            snapshot.cooling_valve_fraction,
            "cooling_valve_fraction",
        )
        heating_valve_fraction = _optional_fraction(
            snapshot.heating_valve_fraction,
            "heating_valve_fraction",
        )
        cooling_capacity_factor = _optional_fraction(
            snapshot.cooling_capacity_factor,
            "cooling_capacity_factor",
        )

        dt_h = elapsed_seconds / 3600.0

        interval_electric_energy_kwh = electric_power_kw * dt_h

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

        return AHUEnergyResult(
            currency=self.config.currency,
            region=self.config.region,
            electric_power_kw=electric_power_kw,
            supply_fan_power_kw=supply_fan_power_kw,
            interval_electric_energy_kwh=interval_electric_energy_kwh,
            total_electric_energy_kwh=self.total_electric_energy_kwh,
            interval_electric_cost=interval_electric_cost,
            total_electric_cost=self.total_electric_cost,
            interval_electric_co2e_kg=interval_electric_co2e_kg,
            total_electric_co2e_kg=self.total_electric_co2e_kg,
            cooling_load_kw=cooling_load_kw,
            heating_load_kw=heating_load_kw,
            chilled_water_flow_m3_s=chilled_water_flow_m3_s,
            chilled_water_supply_temperature_k=(
                chilled_water_supply_temperature_k
            ),
            chilled_water_return_temperature_k=(
                chilled_water_return_temperature_k
            ),
            fan_command_fraction=fan_command_fraction,
            cooling_valve_fraction=cooling_valve_fraction,
            heating_valve_fraction=heating_valve_fraction,
            cooling_capacity_factor=cooling_capacity_factor,
        )

    def reset(self) -> None:
        self.total_electric_energy_kwh = 0.0
        self.total_electric_cost = 0.0
        self.total_electric_co2e_kg = 0.0


def snapshot_from_ahu_fmu(
    *,
    PSupFan_W: float,
    QCoolLoad_W: float | None = None,
    QHeaLoad_W: float | None = None,
    VChiWat_flow_m3_s: float | None = None,
    TChiWatSup_K: float | None = None,
    TChiWatRet_K: float | None = None,
    yFan: float | None = None,
    yCooVal: float | None = None,
    yHeaVal: float | None = None,
    cooCapacityFactor: float | None = None,
) -> AHUSnapshot:
    """
    Convenience adapter using the current AHU.mo signal names.

    Modelica powers/heat flows are W; the accounting model uses kW.
    """

    return AHUSnapshot(
        supply_fan_power_kw=_required_nonnegative(
            PSupFan_W,
            "PSupFan_W",
        ) / 1000.0,
        cooling_load_kw=_watts_to_optional_kw(
            QCoolLoad_W,
            "QCoolLoad_W",
        ),
        heating_load_kw=_watts_to_optional_kw(
            QHeaLoad_W,
            "QHeaLoad_W",
        ),
        chilled_water_flow_m3_s=VChiWat_flow_m3_s,
        chilled_water_supply_temperature_k=TChiWatSup_K,
        chilled_water_return_temperature_k=TChiWatRet_K,
        fan_command_fraction=yFan,
        cooling_valve_fraction=yCooVal,
        heating_valve_fraction=yHeaVal,
        cooling_capacity_factor=cooCapacityFactor,
    )


def _watts_to_optional_kw(
    value: float | None,
    name: str,
) -> float | None:
    if value is None:
        return None
    return _required_nonnegative(value, name) / 1000.0


def _required_nonnegative(value: float, name: str) -> float:
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


def _validate_nonnegative(value: float, name: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(
            f"{name} must be finite and non-negative"
        )
