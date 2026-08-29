#!/usr/bin/env python3
"""Run Differential Evolution calibration for an FMU using model.json + CSV.

Example:

    python shared/run_calibration.py \
      --fmu models/zone/ThermalZone.fmu \
      --metadata models/zone/model.json \
      --data models/zone/calibration/zone1_history.csv \
      --sensitivity models/zone/calibration/zone1_sensitivity.json \
      --output models/zone/calibration/zone1_calibration.json

Dependencies:
    python -m pip install numpy scipy fmpy

This script never modifies the live simulator or model.json.
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
except ImportError as exc:
    raise SystemExit("FMPy is required. Install with: python -m pip install fmpy") from exc

try:
    from scipy.optimize import differential_evolution
except ImportError as exc:
    raise SystemExit("SciPy is required. Install with: python -m pip install scipy") from exc


def _raise_cvode_max_steps(limit: int) -> None:
    """See run_sensitivity._raise_cvode_max_steps -- same mxstep workaround,
    kept here since this file doesn't import that one."""
    import inspect

    from fmpy.sundials import CVodeSolver

    parameters = inspect.signature(CVodeSolver.__init__).parameters
    if "maxNumSteps" not in parameters:
        raise CalibrationError(
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
SUPPORTED_OPTIMIZERS = {"differential_evolution"}


class CalibrationError(RuntimeError):
    pass


# Must run after CalibrationError is defined above -- its (unlikely)
# fallback path raises that.
_raise_cvode_max_steps(5000)


# See run_sensitivity._FMI_TYPE_TO_NUMPY_DTYPE / fmu_input_variables -- same
# contract, kept here since this file doesn't import that one.
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
    """See run_sensitivity.fmu_input_variables -- same dtype/variability
    contract, kept here since this file doesn't import that one."""
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
        raise CalibrationError(f"{path} not found") from exc
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"{path} is not valid JSON: {exc}") from exc


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise CalibrationError(f"{path} has no header row")
            rows = list(reader)
            if not rows:
                raise CalibrationError(f"{path} has no data rows")
            return list(reader.fieldnames), rows
    except FileNotFoundError as exc:
        raise CalibrationError(f"{path} not found") from exc


def _parse_timestamp(value: str) -> float | datetime:
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalibrationError(
            f"Invalid timestamp {value!r}; use numeric seconds or ISO-8601"
        ) from exc


def build_time_vector(rows: list[dict[str, str]], timestamp_column: str) -> np.ndarray:
    parsed = [_parse_timestamp(row[timestamp_column]) for row in rows]
    first = parsed[0]

    if isinstance(first, datetime):
        if not all(isinstance(v, datetime) for v in parsed):
            raise CalibrationError("timestamp column mixes datetime and numeric values")
        t0 = first
        seconds = np.asarray([(v - t0).total_seconds() for v in parsed], dtype=float)
    else:
        if not all(isinstance(v, float) for v in parsed):
            raise CalibrationError("timestamp column mixes datetime and numeric values")
        raw = np.asarray(parsed, dtype=float)
        seconds = raw - raw[0]

    if len(seconds) < 2:
        raise CalibrationError("At least two historical samples are required")
    if np.any(np.diff(seconds) <= 0):
        raise CalibrationError("timestamps must be strictly increasing")
    return seconds


def numeric_column(
    rows: list[dict[str, str]],
    column: str,
    *,
    default: float | None = None,
) -> np.ndarray:
    """See run_sensitivity.numeric_column -- same fallback contract, kept
    here since run_calibration.py does not import run_sensitivity.py."""
    values = []
    for i, row in enumerate(rows, start=2):
        raw = row.get(column)
        if raw is None or raw.strip() == "":
            if default is not None:
                values.append(default)
                continue
            raise CalibrationError(f"Missing value for {column!r} on CSV row {i}")
        try:
            value = float(raw)
        except ValueError as exc:
            raise CalibrationError(
                f"Non-numeric value for {column!r} on CSV row {i}: {raw!r}"
            ) from exc
        if not math.isfinite(value):
            raise CalibrationError(
                f"Non-finite value for {column!r} on CSV row {i}: {raw!r}"
            )
        values.append(value)
    return np.asarray(values, dtype=float)


