"""AHU.mo regression suite -- exercises the exported AHU.fmu via pyfmi in
Model Exchange mode, matching shared/runtime/models/manager.py's production
calling convention (fmu.simulate(input=(names, trajectory))), not a raw
FMI co-simulation doStep() loop.

This suite is a direct port of the scratch harness used to produce the
40-test verification report (models/ahu/tests/readme). It intentionally
does not add, remove, or loosen physics assertions from that verified run --
see README.md for what "core" vs "extended" means and for the two known,
documented model behaviors that are deliberately NOT asserted as failures
(TChiWatRet at zero CHW flow, mAir_flow_nominal FMU override).

Run core tests only (fast, CI default):
    pytest models/ahu/tests -m core

Run extended tests (sweeps, six-hour runs, SimpleAHU regression):
    pytest models/ahu/tests -m extended
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyfmi import load_fmu

AHU_DIR = Path(__file__).resolve().parent.parent
AHU_FMU = AHU_DIR / "AHU.fmu"
# SimpleAHU.mo/.fmu lives in a sibling models/ahu-simple/ folder, not
# alongside the new AHU.mo. See AHU.mo's own header.
SIMPLE_FMU = AHU_DIR.parent / "ahu-simple" / "SimpleAHU.fmu"

NAMES = ["TSupSet", "TOut", "TRet", "uFan", "uVAVDamMax", "TChiWatSup"]

ALL_OUTPUTS = [
    "TSup", "TMix", "VSup_flow", "yFan", "yOutDam", "yHeaVal", "yCooVal",
    "cooCapacityFactor", "QCoolLoad", "QHeaLoad", "TChiWatRet",
    "VChiWat_flow", "PSupFan", "dpSup", "dpSupSet",
]

# Nominal parameters mirroring AHU.mo's own defaults -- used only for
# invariant bounds and expected fan-law relations, not fed into the model.
P = dict(
    vavDamLow=0.60, vavDamHigh=0.90,
    dpSupSetMin=200.0, dpSupSetMax=500.0, dpSup_nominal=500.0,
    PSupFan_nominal=5000.0, minOutAirFra=0.15,
    rhoWat=998.0, cpWat=4180.0,
)

# Below this CHW volumetric flow, TChiWatRet is not asserted -- see README
# "Known behavior: TChiWatRet at zero CHW flow".
CHW_FLOW_EPS = 1e-6


# ─── Helpers ─────────────────────────────────────────────────────────────

def c2k(celsius: float) -> float:
    return celsius + 273.15


def load_ahu_fmu(fmu_path: Path = AHU_FMU):
    if not fmu_path.exists():
        pytest.skip(f"{fmu_path} not found -- build it before running these tests")
    return load_fmu(str(fmu_path), kind="ME")


def const_rows(TSupSet_c, TOut_c, TRet_c, uFan, uVAVDamMax, TChiWatSup_c, final_time):
    """A two-point trajectory holding every input constant for final_time seconds."""
    v = [c2k(TSupSet_c), c2k(TOut_c), c2k(TRet_c), uFan, uVAVDamMax, c2k(TChiWatSup_c)]
    return [[0.0] + v, [final_time] + v]


def simulate_ahu(rows, fmu_path: Path = AHU_FMU, params: dict | None = None, ncp: int | None = None):
    """rows: [[t, TSupSet_K, TOut_K, TRet_K, uFan, uVAVDamMax, TChiWatSup_K], ...].

    result_handling='memory' is required, not optional. pyfmi's default
    result_handling='file' writes each simulation's trajectory to a shared,
    model-name-derived .mat file on disk; running this model repeatedly in
    one process (as this suite does, dozens of times) means a later run
    silently overwrites an earlier run's result file before it's read back,
    raising "result file has been modified" on lazy access to the earlier
    ResultObject. This produced five false failures during the original
    verification pass and must not be reintroduced.
    """
    fmu = load_ahu_fmu(fmu_path)
    if params:
        for k, v in params.items():
            fmu.set(k, v)
    fmu.set_log_level(3)
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


def assert_global_invariants(res, dpSupSetMin=P["dpSupSetMin"], dpSupSetMax=P["dpSupSetMax"],
                              minOutAirFra=P["minOutAirFra"]):
    """The section-K sanity checks from models/ahu/tests/readme, applied to a
    full trajectory rather than just the final point."""
    tol = 1e-6
    yFan = get_series(res, "yFan")
    yOutDam = get_series(res, "yOutDam")
    yHea = get_series(res, "yHeaVal")
    yCoo = get_series(res, "yCooVal")
    ccf = get_series(res, "cooCapacityFactor")
    dpSup = get_series(res, "dpSup")
    dpSupSet = get_series(res, "dpSupSet")
    PFan = get_series(res, "PSupFan")
    VChi = get_series(res, "VChiWat_flow")

    assert np.all(yFan >= -tol) and np.all(yFan <= 1 + tol), "0<=yFan<=1 violated"
    assert np.all(yOutDam >= minOutAirFra - 1e-4) and np.all(yOutDam <= 1 + tol), \
        "minOutAirFra<=yOutDam<=1 violated"
    assert np.all(yHea >= -tol) and np.all(yHea <= 1 + tol), "0<=yHeaVal<=1 violated"
    assert np.all(yCoo >= -tol) and np.all(yCoo <= 1 + tol), "0<=yCooVal<=1 violated"
    assert np.all(ccf >= -tol) and np.all(ccf <= 1 + tol), "0<=cooCapacityFactor<=1 violated"
    assert np.all(dpSup >= -1e-3), "dpSup>=0 violated"
    assert np.all(dpSupSet >= dpSupSetMin - 1e-3) and np.all(dpSupSet <= dpSupSetMax + 1e-3), \
        "dpSupSetMin<=dpSupSet<=dpSupSetMax violated"
    assert np.all(PFan >= -1e-3), "PSupFan>=0 violated"
    assert np.all(VChi >= -1e-6), "VChiWat_flow materially negative"
    assert_finite(res)


# ─── 1. FMU initialization (core) ──────────────────────────────────────────

@pytest.mark.core
def test_cold_start_initialization():
    rows = const_rows(TSupSet_c=13, TOut_c=30, TRet_c=24, uFan=0, uVAVDamMax=0.5,
                       TChiWatSup_c=7, final_time=300.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert abs(get_final_value(res, "yFan")) < 1e-6
    assert abs(get_final_value(res, "yHeaVal")) < 1e-6
    assert abs(get_final_value(res, "yCooVal")) < 1e-6
    assert abs(get_final_value(res, "VSup_flow")) < 1e-6
    assert abs(get_final_value(res, "QCoolLoad")) < 1e-6
    assert abs(get_final_value(res, "QHeaLoad")) < 1e-6


# ─── 2. Fan startup transient (core) ────────────────────────────────────────

@pytest.mark.core
def test_fan_startup_transient_no_valve_open_excursion():
    """Protects the intentional y_start=0 setting on valCooCoi -- see AHU.mo.
    The component's own default (y_start=1, fully open) let the CHW valve
    dump design-capacity cooling into the air stream before the SAT loop
    caught up, which crashed the mixing volume's energy balance in the
    original verification run."""
    base = [c2k(13), c2k(30), c2k(24), 0, 0.5, c2k(7)]
    rows = [[0.0] + base, [60.0] + base]
    step = base.copy(); step[3] = 1
    rows += [[60.05] + step, [3600.0] + step]
    res = simulate_ahu(rows, ncp=240)
    assert_global_invariants(res)
    tsup = get_series(res, "TSup")
    tmix = get_series(res, "TMix")
    assert np.all(tsup > 150) and np.all(tsup < 400), "TSup left the medium's valid range"
    assert np.all(tmix > 150) and np.all(tmix < 400), "TMix left the medium's valid range"
    assert_close(get_final_value(res, "TSup"), c2k(13), abs_tol=0.5, msg="TSup should settle at setpoint")


# ─── 3. Normal cooling (core) ───────────────────────────────────────────────

@pytest.mark.core
def test_normal_cooling():
    rows = const_rows(TSupSet_c=13, TOut_c=30, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                       TChiWatSup_c=7, final_time=21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yCooVal") > 0
    assert get_final_value(res, "VChiWat_flow") > 0
    assert get_final_value(res, "TChiWatRet") > c2k(7)
    assert get_final_value(res, "QCoolLoad") > 0
    assert get_final_value(res, "TSup") < get_final_value(res, "TMix")
    assert abs(get_final_value(res, "yHeaVal")) < 1e-3


# ─── 4. CHW energy balance (core) ───────────────────────────────────────────

@pytest.mark.core
def test_chw_energy_balance():
    """The purpose of AHU.mo is a physically coupled CHW/air side -- this is
    the test that actually verifies that coupling, not just that the
    outputs exist. 2% relative-error tolerance; the verified operating
    point measured ~0.15%."""
    rows = const_rows(TSupSet_c=13, TOut_c=30, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                       TChiWatSup_c=7, final_time=21600.0)
    res = simulate_ahu(rows)
    QCoolLoad = get_final_value(res, "QCoolLoad")
    VChiWat_flow = get_final_value(res, "VChiWat_flow")
    TChiWatRet = get_final_value(res, "TChiWatRet")
    Q_balance = P["rhoWat"] * VChiWat_flow * P["cpWat"] * (TChiWatRet - c2k(7))
    assert_close(QCoolLoad, Q_balance, rel_tol=0.02,
                 msg="QCoolLoad vs rho*V*cp*dT energy balance")


# ─── 5. Cooling off (core) ───────────────────────────────────────────────────

@pytest.mark.core
def test_cooling_demand_removed():
    """Does NOT assert TChiWatRet here -- see README 'Known behavior:
    TChiWatRet at zero CHW flow'. At near-zero flow the sensor has nothing
    moving through it to carry the supply temperature forward, so it can
    hold a stale/default value indefinitely. VChiWat_flow and QCoolLoad are
    the physically meaningful signals when the coil is idle."""
    rows = const_rows(TSupSet_c=37, TOut_c=12, TRet_c=22, uFan=1, uVAVDamMax=0.8,
                       TChiWatSup_c=7, final_time=21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yCooVal") < 1e-6
    assert get_final_value(res, "VChiWat_flow") < 1e-6
    assert get_final_value(res, "QCoolLoad") < 1e-3
    # Deliberately no assertion on TChiWatRet here.


# ─── 6. Heating + mutual exclusion (core) ───────────────────────────────────

@pytest.mark.core
def test_heating_demand():
    rows = const_rows(TSupSet_c=30, TOut_c=5, TRet_c=18, uFan=1, uVAVDamMax=0.8,
                       TChiWatSup_c=7, final_time=21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yHeaVal") > 0
    assert get_final_value(res, "QHeaLoad") > 0
    assert abs(get_final_value(res, "yCooVal")) < 1e-3
    assert get_final_value(res, "TSup") > get_final_value(res, "TMix")


@pytest.mark.core
def test_heating_cooling_mutual_exclusion():
    heating_rows = const_rows(30, 5, 18, 1, 0.8, 7, 21600.0)
    cooling_rows = const_rows(13, 30, 25, 1, 0.8, 7, 21600.0)
    worst = 0.0
    for rows in (heating_rows, cooling_rows):
        res = simulate_ahu(rows)
        prod = get_series(res, "yHeaVal") * get_series(res, "yCooVal")
        worst = max(worst, float(np.max(np.abs(prod))))
    assert worst < 1e-4, f"yHeaVal*yCooVal reached {worst}, coils operated simultaneously"


# ─── 7. Economizer (core) ───────────────────────────────────────────────────

@pytest.mark.core
def test_economizer_favorable_outdoor_air():
    rows = const_rows(13, 10, 24, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yOutDam") > P["minOutAirFra"]


@pytest.mark.core
def test_economizer_unfavorable_outdoor_air():
    rows = const_rows(13, 30, 24, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert_close(get_final_value(res, "yOutDam"), P["minOutAirFra"], abs_tol=1e-6)


@pytest.mark.core
def test_economizer_disabled():
    rows = const_rows(13, 10, 24, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows, params={"haveEconomizer": False})
    assert_global_invariants(res)
    assert_close(get_final_value(res, "yOutDam"), P["minOutAirFra"], abs_tol=1e-6)


# ─── 8. VAV static-pressure reset (core) ────────────────────────────────────

@pytest.mark.core
def test_vav_static_pressure_reset():
    points = [(0.5, 200.0), (0.75, 350.0), (0.95, 500.0)]
    results = []
    for uVAVDamMax, expected_dp in points:
        rows = const_rows(13, 30, 25, 1, uVAVDamMax, 7, 21600.0)
        res = simulate_ahu(rows)
        dp = get_final_value(res, "dpSupSet")
        results.append(dp)
        assert_close(dp, expected_dp, abs_tol=1.0,
                     msg=f"dpSupSet at uVAVDamMax={uVAVDamMax}")
    assert all(results[i] <= results[i + 1] for i in range(len(results) - 1)), \
        "dpSupSet must be monotonically non-decreasing with uVAVDamMax"


# ─── 9. Fan laws (core) ─────────────────────────────────────────────────────

@pytest.mark.core
def test_fan_pressure_and_power_laws():
    rows = const_rows(13, 30, 25, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows)
    yFan = get_final_value(res, "yFan")
    dpSup = get_final_value(res, "dpSup")
    PFan = get_final_value(res, "PSupFan")
    assert_close(dpSup, P["dpSup_nominal"] * yFan ** 2, rel_tol=1e-6,
                 msg="dpSup = dpSup_nominal * yFan^2")
    assert_close(PFan, P["PSupFan_nominal"] * yFan ** 3, rel_tol=1e-6,
                 msg="PSupFan = PSupFan_nominal * yFan^3")


# ─── 10. Fan off (core) ─────────────────────────────────────────────────────

@pytest.mark.core
def test_fan_off():
    rows = const_rows(13, 30, 25, 0, 0.9, 7, 21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    for name in ("yFan", "dpSup", "PSupFan", "VSup_flow", "yHeaVal", "yCooVal"):
        assert abs(get_final_value(res, name)) < 1e-6, f"{name} should be 0 with fan off"


# ─── 11. Mixed-air sanity (core) ────────────────────────────────────────────

@pytest.mark.core
def test_mixed_air_temperature_bounds():
    rows = const_rows(13, 30, 24, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    tmix = get_final_value(res, "TMix")
    lo, hi = min(c2k(30), c2k(24)), max(c2k(30), c2k(24))
    assert lo - 1e-3 <= tmix <= hi + 1e-3


# ─── 12. FMU interface regression (core) ────────────────────────────────────

@pytest.mark.core
def test_fmu_interface_regression():
    import zipfile
    from xml.etree import ElementTree as ET

    if not AHU_FMU.exists():
        pytest.skip(f"{AHU_FMU} not found -- build it before running these tests")

    expected_inputs = {"TSupSet", "TOut", "TRet", "uFan", "uVAVDamMax", "TChiWatSup"}
    expected_outputs = {
        "TSup", "TMix", "VSup_flow", "yFan", "yOutDam", "yHeaVal", "yCooVal",
        "cooCapacityFactor", "QCoolLoad", "QHeaLoad", "TChiWatRet",
        "VChiWat_flow", "PSupFan", "dpSup", "dpSupSet",
    }

    with zipfile.ZipFile(AHU_FMU) as zf:
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

    missing_in = expected_inputs - actual_inputs
    extra_in = actual_inputs - expected_inputs
    missing_out = expected_outputs - actual_outputs
    extra_out = actual_outputs - expected_outputs

    assert not missing_in, f"missing FMU inputs: {missing_in}"
    assert not extra_in, f"unexpected FMU inputs: {extra_in}"
    assert not missing_out, f"missing FMU outputs: {missing_out}"
    assert not extra_out, f"unexpected FMU outputs: {extra_out}"

    assert units.get("QCoolLoad") == "W"
    assert units.get("TSupSet") == "K"
    assert units.get("yFan") == "1"


@pytest.mark.core
def test_model_json_metadata_matches_fmu():
    """models/ahu/model.json declares AHU's inputs/outputs for the runtime
    catalog and BACnet mapping -- reuses the repo's own validator so this
    suite catches interface drift the same way build-ahu-v2-fmu.yml's
    "Validate & sync AHU FMU metadata" CI step does."""
    from shared.validate_fmu_metadata import (
        extract_io_variables, load_metadata, load_model_description, validate,
    )

    metadata_path = AHU_DIR / "model.json"
    if not AHU_FMU.exists() or not metadata_path.exists():
        pytest.skip("AHU.fmu or model.json not found -- build/create before running this test")

    fmu_vars = extract_io_variables(load_model_description(AHU_FMU))
    metadata = load_metadata(metadata_path)
    result = validate(fmu_vars, metadata)

    assert not result["missing_inputs"], f"FMU inputs missing from model.json: {result['missing_inputs']}"
    assert not result["missing_outputs"], f"FMU outputs missing from model.json: {result['missing_outputs']}"
    assert not result["stale_inputs"], f"model.json inputs no longer in the FMU: {result['stale_inputs']}"
    assert not result["stale_outputs"], f"model.json outputs no longer in the FMU: {result['stale_outputs']}"
    # Causality mismatches are a warning in validate_fmu_metadata.py's own
    # convention (a field flipping direction can be a legitimate redesign),
    # not asserted here for the same reason.


# ═══════════════════════════════════════════════════════════════════════════
# EXTENDED -- slower sweeps/stability/parameter/regression tests.
# Not run by default CI; use `pytest models/ahu/tests -m extended` or the
# workflow's workflow_dispatch "extended" input.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.extended
def test_six_hour_stability():
    rows = const_rows(13, 30, 25, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows)
    assert_global_invariants(res)
    assert_finite(res)


@pytest.mark.extended
def test_warm_chilled_water_sweep():
    chws_sweep = [7, 10, 13, 16, 18]
    ccf_values, yCoo_values = [], []
    for chws in chws_sweep:
        rows = const_rows(13, 30, 25, 1, 0.8, chws, 21600.0)
        res = simulate_ahu(rows)
        ccf_values.append(get_final_value(res, "cooCapacityFactor"))
        yCoo_values.append(get_final_value(res, "yCooVal"))
    assert all(ccf_values[i] >= ccf_values[i + 1] - 1e-9 for i in range(len(ccf_values) - 1)), \
        "cooCapacityFactor should be monotonically non-increasing as CHWS warms"
    # No double-derating: the valve should open WIDER (not shrink in lockstep
    # with cooCapacityFactor) as capacity drops -- see AHU.mo's cooling
    # section comment.
    assert yCoo_values[-1] >= yCoo_values[0] - 1e-6


@pytest.mark.extended
def test_chws_above_entering_air_temperature():
    """Documents, does not fix, the inverted-heat-transfer edge case -- see
    README 'Known behavior: hot "chilled" water'."""
    rows = const_rows(13, 30, 25, 1, 0.8, 35, 21600.0)
    res = simulate_ahu(rows)
    assert_finite(res)  # no crash is the actual requirement here
    assert_close(get_final_value(res, "cooCapacityFactor"), 0.0, abs_tol=1e-6)
    assert abs(get_final_value(res, "QCoolLoad")) < 40000  # not an absurd magnitude


@pytest.mark.extended
def test_full_economizer_saturation():
    rows = const_rows(4, 5, 24, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows)
    assert get_final_value(res, "yOutDam") > 0.999
    assert_close(get_final_value(res, "TMix"), c2k(5), abs_tol=1.0)


@pytest.mark.extended
def test_static_pressure_reset_full_sweep():
    sweep = [0.0, 0.2, 0.4, 0.6, 0.75, 0.9, 0.95, 1.0]
    values = []
    for v in sweep:
        rows = const_rows(13, 30, 25, 1, v, 7, 21600.0)
        res = simulate_ahu(rows)
        values.append(get_final_value(res, "dpSupSet"))
    assert all(values[i] <= values[i + 1] + 1e-6 for i in range(len(values) - 1))
    assert all(P["dpSupSetMin"] - 1e-3 <= v <= P["dpSupSetMax"] + 1e-3 for v in values)


@pytest.mark.extended
def test_alternate_chw_flow_nominal():
    rows = const_rows(13, 30, 25, 1, 0.8, 7, 21600.0)
    res_default = simulate_ahu(rows)
    res_alt = simulate_ahu(rows, params={"VChiWat_flow_nominal": 0.001})
    assert_finite(res_alt)
    assert get_final_value(res_alt, "VChiWat_flow") < get_final_value(res_default, "VChiWat_flow")


@pytest.mark.extended
def test_alternate_cooling_capacity():
    rows = const_rows(13, 30, 25, 1, 0.8, 7, 21600.0)
    res = simulate_ahu(rows, params={"QCoo_flow_nominal": -20000.0})
    assert_finite(res)


@pytest.mark.extended
def test_mair_flow_nominal_override_is_a_known_limitation():
    """Documents, does not attempt to fix, a genuine OpenModelica FMU-export
    limitation found during verification: overriding mAir_flow_nominal away
    from its compiled default (2.0) fails FMU initialization, even for a 5%
    change. See README 'Known behavior: mAir_flow_nominal FMU override'.
    This test asserts the DOCUMENTED failure mode, so it would itself fail
    (loudly) if a future OM/Buildings upgrade silently fixes or worsens it --
    that's the point, not a bug to chase in this task."""
    rows = const_rows(13, 30, 25, 1, 0.8, 7, 600.0)
    with pytest.raises(Exception):
        simulate_ahu(rows, params={"mAir_flow_nominal": 1.9})


