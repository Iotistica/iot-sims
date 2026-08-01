"""BACnet Schedule objects (Clause 12.24) — weekly schedule + exception
schedule + effective period + priority-array writes to one or more target
object properties on the same virtual device.

Built directly on bacpypes3.local.schedule.ScheduleObject rather than
reimplementing evaluation from scratch — it already handles weekday /
exception / priority evaluation and automatically schedules its own next
transition (via asyncio.call_at), which is exactly what's needed here.

Two real bugs in bacpypes3==0.0.91's ScheduleObject.present_value_changed()
made its built-in auto-write unusable as-is:
  1. It calls write_property(..., arrayIndex=...) but the real keyword is
     `index=` — every call raised TypeError, silently swallowed by a bare
     `except Exception`.
  2. write_property is async, but present_value_changed is a *synchronous*
     property-monitor callback that never awaits it — even after fixing (1),
     the call just creates a coroutine that's never run.
LocalScheduleObject overrides present_value_changed to route writes through
the owning SimEngine's write_object() instead of calling the target's
write_property() directly — which is also the architecturally correct
choice here: it keeps DB state, behavior (switches to "manual"), and the
live BACnet object in sync exactly like a real WriteProperty would (see
SimApplication.do_WritePropertyRequest in bacnet_simulator.py).

Deferred (narrow scope, matching the rest of GH #7):
  - Null/relinquish schedule entries — bacpypes3's Null() primitive doesn't
    construct as a real instance in this version (same class of issue as
    `Any()` needed a workaround for in ede.py); every entry here carries an
    explicit value instead of "revert to previous".
  - Recurring weekday-pattern exceptions (BACnetWeekNDay) — only single-date
    and inclusive date-range exceptions are supported.
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any, Optional

from bacpypes3.local.schedule import ScheduleObject as _ScheduleObjectBase
from bacpypes3.constructeddata import AnyAtomic, ArrayOf, SequenceOf
from bacpypes3.basetypes import (
    DailySchedule, TimeValue, DateRange, DeviceObjectPropertyReference,
    SpecialEvent, SpecialEventPeriod, CalendarEntry,
)
from bacpypes3.primitivedata import Time, Date, Real, Boolean, Unsigned

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
VALUE_TYPES = ("real", "boolean", "unsigned")


def python_to_atomic(value: Any, value_type: str) -> AnyAtomic:
    if value_type == "boolean":
        return AnyAtomic(Boolean(bool(value)))
    if value_type == "unsigned":
        return AnyAtomic(Unsigned(int(value)))
    return AnyAtomic(Real(float(value)))


def atomic_to_python(value: Any, value_type: str) -> Any:
    raw = value.get_value() if hasattr(value, "get_value") else value
    if value_type == "boolean":
        return bool(raw)
    if value_type == "unsigned":
        return int(raw)
    return float(raw)


def _parse_time(t: str) -> Time:
    parts = (t.split(":") + ["0", "0"])[:3]
    h, m, s = parts
    return Time((int(h), int(m), int(float(s)), 0))


def parse_date_tuple(d: str) -> tuple:
    dt = datetime.date.fromisoformat(d)
    return (dt.year - 1900, dt.month, dt.day)


def build_weekly_schedule(weekly: dict, value_type: str):
    days = []
    for day_name in DAY_NAMES:
        entries = weekly.get(day_name) or []
        time_values = [
            TimeValue(time=_parse_time(e["time"]), value=python_to_atomic(e["value"], value_type))
            for e in entries
        ]
        days.append(DailySchedule(daySchedule=time_values))
    return ArrayOf(DailySchedule)(days)


def build_exception_schedule(exceptions: list, value_type: str) -> Optional[Any]:
    if not exceptions:
        return None
    events = []
    for exc in exceptions:
        period = exc.get("period") or {}
        ptype = period.get("type")
        if ptype == "date":
            y, m, d = parse_date_tuple(period["date"])
            calendar_entry = CalendarEntry(date=Date((y, m, d, 255)))
        elif ptype == "date-range":
            ys, ms, ds = parse_date_tuple(period["start"])
            ye, me, de = parse_date_tuple(period["end"])
            calendar_entry = CalendarEntry(dateRange=DateRange(
                startDate=Date((ys, ms, ds, 255)), endDate=Date((ye, me, de, 255)),
            ))
        else:
            continue
        time_values = [
            TimeValue(time=_parse_time(e["time"]), value=python_to_atomic(e["value"], value_type))
            for e in exc.get("entries", [])
        ]
        events.append(SpecialEvent(
            period=SpecialEventPeriod(calendarEntry=calendar_entry),
            listOfTimeValues=time_values,
            eventPriority=Unsigned(max(1, min(16, int(exc.get("priority", 1))))),
        ))
    return SequenceOf(SpecialEvent)(events)


def build_effective_period(start: Optional[str], end: Optional[str]) -> DateRange:
    """A missing start/end means "always". bacpypes3's match_date_range()
    does a literal tuple comparison rather than true BACnet 255-wildcard
    semantics, so "always" is approximated with a wide practical range
    (1900-2154) instead of a real wildcard — verified against the installed
    bacpypes3 version during development."""
    start_tuple = parse_date_tuple(start) if start else (0, 1, 1)
    end_tuple = parse_date_tuple(end) if end else (254, 255, 255)
    return DateRange(
        startDate=Date(start_tuple + (255,)),
        endDate=Date(end_tuple + (255,)),
    )


def default_value(value_type: str, raw: Any) -> Any:
    if value_type == "boolean":
        return Boolean(bool(raw))
    if value_type == "unsigned":
        return Unsigned(int(raw))
    return Real(float(raw))


def initial_present_value(value_type: str, raw: Any) -> Any:
    return default_value(value_type, raw)


class LocalScheduleObject(_ScheduleObjectBase):
    """bacpypes3's ScheduleObject with a working present_value_changed() —
    see module docstring for why the override is needed. The engine sets
    `_value_type` right after construction so this callback knows how to
    convert the evaluated AnyAtomic back into a plain Python value.
    (Leading underscore matters: bacpypes3's Object.__setattr__ only allows
    setting names that aren't declared BACnet properties when they start
    with `_` — anything else raises KeyError.)"""

    _value_type: str = "real"

    def present_value_changed(self, old_value: Any, new_value: Any) -> None:
        app = self._app
        if app is None or getattr(app, "_sim_engine", None) is None:
            return
        obj_prop_refs = self.listOfObjectPropertyReferences
        if not obj_prop_refs:
            return
        engine = app._sim_engine
        python_value = atomic_to_python(new_value, self._value_type)
        for ref in obj_prop_refs:
            if ref.deviceIdentifier:
                continue
            target_obj = app.get_object_id(ref.objectIdentifier)
            if target_obj is None:
                continue
            db_id = engine.db_id_for_bacnet_object(target_obj)
            if db_id is None:
                continue
            asyncio.create_task(engine.write_object(db_id, python_value))
