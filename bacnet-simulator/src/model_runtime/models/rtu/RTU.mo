within ;
model RTU
  "Packaged rooftop unit: DX cooling, gas heat, economizer, VAV-capable supply fan"

  // ===========================================================================
  // Standalone packaged-air-system counterpart to the central plant
  // architecture already in this repo:
  //   SimpleChillerPlant -> AHU -> VAV -> ThermalZone
  //   BoilerPlant          -> AHU -> VAV -> ThermalZone
  // This model instead serves:
  //   Weather -> RTU -> VAV -> ThermalZone
  // with its own self-contained DX cooling and gas heating -- no
  // TChiWatSup/TChiWatRet/VChiWat_flow/THotWatSup/THotWatRet inputs, by
  // design (see Documentation, "No central plant inputs").
  //
  // Control-sequence pattern (SAT PI, fan static-pressure PI with most-
  // open-VAV reset, minimum-OA economizer, uFan as enable/max-speed limit)
  // is carried over from models/ahu/AHU.mo, which is not modified by this
  // model. Only the generic air-side/control ideas are reused -- AHU.mo's
  // chilled-water coil and this project's central-plant signal interface
  // are not applicable to a packaged unit with its own DX refrigerant
  // circuit and gas furnace.
  // ===========================================================================

  replaceable package MediumA = Buildings.Media.Air
    constrainedby Modelica.Media.Interfaces.PartialMedium
    "Air medium";

  // ---------------------------------------------------------------------------
  // Air-side design parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal = 2.0
    "Nominal supply air mass flow rate";

  parameter Modelica.Units.SI.Volume VMix = 2.0
    "Mixed-air plenum volume";

  parameter Real minOutAirFra(min=0, max=1) = 0.15
    "Default/backward-compatible minimum outdoor-air fraction. No longer
    used directly by the economizer equations (see uMinOutAir) -- kept as
    uMinOutAir's own start value, so the exported FMU still behaves
    sensibly (same 0.15 floor as before) if a caller never explicitly
    drives uMinOutAir, and so a standalone Modelica simulation of this
    model (no FMU wrapper) keeps working unchanged.";

  parameter Modelica.Units.SI.PressureDifference dpAir_nominal = 150
    "Air-side pressure drop through the DX coil and heating section combined
    at nominal flow. Filter pressure drop is not separately modeled as a
    distinct component in v1 -- it is accounted for only as part of the
    overall duct static-pressure budget (dpSup_nominal below), matching how
    models/ahu/AHU.mo also lumps its coils' air-side dp rather than
    modeling a full duct pressure network.";

  // ---------------------------------------------------------------------------
  // SAT control parameters
  // ---------------------------------------------------------------------------
  parameter Real kSAT = 0.20
    "PI proportional gain for supply-air-temperature control";

  parameter Modelica.Units.SI.Time TiSAT = 120
    "PI integral time for supply-air-temperature control";

  // ---------------------------------------------------------------------------
  // Supply-fan static-pressure control parameters
  // ---------------------------------------------------------------------------
  parameter Real kFan = 0.002
    "PI proportional gain for supply-duct static-pressure control";

  parameter Modelica.Units.SI.Time TiFan = 60
    "PI integral time for supply-duct static-pressure control";

  parameter Modelica.Units.SI.PressureDifference dpSup_nominal = 500
    "Supply-duct static pressure at full fan speed";

  parameter Modelica.Units.SI.PressureDifference dpSupSetMin = 200
    "Minimum supply-duct static-pressure setpoint";

  parameter Modelica.Units.SI.PressureDifference dpSupSetMax = 500
    "Maximum supply-duct static-pressure setpoint";

  parameter Modelica.Units.SI.PressureDifference dpSupSetFixed = dpSupSetMax
    "Fixed duct static-pressure setpoint used when haveVAVControl=false";

  parameter Real vavDamLow(min=0, max=1) = 0.60
    "Most-open VAV damper position below which static-pressure setpoint is minimum";

  parameter Real vavDamHigh(min=0, max=1) = 0.90
    "Most-open VAV damper position above which static-pressure setpoint is maximum";

  parameter Modelica.Units.SI.Power PSupFan_nominal = 5000
    "Nominal supply-fan electrical power at full command";

  // ---------------------------------------------------------------------------
  // DX cooling parameters
  //
  // Idealized sensible coil (Buildings.Fluid.HeatExchangers.HeaterCooler_u,
  // the same component AHU.mo and this model's own heating section use) plus
  // a bounded, documented compressor-power/COP approximation, NOT a real
  // Buildings.Fluid.DXSystems component -- see Documentation, 'DX cooling
  // component selection', for the two genuine attempts with the real
  // SingleSpeed DX coil that were tried and rejected first.
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.HeatFlowRate QCoo_flow_nominal = -36846.82
    "Nominal sensible cooling capacity (negative). Value carried over from
    the Carrier Weathermaster 50HJ012 unit investigated for the real DX
    coil attempt (see Documentation) -- kept for continuity with that sizing
    exercise even though the coil itself is now idealized.";

  parameter Real COP_nominal(unit="1") = 3.71
    "Compressor COP at TOutCOPRef. Also carried over from the Carrier
    Weathermaster 50HJ012 record's own rated COP.";

  parameter Modelica.Units.SI.Temperature TOutCOPRef = 308.15
    "Outdoor temperature at which COP_nominal applies (35 degC, a standard
    AHRI-style outdoor rating point)";

  parameter Real kCOPPerKelvin(unit="1/K") = 0.04
    "COP degradation slope per kelvin of TOut above TOutCOPRef (and
    improvement per kelvin below it) -- condenser rejects heat less
    effectively as outdoor temperature rises, raising condensing pressure
    and reducing COP; the reverse holds as TOut falls. A simple linear
    approximation, not a manufacturer curve -- see Documentation.";

  parameter Real COP_min(unit="1") = 2.0
    "Lower bound on compressorCOP regardless of TOut";

  parameter Real COP_max(unit="1") = 5.0
    "Upper bound on steady-state compressor COP regardless of TOut";

  parameter Modelica.Units.SI.Temperature TOutCapRef = 308.15
    "Outdoor-air reference temperature for DX sensible-capacity modifier (35 degC)";

  parameter Modelica.Units.SI.Temperature TMixCapRef = 299.15
    "Entering/mixed-air reference temperature for DX sensible-capacity modifier (26 degC)";

  parameter Real kCapOutPerKelvin(unit="1/K") = 0.006
    "Fractional sensible-capacity reduction per kelvin of outdoor temperature above TOutCapRef";

  parameter Real kCapMixPerKelvin(unit="1/K") = 0.010
    "Fractional sensible-capacity change per kelvin of mixed-air temperature relative to TMixCapRef";

  parameter Real capModifierMin(min=0, max=1) = 0.65
    "Lower bound on the DX sensible-capacity temperature modifier";

  parameter Real partLoadDegradation(min=0, max=1) = 0.15
    "Generic DX cycling/part-load degradation coefficient; 0 means no part-load efficiency penalty";

  parameter Real partLoadFactorMin(min=0.1, max=1) = 0.70
    "Lower bound on DX part-load factor used to keep effective COP physical at very low load";

  // Optional packaged-DX staging surrogate. The default false preserves the
  // existing continuously modulating cooling behaviour exactly. When enabled,
  // the SAT controller demand is quantized to off / stage 1 / stage 2 before
  // the existing temperature-capacity modifier is applied. This mirrors the
  // staged-compressor architecture commonly found in packaged RTUs (including
  // the public LBNL/NREL benchmark datasets) without introducing refrigerant-
  // circuit states or anti-short-cycle timers that would reduce FMU robustness.
  parameter Boolean useTwoStageCooling = false
    "Enable two-stage packaged-DX cooling surrogate instead of continuous modulation";

  parameter Real stage1Fraction(min=0.05, max=0.95) = 0.67
    "Stage-1 fraction of nominal sensible cooling capacity";

  parameter Real stage1DemandThreshold(min=0, max=1) = 0.02
    "Controller demand above which stage 1 is enabled";

  parameter Real stage2DemandThreshold(min=0, max=1) = 0.67
    "Controller demand above which stage 2 is enabled";

  // ---------------------------------------------------------------------------
  // Gas heating parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.HeatFlowRate QHea_flow_nominal = 30000
    "Nominal sensible heating capacity";

  parameter Modelica.Units.SI.Efficiency etaHeat = 0.80
    "Gas furnace steady-state combustion efficiency (useful heat output /
    fuel input power). A single fixed value, not a part-load- or
    temperature-dependent curve -- explicitly acceptable for v1 per this
    model's own scope (see Documentation, 'Gas heating').";

  // ---------------------------------------------------------------------------
  // Configuration hooks
  // ---------------------------------------------------------------------------
  parameter Boolean haveEconomizer = true
    "Set to false to disable the outdoor-air economizer reset and lock yOutDam at uMinOutAir";

  parameter Boolean haveVAVControl = true
    "Set to false for a CAV unit: dpSupSet becomes the fixed dpSupSetFixed
    instead of the most-open-VAV-damper reset. Keeps the same model usable
    for both packaged VAV and packaged CAV without duplicating it.";

  parameter Real economizerActiveTol(min=0) = 0.02
    "Small tolerance above uMinOutAir before economizerStatus reports
    active -- avoids a false-positive/chattering status right at the
    minimum-OA floor from solver/settling noise. Status-derivation only;
    does not affect yOutDamCmd or yOutDam itself.";

  // ---------------------------------------------------------------------------
  // FMU-facing inputs
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.RealInput TSupSet(
    final unit="K",
    displayUnit="degC")
    "Supply-air temperature setpoint";

  Modelica.Blocks.Interfaces.RealInput TOut(
    final unit="K",
    displayUnit="degC")
    "Outdoor-air temperature; used for economizer control, mixed-air
    calculation, and compressor COP approximation";

  Modelica.Blocks.Interfaces.RealInput TRet(
    final unit="K",
    displayUnit="degC")
    "Return-air temperature";

  Modelica.Blocks.Interfaces.RealInput uFan(
    final unit="1")
    "Supply fan enable/maximum command, 0..1";

  Modelica.Blocks.Interfaces.RealInput uVAVDamMax(
    final unit="1")
    "Most-open downstream VAV damper position, 0..1 (ignored when haveVAVControl=false)";

  Modelica.Blocks.Interfaces.RealInput uMinOutAir(
    final unit="1",
    start=minOutAirFra)
    "Minimum outdoor-air economizer floor position, 0..1 -- FMU-facing
    replacement for the formerly-fixed minOutAirFra parameter. The
    economizer command equations (see yOutDamCmd) use this value directly, so it can
    be driven live (e.g. an operator/BAS point) instead of being baked in
    at compile time. start=minOutAirFra so a caller that never explicitly
    sets this input still gets the model's original default behavior.";

  Modelica.Blocks.Interfaces.BooleanInput uOutDamOvrEna(
    start=false)
    "Outdoor-air damper actuator override enable. False = normal economizer
    command drives the physical damper. True = uOutDamOvr forces the actual
    damper position for actuator-fault simulation.";

  Modelica.Blocks.Interfaces.RealInput uOutDamOvr(
    final unit="1",
    start=minOutAirFra)
    "Forced outdoor-air damper actual position, 0..1, used only when
    uOutDamOvrEna=true. This is an actuator/fault input, not feedback from
    yOutDam, and must not be mapped from the damper-position output itself.";

  // ---------------------------------------------------------------------------
  // FMU-facing outputs
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.RealOutput TSup(
    final unit="K",
    displayUnit="degC")
    "Supply-air temperature";

  Modelica.Blocks.Interfaces.RealOutput TMix(
    final unit="K",
    displayUnit="degC")
    "Mixed-air temperature";

  Modelica.Blocks.Interfaces.RealOutput VSup_flow(
    final unit="m3/s")
    "Supply-air volume flow rate";

  Modelica.Blocks.Interfaces.RealOutput yFan(
    final unit="1")
    "Actual normalized fan/airflow command";

  Modelica.Blocks.Interfaces.RealOutput yOutDamCmd(
    final unit="1")
    "Outdoor-air/economizer damper command requested by the RTU controller,
    0..1. This remains the controller intent even when an actuator override
    forces the physical damper elsewhere.";

  Modelica.Blocks.Interfaces.RealOutput yOutDam(
    final unit="1")
    "Actual physical outdoor-air damper position, 0..1. Equals yOutDamCmd in
    normal operation; when uOutDamOvrEna=true it is forced by uOutDamOvr.
    Outdoor-airflow and mixed-air physics use this actual position.";

  Modelica.Blocks.Interfaces.RealOutput VOutAir_flow(
    final unit="m3/s")
    "Outdoor-air volume flow rate -- see Documentation, 'Outdoor-air volume
    flow output', for the derivation";

  Modelica.Blocks.Interfaces.RealOutput yCoo(
    final unit="1")
    "Continuous cooling demand requested by the SAT controller, 0..1. In the
    default modulating mode it drives the cooling-capacity fraction directly;
    when useTwoStageCooling=true it is converted to off/stage-1/stage-2 before
    the temperature-dependent capacity modifier is applied.";

  Modelica.Blocks.Interfaces.RealOutput yHea(
    final unit="1")
    "Heating command requested by SAT controller";

  Modelica.Blocks.Interfaces.RealOutput QCoolLoad(
    final unit="W")
    "Cooling delivered to the air, application-level convention (positive
    when cooling), from the idealized coil's own Q_flow";

  Modelica.Blocks.Interfaces.RealOutput QHeaLoad(
    final unit="W")
    "Gas furnace heating delivered to the air";

  Modelica.Blocks.Interfaces.RealOutput PCompressor(
    final unit="W")
    "DX compressor electrical power, derived as QCoolLoad/compressorCOP --
    see Documentation, 'Compressor power and COP'";

  Modelica.Blocks.Interfaces.RealOutput PFan(
    final unit="W")
    "Calculated supply-fan electrical power";

  Modelica.Blocks.Interfaces.RealOutput dpSup(
    final unit="Pa")
    "Calculated supply-duct static pressure";

  Modelica.Blocks.Interfaces.RealOutput dpSupSet(
    final unit="Pa")
    "Reset (or fixed) supply-duct static-pressure setpoint";

  Modelica.Blocks.Interfaces.RealOutput coolingPLR(
    final unit="1")
    "Cooling part-load ratio diagnostic after temperature-dependent capacity
    derating. yCoo remains controller demand while coolingPLR represents the
    actual normalized sensible-coil loading used for energy calculations.";

  Modelica.Blocks.Interfaces.RealOutput heatingPLR(
    final unit="1")
    "Heating part-load ratio diagnostic. Identical to yHea for this
    single-stage-furnace v1, same forward-compatibility rationale as coolingPLR.";

  Modelica.Blocks.Interfaces.RealOutput totalElectricPower(
    final unit="W")
    "PCompressor + PFan (gas furnace fuel input is not electrical -- see gasHeatingPower)";

  Modelica.Blocks.Interfaces.RealOutput gasHeatingPower(
    final unit="W")
    "Gas furnace fuel input power = QHeaLoad / etaHeat -- NOT electrical power";

  Modelica.Blocks.Interfaces.RealOutput compressorCOP(
    final unit="1")
    "Effective compressor COP after outdoor-temperature and part-load
    efficiency effects; 0 when the compressor is off (QCoolLoad ~ 0)";

  Modelica.Blocks.Interfaces.RealOutput availableCoolingCapacity(
    final unit="W")
    "Positive available sensible DX cooling capacity after outdoor- and
    entering-air temperature derating, before controller/stage loading";

  Modelica.Blocks.Interfaces.RealOutput compressorStage(
    final unit="1")
    "Packaged-DX compressor stage diagnostic: 0=off, 1=stage 1, 2=stage 2.
    In continuous-modulating mode it reports 1 whenever cooling is active.";

  // ---------------------------------------------------------------------------
  // BACnet BI status outputs -- pure derived diagnostics of existing
  // continuous outputs, no new physics. See Documentation, 'BACnet status
  // outputs'.
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.BooleanOutput supplyFanStatus
    "True when the supply fan is commanded on (yFan > 0.01). Maps to the
    BACnet Supply-Fan-Status BI point.";

  Modelica.Blocks.Interfaces.BooleanOutput coolingStatus
    "True when DX cooling is active (coolingPLR > 0.01). Maps to the
    BACnet Cooling-Status BI point.";

  Modelica.Blocks.Interfaces.BooleanOutput heatingStatus
    "True when gas heating is active (heatingPLR > 0.01). Maps to the
    BACnet Heating-Status BI point.";

  Modelica.Blocks.Interfaces.BooleanOutput economizerStatus
    "True when the economizer command yOutDamCmd is more than
    economizerActiveTol above its minimum-OA floor (uMinOutAir). Maps to
    the BACnet Economizer-Status BI point.";

