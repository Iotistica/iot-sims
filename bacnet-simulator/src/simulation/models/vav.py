from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..providers.base import ValidationResult


@dataclass
class VAVParameters:
    min_airflow_cfm: float = 150.0
    max_airflow_cfm: float = 500.0

    zone_thermal_time_constant_s: float = 1800.0
    occupied_internal_gain_c_per_hour: float = 0.35
    unoccupied_internal_gain_c_per_hour: float = 0.08

    cooling_proportional_band_c: float = 2.0
    heating_proportional_band_c: float = 2.0

    max_reheat_temp_rise_c: float = 15.0

    damper_time_constant_s: float = 20.0
    reheat_time_constant_s: float = 30.0

    outdoor_co2_ppm: float = 420.0
    occupied_co2_generation_ppm_per_hour: float = 180.0
    unoccupied_co2_decay_per_hour: float = 0.7


class VAVModel:
    """
    Simplified pressure-independent VAV + zone model.

    Inputs:
      cooling_setpoint_c
      heating_setpoint_c
      supply_air_temp_c
      occupied
      zone_temp_c (optional external seed/override)
      zone_co2_ppm (optional external seed/override)

    Outputs:
      zone_temp_c
      damper_command_pct
      zone_airflow_cfm
      reheat_valve_pct
      zone_co2_ppm
      mode
    """

    def __init__(self, params: VAVParameters | None = None) -> None:
        self.params = params or VAVParameters()
        self.reset()

    def reset(self) -> None:
        self._inputs: dict[str, Any] = {
            "cooling_setpoint_c": 23.0,
            "heating_setpoint_c": 20.0,
            "supply_air_temp_c": 13.0,
            "occupied": True,
        }
        self._zone_temp_c = 22.0
        self._zone_co2_ppm = 650.0
        self._damper_pct = 35.0
        self._reheat_pct = 0.0
        self._outputs: dict[str, Any] = {
            "zone_temp_c": self._zone_temp_c,
            "damper_command_pct": self._damper_pct,
            "zone_airflow_cfm": self._airflow_from_damper(self._damper_pct),
            "reheat_valve_pct": self._reheat_pct,
            "zone_co2_ppm": self._zone_co2_ppm,
            "mode": "deadband",
        }

    def set_inputs(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            if key == "zone_temp_c":
                self._zone_temp_c = float(value)
            elif key == "zone_co2_ppm":
                self._zone_co2_ppm = float(value)
            elif key in self._inputs:
                self._inputs[key] = value

    def _airflow_from_damper(self, damper_pct: float) -> float:
        p = self.params
        frac = max(0.0, min(1.0, damper_pct / 100.0))
        return p.min_airflow_cfm + frac * (p.max_airflow_cfm - p.min_airflow_cfm)

    @staticmethod
    def _first_order(current: float, target: float, dt: float, tau: float) -> float:
        alpha = min(1.0, max(0.0, dt) / max(0.001, tau))
        return current + alpha * (target - current)

    def step(self, dt: float) -> None:
        p = self.params
        cool_sp = float(self._inputs["cooling_setpoint_c"])
        heat_sp = float(self._inputs["heating_setpoint_c"])
        sat = float(self._inputs["supply_air_temp_c"])
        occupied = bool(self._inputs["occupied"])

        if cool_sp < heat_sp:
            cool_sp = heat_sp

        if self._zone_temp_c > cool_sp:
            mode = "cooling"
            error = self._zone_temp_c - cool_sp
            demand = min(1.0, error / max(0.001, p.cooling_proportional_band_c))
            target_damper = demand * 100.0
            target_reheat = 0.0
        elif self._zone_temp_c < heat_sp:
            mode = "heating"
            error = heat_sp - self._zone_temp_c
            demand = min(1.0, error / max(0.001, p.heating_proportional_band_c))
            target_damper = 0.0
            target_reheat = demand * 100.0
        else:
            mode = "deadband"
            target_damper = 0.0
            target_reheat = 0.0

        self._damper_pct = self._first_order(
            self._damper_pct, target_damper, dt, p.damper_time_constant_s
        )
        self._reheat_pct = self._first_order(
            self._reheat_pct, target_reheat, dt, p.reheat_time_constant_s
        )

        airflow = self._airflow_from_damper(self._damper_pct)
        airflow_fraction = (
            (airflow - p.min_airflow_cfm)
            / max(1.0, p.max_airflow_cfm - p.min_airflow_cfm)
        )
        airflow_fraction = max(0.0, min(1.0, airflow_fraction))

        discharge_temp = sat + (self._reheat_pct / 100.0) * p.max_reheat_temp_rise_c

        ventilation_strength = 0.15 + 0.85 * airflow_fraction
        effective_tau = p.zone_thermal_time_constant_s / max(0.15, ventilation_strength)

        self._zone_temp_c = self._first_order(
            self._zone_temp_c, discharge_temp, dt, effective_tau
        )

        gain_c_per_hour = (
            p.occupied_internal_gain_c_per_hour
            if occupied
            else p.unoccupied_internal_gain_c_per_hour
        )
        self._zone_temp_c += gain_c_per_hour * dt / 3600.0

        if occupied:
            self._zone_co2_ppm += (
                p.occupied_co2_generation_ppm_per_hour * dt / 3600.0
            )

        ventilation_rate_per_hour = 0.15 + 1.85 * (
            airflow / max(1.0, p.max_airflow_cfm)
        )
        if not occupied:
            ventilation_rate_per_hour += p.unoccupied_co2_decay_per_hour

        decay = min(1.0, ventilation_rate_per_hour * dt / 3600.0)
        self._zone_co2_ppm += decay * (
            p.outdoor_co2_ppm - self._zone_co2_ppm
        )
        self._zone_co2_ppm = max(p.outdoor_co2_ppm, self._zone_co2_ppm)

        self._outputs = {
            "zone_temp_c": self._zone_temp_c,
            "damper_command_pct": self._damper_pct,
            "zone_airflow_cfm": airflow,
            "reheat_valve_pct": self._reheat_pct,
            "zone_co2_ppm": self._zone_co2_ppm,
            "mode": mode,
        }

    def get_outputs(self) -> Mapping[str, Any]:
        return dict(self._outputs)

    def validate(self) -> ValidationResult:
        p = self.params
        errors: list[str] = []
        warnings: list[str] = []

        if p.min_airflow_cfm < 0:
            errors.append("min_airflow_cfm must be >= 0")
        if p.max_airflow_cfm <= p.min_airflow_cfm:
            errors.append("max_airflow_cfm must be > min_airflow_cfm")
        if p.zone_thermal_time_constant_s <= 0:
            errors.append("zone_thermal_time_constant_s must be > 0")
        if p.cooling_proportional_band_c <= 0:
            errors.append("cooling_proportional_band_c must be > 0")
        if p.heating_proportional_band_c <= 0:
            errors.append("heating_proportional_band_c must be > 0")
        if p.max_reheat_temp_rise_c < 0:
            errors.append("max_reheat_temp_rise_c must be >= 0")

        if p.max_airflow_cfm > 5000:
            warnings.append("max_airflow_cfm looks unusually high for a VAV box")

        return ValidationResult(
            valid=not errors,
            errors=errors,
            warnings=warnings,
        )
