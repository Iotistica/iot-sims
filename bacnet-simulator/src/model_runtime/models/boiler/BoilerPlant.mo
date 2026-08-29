within ;
model BoilerPlant
  "Central two-boiler hot-water plant with condensing efficiency and outdoor-air reset"

  // ===========================================================================
  // EXPERIMENTAL MODEL -- new central hot-water plant, the boiler-side
  // counterpart to models/ahu/AHU.mo. models/boiler/ had no prior model to
  // preserve (it was an empty directory), so there is nothing to rename or
  // avoid modifying here.
  //
  // Central-plant architecture this belongs to:
  //   SimpleChillerPlant -> AHU -> VAV -> ThermalZone   (cooling side)
  //   BoilerPlant         -> AHU -> VAV -> ThermalZone   (heating side, this model)
  //
  // Integration is signal-based, not a shared Modelica fluid network: this
  // FMU's inputs are THotWatRet/VHotWat_flow (aggregate downstream return
  // temperature and flow), not fluid ports connecting to AHU.mo's or
  // SimpleVAVZone.mo's own FMUs. See section "Future integration" in this
  // documentation string for the intended signal wiring once multiple FMUs
  // are composed together.
  // ===========================================================================

  // ---------------------------------------------------------------------------
  // Media
  // ---------------------------------------------------------------------------
  replaceable package MediumW = Buildings.Media.Water
    constrainedby Modelica.Media.Interfaces.PartialMedium
    "Water medium (plant hot-water loop)";

  // ---------------------------------------------------------------------------
  // Design parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.Power QBoi_nominal = 500000
    "Nominal heating capacity per boiler (500 kW)";

  parameter Modelica.Units.SI.VolumeFlowRate VHotWat_flow_nominal = 0.012
    "Nominal combined plant hot-water flow (both boilers), ~12 L/s.
    Sized from 2*QBoi_nominal at a 20 K design supply/return temperature
    difference: 1e6 W / (998 kg/m3 * 4180 J/kg.K * 20 K).";

  parameter Modelica.Units.SI.PressureDifference dpBoi_nominal = 30000
    "Water-side pressure drop through each boiler at its nominal flow";

  parameter Modelica.Units.SI.Density rhoWat = 998
    "Water density, used to convert VHotWat_flow to mass flow";

  parameter Modelica.Units.SI.SpecificHeatCapacity cpWat = 4180
    "Water specific heat capacity";

  parameter Modelica.Units.SI.MassFlowRate mHotWat_flow_min = 0.02*rhoWat*VHotWat_flow_nominal
    "Minimum plant mass flow below which THotWatSup is reported as
    THotWatRet rather than the mixing-volume reading, which (like any
    lumped-volume temperature at near-zero throughflow) is not physically
    meaningful there -- see AHU.mo's own documented zero-CHW-flow behavior
    for the same underlying reason this guard exists.";

  // ---------------------------------------------------------------------------
  // Outdoor-air hot-water reset parameters
  // ---------------------------------------------------------------------------
  parameter Boolean haveOutdoorReset = true
    "Set to false to use a fixed THotWatSupSetFixed instead of resetting
    the supply setpoint from outdoor temperature";

  parameter Modelica.Units.SI.Temperature THotWatSupSetMax = 355.15
    "Maximum hot-water supply setpoint, at/below TOutResetLow (82 degC)";

  parameter Modelica.Units.SI.Temperature THotWatSupSetMin = 328.15
    "Minimum hot-water supply setpoint, at/above TOutResetHigh (55 degC)";

  parameter Modelica.Units.SI.Temperature TOutResetLow = 263.15
    "Outdoor temperature at/below which the reset uses THotWatSupSetMax (-10 degC)";

  parameter Modelica.Units.SI.Temperature TOutResetHigh = 288.15
    "Outdoor temperature at/above which the reset uses THotWatSupSetMin (15 degC)";

  parameter Modelica.Units.SI.Temperature THotWatSupSetFixed = THotWatSupSetMax
    "Fixed hot-water supply setpoint used when haveOutdoorReset=false";

  // ---------------------------------------------------------------------------
  // Condensing efficiency parameters
  //
  // eta = etaTemperature(THotWatRet) * etaPartLoad(y), clamped to
  // [eta_min, eta_max]. See the CondensingBoiler local class below and the
  // model-level Documentation annotation for the exact formula and why it
  // depends on entering (return) water temperature rather than the boiler's
  // own leaving-water temperature.
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.Efficiency eta_nominal = 0.85
    "Boiler efficiency at the non-condensing design condition (used to
    normalize fuel consumption in PartialBoiler; see QFue_flow)";

  parameter Modelica.Units.SI.Efficiency eta_min = 0.75
    "Lower bound on reported/effective efficiency";

  parameter Modelica.Units.SI.Efficiency eta_max = 0.98
    "Upper bound on reported/effective efficiency (full condensing, near-peak part load)";

  parameter Modelica.Units.SI.Temperature TCondensingHigh = 333.15
    "Return-water temperature at/above which no condensing bonus applies,
    eta -> eta_nominal (60 degC)";

  parameter Modelica.Units.SI.Temperature TCondensingLow = 303.15
    "Return-water temperature at/below which the full condensing bonus
    applies, eta -> eta_max (30 degC)";

  parameter Real yEtaPeak(min=0, max=1) = 0.4
    "Part-load ratio at which the part-load efficiency multiplier peaks (1.0)";

  parameter Real kPlrPenalty(min=0) = 0.10
    "Part-load efficiency penalty coefficient -- quadratic falloff in firing
    rate away from yEtaPeak, approximating increased flue losses at high
    fire and reduced heat-exchanger effectiveness at very low fire";

  // ---------------------------------------------------------------------------
  // Pump / differential-pressure parameters
  // ---------------------------------------------------------------------------
  parameter Modelica.Units.SI.PressureDifference dpHotWat_nominal = 150000
    "Nominal plant differential pressure at full combined flow, both pumps running";

  parameter Modelica.Units.SI.Power PPum_nominal = 3000
    "Nominal electrical power per pump at full flow (3 kW; hydraulic power
    dpHotWat_nominal*VHotWat_flow_nominal/2 per pump at an assumed ~70% pump
    efficiency)";

  // ---------------------------------------------------------------------------
  // FMU-facing inputs
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.RealInput THotWatRet(
    final unit="K",
    displayUnit="degC")
    "Plant hot-water return temperature (aggregate, from downstream heating coils)";

  Modelica.Blocks.Interfaces.RealInput TOut(
    final unit="K",
    displayUnit="degC")
    "Outdoor-air temperature, drives the supply-water reset";

  Modelica.Blocks.Interfaces.RealInput VHotWat_flow(
    final unit="m3/s")
    "Total downstream plant hot-water volumetric flow";

  Modelica.Blocks.Interfaces.BooleanInput uBoi1
    "Boiler 1 enable/availability";

  Modelica.Blocks.Interfaces.BooleanInput uBoi2
    "Boiler 2 enable/availability";

  Modelica.Blocks.Interfaces.BooleanInput uPum1
    "Hot-water pump 1 run command";

  Modelica.Blocks.Interfaces.BooleanInput uPum2
    "Hot-water pump 2 run command";

  // ---------------------------------------------------------------------------
  // FMU-facing outputs
  // ---------------------------------------------------------------------------
  Modelica.Blocks.Interfaces.RealOutput THotWatSup(
    final unit="K",
    displayUnit="degC")
    "Plant hot-water supply temperature";

  Modelica.Blocks.Interfaces.RealOutput THotWatSupSet(
    final unit="K",
    displayUnit="degC")
    "Hot-water supply temperature setpoint (post outdoor-air reset)";

  Modelica.Blocks.Interfaces.RealOutput dpHotWat(
    final unit="Pa")
    "Calculated plant differential pressure";

  Modelica.Blocks.Interfaces.RealOutput QBoi1(
    final unit="W")
    "Boiler 1 useful thermal heating output";

  Modelica.Blocks.Interfaces.RealOutput QBoi2(
    final unit="W")
    "Boiler 2 useful thermal heating output";

  Modelica.Blocks.Interfaces.RealOutput QHeatDelivered(
    final unit="W")
    "Total plant delivered heating (QBoi1 + QBoi2, from actual coil-model output)";

  Modelica.Blocks.Interfaces.RealOutput PBoi1(
    final unit="W")
    "Boiler 1 fuel (natural gas) input power -- NOT electrical power";

  Modelica.Blocks.Interfaces.RealOutput PBoi2(
    final unit="W")
    "Boiler 2 fuel (natural gas) input power -- NOT electrical power";

  Modelica.Blocks.Interfaces.RealOutput etaBoi1(
    final unit="1")
    "Boiler 1 useful thermal output / fuel input power";

  Modelica.Blocks.Interfaces.RealOutput etaBoi2(
    final unit="1")
    "Boiler 2 useful thermal output / fuel input power";

  Modelica.Blocks.Interfaces.RealOutput PPum1(
    final unit="W")
    "Hot-water pump 1 electrical power";

  Modelica.Blocks.Interfaces.RealOutput PPum2(
    final unit="W")
    "Hot-water pump 2 electrical power";

  Modelica.Blocks.Interfaces.RealOutput plantPLR(
    final unit="1")
    "Plant part-load ratio: actual delivered heating / available enabled capacity";

  Modelica.Blocks.Interfaces.RealOutput flowFraction(
    final unit="1")
    "Actual plant flow / VHotWat_flow_nominal";

  Modelica.Blocks.Interfaces.RealOutput availableHeatingCapacity(
    final unit="W")
    "Nominal heating capacity of currently enabled boilers (nBoiEnabled * QBoi_nominal)";

