#!/bin/sh
set -eu

# EnergyPlusThermalZone.mo (verified standalone in OMEdit) now declares
# `within ;` and references its IDF/EPW/MOS by bare filename (idfName=
# "SmallOffice.idf", etc.) instead of the earlier
# Modelica.Utilities.Files.loadResource("modelica://ZoneEnergy/Resources/...")
# call -- so it no longer needs to be loaded inside the ZoneEnergy package
# for resource resolution, and this script no longer copies package.mo.
#
# Those bare filenames aren't shipped by EnergyPlus/Spawn itself -- they're
# just the names EnergyPlusThermalZone.mo happens to reference. This script
# supplies the real bundled small-office/Toronto IDF+EPW+MOS from
# Resources/, copied into OMC's working directory under the exact bare
# names the model expects, so a plain relative-path lookup resolves them.
#
# KNOWN RISK, flagged rather than silently worked around: this repo's IDF
# only defines zones Attic/Core_ZN/Perimeter_ZN_1-4, but
# EnergyPlusThermalZone.mo's zoneName default is currently "Thermal Zone"
# (not "Core_ZN") -- the exact mismatch that caused EnergyPlus to segfault
# during warmup earlier in this model's history (see git log). If this
# build fails with no clear checkModel/buildModelFMU error but the log
# shows EnergyPlus warming up then dying, that mismatch is the first thing
# to check.

fmu_type="${FMU_TYPE:-cs}"
fmu_output_name="${FMU_OUTPUT_NAME:-EnergyPlusThermalZone.fmu}"

package_src="models/zone-energy"
buildings_path="/build/libraries/Buildings/package.mo"

echo "=========================================="
echo "Modelica FMU build configuration (zone-energy)"
echo "=========================================="
echo "Package src   : ${package_src}"
echo "Model         : EnergyPlusThermalZone (standalone, no package wrapper)"
echo "FMU type      : ${fmu_type}"
echo "FMU output    : ${fmu_output_name}"
echo "Buildings     : ${buildings_path}"
echo "=========================================="

if [ ! -f "${buildings_path}" ]; then
    echo "ERROR: Buildings library package.mo not found:"
    echo "  ${buildings_path}"
    exit 1
fi

mkdir -p /build
cp "${package_src}/EnergyPlusThermalZone.mo" /build/EnergyPlusThermalZone.mo

# Real bundled resource files, renamed to the bare filenames
# EnergyPlusThermalZone.mo currently references.
#
# Placed in BOTH locations:
#   /build/                          -- in case anything resolves relative
#                                        to OMC's own working directory.
#   /build/spawn-EnergyPlusThermalZone/ -- confirmed via a real CI run's
#                                        error output to be where Spawn
#                                        actually looks: it creates this
#                                        spawn-<ModelName>/ directory itself
#                                        at runtime (for its own EnergyPlus
#                                        binaries/resources) and resolves
#                                        idfName/epwName/weaName relative to
#                                        it, not to /build/. Pre-creating it
#                                        with the resource files already in
#                                        place, ahead of Spawn's own
#                                        directory setup, is the fix.
resource_dir="/build/spawn-EnergyPlusThermalZone"
mkdir -p "${resource_dir}"

# The bare filenames EnergyPlusThermalZone.mo references (SmallOffice.idf,
# USA_CA_San.Francisco.Intl.AP.724940_TMY3.*) match the naming convention
# of Buildings' own bundled EnergyPlus_24_2_0.Examples.SmallOffice
# reference model -- a real CI run's file listing showed
# libraries/Buildings/ThermalZones/EnergyPlus_24_2_0/Examples/SmallOffice
# present in the image. If Buildings genuinely ships matching IDF/EPW/MOS
# files, they're almost certainly the correct pairing for zoneName's
# current default ("Thermal Zone") -- our own Toronto IDF only defines
# Attic/Core_ZN/Perimeter_ZN_1-4 and segfaults EnergyPlus during warmup
# when zoneName doesn't match a real zone (see prior git history). Prefer
# Buildings' bundled files if found; fall back to our own Toronto ones
# (previously confirmed to at least be found/loaded, even if zoneName
# doesn't match) otherwise.
buildings_idf="$(find /build/libraries/Buildings -iname 'SmallOffice.idf' 2>/dev/null | head -n 1 || true)"
buildings_epw="$(find /build/libraries/Buildings -iname 'USA_CA_San.Francisco*.epw' 2>/dev/null | head -n 1 || true)"
buildings_mos="$(find /build/libraries/Buildings -iname 'USA_CA_San.Francisco*.mos' 2>/dev/null | head -n 1 || true)"

