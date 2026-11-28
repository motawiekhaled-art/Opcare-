# OPC UA client connector (`opcua`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.2. Reads PLCs, DCS, SCADA and gateways that expose an
OPC UA server: Siemens S7-1500 / S7-1200, Rockwell through FactoryTalk Linx Gateway, Beckhoff, B&R, Omron, ABB 800xA,
Emerson DeltaV, Honeywell Experion, Yokogawa CENTUM, Kepware, Ignition, Matrikon and machines with companion specs.

**Opcare's own OPC UA stack.** No OPC UA library is used in the product or its image; `cryptography` provides only the
RSA / AES / HMAC primitives.

| File | Content |
|---|---|
| `uabin.py` | UA Binary encoding (IEC 62541-6 section 5): built-in types, NodeId text forms (`ns=`, `nsu=`), messages of the read-only services |
| `security.py` | Security policies None, Basic256Sha256, Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss; P_SHA256 keys; application certificates |
| `channel.py` | UA-TCP Hello/Acknowledge/Error, secure channel chunks (OPN asymmetric, MSG/CLO symmetric), padding, signatures, chunking |
| `client.py` | Asyncio client: discovery, secure channel with token renewal, session (anonymous, user name with encrypted password, user certificate), Read, Browse/BrowseNext, subscriptions |
| `connector.py` | The connector: subscribe or poll mode, quality and timestamps per spec 15.2, browse, read_now, test_connection |

- **Read-only (spec 12):** Write, Call, HistoryUpdate and node management have no encoder at all, and every request
  passes the guard (`ALLOW["opcua"]`). Tests check the server never receives anything else.
- **Server certificate trust:** a secure endpoint is used only when its certificate thumbprint is in
  `trusted_server_thumbprints`. *Test connection* shows the server certificate (subject, application URI, expiry,
  thumbprint) so an engineer can compare and trust it. `insecure_skip_verify` exists for testing only.
- **Client certificate:** the platform creates it (`POST /api/opcua-certificate`) and keeps the private key as a
  secret; the GUI offers the certificate as .der / .pem to add to the server's trusted clients.
- Node IDs from browse are saved with the namespace URI (`nsu=`), which survives server restarts and reordering.

Tests: `python -m pytest connectors/opcua/tests -q` — codec and channel unit tests for every policy (including 4096-bit
keys and tamper detection), and end-to-end runs against `tools/simulators/opcua_server.py` (our stack in the server
role). The CI job *opcua-interop* also runs against an independent server (the open-source asyncua package, installed
only in that test job).
