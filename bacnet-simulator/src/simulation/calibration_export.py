"""Builds the wide-format CSV iot-models' calibration API expects (one
column per mapped model variable + a shared timestamp column) directly
from a Recording's own stored samples. No unit conversion is attempted --
recorded values are written as-is; the mapping step is where a user judges
whether a point's units are a reasonable fit for what the model expects
(see src/api/routers/calibration.py's mapping-suggestions route, which
surfaces both units side by side)."""
from __future__ import annotations

import csv
import io
from typing import Any


def build_calibration_dataset(database: Any, recording_id: int, mapping: dict[str, int]) -> bytes:
    """`mapping` is {variable_name: recording_point_id}, one entry per model
    input the caller chose to map plus the one required calibration goal
    variable. Pivots the recording's long-format samples
    (get_replay_recording_all_samples) into one CSV row per sample_index,
    columns in `mapping`'s own order (dict insertion order -- the caller
    controls this, e.g. inputs first then the goal last)."""
    rows = database.get_replay_recording_all_samples(recording_id)

    # point_id -> {sample_index: (timestamp, value)}
    by_point: dict[int, dict[int, tuple[str, Any]]] = {}
    for row in rows:
        by_point.setdefault(row["recording_point_id"], {})[row["sample_index"]] = (
            row["timestamp"], row["value"],
        )

    sample_indices = sorted({row["sample_index"] for row in rows})

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    variable_names = list(mapping.keys())
    writer.writerow(["timestamp", *variable_names])

    for sample_index in sample_indices:
        timestamp: str | None = None
        values: list[Any] = []
        skip_row = False
        for variable_name in variable_names:
            point_samples = by_point.get(mapping[variable_name], {})
            entry = point_samples.get(sample_index)
            if entry is None:
                # This point has no sample at this sample_index (can happen
                # if points were added to the recording at different times,
                # though today every point is captured together every
                # cycle) -- drop the whole row rather than write a gap the
                # calibration script has no concept of.
                skip_row = True
                break
            ts, value = entry
            timestamp = ts
            values.append(value)
        if skip_row:
            continue
        writer.writerow([timestamp, *values])

    return buffer.getvalue().encode("utf-8")
