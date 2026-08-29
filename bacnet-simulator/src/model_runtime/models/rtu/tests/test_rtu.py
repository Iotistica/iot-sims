"""RTU.mo regression suite -- exercises the exported RTU.fmu via pyfmi in
Model Exchange mode, matching shared/runtime/models/manager.py's production
calling convention (fmu.simulate(input=(names, trajectory))), not a raw
FMI co-simulation doStep() loop. Same harness pattern as
models/ahu/tests/test_ahu.py and models/boiler/tests/test_boiler.py.

RTU has only Real inputs (no Booleans), so this suite is simpler than
test_boiler.py's Real/Boolean split -- every input rides the continuous
simulate(input=...) trajectory directly.

Test matrix R1-R22 plus global invariants, covering: cold start/fan-off,
normal cooling/heating, heating/cooling mutual exclusion, economizer
(favorable/unfavorable/disabled), VAV static-pressure reset (VAV and CAV
modes), fan laws, mixed-air bounds, the compressor COP/power model, the
gas-furnace fuel-power model, total electric power, FMU interface
regression, model.json/FMU metadata sync, and (extended) full-economizer
saturation, six-hour stability, an alternate-capacity parameter override,
and a SAT-setpoint step response.

Run core tests only (fast, CI default):
    pytest models/rtu/tests -m core

Run extended tests (sweeps/stability/step-response):
    pytest models/rtu/tests -m extended
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pyfmi import load_fmu

RTU_DIR = Path(__file__).resolve().parent.parent
RTU_FMU = RTU_DIR / "RTU.fmu"

NAMES = ["TSupSet", "TOut", "TRet", "uFan", "uVAVDamMax", "uMinOutAir"]

ALL_OUTPUTS = [
    "TSup", "TMix", "VSup_flow", "yFan", "yOutDamCmd", "yOutDam", "VOutAir_flow", "yCoo",
    "yHea", "QCoolLoad", "QHeaLoad", "PCompressor", "PFan", "dpSup",
    "dpSupSet", "coolingPLR", "heatingPLR", "totalElectricPower",
    "gasHeatingPower", "compressorCOP",
    # Two-stage packaged-DX cooling surrogate diagnostics (see RTU.mo's
    # useTwoStageCooling doc comment): availableCoolingCapacity is the
    # positive sensible capacity after temperature derating but before
    # controller/stage loading; compressorStage reports 0/1/2 (off/stage1/
    # stage2), collapsing to 1 whenever cooling is active in the default
    # continuous-modulating mode (useTwoStageCooling=false).
    "availableCoolingCapacity", "compressorStage",
    "supplyFanStatus", "coolingStatus", "heatingStatus", "economizerStatus",
]

STATUS_OUTPUTS = ("supplyFanStatus", "coolingStatus", "heatingStatus", "economizerStatus")

# Nominal parameters mirroring RTU.mo's own defaults -- used only for
# invariant bounds and expected relations, not fed into the model.
P = dict(
    vavDamLow=0.60, vavDamHigh=0.90,
    dpSupSetMin=200.0, dpSupSetMax=500.0, dpSup_nominal=500.0,
    dpSupSetFixed=500.0,
    PSupFan_nominal=5000.0, minOutAirFra=0.15,
    QCoo_flow_nominal=-36846.82, COP_nominal=3.71,
    TOutCOPRef=308.15, kCOPPerKelvin=0.04, COP_min=2.0, COP_max=5.0,
    # DX capacity/part-load performance map (see RTU.mo): compressorCOP is
    # now steadyStateCOP * coolingPartLoadFactor, so it's no longer a pure
    # function of TOut alone -- see test_r12's own updated comment.
    partLoadFactorMin=0.70, partLoadDegradation=0.15,
    TOutCapRef=308.15, TMixCapRef=299.15,
    kCapOutPerKelvin=0.006, kCapMixPerKelvin=0.010, capModifierMin=0.65,
    QHea_flow_nominal=30000.0, etaHeat=0.80,
)


# ─── Helpers ─────────────────────────────────────────────────────────────

def c2k(celsius: float) -> float:
    return celsius + 273.15


def load_rtu_fmu(fmu_path: Path = RTU_FMU):
    if not fmu_path.exists():
        pytest.skip(f"{fmu_path} not found -- build it before running these tests")
    return load_fmu(str(fmu_path), kind="ME")


def const_rows(TSupSet_c, TOut_c, TRet_c, uFan, uVAVDamMax, final_time,
                uMinOutAir=P["minOutAirFra"]):
    """A two-point trajectory holding every input constant for final_time
    seconds. uMinOutAir defaults to RTU.mo's own minOutAirFra default
    (0.15) so every pre-existing call site (none of which pass it) keeps
    exercising exactly the same economizer floor as before uMinOutAir
    existed as a live FMU input."""
    v = [c2k(TSupSet_c), c2k(TOut_c), c2k(TRet_c), uFan, uVAVDamMax, uMinOutAir]
    return [[0.0] + v, [final_time] + v]


def simulate_rtu(rows, fmu_path: Path = RTU_FMU, params: dict | None = None, ncp: int | None = None):
    """rows: [[t, TSupSet_K, TOut_K, TRet_K, uFan, uVAVDamMax], ...].

    result_handling='memory' is required, not optional -- see
    models/ahu/tests/test_ahu.py's simulate_ahu docstring for why (pyfmi's
    default 'file' mode silently overwrites a shared, model-name-derived
    .mat file across repeated simulations in one process).
    """
    fmu = load_rtu_fmu(fmu_path)
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


def get_final_bool(res, name: str) -> bool:
    """Boolean FMU outputs come back from pyfmi as 0.0/1.0 (or True/False,
    depending on pyfmi version) -- normalize to a plain Python bool via the
    same >0.5 threshold used for the invariant check below."""
    return get_final_value(res, name) > 0.5


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
                              minOutAirFra=P["minOutAirFra"], COP_min=P["COP_min"], COP_max=P["COP_max"]):
    """Bounds and cross-output relations that must hold at every point on the
    trajectory, not just the final value -- the RTU counterpart to
    test_ahu.py's assert_global_invariants."""
    tol = 1e-6
    yFan = get_series(res, "yFan")
    yOutDam = get_series(res, "yOutDam")
    VOutAir_flow = get_series(res, "VOutAir_flow")
    VSup_flow = get_series(res, "VSup_flow")
    yCoo = get_series(res, "yCoo")
    yHea = get_series(res, "yHea")
    dpSup = get_series(res, "dpSup")
    dpSupSet = get_series(res, "dpSupSet")
    PFan = get_series(res, "PFan")
    PCompressor = get_series(res, "PCompressor")
    QCoolLoad = get_series(res, "QCoolLoad")
    QHeaLoad = get_series(res, "QHeaLoad")
    gasHeatingPower = get_series(res, "gasHeatingPower")
    totalElectricPower = get_series(res, "totalElectricPower")
    compressorCOP = get_series(res, "compressorCOP")
    coolingPLR = get_series(res, "coolingPLR")
    heatingPLR = get_series(res, "heatingPLR")

    assert np.all(yFan >= -tol) and np.all(yFan <= 1 + tol), "0<=yFan<=1 violated"
    assert np.all(yOutDam >= minOutAirFra - 1e-4) and np.all(yOutDam <= 1 + tol), \
        "minOutAirFra<=yOutDam<=1 violated"
    assert np.all(VOutAir_flow >= -1e-6), "VOutAir_flow>=0 violated"
    assert np.all(VOutAir_flow <= VSup_flow + 1e-6), "VOutAir_flow<=VSup_flow violated"
    assert np.all(yCoo >= -tol) and np.all(yCoo <= 1 + tol), "0<=yCoo<=1 violated"
    assert np.all(yHea >= -tol) and np.all(yHea <= 1 + tol), "0<=yHea<=1 violated"
    assert np.all(dpSup >= -1e-3), "dpSup>=0 violated"
    assert np.all(dpSupSet >= dpSupSetMin - 1e-3) and np.all(dpSupSet <= dpSupSetMax + 1e-3), \
        "dpSupSetMin<=dpSupSet<=dpSupSetMax violated"
    assert np.all(PFan >= -1e-3), "PFan>=0 violated"
    assert np.all(PCompressor >= -1e-3), "PCompressor>=0 violated"
    assert np.all(QCoolLoad >= -1e-3), "QCoolLoad>=0 violated (positive-when-cooling convention)"
    assert np.all(QHeaLoad >= -1e-3), "QHeaLoad>=0 violated"
    assert np.all(gasHeatingPower >= -1e-3), "gasHeatingPower>=0 violated"
    assert np.all(totalElectricPower >= -1e-3), "totalElectricPower>=0 violated"
    assert np.all((compressorCOP <= 1e-6) | ((compressorCOP >= COP_min - 1e-6) & (compressorCOP <= COP_max + 1e-6))), \
        "compressorCOP must be 0 (compressor off) or within [COP_min, COP_max]"
    # coolingPLR/heatingPLR/totalElectricPower are pure algebraic copies of
    # other outputs (coolingPLR=yCoo, heatingPLR=yHea,
    # totalElectricPower=PCompressor+PFan) -- exact at every point the
    # solver actually evaluates, but NOT checked here across the full
    # recorded trajectory: pyfmi's CVode dense-output interpolation
    # reconstructs each output's trajectory independently at the ncp
    # communication points, and aliased/copy outputs can differ from their
    # source by up to ~2% at interpolated (non-solver-step) points even
    # though the underlying equation is exact -- this reproduces the same
    # alias-variable behavior the FMU export itself warns about ("alias
    # variables with redundant start and/or conflicting nominal values").
    # Verified as a harness/interpolation artifact, not a model defect, by
    # checking these relations on final (settled) values instead -- see
    # test_r2/test_r3/test_r13/test_r14/test_r15.
    assert np.all(yCoo >= -tol) and np.all(coolingPLR >= -tol), "coolingPLR/yCoo bounds"
    # coolingPLR = coolingCoilCommand = min(1, yCoo*coolingCapacityModifier)
    # is bounded [0,1] by construction, same as yCoo itself -- see RTU.mo's
    # DX capacity/part-load performance map.
    assert np.all(coolingPLR <= 1 + tol), "coolingPLR<=1 violated"
    assert np.all(yHea >= -tol) and np.all(heatingPLR >= -tol), "heatingPLR/yHea bounds"
    assert_finite(res)


