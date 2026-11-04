"""Central platform tests. Run from the repository root:  python -m pytest platform/tests -q

test_full_stack runs the Modbus simulator, the real Modbus connector in the SDK runtime, the platform
services, SQLite and the HTTP API (uvicorn on a free port) in one event loop and drives everything
through real HTTP calls, the live stream (Server-Sent Events) and simulated faults.
"""
import asyncio
import json
import pathlib
import socket
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "connectors/sdk-python", "connectors/modbus", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus, topic_matches  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store, iso_to_ms, ms_to_iso  # noqa: E402
from modbus_connector import ModbusConnector  # noqa: E402
from modbus_sim import ModbusSimulator  # noqa: E402
from opcare_connector_sdk import Env, JsonLog, Runtime, SchemaValidator  # noqa: E402
from opcare_connector_sdk.testing import find_schemas  # noqa: E402

VALIDATOR = SchemaValidator(find_schemas())


# ===================================================================== bus and store
def test_topic_matching_and_retained():
    assert topic_matches("opcare/v1/+/data/+", "opcare/v1/s/data/x")
    assert not topic_matches("opcare/v1/+/data/+", "opcare/v1/s/data/x/y")
    assert topic_matches("opcare/#", "opcare/v1/s") and not topic_matches("a/+", "a")
    bus, got = InMemoryBus(), []
    bus.publish("a/b", "1", retain=True)
    bus.subscribe("a/+", lambda t, p: got.append((t, p)))
    assert got == [("a/b", b"1")]
    bus.publish("a/b", b"", retain=True)                 # empty retained message clears it
    assert bus.retained("a/b") is None
    c = bus.client()
    c.set_will("a/will", "gone", 1, True)
    c.crash()
    assert bus.retained("a/will") == b"gone"


def test_time_conversion():
    assert ms_to_iso(iso_to_ms("2026-11-03T18:02:11.250Z")) == "2026-11-03T18:02:11.250Z"


def _val(t, v, ts, q=192):
    return {"t": t, "v": v, "q": q, "ts": ts, "rx": ts}


def test_store_values_latest_history_seq_events_audit():
    st = Store("sqlite://:memory:")
    cfg = {"spec": "1.0", "revision": 1, "site": "s1", "source_id": "src", "type": "modbus", "name": "n", "enabled": True,
           "connection": {}, "points": [{"tag_id": "s1.a", "data_type": "float", "unit": "kW", "address": {}},
                                        {"tag_id": "s1.b", "data_type": "string", "address": {}},
                                        {"tag_id": "s1.c", "data_type": "bool", "address": {}}]}
    st.save_source(cfg, "khaled")
    st.write_values([_val("s1.a", 1.5, "2026-11-03T18:00:01.000Z"), _val("s1.b", "ABC", "2026-11-03T18:00:01.000Z"),
                     _val("s1.c", True, "2026-11-03T18:00:01.000Z")])
    st.write_values([_val("s1.a", 2.5, "2026-11-03T18:00:03.000Z")])
    st.write_values([_val("s1.a", 9.9, "2026-11-03T18:00:02.000Z")])         # late, older value: history only
    st.write_values([_val("s1.a", 2.5, "2026-11-03T18:00:03.000Z")])         # duplicate: ignored
    latest = {t["tag_id"]: t for t in st.list_tags()}
    assert latest["s1.a"]["v"] == 2.5 and latest["s1.b"]["v"] == "ABC" and latest["s1.c"]["v"] is True
    assert latest["s1.a"]["unit"] == "kW"
    hist = st.history("s1.a", 0, 10 ** 14)
    assert [h["v"] for h in hist] == [1.5, 9.9, 2.5]
    assert st.check_seq("src", "boot-1", 1) == 0 and st.check_seq("src", "boot-1", 4) == 2
    assert st.check_seq("src", "boot-2", 1) == 0 and st.seq_stats()["src"]["missing_batches"] == 2
    st.save_event({"id": "01JBX3Q7K2M8V5T9N4R6C1D0EF", "src": "src", "type": "conn.lost", "severity": "medium",
                   "ts": "2026-11-03T18:00:00.000Z", "message": "m"})
    assert st.events()[0]["type"] == "conn.lost"
    st.audit("khaled", "create_source", "src", "revision 1")
    assert st.audit_log()[0]["action"] == "create_source"
    assert st.delete_source("src") == 1 and st.list_tags() == []
    st.close()


