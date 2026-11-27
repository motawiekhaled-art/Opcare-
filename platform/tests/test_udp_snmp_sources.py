"""UDP and SNMP sources through the platform: secrets API, check / test / browse before saving (edge agent),
save, live values and events; plus the configuration screens in a real browser (skipped without Playwright).

Run from the repository root:  python -m pytest platform/tests/test_udp_snmp_sources.py -q
"""
import asyncio
import json
import pathlib
import socket
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/udp", "connectors/snmp", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from snmp_connector import SnmpConnector  # noqa: E402
from snmp_sim import SnmpAgent, standard_mib, ups_mib  # noqa: E402
from test_config_screens import TOKENS, VALIDATOR, free_port, http  # noqa: E402
from udp_connector import UdpConnector  # noqa: E402


def free_udp() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """SNMP agent + UDP station + agent + platform + HTTP server, like the demo."""

    async def __aenter__(self):
        import run_demo
        self.secrets = pathlib.Path(tempfile.mkdtemp())
        self.snmp = SnmpAgent("public")
        standard_mib(self.snmp, "server-room-sw", "Test switch", interfaces=2)
        ups_mib(self.snmp)
        self.snmp_port = await self.snmp.start()
        self.udp_port = free_udp()
        self.station = asyncio.create_task(run_demo.drive_udp_station(self.udp_port))
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR, available_types={"udp", "snmp"}, secrets_dir=self.secrets)
        await self.platform.start()
        self.agent = LocalAgent(self.bus, {"udp": UdpConnector, "snmp": SnmpConnector}, VALIDATOR, secrets_dir=self.secrets)
        await self.agent.start()
        self.demo = {"site": "ps-west", "devices": [
            {"type": "snmp", "name": "Server room switch and UPS", "community": "public",
             "devices": [{"id": "server-room-sw", "host": "127.0.0.1", "port": self.snmp_port, "name": "Switch"}]},
            {"type": "udp", "name": "GNSS and weather station", "bind_port": self.udp_port,
             "senders": [{"id": "station", "host": "127.0.0.1", "name": "Station"}]}]}
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
        self.station.cancel()
        await self.platform.stop()
        self.snmp.stop()
        self.store.close()

    async def call(self, *a, **k):
        return await asyncio.to_thread(http, *a, **k)


def snmp_cfg(port: int) -> dict:
    return {"site": "ps-west", "source_id": "server-room", "type": "snmp", "name": "Server room", "enabled": True,
            "connection": {"version": "2c", "port": port, "community_ref": "server-room-community", "retries": 0,
                           "devices": [{"id": "server-room-sw", "host": "127.0.0.1"}]},
            "acquisition": {"default_scan_ms": 1000, "request_timeout_ms": 800},
            "points": [{"tag_id": "ps-west.server-room.sw.name", "address": {"device": "server-room-sw", "oid": "1.3.6.1.2.1.1.5.0"},
                        "data_type": "string"},
                       {"tag_id": "ps-west.server-room.ups.charge", "address": {"device": "server-room-sw", "oid": "1.3.6.1.2.1.33.1.2.4.0"},
                        "data_type": "int", "unit": "%"}]}


def udp_cfg(port: int) -> dict:
    return {"site": "ps-west", "source_id": "station", "type": "udp", "name": "Station", "enabled": True,
            "connection": {"bind_host": "127.0.0.1", "bind_port": port, "senders": [{"id": "station", "host": "127.0.0.1"}],
                           "syslog": {"min_severity": "warning"}},
            "acquisition": {"request_timeout_ms": 3000, "batch_max_ms": 200},
            "points": [{"tag_id": "ps-west.station.wind", "address": {"device": "station", "format": "json", "value_pointer": "/wind/speed"},
                        "data_type": "float", "unit": "m/s"},
                       {"tag_id": "ps-west.station.lat", "address": {"device": "station", "format": "nmea", "sentence": "RMC",
                                                                     "field": 3, "coordinate": True}, "data_type": "float"}]}