# ─── R1: cold start / unit off (core) ──────────────────────────────────────

@pytest.mark.core
def test_r1_cold_start_unit_off():
    """Baseline scenario that segfaulted the abandoned real DX coil (see
    RTU.mo Documentation, 'DX cooling component selection'). Every model in
    this repo's own test suites exercises fan-off first."""
    rows = const_rows(TSupSet_c=13, TOut_c=32, TRet_c=25, uFan=0, uVAVDamMax=0.5,
                       final_time=300.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    assert abs(get_final_value(res, "yFan")) < 1e-6
    assert abs(get_final_value(res, "VSup_flow")) < 1e-6
    assert abs(get_final_value(res, "yCoo")) < 1e-6
    assert abs(get_final_value(res, "yHea")) < 1e-6
    assert abs(get_final_value(res, "QCoolLoad")) < 1e-6
    assert abs(get_final_value(res, "QHeaLoad")) < 1e-6
    assert abs(get_final_value(res, "PCompressor")) < 1e-6
    assert get_final_bool(res, "supplyFanStatus") is False
    assert get_final_bool(res, "coolingStatus") is False
    assert get_final_bool(res, "heatingStatus") is False
    assert get_final_bool(res, "economizerStatus") is False


# ─── R2: normal cooling (core) ──────────────────────────────────────────────

@pytest.mark.core
def test_r2_normal_cooling():
    rows = const_rows(TSupSet_c=13, TOut_c=32, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                       final_time=21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yCoo") > 0
    assert abs(get_final_value(res, "yHea")) < 1e-3
    assert get_final_value(res, "TSup") < get_final_value(res, "TMix")
    assert get_final_value(res, "QCoolLoad") > 0
    assert get_final_value(res, "PCompressor") > 0
    assert get_final_value(res, "compressorCOP") > 0
    assert_close(get_final_value(res, "TSup"), c2k(13), abs_tol=0.5, msg="TSup should settle at setpoint")
    assert_close(get_final_value(res, "coolingPLR"), get_final_value(res, "yCoo"), rel_tol=1e-4,
                 msg="coolingPLR vs yCoo at settled state")
    assert get_final_bool(res, "supplyFanStatus") is True
    assert get_final_bool(res, "coolingStatus") is True
    assert get_final_bool(res, "heatingStatus") is False


# ─── R3: normal heating (core) ──────────────────────────────────────────────

@pytest.mark.core
def test_r3_normal_heating():
    rows = const_rows(TSupSet_c=30, TOut_c=0, TRet_c=18, uFan=1, uVAVDamMax=0.8,
                       final_time=21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yHea") > 0
    assert abs(get_final_value(res, "yCoo")) < 1e-3
    assert get_final_value(res, "TSup") > get_final_value(res, "TMix")
    assert get_final_value(res, "QHeaLoad") > 0
    assert get_final_value(res, "gasHeatingPower") > 0
    assert abs(get_final_value(res, "PCompressor")) < 1e-6
    assert_close(get_final_value(res, "TSup"), c2k(30), abs_tol=0.5, msg="TSup should settle at setpoint")
    assert_close(get_final_value(res, "heatingPLR"), get_final_value(res, "yHea"), rel_tol=1e-4,
                 msg="heatingPLR vs yHea at settled state")
    assert get_final_bool(res, "supplyFanStatus") is True
    assert get_final_bool(res, "heatingStatus") is True
    assert get_final_bool(res, "coolingStatus") is False


# ─── R4: heating/cooling mutual exclusion (core) ────────────────────────────

@pytest.mark.core
def test_r4_heating_cooling_mutual_exclusion():
    heating_rows = const_rows(30, 0, 18, 1, 0.8, 21600.0)
    cooling_rows = const_rows(13, 32, 25, 1, 0.8, 21600.0)
    worst = 0.0
    for rows in (heating_rows, cooling_rows):
        res = simulate_rtu(rows)
        prod = get_series(res, "yHea") * get_series(res, "yCoo")
        worst = max(worst, float(np.max(np.abs(prod))))
    assert worst < 1e-4, f"yHea*yCoo reached {worst}, heat and DX cooling operated simultaneously"


# ─── R5-R7: economizer (core) ───────────────────────────────────────────────

@pytest.mark.core
def test_r5_economizer_favorable_outdoor_air():
    rows = const_rows(13, 10, 24, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    assert get_final_value(res, "yOutDam") > P["minOutAirFra"]
    assert get_final_bool(res, "economizerStatus") is True


@pytest.mark.core
def test_r6_economizer_unfavorable_outdoor_air():
    rows = const_rows(13, 32, 24, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    assert_close(get_final_value(res, "yOutDam"), P["minOutAirFra"], abs_tol=1e-6)
    assert get_final_bool(res, "economizerStatus") is False


@pytest.mark.core
def test_r7_economizer_disabled():
    rows = const_rows(13, 10, 24, 1, 0.8, 21600.0)
    res = simulate_rtu(rows, params={"haveEconomizer": False})
    assert_global_invariants(res)
    assert_close(get_final_value(res, "yOutDam"), P["minOutAirFra"], abs_tol=1e-6)
    assert get_final_bool(res, "economizerStatus") is False, \
        "economizerStatus should be off when locked exactly at the minimum-OA floor"

    # haveEconomizer=false must lock yOutDam at whatever uMinOutAir is
    # SUPPLIED, not just the parameter's old compiled-in default -- proves
    # the lock now tracks the live uMinOutAir input, not a constant.
    custom_min_oa = 0.35
    rows_custom = const_rows(13, 10, 24, 1, 0.8, 21600.0, uMinOutAir=custom_min_oa)
    res_custom = simulate_rtu(rows_custom, params={"haveEconomizer": False})
    assert_global_invariants(res_custom, minOutAirFra=custom_min_oa)
    assert_close(get_final_value(res_custom, "yOutDam"), custom_min_oa, abs_tol=1e-6,
                 msg="haveEconomizer=false should lock yOutDam at the supplied uMinOutAir")
    assert get_final_bool(res_custom, "economizerStatus") is False


# ─── R8: VAV static-pressure reset (core) ───────────────────────────────────

@pytest.mark.core
def test_r8_vav_static_pressure_reset():
    points = [(0.5, 200.0), (0.75, 350.0), (0.95, 500.0)]
    results = []
    for uVAVDamMax, expected_dp in points:
        rows = const_rows(13, 32, 25, 1, uVAVDamMax, 21600.0)
        res = simulate_rtu(rows)
        dp = get_final_value(res, "dpSupSet")
        results.append(dp)
        assert_close(dp, expected_dp, abs_tol=1.0,
                     msg=f"dpSupSet at uVAVDamMax={uVAVDamMax}")
    assert all(results[i] <= results[i + 1] for i in range(len(results) - 1)), \
        "dpSupSet must be monotonically non-decreasing with uVAVDamMax"


# ─── R9: fan laws (core) ────────────────────────────────────────────────────

@pytest.mark.core
def test_r9_fan_pressure_and_power_laws():
    rows = const_rows(13, 32, 25, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    yFan = get_final_value(res, "yFan")
    dpSup = get_final_value(res, "dpSup")
    PFan = get_final_value(res, "PFan")
    assert_close(dpSup, P["dpSup_nominal"] * yFan ** 2, rel_tol=1e-6,
                 msg="dpSup = dpSup_nominal * yFan^2")
    assert_close(PFan, P["PSupFan_nominal"] * yFan ** 3, rel_tol=1e-6,
                 msg="PFan = PSupFan_nominal * yFan^3")


# ─── R10: fan off (core) ─────────────────────────────────────────────────────

@pytest.mark.core
def test_r10_fan_off():
    rows = const_rows(13, 32, 25, 0, 0.9, 21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    for name in ("yFan", "dpSup", "PFan", "VSup_flow", "VOutAir_flow", "yHea", "yCoo",
                 "QCoolLoad", "QHeaLoad", "PCompressor"):
        assert abs(get_final_value(res, name)) < 1e-6, f"{name} should be 0 with fan off"
    assert get_final_bool(res, "supplyFanStatus") is False
    assert get_final_bool(res, "coolingStatus") is False
    assert get_final_bool(res, "heatingStatus") is False


# ─── R11: mixed-air sanity (core) ───────────────────────────────────────────

@pytest.mark.core
def test_r11_mixed_air_temperature_bounds():
    rows = const_rows(13, 32, 24, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    tmix = get_final_value(res, "TMix")
    lo, hi = min(c2k(32), c2k(24)), max(c2k(32), c2k(24))
    assert lo - 1e-3 <= tmix <= hi + 1e-3


# ─── R12: compressor COP bounds and TOut dependence (core) ─────────────────

@pytest.mark.core
def test_r12_compressor_cop_bounds_and_monotonicity():
    """compressorCOP = steadyStateCOP * coolingPartLoadFactor. steadyStateCOP
    is a bounded linear function of TOut, decreasing as TOut rises above
    TOutCOPRef (35 degC); coolingPartLoadFactor (see RTU.mo's DX capacity/
    part-load performance map) further derates it whenever the coil isn't at
    exactly 100% load, which it generally isn't for this test's fixed
    setpoint/load conditions -- so compressorCOP itself is no longer a pure
    function of TOut alone, but both factors move the same direction as TOut
    rises (steadyStateCOP falls, and less available capacity from the
    temperature-derated coil also pulls the part-load factor down), so
    compressorCOP stays monotonically non-increasing overall."""
    touts = [20, 25, 30, 35, 40]
    cops = []
    for tout in touts:
        rows = const_rows(13, tout, 25, 1, 0.8, 21600.0)
        res = simulate_rtu(rows)
        cop = get_final_value(res, "compressorCOP")
        cops.append(cop)
        assert P["COP_min"] - 1e-6 <= cop <= P["COP_max"] + 1e-6, \
            f"compressorCOP={cop} outside [{P['COP_min']}, {P['COP_max']}] at TOut={tout}C"
    assert all(cops[i] >= cops[i + 1] - 1e-9 for i in range(len(cops) - 1)), \
        "compressorCOP should be monotonically non-increasing as TOut rises"
    # At TOutCOPRef (35 degC), steadyStateCOP alone reduces to COP_nominal
    # exactly -- but compressorCOP additionally carries the part-load
    # penalty, so it can only ever be <= COP_nominal here, never equal to it
    # (unless the coil happens to be at exactly 100% load), and is bounded
    # below by the worst-case part-load floor (partLoadFactorMin).
    rows = const_rows(13, 35, 25, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    cop_at_ref = get_final_value(res, "compressorCOP")
    assert cop_at_ref <= P["COP_nominal"] + 1e-6, \
        f"compressorCOP at TOutCOPRef ({cop_at_ref}) should never exceed COP_nominal ({P['COP_nominal']})"
    assert cop_at_ref >= P["COP_nominal"] * P["partLoadFactorMin"] - 1e-6, \
        f"compressorCOP at TOutCOPRef ({cop_at_ref}) fell below the worst-case part-load floor"

    # Cooling-off behavior, per the current model implementation:
    # compressorCOP = if QCoolLoad>1.0 then ... else 0 -- fan running, but
    # no cooling demand (setpoint above the mixed-air temperature already
    # satisfies TSup with no active cooling), so QCoolLoad stays <=1W and
    # compressorCOP/PCompressor must both read exactly 0, not merely "low".
    rows_off = const_rows(TSupSet_c=30, TOut_c=28, TRet_c=24, uFan=1, uVAVDamMax=0.5,
                           final_time=21600.0)
    res_off = simulate_rtu(rows_off)
    assert_global_invariants(res_off)
    assert abs(get_final_value(res_off, "yCoo")) < 1e-3, "expected no cooling demand in this scenario"
    assert abs(get_final_value(res_off, "compressorCOP")) < 1e-6, \
        "compressorCOP should read (numerically) 0 when the compressor is off, not a low positive value"
    assert abs(get_final_value(res_off, "PCompressor")) < 1e-6, \
        "PCompressor should read (numerically) 0 when the compressor is off"


# ─── R13: compressor power derivation (core) ────────────────────────────────

@pytest.mark.core
def test_r13_compressor_power_derivation():
    """PCompressor = QCoolLoad / compressorCOP -- see RTU.mo Documentation,
    'DX cooling: idealized coil plus a bounded COP approximation'.

    rel_tol relaxed from 1e-6 to 1e-3: the DX capacity/part-load
    performance map (see RTU.mo) chains several non-smooth max/min clamps
    (coolingCapacityModifier, coolingPartLoadFactor) into this algebraic
    relationship, and CVode/BDF's own state tolerances (1e-6 relative/1e-8
    absolute) don't propagate through several stacked clamps at 1e-6
    precision for a *derived* quantity -- 1e-3 (0.1%) is still tight enough
    to catch a genuine formula break, several orders of magnitude above the
    solver noise actually observed (~0.008%)."""
    rows = const_rows(13, 32, 25, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    QCoolLoad = get_final_value(res, "QCoolLoad")
    compressorCOP = get_final_value(res, "compressorCOP")
    PCompressor = get_final_value(res, "PCompressor")
    assert_close(PCompressor, QCoolLoad / compressorCOP, rel_tol=1e-3,
                 msg="PCompressor vs QCoolLoad/compressorCOP")


# ─── R14: gas-furnace fuel power derivation (core) ─────────────────────────

@pytest.mark.core
def test_r14_gas_heating_power_derivation():
    """gasHeatingPower = QHeaLoad / etaHeat -- see RTU.mo Documentation,
    'Gas heating'."""
    rows = const_rows(30, 0, 18, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    QHeaLoad = get_final_value(res, "QHeaLoad")
    gasHeatingPower = get_final_value(res, "gasHeatingPower")
    assert_close(gasHeatingPower, QHeaLoad / P["etaHeat"], rel_tol=1e-6,
                 msg="gasHeatingPower vs QHeaLoad/etaHeat")


# ─── R15: total electric power (core) ───────────────────────────────────────

@pytest.mark.core
def test_r15_total_electric_power_sum():
    rows = const_rows(13, 32, 25, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    assert_close(
        get_final_value(res, "totalElectricPower"),
        get_final_value(res, "PCompressor") + get_final_value(res, "PFan"),
        rel_tol=1e-6, msg="totalElectricPower vs PCompressor+PFan")


# ─── R15b: capacity limiting (core) ─────────────────────────────────────────

@pytest.mark.core
def test_r15b_cooling_capacity_saturates_at_nominal():
    """Under a deliberately oversized cooling demand, yCoo saturates at the
    satCon PI's own yMax=1 limit, so QCoolLoad cannot exceed
    abs(QCoo_flow_nominal) -- a structural guarantee (Q_flow=u*Q_flow_nominal
    with u clipped to [0,1]), verified empirically here rather than assumed."""
    rows = const_rows(TSupSet_c=-10, TOut_c=45, TRet_c=35, uFan=1, uVAVDamMax=0.8,
                       final_time=21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    yCoo = get_final_value(res, "yCoo")
    QCoolLoad = get_final_value(res, "QCoolLoad")
    assert yCoo <= 1.0 + 1e-6, f"yCoo={yCoo} exceeded its PI saturation limit of 1"
    assert QCoolLoad <= abs(P["QCoo_flow_nominal"]) * 1.001, \
        f"QCoolLoad={QCoolLoad} exceeded nameplate abs(QCoo_flow_nominal)={abs(P['QCoo_flow_nominal'])}"


@pytest.mark.core
def test_r15c_heating_capacity_saturates_at_nominal():
    """Same structural guarantee on the heating side: yHea<=1 caps
    QHeaLoad at QHea_flow_nominal."""
    rows = const_rows(TSupSet_c=45, TOut_c=-20, TRet_c=10, uFan=1, uVAVDamMax=0.8,
                       final_time=21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    yHea = get_final_value(res, "yHea")
    QHeaLoad = get_final_value(res, "QHeaLoad")
    assert yHea <= 1.0 + 1e-6, f"yHea={yHea} exceeded its PI saturation limit of 1"
    assert QHeaLoad <= P["QHea_flow_nominal"] * 1.001, \
        f"QHeaLoad={QHeaLoad} exceeded nameplate QHea_flow_nominal={P['QHea_flow_nominal']}"


# ─── R15d: fan command never exceeds uFan (core) ────────────────────────────

@pytest.mark.core
def test_r15d_fan_command_never_exceeds_ufan():
    """yFan = min(clamp(uFan), fanCon.y) by construction -- verify uFan acts
    as a hard ceiling on yFan across a range of partial fan commands, not
    just at uFan=1."""
    for uFan in (0.3, 0.5, 0.7, 1.0):
        rows = const_rows(13, 32, 25, uFan, 0.95, 21600.0)
        res = simulate_rtu(rows)
        yFan = get_final_value(res, "yFan")
        assert yFan <= uFan + 1e-6, f"yFan={yFan} exceeded uFan={uFan}"


# ─── R16: FMU interface regression (core) ───────────────────────────────────

@pytest.mark.core
def test_r16_fmu_interface_regression():
    """No central-plant inputs/outputs (TChiWatSup/TChiWatRet/VChiWat_flow/
    THotWatSup/THotWatRet) -- RTU is self-contained, per its own design
    constraint (see RTU.mo header)."""
    import zipfile
    from xml.etree import ElementTree as ET

    if not RTU_FMU.exists():
        pytest.skip(f"{RTU_FMU} not found -- build it before running these tests")

    expected_inputs = {
        "TSupSet", "TOut", "TRet", "uFan", "uVAVDamMax", "uMinOutAir",
        # Actuator-fault override pair (see RTU.mo's own doc comment on
        # uOutDamOvr): lets a caller force the outdoor-air damper to a
        # specific position for fault simulation via a dedicated input,
        # instead of wiring one of RTU's own outputs back into an input --
        # the self-loop antipattern this repo's own bacnet-simulator
        # integration hit (a BACnet point mapped as both a model's input
        # source and output target latching at 0 with no recovery path).
        "uOutDamOvrEna", "uOutDamOvr",
    }
    expected_outputs = set(ALL_OUTPUTS)
    forbidden = {"TChiWatSup", "TChiWatRet", "VChiWat_flow", "THotWatSup", "THotWatRet"}

    with zipfile.ZipFile(RTU_FMU) as zf:
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
    assert not (actual_inputs & forbidden), f"RTU must not expose central-plant inputs: {actual_inputs & forbidden}"
    assert not (actual_outputs & forbidden), f"RTU must not expose central-plant outputs: {actual_outputs & forbidden}"

    assert units.get("QCoolLoad") == "W"
    assert units.get("TSupSet") == "K"
    assert units.get("yFan") == "1"


# ─── R17: model.json/FMU metadata sync (core) ──────────────────────────────

@pytest.mark.core
def test_r17_model_json_metadata_matches_fmu():
    """models/rtu/model.json declares RTU's inputs/outputs for the runtime
    catalog and BACnet mapping -- reuses the repo's own validator, same
    pattern as test_ahu.py's test_model_json_metadata_matches_fmu and
    test_boiler.py's equivalent."""
    from shared.validate_fmu_metadata import (
        extract_io_variables, load_metadata, load_model_description, validate,
    )

    metadata_path = RTU_DIR / "model.json"
    if not RTU_FMU.exists() or not metadata_path.exists():
        pytest.skip("RTU.fmu or model.json not found -- build/create before running this test")

    fmu_vars = extract_io_variables(load_model_description(RTU_FMU))
    metadata = load_metadata(metadata_path)
    result = validate(fmu_vars, metadata)

    assert not result["missing_inputs"], f"FMU inputs missing from model.json: {result['missing_inputs']}"
    assert not result["missing_outputs"], f"FMU outputs missing from model.json: {result['missing_outputs']}"
    assert not result["stale_inputs"], f"model.json inputs no longer in the FMU: {result['stale_inputs']}"
    assert not result["stale_outputs"], f"model.json outputs no longer in the FMU: {result['stale_outputs']}"


# ─── R18: CAV mode (core) ───────────────────────────────────────────────────

@pytest.mark.core
def test_r18_cav_mode_fixed_static_pressure():
    """haveVAVControl=false locks dpSupSet at dpSupSetFixed regardless of
    uVAVDamMax -- see RTU.mo Documentation, 'VAV/CAV hook'."""
    for uVAVDamMax in (0.1, 0.5, 0.95):
        rows = const_rows(13, 32, 25, 1, uVAVDamMax, 21600.0)
        res = simulate_rtu(rows, params={"haveVAVControl": False})
        assert_close(get_final_value(res, "dpSupSet"), P["dpSupSetFixed"], abs_tol=1e-6,
                     msg=f"dpSupSet should be fixed at dpSupSetFixed regardless of uVAVDamMax={uVAVDamMax}")


# ─── R23: outdoor-air volume flow relationship (core) ───────────────────────

@pytest.mark.core
def test_r23_outdoor_airflow_relationship():
    """VOutAir_flow = VSup_flow * yOutDam by construction (see RTU.mo
    Documentation, 'Outdoor-air volume flow output') -- verify the exact
    relationship holds, that it increases with both higher VAV-driven
    supply airflow and a more-open economizer, and stays within
    [0, VSup_flow] (also checked broadly by assert_global_invariants)."""
    # Baseline: unfavorable economizer (OA pinned at the minimum), moderate VAV demand.
    rows_base = const_rows(13, 32, 25, 1, 0.6, 21600.0)
    res_base = simulate_rtu(rows_base)
    assert_global_invariants(res_base)
    v_sup_base = get_final_value(res_base, "VSup_flow")
    y_out_dam_base = get_final_value(res_base, "yOutDam")
    v_out_base = get_final_value(res_base, "VOutAir_flow")
    assert_close(v_out_base, v_sup_base * y_out_dam_base, rel_tol=1e-6,
                 msg="VOutAir_flow should equal VSup_flow * yOutDam")
    assert v_out_base > 0, "sanity: baseline should have nonzero outdoor airflow"

    # Higher VAV demand -> more supply airflow -> more outdoor airflow at
    # the same (unfavorable, minimum-pinned) yOutDam.
    rows_more_flow = const_rows(13, 32, 25, 1, 0.95, 21600.0)
    res_more_flow = simulate_rtu(rows_more_flow)
    v_out_more_flow = get_final_value(res_more_flow, "VOutAir_flow")
    assert v_out_more_flow > v_out_base, \
        "VOutAir_flow should increase with higher VAV-driven supply airflow"

    # Favorable economizer at the SAME VAV demand as baseline -> higher
    # yOutDam -> more outdoor airflow.
    rows_favorable = const_rows(13, 10, 25, 1, 0.6, 21600.0)
    res_favorable = simulate_rtu(rows_favorable)
    assert_global_invariants(res_favorable)
    y_out_dam_favorable = get_final_value(res_favorable, "yOutDam")
    v_out_favorable = get_final_value(res_favorable, "VOutAir_flow")
    assert y_out_dam_favorable > y_out_dam_base, "sanity: economizer should have opened above the minimum"
    assert v_out_favorable > v_out_base, \
        "VOutAir_flow should increase when the economizer opens the OA damper above minimum"


# ─── R24: minimum outdoor-air input sets the economizer floor (core) ───────

@pytest.mark.core
def test_r24_minimum_outdoor_air_input_sets_economizer_floor():
    """uMinOutAir (replacing the formerly-fixed minOutAirFra parameter as
    the economizer floor during FMU operation -- see RTU.mo Documentation,
    'Minimum outdoor-air input') directly sets yOutDam's floor under
    unfavorable outdoor conditions, at every value tried, not just the
    parameter's old 0.15 default."""
    for min_oa in (0.05, 0.15, 0.30, 0.50):
        rows = const_rows(13, 32, 24, 1, 0.8, 21600.0, uMinOutAir=min_oa)
        res = simulate_rtu(rows)
        assert_global_invariants(res, minOutAirFra=min_oa)
        assert_close(get_final_value(res, "yOutDam"), min_oa, abs_tol=1e-6,
                     msg=f"yOutDam should equal uMinOutAir={min_oa} under unfavorable conditions")


# ─── R25: economizerStatus tolerance boundary (core) ────────────────────────

@pytest.mark.core
def test_r25_economizer_status_tolerance_boundary():
    """economizerStatus = yOutDam > uMinOutAir + economizerActiveTol
    (default 0.02) -- verify it correctly stays False for a small
    economizer opening that hasn't cleared the tolerance, and flips True
    once it has, avoiding status chatter right at the minimum-OA floor
    (see RTU.mo Documentation, 'BACnet status outputs'). Chooses TOut
    precisely (via the known outFraRaw formula) so yOutDam lands just
    inside, then just outside, the tolerance band -- not a loose sweep."""
    min_oa = 0.20
    economizer_active_tol = 0.02  # RTU.mo's economizerActiveTol default
    TRet_c, TSupSet_c = 25.0, 13.0
    delta_T = TRet_c - TSupSet_c

    # yOutDam lands just BELOW the tolerance band: the economizer has
    # technically opened slightly above uMinOutAir, but not enough to
    # clear economizerActiveTol -- status should stay False.
    out_fra_below = min_oa + economizer_active_tol * 0.5
    TOut_below = TRet_c - delta_T / out_fra_below
    rows_below = const_rows(TSupSet_c, TOut_below, TRet_c, 1, 0.8, 21600.0, uMinOutAir=min_oa)
    res_below = simulate_rtu(rows_below)
    assert_close(get_final_value(res_below, "yOutDam"), out_fra_below, abs_tol=1e-4,
                 msg="sanity: yOutDam should land at the intended outFraRaw")
    assert get_final_bool(res_below, "economizerStatus") is False, \
        "economizerStatus should stay False for an opening inside the tolerance band"

    # yOutDam lands just ABOVE the tolerance band -- status should flip True.
    out_fra_above = min_oa + economizer_active_tol * 1.5
    TOut_above = TRet_c - delta_T / out_fra_above
    rows_above = const_rows(TSupSet_c, TOut_above, TRet_c, 1, 0.8, 21600.0, uMinOutAir=min_oa)
    res_above = simulate_rtu(rows_above)
    assert_close(get_final_value(res_above, "yOutDam"), out_fra_above, abs_tol=1e-4,
                 msg="sanity: yOutDam should land at the intended outFraRaw")
    assert get_final_bool(res_above, "economizerStatus") is True, \
        "economizerStatus should flip True once yOutDam clears the tolerance band"


# ─── R26: supply_fan_speed_pct output tracks yFan (core) ───────────────────

@pytest.mark.core
def test_r26_supply_fan_speed_output_tracks_yfan():
    """supply_fan_speed_pct is a dedicated model.json output (fmu_variable
    yFan, conversion fraction_to_pct) for BACnet mapping (RTU-1-Supply-Fan-
    Speed), distinct from the pre-existing fan_command_pct output that also
    reads yFan. Verifies model.json declares it correctly AND that it
    genuinely tracks yFan across varying uFan commands rather than reading
    back as a constant -- the specific failure mode this task called out."""
    from shared.validate_fmu_metadata import load_metadata

    metadata_path = RTU_DIR / "model.json"
    if not RTU_FMU.exists() or not metadata_path.exists():
        pytest.skip("RTU.fmu or model.json not found -- build/create before running this test")

    metadata = load_metadata(metadata_path)
    entries = {o["name"]: o for o in metadata["outputs"]}
    assert "supply_fan_speed_pct" in entries, "supply_fan_speed_pct missing from model.json outputs"
    entry = entries["supply_fan_speed_pct"]
    assert entry["fmu_variable"] == "yFan"
    assert entry["conversion"] == "fraction_to_pct"
    assert entry["unit"] == "percent"

    yfan_values = []
    for uFan in (0.3, 0.5, 0.7, 1.0):
        rows = const_rows(13, 32, 25, uFan, 0.95, 21600.0)
        res = simulate_rtu(rows)
        yFan = get_final_value(res, "yFan")
        supply_fan_speed_pct = yFan * 100.0  # fraction_to_pct, matching apply_output_conversion
        assert_close(supply_fan_speed_pct, uFan * 100.0, abs_tol=1e-4,
                     msg=f"supply_fan_speed_pct should track the commanded uFan={uFan}")
        yfan_values.append(yFan)

    assert len(set(yfan_values)) == len(yfan_values), \
        f"yFan did not vary across the uFan sweep -- would read back as a constant: {yfan_values}"


# ─── R27: compressor COP outdoor-temperature response (core) ───────────────
# DX capacity/part-load performance map regression coverage (R27-R32).
# compressorCOP = clip(steadyStateCOP * coolingPartLoadFactor, COP_min,
# COP_max), steadyStateCOP = clip(COP_nominal - kCOPPerKelvin*(TOut-
# TOutCOPRef), COP_min, COP_max), coolingPartLoadFactor = clip(1 -
# partLoadDegradation*(1-coolingPLR), partLoadFactorMin, 1), coolingPLR =
# coolingCoilCommand = min(1, yCoo*coolingCapacityModifier),
# coolingCapacityModifier = clip(1 - kCapOutPerKelvin*(TOut-TOutCapRef) +
# kCapMixPerKelvin*(TMix-TMixCapRef), capModifierMin, 1). Neither
# coolingCapacityModifier nor steadyStateCOP is an FMU output (both are
# `protected` in RTU.mo), so these tests either recompute them from P's
# mirrored parameters and the model's own OTHER, actually-exposed outputs
# (coolingPLR, TMix), or isolate one term at a time by construction (e.g.
# forcing yCoo to saturate at 1 so coolingCoilCommand tracks
# coolingCapacityModifier directly) rather than assuming a value for them.

@pytest.mark.core
def test_r27_compressor_cop_outdoor_temperature_response():
    """Hotter outdoor air reduces effective compressorCOP -- verified two
    ways: (1) compressorCOP matches the exact analytic formula at each
    point, using the ACTUALLY-simulated coolingPLR (not an assumed
    constant), so the check explicitly accounts for the part-load
    contribution rather than ignoring it; (2) compressorCOP itself is
    monotonically non-increasing across the sweep."""
    touts = [25, 30, 35, 40]
    measured = []
    for tout in touts:
        # Moderate, non-saturating VAV demand (0.6) so coolingCoilCommand
        # stays away from its own min(1, ...) ceiling at every point in the
        # sweep -- keeps the formula cross-check meaningful instead of
        # comparing clamped values.
        rows = const_rows(TSupSet_c=13, TOut_c=tout, TRet_c=25, uFan=1, uVAVDamMax=0.6,
                           final_time=21600.0)
        res = simulate_rtu(rows)
        assert_global_invariants(res)
        cop = get_final_value(res, "compressorCOP")
        plr = get_final_value(res, "coolingPLR")
        measured.append(cop)

        steady_state_cop = max(P["COP_min"], min(P["COP_max"],
            P["COP_nominal"] - P["kCOPPerKelvin"] * (c2k(tout) - P["TOutCOPRef"])))
        part_load_factor = max(P["partLoadFactorMin"], min(1,
            1 - P["partLoadDegradation"] * (1 - plr)))
        expected_cop = max(P["COP_min"], min(P["COP_max"], steady_state_cop * part_load_factor))
        assert_close(cop, expected_cop, rel_tol=0.02,
                     msg=f"compressorCOP formula mismatch at TOut={tout}C (measured coolingPLR={plr})")

    assert all(measured[i] >= measured[i + 1] - 1e-6 for i in range(len(measured) - 1)), \
        "compressorCOP should not increase as TOut rises, even after accounting for part-load"
    assert measured[-1] < measured[0], \
        "hottest scenario should show a strictly lower compressorCOP than the mildest"


# ─── R28: DX capacity temperature response (core) ───────────────────────────

@pytest.mark.core
def test_r28_dx_capacity_derates_with_outdoor_temperature():
    """Higher outdoor-air temperature should derate available/delivered
    cooling capacity. Isolated from controller behavior by forcing yCoo to
    saturate at its PI ceiling of 1 (same oversized-demand technique as
    test_r15b) -- coolingCoilCommand = min(1, 1*coolingCapacityModifier)
    then tracks the capacity correction directly, so QCoolLoad becomes
    (up to the coil's linear Q_flow=u*Q_flow_nominal relationship) a direct
    readout of coolingCapacityModifier's own outdoor-temperature term."""
    touts = [25, 30, 35, 40, 45]
    loads = []
    for tout in touts:
        rows = const_rows(TSupSet_c=-10, TOut_c=tout, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                           final_time=21600.0)
        res = simulate_rtu(rows)
        assert_global_invariants(res)
        yCoo = get_final_value(res, "yCoo")
        assert yCoo >= 1.0 - 1e-3, \
            f"expected yCoo saturated at 1 at TOut={tout}C for this test to isolate the capacity correction"
        loads.append(get_final_value(res, "QCoolLoad"))

    assert all(loads[i] >= loads[i + 1] - 1e-3 for i in range(len(loads) - 1)), \
        "QCoolLoad should not increase as TOut rises under saturated (yCoo=1) demand"
    assert loads[-1] < loads[0], \
        "hottest OAT scenario should deliver strictly less capacity than the mildest"


# ─── R29: DX capacity entering/mixed-air temperature response (core) ───────

@pytest.mark.core
def test_r29_dx_capacity_responds_to_mixed_air_temperature():
    """RTU.mo's capacity modifier includes +kCapMixPerKelvin*(TMix-
    TMixCapRef) -- a positive coefficient, so this model's intended
    direction is that a WARMER mixed/entering-air temperature INCREASES
    available capacity (more usable enthalpy difference across the coil),
    opposite of the outdoor-air term in R28. Isolated the same way as R28
    (saturated yCoo=1 demand) plus haveEconomizer=False (locks yOutDam at
    a fixed, low OA fraction) so varying TRet shifts TMix while TOut --
    and therefore the capacity modifier's OTHER (outdoor) term -- stays
    fixed, holding everything else as constant as practical."""
    trets = [18, 24, 30]
    loads = []
    tmixes = []
    for tret in trets:
        rows = const_rows(TSupSet_c=-10, TOut_c=35, TRet_c=tret, uFan=1, uVAVDamMax=0.8,
                           final_time=21600.0)
        res = simulate_rtu(rows, params={"haveEconomizer": False})
        assert_global_invariants(res)
        yCoo = get_final_value(res, "yCoo")
        assert yCoo >= 1.0 - 1e-3, \
            f"expected yCoo saturated at 1 at TRet={tret}C for this test to isolate the capacity correction"
        loads.append(get_final_value(res, "QCoolLoad"))
        tmixes.append(get_final_value(res, "TMix"))

    assert tmixes[0] < tmixes[1] < tmixes[2], \
        "sanity: higher TRet should raise TMix at a fixed (economizer-disabled) OA fraction"
    assert all(loads[i] <= loads[i + 1] + 1e-3 for i in range(len(loads) - 1)), \
        "higher mixed-air temperature should not decrease delivered capacity (positive kCapMixPerKelvin)"
    assert loads[-1] > loads[0], \
        "warmest mixed-air scenario should deliver strictly more capacity than the coolest"


# ─── R30: part-load efficiency degradation (core) ───────────────────────────

@pytest.mark.core
def test_r30_compressor_cop_part_load_penalty():
    """Compares two stable cooling operating points at the same TOut/TRet
    (economizer/mixed-air conditions held fixed) but markedly different
    part-load ratios, driven via TSupSet (a milder setpoint needs
    proportionally less cooling, so PLR should land noticeably lower).
    Low-part-load operation should receive a real (not just
    theoretically-possible) COP penalty relative to the higher-PLR point,
    unless both happen to be far enough from load that neither exercises
    the penalty meaningfully."""
    rows_high = const_rows(TSupSet_c=13, TOut_c=32, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                            final_time=21600.0)
    res_high = simulate_rtu(rows_high)
    assert_global_invariants(res_high)
    plr_high = get_final_value(res_high, "coolingPLR")
    cop_high = get_final_value(res_high, "compressorCOP")

    rows_low = const_rows(TSupSet_c=24, TOut_c=32, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                           final_time=21600.0)
    res_low = simulate_rtu(rows_low)
    assert_global_invariants(res_low)
    plr_low = get_final_value(res_low, "coolingPLR")
    cop_low = get_final_value(res_low, "compressorCOP")

    assert plr_low < plr_high - 0.05, \
        f"sanity: the milder setpoint should produce a markedly lower part-load ratio " \
        f"(got plr_low={plr_low}, plr_high={plr_high})"

    # Only compare COPs when the compressor is genuinely running at both
    # points -- comparing against an "off" (0) COP would trivially pass
    # without exercising the part-load penalty at all.
    if cop_low > 1e-6 and cop_high > 1e-6:
        assert cop_low <= cop_high + 1e-6, \
            f"lower-part-load point (PLR={plr_low}, COP={cop_low}) should not show a HIGHER " \
            f"COP than the higher-PLR point (PLR={plr_high}, COP={cop_high})"


# ─── R31: yCoo (controller demand) vs coolingPLR (post-correction DX load) ──

@pytest.mark.core
def test_r31_cooling_command_vs_cooling_plr_relationship():
    """yCoo is the SAT PI controller's raw cooling demand, 0..1.
    coolingPLR (=coolingCoilCommand) is the resulting DX loading AFTER the
    outdoor/mixed-air capacity correction: coolingPLR = min(1,
    yCoo*coolingCapacityModifier), so coolingPLR must never exceed yCoo,
    and is strictly below it whenever the capacity modifier is < 1
    (derated conditions) -- verified here at a hot-enough TOut that the
    modifier is meaningfully under 1 without hitting capModifierMin."""
    rows = const_rows(TSupSet_c=18, TOut_c=45, TRet_c=25, uFan=1, uVAVDamMax=0.8,
                       final_time=21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    yCoo = get_final_value(res, "yCoo")
    coolingPLR = get_final_value(res, "coolingPLR")

    assert -1e-6 <= yCoo <= 1 + 1e-6, f"yCoo={yCoo} outside [0,1]"
    assert -1e-6 <= coolingPLR <= 1 + 1e-6, f"coolingPLR={coolingPLR} outside [0,1]"
    assert yCoo > 0.01, "sanity: this scenario should have meaningful cooling demand"
    assert coolingPLR <= yCoo + 1e-6, \
        "coolingPLR (post-capacity-correction DX loading) must never exceed yCoo (raw controller demand)"
    assert coolingPLR < yCoo - 1e-4, \
        "coolingPLR should be strictly below yCoo under derated (hot-OAT) capacity conditions"


# ─── R32: OAT regression scenarios -- load/COP/power chain (core) ──────────

@pytest.mark.core
def test_r32_oat_regression_scenarios_cop_and_power_chain():
    """Three representative steady operating points -- mild/favorable,
    ~30 degC, and ~38-40 degC outdoor conditions -- at a fixed, moderate
    (non-saturating) cooling setpoint/demand, verifying the overall
    expected physical response chain as OAT rises: compressor COP falls
    and compressor power rises. (Delivered cooling load itself is checked
    for staying active/positive at every point, but not asserted
    monotonic here -- the capacity-derating term and the SAT PI's own
    demand response can trade off against each other at a fixed setpoint,
    so QCoolLoad's own trend isn't a safe invariant to assert without
    saturating the loop the way R28 deliberately does.)"""
    scenarios = [("mild/favorable", 22), ("hot", 30), ("very hot", 39)]
    results = []
    for label, tout in scenarios:
        rows = const_rows(TSupSet_c=16, TOut_c=tout, TRet_c=25, uFan=1, uVAVDamMax=0.7,
                           final_time=21600.0)
        res = simulate_rtu(rows)
        assert_global_invariants(res)
        results.append({
            "label": label, "tout": tout,
            "QCoolLoad": get_final_value(res, "QCoolLoad"),
            "compressorCOP": get_final_value(res, "compressorCOP"),
            "PCompressor": get_final_value(res, "PCompressor"),
        })

    for r in results:
        assert r["QCoolLoad"] > 0, f"{r['label']} ({r['tout']}C): expected an active cooling load"
        assert r["compressorCOP"] > 0, f"{r['label']} ({r['tout']}C): expected the compressor running"

    for a, b in zip(results, results[1:]):
        assert b["compressorCOP"] <= a["compressorCOP"] + 1e-6, \
            f"COP should not rise from {a['label']} ({a['tout']}C) to {b['label']} ({b['tout']}C)"
        assert b["PCompressor"] >= a["PCompressor"] - 1e-3, \
            f"compressor power should not fall from {a['label']} ({a['tout']}C) to {b['label']} ({b['tout']}C)"

    assert results[-1]["compressorCOP"] < results[0]["compressorCOP"], \
        "hottest scenario should show a strictly lower COP than the mildest"
    assert results[-1]["PCompressor"] > results[0]["PCompressor"], \
        "hottest scenario should show strictly higher compressor power than the mildest"

    # Energy consistency at the hottest scenario (test_r15 already covers
    # this generically -- re-checked here specifically at this test's own
    # most-loaded regression point).
    res_hottest = simulate_rtu(
        const_rows(TSupSet_c=16, TOut_c=39, TRet_c=25, uFan=1, uVAVDamMax=0.7, final_time=21600.0))
    assert_close(
        get_final_value(res_hottest, "totalElectricPower"),
        results[-1]["PCompressor"] + get_final_value(res_hottest, "PFan"),
        rel_tol=1e-6, msg="totalElectricPower vs PCompressor+PFan at the hottest regression scenario")


# ═══════════════════════════════════════════════════════════════════════════
# EXTENDED -- slower sweeps/stability/parameter/regression tests.
# Not run by default CI; use `pytest models/rtu/tests -m extended` or the
# workflow's workflow_dispatch "extended" input.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.extended
def test_r19_full_economizer_saturation():
    rows = const_rows(4, 5, 24, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    assert get_final_value(res, "yOutDam") > 0.999
    assert_close(get_final_value(res, "TMix"), c2k(5), abs_tol=1.0)


@pytest.mark.extended
def test_r20_six_hour_stability():
    rows = const_rows(13, 32, 25, 1, 0.8, 21600.0)
    res = simulate_rtu(rows)
    assert_global_invariants(res)
    assert_finite(res)


@pytest.mark.extended
def test_r21_alternate_cooling_capacity_override():
    rows = const_rows(13, 32, 25, 1, 0.8, 21600.0)
    res = simulate_rtu(rows, params={"QCoo_flow_nominal": -20000.0})
    assert_finite(res)
    assert get_final_value(res, "QCoolLoad") <= 20000.0 + 1e-3


@pytest.mark.extended
def test_r22_sat_setpoint_step_response():
    base = [c2k(16), c2k(32), c2k(25), 1, 0.8, P["minOutAirFra"]]
    rows = [[0.0] + base, [600.0] + base]
    step = base.copy(); step[0] = c2k(13)
    rows += [[600.05] + step, [3600.0] + step]
    res = simulate_rtu(rows, ncp=240)
    assert_global_invariants(res)
    yCoo_before = float(np.interp(600.0, res["time"], get_series(res, "yCoo")))
    yCoo_after = get_final_value(res, "yCoo")
    assert yCoo_after > yCoo_before
    assert_close(get_final_value(res, "TSup"), c2k(13), abs_tol=0.5)
