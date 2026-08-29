#!/usr/bin/env python3
"""HEBO calibration runner for Modelica FMUs using model.json + historical CSV.

Separate research-derived calibration flow inspired by Martinez-Viol et al.
(Building and Environment 226 (2022) 109693). It does not require Morris.

Dependencies:
    python -m pip install numpy pandas fmpy "HEBO" "pymoo<0.6"

HEBO 0.3.2 (the current PyPI release) is only compatible with a narrower
dependency range than a plain `pip install HEBO` resolves on a modern
Python/numpy/pandas stack -- see the compatibility shims immediately
below for exactly what breaks and why. `pymoo<0.6` is a hard requirement
(HEBO imports `pymoo.factory`, removed in pymoo 0.6); the shims handle
everything that surfaces only at runtime.

Example:
    python shared/run_hebo_calibration.py \
      --fmu models/rtu/RTU.fmu \
      --metadata models/rtu/model.json \
      --data models/rtu/calibration/rtu1_history.csv \
      --output models/rtu/calibration/rtu1_hebo_calibration.json \
      --iterations 100
"""
from __future__ import annotations

import argparse, collections, collections.abc, csv, io, json, math, sys, traceback
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fmpy import read_model_description, simulate_fmu

# --- HEBO/pymoo/cma compatibility shims -------------------------------
# HEBO 0.3.2's own code (and its pymoo/cma dependencies, at the versions
# actually installable today) predates several breaking changes in the
# modern Python/numpy/pandas ecosystem. None of these are fixable in this
# script's own logic -- they're failures *inside* those libraries -- so
# each shim restores just enough of the old API surface for HEBO's actual
# call pattern to work, applied before importing hebo/pymoo/cma at all.
#
# 1. cma (pulled in transitively by pymoo's CMA-ES algorithm, which
#    pymoo's algorithm registry imports unconditionally even when a
#    different algorithm like nsga2 is selected) does
#    `from collections import MutableMapping` -- an alias removed in
#    Python 3.10 (moved to collections.abc back in 3.3). Without this,
#    cma silently falls back to a numpy-free "purecma"-only mode via its
#    own bare `except ImportError`, and any later pymoo call that
#    triggers the algorithm registry fails.
if not hasattr(collections, "MutableMapping"):
    collections.MutableMapping = collections.abc.MutableMapping

# 2. pymoo 0.5.0 (the newest release compatible with HEBO's
#    `pymoo.factory` import -- see the module docstring) uses
#    np.row_stack, removed in numpy 2.0. fmpy requires numpy>=2.1.3, so
#    pinning numpy<2.0 instead is not viable for this script.
if not hasattr(np, "row_stack"):
    np.row_stack = np.vstack

# 2b. Same pymoo 0.5.0, same reasoning: its mixed-variable crossover
#     operator (operators/mixed_variable_operator.py) does
#     `np.full(..., dtype=np.object)` -- the deprecated alias for the
#     builtin `object`, removed entirely in numpy 1.24 (fmpy's
#     numpy>=2.1.3 requirement is nowhere near that old). Only surfaces
#     once the evolution optimizer's crossover step actually runs, not
#     at import time or even on the first few suggest() calls -- it hit
#     after 9 successful iterations in one real run.
if not hasattr(np, "object"):
    np.object = object

def _dataframe_append_shim(self, other, ignore_index=False, **kwargs):
    other_df = other if isinstance(other, pd.DataFrame) else pd.DataFrame([other])
    return pd.concat([self, other_df], ignore_index=ignore_index)


# 3. HEBO.observe() itself calls self.X.append(...), DataFrame.append()
#    having been removed in pandas 2.0.
if not hasattr(pd.DataFrame, "append"):
    pd.DataFrame.append = _dataframe_append_shim

from hebo.design_space.design_space import DesignSpace
from hebo.optimizers.hebo import HEBO


class CalibrationError(RuntimeError):
    pass


# See run_sensitivity._raise_cvode_max_steps -- same mxstep workaround,
# kept here since this file doesn't import that one (or run_calibration's
# copy of it). Harmless/inert if a given FMU ends up running under
# CoSimulation instead of ModelExchange, since CoSimulation never touches
# CVodeSolver at all.
def _raise_cvode_max_steps(limit: int) -> None:
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


