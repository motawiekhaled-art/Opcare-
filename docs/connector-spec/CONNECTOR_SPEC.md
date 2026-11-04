# Opcare DAQ Connector Specification

| Item | Value |
|---|---|
| Document | OSS-DAQ-CON-001 |
| Spec version | **1.0.0-draft.1** (`"spec": "1.0"` in messages) |
| Date | 3 November 2026 |
| Parent documents | MVP Scope OSS-DAQ-MVP-001 v0.2 · Architecture OSS-DAQ-ARC-001 v0.1 (section 7) |
| Status | Draft for engineering review |
| Owner | Opcare Smart Services |

This document is **normative**. A connector that follows it can be started, configured, monitored and replaced by the platform without any change to the platform. The machine-readable parts of this spec live next to this file:

| File | Content |
|---|---|
| `schemas/*.schema.json` | JSON Schema (2020-12) for every message |
| `schemas/protocols/*.config.schema.json` | Configuration schema per connector type |
| `asyncapi.yaml` | AsyncAPI 3.0 description of every MQTT topic |
| `examples/` | Valid example messages (checked in CI) |
| `connectors/sdk-python/` (repository) | Reference SDK that implements the generic parts |

Where this text and a schema disagree, **the schema wins** and the text must be corrected.

---

## 0. Conventions

- The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** are used as in RFC 2119.
- All messages are UTF-8 JSON objects. Unknown fields **MUST** be ignored by receivers (forward compatibility) unless a schema says `additionalProperties: false`.
- All timestamps are RFC 3339 strings in UTC with a `Z` suffix and millisecond precision: `2026-11-03T18:02:11.250Z`.
- Durations in field names carry their unit: `_ms`, `_s`.
- Identifiers:

| Identifier | Pattern | Example |
|---|---|---|
| `site` | `^[a-z0-9][a-z0-9-]{0,62}$` | `ss-giza-220kv` |
| `source_id` | `^[a-z0-9][a-z0-9-]{0,62}$` | `iec61850-relay-f21` |
| `tag_id` | dot-separated segments, each `[a-z0-9][a-z0-9-]*`, max 200 chars | `ss-giza.bay05.relay-f21.ia` |
| `request_id`, `boot` | UUID (any version) | `0b6f…` |
| event `id` | ULID (26 chars, Crockford base32) | `01JBX3Q7K2M8V5T9N4R6C1D0EF` |

## 1. Terms

| Term | Meaning |
|---|---|
| **Connector type** | A program that speaks one protocol, e.g. `modbus`, `iec104`. Shipped as one container image. |
| **Source** | One configured connection (one channel) handled by one running connector. A source may contain several devices (e.g. several Modbus unit IDs on one RS-485 bus). |
| **Point** | One value the source reads. Each point maps to exactly one platform `tag_id`. |
| **Edge agent** | The supervisor on the edge node. Starts and stops connector containers, provides their environment and secrets. |
| **Local broker** | The Mosquitto broker on the same edge node. Connectors talk **only** to it. |
| **Platform** | Central services (config service, ingest, alarm engine, API). |

## 2. Packaging

1. A connector type **MUST** be delivered as an OCI container image named `opcare/connector-{type}:{semver}`.
2. The image **MUST** contain `/opcare/manifest.json` that validates against `schemas/manifest.schema.json`, and the configuration schema it declares.
3. The container **MUST** run as a non-root user (UID `10001`), **MUST** work with a read-only root file system, and **MAY** write only to:
   - `/data` (persistent, per source; at most 100 MB)
   - `/tmp` (tmpfs)
4. The container **MUST NOT** open listening ports. All communication is outbound to the local broker and to field devices.
5. **One container runs exactly one source.** Scaling is done by running more containers.
6. The image **MUST** carry OCI labels `org.opencontainers.image.version` and `org.opencontainers.image.licenses` (SPDX expression).
7. **GPL rule.** A connector that links GPL code (today: `iec104` with lib60870) **MUST** live in its own repository with its own licence file, **MUST** depend on the platform only through this specification (MQTT messages), and **MUST NOT** import any code from the closed platform repositories, including `sdk-python`. Such a connector implements the generic behaviour of this spec itself (the C SDK is released with it under GPL).

### 2.1 Manifest (`/opcare/manifest.json`)

```json
{
  "spec": "1.0",
  "type": "modbus",
  "name": "Modbus TCP / RTU",
  "version": "1.0.0",
  "license": "LicenseRef-Opcare-Proprietary",
  "config_schema": "/opcare/config.schema.json",
  "capabilities": {
    "modes": ["poll"],
    "browse": false,
    "read_now": true,
    "device_timestamps": false,
    "tls": false
  },
  "limits": { "max_points": 5000, "min_scan_ms": 100 }
}
```

## 3. Runtime environment

The edge agent starts each connector with these environment variables. A connector **MUST** fail fast (exit code `2`) if a required one is missing.

| Variable | Required | Example | Meaning |
|---|---|---|---|
| `OPCARE_SITE` | yes | `ss-giza-220kv` | Site id used in topics |
| `OPCARE_EDGE_ID` | yes | `edge-01` | Edge node id (for logs) |
| `OPCARE_SOURCE_ID` | yes | `iec61850-relay-f21` | This source; also the MQTT component id |
| `OPCARE_MQTT_URL` | yes | `mqtt://broker:1883` | Local broker (`mqtt://` or `mqtts://`) |
| `OPCARE_MQTT_USERNAME` | yes | `src-iec61850-relay-f21` | Broker account for this source |
| `OPCARE_MQTT_PASSWORD_FILE` | yes | `/run/secrets/mqtt_password` | File holding the broker password |
| `OPCARE_SECRETS_DIR` | no | `/run/secrets` | Directory of secret files (default `/run/secrets`) |
| `OPCARE_DATA_DIR` | no | `/data` | Persistent directory (default `/data`) |
| `OPCARE_LOG_LEVEL` | no | `info` | `debug`, `info`, `warn`, `error` |

