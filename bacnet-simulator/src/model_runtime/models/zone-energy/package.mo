within ;
package ZoneEnergy
  "EnergyPlus/Spawn-backed thermal zone reference model and its bundled IDF/weather resources"
annotation (
  uses(Modelica(version="4.1.0")),
  Documentation(info="<html>
<p>Package wrapper for EnergyPlusThermalZone.mo, required so that its
<code>Modelica.Utilities.Files.loadResource(\"modelica://ZoneEnergy/Resources/...\")</code>
calls can resolve the bundled IDF and EPW/MOS weather files at
translation time, matching the Buildings library's own convention for
EnergyPlus_24_2_0-coupled models.</p>
</html>"));
end ZoneEnergy;