def conversion_function(name: str | None) -> Callable[[np.ndarray], np.ndarray]:
    if not name:
        return lambda x: x
    conversions: dict[str, Callable[[np.ndarray], np.ndarray]] = {
        "c_to_k": lambda x: x + 273.15,
        "k_to_c": lambda x: x - 273.15,
        "pct_to_fraction": lambda x: x / 100.0,
        "fraction_to_pct": lambda x: x * 100.0,
        "w_to_kw": lambda x: x / 1000.0,
        "kw_to_w": lambda x: x * 1000.0,
        "m3_s_to_lps": lambda x: x * 1000.0,
        "lps_to_m3_s": lambda x: x / 1000.0,
        "cfm_to_m3_s": lambda x: x * 0.00047194745,
        "m3_s_to_cfm": lambda x: x / 0.00047194745,
    }
    try:
        return conversions[name]
    except KeyError as exc:
        raise CalibrationError(f"Unsupported conversion {name!r}") from exc


def _model_default(entry: dict[str, Any]) -> float | None:
    """See run_sensitivity._model_default -- same coercion contract, kept
    here since run_calibration.py does not import run_sensitivity.py."""
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
    """See run_sensitivity.prepare_inputs -- same input_variables contract
    and (signal, discrete_start_values) return shape, kept here since this
    file doesn't import that one."""
    entries = metadata.get("inputs") or []
    if not entries:
        raise CalibrationError("model.json has no inputs")

    input_variables = input_variables or {}
    dtype = [("time", np.float64)]
    series: dict[str, np.ndarray] = {}
    discrete_start_values: dict[str, float] = {}

    for entry in entries:
        csv_name = entry.get("name")
        fmu_name = entry.get("fmu_variable")
        if not csv_name or not fmu_name:
            raise CalibrationError("Every model.json input needs name and fmu_variable")

        values = numeric_column(rows, csv_name, default=_model_default(entry))
        values = conversion_function(entry.get("conversion"))(values)

        variability = input_variables.get(fmu_name, {}).get("variability", "continuous")
        if variability in ("discrete", "tunable"):
            unique = np.unique(values)
            if unique.size > 1:
                raise CalibrationError(
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


def resolve_goal(metadata: dict[str, Any]) -> tuple[str, str, str | None, str]:
    calibration = metadata.get("calibration")
    if not isinstance(calibration, dict):
        raise CalibrationError("model.json has no calibration object")
    goal = calibration.get("goal")
    if not isinstance(goal, dict):
        raise CalibrationError("calibration.goal must be an object")

    configured = goal.get("output") or goal.get("variable")
    if not configured:
        raise CalibrationError("calibration.goal.output is required")

    metric = goal.get("metric") or calibration.get("metric") or "cv_rmse"
    if metric not in SUPPORTED_METRICS:
        raise CalibrationError(f"Unsupported metric {metric!r}")

    for entry in metadata.get("outputs") or []:
        if configured in (entry.get("name"), entry.get("fmu_variable")):
            return (
                entry["name"],
                entry["fmu_variable"],
                entry.get("conversion"),
                metric,
            )

    raise CalibrationError(
        f"Calibration goal {configured!r} does not resolve to a model.json output"
    )


def resolve_tuners(
    metadata: dict[str, Any],
    sensitivity_path: Path | None,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[float, float]]]:
    tuners = list(metadata["calibration"].get("tuners") or [])
    if not tuners:
        raise CalibrationError("calibration.tuners is empty")

    if sensitivity_path is not None:
        sensitivity = load_json(sensitivity_path)
        ranked = sensitivity.get("sensitivity", {}).get("results")
        if not isinstance(ranked, list):
            raise CalibrationError(
                f"{sensitivity_path} does not contain sensitivity.results"
            )
        ranked_names = [r.get("parameter") for r in ranked if isinstance(r, dict)]
        configured_names = [t.get("parameter") for t in tuners]
        missing = [n for n in configured_names if n not in ranked_names]
        if missing:
            raise CalibrationError(
                "Sensitivity result is missing configured tuner(s): "
                + ", ".join(str(x) for x in missing)
            )
        order = {name: i for i, name in enumerate(ranked_names)}
        tuners.sort(key=lambda t: order.get(t.get("parameter"), 10**9))

    names = []
    bounds = []
    for tuner in tuners:
        name = tuner.get("parameter")
        minimum = float(tuner["min"])
        maximum = float(tuner["max"])
        if minimum >= maximum:
            raise CalibrationError(f"Tuner {name!r} requires min < max")
        names.append(str(name))
        bounds.append((minimum, maximum))
    return tuners, names, bounds