**Secrets** (device passwords, private keys) are never inside the configuration document. Configuration fields ending in `_ref` name a file in `OPCARE_SECRETS_DIR`. Example: `"password_ref": "relay-f21-password"` → read `/run/secrets/relay-f21-password`. A missing secret file is error `SECRET_MISSING`.

## 4. MQTT rules

| Rule | Value |
|---|---|
| Protocol | MQTT 5.0 **SHOULD** be used; 3.1.1 **MAY** be used |
| Client id | `{site}.{source_id}` |
| Session | Clean start = false, session expiry 3600 s (MQTT 5); clean session = false (3.1.1) |
| QoS | 1 for every topic |
| Max payload | 256 KiB. Larger results **MUST** be split (see batching and paging) |
| Keep-alive | 30 s |
| Last Will | Topic `status`, retained, QoS 1, payload = offline status (section 9.2) |

### 4.1 Topics

`{s}` = `OPCARE_SITE`, `{id}` = `OPCARE_SOURCE_ID`.

| Topic | Direction | Retained | Message schema |
|---|---|---|---|
| `opcare/v1/{s}/config/{id}` | platform → connector | yes | `protocols/{type}.config.schema.json` |
| `opcare/v1/{s}/data/{id}` | connector → platform | no | `value-batch.schema.json` |
| `opcare/v1/{s}/event/{id}` | connector → platform | no | `event.schema.json` |
| `opcare/v1/{s}/status/{id}` | connector → platform | **yes** | `status.schema.json` |
| `opcare/v1/{s}/admin/{id}/req` | platform → connector | no | `admin-request.schema.json` |
| `opcare/v1/{s}/admin/{id}/res` | connector → platform | no | `admin-response.schema.json` |

The broker ACL for a source account **MUST** allow publish only to its `data`, `event`, `status` and `admin/…/res` topics, and subscribe only to its `config` and `admin/…/req` topics. Connectors **MUST NOT** publish or subscribe anywhere else.

## 5. Lifecycle

```
            ┌──────────┐ config valid ┌────────────┐ connected ┌─────────┐
start ─────►│ WAITING_ ├─────────────►│ CONNECTING ├──────────►│ RUNNING │◄─┐
            │ CONFIG   │◄──┐          └─────┬──────┘           └──┬──────┘  │
            └────┬─────┘   │ new config     │ fatal                │ partial  │ recovered
                 │ invalid │                ▼                      ▼          │
                 ▼         │           ┌────────┐            ┌──────────┐     │
            ┌────────┐     └───────────┤ ERROR  │            │ DEGRADED ├─────┘
            │ ERROR  │                 └────────┘            └──────────┘
            └────────┘          SIGTERM from any state → STOPPING → exit 0
```

| State | Meaning |
|---|---|
| `starting` | Process started, connecting to broker |
| `waiting_config` | Connected to broker, no valid configuration yet |
| `connecting` | Valid configuration; connecting to the device(s) |
| `running` | All enabled devices connected and acquiring |
| `degraded` | At least one device connected, at least one not; or scan overruns |
| `error` | Cannot acquire anything (invalid config, all devices unreachable, auth failure) |
| `stopping` | Shutting down after SIGTERM |
| `offline` | Only ever sent by the Last Will |

### 5.1 Start-up sequence (MUST, in this order)

1. Read environment; exit `2` if a required variable is missing.
2. Generate a new `boot` UUID for this process.
3. Connect to the local broker with the Last Will set (section 9.2).
4. Publish status `starting`, then `waiting_config`.
5. Subscribe to the `config` and `admin/…/req` topics.
6. On a configuration message: validate it (section 6.1). Valid → state `connecting`; invalid → state `error` with `CFG_INVALID` and keep waiting.
7. Connect to the device(s) and start acquisition.
8. Publish the status heartbeat every 10 s (and immediately on every state change).

### 5.2 Configuration changes

- A configuration message with a higher `revision` than the one running **MUST** be applied within 5 s.
- If only `points` changed, the connector **SHOULD** apply them without dropping the device connection.
- If the new configuration is invalid, the connector **MUST** keep running the previous valid configuration, publish event `config.rejected`, and report `last_error` = `CFG_INVALID` while staying in its current state.
- A configuration with `"enabled": false` **MUST** close all device connections and move to `waiting_config` (status shows `config_revision`).
- An empty retained message on the config topic means the source was deleted: disconnect and stay in `waiting_config`.
- After applying, publish event `config.applied` with the revision.

### 5.3 Shutdown

On SIGTERM the connector **MUST**, within 10 s: stop acquisition, publish any pending batch, publish status `stopping`, disconnect from devices, send a normal MQTT DISCONNECT (so the Last Will is **not** sent), and exit `0`. The edge agent then publishes nothing; the platform shows the source as stopped from the last `stopping` status.

## 6. Configuration document

Validated by `schemas/config-envelope.schema.json` plus the type schema in `schemas/protocols/`.

```json
{
  "spec": "1.0",
  "revision": 7,
  "site": "ss-giza-220kv",
  "source_id": "modbus-mcc-01",
  "type": "modbus",
  "name": "MCC-01 energy meters",
  "enabled": true,
  "connection": { "...": "protocol specific, section 15" },
  "acquisition": {
    "default_scan_ms": 1000,
    "batch_max_values": 500,
    "batch_max_ms": 1000,
    "integrity_s": 900,
    "request_timeout_ms": 3000
  },
  "points": [
    {
      "tag_id": "ss-giza.mcc01.meter-03.p-total",
      "address": { "...": "protocol specific, section 15" },
      "data_type": "float",
      "scaling": { "gain": 0.001, "offset": 0 },
      "unit": "kW",
      "deadband": { "mode": "abs", "value": 0.5 },
      "scan_ms": 1000,
      "enabled": true
    }
  ]
}
```

### 6.1 Validation rules

A configuration is valid only if **all** of these hold:

1. It validates against the envelope schema and the schema for its `type`.
2. `spec` major version equals the connector's; otherwise `CFG_UNSUPPORTED_SPEC`.
3. `site` and `source_id` equal the environment values.
4. `type` equals the manifest type.
5. Every `tag_id` is unique within the document.
6. Every `*_ref` secret file exists and is readable.
7. Every `scan_ms` ≥ manifest `limits.min_scan_ms`; number of points ≤ `limits.max_points`.
8. Protocol-specific checks in section 15 pass.

