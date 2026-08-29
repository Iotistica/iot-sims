model TestThermalZone

  ThermalZone zone;

  Modelica.Blocks.Sources.Constant outAirTem(
    k=283.15)
    "Outdoor temperature = 10 C";

  Modelica.Blocks.Sources.Constant peopleGain(
    k=300)
    "People heat gain = 300 W";

  Modelica.Blocks.Sources.Constant lightingGain(
    k=300)
    "Lighting heat gain = 300 W";

  Modelica.Blocks.Sources.Constant equipmentGain(
    k=400)
    "Equipment heat gain = 400 W (total internal gain = 1000 W)";

  Modelica.Blocks.Sources.Constant supTem(
    k=286.15)
    "Discharge air temperature = 13 C";

  Modelica.Blocks.Sources.Constant supFlow(
    k=0.3)
    "Supply airflow = 0.3 m3/s";

equation

  connect(outAirTem.y, zone.TOut);
  connect(peopleGain.y, zone.QPeople);
  connect(lightingGain.y, zone.QLighting);
  connect(equipmentGain.y, zone.QEquipment);
  connect(supTem.y, zone.TSup);
  connect(supFlow.y, zone.VSup_flow);

  annotation(
    experiment(
      StartTime=0,
      StopTime=86400,
      Interval=60,
      Tolerance=1e-6));

end TestThermalZone;
