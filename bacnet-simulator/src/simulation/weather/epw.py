"""EPW/MOS weather header parsing helpers.

An EPW file is a fixed 8-line header followed by exactly one CSV data row
per hour of a single representative year (8760 rows, or 8784 for a leap
year in the source data) -- see the EnergyPlus Auxiliary Programs
documentation for the full field layout. A Buildings-format .mos converted
from one (see weather/convert.py) preserves every original EPW header line
verbatim, `#`-prefixed.
"""
from __future__ import annotations

import re
from datetime import date

_MONTH_ABBR_TO_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_MONTH_YEAR_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)=(\d{4})\b")
_PERIOD_OF_RECORD_RE = re.compile(r"Period of Record=(\d{4}-\d{4})")
_LOCATION_LINE_RE = re.compile(r"^#?LOCATION,([^,]*),([^,]*),([^,]*),([^,]*),", re.MULTILINE)


def parse_weather_provenance(text: str) -> dict[str, object] | None:
    """Best-effort extraction of a TMYx-style composite weather file's
    provenance -- which real calendar year each month's data was actually
    drawn from -- from its own #COMMENTS/LOCATION header lines. Confirmed
    against a real climate.onebuilding.org TMYx file (`COMMENTS 1,"NCEI
    ISD/ERA5 - #years=[15] Period of Record=2011-2025; Jan=2016;
    Feb=2020; ..."`). Works against both a raw .epw (unprefixed header
    lines) and a converted .mos (same lines, `#`-prefixed by convert.py's
    "Add original EPW header lines as comments" step) since the regexes
    key off the `Mon=YYYY` tokens themselves, not line position or a
    leading `#`.

    Not every EPW source writes this line -- returns None rather than
    raising when nothing matches, so an unrecognized source degrades to
    "no provenance available" instead of erroring the upload."""
    months_found = _MONTH_YEAR_RE.findall(text)
    if not months_found:
        return None
    months = {_MONTH_ABBR_TO_NUM[abbr]: int(year) for abbr, year in months_found}
    period_match = _PERIOD_OF_RECORD_RE.search(text)
    location_match = _LOCATION_LINE_RE.search(text)
    return {
        "months": months,
        "period_of_record": period_match.group(1) if period_match else None,
        "station": location_match.group(1) if location_match else None,
        "source": location_match.group(4) if location_match else None,
    }


def start_hour_of_year(start_date: date) -> float:
    """Converts a playback start date to an hour-of-year offset into a
    365-row representative-year weather dataset (EPW, or a Buildings-format
    .mos converted from one -- both use the same row-per-hour convention).

    The day-of-year must be computed against a fixed non-leap reference
    (2001), never the date's own real calendar year -- an EPW/MOS file is
    always a 365-row representative year with no Feb 29, even when
    start_date.year (e.g. 2024) is a real leap year. Using the real
    calendar here silently misaligns every date from March 1 onward by 24
    hours in any leap year (confirmed: Sept 8 2024 landed on the file's
    Sept 9 row). A literal Feb 29 (only possible if the caller passes a
    leap year) has no equivalent row in a 365-row dataset, so it collapses
    onto Feb 28 rather than raising.

    Used by the FMU-based Weather model's "Playback Start Month" parameter
    (models/runtime.py's _build_fmu_provider, which converts a selected
    month into a session warmup_seconds override using this conversion)."""
    try:
        reference = date(2001, start_date.month, start_date.day)
    except ValueError:
        reference = date(2001, 2, 28)
    return (reference.timetuple().tm_yday - 1) * 24.0
