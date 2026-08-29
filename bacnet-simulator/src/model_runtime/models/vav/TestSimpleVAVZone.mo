model TestSimpleVAVZone

  SimpleVAVZone vavZone;

  Modelica.Blocks.Sources.Constant heaSet(
    k=293.15)
    "Heating setpoint = 20 C";

  Modelica.Blocks.Sources.Constant cooSet(
    k=296.15)
    "Cooling setpoint = 23 C";

  Modelica.Blocks.Sources.Constant supAirTem(
    k=286.15)
    "AHU supply air temperature = 13 C";

  Modelica.Blocks.Sources.Constant outAirTem(
    k=283.15)
    "Outdoor temperature = 30 C";

  Modelica.Blocks.Sources.Constant intGain(
    k=0)
    "Internal heat gain = 1000 W";

equation

  connect(heaSet.y, vavZone.TRooHeaSet);
  connect(cooSet.y, vavZone.TRooCooSet);
  connect(supAirTem.y, vavZone.TSupAHU);
  connect(outAirTem.y, vavZone.TOut);
  connect(intGain.y, vavZone.QInternal);

  annotation(
    experiment(
      StartTime=0,
      StopTime=21600,
      Interval=60,
      Tolerance=1e-6));

end TestSimpleVAVZone;
