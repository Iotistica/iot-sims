"""BACnet Calendar objects (Clause 12.23) — a device-scoped list of dates
(single dates, inclusive date ranges, and weekday patterns) that a BACnet
Schedule's exceptionSchedule can reference via calendarReference instead of
repeating the same dates inline in every schedule that needs them (GH #18).

Calendar is just another row in the `objects` table (object_type="calendar"),
constructed in SimEngine._create_object() like every other object type, which
gives it a real (object_type, object_instance) portable reference — exactly
what bacnet_schedule.py's exception-schedule targets already use for Schedule
targets, and what profile save/load already round-trips.

Built on bacpypes3.object.CalendarObject (schema only — bacpypes3 has no
bacpypes3.local.calendar module) combined with the same
bacpypes3.local.object.Object mixin every local object in this sim uses for
ReadProperty/WriteProperty/propertyList support.

presentValue has no live property-monitor hook here (unlike ScheduleObject,
which self-schedules its own re-evaluation) — SimEngine.tick() recomputes it
periodically for any live Calendar object via today_in_date_list(). Schedule's
own calendarReference resolution (built into bacpypes3's ScheduleObject.eval())
reads dateList directly and doesn't depend on presentValue at all.
"""
from __future__ import annotations

import datetime
from typing import Optional

from bacpypes3.object import CalendarObject as _CalendarObjectSchema
from bacpypes3.local.object import Object as _Object
from bacpypes3.local.schedule import date_in_calendar_entry
from bacpypes3.basetypes import CalendarEntry, DateRange, WeekNDay
from bacpypes3.constructeddata import SequenceOf
from bacpypes3.primitivedata import Date, Unsigned

DATE_LIST_ENTRY_TYPES = {"date", "date-range", "weekday"}


class LocalCalendarObject(_Object, _CalendarObjectSchema):
    """A local Calendar object — see module docstring for presentValue caveat.

    notificationClass: bacpypes3.local.object.Object._post_init() (run for
    every local object, unconditionally) reads self.notificationClass to link
    up intrinsic-reporting objects. bacpypes3's CalendarObject schema has no
    such property at all (correctly, per spec — Calendar isn't alarm-capable),
    so that read raised AttributeError from a fire-and-forget asyncio task on
    every construction. Declaring it as a plain class attribute (not a real
    BACnet element — it doesn't appear in _elements or propertyList, verified
    during development) satisfies that internal read without exposing a
    bogus property on the wire."""
    notificationClass: Optional[Unsigned] = None


def _parse_date_tuple(d: str) -> tuple:
    dt = datetime.date.fromisoformat(d)
    return (dt.year - 1900, dt.month, dt.day)


def validate_date_list(entries: list) -> None:
    """Raises ValueError with a human-readable message on the first bad entry."""
    for i, e in enumerate(entries):
        etype = e.get("type") if isinstance(e, dict) else None
        if etype not in DATE_LIST_ENTRY_TYPES:
            raise ValueError(f"date_list[{i}]: type must be one of {sorted(DATE_LIST_ENTRY_TYPES)}")
        try:
            if etype == "date":
                datetime.date.fromisoformat(e["date"])
            elif etype == "date-range":
                datetime.date.fromisoformat(e["start"])
                datetime.date.fromisoformat(e["end"])
            else:  # weekday
                for field in ("month", "week_of_month", "day_of_week"):
                    v = e.get(field)
                    if v is not None and not isinstance(v, int):
                        raise ValueError(f"date_list[{i}].{field} must be an integer or null")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"date_list[{i}]: malformed {etype} entry ({exc})") from None


def build_date_list(entries: list) -> SequenceOf(CalendarEntry):
    calendar_entries = []
    for e in entries:
        etype = e.get("type")
        if etype == "date":
            y, m, d = _parse_date_tuple(e["date"])
            calendar_entries.append(CalendarEntry(date=Date((y, m, d, 255))))
        elif etype == "date-range":
            ys, ms, ds = _parse_date_tuple(e["start"])
            ye, me, de = _parse_date_tuple(e["end"])
            calendar_entries.append(CalendarEntry(dateRange=DateRange(
                startDate=Date((ys, ms, ds, 255)), endDate=Date((ye, me, de, 255)),
            )))
        elif etype == "weekday":
            month = e.get("month") or 255
            week_of_month = e.get("week_of_month") or 255
            day_of_week = e.get("day_of_week") or 255
            calendar_entries.append(CalendarEntry(weekNDay=WeekNDay(bytes([month, week_of_month, day_of_week]))))
    return SequenceOf(CalendarEntry)(calendar_entries)


def today_in_date_list(entries: list) -> bool:
    """Best-effort presentValue: does today's real wall-clock date match any
    entry? Recomputed periodically by SimEngine.tick() — see module docstring."""
    today = datetime.date.today()
    date_tuple = (today.year - 1900, today.month, today.day, today.isoweekday())
    date_obj = Date(date_tuple)
    return any(date_in_calendar_entry(date_obj, ce) for ce in build_date_list(entries))
