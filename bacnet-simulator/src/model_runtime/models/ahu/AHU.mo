within ;
model AHU
  "Single-duct AHU with closed-loop SAT control, physically coupled CHW cooling coil, and energy-ready outputs"

  // ===========================================================================
  // EXPERIMENTAL MODEL -- does not replace SimpleAHU.mo.
  //
  // Behavioral reference: SimpleAHU.mo. Same control sequence (economizer,
  // closed-loop SAT PI, fan static-pressure PI with most-open-VAV-damper
  // reset, idealized sensible heating), FMU input/output names preserved
  // where practical.
  //
  // The one substantive physics change is the cooling coil: SimpleAHU uses
  // Buildings.Fluid.HeatExchangers.HeaterCooler_u (single air-side stream,
  // heat added/removed algebraically) plus a separate hand-written formula
  // for TChiWatRet/VChiWat_flow that is not coupled to the coil at all. This
  // model replaces that with a real two-fluid air/water coil
  // (Buildings.Fluid.HeatExchangers.DryCoilEffectivenessNTU) fed from a
  // chilled-water source through a modulating two-way valve, so
  // QCoolLoad/TChiWatRet/VChiWat_flow are genuine outputs of the coupled
  // air-water heat balance rather than a post-hoc estimate.
  // ===========================================================================

  replaceable package MediumA = Buildings.Media.Air
    constrainedby Modelica.Media.Interfaces.PartialMedium
    "Air medium";

  replaceable package MediumW = Buildings.Media.Water
    constrainedby Modelica.Media.Interfaces.PartialMedium
    "Water medium (chilled-water side of the cooling coil)";

  // ---------------------------------------------------------------------------
  // Air-side design parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal = 2.0
    "Nominal supply air mass flow rate";

  parameter Modelica.Units.SI.Volume VMix = 2.0
    "Mixed-air plenum volume";

  parameter Modelica.Units.SI.HeatFlowRate QHea_flow_nominal = 30000
    "Nominal sensible heating capacity";

  parameter Real minOutAirFra(min=0, max=1) = 0.15
    "Minimum outdoor-air fraction";

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

  // ---------------------------------------------------------------------------
  // Chilled-water cooling coil parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.HeatFlowRate QCoo_flow_nominal = -40000
    "Nominal sensible cooling capacity used to size the coil (negative)";

  parameter Modelica.Units.SI.Temperature TChiWatSup_nominal = 280.15
    "Design chilled-water supply (coil water inlet) temperature (7 degC)";

  parameter Modelica.Units.SI.Temperature TMixCoo_nominal = 300.15
    "Design mixed/entering air temperature at the cooling coil (27 degC)";

  parameter Modelica.Units.SI.Temperature TChiWatSup_disable = 291.15
    "Chilled-water temperature above which cooCapacityFactor reports zero (18 degC)";

  parameter Modelica.Units.SI.VolumeFlowRate VChiWat_flow_nominal = 0.002
    "Nominal chilled-water flow through the cooling coil at full valve (2 L/s)";

  parameter Modelica.Units.SI.Density rhoWat = 998
    "Water density, used to size the coil/valve from VChiWat_flow_nominal";

  parameter Modelica.Units.SI.PressureDifference dpChiWatCoi_nominal = 3000
    "Chilled-water-side pressure drop across the cooling coil at nominal flow";

  parameter Modelica.Units.SI.PressureDifference dpChiWatValve_nominal = 6000
    "Pressure drop across the cooling-coil control valve, full open, at nominal flow";

  // ---------------------------------------------------------------------------
  // Fan-energy parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.Power PSupFan_nominal = 5000
    "Nominal supply-fan electrical power at full command";

  parameter Modelica.Units.SI.PressureDifference dpSup_nominal = 500
    "Supply-duct static pressure at full fan speed";

  parameter Modelica.Units.SI.PressureDifference dpSupSetMin = 200
    "Minimum supply-duct static-pressure setpoint";

  parameter Modelica.Units.SI.PressureDifference dpSupSetMax = 500
    "Maximum supply-duct static-pressure setpoint";

  parameter Real vavDamLow(min=0, max=1) = 0.60
    "Most-open VAV damper position below which static-pressure setpoint is minimum";

  parameter Real vavDamHigh(min=0, max=1) = 0.90
    "Most-open VAV damper position above which static-pressure setpoint is maximum";

  // ---------------------------------------------------------------------------
  // Configuration hooks (first pass -- see Documentation for the roadmap)
  // ---------------------------------------------------------------------------
  parameter Boolean haveEconomizer = true
    "Set to false to disable the outdoor-air economizer reset and lock yOutDam at minOutAirFra";

  parameter Boolean haveReturnFan = false
    "NOT YET IMPLEMENTED -- reserved for a future return/relief-fan branch.
    Changing this value has no effect anywhere in this model.";

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
    "Outdoor-air temperature";

  Modelica.Blocks.Interfaces.RealInput TRet(
    final unit="K",
    displayUnit="degC")
    "Return-air temperature";

  Modelica.Blocks.Interfaces.RealInput uFan(
    final unit="1")
    "Supply fan enable/maximum command, 0..1";

  Modelica.Blocks.Interfaces.RealInput uVAVDamMax(
    final unit="1")
    "Most-open downstream VAV damper position, 0..1";

  Modelica.Blocks.Interfaces.RealInput TChiWatSup(
    final unit="K",
    displayUnit="degC")
    "Chilled-water supply temperature from upstream plant";

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

  Modelica.Blocks.Interfaces.RealOutput yOutDam(
    final unit="1")
    "Outdoor-air/economizer fraction";

  Modelica.Blocks.Interfaces.RealOutput yHeaVal(
    final unit="1")
    "Heating-coil command";

  Modelica.Blocks.Interfaces.RealOutput yCooVal(
    final unit="1")
    "Cooling-coil valve command requested by SAT controller";

  Modelica.Blocks.Interfaces.RealOutput cooCapacityFactor(
    final unit="1")
    "Diagnostic estimate of available cooling capacity fraction based on chilled-water temperature.
    Informational only in this model -- unlike SimpleAHU, it does not gate the coil, because the
    real coil already derates naturally as TChiWatSup approaches the entering air temperature.";

  Modelica.Blocks.Interfaces.RealOutput QCoolLoad(
    final unit="W")
    "AHU cooling thermal load, from the coil's actual water-side heat absorption";

  Modelica.Blocks.Interfaces.RealOutput QHeaLoad(
    final unit="W")
    "Calculated AHU heating thermal load";

  Modelica.Blocks.Interfaces.RealOutput TChiWatRet(
    final unit="K",
    displayUnit="degC")
    "Chilled-water return temperature, from the coil's actual water-side outlet";

  Modelica.Blocks.Interfaces.RealOutput VChiWat_flow(
    final unit="m3/s")
    "Chilled-water flow through the cooling coil, from the actual water-side flow";

  Modelica.Blocks.Interfaces.RealOutput PSupFan(
    final unit="W")
    "Calculated supply-fan electrical power";

  Modelica.Blocks.Interfaces.RealOutput dpSup(
    final unit="Pa")
    "Calculated supply-duct static pressure";

  Modelica.Blocks.Interfaces.RealOutput dpSupSet(
    final unit="Pa")
    "Reset supply-duct static-pressure setpoint";