_raise_cvode_max_steps(5000)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CalibrationError(f"Could not read {path}: {exc}") from exc


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            if not r.fieldnames:
                raise CalibrationError(f"{path} has no header")
            rows = list(r)
            if not rows:
                raise CalibrationError(f"{path} has no data rows")
            return list(r.fieldnames), rows
    except FileNotFoundError as exc:
        raise CalibrationError(f"{path} not found") from exc


def parse_time(v: str) -> float | datetime:
    try:
        return float(v.strip())
    except ValueError:
        try:
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CalibrationError(f"Invalid timestamp {v!r}") from exc


def build_times(rows: list[dict[str, str]], column: str) -> np.ndarray:
    vals = [parse_time(r[column]) for r in rows]
    if isinstance(vals[0], datetime):
        if not all(isinstance(v, datetime) for v in vals):
            raise CalibrationError("timestamp column mixes numeric and datetime values")
        t0 = vals[0]
        out = np.array([(v - t0).total_seconds() for v in vals], float)
    else:
        if not all(isinstance(v, float) for v in vals):
            raise CalibrationError("timestamp column mixes numeric and datetime values")
        raw = np.asarray(vals, float)
        out = raw - raw[0]
    if len(out) < 2 or np.any(np.diff(out) <= 0):
        raise CalibrationError("timestamps must be strictly increasing")
    return out


def convert(values: np.ndarray, name: str | None) -> np.ndarray:
    if not name: return values
    if name == "c_to_k": return values + 273.15
    if name == "k_to_c": return values - 273.15
    if name == "pct_to_fraction": return values / 100.0
    if name == "fraction_to_pct": return values * 100.0
    if name == "w_to_kw": return values / 1000.0
    if name == "kw_to_w": return values * 1000.0
    if name == "cfm_to_m3_s": return values * 0.00047194745
    if name == "m3_s_to_cfm": return values / 0.00047194745
    raise CalibrationError(f"Unsupported conversion {name!r}")


def fmi_input_variables(fmu: Path) -> dict[str, dict[str, str]]:
    """Maps each FMU input's name to its declared FMI type and variability,
    read from the FMU's own modelDescription.xml. variability distinguishes
    inputs FMPy's ModelExchange/CVode solver treats as continuous
    time-varying signals (safe to replay through the structured `input=`
    array) from ones it treats as discrete/tunable -- see prepare_inputs
    for why those can only be applied via start_values."""
    md = read_model_description(str(fmu), validate=False)
    result = {}
    for v in md.modelVariables:
        if getattr(v, "causality", None) == "input":
            result[v.name] = {
                "type": str(getattr(v, "type", type(v).__name__)),
                "variability": str(getattr(v, "variability", "continuous")),
            }
    return result


def resolve_fmi_execution(
    fmu: Path, requested_type: str | None, requested_solver: str | None
) -> tuple[str | None, str | None]:
    """Prefers ModelExchange+CVode when the FMU supports Model Exchange and
    the caller didn't explicitly choose otherwise. FMPy defaults to
    CoSimulation's own black-box fixed-step solver when fmi_type is left
    unset, which is far less numerically controllable than ModelExchange's
    adaptive CVode integration -- the concrete failure this generically
    avoids: under CoSimulation, RTU.fmu's mixing-volume temperature ran
    past its physical bound before the solver could react to a stiff
    transient (see run_sensitivity.run_one). Falls back to whatever FMPy
    would choose by default for an FMU that doesn't export Model Exchange
    at all -- this is a general robustness default, not RTU-specific."""
    if requested_type is not None:
        return requested_type, requested_solver
    md = read_model_description(str(fmu), validate=False)
    if md.modelExchange is not None:
        return "ModelExchange", requested_solver or "CVode"
    return None, requested_solver


def np_dtype(fmi_type: str):
    t = fmi_type.lower()
    if "boolean" in t: return np.bool_
    if "integer" in t or "enumeration" in t: return np.int32
    return np.float64


