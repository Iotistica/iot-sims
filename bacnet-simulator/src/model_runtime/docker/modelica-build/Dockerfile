# BASE_IMAGE must be declared before FROM to parameterize it (Dockerfile
# "global ARG" syntax). Default matches the published iotistic/modelica-
# build image exactly -- build-modelica-build-image.yml never overrides
# this, so that image is unaffected. Overriding it (e.g. for a one-off
# local diagnostic build against a different OMC version) is opt-in via
# --build-arg BASE_IMAGE=....
ARG BASE_IMAGE=openmodelica/openmodelica:v1.27.0-minimal
FROM ${BASE_IMAGE}

ARG BUILDINGS_VERSION=13.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        make \
        python3 \
        tar \
        unzip \
        zip \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /build/libraries /tmp/buildings \
    && curl -fsSL "https://github.com/lbl-srg/modelica-buildings/archive/refs/tags/v${BUILDINGS_VERSION}.tar.gz" -o /tmp/buildings.tar.gz \
    && tar -xzf /tmp/buildings.tar.gz -C /tmp/buildings --strip-components=1 \
    && cp -R /tmp/buildings/Buildings /build/libraries/Buildings \
    && test -f /build/libraries/Buildings/package.mo \
    && rm -rf /tmp/buildings /tmp/buildings.tar.gz

# Spawn/EnergyPlus support (Buildings.ThermalZones.EnergyPlus_*) needs the
# actual spawn binary, which is NOT part of the Buildings library source
# tree/release archive -- it's fetched separately by this script, which
# also rewrites the library's own .mo files (Building.mo/package.mo/
# UsersGuide.mo) to reference the real downloaded executable name instead
# of a placeholder. Skipping this step is why models using
# EnergyPlus_24_2_0 previously failed with "Failed to find spawn
# executable" (both in checkModel/simulate() and, most likely, in
# buildModelFMU's own silent failure for such models).
RUN python3 /build/libraries/Buildings/Resources/src/ThermalZones/install.py --binaries-for-os-only \
    && find /build/libraries/Buildings/Resources/bin -maxdepth 3 -iname "spawn-*" -exec chmod +x {} \;

WORKDIR /work
