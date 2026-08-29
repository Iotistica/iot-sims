"""shared/run_sensitivity.py and shared/run_calibration.py each parse a
model.json's "inputs" list against a historical CSV via their own
(structurally duplicated, not shared) prepare_inputs()/numeric_column().
Both used to require every input to have a real, non-empty CSV value on
every row -- so adding a new optional FMU input (e.g. the RTU's
outdoor_air_damper_override_enable/_pct actuator-override pair) broke
every CSV captured before that input existed.

numeric_column() now accepts a `default` fallback (used whenever a row's
value is missing or blank, including when the column is absent from the
CSV header entirely -- DictReader.get() returns None for every row in
that case too), and prepare_inputs() sources that fallback from each
input's own model.json "default". A missing value with no declared
default still raises, unchanged. This is verified identically against
both runners since they don't share code.

prepare_inputs() also returns (signal, discrete_start_values): any input
the FMU declares FMI variability "discrete"/"tunable" (e.g. RTU's Boolean
uOutDamOvrEna) is pulled out of the time-varying signal array and into a
constant start_values dict instead, because FMPy's ModelExchange/CVode
solver applies signal-array inputs from inside its event-indicator/
right-hand-side callbacks on every evaluation -- valid only for genuinely
continuous signals. Replaying a discrete input that way corrupted the
FMU's internal event-handling state machine (observed as fmi2SetBoolean /
fmi2SetContinuousStates failing with status 3, immediately at t=0).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from shared import run_calibration, run_sensitivity

REPO_ROOT = Path(__file__).resolve().parent.parent
RTU_MODEL_JSON = REPO_ROOT / "models" / "rtu" / "model.json"
RTU_HISTORY_CSV = REPO_ROOT / "models" / "rtu" / "calibration" / "rtu1_history.csv"
RTU_FMU = REPO_ROOT / "models" / "rtu" / "RTU.fmu"

RUNNERS = [
    pytest.param(run_sensitivity, run_sensitivity.SensitivityError, id="sensitivity"),
    pytest.param(run_calibration, run_calibration.CalibrationError, id="calibration"),
]


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_uses_csv_value_when_present(runner, error_cls):
    metadata = {"inputs": [{"name": "temp_c", "fmu_variable": "TOut", "conversion": "c_to_k", "default": 30.0}]}
    rows = [{"temp_c": "10"}, {"temp_c": "20"}]

    signal, discrete_start_values = runner.prepare_inputs(metadata, rows, np.array([0.0, 60.0]))

    assert signal["TOut"].tolist() == pytest.approx([283.15, 293.15])
    assert discrete_start_values == {}


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_falls_back_to_default_when_column_absent(runner, error_cls):
    """Column not present in the CSV header at all -- DictReader.get()
    returns None for every row, same as this session's real failure."""
    metadata = {"inputs": [{"name": "override_pct", "fmu_variable": "uOutDamOvr", "default": 0.0}]}
    rows = [{"other_column": "1"}, {"other_column": "2"}]

    signal, discrete_start_values = runner.prepare_inputs(metadata, rows, np.array([0.0, 60.0]))

    assert signal["uOutDamOvr"].tolist() == [0.0, 0.0]
    assert discrete_start_values == {}


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_falls_back_only_on_blank_rows(runner, error_cls):
    """A real value on one row must be used as-is; only the blank row
    falls back to the model default -- proves the CSV value always wins
    over the default when present, per the requested precedence."""
    metadata = {"inputs": [{"name": "override_pct", "fmu_variable": "uOutDamOvr", "default": 5.0}]}
    rows = [{"override_pct": "40"}, {"override_pct": ""}, {"override_pct": None}]

    signal, discrete_start_values = runner.prepare_inputs(metadata, rows, np.array([0.0, 60.0, 120.0]))

    assert signal["uOutDamOvr"].tolist() == [40.0, 5.0, 5.0]
    assert discrete_start_values == {}


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_coerces_boolean_default(runner, error_cls):
    metadata = {
        "inputs": [
            {"name": "enable_true", "fmu_variable": "vTrue", "default": True},
            {"name": "enable_false", "fmu_variable": "vFalse", "default": False},
        ]
    }
    rows = [{}, {}]

    signal, discrete_start_values = runner.prepare_inputs(metadata, rows, np.array([0.0, 60.0]))

    assert signal["vTrue"].tolist() == [1.0, 1.0]
    assert signal["vFalse"].tolist() == [0.0, 0.0]
    assert discrete_start_values == {}


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_raises_when_missing_and_no_model_default(runner, error_cls):
    """Fails only when a value is missing AND the model.json input has no
    declared default -- the fix must not silently invent values for
    inputs that were never given a default."""
    metadata = {"inputs": [{"name": "required_temp_c", "fmu_variable": "TRet"}]}
    rows = [{"required_temp_c": ""}]

    with pytest.raises(error_cls, match="Missing value for 'required_temp_c'"):
        runner.prepare_inputs(metadata, rows, np.array([0.0, 60.0]))


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_routes_discrete_input_to_start_values(runner, error_cls):
    """A constant discrete/tunable-variability input (per input_variables,
    as fmu_input_variables() would report for a real FMI Boolean/Integer
    input) must NOT end up in the time-varying signal array -- only
    continuous inputs do."""
    metadata = {
        "inputs": [
            {"name": "override_enable", "fmu_variable": "uOutDamOvrEna", "default": False},
            {"name": "temp_c", "fmu_variable": "TOut", "conversion": "c_to_k"},
        ]
    }
    rows = [{"temp_c": "10"}, {"temp_c": "20"}]
    input_variables = {
        "uOutDamOvrEna": {"dtype": np.bool_, "variability": "discrete"},
        "TOut": {"dtype": np.float64, "variability": "continuous"},
    }

    signal, discrete_start_values = runner.prepare_inputs(
        metadata, rows, np.array([0.0, 60.0]), input_variables
    )

    assert "uOutDamOvrEna" not in signal.dtype.names
    assert discrete_start_values == {"uOutDamOvrEna": 0.0}
    assert signal["TOut"].tolist() == pytest.approx([283.15, 293.15])


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_prepare_inputs_raises_when_discrete_input_actually_varies(runner, error_cls):
    """A discrete/tunable input can only be replayed as a constant start
    value -- if the historical data genuinely varies, that must raise
    rather than silently collapsing to one value and losing the rest."""
    metadata = {
        "inputs": [
            {"name": "override_enable", "fmu_variable": "uOutDamOvrEna"},
        ]
    }
    rows = [{"override_enable": "0"}, {"override_enable": "1"}]
    input_variables = {"uOutDamOvrEna": {"dtype": np.bool_, "variability": "discrete"}}

    with pytest.raises(error_cls, match="uOutDamOvrEna.*discrete"):
        runner.prepare_inputs(metadata, rows, np.array([0.0, 60.0]), input_variables)


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_rtu_history_csv_row2_converts_to_expected_fmu_units(runner, error_cls):
    """The RTU history CSV stores engineering units (degC, percent);
    model.json declares c_to_k/pct_to_fraction conversions per input. This
    pins prepare_inputs()'s output at the first data row to the exact
    FMU-native values the RTU FMU must actually receive -- for both real
    CSV columns (temps, fan, dampers) and the two override inputs that
    fall back to their model.json defaults (false/0.0) on this CSV, which
    must be converted the same as any other value, not left in whatever
    unit the raw default literal happens to be written in."""
    metadata = runner.load_json(RTU_MODEL_JSON)
    headers, rows = runner.load_csv(RTU_HISTORY_CSV)
    times = runner.build_time_vector(rows, "timestamp")
    input_variables = runner.fmu_input_variables(RTU_FMU)

    signal, discrete_start_values = runner.prepare_inputs(metadata, rows, times, input_variables)

    # uOutDamOvrEna is a real FMI discrete (Boolean) input -- it must come
    # back as a constant start value, not a signal-array column.
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