protected
  Real mAir_flow_cmd(unit="kg/s");
  Real mOut_flow_cmd(unit="kg/s");
  Real mRet_flow_cmd(unit="kg/s");
  Real outFraRaw;
  Real vavDamResetFra(unit="1")
    "Normalized VAV-demand signal used for static-pressure reset";
  Real coolingCapacityModifier(unit="1")
    "Bounded DX sensible-capacity modifier from outdoor and entering-air temperatures";
  Real coolingStageCommand(unit="1")
    "Pre-derating normalized cooling fraction after optional compressor staging";
  Real coolingCoilCommand(unit="1")
    "Actual normalized sensible-coil capacity fraction after temperature derating";
  Real steadyStateCOP(unit="1")
    "Temperature-adjusted compressor COP before part-load degradation";
  Real coolingPartLoadFactor(unit="1")
    "DX part-load efficiency factor applied to steadyStateCOP";

  // ---------------------------------------------------------------------------
  // Mixing / economizer section (mirrors AHU.mo's air-side pattern exactly)
  // ---------------------------------------------------------------------------
  Buildings.Fluid.Sources.MassFlowSource_T outAir(
    redeclare package Medium = MediumA,
    use_m_flow_in=true,
    use_T_in=true,
    nPorts=1)
    "Outdoor-air source";

  Buildings.Fluid.Sources.MassFlowSource_T retAir(
    redeclare package Medium = MediumA,
    use_m_flow_in=true,
    use_T_in=true,
    nPorts=1)
    "Return-air source";

  Buildings.Fluid.MixingVolumes.MixingVolume mix(
    redeclare package Medium = MediumA,
    V=VMix,
    nPorts=3,
    m_flow_nominal=mAir_flow_nominal,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial,
    T_start=295.15)
    "Mixed-air plenum";

  // ---------------------------------------------------------------------------
  // Supply fan / duct-pressure section
  // ---------------------------------------------------------------------------
  Buildings.Fluid.Sources.Boundary_pT sinAir(
    redeclare package Medium = MediumA,
    p=101325,
    nPorts=1)
    "Supply duct/downstream (VAV/zone) pressure boundary";

  // ---------------------------------------------------------------------------
  // Gas heating section -- idealized sensible heater (same component AHU.mo
  // uses for its own heating), plus a fixed-efficiency fuel-power
  // calculation. See Documentation, 'Gas heating', for why this is
  // sufficient for v1 rather than a packaged-furnace fluid component.
  // ---------------------------------------------------------------------------
  Buildings.Fluid.HeatExchangers.HeaterCooler_u heaCoi(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    Q_flow_nominal=QHea_flow_nominal,
    dp_nominal=50,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial)
    "Idealized sensible gas-furnace heating section";

  // ---------------------------------------------------------------------------
  // DX cooling section -- idealized sensible coil, NOT a real
  // Buildings.Fluid.DXSystems component. See Documentation, 'DX cooling
  // component selection', for the two genuine attempts made with the real
  // SingleSpeed DX coil (both empirically tested against the actual
  // exported FMU, not assumed) and why both were rejected.
  // ---------------------------------------------------------------------------
  Buildings.Fluid.HeatExchangers.HeaterCooler_u coolingCoil(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    Q_flow_nominal=QCoo_flow_nominal,
    dp_nominal=dpAir_nominal,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial)
    "Idealized sensible DX cooling section";

  // ---------------------------------------------------------------------------
  // Sensors
  // ---------------------------------------------------------------------------
  Buildings.Fluid.Sensors.TemperatureTwoPort senTMix(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    tau=10)
    "Mixed-air temperature sensor";

  Buildings.Fluid.Sensors.TemperatureTwoPort senTSup(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    tau=10)
    "Supply-air temperature sensor";

  Buildings.Fluid.Sensors.VolumeFlowRate senFlo(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    tau=10)
    "Supply-air volume flow sensor";

  // ---------------------------------------------------------------------------
  // Control logic
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Continuous.LimPID fanCon(
    controllerType=Modelica.Blocks.Types.SimpleController.PI,
    k=kFan,
    Ti=TiFan,
    yMax=1,
    yMin=0,
    initType=Modelica.Blocks.Types.Init.InitialOutput,
    y_start=0.8)
    "Supply-fan static-pressure controller";

  Modelica.Blocks.Continuous.LimPID satCon(
    controllerType=Modelica.Blocks.Types.SimpleController.PI,
    k=kSAT,
    Ti=TiSAT,
    yMax=1,
    yMin=-1,
    initType=Modelica.Blocks.Types.Init.InitialOutput,
    y_start=0)
    "Closed-loop SAT controller: positive=heating, negative=cooling";

