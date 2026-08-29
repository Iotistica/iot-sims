#!/usr/bin/env python3
"""Validates that an FMU interface matches model.json and optionally
validates sensitivity/calibration configuration.

Generic across FMUs/model.json pairs -- takes both paths as CLI args so it can
be reused for VAV/chiller/zone/etc. workflows without modification.
Stdlib-only (zipfile + xml.etree) so it runs on a bare GitHub Actions runner
with no extra pip install step.

Interface checks:
  1. FMU inputs missing from model.json -- auto-added by --fix.
  2. FMU outputs missing from model.json -- auto-added by --fix.
  3. Metadata inputs whose fmu_variable no longer exists as an FMU input
     -- hard error, never auto-removed.
  4. Metadata outputs whose fmu_variable no longer exists as an FMU output
     -- hard error, never auto-removed.
  5. Causality mismatch -- warning only.

Calibration checks, enabled by --validate-calibration:
  - calibration.goal.output resolves to a metadata output and FMU output
  - every calibration tuner parameter exists as an FMU parameter
  - tuner bounds are numeric and min < max
  - tuner initial value is within [min, max]
  - metric/method vocabulary is supported
  - Morris trajectories and grid_levels are valid

Pass --fix to append metadata entries for newly exposed FMU inputs/outputs.
Existing stale entries and causality mismatches are never silently rewritten.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# FMI 1.0/2.0 wrap each variable in <ScalarVariable> with a typed child
# element (<Real unit="K"/>, <Integer/>, <Boolean/>, <String/>,
# <Enumeration/>). FMI 3.0 drops the wrapper -- the variable element itself
# is the typed element (<Float64 unit="K" .../>, <Int32/>, <Boolean/>, ...).
# Detecting per-element by tag (rather than trusting the file's declared
# fmiVersion attribute) lets one code path handle both without a version
# branch, though it doesn't attempt to cover FMI versions newer than 3.0.
_FMI2_TYPE_TAGS = {"Real", "Integer", "Boolean", "String", "Enumeration"}


class ValidationError(Exception):
    """Raised for structural problems (bad FMU/JSON) -- distinct from an
    interface-drift failure, which is reported and exits non-zero without
    raising."""


class FmuVariable:
    __slots__ = ("name", "causality", "variability", "type", "unit")

    def __init__(
        self,
        name: str,
        causality: str,
        var_type: str,
        unit: Optional[str],
        variability: Optional[str] = None,
    ):
        self.name = name
        self.causality = causality
        self.variability = variability
        self.type = var_type
        self.unit = unit


def load_model_description(fmu_path: Path) -> ET.Element:
    try:
        with zipfile.ZipFile(fmu_path) as zf:
            try:
                with zf.open("modelDescription.xml") as f:
                    return ET.parse(f).getroot()
            except KeyError:
                raise ValidationError(f"{fmu_path} does not contain modelDescription.xml")
    except zipfile.BadZipFile:
        raise ValidationError(f"{fmu_path} is not a valid FMU (not a zip archive)")


def extract_variables(root: ET.Element) -> dict[str, FmuVariable]:
    """Extract inputs, outputs, parameters, and other named FMU variables."""
    model_vars = root.find("ModelVariables")
    if model_vars is None:
        raise ValidationError("modelDescription.xml has no <ModelVariables> element")

    variables: dict[str, FmuVariable] = {}
    for el in model_vars:
        name = el.get("name")
        if not name:
            continue

        causality = el.get("causality") or "local"
        variability = el.get("variability")

        if el.tag == "ScalarVariable":
            type_el = next((c for c in el if c.tag in _FMI2_TYPE_TAGS), None)
            var_type = type_el.tag if type_el is not None else "Unknown"
            unit = type_el.get("unit") if type_el is not None else None
        else:
            var_type = el.tag
            unit = el.get("unit")

        variables[name] = FmuVariable(
            name=name,
            causality=causality,
            var_type=var_type,
            unit=unit,
            variability=variability,
        )

    return variables


def extract_io_variables(root: ET.Element) -> dict[str, FmuVariable]:
    """Return only FMU inputs and outputs."""
    return {
        name: var
        for name, var in extract_variables(root).items()
        if var.causality in ("input", "output")
    }


def load_metadata(metadata_path: Path) -> dict:
    try:
        return json.loads(metadata_path.read_text())
    except FileNotFoundError:
        raise ValidationError(f"{metadata_path} not found")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{metadata_path} is not valid JSON: {exc}")


def _format_variable(var: FmuVariable) -> str:
    return f"{var.name} [{var.type}, {var.unit or '-'}]"


def _snake_case(fmu_variable: str) -> str:
    """PSupFan -> p_sup_fan, VChiWat_flow -> v_chi_wat_flow. A syntactic
    placeholder only -- not a stand-in for an actual point name, which
    still needs a human to choose (see module docstring)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", fmu_variable).lower().replace("__", "_")


