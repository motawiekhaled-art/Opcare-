# opcare-daq

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/motawiekhaled-art/Opcare-?quickstart=1)

Opcare Smart Services — Data Acquisition Platform, main repository (closed source).
Layout follows Architecture document OSS-DAQ-ARC-001 section 13.1. Only the parts built so far are present.

```
docs/connector-spec/        Connector specification OSS-DAQ-CON-001: rulebook, schemas, AsyncAPI, examples
connectors/sdk-python/      Python connector SDK (runtime, value pipeline, guard, test harness)
connectors/modbus/          Modbus TCP / RTU connector 1.0.0 (reference connector)
connectors/mqtt/            MQTT subscriber 1.0.0 (3.1.1 / 5.0, TLS): JSON, plain values and Sparkplug B from IoT and edge gateways
connectors/opcua/           OPC UA client 1.0.0 with Opcare's own stack: all four security policies, subscriptions, browse
connectors/dnp3/            DNP3 master 1.0.0 with Opcare's own stack (TCP / TLS): integrity, class polls, unsolicited, event time
connectors/iec61850/        IEC 61850 MMS client 1.0.0 with Opcare's own stack: buffered / unbuffered reports, GI, quality and time
connectors/iec104/          IEC 60870-5-104 master 1.0.0 with Opcare's own stack: interrogation, spontaneous data with time tags, counters, redundant link
connectors/udp/             UDP listener 1.0.0: NMEA 0183, CSV, JSON, binary telegrams and syslog pushed by devices
connectors/snmp/            SNMP manager 1.0.0 (v1, v2c, v3): switches, servers, UPSs, firewalls; traps as events
platform/                   Central platform: ingest, storage, configuration service, API, live-values and configuration screens
edge/                       Edge agent: starts a connector per configured source, tests configurations before saving
tools/simulators/           Device simulators (Modbus TCP, SNMP agent, MQTT broker, OPC UA server, DNP3 outstation, IEC 61850 server from SCL, IEC 104 station)
tools/demo/                 One-command demo: substation, pump station, switch + UPS, GNSS/weather station, water meters + solar plant (MQTT), bottling line PLC (OPC UA), feeder RTU (DNP3), feeder relay (IEC 61850), substation gateway (IEC 104)
```

Still to come: alarms, the container-based production edge agent, other connectors,
`video/`, `deploy/`.

## Try it in the browser (nothing to install)

Click **Open in GitHub Codespaces** above, or share this link with anyone who has access to the repository:
`https://codespaces.new/motawiekhaled-art/Opcare-?quickstart=1`

GitHub starts the demo in the cloud and opens it in a new tab (first start about 2 minutes, later starts
faster). Sign in as `admin` / `Opcare@2026` (shown on the sign-in screen). Details: `.devcontainer/README.md`.

## Demo page (one HTML file, opens in Chrome)

**Actions → Demo page → Run workflow**, wait about a minute, open the run and download **opcare-demo-page**
from Artifacts. Unzip it and double-click `opcare-demo.html`: the real screens run with a simulated
substation and pump station, no server needed. Send the file to anyone who should try it.
Build it locally with `python tools/demo/build_web_demo.py opcare-demo.html`.

## Users and sign-in

People sign in with a user name and password. Admins manage users in the **Admin** tab: add a user with a role
(viewer, operator, engineer, admin) and a temporary password, change roles, reset passwords, disable or delete users,
and read the audit log. A new user must choose their own password at first sign-in. Passwords are stored only as
salted scrypt hashes; five wrong passwords lock a user name for 5 minutes; there is always at least one active admin.

- Demo: user `admin`, password `Opcare@2026`.
- Production (`python -m daq_platform.server`): the first admin is created on an empty database from
  `OPCARE_ADMIN_USER` / `OPCARE_ADMIN_PASSWORD_FILE` (without a file a random password is written once to the log).
- Machines and scripts can still use static API tokens (`OPCARE_API_TOKENS_FILE`).

## Run everything

```bash
pip install starlette uvicorn jsonschema referencing pytest cryptography   # + playwright for the browser tests
python tools/demo/run_demo.py                                 # live demo; open the printed URL
python docs/connector-spec/tools/validate_examples.py          # spec examples
python -m pytest connectors platform edge -q                  # all tests
python tools/simulators/modbus_sim.py --port 5020              # a Modbus device to point a connector at
python tools/simulators/snmp_sim.py --port 10161               # an SNMP agent (community "public")
```

## Automatic testing (GitHub Actions)

Every pull request and every change to `main` runs `.github/workflows/ci.yml`:

| Job | What it proves |
|---|---|
| Linux | All tests, the browser test, and the full path simulator → connector → **real Mosquitto** → platform → **real PostgreSQL + TimescaleDB**; every database statement on TimescaleDB |
| Windows, macOS | All tests, and the double-click launcher starting the demo and receiving live values (`--self-test`) |
| SNMP interop | The SNMP connector against the real **net-snmp** agent (v1, v2c, v3 with SHA-256/SHA/MD5 + AES) and net-snmp's `snmptrap` / `snmpinform` |
| Docker | All container images build; each connector refuses to start without its settings |

Results: the **Actions** tab of the repository, and the checks at the bottom of each pull request.

## Rules for every change

- Follow `docs/connector-spec/CONNECTOR_SPEC.md`; schemas are generated by `docs/connector-spec/tools/gen_schemas.py`.
- No AGPL, BSL or non-commercial licences in this repository. GPL code only in separate connector repositories.
- Connectors never write to field devices (spec section 12). New request types need a spec change first.