@pytest.mark.parametrize("runner, error_cls", RUNNERS)
def test_rtu_history_csv_missing_override_columns_resolves_via_model_defaults(runner, error_cls):
    """Regression test for the real CI failure: rtu1_history.csv predates
    the outdoor_air_damper_override_enable/_pct optional FMU inputs and
    must keep working unmodified, resolving both to their model.json
    defaults (false / 0.0) rather than failing the run."""
    metadata = runner.load_json(RTU_MODEL_JSON)
    headers, rows = runner.load_csv(RTU_HISTORY_CSV)

    # Guards the premise of this test: if someone adds these columns back
    # to the CSV, this stops proving backward compatibility.
    assert "outdoor_air_damper_override_enable" not in headers
    assert "outdoor_air_damper_override_pct" not in headers

    times = runner.build_time_vector(rows, "timestamp")
    input_variables = runner.fmu_input_variables(RTU_FMU)
    signal, discrete_start_values = runner.prepare_inputs(metadata, rows, times, input_variables)

    assert discrete_start_values == {"uOutDamOvrEna": 0.0}
    assert np.all(signal["uOutDamOvr"] == 0.0)
    # Sanity: a real, present column still parses through unaffected
    # (c_to_k applied, not silently defaulted).
    expected_tsupset_k = float(rows[0]["supply_air_temp_setpoint_c"]) + 273.15
    assert signal["TSupSet"][0] == pytest.approx(expected_tsupset_k)


def _fake_simulate_fmu(**kwargs):
    """Stands in for FMPy's simulate_fmu: reproduces the exact noisy
    'Warning: missing input for variable "X"' line FMPy's own
    Input.__init__ prints for every discrete_start_values input (a bare
    print(), not routed through any logger hook) alongside one line that
    must NOT be swallowed, to prove run_one()/simulate_score() only
    filters the known-noisy line rather than all inner stdout."""
    print('Warning: missing input for variable "uOutDamOvrEna"')
    print("some other FMPy diagnostic line")
    return {"time": np.array([0.0, 60.0]), "TSup": np.array([300.0, 300.0])}


