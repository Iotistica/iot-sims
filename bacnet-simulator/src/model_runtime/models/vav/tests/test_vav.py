"""SimpleVAVZone.mo regression suite -- exercises the exported
SimpleVAVZone.fmu via pyfmi in Model Exchange mode, matching this project's
own production calling convention (fmu.simulate(input=(names, trajectory))),
the same pattern used throughout models/ahu/tests, models/boiler/tests, and
models/rtu/tests.

This is the first standalone pytest suite for SimpleVAVZone.mo -- the
repo's only prior VAV test artifact (TestSimpleVAVZone.mo) predates the
current FMU interface (it wires to vavZone.TOut/vavZone.QInternal, which do
not exist on this model) and is not part of any CI or build pipeline, so
there was no existing pytest baseline to preserve. This suite establishes
that baseline going forward, plus tests for the new `dpSup` upstream-duct-
static-pressure input added to close the RTU/AHU -> VAV airflow coupling
gap found during system integration testing (see
tests/integration/SYSTEM_INTEGRATION_REPORT.md).

Run core tests only (fast, CI default):
    pytest models/vav/tests -m core

Run extended tests:
    pytest models/vav/tests -m extended
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyfmi import load_fmu

VAV_DIR = Path(__file__).resolve().parent.parent
VAV_FMU = VAV_DIR / "SimpleVAVZone.fmu"

NAMES = ["TRoo", "TRooCooSet", "TRooHeaSet", "TSupAHU", "dpSup"]
ALL_OUTPUTS = ["VSup_flow", "TSup", "yDam", "yDam_actual", "yVal", "yVal_actual"]

# Nominal parameters mirroring SimpleVAVZone.mo's own defaults -- used only
# for invariant bounds and expected relations, not fed into the model.
P = dict(
    mCooAir_flow_nominal=0.6, mHeaAir_flow_nominal=0.3,
    dpAir=200.0, rhoAir=1.2,
)
# mCooAir_flow_nominal/1.2 -- the RoomVAV controller's own V_flow_nominal,
# and therefore the cooling-mode design/setpoint airflow the flow-feedback
# loop targets once enough upstream pressure is available.
V_FLOW_NOMINAL = P["mCooAir_flow_nominal"] / P["rhoAir"]


# ─── Helpers ─────────────────────────────────────────────────────────────

def c2k(celsius: float) -> float:
    return celsius + 273.15


def k2c(kelvin: float) -> float:
    return kelvin - 273.15


def load_vav_fmu(fmu_path: Path = VAV_FMU):
    if not fmu_path.exists():
        pytest.skip(f"{fmu_path} not found -- build it before running these tests")
    return load_fmu(str(fmu_path), kind="ME")


def const_rows(TRoo_c, TRooCooSet_c, TRooHeaSet_c, TSupAHU_c, dpSup, final_time):
    """A two-point trajectory holding every input constant for final_time seconds."""
    v = [c2k(TRoo_c), c2k(TRooCooSet_c), c2k(TRooHeaSet_c), c2k(TSupAHU_c), dpSup]
    return [[0.0] + v, [final_time] + v]


def simulate_vav(rows, fmu_path: Path = VAV_FMU, params: dict | None = None, ncp: int | None = None):
    """result_handling='memory' for the same reason as every other suite in
    this repo (see models/ahu/tests/test_ahu.py's simulate_ahu docstring):
    pyfmi's default 'file' mode overwrites a shared, model-name-derived
    .mat file across repeated simulations in one process."""
    fmu = load_vav_fmu(fmu_path)
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


def assert_global_invariants(res):
    tol = 1e-6
    yDam = get_series(res, "yDam")
    yDam_actual = get_series(res, "yDam_actual")
    yVal = get_series(res, "yVal")
    yVal_actual = get_series(res, "yVal_actual")
    VSup_flow = get_series(res, "VSup_flow")

    assert np.all(yDam >= -tol) and np.all(yDam <= 1 + tol), "0<=yDam<=1 violated"
    assert np.all(yDam_actual >= -tol) and np.all(yDam_actual <= 1 + tol), "0<=yDam_actual<=1 violated"
    assert np.all(yVal >= -tol) and np.all(yVal <= 1 + tol), "0<=yVal<=1 violated"
    assert np.all(yVal_actual >= -tol) and np.all(yVal_actual <= 1 + tol), "0<=yVal_actual<=1 violated"
    assert np.all(VSup_flow >= -1e-4), "VSup_flow materially negative"
    assert_finite(res)


# ─── V1: deadband / baseline (core) ─────────────────────────────────────────

@pytest.mark.core
def test_v1_deadband_baseline():
    """Zone temperature between heating and cooling setpoints -- neither
    coil should be demanded, but the terminal still delivers its
    minimum-position airflow (standard Guideline-36 VAV-reheat behavior,
    not zero flow -- a VAV box doesn't fully close in deadband)."""
    rows = const_rows(TRoo_c=21.5, TRooCooSet_c=23.0, TRooHeaSet_c=20.0,
                       TSupAHU_c=13.0, dpSup=200.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yVal") < 1e-3, "reheat should not be active in the deadband"
    assert get_final_value(res, "VSup_flow") > 0, "VAV box should not fully close in deadband"
    assert_close(get_final_value(res, "TSup"), c2k(13.0), abs_tol=0.1,
                 msg="TSup should equal TSupAHU with no reheat active")


# ─── V2: cooling demand (core) ──────────────────────────────────────────────

@pytest.mark.core
def test_v2_cooling_demand():
    rows = const_rows(TRoo_c=27.0, TRooCooSet_c=23.0, TRooHeaSet_c=20.0,
                       TSupAHU_c=13.0, dpSup=200.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yDam") > 0.5, "damper should open substantially under strong cooling demand"
    assert get_final_value(res, "VSup_flow") > 0.3, "airflow should rise well above minimum under cooling demand"
    assert get_final_value(res, "yVal") < 1e-3, "reheat should not be active while cooling"
    assert_close(get_final_value(res, "TSup"), c2k(13.0), abs_tol=0.1,
                 msg="TSup should equal TSupAHU with no reheat active")


# ─── V3: heating/reheat (core) ──────────────────────────────────────────────

@pytest.mark.core
def test_v3_heating_reheat():
    rows = const_rows(TRoo_c=17.0, TRooCooSet_c=23.0, TRooHeaSet_c=20.0,
                       TSupAHU_c=13.0, dpSup=200.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yVal") > 0.9, "reheat valve should be nearly/fully open under strong heating demand"
    assert get_final_value(res, "TSup") > c2k(13.0) + 5.0, "reheat should meaningfully warm the discharge air above TSupAHU"
    assert get_final_value(res, "VSup_flow") > 0, "airflow should not collapse to zero during reheat"


# ─── V4: damper actuator settles to its commanded position (core) ──────────

@pytest.mark.core
def test_v4_damper_actuator_settles():
    rows = const_rows(TRoo_c=27.0, TRooCooSet_c=23.0, TRooHeaSet_c=20.0,
                       TSupAHU_c=13.0, dpSup=200.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_close(get_final_value(res, "yDam_actual"), get_final_value(res, "yDam"),
                 abs_tol=0.01, msg="actual damper position should track its command at steady state")
    assert_close(get_final_value(res, "yVal_actual"), get_final_value(res, "yVal"),
                 abs_tol=0.01, msg="actual valve position should track its command at steady state")


# ─── V5: FMU interface regression (core) ────────────────────────────────────

@pytest.mark.core
def test_v5_fmu_interface_regression():
    import zipfile
    from xml.etree import ElementTree as ET

    if not VAV_FMU.exists():
        pytest.skip(f"{VAV_FMU} not found -- build it before running these tests")

    expected_inputs = {"TRoo", "TRooCooSet", "TRooHeaSet", "TSupAHU", "dpSup"}
    expected_outputs = set(ALL_OUTPUTS)

    with zipfile.ZipFile(VAV_FMU) as zf:
        with zf.open("modelDescription.xml") as f:
            root = ET.parse(f).getroot()

    actual_inputs, actual_outputs = set(), set()
    units = {}
    for el in root.find("ModelVariables"):
        name = el.get("name")
        causality = el.get("causality")
        if causality == "input":
            actual_inputs.add(name)
        elif causality == "output":
            actual_outputs.add(name)
        type_el = next((c for c in el if c.tag in ("Real", "Integer", "Boolean", "String")), None)
        if type_el is not None:
            units[name] = type_el.get("unit")

    assert actual_inputs == expected_inputs, f"input set mismatch: {actual_inputs} != {expected_inputs}"
    assert actual_outputs == expected_outputs, f"output set mismatch: {actual_outputs} != {expected_outputs}"
    assert units.get("dpSup") == "Pa"
    assert units.get("TRoo") == "K"
    assert units.get("yDam") == "1"


@pytest.mark.core
def test_v5b_model_json_metadata_matches_fmu():
    from shared.validate_fmu_metadata import (
        extract_io_variables, load_metadata, load_model_description, validate,
    )

    metadata_path = VAV_DIR / "model.json"
    if not VAV_FMU.exists() or not metadata_path.exists():
        pytest.skip("SimpleVAVZone.fmu or model.json not found -- build/create before running this test")

    fmu_vars = extract_io_variables(load_model_description(VAV_FMU))
    metadata = load_metadata(metadata_path)
    result = validate(fmu_vars, metadata)

    assert not result["missing_inputs"], f"FMU inputs missing from model.json: {result['missing_inputs']}"
    assert not result["missing_outputs"], f"FMU outputs missing from model.json: {result['missing_outputs']}"
    assert not result["stale_inputs"], f"model.json inputs no longer in the FMU: {result['stale_inputs']}"
    assert not result["stale_outputs"], f"model.json outputs no longer in the FMU: {result['stale_outputs']}"


# ─── V6: dpSup pressure sweep -- airflow responds to upstream pressure (core) ──

@pytest.mark.core
def test_v6_pressure_sweep_airflow_responds():
    """Fixed strong cooling demand (so the flow setpoint is well above the
    minimum position) swept across dpSup=0/50/100/150/200/400 Pa.

    Verified empirically (not assumed) before writing this test: airflow
    rises strictly with pressure while the terminal is pressure-limited
    (damper pinned near fully open, unable to reach its flow setpoint), then
    plateaus once dpSup is high enough for RoomVAV's own flow-feedback loop
    to hit its design flow setpoint exactly (~0.5 m3/s = mCooAir_flow_nominal
    /rhoAir) and throttle the damper closed to hold it there -- real VAV
    terminal-box behavior (a well-tuned VAV controller is pressure-
    independent once it has enough authority), not a bug. So the assertion
    is monotonic *non-decreasing* airflow across the whole sweep, plus a
    strict increase confirmed in the pressure-limited low range."""
    dps = [0.0, 50.0, 100.0, 150.0, 200.0, 400.0]
    flows = []
    for dp in dps:
        rows = const_rows(27.0, 23.0, 20.0, 13.0, dp, 7200.0)
        res = simulate_vav(rows)
        assert_global_invariants(res)
        flows.append(get_final_value(res, "VSup_flow"))

    assert all(flows[i] <= flows[i + 1] + 1e-6 for i in range(len(flows) - 1)), \
        f"VSup_flow must be monotonically non-decreasing with dpSup: {list(zip(dps, flows))}"
    # Strict increase confirmed in the pressure-limited regime (0->150 Pa).
    assert flows[3] > flows[0] + 0.3, \
        f"airflow did not meaningfully increase in the pressure-limited range: {list(zip(dps[:4], flows[:4]))}"
    # Plateau near the design flow setpoint once pressure is sufficient.
    assert_close(flows[-1], V_FLOW_NOMINAL, rel_tol=0.02,
                 msg="airflow should settle near the design flow setpoint at high dpSup")


# ─── V7: zero pressure -- numerically stable, no airflow (core) ────────────

@pytest.mark.core
def test_v7_zero_pressure_stable():
    rows = const_rows(27.0, 23.0, 20.0, 13.0, dpSup=0.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    assert abs(get_final_value(res, "VSup_flow")) < 1e-4, \
        "VSup_flow should approach zero with no available upstream pressure"
    # The controller saturates the damper trying (and failing) to reach its
    # flow setpoint -- fully open, not a crash or an unphysical value.
    assert get_final_value(res, "yDam_actual") > 0.9


@pytest.mark.core
def test_v7b_negative_pressure_clamped():
    """dpSup is clamped via max(0, dpSup) in the model -- a negative value
    (never expected from a real RTU/AHU, but not impossible from a stale/
    noisy point) must not crash or produce an unphysical negative driving
    pressure; it should behave identically to dpSup=0."""
    rows_neg = const_rows(27.0, 23.0, 20.0, 13.0, dpSup=-50.0, final_time=3600.0)
    rows_zero = const_rows(27.0, 23.0, 20.0, 13.0, dpSup=0.0, final_time=3600.0)
    res_neg = simulate_vav(rows_neg)
    res_zero = simulate_vav(rows_zero)
    assert_finite(res_neg)
    assert_close(get_final_value(res_neg, "VSup_flow"), get_final_value(res_zero, "VSup_flow"),
                 abs_tol=1e-6, msg="negative dpSup should clamp to the same result as dpSup=0")


# ─── V8: high pressure -- stable, reasonable airflow (core) ────────────────

@pytest.mark.core
def test_v8_high_pressure_stable():
    rows = const_rows(27.0, 23.0, 20.0, 13.0, dpSup=500.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    flow = get_final_value(res, "VSup_flow")
    # Reasonable, not exploding: at/near the design flow setpoint, not some
    # multiple of it.
    assert flow <= V_FLOW_NOMINAL * 1.05, f"VSup_flow={flow} exceeded the design flow setpoint unreasonably at high dpSup"
    assert_close(flow, V_FLOW_NOMINAL, rel_tol=0.02)


# ═══════════════════════════════════════════════════════════════════════════
# EXTENDED -- slower sweeps/stability/regression tests.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.extended
def test_six_hour_stability():
    rows = const_rows(27.0, 23.0, 20.0, 13.0, dpSup=200.0, final_time=21600.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    assert_finite(res)


@pytest.mark.extended
def test_very_high_pressure_no_blowup():
    rows = const_rows(27.0, 23.0, 20.0, 13.0, dpSup=2000.0, final_time=7200.0)
    res = simulate_vav(rows)
    assert_global_invariants(res)
    flow = get_final_value(res, "VSup_flow")
    assert flow <= V_FLOW_NOMINAL * 1.1, f"VSup_flow={flow} blew up at an extreme dpSup"


@pytest.mark.extended
def test_dpsup_step_response():
    base = [c2k(27.0), c2k(23.0), c2k(20.0), c2k(13.0), 50.0]
    rows = [[0.0] + base, [1800.0] + base]
    step = base.copy(); step[4] = 400.0
    rows += [[1800.05] + step, [5400.0] + step]
    res = simulate_vav(rows, ncp=180)
    assert_global_invariants(res)
    flow_before = float(np.interp(1800.0, res["time"], get_series(res, "VSup_flow")))
    flow_after = get_final_value(res, "VSup_flow")
    assert flow_after > flow_before, "airflow should rise in response to a dpSup step increase"


@pytest.mark.extended
def test_heating_pressure_sweep():
    """The heating-mode minimum-flow position is much lower authority-wise
    than cooling-mode, so it should already be reachable at fairly low
    pressure -- documents that behavior rather than assuming it."""
    dps = [0.0, 50.0, 100.0, 200.0]
    flows = []
    for dp in dps:
        rows = const_rows(17.0, 23.0, 20.0, 13.0, dp, 7200.0)
        res = simulate_vav(rows)
        assert_global_invariants(res)
        flows.append(get_final_value(res, "VSup_flow"))
    assert all(flows[i] <= flows[i + 1] + 1e-6 for i in range(len(flows) - 1)), \
        f"heating-mode VSup_flow must be monotonically non-decreasing with dpSup: {list(zip(dps, flows))}"
