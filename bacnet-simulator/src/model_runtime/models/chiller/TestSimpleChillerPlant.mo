within ;
model TestSimpleChillerPlant
  "Smoke-test harness for SimpleChillerPlant"

  SimpleChillerPlant plant;

  Modelica.Blocks.Sources.Constant chwRet(k=285.15)
    "12 C chilled-water return";

  Modelica.Blocks.Sources.Constant conWat(k=302.65)
    "29.5 C condenser-water";

  Modelica.Blocks.Sources.Constant chwFlow(k=0.048)
    "48 L/s chilled-water flow";

  Modelica.Blocks.Sources.BooleanConstant chiller1(k=true);
  Modelica.Blocks.Sources.BooleanConstant chiller2(k=true);
  Modelica.Blocks.Sources.BooleanConstant pump1(k=true);
  Modelica.Blocks.Sources.BooleanConstant pump2(k=false);
  Modelica.Blocks.Sources.BooleanConstant towerFan1(k=true);
  Modelica.Blocks.Sources.BooleanConstant towerFan2(k=true);

equation
  connect(chwRet.y, plant.TChwRet);
  connect(conWat.y, plant.TConWat);
  connect(chwFlow.y, plant.VChw_flow);
  connect(chiller1.y, plant.uChi1);
  connect(chiller2.y, plant.uChi2);
  connect(pump1.y, plant.uPum1);
  connect(pump2.y, plant.uPum2);
  connect(towerFan1.y, plant.uTowFan1);
  connect(towerFan2.y, plant.uTowFan2);

  annotation (
    experiment(
      StartTime=0,
      StopTime=21600,
      Interval=60,
      Tolerance=1e-6));
end TestSimpleChillerPlant;
