within ;
model LightingZone
  "Self-contained lighting-zone model for BMS/BACnet simulation"

  // ---------------------------------------------------------------------------
  // Configuration
  // ---------------------------------------------------------------------------

  parameter Boolean useSchedule = true
    "Require scheduleActive for automatic operation";

  parameter Boolean useOccupancy = true
    "Require effective occupancy for automatic operation";

  parameter Boolean useDaylightHarvesting = true
    "Reduce artificial light as daylight increases";

  parameter Boolean useDayNightControl = false
    "Require isNight for automatic operation (typically exterior lighting)";

  parameter Modelica.Units.SI.Power ratedPowerW = 1000
    "Total electrical power at full light output";

  parameter Modelica.Units.SI.Power standbyPowerRatedW = 2
    "Electrical standby power while driver is powered";

  parameter Real minimumDimFraction(min=0, max=1) = 0.10
    "Minimum non-zero dim level";

  parameter Real dimmingPowerExponent(min=0.01) = 1.0
    "Power curve exponent: active power fraction = dim^exponent";

  parameter Modelica.Units.SI.Time dimmingTimeConstant(min=1e-6) = 2
    "First-order response time from requested to actual dim level";

  parameter Modelica.Units.SI.Time occupancyOffDelay(min=0) = 900
    "Hold effective occupancy after occupancy input becomes false";

  parameter Modelica.Units.SI.Time manualOverrideDuration(min=0) = 7200
    "Manual override duration; 0 means override does not expire";

  parameter Real illuminanceSetpointLux(min=0) = 500
    "Target total illuminance for daylight harvesting";

  parameter Real designIlluminanceLux(min=1e-6) = 500
    "Artificial illuminance at full light output";

  parameter Real radiantFraction(min=0, max=1) = 0.30
    "Fraction of electrical power represented as radiant heat";

  parameter Real convectiveFraction(min=0, max=1) = 0.50
    "Fraction of electrical power represented as convective heat";

  parameter Real visibleFraction(min=0, max=1) = 0.20
    "Fraction of electrical power represented as visible output";

  parameter Real dimFaultMaximumFraction(min=0, max=1) = 0.50
    "Maximum achievable dim fraction when faultDimLimited is active";

  parameter Real degradedLightOutputFraction(min=0, max=1) = 0.60
    "Light-output multiplier when faultLampDegraded is active";

  parameter Real statusThreshold(min=0, max=1) = 0.001
    "Dim fraction above which lightOn is true";

  // ---------------------------------------------------------------------------
  // Inputs
  // ---------------------------------------------------------------------------

  Modelica.Blocks.Interfaces.BooleanInput scheduleActive
    "Normal BMS lighting schedule is enabled";

  Modelica.Blocks.Interfaces.BooleanInput occupied
    "Raw occupancy sensor state";

  Modelica.Blocks.Interfaces.BooleanInput isNight
    "Day/night state; true = night";

  Modelica.Blocks.Interfaces.RealInput daylightLux(unit="lx")
    "Available daylight at the controlled point";

  Modelica.Blocks.Interfaces.IntegerInput manualMode
    "0=Auto, 1=ManualOff, 2=ManualOn, 3=ManualDim";

  Modelica.Blocks.Interfaces.RealInput manualDimFraction
    "Manual dim request, 0..1, used when manualMode=3";

  Modelica.Blocks.Interfaces.BooleanInput driverPowered
    "If false, fixture has no electrical supply (e.g. breaker/circuit
    off) -- runtime-controllable rather than a fixed parameter so it can
    be driven by a BACnet point or simulated event, not just set once at
    configuration time.";

  // Simple physical fault inputs.
  Modelica.Blocks.Interfaces.BooleanInput faultDriverFailed
    "Driver failure: no light output and no electrical power";

  Modelica.Blocks.Interfaces.BooleanInput faultStuckOn
    "Output stuck at full light";

  Modelica.Blocks.Interfaces.BooleanInput faultDimLimited
    "Fixture cannot exceed dimFaultMaximumFraction";

  Modelica.Blocks.Interfaces.BooleanInput faultLampDegraded
    "Electrical operation is normal but produced light is degraded";

  // ---------------------------------------------------------------------------
  // Outputs - control / operating state
  // ---------------------------------------------------------------------------

  Modelica.Blocks.Interfaces.BooleanOutput effectiveOccupied
    "Occupancy after application of off-delay";

  Modelica.Blocks.Interfaces.BooleanOutput automaticAllowed
    "Automatic lighting is permitted by schedule/day-night logic";

  Modelica.Blocks.Interfaces.BooleanOutput manualOverrideActive
    "A non-expired manual override is active";

  Modelica.Blocks.Interfaces.RealOutput overrideTimeRemainingS(unit="s")
    "Seconds remaining in timed manual override; 0 for none or indefinite";

  Modelica.Blocks.Interfaces.RealOutput automaticDimFraction
    "Automatic dim request before manual override";

  Modelica.Blocks.Interfaces.RealOutput requestedDimFraction
    "Final controller request before physical faults/dynamics";

  Modelica.Blocks.Interfaces.RealOutput actualDimFraction(start=0, fixed=true)
    "Physical dim level after faults and first-order response";

  Modelica.Blocks.Interfaces.BooleanOutput lightOn
    "True when actual dim level is above statusThreshold";

  Modelica.Blocks.Interfaces.BooleanOutput faultActive
    "True if any modeled lighting fault is active";

  // ---------------------------------------------------------------------------
  // Outputs - lighting / electrical / thermal
  // ---------------------------------------------------------------------------

  Modelica.Blocks.Interfaces.RealOutput artificialLightLux(unit="lx")
    "Modeled artificial illuminance";

  Modelica.Blocks.Interfaces.RealOutput totalIlluminanceLux(unit="lx")
    "Daylight plus artificial illuminance";

  Modelica.Blocks.Interfaces.RealOutput standbyPowerW(unit="W")
    "Current standby electrical power";

  Modelica.Blocks.Interfaces.RealOutput activeLightingPowerW(unit="W")
    "Current dim-dependent active lighting power";

  Modelica.Blocks.Interfaces.RealOutput electricalPowerW(unit="W")
    "Total instantaneous electrical power";

  Modelica.Blocks.Interfaces.RealOutput radiantHeatGainW(unit="W")
    "Radiant heat-gain component";

  Modelica.Blocks.Interfaces.RealOutput convectiveHeatGainW(unit="W")
    "Convective heat-gain component";

  Modelica.Blocks.Interfaces.RealOutput visibleOutputW(unit="W")
    "Visible-radiation component for reporting";

  Modelica.Blocks.Interfaces.RealOutput electricalEnergyWh(
    unit="W.h",
    start=0,
    fixed=true)
    "Integrated electrical energy since model initialization";

  Modelica.Blocks.Interfaces.RealOutput runtimeHours(
    unit="h",
    start=0,
    fixed=true)
    "Accumulated time with physical light output on";

