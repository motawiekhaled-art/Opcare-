"""Integration tests with a REAL Mosquitto broker and a REAL PostgreSQL/TimescaleDB server.

Skipped unless both are configured (GitHub Actions provides them, see .github/workflows/ci.yml):
    OPCARE_TEST_MQTT_URL = mqtt://localhost:1883
    OPCARE_TEST_PG_DSN   = postgresql://postgres:postgres@localhost:5432/opcare_ci

Path tested: Modbus simulator -> Modbus connector (SDK runtime, PahoClient) -> Mosquitto -> platform
(PahoBus) -> PostgreSQL via psycopg; configuration and admin requests travel the other way.
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "connectors/sdk-python", "connectors/modbus", "tools/simulators"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402

MQTT_URL = os.environ.get("OPCARE_TEST_MQTT_URL")
PG_DSN = os.environ.get("OPCARE_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not (MQTT_URL and PG_DSN), reason="needs a real broker and PostgreSQL (CI)")


def test_real_broker_and_postgres():
    asyncio.run(_scenario())


async def _scenario():
    from daq_platform.bus import PahoBus
    from daq_platform.services import Platform
    from daq_platform.store import Store
    from modbus_connector import ModbusConnector
    from modbus_sim import ModbusSimulator
    from opcare_connector_sdk import Env, JsonLog, Runtime, SchemaValidator
    from opcare_connector_sdk.testing import find_schemas

    site = f"ci-{uuid.uuid4().hex[:8]}"          # unique topics, so reruns never see old retained messages
    source = "modbus-ci"
    validator = SchemaValidator(find_schemas())

    sim = ModbusSimulator()
    sim.set_value(1, "holding_register", 0, ">f", 321.5)
    sim.set_regs(1, "holding_register", 2, [2311])
    sim_port = await sim.start()

    store = Store(PG_DSN)
    bus = PahoBus(MQTT_URL, f"platform-{site}", "ci", "ci")
    platform = Platform(store, bus, validator)
    await platform.start()

    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "pw").write_text("ci")
    env = Env({"OPCARE_SITE": site, "OPCARE_EDGE_ID": "edge-ci", "OPCARE_SOURCE_ID": source,
               "OPCARE_MQTT_URL": MQTT_URL, "OPCARE_MQTT_USERNAME": "ci",
               "OPCARE_MQTT_PASSWORD_FILE": str(tmp / "pw"), "OPCARE_SECRETS_DIR": str(tmp)})
    runtime = Runtime(ModbusConnector(), env=env, validator=validator, log=JsonLog(source, "info"))
    conn_task = asyncio.create_task(runtime.main())

    async def until(check, timeout=20, what=""):
        for _ in range(int(timeout / 0.25)):
            if await check():
                return
            await asyncio.sleep(0.25)
        raise AssertionError(f"timed out: {what}")

    async def tags():
        return {t["tag_id"]: t for t in await store.run(store.list_tags, source)}

    try:
        await asyncio.sleep(1.0)                 # let both MQTT clients finish connecting
        cfg = {"site": site, "source_id": source, "type": "modbus", "name": "CI meters",
               "connection": {"transport": "tcp", "host": "127.0.0.1", "port": sim_port,
                              "devices": [{"id": "m1", "unit_id": 1}]},
               "acquisition": {"default_scan_ms": 500, "batch_max_ms": 200, "request_timeout_ms": 1000},
               "points": [
                   {"tag_id": f"{site}.m1.p", "data_type": "float", "unit": "kW",
                    "address": {"device": "m1", "table": "holding_register", "address": 0, "encoding": "float32"}},
                   {"tag_id": f"{site}.m1.v", "data_type": "float", "unit": "V", "scaling": {"gain": 0.1, "offset": 0},
                    "address": {"device": "m1", "table": "holding_register", "address": 2, "encoding": "uint16"}}]}
        await platform.save_source(cfg, "ci", create=True)

        async def values_arrived():
            t = await tags()
            return t.get(f"{site}.m1.p", {}).get("v") == 321.5 and t.get(f"{site}.m1.v", {}).get("v") == 231.1
        await until(values_arrived, what="values in PostgreSQL through Mosquitto")

        async def running():
            return any(s["source_id"] == source and s["state"] == "running" for s in await store.run(store.list_sources))
        await until(running, what="running status through Mosquitto")

        sim.set_value(1, "holding_register", 0, ">f", 400.0)
        await until(lambda: _value(tags, f"{site}.m1.p", 400.0), what="changed value")
        hist = await store.run(store.history, f"{site}.m1.p", 0, 10 ** 14, 100)
        assert [h["v"] for h in hist][-2:] == [321.5, 400.0]

        res = await platform.admin(source, "test_connection", {}, "ci", 5000)
        assert res["ok"] and res["result"]["devices"][0]["ok"]

        # TimescaleDB: readings must be a hypertable when the extension is available
        def hypertable():
            if not store._q("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"):
                return None
            return bool(store._q("SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'readings'"))
        ht = await store.run(hypertable)
        assert ht in (None, True), "TimescaleDB is installed but readings is not a hypertable"
        print("TimescaleDB hypertable:", ht)

        # a second Store on the same database applies the schema again without errors (restart)
        Store(PG_DSN).close()

        await platform.delete_source(source, "ci")
        assert source not in [s["source_id"] for s in await store.run(store.list_sources)]
        assert platform.stats["rejected"] == 0 and platform.stats["missing_batches"] == 0
    finally:
        runtime.request_stop()
        await asyncio.gather(conn_task, return_exceptions=True)
        await platform.stop()
        bus.close()
        await sim.stop()
        store.close()


async def _value(tags, tag, want):
    return (await tags()).get(tag, {}).get("v") == want
