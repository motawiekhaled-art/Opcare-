# Central platform — `opcare/daq-platform` 0.1.0 (MVP slice)

Receives values, events and status from connectors, stores them, pushes configuration to connectors, relays
admin requests, and serves the HTTP API and the live-values screen. Architecture OSS-DAQ-ARC-001 section 9.

## Try it in one command

```bash
pip install starlette uvicorn jsonschema referencing
python tools/demo/run_demo.py          # prints http://127.0.0.1:8080/?token=...
```

The demo runs a simulated substation (three Modbus devices with changing values) → the real Modbus connector →
the platform → the browser, in one process. Meter 2 drops off the network for 20 s every 90 s so you can watch
quality, source state and events react. `--no-faults` turns that off.

## Parts

| File | Responsibility |
|---|---|
| `daq_platform/bus.py` | Message bus. `PahoBus` → central Mosquitto in production. `InMemoryBus` behaves like a broker (wildcards, retained messages, Last Will) for tests and the demo, and can host connectors |
| `daq_platform/store.py` + `sql/` | Storage. Same tables and statements on PostgreSQL (+TimescaleDB hypertable, compression, retention) and SQLite. All database work on one worker thread |
| `daq_platform/services.py` | Ingest (batches grouped for fewer writes, sequence-gap detection, never stops on a bad message), configuration service (schema validation, revisions, tag ownership, retained publish, delete), admin relay, live hub |
| `daq_platform/api.py` | Starlette HTTP API (FastAPI is built on Starlette, so it can move to FastAPI later unchanged in behaviour) and the Server-Sent-Events live stream |
| `daq_platform/server.py` | Production entry point (environment variables documented in the file) |
| `web/` | Live-values screen (sources, KPIs, live table, history chart, events) and configuration screens (`config.js`): sources list with enable/disable/delete, and the four-step wizard from the Opcare design — source type and protocol, connection and devices, tags (with Excel/CSV paste and Read), review with server-side check. Plain JavaScript, no build step |
| `daq_platform/catalog.py` | Source types and protocols shown in the wizard, each marked Available (connector installed), In development (specified) or Planned |
| `tools/check_postgres.py` | Runs the schema and every Store statement on a real PostgreSQL server |

## API (all JSON; `Authorization: Bearer <token>`)

| Method and path | Role | Purpose |
|---|---|---|
| `GET /api/health` | none | Liveness |
| `GET /api/me` | viewer | Who am I |
| `GET /api/sources` · `GET /api/sources/{id}` | viewer | Sources with live status · one source's configuration and status |
| `POST /api/sources` · `PUT /api/sources/{id}` | engineer | Create · update (validated against the connector schema; revision +1; pushed to the connector) |
| `DELETE /api/sources/{id}` | admin | Delete (connector receives the empty retained configuration) |
| `POST /api/sources/{id}/admin/{op}` | engineer (`ping`, `get_diagnostics`: viewer) | Relay `test_connection`, `read_now`, `browse`, … to the connector |
| `GET /api/catalog` | viewer | Source types and protocols with availability; demo devices when running the demo |
| `POST /api/sources-check?mode=create\|update&source_id=` | engineer | Validate exactly as saving would, without saving |
| `POST /api/sources-test?mode=create\|update&source_id=` | engineer | Test the connection of a configuration that is not saved yet (the site's edge agent opens a short connection) |
| `GET /api/tags?source=&q=` | viewer | Tags with latest value, quality and timestamps |
| `GET /api/tags/{tag}/history?minutes=` (or `from`, `to`) | viewer | Stored history |
| `GET /api/events?limit=&since=` | viewer | Events |
| `GET /api/stats` | viewer | Ingest counters, sequence gaps, live clients |
| `GET /api/audit` | admin | Audit trail of configuration changes |
| `GET /api/live?token=` | viewer | Server-Sent Events: `values`, `status`, `event` messages |

Errors: `{"error": {"code", "message", "details"?}}` with 400, 401, 403, 404, 409 (exists / tag in use),
422 (configuration invalid, with schema messages), 502 (connector answered with an error), 504 (connector did not answer).

## Tests

```bash
python -m pytest platform/tests -q                       # 5 tests
python platform/tools/check_postgres.py "host=... port=... user=... dbname=postgres"
```

`test_full_stack` drives simulator → connector → platform → HTTP: roles, schema rejection, create, live values,
the live stream, history, test connection via the API, update to revision 2, a meter going silent and coming
back, the connector process dying (Last Will → offline, admin → 504), delete and the audit trail.
`test_gui_in_browser` loads the screen in Chromium and checks live rows, quality badges, the chart and filtering
with no script errors.

## Changes from the architecture document, and what is not done yet

- **Live updates use Server-Sent Events instead of WebSocket.** One-way push is all the screen needs, it works
  through proxies, and it needs no extra library. Video overlays (later) may still use WebSocket.
- **Starlette instead of FastAPI** for this slice, because FastAPI could not be installed where it was built.
  Starlette is the framework FastAPI is built on, so moving later is mechanical.
- **Not done yet:** LDAP/AD login and multi-factor login (tokens for now), HTTPS termination (use the site's
  reverse proxy), threshold alarms and e-mail, OPC UA server and MQTT northbound, CSV export, configuration forms
  for protocols other than Modbus (they appear once their connectors exist).
