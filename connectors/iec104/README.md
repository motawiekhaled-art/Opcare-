# IEC 60870-5-104 master connector (`iec104`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.5. Reads RTUs, substation gateways, protection relays and
SCADA front ends in power grids, renewables, water and oil & gas (ABB RTU500, Siemens SICAM, GE D20/D400, Schneider
Saitel/Easergy, Hitachi Energy, Efacec, NARI, NR Electric, Triangle MicroWorks and Kalkitech gateways ...) over
IEC 104, with TLS (IEC 62351-3) and an optional redundant second link.

**Opcare's own IEC 104 stack**, standard library only. No IEC 104 library is used in the product or its image
(lib60870, j60870 and c104 are all GPL), so this connector lives in the main repository (spec 2 rule 7, draft.4).

| File | Content |
|---|---|
| `apci.py` | APDU framing, I/S/U control fields, 15-bit sequence numbers (IEC 60870-5-104 section 5) |
| `asdu.py` | ASDU header (2-octet COT, 2-octet CA, 3-octet IOA, SQ), the monitor types of spec 15.5, CP56Time2a in the station's time zone, quality descriptors; builders for the three allowed requests only |
| `client.py` | Asyncio controlling station: STARTDT, k / w flow control, t1 / t2 / t3, general and counter interrogation, read command; the guard checks every frame |
| `connector.py` | The connector: interrogation on connect and periodically, spontaneous data with time tags, counters, end of initialisation, redundant host, browse, read_now, test_connection |

- **Read-only (spec 12 and 15.5):** the only I-frames are general interrogation, counter interrogation with
  FRZ = 0 (read the counters; freeze or reset is blocked as `C_CI_NA_1:FREEZE`) and the read command. There is no
  code for commands, set-points, clock synchronisation, reset, parameters or file transfer. Tests check the stations
  receive nothing else.
- **Time tags:** CP56Time2a carries the station's local time; `connection.device_time_zone` (IANA name, default
  `UTC`) converts it, and the SU bit settles the repeated hour in autumn. An invalid time tag falls back to receive
  time with flag `CLOCK`.
- **Quality:** IV, NT, OV, BL, SB (and the counter IV/CA/CY bits) map to platform quality and flags; ASDUs sent in
  test mode get flag `TEST`. A point the station does not return to interrogation shows `NOT_IN_STATION` until a value
  for it arrives (periodic-only objects, for example).
- **Browse** runs general and counter interrogation and lists every object by common address and type, ready to add
  as tags with the right address.

Tests: `python -m pytest connectors/iec104/tests -q` — APCI/ASDU unit tests and end-to-end runs against
`tools/simulators/iec104_station.py` (our stack in the station role) over TCP and TLS: interrogation, spontaneous
data, quality, time zone, counters, restart, link loss, redundant host, a silent station (TESTFR), and flow control
with 600 I-frames. The CI job *iec104-interop* also runs them against lib60870's unmodified example server
(`cs104_server`, GPLv3): CI downloads and builds it from the upstream sources at test time; no lib60870 code is in this
repository or in any image.
