# UDP datagram listener connector (`udp`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.8. For devices and applications that **push** UDP telegrams:

| Typical sender | Format in the point address |
|---|---|
| GNSS / GPS receivers, marine and wind instruments | `nmea` — sentence (RMC, GGA, MWV…) and field; `coordinate: true` for lat/long |
| Weather stations, IoT sensors, gateways | `json` (JSON pointer) or `delimited` (CSV / semicolon / tab field) |
| PLC send/receive telegrams (S7 TSEND/TURCV, ControlLogix, TwinCAT…) | `binary` — byte offset, encoding, byte order |
| Switches, firewalls, servers (syslog RFC 5424 / 3164) | `syslog` field, and `connection.syslog` turns messages into events |
| Anything sending one value per line | `raw` |

- **Listens only.** It never sends a datagram: the read-only guard allow-list is empty.
- Only the configured `senders` (IP or CIDR) are accepted; a silent sender turns its tags Bad (24) after `silence_timeout_s`.
- *Listen for datagrams* (test) and *Listen & pick values* (browse) show what really arrives, with ready-made addresses.
- Standard library only. Container: `connectors/udp/Dockerfile` (bind a port ≥ 1024, map 514 → 5514 for syslog).

Tests: `python -m pytest connectors/udp/tests -q` (real UDP sockets on localhost).