echo "========== Searching Buildings library for bundled SmallOffice resources =========="
echo "IDF: ${buildings_idf:-not found -- falling back to our own Toronto IDF}"
echo "EPW: ${buildings_epw:-not found -- falling back to our own Toronto EPW}"
echo "MOS: ${buildings_mos:-not found -- falling back to our own Toronto MOS}"

for dest in /build "${resource_dir}"; do
    cp "${buildings_idf:-${package_src}/Resources/idf/RefBldgSmallOfficeNew2004_Toronto.idf}" \
        "${dest}/SmallOffice.idf"
    cp "${buildings_epw:-${package_src}/Resources/weatherdata/CAN_ON_Toronto-Pearson.Intl.AP.716240_CWEC2020.epw}" \
        "${dest}/USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
    cp "${buildings_mos:-${package_src}/Resources/weatherdata/CAN_ON_Toronto-Pearson.Intl.AP.716240_CWEC2020.mos}" \
        "${dest}/USA_CA_San.Francisco.Intl.AP.724940_TMY3.mos"
done

cat > /build/export.mos <<EOF
print("========== Loading Modelica ==========\\n");

installPackage(Modelica, "4.1.0", exactMatch=false);
print(getErrorString());

loadModel(Modelica, {"4.1.0"});
print(getErrorString());

print("========== Loading Buildings ==========\\n");

loadFile("${buildings_path}");
print(getErrorString());

print("========== Loading EnergyPlusThermalZone ==========\\n");

loadFile("/build/EnergyPlusThermalZone.mo");
print(getErrorString());

print("========== Checking model ==========\\n");

checkModel(EnergyPlusThermalZone);
print(getErrorString());

print("========== DIAGNOSTIC: simulate EnergyPlusThermalZone ==========\\n");
simulate(EnergyPlusThermalZone, stopTime=3600, numberOfIntervals=60, outputFormat="csv");
print(getErrorString());

print("========== Exporting FMU ==========\\n");

setCommandLineOptions("-d=failtrace");
print(getErrorString());

buildModelFMU(
    EnergyPlusThermalZone,
    version="2.0",
    fmuType="${fmu_type}",
    fileNamePrefix="EnergyPlusThermalZone",
    includeResources=true
);

print(getErrorString());

exit(0);
EOF

echo "========== OpenModelica export script =========="
cat /build/export.mos

echo "========== Running OpenModelica =========="
echo "OMC version: $(omc --version 2>&1 || true)"

cd /build

touch /build/.build-start-marker
omc /build/export.mos > /build/omc-export.log 2>&1 || true

cat /build/omc-export.log

echo "========== Diagnostic results =========="
real_csv="$(find /build -maxdepth 3 -type f -iname 'EnergyPlusThermalZone_res.csv' | head -n 1 || true)"
if [ -s "${real_csv}" ]; then
    echo "EnergyPlusThermalZone simulate(): RESULT FILE HAS DATA (${real_csv}, $(wc -l < "${real_csv}") lines)"
else
    echo "EnergyPlusThermalZone simulate(): NO RESULT DATA${real_csv:+ (empty file at ${real_csv})}"
fi
real_log="$(find /build -maxdepth 3 -type f -iname 'EnergyPlusThermalZone.log' | head -n 1 || true)"
if [ -n "${real_log}" ]; then
    echo "--- ${real_log} (last 15 lines) ---"
    tail -n 15 "${real_log}"
fi

echo "========== Files created/modified during the OMC build =========="
find /build -newer /build/.build-start-marker 2>/dev/null | sort

echo "========== Searching for EnergyPlusThermalZone FMU =========="

fmu_path="$(find /build -maxdepth 5 -type f -iname 'EnergyPlusThermalZone*.fmu' | head -n 1 || true)"

if [ -z "${fmu_path}" ]; then
    makefile_path="$(find /build -maxdepth 3 -type f -name 'EnergyPlusThermalZone*_FMU.makefile' | head -n 1 || true)"

    if [ -n "${makefile_path}" ]; then
        echo "No FMU found yet."
        echo "Running generated FMU makefile:"
        echo "  ${makefile_path}"

        make -f "${makefile_path}" \
            > /build/fmu-make.log 2>&1 || true

        cat /build/fmu-make.log

        fmu_path="$(find /build -maxdepth 5 -type f -iname 'EnergyPlusThermalZone*.fmu' | head -n 1 || true)"
    fi
fi

if [ -z "${fmu_path}" ]; then
    echo ""
    echo "ERROR: OpenModelica did not generate an FMU."
    echo "Model: EnergyPlusThermalZone"
    echo ""
    exit 1
fi

mkdir -p /fmu-out

cp "${fmu_path}" "/fmu-out/${fmu_output_name}"

echo "========== FMU handoff =========="
ls -lh /fmu-out

echo ""
echo "Successfully generated:"
echo "  /fmu-out/${fmu_output_name}"