protected
  Real mHotWat_flow(unit="kg/s");
  Real resetFra(unit="1")
    "Normalized outdoor-reset signal: 1 at/below TOutResetLow, 0 at/above TOutResetHigh";
  Integer nBoiEnabled "Count of enabled boilers, 0..2";
  Integer nBoiRouted "Boiler legs actually receiving flow -- both, if none enabled but a pump runs";
  Integer nPumOn "Count of running pumps, 0..2";
  Real y1(unit="1", min=0, max=1) "Boiler 1 firing/part-load-ratio command";
  Real y2(unit="1", min=0, max=1) "Boiler 2 firing/part-load-ratio command";
  Real mBoi1_flow_cmd(unit="kg/s") "Mass flow routed through boiler 1";
  Real mBoi2_flow_cmd(unit="kg/s") "Mass flow routed through boiler 2";
  Real QReq(unit="W") "Feed-forward required heating to hit THotWatSupSet from THotWatRet at current flow";
  Real plantPLRTarget(unit="1") "Feed-forward PLR target driving y1/y2, before real-boiler-physics feedback";
  Real etaTemperatureBonus(unit="1")
    "Condensing efficiency bonus from THotWatRet alone (no part-load term) --
    used only to compute yCap below, not reported as etaBoi1/etaBoi2.";
  Real yCap(unit="1", min=0, max=1)
    "Firing-rate ceiling that keeps each boiler's real thermal output from
    exceeding its nameplate QBoi_nominal even when the condensing bonus
    pushes eta above eta_nominal (QFue_flow is normalized by eta_nominal,
    so eta > eta_nominal at y=1 would otherwise let QWat_flow exceed
    Q_flow_nominal -- see Documentation, 'Capacity limiting and the
    condensing bonus'). Computed from THotWatRet alone, independent of y,
    so applying it to y1/y2 introduces no algebraic loop.";

  // ---------------------------------------------------------------------------
  // Local class: PartialBoiler extended with a custom condensing efficiency
  // curve driven by an externally supplied entering (return) water
  // temperature, rather than PartialBoiler's own leaving-water-temperature
  // -based EfficiencyCurves.QuadraticLinear option. See the model-level
  // Documentation annotation for why.
  //
  // eta's binding here mirrors exactly how Buildings.Fluid.Boilers.
  // BoilerPolynomial itself supplies eta to PartialBoiler (extends
  // PartialBoiler(eta=...)) -- this is the library's own supported
  // extension pattern, not a workaround.
  // ---------------------------------------------------------------------------
  model CondensingBoiler
    "PartialBoiler with efficiency driven by entering (return) water temperature and part-load ratio"
    extends Buildings.Fluid.Boilers.BaseClasses.PartialBoiler(
      final eta = max(etaMin, min(etaMax,
        (etaNom + (etaMax - etaNom) * max(0, min(1,
          (tCondHigh - TRetBoiler) / max(1, tCondHigh - tCondLow))))
        * (1 - kPlrPen * (y - yPeak)^2))));

    parameter Modelica.Units.SI.Efficiency etaNom "See BoilerPlant.eta_nominal";
    parameter Modelica.Units.SI.Efficiency etaMin "See BoilerPlant.eta_min";
    parameter Modelica.Units.SI.Efficiency etaMax "See BoilerPlant.eta_max";
    parameter Modelica.Units.SI.Temperature tCondHigh "See BoilerPlant.TCondensingHigh";
    parameter Modelica.Units.SI.Temperature tCondLow "See BoilerPlant.TCondensingLow";
    parameter Real yPeak "See BoilerPlant.yEtaPeak";
    parameter Real kPlrPen "See BoilerPlant.kPlrPenalty";

    input Modelica.Units.SI.Temperature TRetBoiler
      "Entering (return) water temperature driving the condensing bonus -- supplied
      by the enclosing BoilerPlant instance, not measured from this boiler's own
      ports, since it is the plant's aggregate return condition by design.";
  end CondensingBoiler;

  // ---------------------------------------------------------------------------
  // Boiler 1 / Boiler 2
  // ---------------------------------------------------------------------------
  CondensingBoiler boiler1(
    redeclare package Medium = MediumW,
    Q_flow_nominal=QBoi_nominal,
    m_flow_nominal=rhoWat*VHotWat_flow_nominal/2,
    dp_nominal=dpBoi_nominal,
    fue=Buildings.Fluid.Data.Fuels.NaturalGasLowerHeatingValue(),
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial,
    T_start=333.15,
    eta_nominal=eta_nominal,
    etaNom=eta_nominal,
    etaMin=eta_min,
    etaMax=eta_max,
    tCondHigh=TCondensingHigh,
    tCondLow=TCondensingLow,
    yPeak=yEtaPeak,
    kPlrPen=kPlrPenalty)
    "Boiler 1 -- real two-port fluid/thermal/fuel physics";

  CondensingBoiler boiler2(
    redeclare package Medium = MediumW,
    Q_flow_nominal=QBoi_nominal,
    m_flow_nominal=rhoWat*VHotWat_flow_nominal/2,
    dp_nominal=dpBoi_nominal,
    fue=Buildings.Fluid.Data.Fuels.NaturalGasLowerHeatingValue(),
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial,
    T_start=333.15,
    eta_nominal=eta_nominal,
    etaNom=eta_nominal,
    etaMin=eta_min,
    etaMax=eta_max,
    tCondHigh=TCondensingHigh,
    tCondLow=TCondensingLow,
    yPeak=yEtaPeak,
    kPlrPen=kPlrPenalty)
    "Boiler 2 -- real two-port fluid/thermal/fuel physics";

  // ---------------------------------------------------------------------------
  // Plant header -- mirrors AHU.mo's own
  // "two MassFlowSource_T streams -> shared MixingVolume -> downstream" air-
  // side pattern, applied here to the water side. Each boiler's own inlet
  // flow is a directly commanded MassFlowSource_T (not a pressure-driven
  // splitter/mixer network) -- deliberately avoiding a full hydronic
  // pressure network per this model's own scope (see Documentation:
  // "Known simplifications"). The two boiler outlets combine in a real
  // MixingVolume, so THotWatSup is a genuine flow-weighted energy balance
  // of both boilers' actual outputs, not a hand-computed average.
  // ---------------------------------------------------------------------------
  Buildings.Fluid.Sources.MassFlowSource_T boi1In(
    redeclare package Medium = MediumW,
    use_m_flow_in=true,
    use_T_in=true,
    nPorts=1)
    "Return water routed to boiler 1";

  Buildings.Fluid.Sources.MassFlowSource_T boi2In(
    redeclare package Medium = MediumW,
    use_m_flow_in=true,
    use_T_in=true,
    nPorts=1)
    "Return water routed to boiler 2";

  Buildings.Fluid.MixingVolumes.MixingVolume mix(
    redeclare package Medium = MediumW,
    V=0.5,
    nPorts=3,
    m_flow_nominal=rhoWat*VHotWat_flow_nominal,
    energyDynamics=Modelica.Fluid.Types.Dynamics.FixedInitial,
    T_start=333.15)
    "Plant supply header -- combines both boilers' outlets";

  Buildings.Fluid.Sensors.TemperatureTwoPort senTHotWatSup(
    redeclare package Medium = MediumW,
    m_flow_nominal=rhoWat*VHotWat_flow_nominal,
    tau=10)
    "Plant hot-water supply temperature sensor";

  Buildings.Fluid.Sources.Boundary_pT sinPlant(
    redeclare package Medium = MediumW,
    p=101325,
    nPorts=1)
    "Idealized downstream distribution-system pressure boundary
    (sole pressure reference for this fluid network, matching AHU.mo's
    own single-sink pattern -- no plant/distribution pump curve or
    hydraulic decoupler is modeled)";

equation
  // ---------------------------------------------------------------------------
  // Flow, staging, and pump interlock
  // ---------------------------------------------------------------------------
  mHotWat_flow = rhoWat * max(0, VHotWat_flow);

  nBoiEnabled = (if uBoi1 then 1 else 0) + (if uBoi2 then 1 else 0);
  nPumOn = (if uPum1 then 1 else 0) + (if uPum2 then 1 else 0);

  // Route flow through enabled boilers only, splitting evenly when both are
  // enabled ("load sharing", no lead/lag rotation yet -- see Documentation).
  // If neither boiler is enabled but a pump is running, still split flow
  // through both boiler legs (with y=0 firing) so water physically passes
  // through the plant unchanged rather than stagnating in an isolated leg --
  // this is what makes THotWatSup approach THotWatRet with boilers off but
  // pumps on (a required behavior), via real energy-balance physics rather
  // than a special-cased override.
  nBoiRouted = if nBoiEnabled > 0 then nBoiEnabled else 2;
  mBoi1_flow_cmd = if nPumOn > 0 and (uBoi1 or nBoiEnabled == 0) then mHotWat_flow/nBoiRouted else 0;
  mBoi2_flow_cmd = if nPumOn > 0 and (uBoi2 or nBoiEnabled == 0) then mHotWat_flow/nBoiRouted else 0;

  boi1In.m_flow_in = mBoi1_flow_cmd;
  boi2In.m_flow_in = mBoi2_flow_cmd;
  boi1In.T_in = THotWatRet;
  boi2In.T_in = THotWatRet;

  // ---------------------------------------------------------------------------
  // Outdoor-air hot-water supply reset
  // ---------------------------------------------------------------------------
  resetFra = max(0, min(1,
    (TOutResetHigh - TOut) / max(0.1, TOutResetHigh - TOutResetLow)));

  THotWatSupSet =
    if not haveOutdoorReset then
      THotWatSupSetFixed
    else
      THotWatSupSetMin + (THotWatSupSetMax - THotWatSupSetMin) * resetFra;

  // ---------------------------------------------------------------------------
  // Capacity-limited feed-forward load target (section 11 of the task spec)
  // -- drives y1/y2, which the REAL boiler physics then turns into actual
  // delivered heat. QHeatDelivered/plantPLR below are read back from that
  // real physics, not from this feed-forward calculation directly.
  // ---------------------------------------------------------------------------
  QReq = mHotWat_flow * cpWat * max(0, THotWatSupSet - THotWatRet);
  availableHeatingCapacity = nBoiEnabled * QBoi_nominal;

  plantPLRTarget =
    if availableHeatingCapacity > 1e-6 then
      min(1, QReq/availableHeatingCapacity)
    else
      0;

  // Capacity limiting and the condensing bonus: see Documentation. eta can
  // exceed eta_nominal when THotWatRet is cool enough, and since fuel input
  // (QFue_flow) is normalized by eta_nominal, firing at y=1 could otherwise
  // deliver more than the nameplate QBoi_nominal. yCap keeps that from
  // happening while leaving y unrestricted (yCap=1) whenever the current
  // return temperature gives no condensing bonus.
  etaTemperatureBonus = eta_nominal + (eta_max - eta_nominal) * max(0, min(1,
    (TCondensingHigh - THotWatRet) / max(1, TCondensingHigh - TCondensingLow)));
  yCap = if etaTemperatureBonus > eta_nominal then eta_nominal/etaTemperatureBonus else 1;

  // Boiler firing command: gated by both this boiler's own enable AND plant
  // pump status (section 12, pump interlock) -- a boiler must never fire
  // into a leg with no circulation.
  y1 = if uBoi1 and nPumOn > 0 then min(plantPLRTarget, yCap) else 0;
  y2 = if uBoi2 and nPumOn > 0 then min(plantPLRTarget, yCap) else 0;

  boiler1.y = y1;
  boiler2.y = y2;
  boiler1.TRetBoiler = THotWatRet;
  boiler2.TRetBoiler = THotWatRet;

  // ---------------------------------------------------------------------------
  // Real-physics outputs
  // ---------------------------------------------------------------------------
  QBoi1 = boiler1.QWat_flow;
  QBoi2 = boiler2.QWat_flow;
  QHeatDelivered = QBoi1 + QBoi2;

  PBoi1 = boiler1.QFue_flow;
  PBoi2 = boiler2.QFue_flow;

  etaBoi1 = boiler1.eta;
  etaBoi2 = boiler2.eta;

  plantPLR =
    if availableHeatingCapacity > 1e-6 then
      max(0, min(1, QHeatDelivered/availableHeatingCapacity))
    else
      0;

  // ---------------------------------------------------------------------------
  // Supply temperature -- low-flow numerical safety (section 14)
  // ---------------------------------------------------------------------------
  THotWatSup =
    if mHotWat_flow > mHotWat_flow_min then
      senTHotWatSup.T
    else
      THotWatRet;

  // ---------------------------------------------------------------------------
  // Pumps / differential pressure
  // ---------------------------------------------------------------------------
  flowFraction = if VHotWat_flow_nominal > 0 then max(0, VHotWat_flow)/VHotWat_flow_nominal else 0;

  dpHotWat = if nPumOn > 0 then dpHotWat_nominal * flowFraction^2 / nPumOn else 0;

  PPum1 = if uPum1 then PPum_nominal * flowFraction^3 else 0;
  PPum2 = if uPum2 then PPum_nominal * flowFraction^3 else 0;

  // ---------------------------------------------------------------------------
  // Fluid connections
  // ---------------------------------------------------------------------------
  connect(boi1In.ports[1], boiler1.port_a);
  connect(boi2In.ports[1], boiler2.port_a);
  connect(boiler1.port_b, mix.ports[1]);
  connect(boiler2.port_b, mix.ports[2]);
  connect(mix.ports[3], senTHotWatSup.port_a);
  connect(senTHotWatSup.port_b, sinPlant.ports[1]);

  annotation (
    experiment(
      StartTime=0,
      StopTime=21600,
      Interval=60,
      Tolerance=1e-6),
    Documentation(info="<html>
<p>
Experimental central hot-water plant: two natural-gas condensing-capable
boilers, staged/load-shared, with outdoor-air supply-water reset. This is
the heating-side counterpart to models/ahu/AHU.mo, completing the
architecture
</p>
<pre>
SimpleChillerPlant -&gt; AHU -&gt; VAV -&gt; ThermalZone   (cooling)
BoilerPlant          -&gt; AHU -&gt; VAV -&gt; ThermalZone   (heating, this model)
</pre>
<h4>Selected Buildings component and why</h4>
<p>
Each boiler extends <code>Buildings.Fluid.Boilers.BaseClasses.PartialBoiler</code>
directly, rather than instantiating
<code>Buildings.Fluid.Boilers.BoilerPolynomial</code>. PartialBoiler declares
<code>eta</code> as a bindable <code>input</code> that whichever subclass
extends it is expected to supply with an equation --
<code>BoilerPolynomial</code> itself works exactly this way
(<code>extends PartialBoiler(eta=if effCur==... )</code>). Reusing that same,
library-supported extension point with a custom formula gets the real fluid
dynamics, thermal capacitance, UA jacket-loss structure, and fuel/power
bookkeeping already implemented and validated in PartialBoiler, while
letting the efficiency curve depend on whichever temperature is physically
appropriate.
</p>
<p>
That distinction matters here: BoilerPolynomial's own
<code>EfficiencyCurves.QuadraticLinear</code> option ties efficiency to
<code>T</code>, which PartialBoiler documents as \"the boiler outlet
temperature\" -- i.e. the boiler's own leaving/supply-side water
temperature. Real condensing-boiler efficiency is governed by the
*entering* (return) water temperature, because that is what contacts the
coolest heat-exchanger surface where flue-gas water vapor condenses and
releases latent heat. This model's <code>CondensingBoiler</code> local
class supplies <code>eta</code> as a function of an externally provided
<code>TRetBoiler</code> (bound to the plant's own <code>THotWatRet</code>
input) and the boiler's own part-load ratio <code>y</code>, matching the
physically standard characterization instead of the built-in
leaving-temperature-based option.
</p>
<h4>Efficiency curve</h4>
<pre>
etaTemperature = eta_nominal + (eta_max - eta_nominal) *
    clip01((TCondensingHigh - THotWatRet) / (TCondensingHigh - TCondensingLow))

etaPartLoad    = 1 - kPlrPenalty * (y - yEtaPeak)^2

eta            = clip(etaTemperature * etaPartLoad, eta_min, eta_max)
</pre>
<p>
Return water at/above <code>TCondensingHigh</code> (60 degC default) gets no
condensing bonus (<code>eta -&gt; eta_nominal</code>); at/below
<code>TCondensingLow</code> (30 degC default) gets the full bonus
(<code>eta -&gt; eta_max</code>), linearly interpolated between. Firing rate
enters as a downward quadratic penalty away from <code>yEtaPeak</code>
(default 0.4), a simplified stand-in for the well-known shape of real
part-load efficiency curves (reduced heat-exchanger effectiveness at very
low fire, increased flue losses at high fire) without claiming a specific
manufacturer's curve. The whole expression is clamped to
<code>[eta_min, eta_max]</code> (default 0.75/0.98) regardless of inputs, so
<code>PartialBoiler</code>'s own <code>assert(eta &gt; 0.001, ...)</code>
can never trigger and the fuel-power calculation
(<code>QFue_flow = y*Q_flow_nominal/eta_nominal</code>) never divides by an
unbounded value.
</p>
<h4>Thermal output vs. fuel input power</h4>
<p>
<code>QBoi1</code>/<code>QBoi2</code> (and their sum, <code>QHeatDelivered</code>)
are <b>useful thermal heating output</b>, read directly from each boiler's
own <code>QWat_flow</code> (heat transferred into the water). <code>PBoi1</code>/
<code>PBoi2</code> are <b>fuel input power</b> (<code>QFue_flow</code>, natural
gas, from <code>Buildings.Fluid.Data.Fuels.NaturalGasLowerHeatingValue()</code>),
not electrical power -- deliberately named/documented to avoid the two being
confused, since <code>etaBoi = QBoi/PBoi</code> only makes sense if the
denominator is fuel energy, not electricity. Pump power
(<code>PPum1</code>/<code>PPum2</code>) is the only genuinely electrical
output in this model.
</p>
<h4>Topology / signal-based integration</h4>
<p>
This FMU's inputs are aggregate return-water temperature and flow
(<code>THotWatRet</code>, <code>VHotWat_flow</code>), not fluid ports --
integration with AHU.mo/SimpleVAVZone.mo happens by wiring FMU signals
together at a higher level (e.g. in the runtime or a co-simulation master),
not through a single continuous Modelica fluid network spanning multiple
FMUs. The intended future signal wiring is:
</p>
<pre>
BoilerPlant.THotWatSup  -&gt; AHU heating-coil hot-water supply / VAV reheat-coil supply
AHU/VAV hot-water return -&gt; BoilerPlant.THotWatRet
AHU/VAV hot-water flow   -&gt; BoilerPlant.VHotWat_flow
</pre>
<p>
AHU.mo and SimpleVAVZone.mo are not modified by this model; this is
documentation of the intended future connection only.
</p>
<p>
Internally, each boiler's own inlet is a directly commanded
<code>MassFlowSource_T</code> (return-water temperature and this boiler's
assigned share of total plant flow), not a pressure-driven splitter/mixer
network -- deliberately avoiding a full hydronic pressure network per this
model's scope (see \"Known simplifications\" below). This mirrors the same
\"MassFlowSource_T streams into a shared MixingVolume\" pattern AHU.mo
already uses on its air side, applied here on the water side: both boilers'
real outlet conditions combine in one <code>MixingVolume</code>
(<code>mix</code>), so <code>THotWatSup</code> is a genuine flow-weighted
result of the coupled physics, not a hand-computed average.
</p>
<h4>Staging and load sharing</h4>
<p>
<code>uBoi1</code>/<code>uBoi2</code> gate both flow routing and firing:
flow is routed only to enabled boilers, split evenly when both are enabled.
The plant's feed-forward part-load target
(<code>plantPLRTarget = min(1, QReq/availableHeatingCapacity)</code>, where
<code>QReq = mHotWat_flow*cpWat*max(0,THotWatSupSet-THotWatRet)</code> and
<code>availableHeatingCapacity = nBoiEnabled*QBoi_nominal</code>) is applied
identically to every enabled boiler -- there is no lead/lag rotation or
unequal loading logic yet. <code>QHeatDelivered</code>/<code>plantPLR</code>
are read back from the real boiler outputs after this feed-forward command
passes through actual boiler physics, not reported as the feed-forward
value directly, so the plant-level energy balance
(<code>QHeatDelivered</code> vs. <code>mHotWat_flow*cpWat*(THotWatSup-THotWatRet)</code>)
holds by construction rather than by coincidence. If plant capacity is
insufficient (<code>QReq &gt; availableHeatingCapacity</code>),
<code>THotWatSup &lt; THotWatSupSet</code> emerges naturally from this same
real energy balance -- it is not separately clamped.
</p>
<p>
The nested <code>CondensingBoiler</code> class and the per-boiler
flow/firing signals are structured so that a future lead/lag rotation
scheme only needs to change how <code>plantPLRTarget</code> is split
between <code>y1</code>/<code>y2</code> (e.g. unequal shares, a rotating
\"lead\" assignment) without touching the boiler physics or topology.
</p>
<h4>Capacity limiting and the condensing bonus</h4>
<p>
<code>PartialBoiler</code> normalizes fuel consumption by
<code>eta_nominal</code> regardless of the actual, live <code>eta</code>:
<code>QFue_flow = y*Q_flow_nominal/eta_nominal</code>, so
<code>QWat_flow = eta*QFue_flow = (eta/eta_nominal)*y*Q_flow_nominal</code>.
When the condensing bonus pushes <code>eta</code> above
<code>eta_nominal</code> (cool return water), firing at <code>y=1</code>
would deliver <i>more</i> than the nameplate <code>QBoi_nominal</code> --
physically real for a condensing boiler, but it breaks the plant-level
capacity cap this model is otherwise required to respect
(<code>QHeatDelivered &lt;= availableHeatingCapacity</code>). Rather than
accept that overshoot or introduce an algebraic loop by capping <code>y</code>
from the live <code>eta</code> (which itself depends on <code>y</code>
through the part-load term), <code>yCap</code> is computed from
<code>THotWatRet</code> alone, using only the temperature-driven portion of
the efficiency bonus (<code>etaTemperatureBonus</code>, the same formula
without the part-load correction): <code>yCap = eta_nominal/etaTemperatureBonus</code>
when that exceeds 1, else <code>yCap = 1</code>. Applying
<code>min(plantPLRTarget, yCap)</code> to <code>y1</code>/<code>y2</code>
keeps each boiler's real output bounded near its nameplate capacity under
favorable (condensing) return conditions, while leaving firing completely
unrestricted (<code>yCap=1</code>) whenever the current return temperature
gives no condensing bonus at all -- i.e. normal, non-condensing operation
can still reach full nameplate output at <code>y=1</code>.
</p>
<h4>Pump interlock and boiler-off behavior</h4>
<p>
<code>y1</code>/<code>y2</code> are forced to zero whenever no pump is
running (<code>nPumOn=0</code>), regardless of boiler enable state --
a boiler must never fire into a stagnant, non-circulating leg. With both
boilers disabled but a pump running, flow is still routed through both
(evenly split) with zero firing, so the water passes through unchanged and
<code>THotWatSup</code> naturally approaches <code>THotWatRet</code> through
the same real energy balance, not a special-cased override.
</p>
<h4>Known simplifications</h4>
<ul>
<li>
No full hydronic pressure network: each boiler's flow is a directly
commanded mass flow rather than emerging from a real splitter/pump/valve
pressure network. <code>dpHotWat</code>/<code>PPum1</code>/<code>PPum2</code>
are simplified affinity-law approximations
(<code>dpHotWat_nominal*flowFraction^2/nPumOn</code>,
<code>PPum_nominal*flowFraction^3</code> per running pump), analogous to
the approach already used in <code>models/chiller/SimpleChillerPlant.mo</code>
and <code>models/ahu/AHU.mo</code>'s own fan-power law.
</li>
<li>
No lead/lag runtime rotation -- load is always split evenly across
whichever boilers are currently enabled. See \"Staging and load sharing\"
above for how a future rotation scheme would extend this.
</li>
<li>
No real gas combustion, flue-stack dynamics, or burner-flame dynamics --
fuel consumption is the algebraic <code>QFue_flow = y*Q_flow_nominal/eta_nominal</code>
already implemented in <code>PartialBoiler</code>.
</li>
<li>
No detailed pump curves, expansion tank, air separator, hydraulic
decoupler, primary/secondary loops, three-way valves, or thermal storage.
</li>
<li>
No district-heating interconnection.
</li>
<li>
Each boiler's <code>heatPort</code> (jacket heat loss to ambient) is left
unconnected, exactly as in Buildings' own
<code>Buildings.Fluid.Boilers.Examples.BoilerPolynomialClosedLoop</code>
reference example -- this makes each boiler adiabatic to its surroundings
(no jacket loss) rather than requiring an arbitrary boiler-room ambient
temperature assumption.
</li>
</ul>
<p>
Inputs: THotWatRet, TOut, VHotWat_flow, uBoi1, uBoi2, uPum1, uPum2.
</p>
<p>
Outputs: THotWatSup, THotWatSupSet, dpHotWat, QBoi1, QBoi2, QHeatDelivered,
PBoi1, PBoi2, etaBoi1, etaBoi2, PPum1, PPum2, plantPLR, flowFraction,
availableHeatingCapacity.
</p>
</html>"));
end BoilerPlant;