def parse_series(rows, csv_name: str, default: Any, fmi_type: str) -> np.ndarray:
    dtype = np_dtype(fmi_type)
    out = []
    for line, row in enumerate(rows, start=2):
        raw = row.get(csv_name)
        value = default if raw is None or raw.strip() == "" else raw.strip()
        if value is None:
            raise CalibrationError(f"Missing {csv_name!r} on CSV row {line} and no default exists")
        try:
            if dtype is np.bool_:
                if isinstance(value, bool): parsed = value
                else:
                    s = str(value).strip().lower()
                    if s in {"1","true","on","yes","active"}: parsed = True
                    elif s in {"0","false","off","no","inactive"}: parsed = False
                    else: raise ValueError
                out.append(parsed)
            elif dtype is np.int32:
                out.append(int(float(value)))
            else:
                x = float(value)
                if not math.isfinite(x): raise ValueError
                out.append(x)
        except Exception as exc:
            raise CalibrationError(f"Invalid {csv_name!r} on CSV row {line}: {value!r}") from exc
    return np.asarray(out, dtype=dtype)


def prepare_inputs(
    fmu: Path, metadata: dict, rows, times: np.ndarray
) -> tuple[np.ndarray, dict[str, float]]:
    """Returns (signal, discrete_start_values):
      - signal is the structured time-varying array for FMPy's `input=`,
        covering every continuous input.
      - discrete_start_values holds the resolved constant value for any
        input the FMU declares discrete/tunable variability (e.g. RTU's
        Boolean uOutDamOvrEna). FMPy's ModelExchange/CVode integrator
        applies signal-array inputs from inside the solver's event-
        indicator/right-hand-side callbacks on every evaluation, which is
        only valid for genuinely continuous signals -- replaying a
        discrete input the same way corrupts the FMU's internal event-
        handling state machine (fmi2SetBoolean/fmi2SetContinuousStates
        failing with status 3). A discrete-variability input can only be
        set once at initialization via start_values, so its CSV/default-
        derived value must be constant across the whole historical
        period; a genuinely time-varying discrete input raises rather
        than silently dropping the variation."""
    variables = fmi_input_variables(fmu)
    dtype = [("time", np.float64)]
    series = {}
    discrete_start_values: dict[str, float] = {}
    for entry in metadata.get("inputs") or []:
        csv_name, fmu_name = entry.get("name"), entry.get("fmu_variable")
        if not csv_name or not fmu_name:
            raise CalibrationError("Every model input needs name and fmu_variable")
        info = variables.get(fmu_name, {})
        fmi_type = info.get("type", "Real")
        vals = parse_series(rows, csv_name, entry.get("default"), fmi_type)
        if np_dtype(fmi_type) is np.float64:
            vals = convert(vals.astype(float), entry.get("conversion"))

        variability = info.get("variability", "continuous")
        if variability in ("discrete", "tunable"):
            numeric = vals.astype(float)
            unique = np.unique(numeric)
            if unique.size > 1:
                raise CalibrationError(
                    f"{fmu_name!r} is a {variability} FMI input, so it can "
                    "only be replayed as a constant start value -- but "
                    f"{csv_name!r} varies over the historical period "
                    f"({unique.tolist()!r})"
                )
            discrete_start_values[fmu_name] = float(unique[0]) if unique.size else 0.0
            continue

        series[fmu_name] = vals
        dtype.append((fmu_name, np_dtype(fmi_type)))
    signal = np.zeros(len(times), dtype=dtype)
    signal["time"] = times
    for name, vals in series.items(): signal[name] = vals
    return signal, discrete_start_values


def resolve_goal(metadata: dict) -> tuple[str, str, str | None, str]:
    cal = metadata.get("calibration") or {}
    goal = cal.get("goal") or {}
    configured = goal.get("output") or goal.get("variable")
    metric = goal.get("metric") or cal.get("metric") or "cv_rmse"
    for out in metadata.get("outputs") or []:
        if configured in (out.get("name"), out.get("fmu_variable")):
            return out["name"], out["fmu_variable"], out.get("conversion"), metric
    raise CalibrationError(f"Calibration goal {configured!r} does not resolve to an output")


