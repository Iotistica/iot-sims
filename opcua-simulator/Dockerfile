# Stage 1: build the Vue admin
FROM node:20-slim AS ui-builder
WORKDIR /ui
COPY admin/package*.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.11-slim

WORKDIR /app

# Install build dependencies for ARM platforms
# gcc, g++, make: C/C++ compiler toolchain
# libffi-dev: Foreign Function Interface library headers
# python3-dev: Python development headers
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        make \
        libffi-dev \
        python3-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy simulator files (build context is sensors/opcua-simulator)
COPY opcua_simulator.py .
COPY lib/ ./lib/
COPY --from=ui-builder /ui/dist ./admin/dist

RUN mkdir -p /data

# OPC UA endpoint + the management REST/WS API (same port serves the admin UI)
EXPOSE 4840/tcp
EXPOSE 47901/tcp

ENV DATA_DIR=/data
ENV SIM_API_PORT=47901
ENV OPCUA_PORT=4840

# Run simulator (single process: OPC UA server + REST/WS management API + admin UI)
CMD ["python", "-u", "opcua_simulator.py"]
