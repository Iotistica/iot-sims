"""Builds the wide-format, multi-trajectory CSV iot-models' training-job API
expects (shared/train_baseline.py's build_supervised_pairs: trajectory_id,
step_index, sim_time_s, input_*/output_* columns) directly from one or more
Recordings' own stored samples. Sibling to calibration_export.py, same "no
unit conversion, write recorded values as-is" precedent -- the mapping step
is where a user judges unit fit (surfaced side-by-side by
training_export.py's own mapping-suggestions route, not decided here).

One recording = one trajectory_id (the recording's own id, which is already
a stable, unique integer -- no reassignment needed). step_index is the
recording's own sample_index (already a per-recording monotonic 0-based
counter -- see database.py's add_replay_sample). sim_time_s is
sample_index * recording.sample_interval_seconds, deliberately NOT derived
from the real `timestamp` column, which can jitter (external BACnet devices
are wall-clock-polled over the network) or coalesce (simulated devices
degrade to once-per-tick if sample_interval_seconds is finer than the tick
rate -- see runtime.py's own comment on this). An evenly-spaced synthetic
time axis matches the FMU generator's own `current_time += time_step`
convention, which matters for training's history-window lag features to
mean the same thing across both data sources.

Recording-level `tags` (see migration 026) are descriptive metadata only --
independent scenario labels like "hot-day"/"fan-test", never written into
this CSV or fed into training as a feature. (An earlier version of this
module turned a recording's single `category` string into a numeric
`input_category` column; that concept doesn't carry over to a list of
independent tags and was retired, not adapted -- whether/how tags become an
ML feature is a separate future decision.)
"""
from __future__ import annotations

import csv
import io
from typing import Any

from ..semantics import ml_units


def _expected_unit_for(variable_name: str, expected_units: dict[str, str | None]) -> str | None:
    """`expected_units` is keyed by the bare model-variable name; the CSV column /
    `mapping` key is direction-prefixed (`input_`/`output_`). Look up either form."""
    if variable_name in expected_units:
        return expected_units[variable_name]
    for prefix in ("input_", "output_"):
        if variable_name.startswith(prefix):
            return expected_units.get(variable_name[len(prefix):])
    return None


def _normalize_row_value(
    value: Any, variable_name: str, source_unit: str | None, expected_units: dict[str, str | None]
) -> Any:
    """Convert one recorded value from its point's engineering unit into the model's
    expected catalog unit. A non-finite value is written through untouched (the row-drop
    logic elsewhere already handles gaps); an incompatible / unknown-source unit raises
    `ml_units.UnitError` -- surfaced by the training-export route as a 400, never a silent
    bad number."""
    expected = _expected_unit_for(variable_name, expected_units)
    if expected is None:
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return value
    return ml_units.normalize(numeric, source_unit, expected)


def _units_by_recording_point_id(recording: dict[str, Any]) -> dict[int, str | None]:
    return {p["id"]: p.get("units") for p in recording["points"]}


def _point_id_by_source_object_id(recording: dict[str, Any]) -> dict[int, int]:
    """objects.id (source_object_id, stable across every recording of the
    same device) -> this recording's own recording_points.id
    (recording_point_id, what recording_samples rows are actually keyed by).
    A point whose source object was since deleted has source_object_id=NULL
    and simply can't be resolved this way -- see build_training_dataset's
    own handling."""
    return {
        p["source_object_id"]: p["id"]
        for p in recording["points"]
        if p["source_object_id"] is not None
    }