def metric_value(metric: str, measured: np.ndarray, predicted: np.ndarray) -> float:
    mask = np.isfinite(measured) & np.isfinite(predicted)
    measured = measured[mask]
    predicted = predicted[mask]
    if measured.size == 0:
        raise CalibrationError("No finite values remain for scoring")

    err = predicted - measured
    rmse = float(np.sqrt(np.mean(err**2)))

    if metric == "rmse":
        return rmse
    if metric == "mae":
        return float(np.mean(np.abs(err)))
    if metric == "cv_rmse":
        mean_measured = abs(float(np.mean(measured)))
        if mean_measured <= 1e-12:
            raise CalibrationError("CV(RMSE) undefined because mean measured value is zero")
        return 100.0 * rmse / mean_measured
    if metric == "nrmse":
        span = float(np.max(measured) - np.min(measured))
        if span <= 1e-12:
            raise CalibrationError("NRMSE undefined because measured range is zero")
        return rmse / span
    raise CalibrationError(f"Unsupported metric {metric!r}")


def simulate_score(
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

    # See run_sensitivity.run_one -- same stdout-capture, applied
    # identically since this file doesn't import that one. FMPy's
    # Input.__init__ prints (via bare print(), not a logger hook) 'Warning:
    # missing input for variable "X"' for every discrete_start_values
    # input, once per FMU evaluation across an entire DE optimization run.
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
            # See run_sensitivity.run_one -- same wall-clock timeout with
            # explicit truncation check below, applied identically since
            # this file doesn't import that one. FMPy's timeout doesn't
            # raise on expiry, it just returns whatever was recorded so
            # far -- the check after this call is what actually turns a
            # stuck evaluation into a clean, catchable failure.
            timeout=timeout_seconds,
            # See run_sensitivity.run_one -- same FMI execution mode,
            # applied identically since this file doesn't import that one.
            # FMPy defaults to CoSimulation (the FMU's own fixed-step
            # solver) when fmi_type is unset; this RTU's fluid/mixing
            # dynamics were only ever validated under Model Exchange with
            # CVode's adaptive internal stepping.
            fmi_type="ModelExchange",
            solver="CVode",
            # See run_sensitivity.run_one -- same tolerance loosening,
            # applied identically since this file doesn't import that one.
            # Avoids CVode hitting SUNDIALS' internal step cap (mxstep=500)
            # on a locally stiff patch before reaching the next
            # output_interval.
            relative_tolerance=1e-4,
        )

    for line in captured_stdout.getvalue().splitlines():
        if line.strip().startswith('Warning: missing input for variable "'):
            continue
        print(line)

    result_time = np.asarray(result["time"], dtype=float)
    stop_time = float(times[-1])
    if result_time.size == 0 or result_time[-1] < stop_time - max(output_interval, 1.0):
        raise CalibrationError(
            f"Simulation stopped at t={result_time[-1] if result_time.size else 0.0:.6g} "
            f"instead of reaching stop_time={stop_time:.6g} -- likely a "
            f"timeout (timeout_seconds={timeout_seconds}) on a stuck/"
            "pathologically slow evaluation"
        )
    prediction = conversion_function(goal_conversion)(
        np.asarray(result[goal_fmu_variable], dtype=float)
    )
    prediction_at_measurements = np.interp(times, result_time, prediction)

    mask = times >= warmup_seconds
    if not np.any(mask):
        raise CalibrationError("Warmup removes the entire historical period")

    return metric_value(metric, measured_goal[mask], prediction_at_measurements[mask])


