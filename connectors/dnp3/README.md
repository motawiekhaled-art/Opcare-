# DNP3 master connector (`dnp3`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.6. Reads RTUs, protection relays, IEDs and gateways in
substations, water networks and pipelines (SEL, GE, ABB, Siemens SICAM, Schneider SCADAPack, Emerson ROC, NovaTech,
Survalent, SCADA Data Gateway ...) over DNP3 TCP or secure DNP3 (TLS, IEC 62351-3).

**Opcare's own DNP3 stack**, standard library only. No DNP3 library is used in the product or its image.

| File | Content |
|---|---|
| `link.py` | Data link layer (0x0564 frames, CRC-16/DNP per 16-byte block) and transport layer (FIR/FIN/sequence segments) |
| `app.py` | Application layer: fragments, object headers and qualifiers, decoding of groups 1-4, 10, 11, 20-23, 30, 32, 40, 42 (and CTO 51 for relative times); request builders only for what the spec allows; flags to quality |
| `master.py` | Asyncio master: one solicited request at a time, multi-fragment responses with confirms, unsolicited responses with confirms, link-status keep-alive, TLS |
| `connector.py` | The connector: integrity poll on connect / periodically / after a restart, class 1/2/3 polls or unsolicited reports, event timestamps, browse (class 0), read_now, test_connection |

- **Read-only (spec 12 and 15.6):** the stack can only build CONFIRM, READ, ENABLE/DISABLE UNSOLICITED and the WRITE
  that clears IIN1.7 (device restarted). There is no code for controls (select/operate), freeze, restart, file
  services or time synchronisation; every frame passes the guard (`ALLOW["dnp3"]`), and tests check that outstations
  never receive anything else. NEED_TIME is reported as `device.time_invalid`, never answered with a time write.
- **Events:** every event in the outstation's buffer is published, in order, with the outstation's own timestamp.
  IIN2.3 (buffer overflow) is reported as `device.events_lost`.
- **One connection:** most outstations accept a single master. Test connection, browse and read_now reuse the
  running association instead of opening a second one.

Tests: `python -m pytest connectors/dnp3/tests -q` — link, transport and object round-trips, guard checks, and
end-to-end runs against `tools/simulators/dnp3_outstation.py` (our stack in the outstation role, TCP and TLS). The CI
job *dnp3-interop* also runs them against opendnp3 (Apache-2.0, through `dnp3-python`, installed only in that job;
`tools/interop/opendnp3_outstation.py`).
