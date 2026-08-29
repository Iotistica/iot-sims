# Combined runtime image: BACnet simulator + model runtime (FMU/Modelica, PyFMI), as two fully
# separate OS processes/Python environments in one container. Built entirely from this one repo --
# no other repo's image needs to exist first. See C:\Users\Dan\.claude\plans\linked-cooking-sloth.md
# for the full design/rationale.
#
# src/model_runtime/ (moved from the former iot-models repo, layout unchanged internally) keeps
# using iot-models' own conda base environment (pyfmi, fastapi, uvicorn, etc.) -- unchanged in
# substance from its own former Dockerfile, just relocated. It lives under src/ purely for a
# cleaner on-disk tree; it is NOT part of bacnet-simulator's own src/ Python package (explicitly
# excluded from that COPY below). The BACnet simulator gets its own dedicated, explicitly
# Python-3.11-pinned conda env alongside it -- never merged into the same interpreter/event loop as
# the model runtime's synchronous PyFMI calls.

# ---- spawn-source: published external image, still not iot-models -- unrelated to this move ----
FROM iotistic/modelica-build:om-1.27.0-buildings-13.0.0 AS spawn-source

# ---- ui-builder: unchanged from this repo's own previous Dockerfile ----
FROM node:20-slim AS ui-builder
WORKDIR /ui
COPY admin/package*.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build

# ---- model-runtime: the former iot-models Dockerfile's conda/runtime setup, relocated -----------
# Named stage so CI can `docker build --target model-runtime` to build/test just this half, without
# building the bacnet-sim env or the Node UI (mirrors the former iot-models-runtime:ci pattern).
FROM continuumio/miniconda3:latest AS model-runtime
WORKDIR /app/model_runtime

ENV SPAWNPATH=/app/model_runtime/spawn/Spawn-light-0.6.0-9f1b36b00b/linux64/bin
ENV PATH="${SPAWNPATH}:${PATH}"
ENV FMU_MODELS_ROOT=/app/model_runtime/models
ENV RUNTIME_DATA_DIR=/app/model_runtime/data
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# EnergyPlusThermalZone.fmu (Spawn/EnergyPlus-coupled) calls out to an external Spawn/EnergyPlus
# binary at runtime -- pulled out of the published modelica-build image rather than installing the
# full Buildings Modelica source into this image.
COPY --from=spawn-source /build/libraries/Buildings/Resources/bin/Spawn-light-0.6.0-9f1b36b00b \
     /app/model_runtime/spawn/Spawn-light-0.6.0-9f1b36b00b
RUN chmod +x /app/model_runtime/spawn/Spawn-light-0.6.0-9f1b36b00b/linux64/bin/*

RUN conda config --add channels conda-forge \
    && conda config --set channel_priority strict \
    && conda install -y \
        pyfmi \
        fastapi \
        uvicorn \
        pydantic \
        requests \
        numpy \
    && conda clean -afy

COPY src/model_runtime/requirements-hebo.txt /app/model_runtime/requirements-hebo.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r /app/model_runtime/requirements-hebo.txt

COPY src/model_runtime/app.py /app/model_runtime/app.py
COPY src/model_runtime/shared /app/model_runtime/shared
COPY src/model_runtime/models /app/model_runtime/models

# EnergyPlusThermalZone.mo references its IDF/EPW/MOS by bare filename -- stage copies at both
# locations the runtime's cwd (/app/model_runtime) and Spawn's own working subdirectory expect.
RUN mkdir -p /app/model_runtime/spawn-EnergyPlusThermalZone \
    && for dest in /app/model_runtime /app/model_runtime/spawn-EnergyPlusThermalZone; do \
        cp models/zone-energy/Resources/idf/RefBldgSmallOfficeNew2004_Toronto.idf "$dest/SmallOffice.idf"; \
        cp models/zone-energy/Resources/weatherdata/CAN_ON_Toronto-Pearson.Intl.AP.716240_CWEC2020.epw "$dest/USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"; \
        cp models/zone-energy/Resources/weatherdata/CAN_ON_Toronto-Pearson.Intl.AP.716240_CWEC2020.mos "$dest/USA_CA_San.Francisco.Intl.AP.724940_TMY3.mos"; \
    done

RUN mkdir -p /app/model_runtime/data

# ---- final: adds the BACnet simulator on top, in its own dedicated conda env ---------------------
FROM model-runtime AS final

RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev \
    && conda create -y -n bacnet-sim python=3.11 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/bacnet-sim/requirements.txt
RUN /opt/conda/envs/bacnet-sim/bin/pip install --no-cache-dir -r /app/bacnet-sim/requirements.txt \
    && conda clean -afy \
    && apt-get purge -y gcc

COPY src/ /app/bacnet-sim/src/
# model_runtime lives under src/ on disk for one clean tree, but it's a separate Python
# environment/process (see header) -- excluded here so it isn't duplicated into bacnet-sim's own
# env; it's already copied into /app/model_runtime above.
RUN rm -rf /app/bacnet-sim/src/model_runtime
COPY --from=ui-builder /ui/dist /app/bacnet-sim/admin/dist
COPY bacnet-vendors.json /app/bacnet-sim/admin/dist/
RUN mkdir -p /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 47808/udp
EXPOSE 47900/tcp
EXPOSE 8000/tcp
ENV DATA_DIR=/data
ENV SIM_API_PORT=47900
ENV BACNET_PORT=47808

ENTRYPOINT ["/entrypoint.sh"]
