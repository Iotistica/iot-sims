"""parse_weather_provenance -- best-effort extraction of a TMYx-style
composite weather file's per-month source-year breakdown from its own
#COMMENTS header line, used to label the "Playback Start Month" dropdown
in SimulationModelDrawer.vue (see src/api/routers/simulation.py's
/simulation/resources upload response and .../weather-provenance route).
"""
from __future__ import annotations

from src.simulation.weather.epw import parse_weather_provenance

# Verbatim shape of a real climate.onebuilding.org TMYx file's header
# (confirmed against CAN_ON_Toronto.City-Univ.of.Toronto.715080_TMYx.2011-2025.epw).
_REAL_EPW_HEADER = (
    'LOCATION,Toronto.City-Univ.of.Toronto,ON,CAN,SRC-TMYx,715080,43.66580,-79.39530,-5.0,112.5\n'
    'DESIGN CONDITIONS,1,...\n'
    'TYPICAL/EXTREME PERIODS,6,...\n'
    'GROUND TEMPERATURES,3,...\n'
    'HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0\n'
    'COMMENTS 1,"NCEI ISD/ERA5 - #years=[15] Period of Record=2011-2025; Jan=2016; '
    'Feb=2020; Mar=2023; Apr=2025; May=2022; Jun=2023; Jul=2024; Aug=2012; Sep=2023; '
    'Oct=2025; Nov=2023; Dec=2014"\n'
    'COMMENTS 2,"Downloaded from Climate.Onebuilding.org"\n'
    'DATA PERIODS,1,1,Data,Sunday,1/ 1,12/31\n'
)


def test_parses_real_tmyx_header_month_years():
    result = parse_weather_provenance(_REAL_EPW_HEADER)
    assert result is not None
    assert result["months"] == {
        1: 2016, 2: 2020, 3: 2023, 4: 2025, 5: 2022, 6: 2023,
        7: 2024, 8: 2012, 9: 2023, 10: 2025, 11: 2023, 12: 2014,
    }
    assert result["period_of_record"] == "2011-2025"
    assert result["station"] == "Toronto.City-Univ.of.Toronto"
    assert result["source"] == "SRC-TMYx"


def test_parses_mos_converted_header_with_hash_prefix():
    # convert.py's convert_epw_to_mos writes every original EPW header line
    # back out prefixed with "#" -- the parser must key off the Mon=YYYY
    # tokens themselves, not line position or a leading "#".
    mos_text = "\n".join(f"#{line}" for line in _REAL_EPW_HEADER.splitlines())
    result = parse_weather_provenance(mos_text)
    assert result is not None
    assert result["months"][7] == 2024
    assert result["station"] == "Toronto.City-Univ.of.Toronto"


def test_returns_none_when_no_comments_line_present():
    text = "LOCATION,Some City,ST,USA,SRC,123456,40.0,-80.0,-5.0,100.0\nDATA PERIODS,1,1\n"
    assert parse_weather_provenance(text) is None


def test_returns_none_for_empty_text():
    assert parse_weather_provenance("") is None
