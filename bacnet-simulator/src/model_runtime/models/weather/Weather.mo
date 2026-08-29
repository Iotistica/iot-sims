within ;
model Weather
  "Standalone Modelica weather-data playback -- wraps Buildings'
   BoundaryConditions.WeatherData.ReaderTMY3, no EnergyPlus/Spawn involved.
   Reads a converted .mos weather file and exposes dry-bulb temperature,
   relative humidity, wind speed, and global horizontal radiation as plain
   FMU outputs, matching the field set bacnet-simulator's own EPW-driven
   Weather Station points already use today."

  parameter String weaName = ""
    "Path to a converted .mos weather file (bare filename or absolute path,
     resolved by the FMU runtime's own file-staging convention -- see the
     app's shared/runtime/models/manager.py for how this parameter is set
     at session initialize() time)";

  Buildings.BoundaryConditions.WeatherData.ReaderTMY3 weaDat(
    filNam=weaName,
    computeWetBulbTemperature=false)
    "Weather file reader";

  Modelica.Blocks.Interfaces.RealOutput TDryBul(
    final unit="K",
    displayUnit="degC")
    "Dry bulb temperature";

  Modelica.Blocks.Interfaces.RealOutput relHum(
    final unit="1")
    "Relative humidity, 0-1";

  Modelica.Blocks.Interfaces.RealOutput winSpe(
    final unit="m/s")
    "Wind speed";

  Modelica.Blocks.Interfaces.RealOutput HGloHor(
    final unit="W/m2")
    "Global horizontal solar irradiation";

equation
  TDryBul = weaDat.weaBus.TDryBul;
  relHum = weaDat.weaBus.relHum;
  winSpe = weaDat.weaBus.winSpe;
  HGloHor = weaDat.weaBus.HGloHor;

  annotation (
    experiment(
      StartTime=0,
      StopTime=86400,
      Interval=60,
      Tolerance=1e-6),

    Documentation(
      info="<html>"
         + "<p>Pure-Modelica weather-data playback, no EnergyPlus/Spawn --"
         + " a lightweight alternative to driving a Weather Station's BACnet"
         + " points from an EPW file parsed in Python, using the same FMU"
         + " provider path every other model already goes through.</p>"
         + "<p>Uses Buildings.BoundaryConditions.WeatherData.ReaderTMY3, which"
         + " reads a Modelica-format (.mos) weather file directly -- no IDF,"
         + " no zone, no building geometry of any kind.</p>"
         + "<p>Configuration parameter: weaName (path to the .mos file, set at"
         + " session initialize() time -- see the app's runtime string-"
         + " parameter mechanism).</p>"
         + "<p>Outputs: TDryBul (K), relHum (0-1), winSpe (m/s), HGloHor"
         + " (W/m2) -- mirror bacnet-simulator's own epw.py WEATHER_FIELDS"
         + " (outdoor_air_temp_c, relative_humidity_pct, wind_speed_m_s,"
         + " global_horizontal_radiation_w_m2) so the app's mapping layer"
         + " needs no new naming/translation logic.</p>"
         + "</html>"));

end Weather;
