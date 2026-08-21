"""Trend-log BACnet wire helpers.

Physically extracted from src/legacy.py (continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
API-router extractions).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from bacpypes3.basetypes import DateTime, LogRecord, LogRecordLogDatum
from bacpypes3.local.object import Object as LocalObject
from bacpypes3.object import TrendLogObject as _TrendLogObjectSchema
from bacpypes3.primitivedata import Boolean, Date, Real, Time, Unsigned

from ..core.config import MULTISTATE_TYPES


# ─── Trend Log (BACnet wire exposure + ReadRange) ─────────────────────────────
# TrendLogObject has no bacpypes3.local implementation (unlike analog/binary/
# multi-state) — it's schema-only, so the "local" mixin here just makes it
# addressable/readable via the standard Object machinery. All the actual
# logging behavior (sampling, circular buffer) lives in SimEngine/Database;
# this class only carries the read-only, BACnet-wire-visible snapshot of it.
class LocalTrendLogObject(LocalObject, _TrendLogObjectSchema):
    pass


def _bacnet_datetime(ts: str) -> DateTime:
    """Parse a 'YYYY-MM-DD HH:MM:SS' SQLite timestamp into a BACnet DateTime."""
    d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return DateTime(
        date=Date((d.year - 1900, d.month, d.day, d.isoweekday())),
        time=Time((d.hour, d.minute, d.second, 0)),
    )


def _parse_trend_value(value_str: str, otype: str) -> Any:
    if otype in ("binary-input", "binary-output", "binary-value"):
        return value_str == "True"
    if otype in MULTISTATE_TYPES:
        return int(round(float(value_str)))
    return float(value_str)


def _build_log_record(record: dict, otype: str) -> LogRecord:
    value = _parse_trend_value(record["value"], otype)
    if otype in ("binary-input", "binary-output", "binary-value"):
        datum = LogRecordLogDatum(booleanValue=Boolean(value))
    elif otype in MULTISTATE_TYPES:
        datum = LogRecordLogDatum(unsignedValue=Unsigned(value))
    else:
        datum = LogRecordLogDatum(realValue=Real(value))
    return LogRecord(
        timestamp=_bacnet_datetime(record["ts"]),
        logDatum=datum,
        statusFlags=[0, 0, 0, 0],
    )


def _slice_trend_records(records: list[dict], range_: Any) -> tuple[list[dict], bool, bool]:
    """Apply a BACnet ReadRange Range choice (byPosition/bySequenceNumber/
    byTime) to an ascending-by-sequence-number list of records. Returns
    (selected, is_first, is_last) — is_first/is_last describe whether the
    selection includes the buffer's oldest/newest record (for resultFlags).
    No range at all (range_ is None) returns everything."""
    if not records:
        return [], True, True

    if range_ is None:
        return records, True, True

    def _apply(idx: int, count: int) -> list[dict]:
        if count >= 0:
            return records[idx: idx + count]
        end = idx + 1
        start = max(0, end + count)
        return records[start:end]

    selected: list[dict] = []
    if range_.byPosition is not None:
        idx = max(0, min(len(records) - 1, range_.byPosition.referenceIndex - 1))
        selected = _apply(idx, int(range_.byPosition.count))
    elif range_.bySequenceNumber is not None:
        ref_seq = range_.bySequenceNumber.referenceSequenceNumber
        idx = next((i for i, r in enumerate(records) if r["sequence_number"] >= ref_seq), len(records) - 1)
        selected = _apply(idx, int(range_.bySequenceNumber.count))
    elif range_.byTime is not None:
        ref_dt = range_.byTime.referenceTime
        ref_ts = f"{ref_dt.date[0] + 1900:04d}-{ref_dt.date[1]:02d}-{ref_dt.date[2]:02d} " \
                 f"{ref_dt.time[0]:02d}:{ref_dt.time[1]:02d}:{ref_dt.time[2]:02d}"
        idx = next((i for i, r in enumerate(records) if r["ts"] >= ref_ts), len(records) - 1)
        selected = _apply(idx, int(range_.byTime.count))
    else:
        selected = records

    is_first = bool(selected) and selected[0]["sequence_number"] == records[0]["sequence_number"]
    is_last = bool(selected) and selected[-1]["sequence_number"] == records[-1]["sequence_number"]
    return selected, is_first, is_last
