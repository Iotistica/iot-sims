"""ThermalZone.mo regression suite -- exercises the exported
ThermalZone.fmu via pyfmi in Model Exchange mode, matching this project's
own production calling convention (fmu.simulate(input=(names, trajectory))),
the same pattern used throughout models/ahu/tests, models/boiler/tests,
models/rtu/tests, and models/vav/tests.

This is the first standalone pytest suite for ThermalZone.mo -- it was
added alongside the internal-heat-gain split (QInternal -> QPeople +
QLighting + QEquipment, summed into the new QInternalTotal output) to
verify two things: the component sum itself, and that the zone's existing
sensible heat balance (HVAC supply-air term + envelope term + internal
gain term, integrated by CZone*der(TRoo)) is byte-for-byte unchanged --
only the source of the internal-gain term changed, from one input to a
computed sum of three.

Run core tests only (fast, CI default):
    pytest models/zone/tests -m core
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyfmi import load_fmu

ZONE_DIR = Path(__file__).resolve().parent.parent
ZONE_FMU = ZONE_DIR / "ThermalZone.fmu"

NAMES = ["TOut", "QPeople", "QLighting", "QEquipment", "TSup", "VSup_flow"]
ALL_OUTPUTS = ["TRoo", "QHVAC", "QEnvelope", "QNet", "QInternalTotal"]

# ThermalZone.mo's own parameter defaults -- used only to compute expected
# steady-state values analytically (never fed into the model, which already
# uses these as its compiled-in defaults).
P = dict(VRoo=150.0, CBuilding=5e6, UA=150.0, rhoAir=1.2, cpAir=1006.0)


# ─── Helpers ─────────────────────────────────────────────────────────────

def c2k(celsius: float) -> float:
    return celsius + 273.15


def k2c(kelvin: float) -> float:
    return kelvin - 273.15


def load_zone_fmu(fmu_path: Path = ZONE_FMU):
    if not fmu_path.exists():
        pytest.skip(f"{fmu_path} not found -- build it before running these tests")
    return load_fmu(str(fmu_path), kind="ME")


def const_rows(TOut_c, QPeople, QLighting, QEquipment, TSup_c, VSup_flow, final_time):
    """A two-point trajectory holding every input constant for final_time
    seconds -- same shape as test_vav.py's const_rows."""
    v = [c2k(TOut_c), QPeople, QLighting, QEquipment, c2k(TSup_c), VSup_flow]
    return [[0.0] + v, [final_time] + v]


def simulate_zone(rows, fmu_path: Path = ZONE_FMU, params: dict | None = None, ncp: int | None = None):
    """result_handling='memory' for the same reason as every other suite in
    this repo (see models/vav/tests/test_vav.py's simulate_vav docstring):
    pyfmi's default 'file' mode overwrites a shared, model-name-derived
    .mat file across repeated simulations in one process."""
    fmu = load_zone_fmu(fmu_path)
    if params:
        for k, v in params.items():
            fmu.set(k, v)
    fmu.set_log_level(2)
    trajectory = np.array(rows, dtype=float)
    final_time = float(rows[-1][0])
    options = fmu.simulate_options()
    options["ncp"] = ncp or max(2, int(final_time / 30.0))
    options["result_handling"] = "memory"
    return fmu.simulate(start_time=0.0, final_time=final_time,
                         input=(NAMES, trajectory), options=options)


def get_final_value(res, name: str) -> float:
    return float(np.asarray(res[name])[-1])


def get_series(res, name: str) -> np.ndarray:
    return np.asarray(res[name], dtype=float)


def assert_finite(res, names=ALL_OUTPUTS):
    for n in names:
        assert np.all(np.isfinite(get_series(res, n))), f"{n} contains NaN/Inf"


def assert_close(actual: float, expected: float, *, abs_tol: float | None = None,
                  rel_tol: float = 0.02, msg: str = ""):
    if abs_tol is not None:
        assert abs(actual - expected) <= abs_tol, \
            f"{msg}: {actual!r} not within {abs_tol} of {expected!r}"
    else:
        assert abs(actual - expected) <= rel_tol * max(abs(expected), 1e-9), \
            f"{msg}: {actual!r} not within {rel_tol*100:.1f}% of {expected!r}"


def expected_steady_state_troo_k(TOut_k: float, TSup_k: float, VSup_flow: float, q_internal_total: float) -> float:
    """Analytical steady state of ThermalZone.mo's own heat balance
    (QHVAC + QEnvelope + QInternalTotal = 0 at der(TRoo) = 0), independent
    of the FMU -- used to prove the compiled model's physics are unchanged,
    not just that it produces *some* finite number."""
    hvac_coeff = P["rhoAir"] * max(0.0, VSup_flow) * P["cpAir"]
    return (hvac_coeff * TSup_k + P["UA"] * TOut_k + q_internal_total) / (hvac_coeff + P["UA"])


# ─── Component sum: QInternalTotal = QPeople + QLighting + QEquipment (core) ─