# ===================================================================== full stack over HTTP
TOKENS = {"t-admin": {"user": "khaled", "role": "admin"}, "t-eng": {"user": "eng.ahmed", "role": "engineer"},
          "t-view": {"user": "control-room", "role": "viewer"}}


def http(method, url, token=None, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def read_sse(url, want: int, timeout=10):
    """Read SSE data messages until `want` messages of type 'values' have arrived."""
    got = []
    with urllib.request.urlopen(url, timeout=timeout) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r:
            line = line.decode().strip()
            if line.startswith("data: ") and line != "data: {}":
                msg = json.loads(line[6:])
                if msg["type"] == "values":
                    got.append(msg)
                    if len(got) >= want:
                        return got
    return got


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def source_cfg(sim_port, extra=()):
    hr = lambda d, a, enc: {"device": d, "table": "holding_register", "address": a, "encoding": enc}  # noqa: E731
    return {"site": "ss-test", "source_id": "modbus-mcc-01", "type": "modbus", "name": "MCC-01",
            "connection": {"transport": "tcp", "host": "127.0.0.1", "port": sim_port,
                           "devices": [{"id": "m1", "unit_id": 1}, {"id": "m2", "unit_id": 2}]},
            "acquisition": {"default_scan_ms": 300, "batch_max_ms": 100, "request_timeout_ms": 400},
            "points": [{"tag_id": "ss-test.m1.p", "data_type": "float", "unit": "kW", "address": hr("m1", 0, "float32")},
                       {"tag_id": "ss-test.m1.v", "data_type": "float", "unit": "V", "scaling": {"gain": 0.1, "offset": 0},
                        "address": hr("m1", 2, "uint16")},
                       {"tag_id": "ss-test.m2.p", "data_type": "float", "unit": "kW", "address": hr("m2", 0, "float32")},
                       *extra]}


def test_full_stack():
    asyncio.run(_full_stack())


async def _full_stack():
    sim = ModbusSimulator()
    sim.set_value(1, "holding_register", 0, ">f", 412.5)
    sim.set_regs(1, "holding_register", 2, [2305])
    sim.set_value(2, "holding_register", 0, ">f", 180.25)
    sim_port = await sim.start()

    bus, tmp = InMemoryBus(), pathlib.Path(tempfile.mkdtemp())
    store = Store(f"sqlite:///{tmp / 'daq.db'}")
    platform = Platform(store, bus, VALIDATOR)
    await platform.start()
    (tmp / "pw").write_text("x")
    env = Env({"OPCARE_SITE": "ss-test", "OPCARE_EDGE_ID": "e1", "OPCARE_SOURCE_ID": "modbus-mcc-01",
               "OPCARE_MQTT_URL": "mqtt://bus", "OPCARE_MQTT_USERNAME": "u", "OPCARE_MQTT_PASSWORD_FILE": str(tmp / "pw"),
               "OPCARE_SECRETS_DIR": str(tmp)})
    client = bus.client()
    runtime = Runtime(ModbusConnector(), env=env, client=client, validator=VALIDATOR, log=JsonLog("m", "error"))
    conn_task = asyncio.create_task(runtime.main())

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(platform, TOKENS), host="127.0.0.1", port=port,
                                           log_level="error", access_log=False))
    srv_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    call = lambda *a, **k: asyncio.to_thread(http, *a, **k)  # noqa: E731

    async def until(check, timeout=8, what=""):
        for _ in range(int(timeout / 0.1)):
            if await check():
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out: {what}")

    async def tags():
        return {t["tag_id"]: t for t in (await call("GET", f"{base}/api/tags", "t-view"))[1]}

    async def state():
        return (await call("GET", f"{base}/api/sources", "t-view"))[1][0]["state"]

    try:
        # auth and roles
        status, health = await call("GET", f"{base}/api/health")
        assert status == 200 and "login_hint" not in health          # only the demo may offer a token
        assert (await call("GET", f"{base}/api/sources"))[0] == 401
        assert (await call("POST", f"{base}/api/sources", "t-view", source_cfg(sim_port)))[0] == 403

        # invalid configuration is rejected with the schema errors
        bad = source_cfg(sim_port)
        bad["points"][0]["address"]["encoding"] = "float99"
        status, body = await call("POST", f"{base}/api/sources", "t-eng", bad)
        assert status == 422 and any("float99" in d for d in body["error"]["details"])

        # create -> connector receives config, values flow to the API
        status, doc = await call("POST", f"{base}/api/sources", "t-eng", source_cfg(sim_port))
        assert status == 201 and doc["revision"] == 1
        assert (await call("POST", f"{base}/api/sources", "t-eng", source_cfg(sim_port)))[0] == 409
        await until(lambda: _all_good(tags), what="first values")
        t = await tags()
        assert t["ss-test.m1.p"]["v"] == 412.5 and t["ss-test.m1.v"]["v"] == 230.5 and t["ss-test.m2.p"]["v"] == 180.25
        await until(lambda: _eq(state, "running"), what="running")

        # live stream delivers new values
        sse = asyncio.to_thread(read_sse, f"{base}/api/live?token=t-view", 1)
        await asyncio.sleep(0.3)
        sim.set_value(1, "holding_register", 0, ">f", 500.0)
        msgs = await sse
        assert any(v["t"] == "ss-test.m1.p" and v["v"] == 500.0 for m in msgs for v in m["values"])

        # history
        status, hist = await call("GET", f"{base}/api/tags/ss-test.m1.p/history?minutes=5", "t-view")
        assert status == 200 and [h["v"] for h in hist][-2:] == [412.5, 500.0]

        # admin relay: test_connection through the API
        status, res = await call("POST", f"{base}/api/sources/modbus-mcc-01/admin/test_connection", "t-eng", {})
        assert status == 200 and [d["ok"] for d in res["result"]["devices"]] == [True, True]
        assert (await call("POST", f"{base}/api/sources/modbus-mcc-01/admin/test_connection", "t-view", {}))[0] == 403

        # update: tag owned by another source is refused; a valid update reaches the connector as revision 2
        sim.set_regs(1, "holding_register", 3, [42])
        upd = source_cfg(sim_port, extra=[{"tag_id": "ss-test.m1.new", "data_type": "int",
                                           "address": {"device": "m1", "table": "holding_register", "address": 3,
                                                       "encoding": "uint16"}}])
        status, doc = await call("PUT", f"{base}/api/sources/modbus-mcc-01", "t-eng", upd)
        assert status == 200 and doc["revision"] == 2
        await until(lambda: _tag_value(tags, "ss-test.m1.new", 42), what="new tag after update")

        # fault: meter 2 silent -> its tag Bad (no comms), source degraded, event recorded
        sim.offline.add(2)
        await until(lambda: _tag_q(tags, "ss-test.m2.p", 24), what="unit 2 bad quality")
        await until(lambda: _eq(state, "degraded"), what="degraded")
        status, evs = await call("GET", f"{base}/api/events", "t-view")
        assert any(e["type"] == "conn.lost" for e in evs)
        sim.offline.discard(2)
        await until(lambda: _eq(state, "running"), what="recovered")

        # connector process dies -> broker Last Will -> source offline
        conn_task.cancel()
        client.crash()
        await until(lambda: _eq(state, "offline"), what="offline after crash")
        status, res = await call("POST", f"{base}/api/sources/modbus-mcc-01/admin/ping?timeout_ms=500", "t-view", {})
        assert status == 504

        # delete (admin only), retained config cleared, audit trail complete
        assert (await call("DELETE", f"{base}/api/sources/modbus-mcc-01", "t-eng"))[0] == 403
        assert (await call("DELETE", f"{base}/api/sources/modbus-mcc-01", "t-admin"))[0] == 204
        assert bus.retained("opcare/v1/ss-test/config/modbus-mcc-01") is None
        assert (await call("GET", f"{base}/api/sources", "t-view"))[1] == []
        actions = [a["action"] for a in (await call("GET", f"{base}/api/audit", "t-admin"))[1]]
        assert actions[:3] == ["delete_source", "update_source", "create_source"]
        status, stats = await call("GET", f"{base}/api/stats", "t-view")
        assert stats["ingest"]["missing_batches"] == 0 and stats["ingest"]["rejected"] == 0
    finally:
        server.should_exit = True
        await asyncio.gather(srv_task, return_exceptions=True)
        runtime.request_stop()
        await asyncio.gather(conn_task, return_exceptions=True)
        await platform.stop()
        await sim.stop()
        store.close()