def measured_column(rows, name: str) -> np.ndarray:
    vals = []
    for line, row in enumerate(rows, start=2):
        raw = row.get(name)
        if raw is None or raw.strip() == "":
            raise CalibrationError(f"Missing measured {name!r} on row {line}")
        vals.append(float(raw))
    return np.asarray(vals, float)


def metric(name: str, measured: np.ndarray, predicted: np.ndarray) -> float:
    mask = np.isfinite(measured) & np.isfinite(predicted)
    y, p = measured[mask], predicted[mask]
    if not len(y): raise CalibrationError("No finite samples to score")
    e = p - y
    rmse = float(np.sqrt(np.mean(e*e)))
    if name == "rmse": return rmse
    if name == "mae": return float(np.mean(np.abs(e)))
    if name == "cv_rmse":
        d = abs(float(np.mean(y)))
        if d <= 1e-12: raise CalibrationError("CV(RMSE) undefined for zero mean")
        return 100.0 * rmse / d
    if name == "nmbe":
        d = abs(float(np.mean(y)))
        if d <= 1e-12: raise CalibrationError("NMBE undefined for zero mean")
        return 100.0 * float(np.mean(y-p)) / d
    if name == "r2":
        ssr = float(np.sum((y-p)**2)); sst = float(np.sum((y-np.mean(y))**2))
        return 1.0 - ssr/sst if sst > 1e-12 else float("nan")
    raise CalibrationError(f"Unsupported metric {name!r}")


def metric_pack(y, p):
    out = {}
    for name in ("cv_rmse","mae","nmbe","r2","rmse"):
        try:
            v = metric(name, y, p); out[name] = v if math.isfinite(v) else None
        except CalibrationError: out[name] = None
    return out


def build_space(metadata: dict) -> tuple[DesignSpace, list[dict]]:
    tuners = (metadata.get("calibration") or {}).get("tuners") or []
    if not tuners: raise CalibrationError("calibration.tuners is empty")
    defs = []
    for t in tuners:
        lo, hi = float(t["min"]), float(t["max"])
        if lo >= hi: raise CalibrationError(f"Invalid bounds for {t['parameter']}")
        defs.append({"name": str(t["parameter"]), "type": "num", "lb": lo, "ub": hi})
    return DesignSpace().parse(defs), tuners


def default_parameters(tuners: list[dict]) -> dict[str, float]:
    out = {}
    for t in tuners:
        initial = t.get("initial")
        if initial is None: initial = (float(t["min"]) + float(t["max"])) / 2.0
        out[str(t["parameter"])] = float(initial)
    return out


def run_candidate(*, fmu, signal, times, params, discrete_start_values, goal_fmu, goal_conv,
                  measured, warmup, interval, fmi_type, solver, timeout_seconds=180.0):
    start_values = dict(params)
    if discrete_start_values:
        start_values.update(discrete_start_values)
    kw = dict(filename=str(fmu), start_time=0.0, stop_time=float(times[-1]),
              input=signal, output=[goal_fmu], output_interval=interval,
              start_values=start_values, validate=False,
              # See run_sensitivity.run_one -- same wall-clock timeout with
              # explicit truncation check below. FMPy's timeout doesn't
              # raise on expiry, it just returns whatever was recorded so
              # far -- the check after this call is what actually turns a
              # stuck evaluation into a clean, catchable failure that the
              # existing failure-penalty/recording logic already handles.
              timeout=timeout_seconds)
    if fmi_type: kw["fmi_type"] = fmi_type
    if solver: kw["solver"] = solver
    if fmi_type == "ModelExchange" and solver == "CVode":
        # See run_sensitivity.run_one -- same tolerance loosening. Avoids
        # CVode hitting SUNDIALS' internal step cap (mxstep, raised above
        # but still finite) on a locally stiff patch before reaching the
        # next output_interval.
        kw["relative_tolerance"] = 1e-4

    # FMPy's Input.__init__ prints (via bare print(), not a logger hook)
    # 'Warning: missing input for variable "X"' for every discrete_start_values
    # input, once per evaluation across a whole HEBO run -- drowning out
    # real progress output. Capture and filter just that line; forward
    # anything else through so a genuinely new FMPy message isn't lost.
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        result = simulate_fmu(**kw)
    for line in captured_stdout.getvalue().splitlines():
        if line.strip().startswith('Warning: missing input for variable "'):
            continue
        print(line)

    rt = np.asarray(result["time"], float)
    stop_time = float(times[-1])
    if rt.size == 0 or rt[-1] < stop_time - max(interval, 1.0):
        raise CalibrationError(
            f"Simulation stopped at t={rt[-1] if rt.size else 0.0:.6g} instead "
            f"of reaching stop_time={stop_time:.6g} -- likely a timeout "
            f"(timeout_seconds={timeout_seconds}) on a stuck/pathologically "
            "slow evaluation"
        )
    pred = convert(np.asarray(result[goal_fmu], float), goal_conv)
    pred = np.interp(times, rt, pred)
    mask = times >= warmup
    if not np.any(mask): raise CalibrationError("Warmup removes all samples")
    return measured[mask], pred[mask]