def synthesize_entry(var: FmuVariable) -> dict:
    return {
        "name": _snake_case(var.name),
        "label": var.name,
        "fmu_variable": var.name,
        "unit": var.unit,
        "suggested_point_types": [],
        "mapping_hints": {},
        "semantic": {},
    }


def _find_array_span(text: str, key: str) -> tuple[int, int]:
    """Locates the top-level `"<key>": [ ... ]` array in the raw file text
    and returns (index of '[', index of matching ']'). A hand-rolled,
    string-aware bracket counter -- a regex can't safely find the matching
    close bracket through nested arrays/objects, and re-parsing+re-dumping
    the whole file with json.dump would reformat every existing entry
    (Python's json module doesn't preserve a file's original formatting),
    turning a 1-entry addition into a full-file diff."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
    if not m:
        raise ValidationError(f'"{key}" array not found in {key}')
    open_idx = m.end() - 1
    depth = 0
    in_string = False
    escape = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return open_idx, i
    raise ValidationError(f'Unterminated "{key}" array in metadata file')


def _render_entry(entry: dict, item_indent: str = "    ") -> str:
    rendered = json.dumps(entry, indent=2)
    return "\n".join(item_indent + line if line else line for line in rendered.splitlines())


def _append_entries_text(text: str, key: str, entries: list[dict]) -> str:
    if not entries:
        return text
    open_idx, close_idx = _find_array_span(text, key)
    inner = text[open_idx + 1:close_idx].rstrip()
    rendered_items = ",\n".join(_render_entry(e) for e in entries)
    new_inner = (inner + ",\n" if inner else "\n") + rendered_items + "\n  "
    return text[:open_idx + 1] + new_inner + text[close_idx:]


def apply_fix(text: str, result: dict) -> tuple[str, dict[str, list[str]]]:
    """Appends a synthesized entry for every FMU variable missing from
    model.json, via a text splice (see _append_entries_text) rather than a
    full json.dump round-trip, so a small addition stays a small diff
    instead of reformatting the whole file. Never touches stale entries or
    causality mismatches -- those need a human, not this script."""
    added: dict[str, list[str]] = {"inputs": [], "outputs": []}

    if result["missing_inputs"]:
        entries = [synthesize_entry(v) for v in result["missing_inputs"]]
        text = _append_entries_text(text, "inputs", entries)
        added["inputs"] = [v.name for v in result["missing_inputs"]]

    if result["missing_outputs"]:
        entries = [synthesize_entry(v) for v in result["missing_outputs"]]
        text = _append_entries_text(text, "outputs", entries)
        added["outputs"] = [v.name for v in result["missing_outputs"]]

    return text, added


def validate(fmu_vars: dict[str, FmuVariable], metadata: dict) -> dict:
    fmu_inputs = {n: v for n, v in fmu_vars.items() if v.causality == "input"}
    fmu_outputs = {n: v for n, v in fmu_vars.items() if v.causality == "output"}

    meta_inputs = {
        entry["fmu_variable"]: entry for entry in metadata.get("inputs", []) if entry.get("fmu_variable")
    }
    meta_outputs = {
        entry["fmu_variable"]: entry for entry in metadata.get("outputs", []) if entry.get("fmu_variable")
    }
    meta_all_vars = set(meta_inputs) | set(meta_outputs)

    causality_mismatches: list[tuple[str, str, str]] = []
    stale_inputs: list[str] = []
    stale_outputs: list[str] = []

    for var_name in meta_inputs:
        if var_name in fmu_inputs:
            continue
        elif var_name in fmu_outputs:
            causality_mismatches.append((var_name, "input", "output"))
        else:
            stale_inputs.append(var_name)

    for var_name in meta_outputs:
        if var_name in fmu_outputs:
            continue
        elif var_name in fmu_inputs:
            causality_mismatches.append((var_name, "output", "input"))
        else:
            stale_outputs.append(var_name)

    # A variable already flagged above as a causality mismatch IS accounted
    # for in model.json (just under the wrong section) -- meta_all_vars
    # includes it regardless of which section it came from, so it's
    # correctly excluded from "missing from model.json" below rather than
    # being reported twice.
    missing_inputs = [v for name, v in fmu_inputs.items() if name not in meta_all_vars]
    missing_outputs = [v for name, v in fmu_outputs.items() if name not in meta_all_vars]

    return {
        "missing_inputs": sorted(missing_inputs, key=lambda v: v.name),
        "missing_outputs": sorted(missing_outputs, key=lambda v: v.name),
        "stale_inputs": sorted(stale_inputs),
        "stale_outputs": sorted(stale_outputs),
        "causality_mismatches": sorted(causality_mismatches, key=lambda t: t[0]),
        "fmu_input_count": len(fmu_inputs),
        "fmu_output_count": len(fmu_outputs),
    }



_SUPPORTED_METRICS = {"cv_rmse", "rmse", "mae", "nrmse"}
_SUPPORTED_SENSITIVITY_METHODS = {"morris"}
_SUPPORTED_SENSITIVITY_MEASURES = {"mu_star", "mu", "sigma"}
_SUPPORTED_OPTIMIZERS = {"differential_evolution", "hebo"}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_calibration(
    fmu_vars: dict[str, FmuVariable],
    metadata: dict,
) -> list[str]:
    """Validate optional model.json calibration/sensitivity configuration."""
    calibration = metadata.get("calibration")
    if calibration is None:
        return []
    if not isinstance(calibration, dict):
        return ["calibration must be an object"]

    errors: list[str] = []

    fmu_outputs = {
        name: var for name, var in fmu_vars.items() if var.causality == "output"
    }
    fmu_parameters = {
        name: var
        for name, var in fmu_vars.items()
        if var.causality in ("parameter", "calculatedParameter", "structuralParameter")
    }

    meta_outputs_by_name = {
        entry.get("name"): entry
        for entry in metadata.get("outputs", [])
        if entry.get("name")
    }
    meta_outputs_by_fmu = {
        entry.get("fmu_variable"): entry
        for entry in metadata.get("outputs", [])
        if entry.get("fmu_variable")
    }

    goal = calibration.get("goal")
    if not isinstance(goal, dict):
        errors.append("calibration.goal must be an object")
    else:
        goal_output = goal.get("output") or goal.get("variable")
        if not isinstance(goal_output, str) or not goal_output:
            errors.append("calibration.goal.output must be a non-empty string")
        elif goal_output in meta_outputs_by_name:
            fmu_name = meta_outputs_by_name[goal_output].get("fmu_variable")
            if not fmu_name:
                errors.append(f"calibration goal output {goal_output!r} has no fmu_variable")
            elif fmu_name not in fmu_outputs:
                errors.append(
                    f"calibration goal output {goal_output!r} maps to {fmu_name!r}, "
                    "which is not an FMU output"
                )
        elif goal_output in meta_outputs_by_fmu:
            if goal_output not in fmu_outputs:
                errors.append(f"calibration goal {goal_output!r} is not an FMU output")
        elif goal_output not in fmu_outputs:
            errors.append(
                f"calibration goal output {goal_output!r} was not found in "
                "model.json outputs or FMU outputs"
            )

        metric = goal.get("metric") or calibration.get("metric")
        if metric is not None and metric not in _SUPPORTED_METRICS:
            errors.append(
                f"unsupported calibration metric {metric!r}; supported: "
                + ", ".join(sorted(_SUPPORTED_METRICS))
            )

    tuners = calibration.get("tuners", [])
    if not isinstance(tuners, list):
        errors.append("calibration.tuners must be an array")
        tuners = []

    seen: set[str] = set()
    for i, tuner in enumerate(tuners):
        prefix = f"calibration.tuners[{i}]"
        if not isinstance(tuner, dict):
            errors.append(f"{prefix} must be an object")
            continue

        parameter = tuner.get("parameter")
        if not isinstance(parameter, str) or not parameter:
            errors.append(f"{prefix}.parameter must be a non-empty string")
            continue

        if parameter in seen:
            errors.append(f"duplicate calibration tuner parameter {parameter!r}")
        seen.add(parameter)

        if parameter not in fmu_parameters:
            if parameter in fmu_vars:
                errors.append(
                    f"calibration tuner {parameter!r} has FMU causality "
                    f"{fmu_vars[parameter].causality!r}, not parameter"
                )
            else:
                errors.append(
                    f"calibration tuner parameter {parameter!r} does not exist in the FMU"
                )

        minimum = tuner.get("min")
        maximum = tuner.get("max")
        initial = tuner.get("initial")

        if not _is_number(minimum):
            errors.append(f"{prefix}.min must be numeric")
        if not _is_number(maximum):
            errors.append(f"{prefix}.max must be numeric")
        if _is_number(minimum) and _is_number(maximum) and minimum >= maximum:
            errors.append(f"{prefix} requires min < max")

        if initial is not None:
            if not _is_number(initial):
                errors.append(f"{prefix}.initial must be numeric")
            elif _is_number(minimum) and _is_number(maximum):
                if initial < minimum or initial > maximum:
                    errors.append(
                        f"{prefix}.initial={initial} must be within [{minimum}, {maximum}]"
                    )

    sensitivity = calibration.get("sensitivity")
    if sensitivity is not None:
        if not isinstance(sensitivity, dict):
            errors.append("calibration.sensitivity must be an object")
        else:
            method = sensitivity.get("method")
            if method not in _SUPPORTED_SENSITIVITY_METHODS:
                errors.append(
                    f"unsupported sensitivity method {method!r}; supported: "
                    + ", ".join(sorted(_SUPPORTED_SENSITIVITY_METHODS))
                )

            trajectories = sensitivity.get("trajectories")
            if (
                not isinstance(trajectories, int)
                or isinstance(trajectories, bool)
                or trajectories <= 0
            ):
                errors.append(
                    "calibration.sensitivity.trajectories must be a positive integer"
                )

            grid_levels = sensitivity.get("grid_levels")
            if (
                not isinstance(grid_levels, int)
                or isinstance(grid_levels, bool)
                or grid_levels < 2
            ):
                errors.append(
                    "calibration.sensitivity.grid_levels must be an integer >= 2"
                )

            measure = sensitivity.get("measure")
            if (
                measure is not None
                and measure not in _SUPPORTED_SENSITIVITY_MEASURES
            ):
                errors.append(
                    f"unsupported sensitivity measure {measure!r}; supported: "
                    + ", ".join(sorted(_SUPPORTED_SENSITIVITY_MEASURES))
                )

    optimizer = calibration.get("optimizer")
    if optimizer is not None:
        if not isinstance(optimizer, dict):
            errors.append("calibration.optimizer must be an object")
        else:
            method = optimizer.get("method")
            if method is not None and method not in _SUPPORTED_OPTIMIZERS:
                errors.append(
                    f"unsupported optimizer method {method!r}; supported: "
                    + ", ".join(sorted(_SUPPORTED_OPTIMIZERS))
                )
            metric = optimizer.get("metric")
            if metric is not None and metric not in _SUPPORTED_METRICS:
                errors.append(
                    f"unsupported optimizer metric {metric!r}; supported: "
                    + ", ".join(sorted(_SUPPORTED_METRICS))
                )

    return errors


def report_calibration(model_name: str, errors: list[str]) -> bool:
    if not errors:
        print(f"Calibration metadata valid: {model_name}")
        return True

    print(f"Calibration metadata validation failed: {model_name}\n")
    for error in errors:
        print(f"  - {error}")
    print()
    return False


def report(model_name: str, result: dict) -> bool:
    """Prints the pass/fail report. Returns True if valid.

    Causality mismatches are printed whenever present but never affect the
    return value -- they're a warning, not a failure (see module docstring
    rule #5)."""
    has_errors = any(
        result[key] for key in ("missing_inputs", "missing_outputs", "stale_inputs", "stale_outputs")
    )

    if result["causality_mismatches"]:
        print("Causality mismatches (warning -- not blocking, review when convenient):")
        for name, meta_causality, fmu_causality in result["causality_mismatches"]:
            print(f"  - {name}: metadata={meta_causality}, FMU={fmu_causality}")
        print()

    if not has_errors:
        print(f"FMU metadata valid: {result['fmu_input_count']} inputs, {result['fmu_output_count']} outputs")
        return True

    print(f"FMU metadata validation failed: {model_name}\n")

    if result["missing_inputs"]:
        print("FMU inputs missing from model.json:")
        for var in result["missing_inputs"]:
            print(f"  + {_format_variable(var)}")
        print()

    if result["missing_outputs"]:
        print("FMU outputs missing from model.json:")
        for var in result["missing_outputs"]:
            print(f"  + {_format_variable(var)}")
        print()

    if result["stale_inputs"]:
        print("Stale metadata inputs:")
        for name in result["stale_inputs"]:
            print(f"  - {name}")
        print()

    if result["stale_outputs"]:
        print("Stale metadata outputs:")
        for name in result["stale_outputs"]:
            print(f"  - {name}")
        print()

    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fmu", required=True, type=Path, help="Path to the .fmu file")
    parser.add_argument("--metadata", required=True, type=Path, help="Path to the model.json file")
    parser.add_argument(
        "--fix", action="store_true",
        help="Append entries for FMU variables missing from model.json, then re-validate.",
    )
    parser.add_argument(
        "--validate-calibration",
        action="store_true",
        help="Validate model.json calibration/sensitivity configuration against FMU parameters/outputs.",
    )
    args = parser.parse_args()

    try:
        root = load_model_description(args.fmu)
        all_fmu_vars = extract_variables(root)
        fmu_vars = {
            name: var
            for name, var in all_fmu_vars.items()
            if var.causality in ("input", "output")
        }
        metadata = load_metadata(args.metadata)
    except ValidationError as exc:
        print(f"FMU metadata validation failed: {exc}", file=sys.stderr)
        return 1

    model_name = metadata.get("id") or metadata.get("label") or args.metadata.stem
    result = validate(fmu_vars, metadata)

    if args.fix and (result["missing_inputs"] or result["missing_outputs"]):
        original_text = args.metadata.read_text()
        new_text, added = apply_fix(original_text, result)
        args.metadata.write_text(new_text)

        print(f"Updated {args.metadata}:")
        for name in added["inputs"]:
            print(f"  + input  {name} (semantic/mapping_hints left empty)")
        for name in added["outputs"]:
            print(f"  + output {name} (semantic/mapping_hints left empty)")
        print()

        # Re-read from disk and re-validate. #1/#2 should now be gone; #3/#4
        # stale entries and #5 causality mismatches were never touched by
        # --fix and are handled by report() as before (hard error / warning).
        try:
            metadata = load_metadata(args.metadata)
        except ValidationError as exc:
            print(f"FMU metadata validation failed: --fix produced invalid JSON: {exc}", file=sys.stderr)
            return 1
        result = validate(fmu_vars, metadata)

    valid = report(model_name, result)

    calibration_valid = True
    if args.validate_calibration:
        calibration_errors = validate_calibration(all_fmu_vars, metadata)
        calibration_valid = report_calibration(model_name, calibration_errors)

    return 0 if valid and calibration_valid else 1


if __name__ == "__main__":
    sys.exit(main())