def main() -> int:
    # See run_sensitivity.main -- same line-buffering fix, applied
    # identically since this file doesn't import that one. Without it,
    # every print() below (baseline status, per-evaluation progress) sits
    # invisible in stdout's block buffer on a non-terminal run (e.g.
    # GitHub Actions) while SUNDIALS' native CVode error lines (written
    # directly to the OS stderr file descriptor from C) show up
    # immediately, making a normally-progressing run look silent/stuck.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fmu", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--sensitivity", type=Path)
    parser.add_argument("--output", type=Path, default=Path("calibration_results.json"))
    parser.add_argument("--timestamp-column", default="timestamp")
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help=(
            "Per-evaluation wall-clock timeout in seconds (default: 180). "
            "Bounds how long one pathologically slow/stuck FMU evaluation "
            "can block the whole optimization run."
        ),
    )
    parser.add_argument(
        "--failure-penalty",
        type=float,
        default=1e6,
        help=(
            "Score assigned to a tuner combination whose FMU evaluation "
            "fails (timeout, mxstep exhaustion, etc.), default: 1e6. "
            "differential_evolution's objective can't raise -- without a "
            "penalty, a single failing combination would abort the whole "
            "optimization instead of just being steered away from."
        ),
    )
    args = parser.parse_args()

    try:
        metadata = load_json(args.metadata)
        headers, rows = load_csv(args.data)
        if args.timestamp_column not in headers:
            raise CalibrationError(
                f"CSV is missing timestamp column {args.timestamp_column!r}"
            )

        times = build_time_vector(rows, args.timestamp_column)
        input_variables = fmu_input_variables(args.fmu)
        input_signal, discrete_start_values = prepare_inputs(
            metadata, rows, times, input_variables
        )

        goal_csv_name, goal_fmu_name, goal_conversion, metric = resolve_goal(metadata)
        measured_goal = numeric_column(rows, goal_csv_name)

        tuners, parameter_names, bounds = resolve_tuners(metadata, args.sensitivity)
        optimizer = metadata["calibration"].get("optimizer") or {}

        method = optimizer.get("method", "differential_evolution")
        if method not in SUPPORTED_OPTIMIZERS:
            raise CalibrationError(f"Unsupported optimizer {method!r}")

        max_iterations = int(optimizer.get("max_iterations", 30))
        population_size = int(optimizer.get("population_size", 10))
        tolerance = float(optimizer.get("tolerance", 0.01))
        seed = int(optimizer.get("seed", 42))
        polish = bool(optimizer.get("polish", True))

        warmup_seconds = float(metadata.get("warmup_seconds", 0.0))
        output_interval = float(np.median(np.diff(times)))

        initial_values = np.asarray(
            [
                float(
                    t.get(
                        "initial",
                        (float(t["min"]) + float(t["max"])) / 2.0,
                    )
                )
                for t in tuners
            ],
            dtype=float,
        )

        first_sample = {
            name: input_signal[name][0]
            for name in input_signal.dtype.names
            if name != "time"
        }
        print(
            f"Baseline replay: first FMU input sample={first_sample} "
            f"discrete_start_values={discrete_start_values}"
        )

        try:
            baseline_score = simulate_score(
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
            raise CalibrationError(
                "Baseline replay at nominal parameter values failed before "
                f"optimization began: {exc}"
            ) from exc
        print(f"Baseline replay succeeded: {metric}={baseline_score:.6g}")

        eval_count = 0
        best_seen = math.inf
        failures: list[dict[str, Any]] = []

        def objective(values: np.ndarray) -> float:
            nonlocal eval_count, best_seen
            eval_count += 1
            values = np.asarray(values, dtype=float)
            try:
                score = simulate_score(
                    fmu=args.fmu,
                    input_signal=input_signal,
                    times=times,
                    parameter_names=parameter_names,
                    parameter_values=values,
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
                # differential_evolution's objective can't raise (scipy
                # doesn't catch per-evaluation exceptions -- one would
                # abort the whole optimization). A tuner corner that makes
                # the FMU non-convergent (e.g. a stuck/pathologically slow
                # evaluation timing out, or CVode's mxstep exhausted) must
                # be steered away from, not allowed to crash the run --
                # same principle as Morris's per-trajectory drop and
                # HEBO's per-evaluation penalty.
                score = float(args.failure_penalty)
                failures.append({
                    "evaluation": eval_count,
                    "parameters": {
                        name: float(value)
                        for name, value in zip(parameter_names, values, strict=True)
                    },
                    "reason": str(exc),
                    "exception_type": type(exc).__name__,
                })
                print(f"  evaluations={eval_count}: FAILED -> penalty={score:.6g}", file=sys.stderr)
            best_seen = min(best_seen, score)
            if args.progress_every > 0 and eval_count % args.progress_every == 0:
                print(
                    f"  evaluations={eval_count} "
                    f"current_{metric}={score:.6g} "
                    f"best_{metric}={best_seen:.6g}"
                )
            return score

        print(
            f"Calibration: model={metadata.get('id', args.fmu.stem)} "
            f"optimizer={method} metric={metric} tuners={parameter_names}"
        )

        result = differential_evolution(
            objective,
            bounds=bounds,
            strategy=str(optimizer.get("strategy", "best1bin")),
            maxiter=max_iterations,
            popsize=population_size,
            tol=tolerance,
            seed=seed,
            polish=polish,
            workers=1,
            updating="immediate",
        )

        best_values = np.asarray(result.x, dtype=float)
        improvement = baseline_score - float(result.fun)
        improvement_pct = (
            100.0 * improvement / baseline_score if abs(baseline_score) > 1e-12 else None
        )

        parameter_results = []
        for tuner, initial, best in zip(tuners, initial_values, best_values, strict=True):
            parameter_results.append({
                "parameter": tuner["parameter"],
                "label": tuner.get("label", tuner["parameter"]),
                "unit": tuner.get("unit"),
                "initial": float(initial),
                "calibrated": float(best),
                "min": float(tuner["min"]),
                "max": float(tuner["max"]),
            })

        payload = {
            "model": metadata.get("id") or args.fmu.stem,
            "fmu": str(args.fmu),
            "metadata": str(args.metadata),
            "data": str(args.data),
            "sensitivity": str(args.sensitivity) if args.sensitivity else None,
            "goal": {
                "metadata_output": goal_csv_name,
                "fmu_variable": goal_fmu_name,
                "metric": metric,
            },
            "optimizer": {
                "method": method,
                "max_iterations": max_iterations,
                "population_size": population_size,
                "tolerance": tolerance,
                "seed": seed,
            },
            "result": {
                "success": bool(result.success),
                "message": str(result.message),
                "evaluations": int(result.nfev),
                "iterations": int(result.nit),
                "baseline_score": float(baseline_score),
                "best_score": float(result.fun),
                "improvement": float(improvement),
                "improvement_pct": improvement_pct,
                "parameters": parameter_results,
            },
            "failures": {
                "count": len(failures),
                "rate_pct": 100.0 * len(failures) / max(1, eval_count),
                "records": failures,
            },
        }

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        print(f"Baseline {metric}: {baseline_score:.6g}")
        print(f"Best     {metric}: {float(result.fun):.6g}")
        print(f"Failed evaluations: {len(failures)}/{eval_count}")
        for p in parameter_results:
            print(f"{p['parameter']}: {p['initial']:.6g} -> {p['calibrated']:.6g}")
        print(f"Wrote {args.output}")
        return 0

    except CalibrationError as exc:
        print(f"Calibration failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Calibration failed with unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
