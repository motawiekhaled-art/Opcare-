# IEC 61850 MMS client connector (`iec61850`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.4. Reads protection relays, bay controllers, station
computers and gateways in substations and plants (SEL, Siemens SIPROTEC, ABB Relion, GE UR/Multilin, Schneider
Easergy MiCOM, NR Electric ...) over MMS (IEC 61850-8-1), with TLS (IEC 62351-4) and ACSE password if the IED asks.

**Opcare's own MMS stack**, standard library only. No IEC 61850 or MMS library is used in the product or its image.

| File | Content |
|---|---|
| `ber.py` | ASN.1 BER (X.690): tags of any class and number, lengths, OIDs, bit strings |
| `iso.py` | TPKT (RFC 1006), COTP class 0, session kernel (ISO 8327), presentation (ISO 8823), ACSE AARQ/AARE (ISO 8650) — both roles |
| `mms.py` | MMS PDUs (ISO 9506-2): Initiate, Conclude, GetNameList, GetVariableAccessAttributes, GetNamedVariableListAttributes, Read, Write (RCB attributes only), InformationReport; MMS Data and TypeSpecification |
| `client.py` | Asyncio MMS client: association, invoke ids, report callback; the guard checks every request |
| `model.py` | IEC 61850 mapping: references `LD/LN.DO.DA` + FC ↔ MMS `LN$FC$DO$DA`, Quality and TimeStamp → quality / time (spec 15.4), naming structured values by type, report decoding (8-1 §17.2.3) |
| `connector.py` | The connector: reports (buffered with EntryID resynchronisation, unbuffered, GI), polling of values no report carries, browse, read_now, test_connection |

- **Read-only (spec 12 and 15.4):** the only writes are to report-control-block attributes (`Resv`, `ResvTms`,
  `RptEna`, `GI`, `TrgOps`, `IntgPd`, `BufTm`, `OptFlds`, `EntryID`). There is no code for controls, setting groups,
  datasets, files or logs; `PurgeBuf` and `DatSet` writes are blocked by the guard. Tests check servers receive nothing else.
- **Datasets of the IED:** reports use the datasets already configured in the IED; the connector never creates or
  changes them. Browsing LLN0 lists the report control blocks with their dataset members.
- **A report that never comes** (an IED that accepts the setup but sends nothing after general interrogation) is
  noticed: its values are polled instead, and diagnostics say so.
- **Quality and time:** every value carries the quality of its data object (validity, detail bits, test,
  operatorBlocked, substituted) and the IED's time stamp; a clock that is not synchronised falls back to receive time
  with flag `CLOCK`.

Tests: `python -m pytest connectors/iec61850/tests -q` — BER/ISO/MMS unit tests, and end-to-end runs against
`tools/simulators/iec61850_server.py` (our stack in the server role; model from `tools/simulators/scl/feeder_relay.icd`)
over TCP and TLS. The CI job *iec61850-interop* also runs them against IEC61850bean (Apache-2.0, built from source by
`tools/interop/build_iec61850bean.sh`, test-only). IEC61850bean sends no buffered reports, which exercises the fallback.
