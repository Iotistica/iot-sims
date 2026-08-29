
#!/usr/bin/env python3
"""Run Morris sensitivity analysis for an FMU using model.json + historical CSV.

Example:

    python shared/run_sensitivity.py \
      --fmu models/zone/ThermalZone.fmu \
      --metadata models/zone/model.json \
      --data zone1_history.csv \
      --output sensitivity_results.json

Expected CSV columns are the model.json application-level input names plus the
calibration goal output name, for example:

    timestamp,outdoor_temp_c,internal_gain_w,discharge_air_temp_c,\
supply_airflow_m3_s,zone_temp_c

The runner:
  1. reads calibration.goal / calibration.tuners / calibration.sensitivity
  2. generates Morris parameter samples with SALib
  3. replays historical input signals through an isolated FMU for every sample
  4. scores simulated vs measured goal values
  5. computes Morris mu, mu_star, sigma and mu_star_conf
  6. writes a machine-readable JSON result

Dependencies:
    python -m pip install numpy fmpy SALib

The script intentionally does not modify the live simulator or model.json.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    from fmpy import read_model_description, simulate_fmu
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "FMPy is required. Install with: python -m pip install fmpy"
    ) from exc

try:
    from SALib.sample import morris as morris_sample
    from SALib.analyze import morris as morris_analyze
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "SALib is required. Install with: python -m pip install SALib"
    ) from exc


def _raise_cvode_max_steps(limit: int) -> None:
    """FMPy's simulate_fmu() passes relative_tolerance through to CVode but
    hardcodes maxNumSteps (SUNDIALS' mxstep -- the max internal steps
    allowed to bridge a single output_interval) at the CVodeSolver
    constructor's own default of 500, with no simulate_fmu() parameter to
    override it. RTU's DX cooling model gets locally stiff enough at some
    Morris-sampled tuner corners that even a loosened relative_tolerance
    still exceeds 500 steps within one 60 s output interval ("mxstep steps
    taken before reaching tout"). Patch the constructor's own default by
    name (not position, so this doesn't silently break if FMPy reorders
    its signature) since there's no public way to pass this through
    simulate_fmu()."""
    import inspect

    from fmpy.sundials import CVodeSolver

    parameters = inspect.signature(CVodeSolver.__init__).parameters
    if "maxNumSteps" not in parameters:
        raise SensitivityError(
            "FMPy's CVodeSolver no longer has a maxNumSteps parameter; "
            "the mxstep workaround needs updating for this FMPy version"
        )
    defaults = [
        limit if name == "maxNumSteps" else p.default
        for name, p in parameters.items()
        if p.default is not inspect.Parameter.empty
    ]
    CVodeSolver.__init__.__defaults__ = tuple(defaults)


SUPPORTED_METRICS = {"cv_rmse", "rmse", "mae", "nrmse"}


class SensitivityError(RuntimeError):
    pass


# Must run after SensitivityError is defined above -- its (unlikely)
# fallback path raises that.
_raise_cvode_max_steps(5000)


# FMI variable type -> numpy dtype FMPy's structured input array must use for
# that field. FMPy's ModelExchange/CVode input callback dispatches each
# input's fmi2SetXxx call from the FMU's own declared type (read from
# modelDescription.xml, via read_model_description below) -- it does not
# infer the type from the array's dtype. A Boolean input (e.g. RTU.fmu's
# uOutDamOvrEna) backed by float64 storage passes a malformed value to
# fmi2SetBoolean and fails; every other FMI type has the same requirement.
_FMI_TYPE_TO_NUMPY_DTYPE: dict[str, Any] = {
    "Real": np.float64,
    "Float64": np.float64,
    "Float32": np.float32,
    "Boolean": np.bool_,
    "Integer": np.int32,
    "Enumeration": np.int32,
    "Int8": np.int8,
    "UInt8": np.uint8,
    "Int16": np.int16,
    "UInt16": np.uint16,
    "Int32": np.int32,
    "UInt32": np.uint32,
    "Int64": np.int64,
    "UInt64": np.uint64,
}


def fmu_input_variables(fmu: Path) -> dict[str, dict[str, Any]]:
    """Maps each of the FMU's own input variable names to its numpy dtype
    and FMI variability, read from the FMU's real modelDescription.xml
    rather than assumed. variability distinguishes inputs FMPy's Model
    Exchange/CVode solver treats as continuous time-varying signals (safe
    to replay through the structured `input=` array) from ones it treats
    as discrete/tunable -- see prepare_inputs for why those can only be
    applied via start_values."""
    model_description = read_model_description(fmu)
    return {
        variable.name: {
            "dtype": _FMI_TYPE_TO_NUMPY_DTYPE.get(variable.type, np.float64),
            "variability": variable.variability,
        }
        for variable in model_description.modelVariables
        if variable.causality == "input"
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SensitivityError(f"{path} not found") from exc
    except json.JSONDecodeError as exc:
        raise SensitivityError(f"{path} is not valid JSON: {exc}") from exc


def _parse_timestamp(value: str) -> float | datetime:
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass

    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SensitivityError(
            f"Invalid timestamp {value!r}; use numeric seconds or ISO-8601"
        ) from exc


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise SensitivityError(f"{path} has no header row")
            rows = list(reader)
            if not rows:
                raise SensitivityError(f"{path} has no data rows")
            return list(reader.fieldnames), rows
    except FileNotFoundError as exc:
        raise SensitivityError(f"{path} not found") from exc


def build_time_vector(
    rows: list[dict[str, str]],
    timestamp_column: str,
) -> np.ndarray:
    parsed = [_parse_timestamp(row[timestamp_column]) for row in rows]

    first = parsed[0]
    if isinstance(first, datetime):
        if not all(isinstance(v, datetime) for v in parsed):
            raise SensitivityError("timestamp column mixes datetime and numeric values")
        t0 = first
        seconds = np.array(
            [(v - t0).total_seconds() for v in parsed],
            dtype=float,
        )
    else:
        if not all(isinstance(v, float) for v in parsed):
            raise SensitivityError("timestamp column mixes datetime and numeric values")
        raw = np.asarray(parsed, dtype=float)
        seconds = raw - raw[0]

    if len(seconds) < 2:
        raise SensitivityError("At least two historical samples are required")
    if not np.all(np.isfinite(seconds)):
        raise SensitivityError("timestamp column contains non-finite values")
    if np.any(np.diff(seconds) <= 0):
        raise SensitivityError("timestamps must be strictly increasing")

    return seconds


def numeric_column(
    rows: list[dict[str, str]],
    column: str,
    *,
    default: float | None = None,
) -> np.ndarray:
    """Reads `column` from every CSV row, falling back to `default`
    whenever a row's value is missing/blank -- including when the column
    doesn't exist in the CSV header at all (DictReader's row.get(column)
    returns None for every row in that case too, the same as a present-
    but-blank cell). default=None means "no fallback available": a
    missing value then still raises, exactly as before this parameter
    existed. This is what lets an optional FMU input added after a
    historical dataset was captured (e.g.
    outdoor_air_damper_override_enable/_pct) resolve to its model.json
    default instead of breaking every existing CSV that predates it --
    see prepare_inputs below, which is the only caller that ever passes a
    non-None default (the goal/measured-output column has no such
    fallback; it must always be present in real historical data)."""
    values: list[float] = []
    for i, row in enumerate(rows, start=2):
        raw = row.get(column)
        if raw is None or raw.strip() == "":
            if default is not None:
                values.append(default)
                continue
            raise SensitivityError(f"Missing value for {column!r} on CSV row {i}")
        try:
            value = float(raw)
        except ValueError as exc:
            raise SensitivityError(
                f"Non-numeric value for {column!r} on CSV row {i}: {raw!r}"
            ) from exc
        if not math.isfinite(value):
            raise SensitivityError(
                f"Non-finite value for {column!r} on CSV row {i}: {raw!r}"
            )
        values.append(value)
    return np.asarray(values, dtype=float)


def conversion_function(name: str | None) -> Callable[[np.ndarray], np.ndarray]:
    """Convert application-level model.json units to native FMU units."""
    if not name:
        return lambda x: x
    if name == "c_to_k":
        return lambda x: x + 273.15
    if name == "k_to_c":
        return lambda x: x - 273.15
    if name == "pct_to_fraction":
        return lambda x: x / 100.0
    if name == "fraction_to_pct":
        return lambda x: x * 100.0
    if name == "w_to_kw":
        return lambda x: x / 1000.0
    if name == "kw_to_w":
        return lambda x: x * 1000.0
    if name == "m3_s_to_lps":
        return lambda x: x * 1000.0
    if name == "lps_to_m3_s":
        return lambda x: x / 1000.0
    if name == "cfm_to_m3_s":
        return lambda x: x * 0.00047194745
    if name == "m3_s_to_cfm":
        return lambda x: x / 0.00047194745
    raise SensitivityError(f"Unsupported conversion {name!r}")


def _model_default(entry: dict[str, Any]) -> float | None:
    """An input's own model.json "default", coerced to the plain float
    numeric_column() works in (JSON booleans -> 0.0/1.0, matching the
    raw/un-converted CSV domain -- conversion_function() runs afterward,
    uniformly, whether a value came from the CSV or from this fallback).
    None means the input has no declared default, so a missing CSV
    value/column must still fail loudly rather than silently guessing."""
    default = entry.get("default")
    if default is None:
        return None
    if isinstance(default, bool):
        return 1.0 if default else 0.0
    return float(default)


def prepare_inputs(
    metadata: dict[str, Any],
    rows: list[dict[str, str]],
    times: np.ndarray,
    input_variables: dict[str, dict[str, Any]] | None = None,
) -> tuple[np.ndarray, dict[str, float]]:
    """input_variables maps fmu_variable -> {"dtype", "variability"}, from
    fmu_input_variables() (the FMU's real declared types). Omitted/unknown
    variables default to continuous Real, so callers without an FMU on
    hand (e.g. synthetic unit tests) still work.

    Returns (signal, discrete_start_values):
      - signal is the structured time-varying array for FMPy's `input=`,
        covering every continuous input as before.
      - discrete_start_values holds the resolved constant value for any
        input the FMU declares discrete/tunable variability (e.g. RTU's
        Boolean uOutDamOvrEna). FMPy's ModelExchange/CVode integrator
        applies signal-array inputs from inside the solver's event-
        indicator/right-hand-side callbacks on every evaluation, which is
        only valid for genuinely continuous signals -- replaying a
        discrete input the same way corrupts the FMU's internal event-
        handling state machine (observed as fmi2SetBoolean /
        fmi2SetContinuousStates failing with status 3). A discrete-
        variability input can only be set once at initialization via
        start_values, so its CSV/default-derived value must be constant
        across the whole historical period; a genuinely time-varying
        discrete input raises rather than silently dropping the
        variation."""
    entries = metadata.get("inputs") or []
    if not entries:
        raise SensitivityError("model.json has no inputs")

    input_variables = input_variables or {}
    dtype: list[tuple[str, Any]] = [("time", np.float64)]
    series: dict[str, np.ndarray] = {}
    discrete_start_values: dict[str, float] = {}

    for entry in entries:
        csv_name = entry.get("name")
        fmu_name = entry.get("fmu_variable")
        if not csv_name or not fmu_name:
            raise SensitivityError("Every model.json input needs name and fmu_variable")

        values = numeric_column(rows, csv_name, default=_model_default(entry))
        values = conversion_function(entry.get("conversion"))(values)

        variability = input_variables.get(fmu_name, {}).get("variability", "continuous")
        if variability in ("discrete", "tunable"):
            unique = np.unique(values)
            if unique.size > 1:
                raise SensitivityError(
                    f"{fmu_name!r} is a {variability} FMI input, so it can "
                    "only be replayed as a constant start value -- but "
                    f"{csv_name!r} varies over the historical period "
                    f"({unique.tolist()!r})"
                )
            discrete_start_values[fmu_name] = float(unique[0]) if unique.size else 0.0
            continue

        series[fmu_name] = values
        dtype.append((fmu_name, input_variables.get(fmu_name, {}).get("dtype", np.float64)))

    signal = np.zeros(len(times), dtype=dtype)
    signal["time"] = times
    for fmu_name, values in series.items():
        signal[fmu_name] = values

    return signal, discrete_start_values


def resolve_goal(metadata: dict[str, Any]) -> tuple[str, str, str | None]:
    calibration = metadata.get("calibration")
    if not isinstance(calibration, dict):
        raise SensitivityError("model.json has no calibration object")

    goal = calibration.get("goal")
    if not isinstance(goal, dict):
        raise SensitivityError("calibration.goal must be an object")

    configured = goal.get("output") or goal.get("variable")
    if not configured:
        raise SensitivityError("calibration.goal.output is required")

    outputs = metadata.get("outputs") or []
    for entry in outputs:
        if configured in (entry.get("name"), entry.get("fmu_variable")):
            csv_name = entry.get("name")
            fmu_name = entry.get("fmu_variable")
            if not csv_name or not fmu_name:
                break
            return csv_name, fmu_name, entry.get("conversion")

    raise SensitivityError(
        f"Calibration goal {configured!r} does not resolve to a model.json output"
    )


def build_problem(metadata: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    calibration = metadata["calibration"]
    tuners = calibration.get("tuners") or []
    if not tuners:
        raise SensitivityError("calibration.tuners is empty")

    names: list[str] = []
    bounds: list[list[float]] = []

    for tuner in tuners:
        name = tuner.get("parameter")
        minimum = tuner.get("min")
        maximum = tuner.get("max")
        if not name:
            raise SensitivityError("A calibration tuner is missing parameter")
        if minimum is None or maximum is None:
            raise SensitivityError(f"Tuner {name!r} is missing min/max")
        names.append(str(name))
        bounds.append([float(minimum), float(maximum)])

    problem = {
        "num_vars": len(names),
        "names": names,
        "bounds": bounds,
    }
    return problem, tuners


def metric_value(
    metric: str,
    measured: np.ndarray,
    predicted: np.ndarray,
) -> float:
    mask = np.isfinite(measured) & np.isfinite(predicted)
    measured = measured[mask]
    predicted = predicted[mask]

    if measured.size == 0:
        raise SensitivityError("No finite measured/predicted values remain for scoring")

    error = predicted - measured
    rmse = float(np.sqrt(np.mean(error ** 2)))

    if metric == "rmse":
        return rmse
    if metric == "mae":
        return float(np.mean(np.abs(error)))
    if metric == "cv_rmse":
        denominator = abs(float(np.mean(measured)))
        if denominator <= 1e-12:
            raise SensitivityError("CV(RMSE) undefined because mean measured value is zero")
        return 100.0 * rmse / denominator
    if metric == "nrmse":
        data_range = float(np.max(measured) - np.min(measured))
        if data_range <= 1e-12:
            raise SensitivityError("NRMSE undefined because measured range is zero")
        return rmse / data_range

    raise SensitivityError(
        f"Unsupported metric {metric!r}; supported: {sorted(SUPPORTED_METRICS)}"
    )


def estimate_output_interval(times: np.ndarray) -> float:
    deltas = np.diff(times)
    interval = float(np.median(deltas))
    if not math.isfinite(interval) or interval <= 0:
        raise SensitivityError("Could not determine a valid simulation output interval")
    return interval


def run_one(
    *,
    fmu: Path,
    input_signal: np.ndarray,
    times: np.ndarray,
    parameter_names: list[str],
    parameter_values: np.ndarray,
    goal_fmu_variable: str,
    goal_conversion: str | None,
    measured_goal: np.ndarray,
    metric: str,
    warmup_seconds: float,
    output_interval: float,
    discrete_start_values: dict[str, float] | None = None,
    timeout_seconds: float | None = 180.0,
) -> float:
    start_values = {
        name: float(value)
        for name, value in zip(parameter_names, parameter_values, strict=True)
    }
    if discrete_start_values:
        start_values.update(discrete_start_values)

    # FMPy's Input.__init__ prints (via a bare print(), not a logger hook)
    # 'Warning: missing input for variable "X"' for every FMU input that
    # isn't in the structured signal array -- which now includes every
    # discrete_start_values input by design (see prepare_inputs). At one
    # print per FMU evaluation across hundreds of Morris runs, this drowns
    # out the actual progress output and reads as a hang. Capture it here
    # rather than at every call site; forward anything unexpected through
    # so a genuinely new FMPy message doesn't get silently lost.
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        result = simulate_fmu(
            filename=str(fmu),
            start_time=0.0,
            stop_time=float(times[-1]),
            input=input_signal,
            output=[goal_fmu_variable],
            output_interval=output_interval,
            start_values=start_values,
            validate=False,
            # FMPy's timeout only checks elapsed wall-clock time between
            # full communication-point steps, not preemptively inside a
            # single stiff step -- and on timeout it just returns whatever
            # was recorded so far, WITHOUT raising. A silently truncated
            # result would flow straight through np.interp below, which
            # flat-extrapolates past the truncated range instead of
            # erroring, producing a plausible-looking but wrong score. The
            # explicit stop_time check after the call (not this parameter
            # alone) is what actually turns a stuck/pathological
            # evaluation into a clean, catchable failure -- observed as a
            # single Morris trajectory blocking all progress for ~20
            # minutes before this was added.
            timeout=timeout_seconds,
            # RTU.fmu exports both CoSimulation and ModelExchange; FMPy
            # prefers CoSimulation when fmi_type is left unset, which uses
            # the FMU's own baked-in fixed-step solver. This RTU's
            # fluid/mixing dynamics were only ever validated under Model
            # Exchange with CVode's adaptive internal stepping --
            # CoSimulation's coarser stepping through a 60 s communication
            # interval is what let the mixing volume's temperature run
            # away past its physical bound before the solver could react.
            # ModelExchange+CVode integrates with adaptive smaller
            # internal steps while still only reporting results at
            # output_interval (the historical sampling interval).
            fmi_type="ModelExchange",
            solver="CVode",
            # CVode's default relative_tolerance (1e-5) can force so many
            # tiny internal steps through a locally stiff patch (e.g. a
            # Morris sample landing on an aggressive DX capacity/SAT-loop
            # corner) that it hits SUNDIALS' internal step cap (mxstep=500)
            # before reaching the next 60 s output_interval, aborting with
            # "mxstep steps taken before reaching tout". FMPy doesn't
            # expose mxstep directly; loosening the tolerance an order of
            # magnitude is the standard fix -- it lets the solver take
            # larger, still-adequate steps instead.
            relative_tolerance=1e-4,
        )

    for line in captured_stdout.getvalue().splitlines():
        if line.strip().startswith('Warning: missing input for variable "'):
            continue
        print(line)

    result_time = np.asarray(result["time"], dtype=float)
    stop_time = float(times[-1])
    if result_time.size == 0 or result_time[-1] < stop_time - max(output_interval, 1.0):
        raise SensitivityError(
            f"Simulation stopped at t={result_time[-1] if result_time.size else 0.0:.6g} "
            f"instead of reaching stop_time={stop_time:.6g} -- likely a "
            f"timeout (timeout_seconds={timeout_seconds}) on a stuck/"
            "pathologically slow evaluation"
        )
    native_prediction = np.asarray(result[goal_fmu_variable], dtype=float)

    prediction = conversion_function(goal_conversion)(native_prediction)
    prediction_at_measurements = np.interp(times, result_time, prediction)

    mask = times >= float(warmup_seconds)
    if not np.any(mask):
        raise SensitivityError(
            f"warmup_seconds={warmup_seconds} removes the entire historical period"
        )

    return metric_value(
        metric,
        measured_goal[mask],
        prediction_at_measurements[mask],
    )


class MorrisTrajectoriesResult:
    """Result of evaluate_morris_trajectories(). Plain attribute holder
    (not a dataclass -- no need for the extra import) so main() can pull
    everything it needs for both the console summary and the JSON report
    off one object."""

    def __init__(
        self,
        samples_kept: np.ndarray,
        scores_kept: np.ndarray,
        dropped_trajectories: list[dict[str, Any]],
        num_trajectories: int,
        evaluations_completed: int,
    ) -> None:
        self.samples_kept = samples_kept
        self.scores_kept = scores_kept
        self.dropped_trajectories = dropped_trajectories
        self.num_trajectories = num_trajectories
        self.evaluations_completed = evaluations_completed
        self.dropped_count = len(dropped_trajectories)
        self.completed_trajectories = num_trajectories - self.dropped_count
        self.dropped_rate = self.dropped_count / num_trajectories if num_trajectories else 0.0
        self.reliable = self.dropped_rate <= 0.10


def evaluate_morris_trajectories(
    *,
    samples: np.ndarray,
    num_vars: int,
    parameter_names: list[str],
    evaluate: Callable[[np.ndarray], float],
    metric: str,
    progress_every: int = 0,
    log: Callable[[str], None] = print,
) -> MorrisTrajectoriesResult:
    """Evaluates every Morris trajectory in `samples` via `evaluate` (one
    parameter vector -> one score, raising on failure). A Morris trajectory
    is num_vars+1 consecutive rows (a base point plus one one-at-a-time
    step per tuner) -- the elementary-effect calculation needs every row
    in a trajectory, so if `evaluate` raises for any one row (e.g. a tuner
    corner that makes the FMU replay genuinely non-convergent, such as an
    aggressive SAT-loop gain interacting with a real setpoint/fan-schedule
    step in the historical data), the *whole* trajectory is dropped and
    recorded in the result rather than aborting the entire analysis.
    Raises SensitivityError only if every trajectory fails."""
    trajectory_size = num_vars + 1
    if samples.shape[0] % trajectory_size != 0:
        raise SensitivityError(
            f"Morris samples ({samples.shape[0]} rows) is not a multiple "
            f"of the trajectory size ({trajectory_size})"
        )
    num_trajectories = samples.shape[0] // trajectory_size

    kept_samples: list[np.ndarray] = []
    kept_scores: list[np.ndarray] = []
    dropped_trajectories: list[dict[str, Any]] = []
    evaluations_completed = 0

    for t_idx in range(num_trajectories):
        start = t_idx * trajectory_size
        trajectory_samples = samples[start:start + trajectory_size]
        trajectory_scores = np.empty(trajectory_size, dtype=float)
        failure: dict[str, Any] | None = None

        for row_idx, sample in enumerate(trajectory_samples):
            try:
                trajectory_scores[row_idx] = evaluate(sample)
                evaluations_completed += 1
                if progress_every > 0 and evaluations_completed % progress_every == 0:
                    log(
                        f"  completed {evaluations_completed} evaluations "
                        f"({t_idx + 1}/{num_trajectories} trajectories) "
                        f"last_{metric}={trajectory_scores[row_idx]:.6g}"
                    )
            except Exception as exc:
                failure = {
                    "trajectory_index": t_idx,
                    "failed_row_in_trajectory": row_idx,
                    "parameters": {
                        name: float(value)
                        for name, value in zip(parameter_names, sample, strict=True)
                    },
                    "error": str(exc),
                }
                break

        if failure is not None:
            dropped_trajectories.append(failure)
            log(
                f"  trajectory {t_idx + 1}/{num_trajectories} DROPPED "
                f"(row {failure['failed_row_in_trajectory']}): {failure['error']}"
            )
            continue

        kept_samples.append(trajectory_samples)
        kept_scores.append(trajectory_scores)

    result = MorrisTrajectoriesResult(
        samples_kept=(
            np.concatenate(kept_samples, axis=0) if kept_samples else np.empty((0, num_vars))
        ),
        scores_kept=np.concatenate(kept_scores, axis=0) if kept_scores else np.empty(0),
        dropped_trajectories=dropped_trajectories,
        num_trajectories=num_trajectories,
        evaluations_completed=evaluations_completed,
    )

    if result.completed_trajectories == 0:
        first_error = dropped_trajectories[0]["error"] if dropped_trajectories else "unknown"
        raise SensitivityError(
            f"All {num_trajectories} Morris trajectories failed -- no "
            f"usable sensitivity data. First failure: {first_error}"
        )

    if not result.reliable:
        log(
            f"WARNING: {result.dropped_count}/{num_trajectories} Morris "
            f"trajectories ({result.dropped_rate:.1%}) failed to complete -- "
            "sensitivity result may be unreliable; review "
            "calibration.tuners bounds before calibration."
        )

    return result


def json_float(value: Any) -> float | None:
    value = float(value)
    return value if math.isfinite(value) else None


def main() -> int:
    # stdout is fully block-buffered (not line-buffered) whenever it isn't
    # a real terminal -- true for GitHub Actions and any piped/redirected
    # run. Every print() below (baseline status, per-evaluation progress,
    # dropped-trajectory notices) would otherwise sit invisible in that
    # buffer until it fills or the process exits, while SUNDIALS' native
    # CVode error lines (written directly to the OS stderr file
    # descriptor from C, bypassing Python's stdout entirely) show up
    # immediately -- making a run that's actually progressing normally
    # look silent/stuck except for that native noise.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fmu", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("sensitivity_results.json"),
        help="Output JSON path (default: sensitivity_results.json)",
    )
    parser.add_argument(
        "--timestamp-column",
        default="timestamp",
        help="CSV time column (default: timestamp)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for Morris sampling/analysis (default: 42)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print progress every N FMU evaluations (default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help=(
            "Per-evaluation wall-clock timeout in seconds (default: 180). "
            "Bounds how long one pathologically slow/stuck FMU evaluation "
            "can block the whole Morris run before its trajectory is "
            "dropped like any other failure."
        ),
    )
    args = parser.parse_args()

    try:
        metadata = load_json(args.metadata)
        headers, rows = load_csv(args.data)

        if args.timestamp_column not in headers:
            raise SensitivityError(
                f"CSV is missing timestamp column {args.timestamp_column!r}"
            )

        times = build_time_vector(rows, args.timestamp_column)
        input_variables = fmu_input_variables(args.fmu)
        input_signal, discrete_start_values = prepare_inputs(
            metadata, rows, times, input_variables
        )

        goal_csv_name, goal_fmu_name, goal_conversion = resolve_goal(metadata)
        if goal_csv_name not in headers:
            raise SensitivityError(
                f"CSV is missing calibration goal column {goal_csv_name!r}"
            )
        measured_goal = numeric_column(rows, goal_csv_name)

        calibration = metadata["calibration"]
        goal_config = calibration["goal"]
        metric = goal_config.get("metric") or calibration.get("metric") or "cv_rmse"
        if metric not in SUPPORTED_METRICS:
            raise SensitivityError(
                f"Unsupported metric {metric!r}; supported: {sorted(SUPPORTED_METRICS)}"
            )

        sensitivity = calibration.get("sensitivity") or {}
        method = sensitivity.get("method")
        if method != "morris":
            raise SensitivityError(
                f"This runner currently supports only Morris; got {method!r}"
            )

        trajectories = int(sensitivity.get("trajectories", 100))
        grid_levels = int(sensitivity.get("grid_levels", 4))
        warmup_seconds = float(metadata.get("warmup_seconds", 0.0))

        problem, tuners = build_problem(metadata)
        parameter_names = problem["names"]

        samples = morris_sample.sample(
            problem,
            N=trajectories,
            num_levels=grid_levels,
            seed=args.seed,
        )

        output_interval = estimate_output_interval(times)

        # Baseline replay at nominal parameter values, before spending the
        # full trajectories*(num_vars+1) Morris budget on a model/input
        # combination that might not even complete one run. Also doubles as
        # a debug print of the first converted FMU input sample.
        first_sample = {
            name: input_signal[name][0]
            for name in input_signal.dtype.names
            if name != "time"
        }
        print(
            f"Baseline replay: first FMU input sample={first_sample} "
            f"discrete_start_values={discrete_start_values}"
        )

        initial_values = np.array(
            [
                float(tuner.get("initial", sum(bounds) / 2.0))
                for tuner, bounds in zip(tuners, problem["bounds"], strict=True)
            ],
            dtype=float,
        )
        try:
            baseline_score = run_one(
                fmu=args.fmu,
                input_signal=input_signal,
                times=times,
                parameter_names=parameter_names,
                parameter_values=initial_values,
                goal_fmu_variable=goal_fmu_name,
                goal_conversion=goal_conversion,
                measured_goal=measured_goal,
                metric=metric,
                warmup_seconds=warmup_seconds,
                output_interval=output_interval,
                discrete_start_values=discrete_start_values,
                timeout_seconds=args.timeout,
            )
        except Exception as exc:
            raise SensitivityError(
                "Baseline replay at nominal parameter values failed before "
                f"Morris sampling began: {exc}"
            ) from exc
        print(f"Baseline replay succeeded: {metric}={baseline_score:.6g}")

        print(
            f"Morris sensitivity: model={metadata.get('id', args.fmu.stem)} "
            f"tuners={parameter_names} trajectories={trajectories} "
            f"grid_levels={grid_levels} evaluations={len(samples)} "
            f"metric={metric}"
        )

        trajectories_result = evaluate_morris_trajectories(
            samples=samples,
            num_vars=len(parameter_names),
            parameter_names=parameter_names,
            evaluate=lambda sample: run_one(
                fmu=args.fmu,
                input_signal=input_signal,
                times=times,
                parameter_names=parameter_names,
                parameter_values=sample,
                goal_fmu_variable=goal_fmu_name,
                goal_conversion=goal_conversion,
                measured_goal=measured_goal,
                metric=metric,
                warmup_seconds=warmup_seconds,
                output_interval=output_interval,
                discrete_start_values=discrete_start_values,
                timeout_seconds=args.timeout,
            ),
            metric=metric,
            progress_every=args.progress_every,
        )
        samples_kept = trajectories_result.samples_kept
        scores_kept = trajectories_result.scores_kept
        dropped_trajectories = trajectories_result.dropped_trajectories
        num_trajectories = trajectories_result.num_trajectories
        completed_trajectories = trajectories_result.completed_trajectories
        dropped_count = trajectories_result.dropped_count
        dropped_rate = trajectories_result.dropped_rate
        reliable = trajectories_result.reliable
        evaluations_completed = trajectories_result.evaluations_completed

        indices = morris_analyze.analyze(
            problem,
            samples_kept,
            scores_kept,
            num_levels=grid_levels,
            seed=args.seed,
            print_to_console=False,
        )

        result_rows: list[dict[str, Any]] = []
        for i, parameter in enumerate(parameter_names):
            tuner = tuners[i]
            result_rows.append({
                "parameter": parameter,
                "label": tuner.get("label", parameter),
                "unit": tuner.get("unit"),
                "mu": json_float(indices["mu"][i]),
                "mu_star": json_float(indices["mu_star"][i]),
                "sigma": json_float(indices["sigma"][i]),
                "mu_star_conf": json_float(indices["mu_star_conf"][i]),
            })

        result_rows.sort(
            key=lambda row: (
                row["mu_star"] is not None,
                row["mu_star"] if row["mu_star"] is not None else -math.inf,
            ),
            reverse=True,
        )
        for rank, row in enumerate(result_rows, start=1):
            row["rank"] = rank

        output_payload = {
            "model": metadata.get("id") or args.fmu.stem,
            "fmu": str(args.fmu),
            "metadata": str(args.metadata),
            "data": str(args.data),
            "goal": {
                "metadata_output": goal_csv_name,
                "fmu_variable": goal_fmu_name,
                "metric": metric,
            },
            "historical_period": {
                "samples": len(times),
                "duration_seconds": float(times[-1]),
                "warmup_seconds_excluded_from_metric": warmup_seconds,
                "output_interval_seconds": output_interval,
            },
            "sensitivity": {
                "method": "morris",
                "trajectories": trajectories,
                "grid_levels": grid_levels,
                "measure": sensitivity.get("measure", "mu_star"),
                "seed": args.seed,
                "fmu_evaluations": evaluations_completed,
                "trajectories_attempted": num_trajectories,
                "trajectories_completed": completed_trajectories,
                "trajectories_dropped": dropped_count,
                "dropped_trajectory_rate": dropped_rate,
                "reliable": reliable,
                "dropped_trajectories": dropped_trajectories,
                "score_summary": {
                    "min": json_float(np.min(scores_kept)),
                    "max": json_float(np.max(scores_kept)),
                    "mean": json_float(np.mean(scores_kept)),
                },
                "results": result_rows,
            },
        }

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(output_payload, indent=2) + "\n",
            encoding="utf-8",
        )

        print(
            f"\nTrajectories: {completed_trajectories}/{num_trajectories} completed "
            f"({dropped_count} dropped, {dropped_rate:.1%}), "
            f"reliable={reliable}"
        )
        print("\nSensitivity ranking:")
        for row in result_rows:
            print(
                f"  {row['rank']}. {row['parameter']}: "
                f"mu*={row['mu_star']:.6g} "
                f"sigma={row['sigma']:.6g}"
            )

        print(f"\nWrote {args.output}")
        return 0

    except SensitivityError as exc:
        print(f"Sensitivity analysis failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Sensitivity analysis failed with unexpected error: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
