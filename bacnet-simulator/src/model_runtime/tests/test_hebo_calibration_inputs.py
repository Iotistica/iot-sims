"""shared/run_hebo_calibration.py is a separate HEBO-based calibration flow
(inspired by Martinez-Viol et al., Building and Environment 226 (2022)
109693), independent of shared/run_sensitivity.py + shared/run_calibration.py's
Morris -> Differential Evolution flow. It has its own, structurally
similar but not shared, CSV/model.json parsing.

These tests cover the pure-Python logic (conversions, defaults, discrete-
input routing, tuner space construction, goal/metric resolution) using
synthetic fixtures and the real RTU assets -- no actual FMU simulation
(and so no dependence on the WSL-bridged FMU execution that makes real
simulate_fmu() calls slow on this dev machine).

Importing this module requires the HEBO/pymoo/pandas/numpy compatibility
shims at the top of run_hebo_calibration.py to succeed first -- see that
module's docstring for exactly what's being patched and why (pymoo's
pymoo.factory import removed in pymoo>=0.6, cma's `from collections
import MutableMapping` removed in Python 3.10+, pymoo 0.5's np.row_stack
removed in numpy 2.0, and HEBO's own DataFrame.append() removed in
pandas 2.0).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from shared import run_hebo_calibration as hc

REPO_ROOT = Path(__file__).resolve().parent.parent
RTU_MODEL_JSON = REPO_ROOT / "models" / "rtu" / "model.json"
RTU_HISTORY_CSV = REPO_ROOT / "models" / "rtu" / "calibration" / "rtu1_history.csv"
RTU_FMU = REPO_ROOT / "models" / "rtu" / "RTU.fmu"


def test_prepare_inputs_uses_csv_value_and_applies_conversion():
    meta = {"inputs": [{"name": "temp_c", "fmu_variable": "TOut", "conversion": "c_to_k"}]}
    rows = [{"temp_c": "10"}, {"temp_c": "20"}]
    times = np.array([0.0, 60.0])

    signal, discrete_start_values = hc.prepare_inputs(RTU_FMU, meta, rows, times)

    assert signal["TOut"].tolist() == pytest.approx([283.15, 293.15])
    assert discrete_start_values == {}


def test_prepare_inputs_falls_back_to_default_when_missing():
    meta = {"inputs": [{"name": "override_pct", "fmu_variable": "uOutDamOvr", "default": 5.0}]}
    rows = [{"other_column": "1"}, {"other_column": "2"}]
    times = np.array([0.0, 60.0])

    signal, discrete_start_values = hc.prepare_inputs(RTU_FMU, meta, rows, times)

    assert signal["uOutDamOvr"].tolist() == [5.0, 5.0]


def test_prepare_inputs_raises_when_missing_and_no_default():
    meta = {"inputs": [{"name": "required_temp_c", "fmu_variable": "TRet"}]}
    rows = [{"required_temp_c": ""}]
    times = np.array([0.0, 60.0])

    with pytest.raises(hc.CalibrationError, match="Missing 'required_temp_c'"):
        hc.prepare_inputs(RTU_FMU, meta, rows, times)


def test_prepare_inputs_routes_discrete_boolean_input_to_start_values():
    """uOutDamOvrEna is a real FMI discrete Boolean input on RTU.fmu -- it
    must come back as a constant start value, not a signal-array column
    (see prepare_inputs's docstring for why: FMPy's ModelExchange/CVode
    solver applies signal-array inputs from inside its event-indicator/
    right-hand-side callbacks on every evaluation, valid only for
    genuinely continuous signals)."""
    meta = {
        "inputs": [
            {"name": "override_enable", "fmu_variable": "uOutDamOvrEna", "default": False},
        ]
    }
    rows = [{}, {}]
    times = np.array([0.0, 60.0])

    signal, discrete_start_values = hc.prepare_inputs(RTU_FMU, meta, rows, times)

    assert "uOutDamOvrEna" not in signal.dtype.names
    assert discrete_start_values == {"uOutDamOvrEna": 0.0}


def test_prepare_inputs_raises_when_discrete_input_actually_varies():
    meta = {"inputs": [{"name": "override_enable", "fmu_variable": "uOutDamOvrEna"}]}
    rows = [{"override_enable": "0"}, {"override_enable": "1"}]
    times = np.array([0.0, 60.0])

    with pytest.raises(hc.CalibrationError, match="uOutDamOvrEna.*discrete"):
        hc.prepare_inputs(RTU_FMU, meta, rows, times)


def test_rtu_history_csv_row2_converts_to_expected_fmu_units_and_splits_discrete():
    """Real RTU assets end-to-end: engineering-unit CSV values convert
    correctly, and the CSV's lack of the override columns resolves via
    model.json defaults -- with uOutDamOvrEna routed to start_values."""
    meta = hc.load_json(RTU_MODEL_JSON)
    headers, rows = hc.load_csv(RTU_HISTORY_CSV)
    times = hc.build_times(rows, "timestamp")

    signal, discrete_start_values = hc.prepare_inputs(RTU_FMU, meta, rows, times)

    expected_signal = {
        "TSupSet": 289.15,
        "TOut": 296.614,
        "TRet": 294.736,
        "uFan": 0.40,
        "uVAVDamMax": 0.525,
        "uMinOutAir": 0.15,
        "uOutDamOvr": 0.0,
    }
    for fmu_name, value in expected_signal.items():
        assert signal[fmu_name][0] == pytest.approx(value), fmu_name
    assert "uOutDamOvrEna" not in signal.dtype.names
    assert discrete_start_values == {"uOutDamOvrEna": 0.0}


def test_resolve_goal_reads_rtu_supply_air_temp_cv_rmse_goal():
    meta = hc.load_json(RTU_MODEL_JSON)

    goal_csv, goal_fmu, goal_conv, objective_metric = hc.resolve_goal(meta)

    assert goal_csv == "supply_air_temp_c"
    assert goal_fmu == "TSup"
    assert objective_metric == "cv_rmse"


def test_build_space_reads_all_eight_rtu_tuners_with_min_max_bounds():
    meta = hc.load_json(RTU_MODEL_JSON)

    space, tuners = hc.build_space(meta)

    names = [t["parameter"] for t in tuners]
    assert len(names) == 8
    assert set(names) == {
        "QCoo_flow_nominal", "COP_nominal", "kCOPPerKelvin", "kCapOutPerKelvin",
        "kCapMixPerKelvin", "partLoadDegradation", "kSAT", "TiSAT",
    }
    # DesignSpace's own bounds must match model.json's tuner min/max exactly.
    for t in tuners:
        bound = space.paras[t["parameter"]]
        assert float(bound.lb) == pytest.approx(float(t["min"]))
        assert float(bound.ub) == pytest.approx(float(t["max"]))


def test_default_parameters_uses_model_json_initial_values():
    meta = hc.load_json(RTU_MODEL_JSON)
    _, tuners = hc.build_space(meta)

    defaults = hc.default_parameters(tuners)

    assert defaults["QCoo_flow_nominal"] == pytest.approx(-36846.82)
    assert defaults["COP_nominal"] == pytest.approx(3.71)
    assert defaults["kSAT"] == pytest.approx(0.2)


def test_resolve_fmi_execution_prefers_model_exchange_cvode_for_rtu():
    """RTU.fmu exports both CoSimulation and ModelExchange; FMPy prefers
    CoSimulation's own black-box fixed-step solver when fmi_type is left
    unset, which is far less numerically controllable -- see
    run_sensitivity.run_one for the concrete failure this generically
    avoids. resolve_fmi_execution must prefer ModelExchange+CVode
    whenever the FMU supports it and the caller didn't explicitly choose
    otherwise."""
    fmi_type, solver = hc.resolve_fmi_execution(RTU_FMU, None, None)

    assert fmi_type == "ModelExchange"
    assert solver == "CVode"


def test_resolve_fmi_execution_respects_explicit_override():
    fmi_type, solver = hc.resolve_fmi_execution(RTU_FMU, "CoSimulation", None)

    assert fmi_type == "CoSimulation"
    assert solver is None


def test_metric_cv_rmse_matches_hand_computation():
    measured = np.array([10.0, 20.0, 30.0])
    predicted = np.array([11.0, 19.0, 33.0])

    value = hc.metric("cv_rmse", measured, predicted)

    rmse = float(np.sqrt(np.mean((predicted - measured) ** 2)))
    expected = 100.0 * rmse / abs(float(np.mean(measured)))
    assert value == pytest.approx(expected)


def test_build_space_raises_when_tuners_missing():
    with pytest.raises(hc.CalibrationError, match="calibration.tuners is empty"):
        hc.build_space({"calibration": {}})


def test_run_candidate_raises_when_simulation_is_truncated_by_timeout(monkeypatch):
    """FMPy's timeout doesn't raise on expiry, it just returns whatever was
    recorded so far -- a truncated result would otherwise flow straight
    through np.interp, which flat-extrapolates past the truncated range
    instead of erroring, producing a plausible-looking but wrong score
    that HEBO would happily optimize against. run_candidate() must detect
    this explicitly so it flows through the existing failure-penalty path
    like any other bad evaluation."""

    def fake_simulate_fmu(**kwargs):
        return {"time": np.array([0.0, 60.0]), "TSup": np.array([300.0, 305.0])}

    monkeypatch.setattr(hc, "simulate_fmu", fake_simulate_fmu)

    with pytest.raises(hc.CalibrationError, match="stop_time"):
        hc.run_candidate(
            fmu=Path("fake.fmu"),
            signal=np.zeros(4, dtype=[("time", np.float64)]),
            times=np.array([0.0, 60.0, 120.0, 180.0]),
            params={},
            discrete_start_values={},
            goal_fmu="TSup",
            goal_conv=None,
            measured=np.array([300.0, 301.0, 302.0, 303.0]),
            warmup=0.0,
            interval=60.0,
            fmi_type="ModelExchange",
            solver="CVode",
        )