protected
  Real mAir_flow_cmd(unit="kg/s");
  Real mOut_flow_cmd(unit="kg/s");
  Real mRet_flow_cmd(unit="kg/s");
  Real outFraRaw;
  Real vavDamResetFra(unit="1")
    "Normalized VAV-demand signal used for static-pressure reset";

  final parameter Modelica.Units.SI.MassFlowRate mChiWat_flow_nominal =
    rhoWat * VChiWat_flow_nominal
    "Nominal chilled-water mass flow rate, used to size the coil and valve";

  // ---------------------------------------------------------------------------
  // Mixing / economizer section
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
    "Supply duct/building pressure boundary";

  // ---------------------------------------------------------------------------
  // Heating section (unchanged from SimpleAHU: idealized sensible coil)
  // ---------------------------------------------------------------------------
  Buildings.Fluid.HeatExchangers.HeaterCooler_u heaCoi(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    Q_flow_nominal=QHea_flow_nominal,
    dp_nominal=50,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial)
    "Idealized sensible heating coil";

  // ---------------------------------------------------------------------------
  // Cooling section -- real two-fluid chilled-water coil
  //
  // Topology: idealized CHW supply boundary (TChiWatSup FMU input) -> modulating
  // two-way valve (yCooVal command) -> DryCoilEffectivenessNTU (air/water,
  // sensible-only, matching the current sensible-only cooling behavior) ->
  // idealized CHW return boundary. This mirrors the same "idealized boundary
  // source/sink around a real coil" pattern already used for the hot-water
  // reheat loop in models/vav/SimpleVAVZone.mo, rather than modeling a full
  // pumped CHW loop -- there is no chiller/plant model in this project, so an
  // infinite-capacity boundary is the appropriate level of detail here.
  //
  // DryCoilEffectivenessNTU (not WetCoilEffectivenessNTU) is used deliberately:
  // it is what Buildings.Examples.VAVReheat.BaseClasses.PartialHVAC uses for
  // its hot-water heating coil, and WetCoilEffectivenessNTU's own
  // documentation recommends DryCoilEffectivenessNTU instead "for heating
  // coil only or for cooling coil with no water condensation" -- exactly this
  // case, since the existing model has no moisture/latent modeling at all.
  // DryCoilEffectivenessNTU is also purely algebraic (effectiveness-NTU, no
  // internal energyDynamics/state), which avoids adding solver states beyond
  // what SimpleAHU already has.
  // ---------------------------------------------------------------------------
  Buildings.Fluid.Sources.Boundary_pT chiWatSup(
    redeclare package Medium = MediumW,
    use_T_in=true,
    p=101325 + dpChiWatValve_nominal + dpChiWatCoi_nominal,
    nPorts=1)
    "Idealized chilled-water supply boundary (infinite upstream plant capacity)";

  Buildings.Fluid.Sources.Boundary_pT chiWatRet(
    redeclare package Medium = MediumW,
    p=101325,
    nPorts=1)
    "Idealized chilled-water return boundary";

  Buildings.Fluid.Actuators.Valves.TwoWayEqualPercentage valCooCoi(
    redeclare package Medium = MediumW,
    m_flow_nominal=mChiWat_flow_nominal,
    dpValve_nominal=dpChiWatValve_nominal,
    dpFixed_nominal=0,
    allowFlowReversal=false,
    y_start=0)
    "Chilled-water modulating control valve for the cooling coil.
    y_start=0 (closed) overrides the component's own default of 1 (fully
    open) -- with the default, the valve's filtered actuator dynamics
    (strokeTime=120s) would start near-fully-open and slew closed over the
    first two minutes regardless of the commanded yCooVal, dumping design-
    capacity cooling into the air stream before the SAT loop has caught up.
    That transient was verified to crash the mixing volume's energy balance
    (air temperature driven below the medium's valid range) in an actual
    fmpy simulation of the exported FMU -- not merely a translation warning.";

  Buildings.Fluid.HeatExchangers.DryCoilEffectivenessNTU cooCoi(
    redeclare package Medium1 = MediumW,
    redeclare package Medium2 = MediumA,
    configuration=Buildings.Fluid.Types.HeatExchangerConfiguration.CounterFlow,
    m1_flow_nominal=mChiWat_flow_nominal,
    m2_flow_nominal=mAir_flow_nominal,
    dp1_nominal=dpChiWatCoi_nominal,
    dp2_nominal=50,
    Q_flow_nominal=QCoo_flow_nominal,
    T_a1_nominal=TChiWatSup_nominal,
    T_a2_nominal=TMixCoo_nominal,
    allowFlowReversal1=false,
    allowFlowReversal2=false,
    show_T=true)
    "Chilled-water cooling coil: real air/water sensible heat exchanger";

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

  Buildings.Fluid.Sensors.VolumeFlowRate senVChiWat(
    redeclare package Medium = MediumW,
    m_flow_nominal=mChiWat_flow_nominal,
    tau=10)
    "Chilled-water volume flow sensor";

  Buildings.Fluid.Sensors.TemperatureTwoPort senTChiWatRet(
    redeclare package Medium = MediumW,
    m_flow_nominal=mChiWat_flow_nominal,
    tau=10)
    "Chilled-water return (coil outlet) temperature sensor";

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
  // ---------------------------------------------------------------------------
  // Static-pressure reset based on the most-open downstream VAV damper.
  // <= 60% open -> minimum pressure setpoint.
  // >= 90% open -> maximum pressure setpoint.
  // Between the thresholds -> linear reset.
  vavDamResetFra =
    max(0, min(1,
      (uVAVDamMax - vavDamLow) /
      max(0.01, vavDamHigh - vavDamLow)));

  dpSupSet =
    dpSupSetMin +
    (dpSupSetMax - dpSupSetMin) * vavDamResetFra;

  fanCon.u_s = dpSupSet;
  fanCon.u_m = dpSup;

  // uFan acts as an enable/maximum-speed limit. Actual fan speed is determined
  // by the static-pressure PI controller.
  yFan =
    if uFan > 0.01 then
      min(max(0, min(1, uFan)), fanCon.y)
    else
      0;

  // Simplified duct model: pressure follows the fan affinity law.
  // At 100% fan speed, dpSup equals dpSup_nominal.
  dpSup = dpSup_nominal * yFan^2;

  mAir_flow_cmd = mAir_flow_nominal * yFan;

  // Simple fan-law approximation: power varies approximately with speed^3.
  PSupFan = PSupFan_nominal * yFan^3;

  // ---------------------------------------------------------------------------
  // Outdoor-air/economizer control
  // ---------------------------------------------------------------------------
  outFraRaw =
    if TOut < TRet and abs(TRet - TOut) > 0.01 then
      (TRet - TSupSet) / (TRet - TOut)
    else
      minOutAirFra;

  yOutDam =
    if not haveEconomizer then
      minOutAirFra
    elseif TOut < TRet then
      max(minOutAirFra, min(1, outFraRaw))
    else
      minOutAirFra;

  mOut_flow_cmd = mAir_flow_cmd * yOutDam;
  mRet_flow_cmd = mAir_flow_cmd - mOut_flow_cmd;

  outAir.m_flow_in = mOut_flow_cmd;
  retAir.m_flow_in = mRet_flow_cmd;
  outAir.T_in = TOut;
  retAir.T_in = TRet;

  // ---------------------------------------------------------------------------
  // Closed-loop SAT control
  // ---------------------------------------------------------------------------
  satCon.u_s = TSupSet;
  satCon.u_m = senTSup.T;

  yHeaVal =
    if yFan > 0.01 then
      max(0, satCon.y)
    else
      0;

  yCooVal =
    if yFan > 0.01 then
      max(0, -satCon.y)
    else
      0;

  heaCoi.u = yHeaVal;

  // Cooling-coil valve is driven directly by yCooVal. Unlike SimpleAHU, this
  // does not also multiply in cooCapacityFactor: the real coil already loses
  // capacity on its own as TChiWatSup rises toward the entering air
  // temperature (QMax_flow = CMin_flow * (T_air_in - T_water_in) shrinks),
  // so applying the diagnostic factor again here would derate twice.
  valCooCoi.y = yCooVal;
  chiWatSup.T_in = TChiWatSup;

  // ---------------------------------------------------------------------------
  // Chilled-water-dependent available cooling capacity (diagnostic only)
  // ---------------------------------------------------------------------------
  cooCapacityFactor =
    max(0, min(1,
      (TChiWatSup_disable - TChiWatSup) /
      max(0.1, TChiWatSup_disable - TChiWatSup_nominal)));

  // ---------------------------------------------------------------------------
  // Simplified thermal loads
  // ---------------------------------------------------------------------------
  QHeaLoad = QHea_flow_nominal * yHeaVal;

  // Real coil output: positive when the water stream absorbs heat (cooling
  // is occurring), consistent with the sign convention QCoolLoad already
  // used in SimpleAHU.
  QCoolLoad = cooCoi.Q1_flow;

  // ---------------------------------------------------------------------------
  // Chilled-water outputs -- read directly from the coil's actual water-side
  // flow and outlet temperature rather than computed from an energy balance.
  // ---------------------------------------------------------------------------
  VChiWat_flow = senVChiWat.V_flow;
  TChiWatRet = senTChiWatRet.T;

  // ---------------------------------------------------------------------------
  // Air path
  // ---------------------------------------------------------------------------
  connect(outAir.ports[1], mix.ports[1]);
  connect(retAir.ports[1], mix.ports[2]);
  connect(mix.ports[3], senTMix.port_a);
  connect(senTMix.port_b, heaCoi.port_a);
  connect(heaCoi.port_b, cooCoi.port_a2);
  connect(cooCoi.port_b2, senTSup.port_a);
  connect(senTSup.port_b, senFlo.port_a);
  connect(senFlo.port_b, sinAir.ports[1]);

  // ---------------------------------------------------------------------------
  // Chilled-water path
  // ---------------------------------------------------------------------------
  connect(chiWatSup.ports[1], valCooCoi.port_a);
  connect(valCooCoi.port_b, cooCoi.port_a1);
  connect(cooCoi.port_b1, senVChiWat.port_a);
  connect(senVChiWat.port_b, senTChiWatRet.port_a);
  connect(senTChiWatRet.port_b, chiWatRet.ports[1]);

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
Experimental AHU model. Does not replace or modify SimpleAHU.mo; the two
compile and export independently.
</p>
<p>
Reproduces SimpleAHU's control sequence: closed-loop PI supply-air-temperature
control, a separate PI supply-fan controller modulating fan speed to maintain
duct static pressure with most-open-VAV-damper reset, and an economizer with a
minimum-outdoor-air floor. The uFan input remains an enable/maximum-speed limit
for the fan controller.
</p>
<p>
The substantive change from SimpleAHU is the cooling coil. SimpleAHU applies
sensible cooling with an idealized single-medium
Buildings.Fluid.HeatExchangers.HeaterCooler_u, then estimates chilled-water
return temperature and flow with a separate algebraic energy-balance formula
that has no feedback into the coil itself. This model instead routes an
idealized chilled-water supply boundary (fed by the TChiWatSup FMU input)
through a modulating two-way valve (Buildings.Fluid.Actuators.Valves.
TwoWayEqualPercentage, driven by yCooVal) into a real two-fluid air/water
coil (Buildings.Fluid.HeatExchangers.DryCoilEffectivenessNTU), and back to an
idealized chilled-water return boundary. QCoolLoad, TChiWatRet, and
VChiWat_flow are now read directly from the coil's own water-side heat
transfer, outlet temperature, and flow sensor, rather than computed
independently of it.
</p>
<p>
DryCoilEffectivenessNTU (sensible-only, no moisture/condensation modeling) was
chosen over WetCoilEffectivenessNTU because the existing model has no latent
component at all, and WetCoilEffectivenessNTU's own documentation recommends
DryCoilEffectivenessNTU for exactly this case (cooling coil with no water
condensation) since it computes faster and avoids the wet/dry regime switching
logic. DryCoilEffectivenessNTU is also the component
Buildings.Examples.VAVReheat.BaseClasses.PartialHVAC (the library's own
reference AHU) uses for its heating coil, and it is purely algebraic
(effectiveness-NTU, no internal energyDynamics/state), so it adds no solver
states beyond what SimpleAHU already has.
</p>
<p>
The chilled-water supply/return boundary pattern (idealized Boundary_pT
source and sink around a real coil, with a control valve modulating flow)
mirrors the existing hot-water reheat loop in
models/vav/SimpleVAVZone.mo -- there is no chiller/plant model in this
project, so an infinite-capacity boundary is the appropriate level of detail
for a first version, per the same simplification already accepted elsewhere
in this codebase.
</p>
<p>
cooCapacityFactor is preserved as an output for FMU/mapping compatibility but
is diagnostic only in this model: it is not multiplied into the valve command,
because the real coil already derates on its own as TChiWatSup approaches the
entering air temperature (QMax_flow shrinks). Applying the old factor on top
would derate twice. If a hard low-side interlock is wanted later, that is a
controls addition, not a physics one.
</p>
<p>
Heating is unchanged from SimpleAHU (idealized sensible HeaterCooler_u) --
hot-water/electric heating variants are left for a future version, per the
task scope.
</p>
<p>
Known simplifications carried over or introduced in this first version:
</p>
<ul>
<li>
Outdoor and return air remain independent MassFlowSource_T streams mixed in a
single plenum, exactly as in SimpleAHU -- no return/relief-air pressure
network. haveReturnFan is declared as a configuration hook for a future
version but has no effect yet.
</li>
<li>
The chilled-water side has no pump or plant model; TChiWatSup is an idealized
temperature boundary as described above.
</li>
<li>
Supply-fan electrical power remains the cubic fan-law approximation
(PSupFan_nominal * yFan^3), not derived from a Buildings fan/mover model.
</li>
</ul>
<p>
Inputs: TSupSet, TOut, TRet, uFan, uVAVDamMax, TChiWatSup.
</p>
<p>
Outputs: TSup, TMix, VSup_flow, yFan, yOutDam, yHeaVal, yCooVal,
cooCapacityFactor, QCoolLoad, QHeaLoad, TChiWatRet, VChiWat_flow, PSupFan,
dpSup, dpSupSet.
</p>
</html>"));
end AHU;
