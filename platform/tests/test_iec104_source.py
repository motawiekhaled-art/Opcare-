"""IEC 104 sources through the platform: test, browse by interrogation, save, live values (spontaneous changes with
the station's time tags) and read_now through the edge agent on one connection; plus the configuration screens in a
real browser (skipped without Playwright).

Run from the repository root:  python -m pytest platform/tests/test_iec104_source.py -q
"""
import asyncio
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/iec104", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from iec104_connector import Iec104Connector  # noqa: E402
from test_config_screens import TOKENS, VALIDATOR, free_port, http  # noqa: E402

ALLOWED = {"STARTDT_ACT", "STOPDT_ACT", "TESTFR_ACT", "TESTFR_CON", "S", "C_IC_NA_1", "C_CI_NA_1", "C_RD_NA_1"}


class Stack:
    """Substation gateway (IEC 104 station) + agent + platform + HTTP server, like the demo."""

    async def __aenter__(self):
        import run_demo
        self.st = run_demo.substation_gateway(0)
        await self.st.start()
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR, available_types={"iec104"},
                                 secrets_dir=pathlib.Path(tempfile.mkdtemp()))
        await self.platform.start()
        self.agent = LocalAgent(self.bus, {"iec104": Iec104Connector}, VALIDATOR, secrets_dir=self.platform.secrets_dir)
        await self.agent.start()
        self.demo = {"site": "ps-west", "devices": [
            {"type": "iec104", "name": "Substation 66/11 kV gateway", "host": "127.0.0.1", "port": self.st.port,
             "common_addresses": [1], "device_time_zone": "UTC", "description": "substation gateway"}]}
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
        await self.st.stop()
        self.store.close()

    async def call(self, *a, **k):
        return await asyncio.to_thread(http, *a, **k)


def gateway_cfg(port: int) -> dict:
    pts = [("kv", 1001, "M_ME_NC_1", "float"), ("mw", 1011, "M_ME_NC_1", "float"), ("q0", 2001, "M_DP_NA_1", "enum"),
           ("trip", 2102, "M_SP_NA_1", "bool"), ("tap", 2201, "M_ST_NA_1", "int"), ("kwh", 3001, "M_IT_NA_1", "int")]
    return {"site": "ps-west", "source_id": "sub66", "name": "Substation 66/11 kV gateway", "type": "iec104", "enabled": True,
            "connection": {"hosts": [{"host": "127.0.0.1", "port": port}], "common_addresses": [1], "t1_s": 5, "t2_s": 2, "t3_s": 10},
            "acquisition": {"batch_max_ms": 200, "integrity_s": 0, "gi_period_s": 0, "counter_interrogation_period_s": 60},
            "points": [{"tag_id": f"ps-west.sub66.{n}", "address": {"ca": 1, "ioa": ioa, "type": t}, "data_type": d}
                       for n, ioa, t, d in pts]}


def test_test_browse_save_live_values_and_read_now():
    async def run():
        async with Stack() as s:
            _, cat = await s.call("GET", f"{s.base}/api/catalog", "t-view")
            assert cat["available"] == ["iec104"]
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", gateway_cfg(s.st.port))
            dev = res["result"]["devices"][0]
            assert status == 200 and dev["ok"] and dev["info"]["objects"]["Short float"] == 6
            status, res = await s.call("POST", f"{s.base}/api/sources-browse", "t-eng", {"config": gateway_cfg(s.st.port)})
            items = res["result"]["items"]
            assert status == 200 and {"1/1001", "1/2001", "1/3001"} <= {i["path"] for i in items}
            assert (await s.call("POST", f"{s.base}/api/sources", "t-eng", gateway_cfg(s.st.port)))[0] == 201
            for _ in range(100):
                _, tags = await s.call("GET", f"{s.base}/api/tags", "t-view")
                good = {t["tag_id"]: t["v"] for t in tags if t["q"] == 192}
                if len(good) == 6:
                    break
                await asyncio.sleep(0.2)
            assert good["ps-west.sub66.kv"] == pytest.approx(66.1) and good["ps-west.sub66.q0"] == 2
            assert good["ps-west.sub66.tap"] == 9 and good["ps-west.sub66.kwh"] == 1_284_400
            t = (datetime.now(timezone.utc) + timedelta(seconds=1)).replace(microsecond=250000)   # newer than the GI values
            s.st.set(1, 2001, 1, t=t)                                     # incomer opens, with the station's time tag
            for _ in range(50):
                _, tags = await s.call("GET", f"{s.base}/api/tags?source=sub66", "t-view")
                q0 = {x["tag_id"]: x for x in tags}["ps-west.sub66.q0"]
                if q0["v"] == 1:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"spontaneous breaker change did not arrive: {q0}")
            assert q0["ts"] == t.strftime("%Y-%m-%dT%H:%M:%S.250Z")
            status, res = await s.call("POST", f"{s.base}/api/sources/sub66/admin/read_now", "t-eng",
                                       {"addresses": [{"address": {"ca": 1, "ioa": 1013}, "data_type": "float"}]})
            assert status == 200 and res["result"]["values"][0]["v"] == pytest.approx(50.0)
            status, res = await s.call("POST", f"{s.base}/api/sources-test?mode=update&source_id=sub66", "t-eng", gateway_cfg(s.st.port))
            assert status == 200 and res["result"]["devices"][0]["ok"]
            assert set(s.st.requests) <= ALLOWED, set(s.st.requests)
    asyncio.run(run())


def test_screens_create_iec104_source_in_a_browser():
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
            await pg.click(".proto:has-text('IEC 60870-5-104')")
            await pg.click("#next")
            await pg.locator(".demo-dev:has-text('Substation 66/11') button").click()
            assert await pg.input_value("#f-cas") == "1"
            await pg.click("#test-btn")
            await pg.wait_for_selector(".test-results .q-good", timeout=10000)
            assert "common address 1" in await pg.locator(".test-results").inner_text()
            await pg.click("#next")
            await pg.click("#discover")
            await pg.wait_for_selector(".browse-list tr", timeout=10000)
            await pg.click("button[data-add-group='1/DP']")               # both breakers
            await pg.locator(".browse-list tbody tr:not(.group):has-text('IOA 1011') button:has-text('Add')").click()
            assert await pg.locator(".tags-edit tbody tr").count() == 3
            assert (await pg.input_value("#p-tag-0")).endswith(".dp-1-2001")
            await pg.click("#next")
            await pg.wait_for_selector(".ok-line", timeout=10000)
            assert "general interrogation on connect" in await pg.locator("dl.summary").inner_text()
            await pg.click("#save")
            await pg.wait_for_selector("#view-live:not([hidden])")
            await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 3", timeout=15000)
            await pg.goto(f"{s.base}/#sources")
            await pg.wait_for_selector("tr[data-source='substation-66-11-kv-gateway']")
            assert "IEC 104 · 127.0.0.1" in await pg.locator("#sources-table").inner_text()
            assert errors == []
            await browser.close()
    asyncio.run(run())