@pytest.mark.extended
def test_reversed_static_pressure_config_documented():
    rows = const_rows(13, 30, 25, 1, 0.95, 7, 21600.0)
    res = simulate_ahu(rows, params={"dpSupSetMin": 500.0, "dpSupSetMax": 200.0})
    assert_finite(res)  # no crash; the reset curve is simply inverted -- documented, not fixed


@pytest.mark.extended
def test_reversed_vav_thresholds_documented():
    rows = const_rows(13, 30, 25, 1, 0.7, 7, 21600.0)
    res = simulate_ahu(rows, params={"vavDamLow": 0.9, "vavDamHigh": 0.6})
    assert_finite(res)  # denominator guard prevents div-by-zero; curve is inverted, documented


@pytest.mark.extended
def test_regression_against_simple_ahu():
    if not SIMPLE_FMU.exists():
        pytest.skip(f"{SIMPLE_FMU} not found")
    rows = const_rows(13, 30, 25, 1, 0.8, 7, 21600.0)
    res_ahu = simulate_ahu(rows, fmu_path=AHU_FMU)
    res_simple = simulate_ahu(rows, fmu_path=SIMPLE_FMU)
    # Control-behavior signals should track closely -- CHW-side signals are
    # intentionally decoupled and NOT compared here (see models/ahu/AHU.mo
    # header for why).
    for name in ("TSup", "TMix", "VSup_flow", "yFan", "dpSup", "dpSupSet", "PSupFan"):
        assert_close(get_final_value(res_ahu, name), get_final_value(res_simple, name),
                     rel_tol=0.15, msg=f"{name} regression vs SimpleAHU")


