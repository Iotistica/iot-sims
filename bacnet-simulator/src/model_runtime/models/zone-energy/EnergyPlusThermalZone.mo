within ;

model EnergyPlusThermalZone
  "EnergyPlus/Spawn-backed thermal zone for RTU/VAV system integration"

  replaceable package MediumA = Buildings.Media.Air
    constrainedby Modelica.Media.Interfaces.PartialMedium;

  parameter String idfName = "SmallOffice.idf";
  parameter String epwName =
    "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw";
  parameter String weaName =
    "USA_CA_San.Francisco.Intl.AP.724940_TMY3.mos";
  parameter String zoneName = "Core_ZN"
    "Must match a real Zone object name in idfName. The bundled small-office "
    + "reference building defines Attic, Core_ZN, Perimeter_ZN_1/2/3/4.";

  parameter Modelica.Units.SI.Temperature TRoo_start = 295.15;
  parameter Modelica.Units.SI.Density rhoAir = 1.2;
  parameter Modelica.Units.SI.SpecificHeatCapacity cpAir = 1006;

  parameter Real XWatSup_default(min=0, max=0.1) = 0.008;

  parameter Real radiantGainFraction(min=0, max=1) = 0.30
    "Fraction of externally applied QInternal treated as radiant gain";

  Modelica.Blocks.Interfaces.RealInput TSup(
    final unit="K",
    displayUnit="degC");

  Modelica.Blocks.Interfaces.RealInput VSup_flow(
    final unit="m3/s");

  Modelica.Blocks.Interfaces.BooleanInput useExternalInternalGain
    "Enable externally supplied QInternal as an additional zone heat gain";

  Modelica.Blocks.Interfaces.RealInput QInternal(
    final unit="W");

  Modelica.Blocks.Interfaces.RealInput phiSup(
    final unit="1",
    start=0.5);

  Modelica.Blocks.Interfaces.RealOutput TRoo(
    final unit="K",
    displayUnit="degC");

  Modelica.Blocks.Interfaces.RealOutput QHVACSensible(
    final unit="W");

  Modelica.Blocks.Interfaces.RealOutput mSup_flow(
    final unit="kg/s");

  Modelica.Blocks.Interfaces.RealOutput VRet_flow(
    final unit="m3/s");

  inner Buildings.ThermalZones.EnergyPlus_24_2_0.Building building(
    idfName=idfName,
    epwName=epwName,
    weaName=weaName)
    "Building-level EnergyPlus/Spawn configuration";

  Buildings.ThermalZones.EnergyPlus_24_2_0.ThermalZone zon(
    redeclare package Medium = MediumA,
    zoneName=zoneName,
    nPorts=2,
    T_start=TRoo_start);

  Buildings.Fluid.Sources.MassFlowSource_T supAir(
    redeclare package Medium = MediumA,
    use_m_flow_in=true,
    use_T_in=true,
    X={XWatSup_default, 1 - XWatSup_default},
    nPorts=1);

  Buildings.Fluid.Sources.Boundary_pT retAir(
    redeclare package Medium = MediumA,
    p=101325,
    nPorts=1);

protected
  Real qGai_flow[3](each unit="W/m2");

  Modelica.Units.SI.Power QInternalApplied;
  Modelica.Units.SI.MassFlowRate mSup_flow_cmd;

equation
  mSup_flow_cmd = rhoAir * max(0, VSup_flow);

  supAir.m_flow_in = mSup_flow_cmd;
  supAir.T_in = TSup;

  connect(supAir.ports[1], zon.ports[1]);
  connect(zon.ports[2], retAir.ports[1]);

  // QInternal is an optional ADDITIONAL external heat gain controlled at runtime.
  // It does not disable people/lights/equipment gains already defined in the IDF.
  QInternalApplied =
    if useExternalInternalGain then QInternal else 0;

  qGai_flow[1] =
    radiantGainFraction
    * QInternalApplied
    / max(1.0, zon.AFlo);

  qGai_flow[2] =
    (1 - radiantGainFraction)
    * QInternalApplied
    / max(1.0, zon.AFlo);

  qGai_flow[3] = 0;

  zon.qGai_flow = qGai_flow;

  TRoo = zon.TAir;

  // zon.TRad / zon.phi are intentionally NOT wired to top-level outputs.
  // Local incremental testing with OpenModelica 1.27.0 showed that exposing
  // zon.TRad (and similarly additional Spawn-derived zone outputs) triggers
  // Pantelides index-reduction failure during buildModelFMU. The zone still
  // computes these quantities internally; they are simply not exported by
  // this FMU-facing wrapper.

  mSup_flow = mSup_flow_cmd;
  VRet_flow = max(0, VSup_flow);

  QHVACSensible =
    mSup_flow_cmd
    * cpAir
    * (TSup - TRoo);

  annotation (
    experiment(
      StartTime=0,
      StopTime=86400,
      Interval=60,
      Tolerance=1e-6),

    Documentation(
      info="<html>"
         + "<p>EnergyPlus/Spawn-backed high-fidelity alternative to ThermalZone.mo.</p>"
         + "<p>Uses Buildings.ThermalZones.EnergyPlus_24_2_0.</p>"
         + "<p>Required runtime assets: IDF, EPW, and matching MOS weather file.</p>"
         + "<p>The building instance must retain the exact name <code>building</code> "
         + "and is declared <code>inner</code> so EnergyPlus objects resolve their "
         + "inherited <code>outer building</code>.</p>"
         + "<p><code>useExternalInternalGain</code> is a Boolean FMU input. "
         + "When false, <code>QInternal</code> is ignored. When true, "
         + "<code>QInternal</code> is applied as an additional external zone gain. "
         + "This does not disable internal gains already defined in the IDF.</p>"
         + "<p><code>radiantGainFraction</code> splits the external QInternal gain "
         + "between radiant and convective portions.</p>"
         + "<p>Current RTU coupling is sensible-only; <code>phiSup</code> is reserved "
         + "for a future humidity/latent extension and is not yet used to set "
         + "supply-air moisture.</p>"
         + "<p>Inputs: TSup, VSup_flow, useExternalInternalGain, QInternal, phiSup.</p>"
         + "<p>Configuration parameters include: idfName, epwName, weaName, zoneName, "
         + "radiantGainFraction, and XWatSup_default.</p>"
         + "<p>Outputs: TRoo, QHVACSensible, mSup_flow, VRet_flow.</p>"
         + "<p>TRad and phiRoo are deliberately not exposed as top-level outputs "
         + "because OpenModelica 1.27.0 fails index reduction during FMU export when "
         + "additional Spawn-derived zone outputs are exposed by this wrapper.</p>"
         + "<p>Existing system loop: VAV.TSup -&gt; TSup, VAV.VSup_flow -&gt; "
         + "VSup_flow, TRoo -&gt; VAV.TRoo, TRoo -&gt; RTU.TRet.</p>"
         + "</html>"));

end EnergyPlusThermalZone;
