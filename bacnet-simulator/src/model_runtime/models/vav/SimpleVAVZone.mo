within ;
model SimpleVAVZone
  "Standalone VAV terminal controller and reheat box for FMU export"

  replaceable package MediumA = Buildings.Media.Air
    constrainedby Modelica.Media.Interfaces.PartialMedium
    "Air medium";

  replaceable package MediumW = Buildings.Media.Water
    constrainedby Modelica.Media.Interfaces.PartialMedium
    "Water medium";

  parameter Modelica.Units.SI.Volume VRoo = 150
    "Zone volume used by the Buildings VAV terminal model";
  parameter Modelica.Units.SI.MassFlowRate mCooAir_flow_nominal = 0.6
    "Nominal cooling air mass flow rate";
  parameter Modelica.Units.SI.MassFlowRate mHeaAir_flow_nominal = 0.3
    "Nominal heating air mass flow rate";
  parameter Modelica.Units.SI.PressureDifference dpAir = 200
    "Default/reference upstream supply duct static pressure -- used only as
    the dpSup input's own start value (for standalone operation with no
    external driver). Once dpSup is actually supplied (e.g. from
    RTU.dpSup/AHU.dpSup), it fully determines the upstream boundary
    pressure; this parameter no longer appears in any equation.";
  parameter Modelica.Units.SI.PressureDifference dpHeaWat = 6000
    "Pressure difference across hot-water circuit";
  parameter Modelica.Units.SI.Temperature THeaWatSup = 328.15
    "Hot-water supply temperature (55 degC)";

  // ---------------------------------------------------------------------------
  // FMU inputs
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.RealInput TRoo(
    final unit="K",
    displayUnit="degC")
    "Zone temperature supplied by the external ThermalZone model";

  Modelica.Blocks.Interfaces.RealInput TRooHeaSet(
    final unit="K",
    displayUnit="degC")
    "Room heating setpoint";

  Modelica.Blocks.Interfaces.RealInput TRooCooSet(
    final unit="K",
    displayUnit="degC")
    "Room cooling setpoint";

  Modelica.Blocks.Interfaces.RealInput TSupAHU(
    final unit="K",
    displayUnit="degC")
    "AHU supply air temperature";

  Modelica.Blocks.Interfaces.RealInput dpSup(
    final unit="Pa",
    start=dpAir)
    "Available upstream supply duct static pressure (e.g. from
    RTU.dpSup/AHU.dpSup). Determines airflow through the VAV damper together
    with the damper's own commanded position -- see supAir/vav below. Not
    tied to any specific upstream model; RTU and AHU both expose a
    compatible dpSup output.";

  // ---------------------------------------------------------------------------
  // FMU outputs
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.RealOutput VSup_flow(
    final unit="m3/s")
    "Supply air volumetric flow rate";

  Modelica.Blocks.Interfaces.RealOutput TSup(
    final unit="K",
    displayUnit="degC")
    "Discharge/supply air temperature after reheat";

  Modelica.Blocks.Interfaces.RealOutput yDam(
    final unit="1")
    "VAV damper command";

  Modelica.Blocks.Interfaces.RealOutput yDam_actual(
    final unit="1")
    "Actual VAV damper position";

  Modelica.Blocks.Interfaces.RealOutput yVal(
    final unit="1")
    "Reheat valve command";

  Modelica.Blocks.Interfaces.RealOutput yVal_actual(
    final unit="1")
    "Actual reheat valve position";

  Buildings.Examples.VAVReheat.BaseClasses.Controls.RoomVAV roomVAV(
    have_preIndDam=false,
    V_flow_nominal=mCooAir_flow_nominal/1.2)
    "Room VAV controller";

  Buildings.Examples.VAVReheat.BaseClasses.VAVReheatBox vavBox(
    redeclare package MediumA = MediumA,
    redeclare package MediumW = MediumW,
    VRoo=VRoo,
    mCooAir_flow_nominal=mCooAir_flow_nominal,
    mHeaAir_flow_nominal=mHeaAir_flow_nominal)
    "VAV terminal with hot-water reheat";

  Buildings.Fluid.Sources.Boundary_pT supAir(
    redeclare package Medium = MediumA,
    use_T_in=true,
    use_p_in=true,
    nPorts=1)
    "Upstream supply-air pressure boundary -- pressure is driven externally
    via dpSup rather than a fixed offset, so RTU/AHU fan static-pressure
    output has a real, physical effect on this terminal's airflow";

  Buildings.Fluid.Sources.Boundary_pT retAir(
    redeclare package Medium = MediumA,
    p=101325,
    nPorts=1)
    "Idealized downstream/return-air pressure boundary";

  Buildings.Fluid.Sources.Boundary_pT heaWatSup(
    redeclare package Medium = MediumW,
    use_T_in=true,
    p=101325 + dpHeaWat,
    nPorts=1)
    "Hot-water supply boundary";

  Buildings.Fluid.Sources.Boundary_pT heaWatRet(
    redeclare package Medium = MediumW,
    p=101325,
    nPorts=1)
    "Hot-water return boundary";

