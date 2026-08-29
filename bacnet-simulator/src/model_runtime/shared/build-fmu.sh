#!/bin/sh
set -eu

modelica_file="${MODELICA_FILE:-SimpleVAVZone.mo}"
modelica_model="${MODELICA_MODEL:-SimpleVAVZone}"
fmu_type="${FMU_TYPE:-me_cs}"
fmu_output_name="${FMU_OUTPUT_NAME:-SimpleVAVZone.fmu}"

modelica_path="/build/${modelica_file}"
buildings_path="/build/libraries/Buildings/package.mo"

echo "=========================================="
echo "Modelica FMU build configuration"
echo "=========================================="
echo "Modelica file : ${modelica_path}"
echo "Modelica model: ${modelica_model}"
echo "FMU type      : ${fmu_type}"
echo "FMU output    : ${fmu_output_name}"
echo "Buildings     : ${buildings_path}"
echo "=========================================="

if [ ! -f "${modelica_path}" ]; then
    echo "ERROR: Modelica file not found:"
    echo "  ${modelica_path}"
    exit 1
fi

if [ ! -f "${buildings_path}" ]; then
    echo "ERROR: Buildings library package.mo not found:"
    echo "  ${buildings_path}"
    exit 1
fi

cat > /build/export.mos <<EOF
print("========== Loading Modelica ==========\\n");

installPackage(Modelica, "4.1.0", exactMatch=false);
print(getErrorString());

loadModel(Modelica, {"4.1.0"});
print(getErrorString());

print("========== Loading Buildings ==========\\n");

loadFile("${buildings_path}");
print(getErrorString());

print("========== Loading user model ==========\\n");

loadFile("${modelica_path}");
print(getErrorString());

print("========== Checking model ==========\\n");

checkModel(${modelica_model});
print(getErrorString());

print("========== Exporting FMU ==========\\n");

buildModelFMU(
    ${modelica_model},
    version="2.0",
    fmuType="${fmu_type}",
    fileNamePrefix="${modelica_model}"
);

print(getErrorString());

exit(0);
EOF

echo "========== OpenModelica export script =========="
cat /build/export.mos

echo "========== Running OpenModelica =========="

cd /build

omc /build/export.mos > /build/omc-export.log 2>&1 || true

cat /build/omc-export.log

echo "========== Searching for FMU =========="

fmu_path="$(find /build -maxdepth 5 -type f -iname '*.fmu' | head -n 1 || true)"

if [ -z "${fmu_path}" ]; then
    makefile_path="$(find /build -maxdepth 2 -type f -name '*_FMU.makefile' | head -n 1 || true)"

    if [ -n "${makefile_path}" ]; then
        echo "No FMU found yet."
        echo "Running generated FMU makefile:"
        echo "  ${makefile_path}"

        make -f "${makefile_path}" \
            > /build/fmu-make.log 2>&1 || true

        cat /build/fmu-make.log

        fmu_path="$(find /build -maxdepth 5 -type f -iname '*.fmu' | head -n 1 || true)"
    fi
fi

if [ -z "${fmu_path}" ]; then
    echo ""
    echo "ERROR: OpenModelica did not generate an FMU."
    echo "Model: ${modelica_model}"
    echo ""
    echo "Build with:"
    echo "  docker build --no-cache --progress=plain ."
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