### 6.2 Point fields

| Field | Required | Meaning |
|---|---|---|
| `tag_id` | yes | Platform tag this point feeds |
| `address` | yes | Protocol-specific address object (section 15) |
| `data_type` | yes | Platform type of the published value: `bool`, `int`, `float`, `string`, `enum` |
| `scaling` | no | `value = raw × gain + offset`, applied only to `int` and `float`. Default `{gain: 1, offset: 0}` |
| `unit` | no | Engineering unit after scaling (informational for the connector) |
| `deadband` | no | `{mode: "abs" \| "pct", value}`. `pct` is a percentage of the last published value. Default none (every change is published) |
| `scan_ms` | no | Poll period for poll-mode points. Default `acquisition.default_scan_ms` |
| `enabled` | no | Default `true` |

`int` values **MUST** be published as JSON integers; if scaling makes them non-integer, the connector publishes a float and the config should declare `float`.

## 7. Publishing values

### 7.1 Value batch message

```json
{
  "spec": "1.0",
  "src": "modbus-mcc-01",
  "boot": "6c1e3b9e-1f7e-4a8e-9a33-3f1c2d4b5a60",
  "seq": 1842,
  "sent": "2026-11-03T18:02:12.003Z",
  "values": [
    { "t": "ss-giza.mcc01.meter-03.p-total", "v": 412.7, "q": 192,
      "ts": "2026-11-03T18:02:11.250Z", "rx": "2026-11-03T18:02:11.262Z" }
  ]
}
```

| Field | Rule |
|---|---|
| `boot` | The process `boot` UUID |
| `seq` | Starts at 1 for each `boot`, +1 for every batch, never skipped. The platform uses `(boot, seq)` to detect lost batches |
| `values[].t` | `tag_id` |
| `values[].v` | Scaled value of the point's `data_type`; `null` only when `q` is a Bad code |
| `values[].q` | Quality code, section 8 |
| `values[].ts` | Device timestamp if the protocol provides one and its time-quality is valid; otherwise equal to `rx` |
| `values[].rx` | Time the connector received the value |
| `values[].f` | Optional array of protocol-native flag names, for diagnostics only (e.g. `["NT","SB"]`) |

### 7.2 When to publish

1. **Report by exception.** A value is added to the batch only when it changed beyond its deadband, or its quality changed, since it was last published.
2. **Integrity refresh.** Every `acquisition.integrity_s` seconds (default 900; `0` disables) every enabled point is published once even if unchanged. Refresh values carry the last value and quality, `ts = rx =` time of the refresh, and `f: ["INTEGRITY"]`. Points currently Bad because of communication loss are not refreshed.
3. **Batching.** A batch is published when it holds `batch_max_values` values (default 500) or its oldest value is `batch_max_ms` old (default 1000), whichever comes first. Batches **MUST** stay under 256 KiB.
4. **Communication loss.** When a device becomes unreachable, every point of that device **MUST** be published once with `v: null`, `q: 24` (`BAD_COMM_FAILURE`), `ts = rx = now`. No further values are published for it until it recovers.
5. **Recovery.** After reconnecting, every point of that device **MUST** be published with fresh values (a general interrogation / integrity poll where the protocol has one).
6. **Order.** Values for the same tag **MUST** be published in time order. Values buffered inside the device (IEC 61850 buffered reports, DNP3 events, IEC 104 spontaneous data with time tags) **MUST** all be published, not only the latest.
7. **Back-pressure.** If the local broker is unreachable, the connector keeps an in-memory queue of at most 100,000 values, then drops the oldest and counts them in `stats.dropped_total`. (Normally this never happens: the local broker runs on the same node.)

## 8. Quality codes

The platform uses one 8-bit quality code, aligned with the OPC Data Access quality byte so it passes unchanged to the OPC UA server and historians.

| Code | Name | Use when |
|---|---|---|
| **192** | `GOOD` | Value valid |
| **216** | `GOOD_LOCAL_OVERRIDE` | Value valid but forced or substituted at the device by an operator |
| **64** | `UNCERTAIN` | Device marks the value questionable, test mode, or blocked |
| **68** | `UNCERTAIN_LAST_USABLE` | Device says the value is not up to date (old data, not topical) |
| **80** | `UNCERTAIN_SENSOR_NOT_ACCURATE` | Device reports reference or calibration error |
| **84** | `UNCERTAIN_EU_EXCEEDED` | Overflow or over-range |
| **0** | `BAD` | Device marks value invalid, or any other bad status |
| **4** | `BAD_CONFIG_ERROR` | Address does not exist on the device, type mismatch |
| **12** | `BAD_DEVICE_FAILURE` | Device reports an internal error for this value (e.g. Modbus exception 4) |
| **24** | `BAD_COMM_FAILURE` | No communication with the device |
| **28** | `BAD_OUT_OF_SERVICE` | Point disabled at the device |

If several conditions apply, the **lowest** code wins (Bad before Uncertain before Good). Per-protocol mapping is in section 15.

## 8a. Data type conversion

| Platform `data_type` | JSON | Rules |
|---|---|---|
| `bool` | `true`/`false` | Single points, coils, binary inputs |
| `int` | integer | Range −2^53+1 … 2^53−1; larger values become `float` with a warning at config validation |
| `float` | number | NaN and ±Infinity are not valid JSON: publish `v: null`, `q: 0`, `f: ["NAN"]` |
| `string` | string | Max 1024 characters, trimmed of trailing NUL bytes |
| `enum` | integer | Double points and multi-state values. The meaning of each number is defined in the tag, not the connector |

## 9. Status (heartbeat)

### 9.1 Heartbeat message

Published retained to `status` every 10 s and immediately on every state change.

