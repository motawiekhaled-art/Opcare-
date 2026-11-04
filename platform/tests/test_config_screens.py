"""Configuration API and screens: catalog, check without saving, test before saving (through the edge
agent), and the full create / read / edit / disable / delete flow in a real browser.

Run from the repository root:  python -m pytest platform/tests/test_config_screens.py -q
"""
import asyncio
import json
import pathlib
import socket
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/modbus", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from modbus_connector import ModbusConnector  # noqa: E402
from modbus_sim import ModbusSimulator  # noqa: E402
from opcare_connector_sdk import SchemaValidator  # noqa: E402
from opcare_connector_sdk.testing import find_schemas  # noqa: E402

VALIDATOR = SchemaValidator(find_schemas())
TOKENS = {"t-admin": {"user": "khaled", "role": "admin"}, "t-eng": {"user": "eng.ahmed", "role": "engineer"},
          "t-view": {"user": "control-room", "role": "viewer"}}


def http(method, url, token=None, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """Simulated pump station + agent + platform + HTTP server, like the demo."""

    async def __aenter__(self):
        import run_demo
        self.sim = ModbusSimulator()
        self.sim_port = await self.sim.start()
        self.drive = asyncio.create_task(run_demo.drive_pump_station(self.sim))
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR, available_types={"modbus"})
        await self.platform.start()
        self.agent = LocalAgent(self.bus, {"modbus": ModbusConnector}, VALIDATOR)
        await self.agent.start()
        self.demo = {"site": "ps-west", "devices": [{"name": "Pump Station West", "device_id": "pump-plc", "host": "127.0.0.1",
                                                    "port": self.sim_port, "unit_id": 1, "tags": run_demo.PUMP_TAGS}]}
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.platform, TOKENS, demo_info=self.demo),
                                                    host="127.0.0.1", port=self.port, log_level="error"))
        self.srv = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await asyncio.gather(self.srv, return_exceptions=True)
        await self.agent.stop()
        self.drive.cancel()
        await self.platform.stop()
        await self.sim.stop()
        self.store.close()

    async def call(self, *a, **k):
        return await asyncio.to_thread(http, *a, **k)


def pump_cfg(port, sid="pump-station-west"):
    return {"site": "ps-west", "source_id": sid, "type": "modbus", "name": "Pump Station West",
            "connection": {"transport": "tcp", "host": "127.0.0.1", "port": port, "devices": [{"id": "pump-plc", "unit_id": 1}]},
            "acquisition": {"default_scan_ms": 500, "request_timeout_ms": 1000},
            "points": [{"tag_id": f"ps-west.{sid}.flow", "data_type": "float", "unit": "m3/h",
                        "address": {"device": "pump-plc", "table": "holding_register", "address": 0, "encoding": "float32"}}]}


def test_catalog_check_and_test_before_save():
    asyncio.run(_api())


async def _api():
    async with Stack() as s:
        status, cat = await s.call("GET", f"{s.base}/api/catalog", "t-view")
        assert status == 200 and cat["available"] == ["modbus"] and cat["demo"]["site"] == "ps-west"
        energy = next(t for t in cat["types"] if t["id"] == "energy")
        st = {p["name"]: p["status"] for p in energy["protocols"]}
        assert st["Modbus TCP"] == "available" and st["IEC 61850 MMS"] == "specified"
        assert {p["status"] for t in cat["types"] if t["id"] == "bms" for p in t["protocols"]} == {"planned"}

        # check without saving: valid, invalid with schema details, unavailable connector, viewer refused
        status, res = await s.call("POST", f"{s.base}/api/sources-check", "t-eng", pump_cfg(s.sim_port))
        assert status == 200 and res["config"]["revision"] == 1
        assert (await s.call("GET", f"{s.base}/api/sources", "t-view"))[1] == []          # nothing saved
        bad = pump_cfg(s.sim_port)
        bad["points"][0]["address"]["table"] = "coil"
        status, res = await s.call("POST", f"{s.base}/api/sources-check", "t-eng", bad)
        assert status == 422 and res["error"]["details"][0].startswith("points/0/address/encoding")
        opcua = {**pump_cfg(s.sim_port), "type": "opcua",
                 "connection": {"endpoint_url": "opc.tcp://h:4840", "security_policy": "None", "security_mode": "None"},
                 "points": [{"tag_id": "ps-west.x", "data_type": "float", "address": {"node_id": "ns=2;s=X"}}]}
        status, res = await s.call("POST", f"{s.base}/api/sources-check", "t-eng", opcua)
        assert status == 422 and res["error"]["code"] == "CONNECTOR_UNAVAILABLE"
        assert (await s.call("POST", f"{s.base}/api/sources-check", "t-view", pump_cfg(s.sim_port)))[0] == 403

        # test before save, through the agent
        status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", pump_cfg(s.sim_port))
        assert status == 200 and res["result"]["devices"][0]["ok"]
        status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", pump_cfg(1))
        # nothing listens on port 1: Linux/macOS refuse at once, Windows retries until the timeout
        assert status == 200 and res["result"]["devices"][0]["error"]["code"] in ("CONN_REFUSED", "CONN_TIMEOUT")
        bad_dev = pump_cfg(s.sim_port)
        bad_dev["points"][0]["address"]["device"] = "nope"
        status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", bad_dev)
        assert status == 502 and res["error"]["code"] == "CFG_INVALID"

        # save -> the agent starts the connector -> values; update check uses mode=update
        assert (await s.call("POST", f"{s.base}/api/sources", "t-eng", pump_cfg(s.sim_port)))[0] == 201
        for _ in range(60):
            await asyncio.sleep(0.1)
            tags = (await s.call("GET", f"{s.base}/api/tags?source=pump-station-west", "t-view"))[1]
            if tags and tags[0]["q"] == 192:
                break
        assert tags[0]["q"] == 192 and tags[0]["v"] > 100
        status, res = await s.call("POST", f"{s.base}/api/sources-check?mode=update&source_id=pump-station-west", "t-eng",
                                   pump_cfg(s.sim_port))
        assert status == 200 and res["config"]["revision"] == 2
        status, res = await s.call("POST", f"{s.base}/api/sources-check", "t-eng", pump_cfg(s.sim_port))
        assert status == 409                                                            # create again: exists


