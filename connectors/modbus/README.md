# Modbus TCP / RTU connector — `opcare/connector-modbus` 1.0.0

Reference connector for the Opcare DAQ platform. Implements spec OSS-DAQ-CON-001 section 15.1 on top of
`connectors/sdk-python`. **Use it as the model for every other Python connector.**

## Design

| File | Responsibility |
|---|---|
| `modbus_connector/protocol.py` | Read-only Modbus client: TCP (MBAP) and RTU (CRC-16) framing. Only function codes 1–4 can be encoded; every request also passes the `ReadOnlyGuard`. One outstanding request per link; late TCP answers are discarded by transaction id; framing errors reset the link |
| `modbus_connector/decode.py` | Decoding of all encodings and word/byte orders (ABCD, CDAB, BADC, DCBA); the read planner that merges points into block reads (`max_registers_per_read`, `max_bits_per_read`, `max_gap`) |
| `modbus_connector/connector.py` | Lifecycle, reconnect with backoff and jitter, one poll task per scan class aligned to the clock, overrun handling, quality mapping, device states, `test_connection`, `read_now`, diagnostics, hot reload |
| `opcare/manifest.json` | Manifest copied into the image |
| `Dockerfile` | Non-root (UID 10001), read-only-root-filesystem image |

Why our own small client instead of pymodbus: it is read-only by construction (there is no write code to
misuse), has no third-party dependency, and is about 200 lines. pymodbus can replace `protocol.py` later
without touching the rest, if ever needed.

## Behaviour worth knowing

- **Merged reads with automatic isolation.** Points close together are read in one request. If the device
  answers a merged read with exception 1–4 (for example because a gap address does not exist), that block is
  split into runs of back-to-back addresses, and a run that still fails into single points. A bad address
  therefore only affects its own tag. The split is remembered (see `get_diagnostics`).
- **Quality.** OK → 192 · exception 1/2/3 → 4 · exception 4 → 12 · exception 5/6 → one retry, then 24 ·
  exception 10/11 or no answer → device unreachable, its points 24 · link lost → every point 24.
- **Units on one link.** All unit ids of a source share one TCP connection (or serial port). A silent unit
  only makes that unit unreachable; the others continue (state `degraded`).
- **RTU.** Needs `pyserial-asyncio` on the edge node. `test_connection` reuses the open port because a serial
  port cannot be opened twice.

## Tests

```bash
python -m pytest connectors/modbus/tests -q
```

12 tests: CRC vectors, read-only encoding, response parsing, all four word/byte orders, all encodings, the
read planner, RTU framing over a stream, a full end-to-end run against `tools/simulators/modbus_sim.py`
(values, scaling, exceptions, block isolation, unit loss and recovery, link loss and reconnect,
`test_connection`, `read_now`, hot reload without reconnect, the guard, schema validity of every message)
and a load test (2,000 registers at 1 s: 20 reads per cycle, no overruns).

Not covered yet: a real Mosquitto broker (tests use the SDK's in-memory MQTT stand-in), a real serial port,
and real meters.
