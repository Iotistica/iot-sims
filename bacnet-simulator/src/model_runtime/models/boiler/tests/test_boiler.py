"""BoilerPlant.mo regression suite -- exercises the exported BoilerPlant.fmu
via pyfmi in Model Exchange mode, matching shared/runtime/models/manager.py's
production calling convention.

Mirrors models/ahu/tests/test_ahu.py's conventions (result_handling='memory',
core/extended pytest markers, helpers named the same way), with one addition
required by BoilerPlant's own interface: uBoi1/uBoi2/uPum1/uPum2 are Boolean
FMU inputs, and FMI2 forbids driving non-Real variables through the
continuous simulate(input=...) trajectory once the model is past
initialization -- see simulate_boiler()'s docstring, and
shared/runtime/models/manager.py's _split_input_variables/_apply_discrete_inputs,
which this suite's split between REAL_NAMES/BOOL_NAMES follows exactly.

Run core tests only (fast, CI default):
    pytest models/boiler/tests -m core

Run extended tests (sweeps, six-hour run, step responses):
    pytest models/boiler/tests -m extended
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyfmi import load_fmu

BOILER_DIR = Path(__file__).resolve().parent.parent
BOILER_FMU = BOILER_DIR / "BoilerPlant.fmu"

REAL_NAMES = ["THotWatRet", "TOut", "VHotWat_flow"]
BOOL_NAMES = ["uBoi1", "uBoi2", "uPum1", "uPum2"]

ALL_OUTPUTS = [
    "THotWatSup", "THotWatSupSet", "dpHotWat", "QBoi1", "QBoi2", "QHeatDelivered",
    "PBoi1", "PBoi2", "etaBoi1", "etaBoi2", "PPum1", "PPum2", "plantPLR",
    "flowFraction", "availableHeatingCapacity",
]

# Nominal parameters mirroring BoilerPlant.mo's own defaults -- used only for
# invariant bounds, not fed into the model.
P = dict(
    QBoi_nominal=500000.0,
    eta_min=0.75, eta_max=0.98,
    rhoWat=998.0, cpWat=4180.0,
)


# ─── Helpers ─────────────────────────────────────────────────────────────

def c2k(celsius: float) -> float:
    return celsius + 273.15


def load_boiler_fmu(fmu_path: Path = BOILER_FMU):
    if not fmu_path.exists():
        pytest.skip(f"{fmu_path} not found -- build it before running these tests")
    return load_fmu(str(fmu_path), kind="ME")


def const_rows(TRet_c, TOut_c, VHotWat_flow, uBoi1, uBoi2, uPum1, uPum2, final_time):
    """A two-point trajectory for the 3 Real inputs, holding constant for
    final_time seconds; Booleans are returned separately for simulate_boiler
    to apply via fmu.set()."""
    v = [c2k(TRet_c), c2k(TOut_c), VHotWat_flow]
    rows = [[0.0] + v, [final_time] + v]
    bools = [uBoi1, uBoi2, uPum1, uPum2]
    return rows, bools


def simulate_boiler(rows, bools, fmu_path: Path = BOILER_FMU, params: dict | None = None,
                     ncp: int | None = None):
    """rows: [[t, THotWatRet_K, TOut_K, VHotWat_flow], ...] (Real inputs only).
    bools: [uBoi1, uBoi2, uPum1, uPum2] -- applied once via fmu.set() before
    simulate(), per FMI2's own rule that discrete (Boolean/Integer) inputs
    cannot ride in the continuous input= trajectory past initialization.
    Held constant for the whole run, matching every test in this suite
    (none step a Boolean input mid-simulation).

    result_handling='memory' is required, not optional -- see
    models/ahu/tests/test_ahu.py's simulate_ahu() docstring for why
    (pyfmi's default 'file' mode silently overwrites a shared, model-name-
    derived .mat file across repeated simulations of the same FMU in one
    process, producing false "result file has been modified" failures).
    """
    fmu = load_boiler_fmu(fmu_path)
    if params:
        for k, v in params.items():
            fmu.set(k, v)
    for name, v in zip(BOOL_NAMES, bools):
        fmu.set(name, bool(v))
    fmu.set_log_level(3)
    trajectory = np.array(rows, dtype=float)
    final_time = float(rows[-1][0])
    options = fmu.simulate_options()
    options["ncp"] = ncp or max(2, int(final_time / 30.0))
    options["result_handling"] = "memory"
    return fmu.simulate(start_time=0.0, final_time=final_time,
                         input=(REAL_NAMES, trajectory), options=options)


def run(TRet_c, TOut_c, VHotWat_flow, uBoi1, uBoi2, uPum1, uPum2,
        final_time=21600.0, params=None, ncp=None):
    """Convenience wrapper: const_rows + simulate_boiler in one call, the
    shape most tests in this suite need."""
    rows, bools = const_rows(TRet_c, TOut_c, VHotWat_flow, uBoi1, uBoi2, uPum1, uPum2, final_time)
    return simulate_boiler(rows, bools, params=params, ncp=ncp)


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
    """Section 21 of the task spec, applied to a full trajectory rather than
    just the final point."""
    tol = 1e-6
    assert np.all(get_series(res, "QHeatDelivered") >= -tol), "QHeatDelivered>=0 violated"
    assert np.all(get_series(res, "QBoi1") >= -tol), "QBoi1>=0 violated"
    assert np.all(get_series(res, "QBoi2") >= -tol), "QBoi2>=0 violated"
    assert np.all(get_series(res, "PBoi1") >= -tol), "PBoi1>=0 violated"
    assert np.all(get_series(res, "PBoi2") >= -tol), "PBoi2>=0 violated"
    assert np.all(get_series(res, "PPum1") >= -tol), "PPum1>=0 violated"
    assert np.all(get_series(res, "PPum2") >= -tol), "PPum2>=0 violated"
    eta1 = get_series(res, "etaBoi1")
    eta2 = get_series(res, "etaBoi2")
    assert np.all(eta1 >= -tol) and np.all(eta1 <= 1 + tol), "0<=etaBoi1<=1 violated"
    assert np.all(eta2 >= -tol) and np.all(eta2 <= 1 + tol), "0<=etaBoi2<=1 violated"
    plr = get_series(res, "plantPLR")
    assert np.all(plr >= -tol) and np.all(plr <= 1 + tol), "0<=plantPLR<=1 violated"
    assert np.all(get_series(res, "dpHotWat") >= -tol), "dpHotWat>=0 violated"
    assert_finite(res)


def assert_eta_matches_q_over_p(res, boiler_num: int, *, rel_tol: float = 0.02,
                                 p_epsilon: float = 1.0):
    """QBoi = useful thermal output, PBoi = fuel input power, etaBoi is
    exposed directly from the boiler physics (boiler.eta) rather than
    computed as QBoi/PBoi -- this checks the two are actually consistent
    with each other, i.e. that etaBoi means what its name says. Skipped
    (not a pass) when PBoi is ~0 (boiler not firing), since QBoi/PBoi is
    undefined there regardless of what etaBoi reports."""
    p = get_final_value(res, f"PBoi{boiler_num}")
    if p <= p_epsilon:
        pytest.skip(f"PBoi{boiler_num}={p} <= {p_epsilon} -- boiler not firing, ratio undefined")
    q = get_final_value(res, f"QBoi{boiler_num}")
    eta = get_final_value(res, f"etaBoi{boiler_num}")
    assert_close(eta, q / p, rel_tol=rel_tol,
                 msg=f"etaBoi{boiler_num} vs QBoi{boiler_num}/PBoi{boiler_num}")


# ═══════════════════════════════════════════════════════════════════════════
# CORE
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.core
def test_b1_plant_off():
    res = run(50, 0, 0.006, False, False, False, False)
    assert_global_invariants(res)
    assert abs(get_final_value(res, "QHeatDelivered")) < 1e-3
    assert abs(get_final_value(res, "PBoi1")) < 1e-3
    assert abs(get_final_value(res, "PBoi2")) < 1e-3
    assert abs(get_final_value(res, "PPum1")) < 1e-3
    assert abs(get_final_value(res, "PPum2")) < 1e-3


@pytest.mark.core
def test_b2_one_boiler_normal_heating():
    res = run(50, 0, 0.006, True, False, True, False)
    assert_global_invariants(res)
    assert get_final_value(res, "THotWatSup") > c2k(50)
    assert get_final_value(res, "QHeatDelivered") > 0
    assert get_final_value(res, "QBoi1") > 0
    assert get_final_value(res, "PBoi1") > 0
    assert abs(get_final_value(res, "QBoi2")) < 1e-3


@pytest.mark.core
def test_b4_capacity_limited():
    """Demand (huge VHotWat_flow, cold TOut driving max reset, cold TRet)
    deliberately exceeds two-boiler nameplate capacity."""
    res = run(30, -15, 0.030, True, True, True, True)
    assert_global_invariants(res)
    avail = get_final_value(res, "availableHeatingCapacity")
    delivered = get_final_value(res, "QHeatDelivered")
    assert delivered <= avail * 1.02, "QHeatDelivered should not meaningfully exceed available capacity"
    assert_close(delivered, avail, rel_tol=0.05, msg="QHeatDelivered vs availableHeatingCapacity")
    assert get_final_value(res, "THotWatSup") < get_final_value(res, "THotWatSupSet")
    assert_close(get_final_value(res, "plantPLR"), 1.0, abs_tol=0.05)

    # Per-boiler nameplate cap: this is what yCap actually exists to
    # protect (see BoilerPlant.mo Documentation, "Capacity limiting and the
    # condensing bonus") -- TRet=30C here is well into condensing territory
    # (eta > eta_nominal), which is exactly the regime where an unbounded
    # firing command would let QBoi exceed QBoi_nominal.
    assert get_final_value(res, "QBoi1") <= P["QBoi_nominal"] * 1.02, \
        "QBoi1 should not materially exceed nameplate QBoi_nominal even under the condensing bonus"
    assert get_final_value(res, "QBoi2") <= P["QBoi_nominal"] * 1.02, \
        "QBoi2 should not materially exceed nameplate QBoi_nominal even under the condensing bonus"


@pytest.mark.core
def test_b9_energy_balance():
    res = run(50, 0, 0.006, True, True, True, True)
    delivered = get_final_value(res, "QHeatDelivered")
    # VHotWat_flow isn't an FMU output; recompute mass flow from the known input.
    m_flow = P["rhoWat"] * 0.006
    balance = m_flow * P["cpWat"] * (get_final_value(res, "THotWatSup") - c2k(50))
    assert_close(delivered, balance, rel_tol=0.02,
                 msg="QHeatDelivered vs rho*V*cp*(THotWatSup-THotWatRet)")


@pytest.mark.core
def test_hot_return_no_demand():
    """Protects QReq = mHotWat_flow*cpWat*max(0, THotWatSupSet-THotWatRet):
    with return water already at or above the supply setpoint, the plant
    has no heating to do regardless of enabled boilers/pumps, and the
    max(0, ...) clamp should prevent QReq (and so the feed-forward PLR
    target) from going negative."""
    res = run(80, 0, 0.006, True, True, True, True)
    setpoint = get_final_value(res, "THotWatSupSet")
    assert c2k(80) >= setpoint, \
        f"test setup invariant broken: THotWatRet (80C) should be >= THotWatSupSet ({setpoint - 273.15:.1f}C)"
    assert_global_invariants(res)
    assert abs(get_final_value(res, "QHeatDelivered")) < 1e-3
    assert abs(get_final_value(res, "QBoi1")) < 1e-3
    assert abs(get_final_value(res, "QBoi2")) < 1e-3
    assert abs(get_final_value(res, "PBoi1")) < 1e-3
    assert abs(get_final_value(res, "PBoi2")) < 1e-3


@pytest.mark.core
def test_b10_no_pump():
    res = run(50, 0, 0.006, True, True, False, False)
    assert_global_invariants(res)
    assert abs(get_final_value(res, "QHeatDelivered")) < 1e-3


@pytest.mark.core
def test_b11_zero_flow():
    """Beyond the bare invariant check: with VHotWat_flow=0, mHotWat_flow
    falls below mHotWat_flow_min, and THotWatSup should be reported as
    THotWatRet directly (the model's explicit low-flow reporting guard --
    see BoilerPlant.mo section 14 / "Supply temperature -- low-flow
    numerical safety"), not the mixing-volume's own reading, which is not
    physically meaningful at zero throughflow."""
    res = run(50, 0, 0.0, True, True, True, True, final_time=3600.0)
    assert_global_invariants(res)
    assert abs(get_final_value(res, "QHeatDelivered")) < 1e-3
    assert_close(get_final_value(res, "THotWatSup"), c2k(50), abs_tol=0.1,
                 msg="THotWatSup should report THotWatRet directly at zero flow")


@pytest.mark.core
def test_b12_pump_differential_pressure():
    res_low = run(50, 0, 0.003, True, False, True, False)
    res_high = run(50, 0, 0.012, True, False, True, False)
    dp_low = get_final_value(res_low, "dpHotWat")
    dp_high = get_final_value(res_high, "dpHotWat")
    assert dp_low >= 0 and dp_high >= 0
    assert dp_high > dp_low, "dpHotWat should rise with flow"

    res_two_pump = run(50, 0, 0.012, True, True, True, True)
    dp_two_pump = get_final_value(res_two_pump, "dpHotWat")
    assert dp_two_pump < dp_high, "dpHotWat should fall when a second pump shares the same flow"


@pytest.mark.core
def test_fuel_efficiency_consistency():
    """etaBoi is exposed directly from boiler physics (boiler.eta), not
    computed as QBoi/PBoi -- this checks the two agree, i.e. that etaBoi
    means what its name says: useful thermal output / fuel input power.
    Runs both boilers under real firing conditions (not just boiler 1, as
    in most other tests here) so PBoi2 is also meaningfully above the
    skip-epsilon."""
    res = run(30, -15, 0.020, True, True, True, True)
    assert get_final_value(res, "PBoi1") > 1.0, "test setup: boiler 1 should be firing"
    assert get_final_value(res, "PBoi2") > 1.0, "test setup: boiler 2 should be firing"
    assert_eta_matches_q_over_p(res, 1)
    assert_eta_matches_q_over_p(res, 2)


@pytest.mark.core
def test_b13_boiler_off_pump_on():
    """With water flow present but no active boilers, THotWatSup should
    approach THotWatRet (unlike AHU's documented zero-CHW-flow TChiWatRet
    staleness -- this is water actively flowing through, not stagnant)."""
    res = run(50, 0, 0.006, False, False, True, False)
    assert_global_invariants(res)
    assert_close(get_final_value(res, "THotWatSup"), c2k(50), abs_tol=0.5)
    assert abs(get_final_value(res, "QHeatDelivered")) < 1e-3


@pytest.mark.core
def test_fmu_interface_regression():
    import zipfile
    from xml.etree import ElementTree as ET

    if not BOILER_FMU.exists():
        pytest.skip(f"{BOILER_FMU} not found -- build it before running this test")

    expected_inputs = {"THotWatRet", "TOut", "VHotWat_flow", "uBoi1", "uBoi2", "uPum1", "uPum2"}
    expected_outputs = set(ALL_OUTPUTS)

    with zipfile.ZipFile(BOILER_FMU) as zf:
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

    assert actual_inputs == expected_inputs, \
        f"missing={expected_inputs - actual_inputs} extra={actual_inputs - expected_inputs}"
    assert actual_outputs == expected_outputs, \
        f"missing={expected_outputs - actual_outputs} extra={actual_outputs - expected_outputs}"

    assert units.get("QHeatDelivered") == "W"
    assert units.get("THotWatRet") == "K"
    assert units.get("plantPLR") == "1"


@pytest.mark.core
def test_model_json_metadata_matches_fmu():
    from shared.validate_fmu_metadata import (
        extract_io_variables, load_metadata, load_model_description, validate,
    )

    metadata_path = BOILER_DIR / "model.json"
    if not BOILER_FMU.exists() or not metadata_path.exists():
        pytest.skip("BoilerPlant.fmu or model.json not found -- build/create before running this test")

    fmu_vars = extract_io_variables(load_model_description(BOILER_FMU))
    metadata = load_metadata(metadata_path)
    result = validate(fmu_vars, metadata)

    assert not result["missing_inputs"], f"FMU inputs missing from model.json: {result['missing_inputs']}"
    assert not result["missing_outputs"], f"FMU outputs missing from model.json: {result['missing_outputs']}"
    assert not result["stale_inputs"], f"model.json inputs no longer in the FMU: {result['stale_inputs']}"
    assert not result["stale_outputs"], f"model.json outputs no longer in the FMU: {result['stale_outputs']}"


# ═══════════════════════════════════════════════════════════════════════════
# EXTENDED -- slower sweeps/stability/step-response tests.
# Not run by default CI; use `pytest models/boiler/tests -m extended` or the
# workflow's workflow_dispatch "extended" input.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.extended
def test_b3_two_boilers_load_sharing():
    res_one = run(30, -15, 0.020, True, False, True, False)
    res_two = run(30, -15, 0.020, True, True, True, True)
    assert_global_invariants(res_two)
    avail_one = get_final_value(res_one, "availableHeatingCapacity")
    avail_two = get_final_value(res_two, "availableHeatingCapacity")
    assert_close(avail_two, 2 * avail_one, rel_tol=1e-6)
    delivered_two = get_final_value(res_two, "QHeatDelivered")
    assert delivered_two > get_final_value(res_one, "QHeatDelivered"), \
        "two enabled boilers should deliver more than one under the same demand"
    q1 = get_final_value(res_two, "QBoi1")
    q2 = get_final_value(res_two, "QBoi2")
    assert_close(q1, q2, rel_tol=0.02, msg="even load sharing between boiler 1 and 2")


@pytest.mark.extended
def test_low_load_one_vs_two_boilers():
    """Complement to test_b3 (which uses demand exceeding even two-boiler
    capacity): here demand (~68 kW) is small relative to a single boiler's
    500 kW nameplate, so enabling a second boiler should barely change
    total delivered heat -- it's a load-sharing choice, not a capacity
    necessity."""
    res_one = run(55, 10, 0.003, True, False, True, False)
    res_two = run(55, 10, 0.003, True, True, True, True)
    assert_global_invariants(res_one)
    assert_global_invariants(res_two)

    delivered_one = get_final_value(res_one, "QHeatDelivered")
    delivered_two = get_final_value(res_two, "QHeatDelivered")
    assert delivered_one > 0, "test setup: demand should be nonzero"
    assert_close(delivered_two, delivered_one, rel_tol=0.05,
                 msg="one vs. two boilers should satisfy approximately the same low-load demand")
    assert delivered_two < 1.5 * delivered_one, \
        "enabling a second boiler at low load should not approximately double total heating"

    q1 = get_final_value(res_two, "QBoi1")
    q2 = get_final_value(res_two, "QBoi2")
    assert_close(q1, q2, rel_tol=0.05, msg="even load sharing between boiler 1 and 2 at low load")


@pytest.mark.extended
def test_b5_outdoor_reset_sweep():
    sweep = [-15, -5, 5, 15, 25]
    setpoints = []
    for tout in sweep:
        res = run(50, tout, 0.006, True, False, True, False, final_time=600.0)
        setpoints.append(get_final_value(res, "THotWatSupSet"))
    assert all(setpoints[i] >= setpoints[i + 1] - 1e-6 for i in range(len(setpoints) - 1)), \
        f"THotWatSupSet should fall monotonically as TOut rises: {setpoints}"
    assert setpoints[0] > setpoints[-1]


@pytest.mark.extended
def test_b6_outdoor_reset_disabled():
    res_cold = run(50, -15, 0.006, True, False, True, False, final_time=600.0,
                    params={"haveOutdoorReset": False})
    res_warm = run(50, 25, 0.006, True, False, True, False, final_time=600.0,
                    params={"haveOutdoorReset": False})
    assert_close(get_final_value(res_cold, "THotWatSupSet"),
                 get_final_value(res_warm, "THotWatSupSet"), abs_tol=1e-6)


@pytest.mark.extended
def test_b7_condensing_efficiency_vs_return_temp():
    """"At comparable PLR" requires actually holding demand comparable
    across the sweep, not just fixing TOut: with outdoor reset driving
    THotWatSupSet, required deltaT (and so PLR) shrinks as TRet approaches
    the setpoint ceiling, so a naive TOut-only sweep confounds the
    temperature effect with a part-load effect (verified: at a fixed PLR,
    the efficiency formula IS monotonic in TRet -- the earlier version of
    this test wasn't controlling for that and failed as a result).

    Fixed here by disabling outdoor reset and forcing a high, fixed
    THotWatSupSet (95 degC) with a large flow, so required heating vastly
    exceeds single-boiler capacity at every TRet in the sweep -- PLR
    saturates near 1 (modulo yCap's own temperature-driven capacity limit,
    itself part of what's under test) at each point instead of drifting."""
    etas = []
    for tret in [40, 50, 60, 70]:
        res = run(tret, 0, 0.020, True, False, True, False,
                  params={"haveOutdoorReset": False, "THotWatSupSetFixed": c2k(95)})
        etas.append(get_final_value(res, "etaBoi1"))
    assert all(etas[i] >= etas[i + 1] - 1e-6 for i in range(len(etas) - 1)), \
        f"efficiency should be monotonically non-increasing as return temp rises: {etas}"
    assert etas[0] > etas[-1]


@pytest.mark.extended
def test_b8_part_load_efficiency_bounds():
    for flow in [0.0015, 0.003, 0.006, 0.009, 0.012]:
        res = run(50, -10, flow, True, False, True, False)
        eta = get_final_value(res, "etaBoi1")
        assert P["eta_min"] - 1e-6 <= eta <= P["eta_max"] + 1e-6, \
            f"eta={eta} out of [{P['eta_min']}, {P['eta_max']}] at flow={flow}"


@pytest.mark.extended
def test_b14_boiler_staging_configurations():
    """NOT a dynamic in-run staging-transition test -- see below for why,
    and do not read the two segments as "boiler 2 turns on at t=600s".

    shared/runtime/models/manager.py's own step() (the production FMU integration
    pattern this suite matches everywhere else) calls load_fmu_model() fresh
    on every single step -- it never keeps one FMU instance alive across
    calls, so there is no live, continuously-evolving FMU state for a
    Boolean input to toggle within. Worse, even across a multi-step
    session, only the CURRENT step's discrete values are ever applied
    (_apply_discrete_inputs uses `values` from the latest step only, not
    session.input_history) and held constant for the FULL replayed
    trajectory from t=0 -- production does not history-track Boolean
    changes the way it does Real inputs. So "one FMU instance, uBoi2 flips
    from false to true partway through a single simulate() call" would not
    just be inconvenient to set up with PyFMI -- it would test a capability
    the production system does not actually use, which would be more
    misleading than useful here.

    What this test actually verifies: both staging configurations
    (1 boiler enabled, then 2) independently initialize and operate
    correctly from a cold start, each in its own freshly loaded FMU
    instance -- i.e. exactly how production would encounter either
    configuration on any given step. It does not exercise, and does not
    claim to exercise, state-preserving Boolean switching mid-run.
    """
    res_one = run(30, -15, 0.020, True, False, True, True, final_time=600.0)
    assert_global_invariants(res_one)

    res_two = run(30, -15, 0.020, True, True, True, True, final_time=1200.0)
    assert_global_invariants(res_two)


@pytest.mark.extended
def test_b15_return_temperature_step():
    base_pre = [c2k(40), c2k(-10), 0.010]
    base_post = [c2k(60), c2k(-10), 0.010]
    rows = [[0.0] + base_pre, [600.0] + base_pre, [600.05] + base_post, [1800.0] + base_post]
    res = simulate_boiler(rows, [True, True, True, True], ncp=60)
    assert_global_invariants(res)


@pytest.mark.extended
def test_b16_outdoor_temperature_step():
    base_pre = [c2k(50), c2k(-15), 0.010]
    base_post = [c2k(50), c2k(20), 0.010]
    rows = [[0.0] + base_pre, [600.0] + base_pre, [600.05] + base_post, [1800.0] + base_post]
    res = simulate_boiler(rows, [True, False, True, False], ncp=60)
    assert_global_invariants(res)
    setpoint_before = float(np.interp(600.0, res["time"], get_series(res, "THotWatSupSet")))
    setpoint_after = get_final_value(res, "THotWatSupSet")
    assert setpoint_after < setpoint_before, "reset should lower the setpoint as TOut rises"


@pytest.mark.extended
def test_b17_six_hour_stability():
    res = run(50, 0, 0.006, True, True, True, True, final_time=21600.0)
    assert_global_invariants(res)
    assert_finite(res)