def main() -> int:
    # See run_sensitivity.main -- same line-buffering fix. Without it,
    # every print() below sits invisible in stdout's block buffer on a
    # non-terminal run (e.g. GitHub Actions) while SUNDIALS' native CVode
    # error lines (written directly to the OS stderr file descriptor from
    # C) show up immediately, making a normally-progressing run look
    # silent/stuck.
    sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fmu", required=True, type=Path)
    ap.add_argument("--metadata", required=True, type=Path)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--timestamp-column", default="timestamp")
    ap.add_argument("--iterations", type=int, default=100)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--failure-penalty", type=float, default=1e6)
    ap.add_argument("--fmi-type", choices=["ModelExchange","CoSimulation"], default=None)
    ap.add_argument("--solver", default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help=(
            "Per-evaluation wall-clock timeout in seconds (default: 180). "
            "Bounds how long one pathologically slow/stuck FMU evaluation "
            "can block the whole HEBO run before it's penalized like any "
            "other failed simulation."
        ),
    )
    args = ap.parse_args()
    np.random.seed(args.seed)

    try:
        meta = load_json(args.metadata)
        headers, rows = load_csv(args.data)
        if args.timestamp_column not in headers:
            raise CalibrationError(f"CSV missing {args.timestamp_column!r}")
        times = build_times(rows, args.timestamp_column)
        signal, discrete_start_values = prepare_inputs(args.fmu, meta, rows, times)
        goal_csv, goal_fmu, goal_conv, objective_metric = resolve_goal(meta)
        if goal_csv not in headers: raise CalibrationError(f"CSV missing goal {goal_csv!r}")
        measured = measured_column(rows, goal_csv)
        interval = float(np.median(np.diff(times)))
        warmup = float(meta.get("warmup_seconds", 0.0))
        space, tuners = build_space(meta)
        baseline_params = default_parameters(tuners)
        fmi_type, solver = resolve_fmi_execution(args.fmu, args.fmi_type, args.solver)

        print(f"HEBO calibration: tuners={[t['parameter'] for t in tuners]} iterations={args.iterations} metric={objective_metric}")
        print(f"  fmi_type={fmi_type or 'auto'} solver={solver or 'auto'} discrete_start_values={discrete_start_values}")

        baseline = None
        if not args.skip_baseline:
            print("Running baseline replay...")
            try:
                y, p = run_candidate(fmu=args.fmu, signal=signal, times=times,
                    params=baseline_params, discrete_start_values=discrete_start_values,
                    goal_fmu=goal_fmu, goal_conv=goal_conv,
                    measured=measured, warmup=warmup, interval=interval,
                    fmi_type=fmi_type, solver=solver, timeout_seconds=args.timeout)
                baseline = {"parameters": baseline_params,
                            "objective": metric(objective_metric, y, p),
                            "metrics": metric_pack(y,p), "status": "ok"}
                print(f"  baseline {objective_metric}={baseline['objective']:.6g}")
            except Exception as exc:
                raise CalibrationError(f"Baseline replay failed before HEBO: {exc}") from exc

        opt = HEBO(space)
        history, failures = [], []
        best_obj, best_params, best_metrics = math.inf, None, None
        completed = 0
        while completed < args.iterations:
            n = min(args.batch_size, args.iterations-completed)
            suggestions = opt.suggest(n_suggestions=n)
            ys = []
            for j in range(len(suggestions)):
                one = suggestions.iloc[[j]].reset_index(drop=True)
                params = {str(c): float(one.iloc[0][c]) for c in one.columns}
                idx = completed + j + 1
                try:
                    y, p = run_candidate(fmu=args.fmu, signal=signal, times=times,
                        params=params, discrete_start_values=discrete_start_values,
                        goal_fmu=goal_fmu, goal_conv=goal_conv,
                        measured=measured, warmup=warmup, interval=interval,
                        fmi_type=fmi_type, solver=solver, timeout_seconds=args.timeout)
                    obj = metric(objective_metric, y, p)
                    metrics = metric_pack(y,p); status = "ok"
                    if obj < best_obj:
                        best_obj, best_params, best_metrics = obj, dict(params), dict(metrics)
                    print(f"  {idx}/{args.iterations}: {objective_metric}={obj:.6g} best={best_obj:.6g}")
                except Exception as exc:
                    obj = float(args.failure_penalty); metrics = None; status = "failed_simulation"
                    failures.append({"evaluation": idx, "parameters": params,
                                     "reason": str(exc), "exception_type": type(exc).__name__})
                    print(f"  {idx}/{args.iterations}: FAILED -> penalty={obj:.6g}", file=sys.stderr)
                history.append({"evaluation": idx, "status": status, "objective": obj,
                                "parameters": params, "metrics": metrics})
                ys.append([obj])
            opt.observe(suggestions, np.asarray(ys, float))
            completed += len(suggestions)

        if best_params is None: raise CalibrationError("Every HEBO simulation failed")
        improvement = improvement_pct = None
        if baseline:
            improvement = float(baseline["objective"]) - best_obj
            if abs(float(baseline["objective"])) > 1e-12:
                improvement_pct = 100.0 * improvement / float(baseline["objective"])

        payload = {
            "methodology": {"name": "HEBO Modelica calibration",
                "inspired_by": "Martinez-Viol et al., Building and Environment 226 (2022) 109693"},
            "model": meta.get("id") or args.fmu.stem,
            "fmu": str(args.fmu), "metadata": str(args.metadata), "data": str(args.data),
            "goal": {"metadata_output": goal_csv, "fmu_variable": goal_fmu, "metric": objective_metric},
            "historical_period": {"samples": len(times), "duration_seconds": float(times[-1]),
                "warmup_seconds_excluded_from_metric": warmup, "output_interval_seconds": interval},
            "optimizer": {"method": "hebo", "iterations_requested": args.iterations,
                "evaluations_completed": completed, "batch_size": args.batch_size,
                "seed": args.seed, "failure_penalty": args.failure_penalty,
                "fmi_type": fmi_type or "auto", "solver": solver or "auto"},
            "tuner_bounds": [{k:t.get(k) for k in ("parameter","label","unit","initial","min","max")} for t in tuners],
            "baseline": baseline,
            "best": {"objective": best_obj, "parameters": best_params, "metrics": best_metrics,
                "improvement": improvement, "improvement_pct": improvement_pct},
            "failures": {"count": len(failures), "rate_pct": 100.0*len(failures)/max(1,completed), "records": failures},
            "history": history,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nBest {objective_metric}: {best_obj:.6g}")
        print(f"Failed simulations: {len(failures)}/{completed}")
        for k,v in best_params.items(): print(f"  {k} = {v:.10g}")
        print(f"Wrote {args.output}")
        return 0
    except CalibrationError as exc:
        print(f"HEBO calibration failed: {exc}", file=sys.stderr); return 1
    except Exception as exc:
        print(f"HEBO calibration failed with unexpected error: {exc}", file=sys.stderr)
        traceback.print_exc(); return 1


if __name__ == "__main__":
    sys.exit(main())
