from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..providers.base import ValidationResult

CP_WATER_KJ_PER_KG_K = 4.186


@dataclass
class ChillerParameters:
    capacity_kw: float = 500.0
    nominal_cop: float = 5.5
    minimum_plr: float = 0.15
    maximum_plr: float = 1.0
    condenser_temp_reference_c: float = 29.0
    condenser_cop_penalty_per_k: float = 0.018
    low_load_cop_penalty: float = 0.18
    leaving_temp_time_constant_s: float = 45.0
    minimum_flow_kg_s: float = 2.0


class ChillerModel:
    """Simplified dynamic electric chiller model for BAS simulation.

    Inputs:
      enable
      chw_return_temp_c
      chw_setpoint_c
      chw_flow_kg_s
      condenser_entering_temp_c

    Outputs:
      run
      chw_leaving_temp_c
      cooling_kw
      power_kw
      cop
      plr
    """

    def __init__(self, params: ChillerParameters | None = None) -> None:
        self.params = params or ChillerParameters()
        self.reset()

    def reset(self) -> None:
        self._inputs: dict[str, Any] = {
            "enable": False,
            "chw_return_temp_c": 12.0,
            "chw_setpoint_c": 6.5,
            "chw_flow_kg_s": 20.0,
            "condenser_entering_temp_c": 29.0,
        }
        self._leaving_temp_c = float(self._inputs["chw_return_temp_c"])
        self._outputs: dict[str, Any] = {
            "run": False,
            "chw_leaving_temp_c": self._leaving_temp_c,
            "cooling_kw": 0.0,
            "power_kw": 0.0,
            "cop": 0.0,
            "plr": 0.0,
        }

    def set_inputs(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            if key in self._inputs:
                self._inputs[key] = value

    def _effective_cop(self, plr: float, condenser_temp_c: float) -> float:
        p = self.params
        temp_delta = condenser_temp_c - p.condenser_temp_reference_c
        temp_factor = max(0.55, 1.0 - p.condenser_cop_penalty_per_k * temp_delta)
        load_distance = max(0.0, 0.55 - plr) / 0.55
        load_factor = max(0.65, 1.0 - p.low_load_cop_penalty * load_distance)
        return max(1.0, p.nominal_cop * temp_factor * load_factor)

    def step(self, dt: float) -> None:
        p = self.params
        enable = bool(self._inputs["enable"])
        t_return = float(self._inputs["chw_return_temp_c"])
        setpoint = float(self._inputs["chw_setpoint_c"])
        flow = max(0.0, float(self._inputs["chw_flow_kg_s"]))
        t_cond = float(self._inputs["condenser_entering_temp_c"])

        can_run = enable and flow >= p.minimum_flow_kg_s and t_return > setpoint

        if not can_run:
            target_leaving = t_return
            cooling_kw = power_kw = cop = plr = 0.0
        else:
            load_to_setpoint_kw = flow * CP_WATER_KJ_PER_KG_K * max(0.0, t_return - setpoint)
            cooling_kw = min(p.capacity_kw * p.maximum_plr, load_to_setpoint_kw)
            raw_plr = cooling_kw / p.capacity_kw if p.capacity_kw > 0 else 0.0
            plr = max(p.minimum_plr, min(p.maximum_plr, raw_plr))
            cop = self._effective_cop(plr, t_cond)
            power_kw = cooling_kw / cop if cop > 0 else 0.0
            target_leaving = t_return - cooling_kw / (flow * CP_WATER_KJ_PER_KG_K)
            target_leaving = max(setpoint, target_leaving)

        tau = max(0.001, p.leaving_temp_time_constant_s)
        alpha = min(1.0, dt / tau)
        self._leaving_temp_c += alpha * (target_leaving - self._leaving_temp_c)

        self._outputs = {
            "run": can_run,
            "chw_leaving_temp_c": self._leaving_temp_c,
            "cooling_kw": cooling_kw,
            "power_kw": power_kw,
            "cop": cop,
            "plr": plr,
        }

    def get_outputs(self) -> Mapping[str, Any]:
        return dict(self._outputs)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        p = self.params
        if p.capacity_kw <= 0:
            errors.append("capacity_kw must be > 0")
        if p.nominal_cop <= 0:
            errors.append("nominal_cop must be > 0")
        if not (0 < p.minimum_plr <= p.maximum_plr <= 1.0):
            errors.append("Require 0 < minimum_plr <= maximum_plr <= 1")
        if p.leaving_temp_time_constant_s <= 0:
            errors.append("leaving_temp_time_constant_s must be > 0")
        if p.nominal_cop > 10:
            warnings.append("nominal_cop looks unusually high")
        return ValidationResult(valid=not errors, errors=errors, warnings=warnings)