@pytest.mark.extended
@pytest.mark.parametrize("step_input,before_c,after_c,time_key", [
    ("TSupSet", 16, 13, None),
])
def test_sat_setpoint_step(step_input, before_c, after_c, time_key):
    base = [c2k(before_c), c2k(30), c2k(25), 1, 0.8, c2k(7)]
    rows = [[0.0] + base, [600.0] + base]
    step = base.copy(); step[0] = c2k(after_c)
    rows += [[600.05] + step, [3600.0] + step]
    res = simulate_ahu(rows, ncp=240)
    assert_global_invariants(res)
    yCoo_before = float(np.interp(600.0, res["time"], get_series(res, "yCooVal")))
    yCoo_after = get_final_value(res, "yCooVal")
    assert yCoo_after > yCoo_before
    assert_close(get_final_value(res, "TSup"), c2k(after_c), abs_tol=0.5)


@pytest.mark.extended
def test_outdoor_temperature_step():
    base = [c2k(13), c2k(30), c2k(24), 1, 0.8, c2k(7)]
    rows = [[0.0] + base, [600.0] + base]
    step = base.copy(); step[1] = c2k(15)
    rows += [[600.05] + step, [3600.0] + step]
    res = simulate_ahu(rows, ncp=240)
    assert_global_invariants(res)
    yod_before = float(np.interp(600.0, res["time"], get_series(res, "yOutDam")))
    assert get_final_value(res, "yOutDam") >= yod_before - 1e-6