def build_training_dataset(
    database: Any,
    recording_ids: list[int],
    mapping: dict[str, int],
    constant_mapping: dict[str, dict[int, float]] | None = None,
    expected_units: dict[str, str | None] | None = None,
) -> bytes:
    """`mapping` is {variable_name: source_object_id} -- one object id per
    variable, shared conceptually across every given recording (the same
    physical/simulated BACnet object keeps the same objects.id across
    however many times it's been recorded). Resolved independently per
    recording below into that recording's own recording_point_id, since
    recording_point_id itself is recording-specific.

    `constant_mapping` is {variable_name: {recording_id: value}} -- for a
    constant-sourced input (see migration 024 / recording_input_
    constants), there is no point at all to resolve, only the literal value
    THAT recording snapshotted at record time. Deliberately per-recording,
    not one shared value: two recordings of the same device can carry
    different constant values if the device's config changed between them,
    and using each recording's own captured value (rather than today's live
    config) is the whole point of snapshotting it in the first place -- see
    recordings.py's _constant_sourced_inputs. A recording with no
    snapshot for a given variable (predates migration 024, or the variable
    wasn't constant-sourced at record time) is handled exactly like a
    missing point below: that recording contributes no rows for a dataset
    that needs this variable, it isn't asked to fall back to a current value
    that may not represent what it actually captured.

    A recording missing a mapped point for some variable (e.g. that
    variable was added to the mapping after this recording ran, or its
    source object didn't exist yet) doesn't fail the whole export -- that
    recording's own rows are simply dropped, mirroring
    build_calibration_dataset's existing "drop the whole row rather than
    write a gap" precedent, just applied per-recording instead of
    per-sample. The training_export API's mapping-suggestions route is
    where this should be caught and surfaced to a human *before* it gets
    this far, not silently discovered here.
    """
    constant_mapping = constant_mapping or {}
    expected_units = expected_units or {}
    variable_names = list(mapping.keys()) + [name for name in constant_mapping if name not in mapping]
    constant_variable_names = set(constant_mapping.keys())

    recordings: dict[int, dict[str, Any]] = {}
    for recording_id in recording_ids:
        recording = database.get_recording(recording_id)
        if recording is None:
            raise ValueError(f"Recording {recording_id} not found")
        recordings[recording_id] = recording

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["trajectory_id", "step_index", "sim_time_s", *variable_names]
    writer.writerow(header)

    for recording_id in recording_ids:
        recording = recordings[recording_id]

        point_id_by_object_id = _point_id_by_source_object_id(recording)
        recording_point_ids: dict[str, int | None] = {
            variable_name: point_id_by_object_id.get(source_object_id)
            for variable_name, source_object_id in mapping.items()
        }
        if any(pid is None for pid in recording_point_ids.values()):
            # At least one requested variable has no point in this
            # recording at all -- every row from this recording would be
            # incomplete, so the whole recording contributes nothing rather
            # than writing partial rows train_baseline.py can't consume.
            continue
        if any(recording_id not in constant_mapping[name] for name in constant_variable_names):
            # Same "whole recording contributes nothing" rule, for a
            # constant-sourced variable this recording never snapshotted --
            # either it predates migration 024, or the variable wasn't
            # constant-sourced when this recording ran. See this function's
            # own docstring: never fall back to a current live value here.
            continue

        units_by_point_id = _units_by_recording_point_id(recording)
        rows = database.get_recording_all_samples(recording_id)
        by_point: dict[int, dict[int, Any]] = {}
        for row in rows:
            by_point.setdefault(row["recording_point_id"], {})[row["sample_index"]] = row["value"]

        interval = recording["sample_interval_seconds"]
        sample_indices = sorted({row["sample_index"] for row in rows})
        for sample_index in sample_indices:
            values: list[Any] = []
            skip_row = False
            for variable_name in variable_names:
                if variable_name in constant_variable_names:
                    # Same literal value on every row from this recording --
                    # that's what "constant" means -- never looked up from a
                    # point/sample at all.
                    values.append(constant_mapping[variable_name][recording_id])
                    continue
                point_id = recording_point_ids[variable_name]
                point_samples = by_point.get(point_id, {})
                if sample_index not in point_samples:
                    # This one sample cycle is missing a value for this
                    # point (e.g. a read failure for an external device) --
                    # drop just this row, not the whole recording.
                    skip_row = True
                    break
                values.append(_normalize_row_value(
                    point_samples[sample_index], variable_name,
                    units_by_point_id.get(point_id), expected_units,
                ))
            if skip_row:
                continue
            row = [recording_id, sample_index, sample_index * interval, *values]
            writer.writerow(row)

    return buffer.getvalue().encode("utf-8")


def _recording_label(recording: dict[str, Any], classifier_target: str) -> Any:
    """The label a recording contributes for `classifier_target`. "condition" is a direct field
    read. "fault_type" needs one exception: RecordingsDrawer.vue only ever shows/sets fault_type
    when condition isn't 'normal' (there's nothing to classify for a normal recording at
    LABELING time) -- so a Normal recording's fault_type is always None by construction, never
    because it's "unlabeled". Without this, a fault-type classifier could never include a Normal
    recording at all, making it impossible to mix e.g. one Normal + one Fault recording for
    training (a fault classifier needs a "normal" baseline to distinguish faults from). "normal"
    here is a synthetic training label only -- never written into recordings.fault_type itself,
    which stays exactly the closed 20-value enum it always was. Mirrors
    ClassifierTrainingFlow.vue's own recordingLabel() -- the two must agree, since the frontend's
    own coverage preview is computed from the same rule."""
    if classifier_target == "fault_type" and recording.get("condition") == "normal":
        return "normal"
    return recording.get(classifier_target)