async def _all_good(tags):
    t = await tags()
    return len(t) == 3 and all(x["q"] == 192 for x in t.values())


async def _eq(fn, want):
    return await fn() == want


async def _tag_value(tags, tag, want):
    return (await tags()).get(tag, {}).get("v") == want


async def _tag_q(tags, tag, want):
    return (await tags()).get(tag, {}).get("q") == want


def test_gui_in_browser():
    """Loads the real page in Chromium against the demo stack: no script errors, live rows, quality badges."""
    pytest.importorskip("playwright")
    asyncio.run(_gui())


async def _gui():
    from playwright.async_api import async_playwright
    import run_demo
    sim = ModbusSimulator()
    sim_port = await sim.start()
    drive = asyncio.create_task(run_demo.drive_simulator(sim, faults=False))
    bus, store = InMemoryBus(), Store("sqlite://:memory:")
    platform = Platform(store, bus, VALIDATOR)
    await platform.start()
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "pw").write_text("x")
    env = Env({"OPCARE_SITE": run_demo.SITE, "OPCARE_EDGE_ID": "e", "OPCARE_SOURCE_ID": run_demo.SOURCE,
               "OPCARE_MQTT_URL": "mqtt://bus", "OPCARE_MQTT_USERNAME": "u", "OPCARE_MQTT_PASSWORD_FILE": str(tmp / "pw"),
               "OPCARE_SECRETS_DIR": str(tmp)})
    rt = Runtime(ModbusConnector(), env=env, client=bus.client(), validator=VALIDATOR, log=JsonLog("m", "error"))
    rt_task = asyncio.create_task(rt.main())
    await platform.save_source(run_demo.demo_config(sim_port), "test", create=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(platform, TOKENS), host="127.0.0.1", port=port, log_level="error"))
    srv = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch()
            page = await b.new_page(viewport={"width": 1280, "height": 800})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await page.goto(f"http://127.0.0.1:{port}/?token=t-view")
            await page.wait_for_function("document.querySelectorAll('#tag-rows tr').length === 13", timeout=10000)
            await page.wait_for_function("document.getElementById('live-text').textContent === 'Live'", timeout=5000)
            await page.wait_for_function("document.querySelectorAll('.q-good').length === 13", timeout=10000)
            assert "token" not in page.url                       # token removed from the address bar
            await page.locator("#tag-rows tr").first.click()
            await page.wait_for_selector("#chart svg, #chart p", timeout=5000)
            await page.fill("#filter", "tr1")
            assert await page.locator("#tag-rows tr:not([hidden])").count() == 2
            assert errors == []
            await b.close()
    finally:
        server.should_exit = True
        await asyncio.gather(srv, return_exceptions=True)
        rt.request_stop()
        await asyncio.gather(rt_task, return_exceptions=True)
        drive.cancel()
        await platform.stop()
        await sim.stop()
        store.close()
