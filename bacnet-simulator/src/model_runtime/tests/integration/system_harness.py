"""RTU -> VAV -> ThermalZone system co-simulation harness.

RTU.fmu, SimpleVAVZone.fmu, and ThermalZone.fmu are three independently
exported FMUs -- there is no single Modelica system model wiring them
together, so proving they work as one packaged VAV system means driving all
three from Python and exchanging their outputs as each other's inputs.

Design: a Gauss-Seidel co-simulation loop, matching this repo's own
production calling convention as closely as three independent FMUs allow.
`shared/runtime/models/manager.py`'s `step()` reloads a fresh FMU instance on every
call and replays the ENTIRE accumulated input history from t=0 up to the new
final_time -- it does this because pyfmi's high-level `simulate()` cannot be
called a second time on an already-simulated FMUModelME2 instance (verified
empirically: a second `simulate(start_time=300, ...)` call on an instance
already simulated 0->300 raises `FMUException: Failed to setup the
experiment` from `setup_experiment`). This harness reuses that exact
technique (`DeviceHistory` + `simulate_device`) for all three models, not as
a workaround but because it is this project's own established, tested
solution to the identical problem.

At each exchange step (interval `dt_s`), every model is simulated from t=0
up to the new time using its currently held input history (each model's
inputs are held constant between exchange points, mirroring how independent
BACnet devices actually exchange periodically-polled point values rather
than being algebraically co-simulated). After all three finish, the latest
outputs are read and become the cross-model inputs that take effect from
this exchange point onward -- a one-step (`dt_s`) lag in the coupling,
standard for Gauss-Seidel co-simulation and physically representative of a
real supervisory-control scan cycle.

RTU.dpSup -> VAV.dpSup: the first version of this harness found that RTU's
supply airflow/duct pressure had no consuming input anywhere in VAV or
ThermalZone -- VAV computed its own airflow from a fixed internal boundary
condition, completely decoupled from what RTU actually delivered. That gap
was closed in SimpleVAVZone.mo itself (a new `dpSup` FMU input, see its own
Documentation annotation) rather than worked around here, so this harness
now wires RTU's own calculated `dpSup` output into VAV as a real physical
input, closing the loop:
    VAV damper -> RTU.uVAVDamMax -> RTU dpSupSet/dpSup -> VAV.dpSup ->
    VAV actual airflow -> ThermalZone -> VAV damper (again)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyfmi import load_fmu

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RTU_FMU = REPO_ROOT / "models" / "rtu" / "RTU.fmu"
VAV_FMU = REPO_ROOT / "models" / "vav" / "SimpleVAVZone.fmu"
ZONE_FMU = REPO_ROOT / "models" / "zone" / "ThermalZone.fmu"

# ─── Exact FMU-level signal mapping (verified against modelDescription.xml,
# not just model.json's display-unit metadata -- see
# tests/integration/SYSTEM_INTEGRATION_REPORT.md, "Signal mapping" for the
# full verification). All temperatures below are Kelvin at the FMU boundary
# on both sides of every connection; damper/valve fractions are unit "1";
# VSup_flow is m3/s; QPeople/QLighting/QEquipment are W. No mismatched-unit
# connection exists anywhere in this wiring. ───────────────────────────────

# RTU.fmu's uMinOutAir replaces its formerly-fixed minOutAirFra parameter
# (see models/rtu/RTU.mo Documentation, 'Minimum outdoor-air input'). This
# harness has no per-scenario concept of a minimum-OA setpoint, so it holds
# uMinOutAir at RTU.mo's own default (0.15, RTU_MIN_OUT_AIR_DEFAULT below)
# for every exchange step -- preserves the exact economizer behavior every
# existing scenario in this file already asserts against.
RTU_MIN_OUT_AIR_DEFAULT = 0.15
RTU_NAMES = ["TSupSet", "TOut", "TRet", "uFan", "uVAVDamMax", "uMinOutAir"]
VAV_NAMES = ["TRoo", "TRooCooSet", "TRooHeaSet", "TSupAHU", "dpSup"]
# ThermalZone.fmu splits internal heat gain into three components
# (QPeople/QLighting/QEquipment, summed internally into QInternalTotal --
# see models/zone/ThermalZone.mo). This harness only cares about the
# system-level TOTAL internal gain for a scenario (its own QInternal_w
# parameter below), so it splits that total evenly across the three FMU
# inputs rather than exposing a three-way split at the harness's own API --
# the split is a harness-internal wiring detail, the total is what every
# scenario in this file actually specifies and asserts against.
ZONE_NAMES = ["TOut", "QPeople", "QLighting", "QEquipment", "TSup", "VSup_flow"]

RTU_TRACK = [
    "TSup", "TMix", "yFan", "yOutDam", "VOutAir_flow", "yCoo", "yHea",
    "dpSup", "dpSupSet", "totalElectricPower", "PCompressor", "PFan",
]
VAV_TRACK = ["yDam", "yDam_actual", "VSup_flow", "TSup", "yVal", "yVal_actual"]
ZONE_TRACK = ["TRoo", "QHVAC", "QEnvelope", "QNet", "QInternalTotal"]


def c2k(celsius: float) -> float:
    return celsius + 273.15


def k2c(kelvin: float) -> float:
    return kelvin - 273.15


class DeviceHistory:
    """Accumulates a (time, values) input history for one FMU. The first
    push must be at t=0.0 -- see module docstring for why full replay from
    t=0 is used instead of a continuation call."""

    def __init__(self, names: list[str]):
        self.names = names
        self.rows: list[list[float]] = []

    def push(self, t: float, values: list[float]) -> None:
        assert len(values) == len(self.names), \
            f"expected {len(self.names)} values, got {len(values)}"
        assert not self.rows or t >= self.rows[-1][0], "history must be time-ordered"
        self.rows.append([float(t)] + [float(v) for v in values])

    def trajectory(self, upto_t: float) -> np.ndarray:
        assert self.rows and self.rows[0][0] == 0.0, "history must start at t=0"
        rows = list(self.rows)
        if rows[-1][0] < upto_t:
            rows = rows + [[upto_t] + rows[-1][1:]]
        return np.array(rows, dtype=float)


def load_device_fmu(fmu_path: Path):
    if not fmu_path.exists():
        pytest.skip(f"{fmu_path} not found -- build all three FMUs before running this suite")
    return load_fmu(str(fmu_path), kind="ME")


def simulate_device(fmu_path: Path, history: DeviceHistory, upto_t: float,
                     params: dict | None = None, ncp: int | None = None):
    """result_handling='memory' for the same reason as every other suite in
    this repo (see models/ahu/tests/test_ahu.py's simulate_ahu docstring):
    pyfmi's default 'file' mode overwrites a shared, model-name-derived
    .mat file across repeated simulations in one process."""
    fmu = load_device_fmu(fmu_path)
    if params:
        for k, v in params.items():
            fmu.set(k, v)
    fmu.set_log_level(2)
    trajectory = history.trajectory(upto_t)
    options = fmu.simulate_options()
    options["ncp"] = ncp or max(2, int(upto_t / 30.0))
    options["result_handling"] = "memory"
    return fmu.simulate(start_time=0.0, final_time=upto_t,
                         input=(history.names, trajectory), options=options)


def get_final(res, name: str) -> float:
    return float(np.asarray(res[name])[-1])


def run_system(
    duration_s: float,
    dt_s: float,
    TOut_c: float,
    TSupSet_c: float,
    uFan: float,
    TRooHeaSet_c: float,
    TRooCooSet_c: float,
    QInternal_w: float,
    TRoo0_c: float = 24.0,
    haveEconomizer: bool = True,
    rtu_extra_params: dict | None = None,
):
    """Runs the coupled RTU->VAV->ThermalZone system for duration_s, with
    Gauss-Seidel exchange every dt_s. TOut/TSupSet/uFan/setpoints/QInternal
    are held constant for the whole run (a steady scenario, per the
    integration task's own scenario descriptions) -- only the closed-loop
    signals (TRet/uVAVDamMax/TSupAHU/TRoo/TSup/VSup_flow) evolve.

    Returns a dict of numpy arrays: "time" plus every tracked RTU/VAV/zone
    output, one value per exchange point (including t=0's initial guesses).
    """
    rtu_hist = DeviceHistory(RTU_NAMES)
    vav_hist = DeviceHistory(VAV_NAMES)
    zone_hist = DeviceHistory(ZONE_NAMES)

    # t=0 cross-model values: reasonable boot-time guesses, matching each
    # model.json's own documented "default" field -- exactly what a real
    # multi-device network falls back on before its first live exchange.
    # dpSup_rtu0 matches both RTU's own dpSupSetMin and VAV's own dpAir
    # default (200 Pa), so the very first exchange window starts from a
    # value both models already treat as "normal."
    TRoo_k = c2k(TRoo0_c)
    yDam_actual = 0.5
    TSup_rtu_k = c2k(13.0)
    TSup_vav_k = c2k(13.0)
    VSup_flow = 0.3
    dpSup_rtu = 200.0

    q_internal_component = QInternal_w / 3.0

    rtu_hist.push(0.0, [c2k(TSupSet_c), c2k(TOut_c), TRoo_k, uFan, yDam_actual, RTU_MIN_OUT_AIR_DEFAULT])
    vav_hist.push(0.0, [TRoo_k, c2k(TRooCooSet_c), c2k(TRooHeaSet_c), TSup_rtu_k, dpSup_rtu])
    zone_hist.push(0.0, [
        c2k(TOut_c), q_internal_component, q_internal_component, q_internal_component,
        TSup_vav_k, VSup_flow,
    ])

    # series only records points a model actually simulated -- the t=0
    # cross-model values above are boot-time guesses, never computed by any
    # FMU, so they are not recorded as if they were a simulated result.
    series: dict[str, list[float]] = {"time": []}
    for k in RTU_TRACK:
        series[f"rtu.{k}"] = []
    for k in VAV_TRACK:
        series[f"vav.{k}"] = []
    for k in ZONE_TRACK:
        series[f"zone.{k}"] = []

    rtu_params = {} if haveEconomizer else {"haveEconomizer": False}
    if rtu_extra_params:
        rtu_params.update(rtu_extra_params)
    rtu_params = rtu_params or None
    # ThermalZone.mo's actual initial state comes from its own TRoo_start
    # parameter (initial equation TRoo = TRoo_start;, default 295.15 K =
    # 22C) -- NOT from the t=0 boundary-condition guesses fed to the other
    # models above. Every fresh ZONE_FMU instance (reloaded each exchange
    # step, per this module's full-replay-from-t=0 design) must have this
    # set explicitly, or every scenario silently starts the zone at 22C
    # regardless of the caller's TRoo0_c.
    zone_params = {"TRoo_start": c2k(TRoo0_c)}

    n_steps = int(round(duration_s / dt_s))
    for i in range(1, n_steps + 1):
        t_next = i * dt_s

        res_rtu = simulate_device(RTU_FMU, rtu_hist, t_next, params=rtu_params)
        res_vav = simulate_device(VAV_FMU, vav_hist, t_next)
        res_zone = simulate_device(ZONE_FMU, zone_hist, t_next, params=zone_params)

        rtu_out = {k: get_final(res_rtu, k) for k in RTU_TRACK}
        vav_out = {k: get_final(res_vav, k) for k in VAV_TRACK}
        zone_out = {k: get_final(res_zone, k) for k in ZONE_TRACK}

        TRoo_k = zone_out["TRoo"]
        yDam_actual = vav_out["yDam_actual"]
        TSup_rtu_k = rtu_out["TSup"]
        TSup_vav_k = vav_out["TSup"]
        VSup_flow = vav_out["VSup_flow"]
        dpSup_rtu = rtu_out["dpSup"]

        rtu_hist.push(t_next, [c2k(TSupSet_c), c2k(TOut_c), TRoo_k, uFan, yDam_actual, RTU_MIN_OUT_AIR_DEFAULT])
        vav_hist.push(t_next, [TRoo_k, c2k(TRooCooSet_c), c2k(TRooHeaSet_c), TSup_rtu_k, dpSup_rtu])
        zone_hist.push(t_next, [
            c2k(TOut_c), q_internal_component, q_internal_component, q_internal_component,
            TSup_vav_k, VSup_flow,
        ])

        series["time"].append(t_next)
        for k, v in rtu_out.items():
            series[f"rtu.{k}"].append(v)
        for k, v in vav_out.items():
            series[f"vav.{k}"].append(v)
        for k, v in zone_out.items():
            series[f"zone.{k}"].append(v)

    return {k: np.array(v, dtype=float) for k, v in series.items()}


def assert_no_nan_inf(series: dict[str, np.ndarray]) -> None:
    for name, arr in series.items():
        assert np.all(np.isfinite(arr)), f"{name} contains NaN/Inf"