equation
  connect(supAir.ports[1], vavBox.port_aAir);
  connect(vavBox.port_bAir, retAir.ports[1]);

  connect(heaWatSup.ports[1], vavBox.port_aHeaWat);
  connect(vavBox.port_bHeaWat, heaWatRet.ports[1]);

  connect(TRooHeaSet, roomVAV.TRooHeaSet);
  connect(TRooCooSet, roomVAV.TRooCooSet);

  roomVAV.TRoo = TRoo;
  roomVAV.VDis_flow = vavBox.VSup_flow;

  connect(roomVAV.yDam, vavBox.yVAV);
  connect(roomVAV.yVal, vavBox.yHea);

  supAir.T_in = TSupAHU;
  supAir.p_in = 101325 + max(0, dpSup);
  heaWatSup.T_in = THeaWatSup;

  VSup_flow = vavBox.VSup_flow;
  TSup = vavBox.TSup;
  yDam = roomVAV.yDam;
  yDam_actual = vavBox.y_actual;
  yVal = roomVAV.yVal;
  yVal_actual = vavBox.yVal_actual;

  annotation (
    experiment(
      StartTime=0,
      StopTime=86400,
      Interval=60,
      Tolerance=1e-6),
    Documentation(info="<html>
<p>Standalone VAV terminal and controller for FMU export.</p>
<p>The thermal-zone physics have been removed from this model. Zone temperature is supplied externally through <code>TRoo</code>, typically from a separate <code>ThermalZone</code> FMU.</p>
<p>The upstream AHU/RTU supply-air temperature enters through <code>TSupAHU</code>. The model calculates airflow, discharge temperature, damper position, and reheat-valve position.</p>
<h4>Upstream duct static pressure (dpSup)</h4>
<p>
<code>supAir</code>, the upstream air boundary condition, is driven by the
<code>dpSup</code> input (<code>use_p_in=true</code> on
<code>Buildings.Fluid.Sources.Boundary_pT</code>, the same conditional-input
pattern already used for <code>TSupAHU</code> via <code>use_T_in</code>):
<code>supAir.p_in = 101325 + max(0, dpSup)</code>. Previously this boundary
used a fixed parameter offset (<code>p=101325+dpAir</code>), which meant an
upstream AHU/RTU's own calculated <code>dpSup</code>/fan response had no
physical effect on this terminal's actual delivered airflow -- only the
terminal's own commanded damper position mattered. With <code>dpSup</code>
now externally driven, airflow through the VAV box's damper
(<code>Buildings.Fluid.Actuators.Dampers.Exponential</code>, in
<code>VAVReheatBox</code>) genuinely depends on both the commanded damper
position AND the available upstream-to-downstream pressure difference, the
same way a real VAV terminal's box behaves. <code>dpAir</code> (default 200
Pa) remains as a parameter, but only as <code>dpSup</code>'s own FMU
<code>start</code> value for standalone use with no external driver -- it no
longer appears in any equation once a real <code>dpSup</code> value is
supplied. <code>max(0, dpSup)</code> guards against a negative upstream
pressure differential, which is not physically meaningful for this
boundary and is not asserted against by <code>Boundary_pT</code>'s own
air-medium sanity range (50,000-150,000 Pa absolute) without it.
</p>
<p>Inputs: TRoo, TRooHeaSet, TRooCooSet, TSupAHU, dpSup.</p>
<p>Outputs: VSup_flow, TSup, yDam, yDam_actual, yVal, yVal_actual.</p>
</html>"));
end SimpleVAVZone;
