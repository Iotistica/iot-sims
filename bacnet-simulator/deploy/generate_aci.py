#!/usr/bin/env python3
"""Generates aci-deploy.json for az rest --method PUT (ARM REST API body).

The apiVersion and container group name go in the URL, not the body, so this
file contains only location + properties.  Using az rest instead of
'az container create --file' is required to preserve UDP protocol on ports --
the az container CLI command silently converts all ports to TCP.
"""
import json, os, sys

required = ["ACI_NAME", "ACI_DNS_LABEL", "IMAGE", "STORAGE_KEY"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"Missing env vars: {missing}", file=sys.stderr)
    sys.exit(1)

config = {
    "location": "canadacentral",
    "properties": {
        "containers": [{
            "name": "bacnet-sim",
            "properties": {
                "image": os.environ["IMAGE"],
                "resources": {"requests": {"cpu": 1, "memoryInGB": 1.5}},
                "ports": [
                    {"protocol": "TCP", "port": 47900},
                    {"protocol": "UDP", "port": 47808},
                ],
                "environmentVariables": [
                    {"name": "DATA_DIR",       "value": "/data"},
                    {"name": "SIM_API_PORT",   "value": "47900"},
                    {"name": "BACNET_PORT",    "value": "47808"},
                    {"name": "BACPYPES_IFACE", "value": "0.0.0.0"},
                ],
                "volumeMounts": [{"name": "bacnet-data", "mountPath": "/data"}],
            },
        }],
        "volumes": [{
            "name": "bacnet-data",
            "azureFile": {
                "shareName": "bacnet-storage-volume",
                "storageAccountName": "iotistic",
                "storageAccountKey": os.environ["STORAGE_KEY"],
            },
        }],
        "ipAddress": {
            "type": "Public",
            "dnsNameLabel": os.environ["ACI_DNS_LABEL"],
            "ports": [
                {"protocol": "TCP", "port": 47900},
                {"protocol": "UDP", "port": 47808},
            ],
        },
        "osType": "Linux",
        "restartPolicy": "Always",
    },
}

out = os.environ.get("OUTPUT", "aci-deploy.json")
with open(out, "w") as f:
    json.dump(config, f, indent=2)
print(f"Written {out}")
