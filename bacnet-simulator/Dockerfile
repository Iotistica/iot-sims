FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bacnet_simulator.py .

RUN mkdir -p /data

# BACnet UDP + management REST API
EXPOSE 47808/udp
EXPOSE 47900/tcp

ENV DATA_DIR=/data
ENV SIM_API_PORT=47900
ENV BACNET_PORT=47808

CMD ["python", "-u", "bacnet_simulator.py"]
