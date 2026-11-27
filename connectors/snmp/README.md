# SNMP manager connector (`snmp`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.9. Polls IT and network equipment — switches, routers,
firewalls, servers (HOST-RESOURCES-MIB), UPSs (UPS-MIB, RFC 1628), PDUs, industrial Ethernet switches.

- **Versions:** v1, v2c (community kept as a secret file) and v3 USM: authentication MD5, SHA, SHA-224/256/384/512
  (RFC 7860), privacy AES-128 (RFC 3826). Our own BER/USM code (`ber.py`, `messages.py`); `cryptography` only for AES.
- **Read-only:** GetRequest, GetNextRequest, GetBulkRequest. SetRequest cannot even be encoded. The only other packet
  ever sent is the Response that acknowledges a received InformRequest.
- **Polling:** OIDs of a device are grouped into few requests; `tooBig` halves them, an error isolates the bad OID.
  `transform: "rate"` turns counters into per-second values (traffic in Mbit/s with scale 0.000008).
- **Traps:** v1/v2c traps and informs become `snmp.trap` events (linkDown, UPS on battery…), when `traps` is set.
- **Browse:** walks a MIB subtree with readable names (`ifOperStatus.3`); the configuration screen adds tags from it
  and from templates (device health, network port, UPS, server).

Tests: `python -m pytest connectors/snmp/tests -q` — RFC 3414 key test vectors, BER, and end-to-end runs against
`tools/simulators/snmp_sim.py`. CI also runs `tests/test_netsnmp_interop.py` against the real net-snmp agent.