def test_configuration_screens_in_browser():
    pytest.importorskip("playwright")
    asyncio.run(_browser())


async def _browser():
    from playwright.async_api import async_playwright
    async with Stack() as s, async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(f"{s.base}/?token=t-admin#/sources")
        await pg.wait_for_selector("#sources-table tbody tr")
        assert await pg.locator("#view-live").is_hidden()

        # create through the wizard with the demo device
        await pg.click("#new-source")
        await pg.wait_for_selector(".type-list")
        assert await pg.locator(".proto:has-text('IEC 61850 MMS')").is_disabled()          # not installed yet
        await pg.click(".proto:has-text('Modbus TCP')")
        await pg.click("#next")
        await pg.click(".demo-box button")
        await pg.click("#test-btn")
        await pg.wait_for_selector(".test-results .q-good", timeout=10000)
        await pg.click("#next")
        await pg.click("#demo-tags")
        assert await pg.locator(".tags-edit tbody tr").count() == 7
        await pg.click("#next")
        await pg.wait_for_selector(".ok-line", timeout=10000)
        await pg.click("#save")
        await pg.wait_for_selector("#view-live:not([hidden])")
        await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 7", timeout=15000)

        # edit: read a value, change a unit, save revision 2
        await pg.goto(f"{s.base}/#/sources/edit/pump-station-west")
        await pg.wait_for_selector(".step2")
        await pg.click("#next")
        await pg.locator(".read-cell button").first.click()
        await pg.wait_for_function("/^\\d/.test(document.querySelector('.read-cell button').textContent)", timeout=8000)
        await pg.fill("#p-unit-0", "m3/hour")
        await pg.click("#next")
        await pg.wait_for_selector(".ok-line")
        await pg.click("#save")
        await pg.wait_for_selector("#view-live:not([hidden])")
        cfg = (await s.call("GET", f"{s.base}/api/sources/pump-station-west", "t-view"))[1]["config"]
        assert cfg["revision"] == 2 and cfg["points"][0]["unit"] == "m3/hour" and cfg["acquisition"]["default_scan_ms"] == 1000

        # an invalid tag is shown against its row and blocks saving
        await pg.goto(f"{s.base}/#/sources/edit/pump-station-west")
        await pg.wait_for_selector(".step2")
        await pg.click("#next")
        await pg.fill("#p-tag-0", "Not A Valid Tag!")
        await pg.click("#next")
        await pg.wait_for_selector(".errors")
        assert await pg.locator("#save").is_disabled()
        await pg.click("a:has-text('Cancel')")

        # disable, enable, delete from the list
        await pg.wait_for_selector("tr[data-source='pump-station-west']")
        await pg.locator("tr[data-source='pump-station-west'] button:has-text('Disable')").click()
        await pg.wait_for_selector("tr[data-source='pump-station-west'] button:has-text('Enable')")
        assert (await s.call("GET", f"{s.base}/api/sources/pump-station-west", "t-view"))[1]["config"]["enabled"] is False
        await pg.locator("tr[data-source='pump-station-west'] button:has-text('Enable')").click()
        await pg.wait_for_selector("tr[data-source='pump-station-west'] button:has-text('Disable')")
        await pg.locator("tr[data-source='pump-station-west'] button:has-text('Delete')").click()
        await pg.locator("tr[data-source='pump-station-west'] .btn.danger").click()
        await pg.wait_for_function("!document.querySelector(\"tr[data-source='pump-station-west']\")", timeout=5000)
        assert (await s.call("GET", f"{s.base}/api/sources", "t-view"))[1] == []
        await asyncio.sleep(0.5)
        assert s.agent.running == {}

        # a viewer sees the list but cannot change anything
        await pg.goto(f"{s.base}/?token=t-view#/sources")
        await pg.wait_for_selector("#sources-table")
        assert await pg.locator("#new-source").is_hidden()
        # the only expected error: the browser logs the 422 answer to the deliberately invalid tag above
        assert [e for e in errors if "status of 422" not in e] == [], errors
        await b.close()
