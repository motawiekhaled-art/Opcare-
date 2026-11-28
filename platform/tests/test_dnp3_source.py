"""DNP3 sources through the platform: test, read points, save, live values and read_now through the edge agent,
with one connection to the outstation; plus the configuration screens in a real browser (skipped without Playwright).

Run from the repository root:  python -m pytest platform/tests/test_dnp3_source.py -q
"""
import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/dnp3", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from dnp3_connector import Dnp3Connector  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from test_config_screens import TOKENS, VALIDATOR, free_port, http  # noqa: E402


class Stack:
    """Feeder RTU outstation + agent + platform + HTTP server, like the demo."""

    async def __aenter__(self):
        import run_demo
        self.rtu = run_demo.feeder_rtu(0)
        await self.rtu.start()
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR, available_types={"dnp3"},
                                 secrets_dir=pathlib.Path(tempfile.mkdtemp()))
        await self.platform.start()
        self.agent = LocalAgent(self.bus, {"dnp3": Dnp3Connector}, VALIDATOR, secrets_dir=self.platform.secrets_dir)
        await self.agent.start()
        self.demo = {"site": "ps-west", "devices": [
            {"type": "dnp3", "name": "Feeder 3 RTU", "host": "127.0.0.1", "port": self.rtu.port, "outstation_address": 10,
             "master_address": 1, "description": "11 kV feeder"}]}
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
        await self.platform.stop()
        await self.rtu.stop()
        self.store.close()

    async def call(self, *a, **k):
        return await asyncio.to_thread(http, *a, **k)


def rtu_cfg(port: int) -> dict:
    pts = [("ia", "analog_input", 0, "float", "A"), ("p", "analog_input", 6, "float", "MW"), ("trip", "binary_input", 0, "bool", None),
           ("cb", "double_bit_binary_input", 0, "enum", None), ("kwh", "counter", 0, "int", "kWh")]
    return {"site": "ps-west", "source_id": "feeder3", "name": "Feeder 3 RTU", "type": "dnp3", "enabled": True,
            "connection": {"host": "127.0.0.1", "port": port, "master_address": 1, "outstation_address": 10,
                           "response_timeout_ms": 2000, "keep_alive_s": 30},
            "acquisition": {"default_scan_ms": 1000, "batch_max_ms": 200, "integrity_s": 0, "integrity_period_s": 0,
                            "class_polls": {"class1_ms": 500, "class2_ms": 500, "class3_ms": 1000}},
            "points": [{"tag_id": f"ps-west.feeder3.{n}", "address": {"group": g, "index": i}, "data_type": d, **({"unit": u} if u else {})}
                       for n, g, i, d, u in pts]}


def test_test_browse_save_live_values_and_read_now():
    async def run():
        async with Stack() as s:
            _, cat = await s.call("GET", f"{s.base}/api/catalog", "t-view")
            assert cat["available"] == ["dnp3"]
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", rtu_cfg(s.rtu.port))
            dev = res["result"]["devices"][0]
            assert status == 200 and dev["ok"] and dev["info"]["points"]["Analog input"] == 10
            status, res = await s.call("POST", f"{s.base}/api/sources-browse", "t-eng", {"config": rtu_cfg(s.rtu.port), "path": "counter"})
            assert status == 200 and [i["path"] for i in res["result"]["items"]] == ["counter", "counter/0", "counter/1"]
            assert (await s.call("POST", f"{s.base}/api/sources", "t-eng", rtu_cfg(s.rtu.port)))[0] == 201
            for _ in range(100):
                _, tags = await s.call("GET", f"{s.base}/api/tags", "t-view")
                good = {t["tag_id"]: t["v"] for t in tags if t["q"] == 192}
                if len(good) == 5:
                    break
                await asyncio.sleep(0.2)
            assert 100 < good["ps-west.feeder3.ia"] < 260 and good["ps-west.feeder3.cb"] == 2 and good["ps-west.feeder3.kwh"] >= 8_412_650
            connections = s.rtu.connections
            status, res = await s.call("POST", f"{s.base}/api/sources/feeder3/admin/read_now", "t-eng",
                                       {"addresses": [{"address": {"group": "analog_input", "index": 9}, "data_type": "float"}]})
            assert status == 200 and 49.9 < res["result"]["values"][0]["v"] < 50.1
            status, res = await s.call("POST", f"{s.base}/api/sources-test?mode=update&source_id=feeder3", "t-eng", rtu_cfg(s.rtu.port))
            assert status == 200 and res["result"]["devices"][0]["ok"]
            assert s.rtu.connections == connections                     # the running association was reused
            assert set(s.rtu.requests) <= {"READ", "CONFIRM", "WRITE"} and not s.rtu.restart
    asyncio.run(run())


def test_screens_create_dnp3_source_in_a_browser():
    pw = pytest.importorskip("playwright.async_api")

    async def run():
        async with Stack() as s, pw.async_playwright() as p:
            browser = await p.chromium.launch()
            pg = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await pg.goto(f"{s.base}/?token=t-eng")
            await pg.wait_for_selector("#source-list")
            await pg.goto(f"{s.base}/#new-source")
            await pg.click(".type:has-text('Energy')")
            await pg.click(".proto:has-text('DNP3')")
            await pg.click("#next")
            await pg.locator(".demo-dev:has-text('Feeder 3') button").click()
            await pg.click("#test-btn")
            await pg.wait_for_selector(".test-results .q-good", timeout=10000)
            assert "outstation 10" in await pg.locator(".test-results").inner_text()
            await pg.click("#next")
            await pg.click("#discover")
            await pg.click("button[data-add-group='double_bit_binary_input']")
            await pg.locator(".browse-list tr:has(td.mono:text-is('Analog input 6')) button").click()
            assert await pg.locator(".tags-edit tbody tr").count() == 3
            await pg.click("#next")
            await pg.wait_for_selector(".ok-line", timeout=10000)
            await pg.click("#save")
            await pg.wait_for_selector("#view-live:not([hidden])")
            await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 3", timeout=15000)
            await pg.goto(f"{s.base}/#sources")
            await pg.wait_for_selector("tr[data-source='feeder-3-rtu']")
            assert "DNP3 · 127.0.0.1" in await pg.locator("#sources-table").inner_text()
            assert errors == []
            await browser.close()
    asyncio.run(run())