@pytest.mark.extended
def test_return_temperature_step():
    base = [c2k(13), c2k(20), c2k(24), 1, 0.8, c2k(7)]
    rows = [[0.0] + base, [600.0] + base]
    step = base.copy(); step[2] = c2k(30)
    rows += [[600.05] + step, [3600.0] + step]
    res = simulate_ahu(rows, ncp=240)
    assert_global_invariants(res)


@pytest.mark.extended
def test_vav_demand_step():
    base = [c2k(13), c2k(30), c2k(25), 1, 0.5, c2k(7)]
    rows = [[0.0] + base, [600.0] + base]
    step = base.copy(); step[4] = 0.95
    rows += [[600.05] + step, [3600.0] + step]
    res = simulate_ahu(rows, ncp=240)
    dpss_before = float(np.interp(600.0, res["time"], get_series(res, "dpSupSet")))
    assert get_final_value(res, "dpSupSet") > dpss_before


@pytest.mark.extended
def test_chws_temperature_step_during_cooling():
    base = [c2k(13), c2k(30), c2k(25), 1, 0.8, c2k(7)]
    rows = [[0.0] + base, [600.0] + base]
    step = base.copy(); step[5] = c2k(14)
    rows += [[600.05] + step, [3600.0] + step]
    res = simulate_ahu(rows, ncp=240)
    assert_global_invariants(res)  # no numerical failure is the requirement; degradation is expected
