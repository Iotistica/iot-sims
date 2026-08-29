within ;
model ThermalZone
  "Standalone thermal-zone model for FMU export"

  parameter Modelica.Units.SI.Volume VRoo = 150
    "Room air volume";
  parameter Modelica.Units.SI.HeatCapacity CBuilding = 5e6
    "Effective building/room thermal capacitance excluding room air";
  parameter Modelica.Units.SI.ThermalConductance UA = 150
    "Effective thermal conductance between zone and outdoors";
  parameter Modelica.Units.SI.Density rhoAir = 1.2
    "Representative room-air density";
  parameter Modelica.Units.SI.SpecificHeatCapacity cpAir = 1006
    "Representative dry-air specific heat capacity";
  parameter Modelica.Units.SI.Temperature TRoo_start = 295.15
    "Initial zone temperature";

  Modelica.Blocks.Interfaces.RealInput TOut(
    final unit="K",
    displayUnit="degC")
    "Outdoor-air temperature";

  Modelica.Blocks.Interfaces.RealInput QPeople(
    final unit="W")
    "People sensible heat gain";

  Modelica.Blocks.Interfaces.RealInput QLighting(
    final unit="W")
    "Lighting heat gain";

  Modelica.Blocks.Interfaces.RealInput QEquipment(
    final unit="W")
    "Equipment heat gain";

  Modelica.Blocks.Interfaces.RealInput TSup(
    final unit="K",
    displayUnit="degC")
    "Supply/discharge air temperature entering the zone";

  Modelica.Blocks.Interfaces.RealInput VSup_flow(
    final unit="m3/s")
    "Supply-air volumetric flow rate entering the zone";

  Modelica.Blocks.Interfaces.RealOutput TRoo(
    final unit="K",
    displayUnit="degC")
    "Zone air temperature";

  Modelica.Blocks.Interfaces.RealOutput QHVAC(
    final unit="W")
    "Sensible HVAC heat flow into the zone; negative means cooling";

  Modelica.Blocks.Interfaces.RealOutput QEnvelope(
    final unit="W")
    "Envelope heat flow into the zone; positive means heat gain";

  Modelica.Blocks.Interfaces.RealOutput QNet(
    final unit="W")
    "Net sensible heat flow into the zone";

  Modelica.Blocks.Interfaces.RealOutput QInternalTotal(
    final unit="W")
    "Total internal sensible heat gain (people + lighting + equipment)";

protected
  parameter Modelica.Units.SI.HeatCapacity CAir =
    rhoAir * VRoo * cpAir
    "Thermal capacitance of the room air";

  parameter Modelica.Units.SI.HeatCapacity CZone =
    CBuilding + CAir
    "Total effective zone thermal capacitance";

initial equation
  TRoo = TRoo_start;

equation
  QInternalTotal = QPeople + QLighting + QEquipment;
  QHVAC = rhoAir * max(0, VSup_flow) * cpAir * (TSup - TRoo);
  QEnvelope = UA * (TOut - TRoo);
  QNet = QHVAC + QEnvelope + QInternalTotal;
  CZone * der(TRoo) = QNet;

  annotation (
    experiment(
      StartTime=0,
      StopTime=86400,
      Interval=60,
      Tolerance=1e-6),
    Documentation(info="<html>
<p>Standalone thermal-zone model intended for FMU use.</p>
<p>The model is independent of the VAV controller. A terminal-unit model supplies discharge-air temperature and airflow through TSup and VSup_flow. The zone returns TRoo.</p>
<p>Zone temperature follows a lumped sensible heat balance consisting of HVAC supply-air heat transfer, envelope heat transfer, and internal sensible heat gain. Internal sensible heat gain is the sum of three separately-driven components -- people, lighting, and equipment -- summed into QInternalTotal, which is used everywhere the heat balance needs a single internal-gain term and is also exposed as a diagnostic output.</p>
<p>Inputs: TOut, QPeople, QLighting, QEquipment, TSup, VSup_flow.</p>
<p>Outputs: TRoo, QHVAC, QEnvelope, QNet, QInternalTotal.</p>
<p>Parameters: VRoo, CBuilding, UA, rhoAir, cpAir, TRoo_start.</p>
</html>"));
end ThermalZone;
