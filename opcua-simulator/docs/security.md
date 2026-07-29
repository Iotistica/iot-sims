# Security

The simulator has two independent security surfaces: the OPC UA server
(port `OPCUA_PORT`, default 4840) and the admin REST/WebSocket API
(port `SIM_API_PORT`, default 47901, also serves the admin SPA). Both are
controlled entirely by environment variables — no code changes needed to
move between postures.

## Default posture (out of the box)

- **OPC UA**: two endpoints are offered side by side — `NoSecurity` and
  `Basic256Sha256_SignAndEncrypt`. Anonymous sessions are allowed. A
  self-signed server certificate is auto-generated on first boot into
  `${DATA_DIR}/pki/opcua/` and reused (regenerated only if missing or
  expired). The server does **not** validate client certificates (any
  self-signed cert, including node-opcua's own default, is accepted) — this
  is a deliberate simplification appropriate for a simulator, not a
  hardened PKI trust store.
- **Admin API**: plain HTTP.

This keeps existing clients (e.g. the `iot-agent` OPC-UA source currently
configured with `securityMode: 'None'`) and existing HTTP-based admin access
working unchanged.

## Hardened posture (recommended for Azure / internet-facing hosting)

Set these environment variables:

| Variable | Value | Effect |
|---|---|---|
| `OPCUA_SECURITY_POLICIES` | `Basic256Sha256_SignAndEncrypt` | Drops the `NoSecurity` endpoint entirely — every OPC UA session is encrypted. |
| `OPCUA_REQUIRE_AUTH` | `true` | Drops the Anonymous identity token — every OPC UA session must present a valid username/password. |
| `ADMIN_TLS_ENABLED` | `true` | Admin API serves HTTPS instead of HTTP on `SIM_API_PORT` (self-signed cert auto-generated into `${DATA_DIR}/pki/admin/`, or supply your own via `ADMIN_TLS_CERT_FILE`/`ADMIN_TLS_KEY_FILE`). |

For a cloud-hosted simulator, network-level restriction (Azure NSG rules /
private endpoint / VPN limiting who can reach `OPCUA_PORT` and
`SIM_API_PORT` at all) is the highest-leverage control and should be used
*in addition to* the above, not instead of it. A full OPC UA certificate
trust store (approve/reject individual client certs, like commercial tools
such as Prosys expose) was evaluated and deliberately left out of this pass
— redundant with network restriction + username/password auth for a
simulator with a small number of known clients; revisit if the OPC UA port
ends up open to arbitrary/unknown clients.

## OPC UA authentication

`OPCUA_REQUIRE_AUTH=true` reuses the **same `users` table** that backs admin
web UI login (`lib/db.py`) — there is no separate OPC UA credential store.
Any admin user's username/password works as an OPC UA `UserNameIdentityToken`.

## Certificate/key locations

| Path | Contents |
|---|---|
| `${DATA_DIR}/pki/opcua/server_private_key.pem` | OPC UA server private key (PEM) |
| `${DATA_DIR}/pki/opcua/server_certificate.der` | OPC UA server certificate (DER) |
| `${DATA_DIR}/pki/admin/admin_key.pem` | Admin API TLS private key (PEM) |
| `${DATA_DIR}/pki/admin/admin_cert.pem` | Admin API TLS certificate (PEM) |

All auto-generated the first time they're needed and persist as long as
`DATA_DIR` is a mounted volume (already the case today). Delete a
cert/key pair to force regeneration.

## Config reference

```
OPCUA_SECURITY_POLICIES=NoSecurity,Basic256Sha256_SignAndEncrypt   # comma list of ua.SecurityPolicyType names
OPCUA_REQUIRE_AUTH=false                                            # true disables Anonymous OPC UA sessions
OPCUA_APP_URI=urn:iotistica:opcua-simulator                         # OPC UA server certificate application URI
OPCUA_CERT_DIR=${DATA_DIR}/pki/opcua

ADMIN_TLS_ENABLED=false                                             # true switches the admin API from HTTP to HTTPS
ADMIN_TLS_CERT_DIR=${DATA_DIR}/pki/admin
ADMIN_TLS_CERT_FILE=                                                # optional: use a real cert instead of auto-generating
ADMIN_TLS_KEY_FILE=
```

## Agent-side follow-up (not part of this change)

If `OPCUA_REQUIRE_AUTH` or a `NoSecurity`-free `OPCUA_SECURITY_POLICIES` is
turned on, any `iot-agent` device pointed at this simulator must have its
per-device `securityMode`/`securityPolicy`/`certificateTrustMode` (and, once
auth is required, credentials) updated to match — otherwise it will fail to
connect. The agent's OPC UA client already supports all of this
(`src/plugins/opcua/client.ts`); only the per-device connection config needs
updating, no agent code changes.