@pytest.mark.core
def test_internal_heat_gain_component_sum_unequal_values():
    """The three components are deliberately unequal (and one is zero) so
    the assertion can't pass by coincidence the way equal/symmetric inputs
    might (e.g. a bug that used max() or averaged instead of summed)."""
    rows = const_rows(TOut_c=20.0, QPeople=180.0, QLighting=0.0, QEquipment=245.5,
                       TSup_c=20.0, VSup_flow=0.0, final_time=3600.0)
    res = simulate_zone(rows)
    assert_finite(res)
    assert_close(get_final_value(res, "QInternalTotal"), 180.0 + 0.0 + 245.5,
                 abs_tol=1e-3, msg="QInternalTotal must equal QPeople+QLighting+QEquipment")


@pytest.mark.core
def test_internal_heat_gain_component_sum_tracks_changing_inputs():
    """The sum must track a mid-simulation step change in the components,
    not just match at t=0 -- proves QInternalTotal is an algebraic
    equation evaluated every step, not a one-time initial computation."""
    fmu = load_zone_fmu()
    fmu.set_log_level(2)
    names = NAMES
    rows = [
        [0.0, c2k(20.0), 100.0, 50.0, 25.0, c2k(20.0), 0.0],
        [1800.0, c2k(20.0), 100.0, 50.0, 25.0, c2k(20.0), 0.0],
        [1800.0001, c2k(20.0), 400.0, 300.0, 150.0, c2k(20.0), 0.0],
        [3600.0, c2k(20.0), 400.0, 300.0, 150.0, c2k(20.0), 0.0],
    ]
    trajectory = np.array(rows, dtype=float)
    options = fmu.simulate_options()
    options["ncp"] = 120
    options["result_handling"] = "memory"
    res = fmu.simulate(start_time=0.0, final_time=3600.0, input=(names, trajectory), options=options)
    assert_finite(res)

    time = get_series(res, "time") if "time" in res.keys() else None
    total = get_series(res, "QInternalTotal")
    # Before the step: 100+50+25=175. After: 400+300+150=850.
    assert_close(float(total[0]), 175.0, abs_tol=1.0, msg="pre-step QInternalTotal")
    assert_close(float(total[-1]), 850.0, abs_tol=1.0, msg="post-step QInternalTotal")


# ─── Existing thermal behavior preserved (core) ─────────────────────────────

@pytest.mark.core
def test_steady_state_matches_prior_single_gain_default():
    """Same total internal gain (1000 W) the model used to take as one
    QInternal input, now split 300/300/400 across the three components --
    the zone's steady-state temperature must match the SAME analytical
    heat-balance formula as before (only the internal-gain term's SOURCE
    changed, not the equation itself, and not any other parameter)."""
    TOut_c, TSup_c, VSup_flow = 30.0, 13.0, 0.3
    rows = const_rows(TOut_c=TOut_c, QPeople=300.0, QLighting=300.0, QEquipment=400.0,
                       TSup_c=TSup_c, VSup_flow=VSup_flow, final_time=86400.0)
    res = simulate_zone(rows, ncp=1440)
    assert_finite(res)

    expected_k = expected_steady_state_troo_k(
        c2k(TOut_c), c2k(TSup_c), VSup_flow, q_internal_total=1000.0,
    )
    assert_close(get_final_value(res, "TRoo"), expected_k, abs_tol=0.2,
                 msg="steady-state TRoo should match the unchanged heat-balance formula")
    assert_close(get_final_value(res, "QInternalTotal"), 1000.0, abs_tol=1e-3)


@pytest.mark.core
def test_zero_internal_gain_matches_pure_envelope_hvac_balance():
    """A second, independent operating point (all internal gain sources
    off) -- guards against a test that only happens to pass at the
    original default split."""
    TOut_c, TSup_c, VSup_flow = 5.0, 18.0, 0.15
    rows = const_rows(TOut_c=TOut_c, QPeople=0.0, QLighting=0.0, QEquipment=0.0,
                       TSup_c=TSup_c, VSup_flow=VSup_flow, final_time=86400.0)
    res = simulate_zone(rows, ncp=1440)
    assert_finite(res)

    expected_k = expected_steady_state_troo_k(
        c2k(TOut_c), c2k(TSup_c), VSup_flow, q_internal_total=0.0,
    )
    assert_close(get_final_value(res, "TRoo"), expected_k, abs_tol=0.2)
    assert_close(get_final_value(res, "QInternalTotal"), 0.0, abs_tol=1e-6)


@pytest.mark.core
def test_net_heat_flow_balance_unaffected_by_gain_split():
    """QNet = QHVAC + QEnvelope + QInternalTotal must still hold exactly --
    the component split changes how QInternalTotal is computed, not the
    downstream QNet/CZone*der(TRoo) equations that consume it."""
    rows = const_rows(TOut_c=28.0, QPeople=250.0, QLighting=150.0, QEquipment=600.0,
                       TSup_c=14.0, VSup_flow=0.25, final_time=7200.0)
    res = simulate_zone(rows)
    assert_finite(res)

    q_hvac = get_final_value(res, "QHVAC")
    q_envelope = get_final_value(res, "QEnvelope")
    q_internal_total = get_final_value(res, "QInternalTotal")
    q_net = get_final_value(res, "QNet")
    assert_close(q_net, q_hvac + q_envelope + q_internal_total, abs_tol=1e-3,
                 msg="QNet must equal QHVAC+QEnvelope+QInternalTotal")
