within ;
model SimpleChillerPlant
  "Simple two-chiller plant model for BACnet/FMUs"

  parameter Modelica.Units.SI.Temperature TChwSupSet = 279.15
    "Chilled-water supply temperature setpoint (6 C)";

  parameter Modelica.Units.SI.Power QChi_nominal = 500000
    "Nominal cooling capacity per chiller";

  parameter Real COP_nominal = 5.8
    "Nominal chiller COP";

  parameter Modelica.Units.SI.Temperature TCon_nominal = 298.15
    "Nominal condenser-water temperature (25 C)";

  parameter Modelica.Units.SI.VolumeFlowRate VChw_flow_nominal = 0.050
    "Nominal chilled-water volume flow rate";

  parameter Modelica.Units.SI.PressureDifference dpChw_nominal = 225000
    "Nominal chilled-water differential pressure";

  parameter Modelica.Units.SI.Density rhoWat = 998
    "Water density";

  parameter Modelica.Units.SI.SpecificHeatCapacity cpWat = 4180
    "Water specific heat capacity";

  parameter Real minCOP = 2.0
    "Minimum reported COP";

  parameter Real maxCOP = 8.0
    "Maximum reported COP";

  Modelica.Blocks.Interfaces.RealInput TChwRet(
    final unit="K",
    displayUnit="degC")
    "Chilled-water return temperature";

  Modelica.Blocks.Interfaces.RealInput TConWat(
    final unit="K",
    displayUnit="degC")
    "Cooling-tower/condenser-water leaving temperature";

  Modelica.Blocks.Interfaces.RealInput VChw_flow(
    final unit="m3/s")
    "Chilled-water volume flow rate";

  Modelica.Blocks.Interfaces.BooleanInput uChi1
    "Chiller 1 run/enable";

  Modelica.Blocks.Interfaces.BooleanInput uChi2
    "Chiller 2 run/enable";

  Modelica.Blocks.Interfaces.BooleanInput uPum1
    "Chilled-water pump 1 run";

  Modelica.Blocks.Interfaces.BooleanInput uPum2
    "Chilled-water pump 2 run";

  Modelica.Blocks.Interfaces.BooleanInput uTowFan1
    "Cooling-tower fan 1 run";

  Modelica.Blocks.Interfaces.BooleanInput uTowFan2
    "Cooling-tower fan 2 run";

  Modelica.Blocks.Interfaces.RealOutput TChwSup(
    final unit="K",
    displayUnit="degC")
    "Calculated chilled-water supply temperature";

  Modelica.Blocks.Interfaces.RealOutput dpChw(
    final unit="Pa")
    "Calculated chilled-water differential pressure";

  Modelica.Blocks.Interfaces.RealOutput PChi1(
    final unit="W")
    "Chiller 1 electric power";

  Modelica.Blocks.Interfaces.RealOutput PChi2(
    final unit="W")
    "Chiller 2 electric power";

  Modelica.Blocks.Interfaces.RealOutput COP1(
    final unit="1")
    "Chiller 1 coefficient of performance";

  Modelica.Blocks.Interfaces.RealOutput COP2(
    final unit="1")
    "Chiller 2 coefficient of performance";

  Modelica.Blocks.Interfaces.RealOutput QCoolDelivered(
    final unit="W")
    "Total delivered cooling";

  Modelica.Blocks.Interfaces.RealOutput plantPLR(
    final unit="1")
    "Plant part-load ratio";

protected
  Integer nChi;
  Integer nPum;
  Integer nTowFan;

  Real flowFra(unit="1");
  Real towerFactor(unit="1");
  Real condenserPenalty(unit="1");
  Real loadFactor(unit="1");

  Modelica.Units.SI.MassFlowRate mChw_flow;
  Modelica.Units.SI.Power QReq;
  Modelica.Units.SI.Power QAvail;
  Modelica.Units.SI.Power QChi1;
  Modelica.Units.SI.Power QChi2;
  Real COPBase(unit="1");

equation
  nChi = (if uChi1 then 1 else 0) + (if uChi2 then 1 else 0);
  nPum = (if uPum1 then 1 else 0) + (if uPum2 then 1 else 0);
  nTowFan = (if uTowFan1 then 1 else 0) + (if uTowFan2 then 1 else 0);

  mChw_flow = max(0, VChw_flow) * rhoWat;
  flowFra = if VChw_flow_nominal > 0 then max(0, VChw_flow) / VChw_flow_nominal else 0;

  // Required sensible cooling to reach the CHW supply setpoint.
  QReq = max(0, mChw_flow * cpWat * (TChwRet - TChwSupSet));

  // No chilled-water pump means no useful plant cooling.
  QAvail =
    if nPum > 0 then
      nChi * QChi_nominal
    else
      0;

  QCoolDelivered = min(QReq, QAvail);

  // Calculate leaving-water temperature from delivered cooling.
  TChwSup =
    if mChw_flow > 0 then
      max(TChwSupSet, TChwRet - QCoolDelivered / (mChw_flow * cpWat))
    else
      TChwRet;

  plantPLR =
    if QAvail > 0 then
      max(0, min(1, QCoolDelivered / QAvail))
    else
      0;

  // Tower fans slightly improve the effective condenser condition.
  towerFactor =
    if nTowFan >= 2 then 1.00
    elseif nTowFan == 1 then 0.95
    else 0.85;

  // COP falls as condenser-water temperature rises above nominal.
  condenserPenalty = max(0.45, 1 - 0.025 * (TConWat - TCon_nominal));

  // Mild efficiency penalty at very low plant load.
  loadFactor = 0.85 + 0.15 * plantPLR;

  COPBase = max(minCOP, min(maxCOP,
    COP_nominal * condenserPenalty * towerFactor * loadFactor));

  // Split plant load equally across enabled chillers.
  QChi1 =
    if uChi1 and nChi > 0 then
      QCoolDelivered / nChi
    else
      0;

  QChi2 =
    if uChi2 and nChi > 0 then
      QCoolDelivered / nChi
    else
      0;

  COP1 = if uChi1 then COPBase else 0;
  COP2 = if uChi2 then COPBase else 0;

  PChi1 = if QChi1 > 0 and COP1 > 0 then QChi1 / COP1 else 0;
  PChi2 = if QChi2 > 0 and COP2 > 0 then QChi2 / COP2 else 0;

  // Simplified pump/loop pressure model.
  dpChw =
    if nPum > 0 then
      dpChw_nominal * flowFra * flowFra / nPum
    else
      0;

  annotation (
    experiment(
      StartTime=0,
      StopTime=21600,
      Interval=60,
      Tolerance=1e-6),
    Documentation(info="<html>
<p>
Simple two-chiller plant model intended for FMU-based BACnet simulation.
It models chilled-water cooling delivery, chiller power/COP, and a simple
pump differential-pressure relationship without detailed refrigerant,
pump-curve, or cooling-tower physics.
</p>
<p>
Inputs: TChwRet, TConWat, VChw_flow, uChi1, uChi2, uPum1, uPum2,
uTowFan1, uTowFan2.
</p>
<p>
Outputs: TChwSup, dpChw, PChi1, PChi2, COP1, COP2,
QCoolDelivered, plantPLR.
</p>
</html>"));
end SimpleChillerPlant;