equation
  // ---------------------------------------------------------------------------
  // Supply-fan static-pressure control, airflow, and fan power
  // (unchanged formulas from AHU.mo)
  // ---------------------------------------------------------------------------
  vavDamResetFra =
    max(0, min(1,
      (uVAVDamMax - vavDamLow) /
      max(0.01, vavDamHigh - vavDamLow)));

  dpSupSet =
    if not haveVAVControl then
      dpSupSetFixed
    else
      dpSupSetMin + (dpSupSetMax - dpSupSetMin) * vavDamResetFra;

  fanCon.u_s = dpSupSet;
  fanCon.u_m = dpSup;

  yFan =
    if uFan > 0.01 then
      min(max(0, min(1, uFan)), fanCon.y)
    else
      0;

  dpSup = dpSup_nominal * yFan^2;

  mAir_flow_cmd = mAir_flow_nominal * yFan;

  PFan = PSupFan_nominal * yFan^3;

  // ---------------------------------------------------------------------------
  // Outdoor-air/economizer control (command logic from AHU.mo + actuator override layer)
  // ---------------------------------------------------------------------------
  outFraRaw =
    if TOut < TRet and abs(TRet - TOut) > 0.01 then
      (TRet - TSupSet) / (TRet - TOut)
    else
      uMinOutAir;

  // Controller command is kept separate from actual actuator position so
  // a mechanical/actuator fault can be simulated without corrupting the
  // economizer controller itself or creating an output->input self-loop.
  yOutDamCmd =
    if not haveEconomizer then
      uMinOutAir
    elseif TOut < TRet then
      max(uMinOutAir, min(1, outFraRaw))
    else
      uMinOutAir;

  // Normal operation follows the economizer command. When explicitly
  // overridden, the physical damper is forced to the requested fault
  // position while the controller command continues to be calculated.
  yOutDam =
    if uOutDamOvrEna then
      max(0, min(1, uOutDamOvr))
    else
      yOutDamCmd;

  mOut_flow_cmd = mAir_flow_cmd * yOutDam;
  mRet_flow_cmd = mAir_flow_cmd - mOut_flow_cmd;

  // Outdoor-air volume flow: derived from the measured supply-air volume
  // flow (senFlo.V_flow, via the VSup_flow output below) rather than a
  // separate mass-flow/fixed-density conversion of mOut_flow_cmd -- see
  // Documentation, 'Outdoor-air volume flow output', for why this is the
  // more consistent choice. yOutDam in [uMinOutAir, 1] guarantees
  // 0 <= VOutAir_flow <= VSup_flow by construction.
  VOutAir_flow = VSup_flow * yOutDam;

  outAir.m_flow_in = mOut_flow_cmd;
  retAir.m_flow_in = mRet_flow_cmd;
  outAir.T_in = TOut;
  retAir.T_in = TRet;

  // ---------------------------------------------------------------------------
  // Closed-loop SAT control
  // ---------------------------------------------------------------------------
  satCon.u_s = TSupSet;
  satCon.u_m = senTSup.T;

  yHea =
    if yFan > 0.01 then
      max(0, satCon.y)
    else
      0;

  yCoo =
    if yFan > 0.01 then
      max(0, -satCon.y)
    else
      0;

  heaCoi.u = yHea;

  // Generic DX sensible-capacity performance map. The nominal coil remains
  // algebraic and FMU-stable, while its available capacity now responds to
  // outdoor and entering/mixed-air temperature. Capacity is intentionally
  // capped at the nominal rating; QCoo_flow_nominal therefore remains the
  // maximum sensible capacity parameter to be identified during calibration.
  coolingCapacityModifier =
    max(capModifierMin, min(1,
      1 - kCapOutPerKelvin*(TOut - TOutCapRef)
        + kCapMixPerKelvin*(senTMix.T - TMixCapRef)));

  // Keep the controller itself continuous. Only the equipment response is
  // optionally staged, which lets the same RTU represent either a modulating
  // compressor or a common two-stage packaged unit without changing inputs.
  coolingStageCommand =
    if useTwoStageCooling then
      (if yCoo <= stage1DemandThreshold then 0
       elseif yCoo < stage2DemandThreshold then stage1Fraction
       else 1)
    else
      yCoo;

  coolingCoilCommand = min(1, coolingStageCommand * coolingCapacityModifier);
  coolingCoil.u = coolingCoilCommand;

  availableCoolingCapacity = -QCoo_flow_nominal * coolingCapacityModifier;

  compressorStage =
    if coolingCoilCommand <= 0.01 then 0
    elseif useTwoStageCooling and coolingStageCommand < 0.999 then 1
    elseif useTwoStageCooling then 2
    else 1;

  // ---------------------------------------------------------------------------
  // Cooling outputs -- temperature-dependent sensible capacity, bounded COP,
  // and a generic part-load efficiency penalty. This deliberately stays
  // algebraic rather than reintroducing the unstable DX fluid component.
  // ---------------------------------------------------------------------------
  // HeaterCooler_u's own convention: Q_flow = u*Q_flow_nominal, negative
  // here since Q_flow_nominal (QCoo_flow_nominal) is negative. Flipped to
  // match this model's positive-when-cooling application convention.
  QCoolLoad = -coolingCoil.Q_flow;

  steadyStateCOP =
    max(COP_min, min(COP_max,
      COP_nominal - kCOPPerKelvin*(TOut - TOutCOPRef)));

  coolingPLR = coolingCoilCommand;

  coolingPartLoadFactor =
    max(partLoadFactorMin, min(1,
      1 - partLoadDegradation*(1 - coolingPLR)));

  compressorCOP =
    if QCoolLoad > 1.0 then
      max(COP_min, min(COP_max, steadyStateCOP * coolingPartLoadFactor))
    else
      0;

  PCompressor = if QCoolLoad > 1.0 then QCoolLoad/compressorCOP else 0;

  // ---------------------------------------------------------------------------
  // Heating outputs
  // ---------------------------------------------------------------------------
  QHeaLoad = QHea_flow_nominal * yHea;
  gasHeatingPower = QHeaLoad / etaHeat;
  heatingPLR = yHea;

  totalElectricPower = PCompressor + PFan;

  // ---------------------------------------------------------------------------
  // BACnet BI status outputs -- pure threshold derivations of already-
  // computed continuous outputs above; no new control logic, no feedback
  // into yFan/yCoo/yHea/yOutDam. See Documentation, 'BACnet status outputs'.
  // ---------------------------------------------------------------------------
  supplyFanStatus = yFan > 0.01;
  coolingStatus = coolingPLR > 0.01;
  heatingStatus = heatingPLR > 0.01;
  // Status represents economizer/controller intent, not physical actuator
  // feedback. A stuck damper can therefore produce the diagnostically useful
  // condition: economizerStatus=true while yOutDam differs from yOutDamCmd.
  economizerStatus = yOutDamCmd > uMinOutAir + economizerActiveTol;

  // ---------------------------------------------------------------------------
  // Air path
  // ---------------------------------------------------------------------------
  connect(outAir.ports[1], mix.ports[1]);
  connect(retAir.ports[1], mix.ports[2]);
  connect(mix.ports[3], senTMix.port_a);
  connect(senTMix.port_b, heaCoi.port_a);
  connect(heaCoi.port_b, coolingCoil.port_a);
  connect(coolingCoil.port_b, senTSup.port_a);
  connect(senTSup.port_b, senFlo.port_a);
  connect(senFlo.port_b, sinAir.ports[1]);

  // ---------------------------------------------------------------------------
  // Exposed air-side measurements
  // ---------------------------------------------------------------------------
  TMix = senTMix.T;
  TSup = senTSup.T;
  VSup_flow = senFlo.V_flow;

  annotation (
    experiment(
      StartTime=0,
      StopTime=21600,
      Interval=60,
      Tolerance=1e-6),
    Documentation(info="<html>
<p>
Experimental packaged rooftop unit (RTU): DX cooling, gas heat, dry-bulb
economizer, and a VAV-capable supply fan, intended to serve downstream VAV
terminals or thermal zones directly without a central chiller/boiler plant:
</p>
<pre>
Weather -&gt; RTU -&gt; VAV -&gt; ThermalZone
</pre>
<p>
This completes a second, standalone architecture alongside the central-plant
one already in this repo (<code>SimpleChillerPlant/BoilerPlant -&gt; AHU -&gt;
VAV -&gt; ThermalZone</code>) -- RTU.mo does not modify, and is not modified
by, AHU.mo, SimpleChillerPlant.mo, BoilerPlant.mo, SimpleVAVZone.mo, or
ThermalZone.mo.
</p>
<h4>Control sequence: reused from AHU.mo, not copied wholesale</h4>
<p>
The SAT PI control (positive=heating, negative=cooling), fan static-pressure
PI control with most-open-VAV-damper reset, minimum-outdoor-air economizer,
and <code>uFan</code> enable/max-speed-limit semantics are the same pattern
as <code>models/ahu/AHU.mo</code>, since that is proven, tested control
logic independent of how the thermal energy itself is produced. AHU.mo's
chilled-water coil and this project's central-plant FMU signal interface
(<code>TChiWatSup</code>, etc.) are not reused -- a packaged unit has no
plant to interface with.
</p>
<h4>DX cooling component selection</h4>
<p>
<b>The real Buildings.Fluid.DXSystems.Cooling.AirSource.SingleSpeed coil was
tried first, built, and empirically tested against the actual exported
FMU -- not assumed to work -- and rejected after two genuine fix attempts
both failed on this project's own baseline scenario (cold start, fan off,
uFan=0, exercised by every model in this repo's own test suites as their
first/simplest test). This section documents that investigation in full,
per this task's own instruction not to silently fall back without
explanation.</b>
</p>
<p>
<code>Buildings.Fluid.DXSystems.Cooling.AirSource.SingleSpeed</code> (the
package that superseded the older <code>Buildings.Fluid.HeatExchangers.
DXCoils</code>, which does not exist in this Buildings 13 install) was
selected initially, over <code>MultiStage</code>/<code>VariableSpeed</code>,
because single-stage DX is this model's own v1 scope. Its performance data
was populated from the bundled <code>Buildings.Fluid.DXSystems.Cooling.
AirSource.Data.SingleSpeed.Carrier_Weathermaster_50HJ012</code> record -- a
real, EnergyPlus-validated dataset (<code>Coil:Cooling:DX:SingleSpeed</code>,
36,846.82 W / COP 3.71 / SHR 0.73 nominal) -- rather than hand-derived
biquadratic capacity/EIR-vs-temperature curve coefficients, which could
easily be subtly wrong (e.g. capacity increasing with outdoor temperature
instead of decreasing).
</p>
<p>
Instantiating with <code>datCoi=...Carrier_Weathermaster_50HJ012()</code>
directly as an inline component modifier failed OpenModelica translation
outright: <code>\"Could not evaluate structural parameter (or constant):
coolingCoil.datCoi.nSta ... Array dimensions must be known at compile
time.\"</code>, even though <code>nSta=1</code> is fixed by the record's own
type chain. Confirmed against Buildings' own
<code>Cooling.AirSource.Examples.SpaceCooling</code> (which declares its
<code>datCoi</code> the same way) that the fix is to declare the record as
its own top-level model parameter first, then reference it into the coil
instance -- an OpenModelica frontend limitation with resolving a
structural parameter through a type-alias-record-constructor-call passed
as a component modifier, not a problem with the data. With that fix, the
model translated and exported an FMU successfully.
</p>
<p>
Simulating that FMU (via <code>pyfmi</code>, Model Exchange, matching this
project's own production calling convention) at <code>uFan=0</code>
segfaulted OpenModelica's nonlinear initialization solver -- NaN/Inf
iteration variables, process exit code 139. Traced to the coil's moisture
re-evaporation submodel, <code>Buildings.Fluid.DXSystems.Cooling.
BaseClasses.Evaporation</code>: its off-compressor branch does an iterative
wet-bulb-temperature solve that fails at zero airflow. Two fixes were
attempted and empirically retested against a rebuilt FMU each time:
</p>
<ol>
<li>
<code>computeReevaporation=false</code> (the component's own documented
escape hatch for exactly this submodel) translated to an unbalanced DAE --
628 equations for 629 variables. Traced to the exact line: <code>
Evaporation.mo</code>'s <code>if computeReevaporation then ... else ...
end if</code> equation section never assigns <code>XiSatRefOut</code> in
its <code>false</code> branch, while the variable itself is declared
unconditionally. This is a genuine gap in that Buildings model version, not
something fixable from this file, so it was reverted.
</li>
<li>
<code>allowFlowReversal=false</code> (this RTU's air path is a
fixed-direction <code>MassFlowSource_T</code>-driven topology identical in
shape to AHU.mo's own air side, which never reverses -- and the same fix
already resolved an analogous issue on AHU.mo's cooling coil) translated
cleanly (629/629, balanced) but did <b>not</b> fix the crash -- still
segfaulted, at a different residual function index, confirming the
wet-bulb iterative solve itself (which does not reference flow direction)
was the actual failure, not the stream-connector ambiguity this fix
targets.
</li>
</ol>
<p>
Cold start with the fan off is not a rare edge case for this project -- it
is the first thing every model's own test suite exercises (AHU.mo's
A1/A2, BoilerPlant.mo's B1). A DX coil that cannot survive it is not
usable here regardless of how physically detailed its cooling curves are.
Per this task's own explicit guidance (\"If the Buildings DX components are
too complex or unstable for OpenModelica FMU export, use the simplest
physically defensible alternative and document the tradeoff\"), the real
coil was replaced with the idealized approach below. <code>QCoo_flow_nominal</code>
(this model's own capacity parameter) and <code>COP_nominal</code> were
kept at the Carrier record's own values (-36,846.82 W, COP 3.71) for
continuity with this investigation, even though the coil itself is now
algebraic.
</p>
<h4>DX cooling: idealized coil plus a bounded COP approximation</h4>
<p>
Cooling uses <code>Buildings.Fluid.HeatExchangers.HeaterCooler_u</code> --
the same component this model's own gas heating section uses, and the same
one <code>models/ahu-simple/SimpleAHU.mo</code> already uses successfully
for its own cooling coil (<code>cooCoi</code>) before AHU.mo replaced it
with a real chilled-water coil. <code>yCoo</code> (the continuous 0..1
SAT-PI output) drives <code>coolingCoil.u</code> directly, so -- unlike the
real DX coil's genuinely binary on/off compressor -- capacity here
<i>is</i> continuously modulated, matching AHU.mo's control-loop
expectations exactly.
</p>
<p>
<code>PCompressor</code> is derived using this task's own suggested
fallback formula, <code>PCompressor = QCoolLoad/compressorCOP</code>.
<code>steadyStateCOP</code> is a bounded function of <code>TOut</code>, and
<code>compressorCOP</code> additionally includes a bounded generic part-load
degradation factor. Available sensible cooling capacity is also derated from
<code>QCoo_flow_nominal</code> using outdoor and mixed-air temperatures. These
small algebraic performance maps preserve the FMU stability of the idealized
coil while exposing physically meaningful parameters for sensitivity analysis
and automated calibration.
</p>
<p>
0 when <code>QCoolLoad &lt;= 1 W</code> (compressor effectively off).
Default <code>TOutCOPRef=35 degC</code> (a standard AHRI-style outdoor
rating point), <code>kCOPPerKelvin=0.04 /K</code>, bounds
<code>[COP_min, COP_max] = [2.0, 5.0]</code> matching this task's suggested
range. This is a documented linear approximation, not a manufacturer
curve -- but because <code>PCompressor</code> is computed from it (not the
reverse), it is guaranteed non-negative and bounded by construction, and
being purely algebraic (no fluid states, no iterative solves), it cannot
suffer the class of numerical instability that ruled out the real
component above.
</p>
<h4>Gas heating</h4>
<p>
Uses the same idealized <code>Buildings.Fluid.HeatExchangers.HeaterCooler_u</code>
sensible heater AHU.mo already uses for its own heating section, plus a
single fixed <code>etaHeat</code> (default 0.80) to derive
<code>gasHeatingPower = QHeaLoad/etaHeat</code> -- explicitly acceptable
for v1 per this task's own scope (\"may initially use a sensible air-side
heater plus a gas-efficiency calculation\"), unlike the DX cooling side
where a real Buildings component was available and used. No part-load- or
condensing-flue-temperature-dependent efficiency curve is modeled (that
would mirror <code>BoilerPlant.mo</code>'s own condensing-efficiency
approach, but a packaged gas furnace's efficiency characteristics are a
distinct future exercise, not needed for v1).
</p>
<h4>VAV/CAV hook</h4>
<p>
<code>haveVAVControl</code> (default true) selects between the most-open-
VAV-damper static-pressure reset (identical formula to AHU.mo) and a fixed
<code>dpSupSetFixed</code> setpoint when false -- the same model serves
both a packaged VAV RTU and a packaged CAV RTU without duplication.
</p>
<h4>Outdoor-air volume flow output</h4>
<p>
<code>VOutAir_flow</code> exposes a physically meaningful outdoor-air volume
flow rate, derived as <code>VSup_flow * yOutDam</code> -- the measured
supply-air volume flow (<code>senFlo.V_flow</code>, already exposed as
<code>VSup_flow</code>) scaled by the economizer fraction, rather than a
separate mass-flow/fixed-density conversion of the existing
<code>mOut_flow_cmd = mAir_flow_cmd * yOutDam</code> protected variable.
Both are algebraically consistent (the same <code>yOutDam</code> fraction
applied to the total flow, just on a volumetric basis instead of mass), but
reusing the measured <code>VSup_flow</code> sensor keeps
<code>VOutAir_flow</code> automatically consistent with whatever density
the simulated air actually has at that sensor's location and avoids
introducing a second, independent density assumption that could drift from
it. In normal operation <code>yOutDam</code> follows the economizer command
and is bounded by the minimum-OA floor. During an explicit actuator override,
<code>yOutDam</code> is bounded to <code>[0, 1]</code>, so
<code>0 &lt;= VOutAir_flow &lt;= VSup_flow</code> still holds by construction.
</p>
<h4>Outdoor-air damper command, actual position, and actuator override</h4>
<p>
The economizer now exposes separate command and physical-position signals.
<code>yOutDamCmd</code> is the controller-requested outdoor-air damper position.
<code>yOutDam</code> is the actual physical damper position used by outdoor-air
mass flow, return-air mass flow, mixed-air temperature, and
<code>VOutAir_flow</code>. In normal operation they are identical.
</p>
<p>
For actuator-fault simulation, set <code>uOutDamOvrEna=true</code> and drive
<code>uOutDamOvr</code> to the forced physical position (for example 0 for a
stuck-closed damper). The economizer command continues to calculate normally,
so a realistic command/position mismatch is observable. These inputs are an
explicit actuator-fault interface; <code>yOutDam</code> must not be mapped back
into <code>uOutDamOvr</code>, avoiding the output-to-input self-loop class of
problem. When the override is disabled, <code>yOutDam</code> immediately follows
<code>yOutDamCmd</code> again.
</p>

<h4>Minimum outdoor-air input</h4>
<p>
<code>uMinOutAir</code> replaces the formerly-fixed <code>minOutAirFra</code>
parameter as the economizer's minimum-outdoor-air floor during FMU
operation -- every place the economizer command equations (<code>outFraRaw</code>,
<code>yOutDamCmd</code>) referenced <code>minOutAirFra</code> now references
<code>uMinOutAir</code> instead. Favorable outdoor conditions can raise
<code>yOutDamCmd</code> above the floor, unfavorable conditions hold the command
at the floor, and <code>haveEconomizer=false</code> locks the command at the
floor. The actual <code>yOutDam</code> follows this command unless the explicit
actuator override is enabled -- only the floor's own source changed, from a compiled-in
constant to a live input. <code>minOutAirFra</code> itself is kept as a
parameter (now used only as <code>uMinOutAir</code>'s <code>start</code>
value) so a caller that never explicitly drives <code>uMinOutAir</code>, or
a standalone (non-FMU) simulation of this model, still behaves exactly as
before.
</p>
<h4>BACnet status outputs</h4>
<p>
<code>supplyFanStatus</code>, <code>coolingStatus</code>,
<code>heatingStatus</code>, and <code>economizerStatus</code> are Boolean
outputs derived from already-computed controller/continuous signals above --
threshold checks, not new control logic, and they feed nothing back into
<code>yFan</code>/<code>yCoo</code>/<code>yHea</code>/<code>yOutDam</code>:
</p>
<pre>
supplyFanStatus  = yFan &gt; 0.01
coolingStatus    = coolingPLR &gt; 0.01
heatingStatus    = heatingPLR &gt; 0.01
economizerStatus = yOutDamCmd &gt; uMinOutAir + economizerActiveTol
</pre>
<p>
<code>economizerActiveTol</code> (default 0.02) exists only so
<code>economizerStatus</code> does not chatter right at the minimum-OA floor
from solver/settling noise. Status follows <code>yOutDamCmd</code> (controller
intent), while <code>yOutDam</code> is the actual physical damper position.
These four map directly to this project's existing BACnet BI point
convention (<code>Supply-Fan-Status</code>, <code>Cooling-Status</code>,
<code>Heating-Status</code>, <code>Economizer-Status</code>).
</p>
<h4>Known simplifications / not implemented in v1</h4>
<ul>
<li>
No latent cooling at all -- not internally modeled, not exposed. The
idealized <code>HeaterCooler_u</code> coil is purely a sensible heat-flow
boundary condition on the air stream (<code>Q_flow = u*Q_flow_nominal</code>);
it has no moisture model, no wet-bulb calculation, no dry/wet coil
selector, and therefore no <code>computeReevaporation</code> or
<code>use_mCon_flow</code> parameters to configure -- those belonged only
to the abandoned real DX coil (see 'DX cooling component selection'
above) and do not apply to this design. There is no
<code>QLat_flow</code>/humidity output because there is no latent
computation to surface; a future RTU variant that reintroduces a real DX
coil (once the zero-airflow instability above is fixed upstream in
Buildings, or worked around with a minimum-airflow floor) would be the
place to add it.
</li>
<li>
No multi-stage or variable-speed compressor, no heat-pump reversing cycle,
no auxiliary electric heat, no return/exhaust fan, no energy recovery, no
detailed filter loading or refrigerant circuit, no building-level
thermostat logic. These are documented future RTU variants, not gaps this
version tries to hide.
</li>
<li>
Filter pressure drop is not a separate fluid component -- it is folded
into the overall <code>dpSup_nominal</code> duct static-pressure budget,
the same level of duct-network detail AHU.mo uses.
</li>
<li>
The condenser side of the DX cycle is not modeled at all -- there is no
condenser airflow, no refrigerant loop, and no direct use of
<code>TOut</code> as a condenser-entering temperature in the coil itself.
<code>TOut</code> only enters this model through <code>compressorCOP</code>'s
linear approximation and the economizer/mixing calculation, which is a
simplification relative to the real DX coil's own physically modeled
condenser-side heat rejection.
</li>
</ul>
<p>
Inputs: TSupSet, TOut, TRet, uFan, uVAVDamMax, uMinOutAir, uOutDamOvrEna, uOutDamOvr.
</p>
<p>
Outputs: TSup, TMix, VSup_flow, yFan, yOutDamCmd, yOutDam, VOutAir_flow, yCoo, yHea,
QCoolLoad, QHeaLoad, PCompressor, PFan, dpSup, dpSupSet, coolingPLR,
heatingPLR, totalElectricPower, gasHeatingPower, compressorCOP,
supplyFanStatus, coolingStatus, heatingStatus, economizerStatus.
</p>
</html>"));
end RTU;
