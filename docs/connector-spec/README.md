# Opcare DAQ Connector Specification pack (OSS-DAQ-CON-001, spec 1.0.0-draft.1)

Everything a developer needs to build a protocol connector for the Opcare Data Acquisition Platform.
Put this folder in the main repository at `docs/connector-spec/` (schemas also go into each connector image).

| Path | What it is |
|---|---|
| `CONNECTOR_SPEC.md` | The rulebook (normative). Read sections 0–14 once, then your protocol's profile in section 15 |
| `schemas/` | JSON Schema 2020-12 for every MQTT message, the manifest and the configuration envelope |
| `schemas/protocols/` | Configuration schema per connector type: modbus, opcua, mqtt, iec61850, iec104, dnp3, camera, udp, snmp |
| `asyncapi.yaml` | AsyncAPI 3.0 description of the six MQTT topics |
| `examples/` | Valid example messages for every schema, plus `examples/invalid/` negative tests that must be rejected |
| `tools/gen_schemas.py` | Source of the schemas: edit this, then regenerate (do not hand-edit the JSON) |
| `tools/validate_examples.py` | CI check: all examples valid, all negative tests rejected |
| `../../connectors/sdk-python/` | Python SDK (lives in `connectors/` in the repository): runtime (MQTT, lifecycle, config, status, admin, paging, shutdown), value pipeline, batcher, read-only guard, connector base class and a connector template |

## Quick start

```bash
pip install jsonschema referencing paho-mqtt pytest
python tools/gen_schemas.py          # regenerate schemas after editing the generator
python tools/validate_examples.py    # 32 examples, 0 failures expected
python -m pytest ../../connectors/sdk-python -q   # SDK tests; the runtime test validates every message it publishes
```

To start a new Python connector: copy `connectors/sdk-python/connector_template.py` (or follow `connectors/modbus`, the reference connector), set `type`/`version`, implement
`_connect`, `_request` (the only place requests leave the connector), polling or subscriptions, `test_connection`,
and `browse`/`read_now` if supported. Then pass conformance tests CT-01 … CT-18 (spec section 16).

## Status of this draft

- Verified here: schemas are valid JSON Schema 2020-12; all examples pass; SDK tests pass against an in-memory
  MQTT stand-in.
- Not yet verified: the SDK's paho-mqtt adapter against a real Mosquitto broker, and the conformance kit itself
  (simulators and CT tests are to be built in Phase 0, see Architecture section 13.3).
- The IEC 104 connector is GPL and must **not** use `sdk-python`; it implements the same behaviour in C
  (spec section 2, rule 7).