def test_secrets_check_test_browse_save_and_live_values():
    async def run():
        async with Stack() as s:
            # catalog: both connectors available, templates, secrets kept here
            _, cat = await s.call("GET", f"{s.base}/api/catalog", "t-view")
            assert cat["available"] == ["snmp", "udp"] and cat["secrets"] is True and cat["templates"]["udp"]

            # a referenced secret that was never stored is reported at check time
            status, res = await s.call("POST", f"{s.base}/api/sources-check", "t-eng", snmp_cfg(s.snmp_port))
            assert status == 422 and "is not stored yet" in res["error"]["details"][0]

            # secrets API: engineers store, nobody reads values back, names are checked
            assert (await s.call("PUT", f"{s.base}/api/secrets/server-room-community", "t-view", {"value": "public"}))[0] == 403
            assert (await s.call("PUT", f"{s.base}/api/secrets/bad%20name", "t-eng", {"value": "public"}))[0] == 400
            assert (await s.call("PUT", f"{s.base}/api/secrets/mqtt_password", "t-eng", {"value": "x"}))[0] == 400
            assert (await s.call("PUT", f"{s.base}/api/secrets/server-room-community", "t-eng", {"value": "public"}))[0] == 204
            _, names = await s.call("GET", f"{s.base}/api/secrets", "t-eng")
            assert names["names"] == ["server-room-community"] and "public" not in json.dumps(names)
            if sys.platform != "win32":
                assert (s.secrets / "server-room-community").stat().st_mode & 0o077 == 0     # owner-only file
            _, audit = await s.call("GET", f"{s.base}/api/audit", "t-admin")
            assert any(a["action"] == "store_secret" for a in audit) and "public" not in json.dumps(audit)

            # SNMP: test and browse before saving, through the edge agent
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", snmp_cfg(s.snmp_port))
            assert status == 200 and res["result"]["devices"][0]["info"]["sysName"] == "server-room-sw"
            status, res = await s.call("POST", f"{s.base}/api/sources-browse", "t-eng",
                                       {"config": snmp_cfg(s.snmp_port), "path": "server-room-sw/1.3.6.1.2.1.1", "max_items": 20})
            assert status == 200 and "sysName.0" in [i["name"] for i in res["result"]["items"]]

            # UDP: listen and discover before saving
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", udp_cfg(s.udp_port))
            assert status == 200 and res["result"]["devices"][0]["ok"]
            status, res = await s.call("POST", f"{s.base}/api/sources-browse", "t-eng",
                                       {"config": udp_cfg(s.udp_port), "listen_s": 3, "depth": 2})
            paths = [i["path"] for i in res["result"]["items"]]
            assert status == 200 and "127.0.0.1//wind/speed" in paths and "127.0.0.1/RMC.3" in paths

            # save both; values and events arrive
            for cfg in (snmp_cfg(s.snmp_port), udp_cfg(s.udp_port)):
                assert (await s.call("POST", f"{s.base}/api/sources", "t-eng", cfg))[0] == 201
            for _ in range(100):
                _, tags = await s.call("GET", f"{s.base}/api/tags", "t-view")
                good = {t["tag_id"]: t["v"] for t in tags if t["q"] == 192}
                if len(good) == 4:
                    break
                await asyncio.sleep(0.2)
            assert good["ps-west.server-room.sw.name"] == "server-room-sw" and 95 <= good["ps-west.server-room.ups.charge"] <= 100
            assert 29.9 < good["ps-west.station.lat"] < 30.1 and good["ps-west.station.wind"] >= 0

            # browse a running UDP source: uses its socket (no "port in use"), and test works on it too
            status, res = await s.call("POST", f"{s.base}/api/sources-test?mode=update&source_id=station", "t-eng", udp_cfg(s.udp_port))
            assert status == 200 and res["result"]["devices"][0]["ok"]
    asyncio.run(run())


def test_screens_create_snmp_and_udp_sources_in_a_browser():
    pw = pytest.importorskip("playwright.async_api")

    async def run():
        async with Stack() as s, pw.async_playwright() as p:
            browser = await p.chromium.launch()
            pg = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await pg.goto(f"{s.base}/?token=t-eng")
            await pg.wait_for_selector("#source-list")

            # SNMP: IT & Network > SNMP v1/v2c > demo settings (community typed for us) > test > templates > save
            await pg.goto(f"{s.base}/#new-source")
            await pg.click(".type:has-text('IT & Network')")
            await pg.click(".proto:has-text('SNMP v1 / v2c')")
            await pg.click("#next")
            await pg.locator(".demo-dev:has-text('Server room') button").click()
            await pg.click("#test-btn")
            await pg.wait_for_selector(".test-results .q-good", timeout=10000)
            assert "server-room-sw" in await pg.locator(".test-results").inner_text()
            assert (s.secrets / "server-room-switch-and-ups-community").read_text() == "public"
            await pg.click("#next")
            await pg.select_option("#tpl-sel", "ups")
            await pg.click("#tpl-add")
            await pg.click("#discover")
            await pg.wait_for_selector(".browse-list tr", timeout=10000)
            await pg.locator(".browse-list tr:has-text('sysName.0') button").click()
            assert await pg.locator(".tags-edit tbody tr").count() == 8
            await pg.click("#next")
            await pg.wait_for_selector(".ok-line", timeout=10000)
            await pg.click("#save")
            await pg.wait_for_selector("#view-live:not([hidden])")
            await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 8", timeout=15000)

            # UDP: IoT > UDP datagrams > demo settings > listen > template + discovered value > save
            await pg.goto(f"{s.base}/#new-source")
            await pg.click(".type:has-text('IoT Sensors')")
            await pg.click(".proto:has-text('UDP datagrams')")
            await pg.click("#next")
            await pg.locator(".demo-dev:has-text('GNSS') button").click()
            await pg.click("#test-btn")
            await pg.wait_for_selector(".test-results .q-good", timeout=10000)
            await pg.click("#next")
            await pg.select_option("#tpl-sel", "gnss-rmc")
            await pg.click("#tpl-add")
            await pg.click("#discover")
            await pg.wait_for_selector(".browse-list td.mono:text-is('/wind/speed')", timeout=20000)
            await pg.locator(".browse-list tr:has(td.mono:text-is('/wind/speed')) button").click()
            assert await pg.locator(".tags-edit tbody tr").count() == 6
            await pg.click("#next")
            await pg.wait_for_selector(".ok-line", timeout=10000)
            await pg.click("#save")
            await pg.wait_for_selector("#view-live:not([hidden])")
            await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 6", timeout=15000)

            # the sources list names the protocols
            await pg.goto(f"{s.base}/#sources")
            await pg.wait_for_selector("tr[data-source='gnss-and-weather-station']")
            text = await pg.locator("#sources-table").inner_text()
            assert "SNMP v2c" in text and f"UDP · port {s.udp_port}" in text
            assert errors == []
            await browser.close()
    asyncio.run(run())