protected
  discrete Real unoccupiedSince(start=-1, fixed=true)
    "Time at which raw occupancy most recently became false";

  discrete Real manualOverrideStart(start=-1, fixed=true)
    "Time at which current manual override began";

  Real daylightDimRequest
    "Unclamped daylight-harvesting request";

  Real controllerDim
    "Controller request after minimum-dim handling";

  Real physicalTargetDim
    "Target physical dim after modeled faults";

  Real powerFraction
    "Normalized active electrical power fraction";

  Real lightOutputMultiplier
    "Multiplier used for degraded light-output fault";

initial equation
  assert(ratedPowerW >= standbyPowerRatedW,
    "ratedPowerW must be greater than or equal to standbyPowerRatedW.");

  assert(radiantFraction + convectiveFraction + visibleFraction <= 1.000001,
    "radiantFraction + convectiveFraction + visibleFraction must be <= 1.");

equation

  // ---------------------------------------------------------------------------
  // Occupancy off-delay
  // ---------------------------------------------------------------------------

  effectiveOccupied =
    if not useOccupancy then
      true
    elseif occupied then
      true
    elseif unoccupiedSince < 0 then
      false
    else
      time - unoccupiedSince < occupancyOffDelay;

  // ---------------------------------------------------------------------------
  // Automatic permission logic
  // ---------------------------------------------------------------------------

  automaticAllowed =
    (not useSchedule or scheduleActive)
    and
    (not useDayNightControl or isNight);

  // Daylight harvesting requests the fraction of design artificial
  // illuminance needed to make up the remaining illuminance deficit.
  daylightDimRequest =
    (illuminanceSetpointLux - max(0.0, daylightLux))
    / designIlluminanceLux;

  automaticDimFraction =
    if automaticAllowed and effectiveOccupied then
      if useDaylightHarvesting then
        min(1.0, max(0.0, daylightDimRequest))
      else
        1.0
    else
      0.0;

  // Apply minimum non-zero dim level.
  controllerDim =
    if automaticDimFraction > 0.0 then
      max(minimumDimFraction, automaticDimFraction)
    else
      0.0;

  // ---------------------------------------------------------------------------
  // Manual override
  // ---------------------------------------------------------------------------

  manualOverrideActive =
    manualMode <> 0
    and manualOverrideStart >= 0
    and (
      manualOverrideDuration <= 0
      or time - manualOverrideStart < manualOverrideDuration
    );

  overrideTimeRemainingS =
    if manualOverrideActive and manualOverrideDuration > 0 then
      max(0.0, manualOverrideDuration - (time - manualOverrideStart))
    else
      0.0;

  requestedDimFraction =
    if manualOverrideActive then
      if manualMode == 1 then
        0.0
      elseif manualMode == 2 then
        1.0
      elseif manualMode == 3 then
        min(1.0, max(0.0, manualDimFraction))
      else
        controllerDim
    else
      controllerDim;

  // ---------------------------------------------------------------------------
  // Physical fault precedence:
  //   1) no driver power / failed driver
  //   2) stuck on
  //   3) dim-limited
  //   4) normal request
  // ---------------------------------------------------------------------------

  physicalTargetDim =
    if not driverPowered or faultDriverFailed then
      0.0
    elseif faultStuckOn then
      1.0
    elseif faultDimLimited then
      min(requestedDimFraction, dimFaultMaximumFraction)
    else
      requestedDimFraction;

  // First-order fixture / driver response.
  der(actualDimFraction) =
    (physicalTargetDim - actualDimFraction) / dimmingTimeConstant;

  lightOn = actualDimFraction > statusThreshold;

  faultActive =
    faultDriverFailed
    or faultStuckOn
    or faultDimLimited
    or faultLampDegraded;

  // ---------------------------------------------------------------------------
  // Illuminance model
  // ---------------------------------------------------------------------------

  lightOutputMultiplier =
    if faultLampDegraded then degradedLightOutputFraction else 1.0;

  artificialLightLux =
    designIlluminanceLux
    * min(1.0, max(0.0, actualDimFraction))
    * lightOutputMultiplier;

  totalIlluminanceLux =
    max(0.0, daylightLux) + artificialLightLux;

  // ---------------------------------------------------------------------------
  // Electrical model
  //
  // ratedPowerW is interpreted as TOTAL fixture power at 100% output.
  // Therefore standby is not added on top of ratedPowerW at full output.
  // ---------------------------------------------------------------------------

  standbyPowerW =
    if driverPowered and not faultDriverFailed then
      standbyPowerRatedW
    else
      0.0;

  powerFraction =
    min(1.0, max(0.0, actualDimFraction)) ^ dimmingPowerExponent;

  activeLightingPowerW =
    if driverPowered and not faultDriverFailed then
      max(0.0, ratedPowerW - standbyPowerRatedW) * powerFraction
    else
      0.0;

  electricalPowerW = standbyPowerW + activeLightingPowerW;

  // ---------------------------------------------------------------------------
  // Thermal / visible partitioning
  // ---------------------------------------------------------------------------

  radiantHeatGainW = electricalPowerW * radiantFraction;
  convectiveHeatGainW = electricalPowerW * convectiveFraction;
  visibleOutputW = electricalPowerW * visibleFraction;

  // ---------------------------------------------------------------------------
  // Integrated diagnostic quantities.
  // The external Energy Engine can still remain authoritative for project
  // history, utility accounting, cost, and emissions.
  // ---------------------------------------------------------------------------

  der(electricalEnergyWh) = electricalPowerW / 3600.0;
  der(runtimeHours) = if lightOn then 1.0 / 3600.0 else 0.0;

algorithm
  // Track start of unoccupied period.
  when {initial(), change(occupied)} then
    if occupied then
      unoccupiedSince := -1.0;
    else
      unoccupiedSince := time;
    end if;
  end when;

  // Start/restart override timer whenever the manual mode changes.
  when {initial(), change(manualMode)} then
    if manualMode == 0 then
      manualOverrideStart := -1.0;
    else
      manualOverrideStart := time;
    end if;
  end when;

  annotation (
    Documentation(info="<html>
<p>
Self-contained lighting-zone model intended for initial OMEdit/OpenModelica
verification and later FMU/BACnet integration.
</p>

<p><b>manualMode:</b></p>
<ul>
<li>0 = Auto</li>
<li>1 = Manual Off</li>
<li>2 = Manual On</li>
<li>3 = Manual Dim (uses manualDimFraction)</li>
</ul>

<p>
Automatic control can combine schedule permission, occupancy with off-delay,
optional day/night permission, and daylight harvesting. The model also
contains first-order dimming dynamics, simple fault injection, electrical
power, illuminance, thermal/visible power partitions, energy integration,
and runtime integration.
</p>
</html>"),
    experiment(StartTime=0, StopTime=86400, Tolerance=1e-6, Interval=60));
end LightingZone;