```json
{
  "spec": "1.0",
  "src": "modbus-mcc-01",
  "kind": "connector",
  "type": "modbus",
  "version": "1.0.0",
  "boot": "6c1e3b9e-1f7e-4a8e-9a33-3f1c2d4b5a60",
  "state": "degraded",
  "since": "2026-11-03T17:55:02.118Z",
  "ts": "2026-11-03T18:02:20.000Z",
  "uptime_s": 3021,
  "config_revision": 7,
  "devices": [
    { "id": "meter-03", "state": "connected", "last_ok": "2026-11-03T18:02:19.870Z" },
    { "id": "meter-04", "state": "unreachable", "last_ok": "2026-11-03T17:54:58.004Z",
      "error": { "code": "CONN_TIMEOUT", "message": "No response from unit 4 within 3000 ms" } }
  ],
  "stats": { "points": 120, "values_total": 98213, "values_per_s": 24.6,
             "batches_total": 3001, "dropped_total": 0, "scan_overruns_total": 0 },
  "last_error": { "code": "CONN_TIMEOUT", "message": "No response from unit 4 within 3000 ms",
                  "ts": "2026-11-03T17:55:02.118Z" }
}
```

`devices[].state` is one of `connecting`, `connected`, `unreachable`, `disabled`.

### 9.2 Last Will (offline) message

Set at MQTT connect; sent by the broker if the connector dies:

```json
{ "spec": "1.0", "src": "modbus-mcc-01", "kind": "connector", "boot": "6c1e3b9e-1f7e-4a8e-9a33-3f1c2d4b5a60", "state": "offline" }
```

The platform treats a heartbeat older than 35 s as `offline` as well.

## 10. Events

Connectors publish operational events to the `event` topic (schema `event.schema.json`). Process values are **never** events: a breaker changing state is a value with a timestamp, not an event. (Analytics events from the video pipeline use the same schema with `ppe.*`, `zone.*`, `fire.*` types.)

| `type` | Severity | When |
|---|---|---|
| `conn.connected` | info | A device connection is established |
| `conn.lost` | medium | A device connection is lost |
| `conn.auth_failed` | high | Device or TLS authentication failed |
| `config.applied` | info | A configuration revision is applied (`attrs.revision`) |
| `config.rejected` | medium | A configuration was rejected (`attrs.revision`, `attrs.errors`) |
| `device.restart` | low | The device reports it restarted (e.g. DNP3 IIN1.7) |
| `device.time_invalid` | low | Device timestamps marked invalid; connector falls back to `rx` |
| `scan.overrun` | low | A poll cycle took longer than its scan period (max one event per minute) |
| `guard.blocked` | critical | The read-only guard blocked an outgoing write (section 12). Must never happen in production |

## 11. Admin requests

The platform sends requests to `admin/{id}/req`; the connector answers on `admin/{id}/res`, matching `request_id`.

```json
{ "spec": "1.0", "request_id": "1f0c2a8e-8a7b-4c2e-b0f4-0f7c7f5d2e11", "op": "browse",
  "args": { "path": "F21LD0/MMXU1", "depth": 1, "max_items": 500 },
  "timeout_ms": 15000, "requested_by": "eng.ahmed", "ts": "2026-11-03T18:10:00.000Z" }
```

```json
{ "spec": "1.0", "request_id": "1f0c2a8e-8a7b-4c2e-b0f4-0f7c7f5d2e11", "src": "iec61850-relay-f21",
  "ok": true, "ts": "2026-11-03T18:10:01.420Z", "done": true,
  "result": { "items": [ { "path": "F21LD0/MMXU1.A", "name": "A", "kind": "object",
                           "has_children": true } ] } }
```

### 11.1 Operations

| `op` | Args | Result | Required |
|---|---|---|---|
| `ping` | none | `{ "version", "state" }` | MUST |
| `get_diagnostics` | none | Free-form object: connection details, counters, last errors per device | MUST |
| `test_connection` | `{ "config"?: <config document> }` | `{ "devices": [ { "id", "ok", "latency_ms", "error"? , "info"? } ] }`. If `config` is given, test **that** configuration on a separate connection without applying it | MUST |
| `browse` | `{ "path"?, "depth"? (default 1, max 3), "max_items"? (default 500), "cursor"? , "listen_s"? }` | `{ "items": [ { "path", "name", "kind": "folder"\|"object"\|"variable", "has_children", "data_type"?, "address"? } ] }` where `address` is ready to paste into a point | MUST if `capabilities.browse` |
| `read_now` | `{ "tag_ids"?: [...], "addresses"?: [ {address, data_type} ] }` (max 100) | `{ "values": [ { "t"?, "address"?, "v", "q", "ts" } ] }` | MUST if `capabilities.read_now` |
| `reload_config` | none | `{ "revision" }` — re-reads the retained configuration | MUST |

Rules:

1. The connector **MUST** answer every request, before `timeout_ms`, with `ok: false` and `error.code = "TIMEOUT"` if it cannot finish in time, or `OP_UNSUPPORTED` for an unknown or unsupported `op`.
2. At most **2** admin operations run at the same time; extra requests are answered with `DEVICE_BUSY`.
3. Admin operations **MUST NOT** delay acquisition by more than one scan period. Browsing large devices must be paced.
4. Results larger than 256 KiB **MUST** be split: each part has `done: false` and a `cursor`; the last part has `done: true`. The platform sends a new `browse` with that `cursor` to continue.
5. Admin operations are subject to the read-only guard exactly like acquisition. No admin operation writes to a device.
6. `requested_by` **MUST** be written to the connector log at `info` level for audit.

## 12. Read-only guard (normative)

The MVP never changes anything on a field device.

1. Every connector **MUST** implement a **guard** at the lowest layer that sends requests to devices. The guard holds an **allow-list** of request types (section 15, "Allowed requests" per protocol). Every outgoing request is checked; anything not on the list is **not sent**, the call fails with `OP_FORBIDDEN`, and the connector publishes event `guard.blocked`.
2. Write, control, command, set-point, file-transfer, parameter, time-set and reboot services **MUST NOT** be implemented at all, even behind a flag.
3. Library features that send writes automatically (for example DNP3 time synchronisation, IEC 104 clock sync, OPC UA "write on reconnect") **MUST** be switched off explicitly in code, with a comment pointing to this section.
4. The only writes allowed are the **reporting-setup** writes listed per protocol (IEC 61850 report control block attributes, DNP3 enable-unsolicited and IIN restart-bit clear). They change how the device reports, never a process value or setting.
5. The conformance kit runs each connector against a simulator that records every request; any request outside the allow-list fails the build.

