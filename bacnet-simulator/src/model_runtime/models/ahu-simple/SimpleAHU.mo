within ;
model SimpleAHU
  "Single-duct AHU with closed-loop SAT control, CHW plant coupling, and energy-ready outputs"

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

  parameter Modelica.Units.SI.HeatFlowRate QHea_flow_nominal = 30000
    "Nominal sensible heating capacity";

  parameter Modelica.Units.SI.HeatFlowRate QCoo_flow_nominal = -40000
    "Nominal sensible cooling capacity (negative)";

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
  // Simplified chilled-water-side parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.Temperature TChiWatSup_nominal = 280.15
    "Nominal chilled-water supply temperature (7 degC)";

  parameter Modelica.Units.SI.Temperature TChiWatSup_disable = 291.15
    "Chilled-water temperature where cooling capacity falls to zero (18 degC)";

  parameter Modelica.Units.SI.VolumeFlowRate VChiWat_flow_nominal = 0.002
    "Nominal chilled-water flow through AHU cooling coil (2 L/s)";

  parameter Modelica.Units.SI.Density rhoWat = 998
    "Water density";

  parameter Modelica.Units.SI.SpecificHeatCapacity cpWat = 4180
    "Water specific heat capacity";

  parameter Modelica.Units.SI.MassFlowRate mChiWat_flow_min = 1e-4
    "Minimum CHW mass flow used to evaluate CHWR; below this, CHWR equals CHWS";

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
    "Cooling-coil command requested by SAT controller";

  Modelica.Blocks.Interfaces.RealOutput cooCapacityFactor(
    final unit="1")
    "Available cooling-capacity fraction based on chilled-water temperature";

  Modelica.Blocks.Interfaces.RealOutput QCoolLoad(
    final unit="W")
    "Calculated AHU cooling thermal load";

  Modelica.Blocks.Interfaces.RealOutput QHeaLoad(
    final unit="W")
    "Calculated AHU heating thermal load";

  Modelica.Blocks.Interfaces.RealOutput TChiWatRet(
    final unit="K",
    displayUnit="degC")
    "Calculated chilled-water return temperature";

  Modelica.Blocks.Interfaces.RealOutput VChiWat_flow(
    final unit="m3/s")
    "Calculated chilled-water flow through AHU cooling coil";

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
  Real yCooEffective(unit="1");
  Real vavDamResetFra(unit="1")
    "Normalized VAV-demand signal used for static-pressure reset";

  Modelica.Units.SI.MassFlowRate mChiWat_flow;

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

  Buildings.Fluid.Sensors.TemperatureTwoPort senTMix(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    tau=10)
    "Mixed-air temperature sensor";

  Buildings.Fluid.HeatExchangers.HeaterCooler_u heaCoi(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    Q_flow_nominal=QHea_flow_nominal,
    dp_nominal=50,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial)
    "Idealized sensible heating coil";

  Buildings.Fluid.HeatExchangers.HeaterCooler_u cooCoi(
    redeclare package Medium = MediumA,
    m_flow_nominal=mAir_flow_nominal,
    Q_flow_nominal=QCoo_flow_nominal,
    dp_nominal=50,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial)
    "Idealized sensible cooling coil with plant-dependent available capacity";

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

  Buildings.Fluid.Sources.Boundary_pT sinAir(
    redeclare package Medium = MediumA,
    p=101325,
    nPorts=1)
    "Supply duct/building pressure boundary";

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
    if TOut < TRet then
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

  // ---------------------------------------------------------------------------
  // Chilled-water-dependent available cooling capacity
  // ---------------------------------------------------------------------------
  cooCapacityFactor =
    max(0, min(1,
      (TChiWatSup_disable - TChiWatSup) /
      max(0.1, TChiWatSup_disable - TChiWatSup_nominal)));

  yCooEffective = yCooVal * cooCapacityFactor;

  heaCoi.u = yHeaVal;
  cooCoi.u = yCooEffective;

  // ---------------------------------------------------------------------------
  // Simplified thermal loads
  // ---------------------------------------------------------------------------
  QCoolLoad = -QCoo_flow_nominal * yCooEffective;
  QHeaLoad = QHea_flow_nominal * yHeaVal;

  // ---------------------------------------------------------------------------
  // Simplified chilled-water feedback with exact normal-operation energy balance
  //
  // CHW flow follows the cooling-valve request. The CHWR calculation uses the
  // actual calculated CHW mass flow whenever flow is meaningfully nonzero, so
  // QCoolLoad = m*cp*(TChiWatRet - TChiWatSup) is exact during normal cooling.
  //
  // noEvent() prevents the near-zero flow threshold from creating solver
  // zero-crossing events. When flow is effectively zero, CHWR is defined equal
  // to CHWS.
  // ---------------------------------------------------------------------------
  VChiWat_flow = VChiWat_flow_nominal * yCooVal;
  mChiWat_flow = rhoWat * VChiWat_flow;

  TChiWatRet =
    noEvent(
      if mChiWat_flow > mChiWat_flow_min then
        TChiWatSup + QCoolLoad / (mChiWat_flow * cpWat)
      else
        TChiWatSup);

  // ---------------------------------------------------------------------------
  // Air path
  // ---------------------------------------------------------------------------
  connect(outAir.ports[1], mix.ports[1]);
  connect(retAir.ports[1], mix.ports[2]);
  connect(mix.ports[3], senTMix.port_a);
  connect(senTMix.port_b, heaCoi.port_a);
  connect(heaCoi.port_b, cooCoi.port_a);
  connect(cooCoi.port_b, senTSup.port_a);
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
Compact AHU model intended for OpenModelica/FMU use.
</p>
<p>
The AHU uses closed-loop PI supply-air-temperature control and a separate
PI supply-fan controller that modulates fan speed to maintain duct static
pressure. The static-pressure setpoint is automatically reset from the most-open
downstream VAV damper position: low VAV demand lowers pressure, while high VAV
demand raises it. The calculated pressure and reset setpoint are exposed as dpSup
and dpSupSet for FMU/BACnet mapping. The uFan input acts as an enable/maximum-speed
limit for the fan controller. Cooling availability is coupled to upstream chilled-water supply
temperature.
</p>
<p>
The model exposes simplified chilled-water feedback through cooling load,
chilled-water flow, and chilled-water return temperature. CHW flow follows
the cooling-valve request. During normal cooling, CHWR is calculated from the
actual CHW mass flow so the water-side energy balance is exact. A noEvent
near-zero-flow guard avoids solver event chatter when the valve is closed.
</p>
<p>
The model also exposes heating thermal load and supply-fan electrical power
for future energy reporting.
</p>
<p>
Supply-fan power uses a simple cubic fan-law approximation. Cooling and heating
loads are based on the idealized HeaterCooler_u nominal capacities and current
commands.
</p>
<p>
This remains a first-stage equipment model: the cooling coil is still an
air-side idealized component rather than a true two-fluid hydronic coil.
</p>
<p>
Inputs: TSupSet, TOut, TRet, uFan, uVAVDamMax, TChiWatSup.
</p>
<p>
Outputs: TSup, TMix, VSup_flow, yFan, yOutDam, yHeaVal, yCooVal,
cooCapacityFactor, QCoolLoad, QHeaLoad, TChiWatRet, VChiWat_flow, PSupFan, dpSup, dpSupSet.
</p>
</html>"));
end SimpleAHU;
