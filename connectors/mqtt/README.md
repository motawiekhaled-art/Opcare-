# MQTT subscriber connector (`mqtt`, 1.0.0)

Spec: `docs/connector-spec/CONNECTOR_SPEC.md` section 15.3. Reads a **client's** MQTT broker — IoT gateways, smart
meters behind LoRaWAN / NB-IoT network servers, solar and EV equipment gateways, Sparkplug B edge nodes (Ignition Edge,
HiveMQ Edge, Opto 22 groov) and Unified Namespace brokers.

| Point format | Reads |
|---|---|
| `json` | A field of a JSON message (RFC 6901 pointer), optional timestamp field (ISO text, epoch ms or s) |
| `raw` | The whole message as a number, on/off or text |
| `sparkplug_b` | A Sparkplug B metric of an edge node or device: births, aliases, data, NDEATH / DDEATH → Bad (24) |

- **Read-only:** our own MQTT 3.1.1 / 5.0 client (`packets.py`, `client.py`) has no PUBLISH encoder; the guard allows
  only CONNECT, SUBSCRIBE, UNSUBSCRIBE, PINGREQ, acknowledgements and DISCONNECT. No Sparkplug rebirth commands.
- TLS (`mqtts://`) with the system CAs or a private CA, user name / password or client certificate; secrets are files.
- *Listen & pick values* (browse) shows the topics, JSON fields and Sparkplug metrics that really arrive.
- Standard library only (our own Sparkplug B protobuf decoder in `sparkplug.py`).

Tests: `python -m pytest connectors/mqtt/tests -q` (test broker `tools/simulators/mqtt_broker.py`, MQTT 3.1.1 and 5.0,
TLS with a test CA). CI also runs them against a real Mosquitto.