## 13. Timing, retries and performance

| Item | Rule |
|---|---|
| Device reconnect | Exponential backoff: 1 s, 2 s, 4 s … max 60 s, ±20 % random jitter. Reset after 60 s connected |
| Request timeout | `acquisition.request_timeout_ms` (default 3000) |
| Poll scheduling | Points are grouped by `scan_ms` (scan classes). Each class runs on its own timer aligned to wall-clock multiples of `scan_ms` |
| Overrun | If a poll cycle takes longer than its period, the next cycle starts immediately after (no queue build-up), `stats.scan_overruns_total` increases, state becomes `degraded` after 3 consecutive overruns |
| Requests per device | Modbus: 1 outstanding request per device by default; others as the protocol allows |
| Start-up | Status `starting` within 2 s of process start; first values within `scan_ms` + 5 s of a valid configuration (device permitting) |
| Resource use | ≤ 256 MB RAM and ≤ 1 CPU core for 5,000 points at 1 s |

## 14. Logging and security

**Logging.** One JSON object per line on stdout:

```json
{"ts":"2026-11-03T18:02:11.250Z","level":"warn","src":"modbus-mcc-01","boot":"6c1e…","msg":"Device unreachable","code":"CONN_TIMEOUT","device":"meter-04"}
```

- Levels `debug`, `info`, `warn`, `error`. Default `info`.
- **MUST NOT** log secrets, passwords, private keys or full configuration documents. Log `revision` instead.
- **MUST NOT** log raw protocol frames at `info` or above.

**Security.**

- TLS certificate validation **MUST** be on by default. Turning it off needs `"insecure_skip_verify": true` in the configuration and logs a `warn` on every connect.
- Secrets are read from files at connect time and kept only in memory; they **MUST NOT** appear in status, events, admin responses or `/data`.
- The connector **MUST** connect only to the hosts in its configuration.

### 14.1 Error codes

Used in `last_error`, `devices[].error`, admin `error` and logs.

| Code | Meaning |
|---|---|
| `CFG_INVALID` | Configuration failed validation |
| `CFG_UNSUPPORTED_SPEC` | `spec` major version not supported |
| `SECRET_MISSING` | A `*_ref` secret file is missing or unreadable |
| `CONN_REFUSED` | TCP connection refused |
| `CONN_TIMEOUT` | No connection or no response in time |
| `CONN_CLOSED` | Peer closed the connection |
| `TLS_FAILED` | TLS handshake or certificate validation failed |
| `AUTH_FAILED` | Device rejected credentials |
| `ADDR_INVALID` | Address syntax wrong for this protocol |
| `POINT_NOT_FOUND` | Address does not exist on the device |
| `TYPE_MISMATCH` | Device data type does not fit `data_type`/encoding |
| `DEVICE_BUSY` | Device or connector too busy; retry later |
| `OP_UNSUPPORTED` | Admin operation not supported |
| `OP_FORBIDDEN` | Blocked by the read-only guard |
| `TIMEOUT` | Admin operation did not finish in `timeout_ms` |
| `PROTOCOL_ERROR` | Malformed or unexpected protocol data |
| `INTERNAL` | Bug in the connector |

## 15. Protocol profiles

Each profile defines the `connection` object, the point `address` object, extra `acquisition` fields, data mapping, quality mapping, browse behaviour and the **allowed requests** for the guard. The JSON Schemas in `schemas/protocols/` are the exact definition.

### 15.1 Modbus TCP / RTU — type `modbus`

**connection**

| Field | Default | Notes |
|---|---|---|
| `transport` | — | `tcp` or `rtu` |
| `host`, `port` | —, 502 | TCP only |
| `serial` | — | RTU only: `{device, baud (9600), parity "N"\|"E"\|"O" ("E"), data_bits (8), stop_bits (1)}` |
| `devices` | — | `[{id, unit_id (0–247), name?}]` — at least one |
| `max_registers_per_read` | 100 | 1–125 (registers) |
| `max_bits_per_read` | 1000 | 1–2000 (coils/inputs) |
| `max_gap` | 10 | Gap of unused registers allowed when merging reads |
| `inter_request_delay_ms` | 0 (TCP), 20 (RTU) | Pause between requests |

**address**: `{device, table, address, encoding, word_order?, byte_order?, bit?, length?}`

- `table`: `coil` (FC 1), `discrete_input` (FC 2), `holding_register` (FC 3), `input_register` (FC 4).
- `address`: **0-based protocol address**. The GUI converts "Modicon" notation (e.g. 40001 → `holding_register` 0).
- `encoding`: `bool`, `int16`, `uint16`, `int32`, `uint32`, `int64`, `uint64`, `float32`, `float64`, `string` (needs `length` in registers), `bit` (needs `bit` 0–15 inside one register).
- `word_order`: `big` (default, high word first) or `little`. `byte_order`: `big` (default) or `little`.

**Acquisition**: poll only. The connector **MUST** merge points of the same device, table and scan class into as few reads as possible within `max_registers_per_read` and `max_gap`. If a merged read returns exception 1, 2, 3 or 4, the connector **MUST** split that block and read its points one by one from then on, so a missing or faulty address only affects its own point.

**Quality**: OK response → 192 · exception 1 (illegal function), 2 (illegal address) or 3 (illegal value) → 4 · exception 4 (device failure) → 12 · exceptions 5/6 (acknowledge/busy) → retry once, then 24 · timeout or link loss → 24 · gateway exceptions 10/11 → 24.

**Browse**: not supported (`capabilities.browse = false`); register maps are imported as CSV by the platform.

**Allowed requests**: function codes **1, 2, 3, 4** only. Everything else is forbidden, for example 5, 6, 15, 16, 22, 23 (writes), 8 (diagnostics, can reset counters) and 43 (encapsulated interface, not needed in the MVP).

### 15.2 OPC UA client — type `opcua`

**connection**