def test_run_one_suppresses_missing_input_warning_but_forwards_other_output(monkeypatch, capsys):
    """The 'missing input for variable' warning fires once per FMU
    evaluation (hundreds of times across a Morris run) now that discrete
    inputs are deliberately excluded from the signal array -- drowning out
    real progress output badly enough that a long-running analysis reads
    as hung. run_one() must filter only that specific line."""
    monkeypatch.setattr(run_sensitivity, "simulate_fmu", _fake_simulate_fmu)

    score = run_sensitivity.run_one(
        fmu=Path("fake.fmu"),
        input_signal=np.zeros(2, dtype=[("time", np.float64)]),
        times=np.array([0.0, 60.0]),
        parameter_names=[],
        parameter_values=np.array([]),
        goal_fmu_variable="TSup",
        goal_conversion=None,
        measured_goal=np.array([300.0, 300.0]),
        metric="rmse",
        warmup_seconds=0.0,
        output_interval=60.0,
    )

    captured = capsys.readouterr()
    assert 'Warning: missing input for variable "uOutDamOvrEna"' not in captured.out
    assert "some other FMPy diagnostic line" in captured.out
    assert score == pytest.approx(0.0)


def test_simulate_score_suppresses_missing_input_warning_but_forwards_other_output(monkeypatch, capsys):
    """See test_run_one_suppresses_missing_input_warning_but_forwards_other_output
    -- same fix, applied identically since run_calibration.py doesn't
    import run_sensitivity.py."""
    monkeypatch.setattr(run_calibration, "simulate_fmu", _fake_simulate_fmu)

    score = run_calibration.simulate_score(
        fmu=Path("fake.fmu"),
        input_signal=np.zeros(2, dtype=[("time", np.float64)]),
        times=np.array([0.0, 60.0]),
        parameter_names=[],
        parameter_values=np.array([]),
        goal_fmu_variable="TSup",
        goal_conversion=None,
        measured_goal=np.array([300.0, 300.0]),
        metric="rmse",
        warmup_seconds=0.0,
        output_interval=60.0,
    )

    captured = capsys.readouterr()
    assert 'Warning: missing input for variable "uOutDamOvrEna"' not in captured.out
    assert "some other FMPy diagnostic line" in captured.out
    assert score == pytest.approx(0.0)


def _fake_truncated_simulate_fmu(**kwargs):
    """Reproduces FMPy's real timeout behavior: on expiry it just returns
    whatever was recorded so far, WITHOUT raising -- the result silently
    stops short of stop_time. run_one()/simulate_score() must detect this
    explicitly (a truncated result would otherwise flow straight through
    np.interp, which flat-extrapolates past the truncated range instead
    of erroring, producing a plausible-looking but wrong score)."""
    return {"time": np.array([0.0, 60.0]), "TSup": np.array([300.0, 305.0])}


def test_run_one_raises_when_simulation_is_truncated_by_timeout(monkeypatch):
    monkeypatch.setattr(run_sensitivity, "simulate_fmu", _fake_truncated_simulate_fmu)

    with pytest.raises(run_sensitivity.SensitivityError, match="stop_time"):
        run_sensitivity.run_one(
            fmu=Path("fake.fmu"),
            input_signal=np.zeros(4, dtype=[("time", np.float64)]),
            times=np.array([0.0, 60.0, 120.0, 180.0]),
            parameter_names=[],
            parameter_values=np.array([]),
            goal_fmu_variable="TSup",
            goal_conversion=None,
            measured_goal=np.array([300.0, 301.0, 302.0, 303.0]),
            metric="rmse",
            warmup_seconds=0.0,
            output_interval=60.0,
        )


def test_simulate_score_raises_when_simulation_is_truncated_by_timeout(monkeypatch):
    """See test_run_one_raises_when_simulation_is_truncated_by_timeout --
    same check, applied identically since run_calibration.py doesn't
    import run_sensitivity.py."""
    monkeypatch.setattr(run_calibration, "simulate_fmu", _fake_truncated_simulate_fmu)

    with pytest.raises(run_calibration.CalibrationError, match="stop_time"):
        run_calibration.simulate_score(
            fmu=Path("fake.fmu"),
            input_signal=np.zeros(4, dtype=[("time", np.float64)]),
            times=np.array([0.0, 60.0, 120.0, 180.0]),
            parameter_names=[],
            parameter_values=np.array([]),
            goal_fmu_variable="TSup",
            goal_conversion=None,
            measured_goal=np.array([300.0, 301.0, 302.0, 303.0]),
            metric="rmse",
            warmup_seconds=0.0,
            output_interval=60.0,
        )