def recording_class_distribution(
    database: Any, recording_ids: list[int], classifier_target: str,
) -> dict[str, dict[str, int]]:
    """Per-class counts by BOTH recording (trajectory) count and sample count -- computed from
    the recordings' own stored `sample_count`, not by fetching every sample row, since this is
    meant to be cheap enough to call before a training job is even submitted (see
    build_classifier_export_job's own pre-training validation). Mirrors iot-models'
    train_classifier.py class_distribution() exactly (same shape), just computed from
    recording-level metadata here rather than the built CSV -- the two must agree, since this is
    what the API validates/surfaces against before the job runs, and iot-models re-derives the
    same numbers from the actual CSV once training starts.

    A recording with a null value for `classifier_target` is skipped entirely, same exclusion
    rule build_classification_dataset applies -- it contributes to neither count."""
    dist: dict[str, dict[str, int]] = {}
    for recording_id in recording_ids:
        recording = database.get_recording(recording_id)
        if recording is None:
            raise ValueError(f"Recording {recording_id} not found")
        label = _recording_label(recording, classifier_target)
        if label is None:
            continue
        entry = dist.setdefault(str(label), {"trajectories": 0, "samples": 0})
        entry["trajectories"] += 1
        entry["samples"] += int(recording.get("sample_count") or 0)
    return dist


def build_classification_dataset(
    database: Any,
    recording_ids: list[int],
    mapping: dict[str, int],
    classifier_target: str,
    constant_mapping: dict[str, dict[int, float]] | None = None,
    expected_units: dict[str, str | None] | None = None,
) -> bytes:
    """Classifier counterpart to build_training_dataset() above -- same CSV shape
    (trajectory_id, step_index, sim_time_s, input_*) but ends in a single `label` column instead
    of output_* columns, since a classifier has no continuous output to learn, only one
    categorical target per recording. `mapping`/`constant_mapping` follow the exact same
    {variable_name: source_object_id}/{variable_name: {recording_id: value}} shapes as
    build_training_dataset -- restricted by the caller (see training_export.py's router) to
    input-direction variables only, since a classifier's features come entirely from input_*
    points, never output_* ones.

    `classifier_target` is "condition" or "fault_type" -- whichever recording field the caller
    chose as the training label (see CreateTrainingExportJobRequest.classifier_target). A
    recording with a null value for that field contributes NO rows at all (excluded outright,
    not defaulted to some placeholder label) -- mirrors recording_class_distribution's own
    exclusion rule; the two must stay consistent since the distribution report is what a caller
    validates against before ever calling this function."""
    constant_mapping = constant_mapping or {}
    expected_units = expected_units or {}
    variable_names = list(mapping.keys()) + [name for name in constant_mapping if name not in mapping]
    constant_variable_names = set(constant_mapping.keys())

    recordings: dict[int, dict[str, Any]] = {}
    for recording_id in recording_ids:
        recording = database.get_recording(recording_id)
        if recording is None:
            raise ValueError(f"Recording {recording_id} not found")
        recordings[recording_id] = recording

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["trajectory_id", "step_index", "sim_time_s", *variable_names, "label"]
    writer.writerow(header)

    for recording_id in recording_ids:
        recording = recordings[recording_id]
        label = _recording_label(recording, classifier_target)
        if label is None:
            continue

        point_id_by_object_id = _point_id_by_source_object_id(recording)
        recording_point_ids: dict[str, int | None] = {
            variable_name: point_id_by_object_id.get(source_object_id)
            for variable_name, source_object_id in mapping.items()
        }
        if any(pid is None for pid in recording_point_ids.values()):
            continue
        if any(recording_id not in constant_mapping[name] for name in constant_variable_names):
            continue

        units_by_point_id = _units_by_recording_point_id(recording)
        rows = database.get_recording_all_samples(recording_id)
        by_point: dict[int, dict[int, Any]] = {}
        for row in rows:
            by_point.setdefault(row["recording_point_id"], {})[row["sample_index"]] = row["value"]

        interval = recording["sample_interval_seconds"]
        sample_indices = sorted({row["sample_index"] for row in rows})
        for sample_index in sample_indices:
            values: list[Any] = []
            skip_row = False
            for variable_name in variable_names:
                if variable_name in constant_variable_names:
                    values.append(constant_mapping[variable_name][recording_id])
                    continue
                point_id = recording_point_ids[variable_name]
                point_samples = by_point.get(point_id, {})
                if sample_index not in point_samples:
                    skip_row = True
                    break
                values.append(_normalize_row_value(
                    point_samples[sample_index], variable_name,
                    units_by_point_id.get(point_id), expected_units,
                ))
            if skip_row:
                continue
            row = [recording_id, sample_index, sample_index * interval, *values, label]
            writer.writerow(row)

    return buffer.getvalue().encode("utf-8")