| Field | Default | Notes |
|---|---|---|
| `endpoint_url` | — | `opc.tcp://host:4840/path` |
| `security_policy` | `Basic256Sha256` | `None`, `Basic256Sha256`, `Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss` |
| `security_mode` | `SignAndEncrypt` | `None`, `Sign`, `SignAndEncrypt` |
| `auth` | `anonymous` | `anonymous`, `username` (`username`, `password_ref`), `certificate` (`user_cert_ref`, `user_key_ref`) |
| `client_cert_ref`, `client_key_ref` | — | Application instance certificate (required unless policy `None`) |
| `trusted_server_thumbprints` | [] | SHA-1 thumbprints of accepted server certificates. Empty + policy not `None` → `TLS_FAILED` unless `insecure_skip_verify` |
| `session_timeout_ms` | 60000 | |

**address**: `{node_id}` in standard string form (`ns=2;s=Line1.Temp`, `ns=3;i=1001`, `nsu=urn:…;s=…`). Namespace URIs (`nsu=`) **SHOULD** be preferred because namespace indexes can change on server restart.

**acquisition** extras: `mode` `subscribe` (default) or `poll`; `publishing_interval_ms` (1000), `sampling_interval_ms` (−1 = same as publishing), `queue_size` (10), `max_items_per_subscription` (1000).

**Timestamps**: `ts` = SourceTimestamp; if absent, ServerTimestamp; if absent, `rx`.

**Quality**: StatusCode severity Good → 192 · Good_LocalOverride → 216 · Uncertain_LastUsableValue → 68 · Uncertain_SensorNotAccurate → 80 · Uncertain_EngineeringUnitsExceeded → 84 · other Uncertain → 64 · Bad_NodeIdUnknown / Bad_NodeIdInvalid / Bad_TypeMismatch → 4 · Bad_DeviceFailure → 12 · Bad_CommunicationError / Bad_NotConnected / Bad_ServerNotConnected / session lost → 24 · Bad_OutOfService → 28 · other Bad → 0.

**Browse**: Browse service on `path` (a node id; empty = Objects folder `i=85`). Items return node class, browse name, display name, data type and `address: {node_id}` (with `nsu=`).

**Allowed requests**: GetEndpoints, FindServers, OpenSecureChannel, CloseSecureChannel, CreateSession, ActivateSession, CloseSession, Browse, BrowseNext, TranslateBrowsePathsToNodeIds, Read, RegisterNodes, UnregisterNodes, CreateSubscription, ModifySubscription, DeleteSubscriptions, SetPublishingMode, CreateMonitoredItems, ModifyMonitoredItems, DeleteMonitoredItems, Publish, Republish. **Forbidden**: Write, Call, HistoryUpdate, AddNodes, DeleteNodes, AddReferences, DeleteReferences, TransferSubscriptions to other sessions.

### 15.3 MQTT subscriber — type `mqtt`

For IoT devices that publish to a **client's** broker (not the Opcare local broker).

**connection**: `{broker_url (mqtt:// or mqtts://), client_id?, protocol_version "5"|"3.1.1", auth "anonymous"|"username"|"certificate", username?, password_ref?, client_cert_ref?, client_key_ref?, ca_cert_ref?, subscriptions: [{topic_filter, qos (0|1)}]}`.

**address** — one of:

- JSON/raw: `{topic, format: "json"|"raw", value_pointer?, timestamp_pointer?, timestamp_format?: "rfc3339"|"epoch_ms"|"epoch_s"}`. `value_pointer` and `timestamp_pointer` are RFC 6901 JSON Pointers (e.g. `/data/temp`). `raw` parses the payload as a number, `true`/`false` or text.
- Sparkplug B: `{format: "sparkplug_b", group_id, edge_node_id, device_id?, metric}`.

**Rules**: `topic` must match one of the configured `subscriptions`. A payload that cannot be parsed sets the point to `q: 0` with `f: ["PARSE_ERROR"]`. Sparkplug: NDEATH/DDEATH → all metrics of that node/device `q: 24`; `is_null` → `v: null, q: 0`; metric timestamps used as `ts`.

**Browse**: subscribes to `path` (a topic filter, default the configured filters) for `listen_s` seconds (default 10, max 60) and returns topics seen with a sample payload (first 1 KiB) and, for Sparkplug, metric names and types.

**Allowed requests**: CONNECT, SUBSCRIBE, UNSUBSCRIBE, PINGREQ, PUBACK/PUBREC/PUBREL/PUBCOMP for received messages, DISCONNECT. **Forbidden**: any PUBLISH, including Sparkplug NCMD/DCMD (rebirth requests). If a Sparkplug node needs a rebirth, the connector waits for the next NBIRTH and marks metrics `q: 68` meanwhile.

### 15.4 IEC 61850 MMS client — type `iec61850`

**connection**: `{host, port (102), ied_name, timeout_ms (10000), tls?: {enabled, client_cert_ref, client_key_ref, ca_cert_ref}, acse_auth?: {password_ref}, scl_file_ref?}`. `scl_file_ref` (an ICD/CID file) lets the connector validate addresses offline.

**address**: `{object_ref, fc}` — `object_ref` is the full MMS reference `LD/LN.DO[.SDO].DA[.BDA]` (e.g. `F21LD0/MMXU1.A.phsA.cVal.mag.f`), `fc` the functional constraint (`MX`, `ST`, `CO` is **not** allowed, `SP`/`SG`/`SE` not allowed in MVP; allowed: `MX`, `ST`, `CF`, `DC`, `EX`).

**acquisition** extras:

- `reports`: `[{rcb_ref, buffered (true), trigger_options: ["dchg","qchg","dupd","integrity","gi"], integrity_period_ms?, buf_time_ms?, reserve (true)}]` — e.g. `F21LD0/LLN0.BR.brcb01`. Points delivered by a report do not need polling.
- `poll_unreported` (true): points not contained in any report dataset are polled with `scan_ms`.
- `gi_on_connect` (true): trigger general interrogation on each enabled report after connecting.

**Datasets**: the connector **MUST** use datasets already configured on the IED. It **MUST NOT** create, change or delete datasets.

