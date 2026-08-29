within ;
model TestSimpleAHU
  "Dynamic setpoint test harness for SimpleAHU"

  SimpleAHU ahu;

  Modelica.Blocks.Sources.CombiTimeTable supSet(
    table=[
         0, 286.15;
      3600, 286.15;
      3601, 289.15;
      7200, 289.15;
      7201, 285.15;
     10800, 285.15;
     10801, 287.15;
     21600, 287.15
    ],
    columns={2},
    smoothness=Modelica.Blocks.Types.Smoothness.ConstantSegments)
    "Supply-air setpoint profile: 13C -> 16C -> 12C -> 14C";

  Modelica.Blocks.Sources.Constant outTem(k=303.15)
    "Outdoor air = 30 C";

  Modelica.Blocks.Sources.Constant retTem(k=297.15)
    "Return air = 24 C";

  Modelica.Blocks.Sources.Constant fanCmd(k=0.8)
    "Fan command = 80%";

equation
  connect(supSet.y[1], ahu.TSupSet);
  connect(outTem.y, ahu.TOut);
  connect(retTem.y, ahu.TRet);
  connect(fanCmd.y, ahu.uFan);

  annotation (
    experiment(
      StartTime=0,
      StopTime=21600,
      Interval=60,
      Tolerance=1e-6),
    Documentation(info="<html>
<p>
Dynamic supply-air-temperature setpoint test.
</p>
<p>
Setpoint schedule:
13 C from 0 to 1 h,
16 C from 1 to 2 h,
12 C from 2 to 3 h,
14 C from 3 to 6 h.
</p>
<p>
Outdoor air is fixed at 30 C, return air at 24 C, and fan command at 80%.
</p>
<p>
Plot ahu.TSupSet, ahu.TSup, ahu.yCooVal, ahu.yHeaVal,
ahu.yOutDam, ahu.TMix, and ahu.VSup_flow.
</p>
</html>"));
end TestSimpleAHU;