**Quality** (from the IEC 61850 `q` attribute of the data object, or the report's quality): `validity` good & `test` false & `operatorBlocked` false & `source` process → 192 · `source` substituted → 216 · `validity` questionable, `test` true, or `operatorBlocked` true → 64 · detail `oldData` → 68 · detail `inaccurate` or `badReference` → 80 · detail `overflow` or `outOfRange` → 84 · detail `failure` → 12 · `validity` invalid → 0 · association lost → 24. Put set detail-bit names in `f`.

**Timestamps**: `ts` = the `t` attribute of the same data object; if its TimeQuality has `ClockFailure` or `ClockNotSynchronized`, use `rx` and add `f: ["CLOCK"]`.

**Browse**: `path` empty → logical devices (GetServerDirectory); `LD` → logical nodes; `LD/LN` → data objects; deeper → data attributes with FC and type. Items carry `address: {object_ref, fc}`.

**Allowed requests**: Initiate, Conclude, Abort, GetNameList, GetVariableAccessAttributes, GetNamedVariableListAttributes, Read, InformationReport (receive), file services **not** allowed. **Allowed writes (reporting setup only)** to report control block attributes: `Resv`, `ResvTms`, `RptEna`, `GI`, `TrgOps`, `IntgPd`, `BufTm`, `OptFlds`, `EntryID` (for buffered-report resynchronisation). **Forbidden**: any other Write, all control services (Select, SelectWithValue, Operate, Cancel, TimeActivatedOperate), SetEditSG/ConfirmEditSG, SetActiveSG, dataset creation/deletion, `PurgeBuf`, file services, log services that modify.

### 15.5 IEC 60870-5-104 master — type `iec104` (GPL connector)

**connection**: `{hosts: [{host, port (2404)}] (1–2; second = redundant link, used only if the first fails), common_addresses: [int] (1–65534), originator_address (0), k (12), w (8), t0_s (30), t1_s (15), t2_s (10), t3_s (20), tls?: {enabled, client_cert_ref, client_key_ref, ca_cert_ref}}`. Parameters as in IEC 60870-5-104 defaults.

**address**: `{ca, ioa, type?}` — `ca` common address, `ioa` information object address (1–16777215), optional expected ASDU `type` (e.g. `M_ME_NC_1`) for validation.

**acquisition** extras: `gi_on_connect` (true), `gi_period_s` (900; 0 = off), `counter_interrogation_period_s` (0 = off).

**Data mapping**

| ASDU types | `data_type` | Value |
|---|---|---|
| M_SP_NA_1, M_SP_TB_1 (single point) | `bool` | SPI |
| M_DP_NA_1, M_DP_TB_1 (double point) | `enum` | 0 = intermediate, 1 = off, 2 = on, 3 = indeterminate |
| M_ST_NA_1, M_ST_TB_1 (step position) | `int` | value −64…63, transient flag in `f: ["TRANSIENT"]` |
| M_BO_NA_1, M_BO_TB_1 (bitstring 32) | `int` | unsigned 32-bit |
| M_ME_NA_1, M_ME_TD_1 (normalised) | `float` | −1.0 … +1−2⁻¹⁵ before scaling |
| M_ME_NB_1, M_ME_TE_1 (scaled) | `int` or `float` | signed 16-bit before scaling |
| M_ME_NC_1, M_ME_TF_1 (short float) | `float` | IEEE 754 |
| M_IT_NA_1, M_IT_TB_1 (integrated totals) | `int` | counter reading; sequence number in `f` |

**Timestamps**: types with CP56Time2a → `ts` from the time tag; if its IV (invalid) bit is set use `rx` and `f: ["CLOCK"]`. CP56Time2a carries no time zone: `connection.device_time_zone` (default `UTC`, IANA name) converts it to UTC.

**Quality** (QDS / SIQ / DIQ bits): none set → 192 · IV → 0 · NT → 68 · SB → 216 · BL → 64 · OV → 84. Counter CY/CA/IV flags: IV → 0, CA → 64. Link down or STARTDT not confirmed → 24.

**Browse**: not supported (IOA lists are imported by the platform). `read_now` uses C_RD_NA_1 (read command).

**Allowed requests**: U-frames STARTDT act, STOPDT act, TESTFR act/con; S-frames; I-frames with type **C_IC_NA_1 (100)** general interrogation, **C_CI_NA_1 (101)** counter interrogation, **C_RD_NA_1 (102)** read. **Forbidden**: types 45–64 (all single/double/regulating/set-point/bitstring commands, with or without time tag), C_CS_NA_1 (103) clock sync, C_TS_NA_1 (104) test, C_RP_NA_1 (105) reset process, C_CD_NA_1 (106) delay acquisition, C_TS_TA_1 (107) test with time tag, P_ME_* / P_AC_NA_1 (110–113) parameters, F_* (120–127) file transfer.

### 15.6 DNP3 master over TCP — type `dnp3`

**connection**: `{host, port (20000), master_address (1), outstation_address (10), response_timeout_ms (5000), keep_alive_s (60), tls?: {enabled, client_cert_ref, client_key_ref, ca_cert_ref}}`.

**address**: `{group, index}` where `group` is `binary_input`, `double_bit_binary_input`, `analog_input`, `counter`, `frozen_counter`, `binary_output_status`, `analog_output_status`; `index` 0–65535.

**acquisition** extras: `integrity_on_connect` (true), `integrity_period_s` (3600), `class_polls: {class1_ms (5000), class2_ms (5000), class3_ms (5000)}` (0 = off), `unsolicited: true|false` (false).

**Data mapping**: binary → `bool`; double-bit → `enum` (0 intermediate, 1 off, 2 on, 3 indeterminate); analog → `float` (or `int` for 16/32-bit integer variations); counters → `int`; output status → as their input equivalents.

**Timestamps**: event objects with time (e.g. g2v2, g32v3) → `ts` from the object; otherwise `rx`. Time is UTC per IEEE 1815.

**Quality** (DNP3 flags): ONLINE only → 192 · not ONLINE → 0 · COMM_LOST → 24 · RESTART → 68 · LOCAL_FORCED or REMOTE_FORCED → 216 · OVER_RANGE or ROLLOVER → 84 · REFERENCE_ERR → 80 · CHATTER_FILTER → 64. Put flag names in `f`.

**Allowed requests** (function codes): **0** CONFIRM, **1** READ, **20** ENABLE_UNSOLICITED and **21** DISABLE_UNSOLICITED (reporting setup, only if `unsolicited` is configured), **2** WRITE **only** for clearing the IIN1.7 DEVICE_RESTART bit (g80v1 index 7, value 0). **Forbidden**: 3–6 (select/operate/direct operate), 7–12 (freeze), 13–14 (restart), 15–19 (initialise data, application control, save configuration), 22 (assign class), 23–24 (delay measurement / record time), 25–31 (file), any other WRITE (including time sync). With opendnp3: set time-sync mode to none and do not issue command sets.

### 15.7 IP camera (ONVIF / RTSP) — type `camera`

The camera connector registers cameras and watches their health. **It does not analyse video**; the video pipeline reads the stream URL from the camera's configuration.

**connection**: `{host, onvif_port (80), onvif: true|false, username?, password_ref?, profile_token?, rtsp_url?, rtsp_transport "tcp"|"udp" ("tcp"), tls?: {enabled, ca_cert_ref}}`. With `onvif: true`, the connector resolves the stream URL from `profile_token` (default: first profile with H.264/H.265); with `onvif: false`, `rtsp_url` is required.

**address**: `{metric}` one of `online` (`bool`), `fps` (`float`), `bitrate_kbps` (`float`), `resolution` (`string`, e.g. `1920x1080`), `codec` (`string`), `onvif_event` (with `topic`, e.g. `tns1:VideoSource/MotionAlarm`, as `bool`).

**Quality**: stream received → 192 · stream connect fails → 24 · ONVIF auth error → 0 with `f: ["AUTH"]`.

**Browse**: `path` empty → WS-Discovery probe on the local network for `listen_s` (default 5) seconds, returns cameras found (address, manufacturer, model); `path` = camera host → its media profiles with resolution, codec and stream URI.

**Allowed requests (ONVIF)**: GetSystemDateAndTime, GetCapabilities, GetServices, GetDeviceInformation, GetNetworkInterfaces, GetProfiles, GetStreamUri, GetSnapshotUri, GetVideoSources, GetEventProperties, CreatePullPointSubscription, PullMessages, Renew, Unsubscribe; WS-Discovery Probe; RTSP OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER (keep-alive). **Forbidden**: every `Set*`, `Create*` other than the pull-point subscription, `Delete*`, PTZ operations (ContinuousMove, AbsoluteMove, GotoPreset, SetPreset…), SystemReboot, SetSystemFactoryDefault, firmware upgrade, relay outputs (SetRelayOutputState), RTSP ANNOUNCE/RECORD/SET_PARAMETER.

## 16. Conformance kit

Every connector **MUST** pass these tests in CI against the matching simulator (`tools/simulators/`). The kit starts the connector container, a Mosquitto broker and the simulator, then plays the steps.

| ID | Test | Pass condition |
|---|---|---|
| CT-01 | Manifest | `/opcare/manifest.json` valid; config schema present |
| CT-02 | Start-up | Status `starting` → `waiting_config` within 2 s; container non-root, read-only rootfs works |
| CT-03 | Missing env | Exits with code 2 |
| CT-04 | Last Will | Kill -9 → broker publishes `offline` status |
| CT-05 | Invalid config | State `error`, `last_error.code = CFG_INVALID`, event `config.rejected` |
| CT-06 | Valid config | `running` and first values within `scan_ms` + 5 s; every message valid against its schema |
| CT-07 | Hot reload | New revision with changed points applied within 5 s without reconnect |
| CT-08 | Bad revision | Invalid newer revision rejected; old revision keeps running |
| CT-09 | Deadband & integrity | Unchanged values not re-sent until `integrity_s`; deadband honoured |
| CT-10 | Comm loss | Simulator stopped → every point once with `q: 24`, state `error`/`degraded`, event `conn.lost` |
| CT-11 | Recovery | Simulator restarted → fresh values for every point, reconnect backoff within spec |
| CT-12 | Sequence | `seq` continuous per `boot`; new `boot` after restart |
| CT-13 | Read-only guard | Simulator records zero requests outside the allow-list across all tests; forced write attempt in test mode → `OP_FORBIDDEN` + `guard.blocked` |
| CT-14 | Admin ops | `ping`, `get_diagnostics`, `test_connection`, `browse`, `read_now`, unknown op → correct responses within `timeout_ms` |
| CT-15 | Shutdown | SIGTERM → `stopping` status, exit 0 within 10 s, no Last Will |
| CT-16 | No secrets | Secret values never appear in stdout, status, events or admin responses |
| CT-17 | Quality mapping | Simulator injects each protocol flag → expected `q` per section 15 |
| CT-18 | Load | 5,000 points at 1 s for 10 min: no drops, ≤ 256 MB RAM, ≤ 1 core |

## 17. Versioning

- This spec follows semantic versioning. Messages carry `"spec": "MAJOR.MINOR"`.
- A **minor** change only adds optional fields or new `op`/event types. Connectors and platform **MUST** accept any minor version of the same major.
- A **major** change uses a new topic root (`opcare/v2/…`). The platform supports the current and previous major in parallel during migration.
- The platform publishes configuration with the highest `spec` the connector's manifest declares.

## Appendix A. Checklist for a new connector

1. Copy the connector template (`connectors/_template`) or, for GPL code, the standalone C template.
2. Write `manifest.json` and `config.schema.json` (start from `schemas/protocols/`).
3. Implement: connect/disconnect, acquisition (poll and/or subscribe), quality and timestamp mapping, `test_connection`, `browse` (if supported), `read_now`.
4. Implement the guard allow-list exactly as in section 15; turn off any automatic library writes.
5. Add simulator scenarios for CT-17 (every quality flag).
6. Pass CT-01 … CT-18 in CI.
7. Record licences of all libraries in the SBOM; the main repository build rejects AGPL, BSL and non-commercial licences.
