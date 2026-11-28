"""IEC 61850 sources through the platform: test, browse to the report control blocks, save with reports, live values
and read_now through the edge agent on one association; plus the configuration screens in a real browser (skipped
without Playwright).

Run from the repository root:  python -m pytest platform/tests/test_iec61850_source.py -q
"""
import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/iec61850", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from iec61850_connector import Iec61850Connector  # noqa: E402
from test_config_screens import TOKENS, VALIDATOR, free_port, http  # noqa: E402

LD = "FDR21LD0"


class Stack:
    """Feeder 21 relay (IEC 61850 server) + agent + platform + HTTP server, like the demo."""

    async def __aenter__(self):
        import run_demo
        self.ied = run_demo.feeder_relay(0)
        await self.ied.start()
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR, available_types={"iec61850"},
                                 secrets_dir=pathlib.Path(tempfile.mkdtemp()))
        await self.platform.start()
        self.agent = LocalAgent(self.bus, {"iec61850": Iec61850Connector}, VALIDATOR, secrets_dir=self.platform.secrets_dir)
        await self.agent.start()
        self.demo = {"site": "ps-west", "devices": [
            {"type": "iec61850", "name": "Feeder 21 relay", "host": "127.0.0.1", "port": self.ied.port, "ied_name": "FDR21",
             "description": "protection relay"}]}
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
        await self.ied.stop()
        self.store.close()

    async def call(self, *a, **k):
        return await asyncio.to_thread(http, *a, **k)


def relay_cfg(port: int) -> dict:
    pts = [("p", "MMXU1.TotW.mag.f", "MX", "float"), ("cb", "XCBR1.Pos.stVal", "ST", "enum"), ("trip", "PTOC1.Op.general", "ST", "bool"),
           ("ia", "MMXU1.A.phsA.cVal.mag.f", "MX", "float"), ("ops", "XCBR1.OpCnt.stVal", "ST", "int")]
    return {"site": "ps-west", "source_id": "fdr21", "name": "Feeder 21 relay", "type": "iec61850", "enabled": True,
            "connection": {"host": "127.0.0.1", "port": port, "ied_name": "FDR21", "timeout_ms": 3000},
            "acquisition": {"default_scan_ms": 500, "batch_max_ms": 200, "integrity_s": 0,
                            "reports": [{"rcb_ref": f"{LD}/LLN0.BR.brcbEV", "buffered": True},
                                        {"rcb_ref": f"{LD}/LLN0.RP.urcbMX", "buffered": False}]},
            "points": [{"tag_id": f"ps-west.fdr21.{n}", "address": {"object_ref": f"{LD}/{r}", "fc": fc}, "data_type": d}
                       for n, r, fc, d in pts]}


def test_test_browse_save_reports_live_values_and_read_now():
    async def run():
        async with Stack() as s:
            _, cat = await s.call("GET", f"{s.base}/api/catalog", "t-view")
            assert cat["available"] == ["iec61850"]
            status, res = await s.call("POST", f"{s.base}/api/sources-check", "t-eng", relay_cfg(s.ied.port))
            assert status == 200, res                       # object_ref / rcb_ref are addresses, not secrets
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", relay_cfg(s.ied.port))
            dev = res["result"]["devices"][0]
            assert status == 200 and dev["ok"] and dev["info"]["logical_devices"] == [LD]
            status, res = await s.call("POST", f"{s.base}/api/sources-browse", "t-eng", {"config": relay_cfg(s.ied.port), "path": f"{LD}/LLN0"})
            assert status == 200 and {i["name"] for i in res["result"]["items"] if i["kind"] == "report"} == {"brcbEV", "urcbMX"}
            assert (await s.call("POST", f"{s.base}/api/sources", "t-eng", relay_cfg(s.ied.port)))[0] == 201
            for _ in range(100):
                _, tags = await s.call("GET", f"{s.base}/api/tags", "t-view")
                good = {t["tag_id"]: t["v"] for t in tags if t["q"] == 192}
                if len(good) == 5:
                    break
                await asyncio.sleep(0.2)
            assert 3.0 < good["ps-west.fdr21.p"] < 3.5 and good["ps-west.fdr21.cb"] == 2 and good["ps-west.fdr21.ops"] >= 842
            s.ied.set(f"{LD}/XCBR1.Pos.stVal", "ST", [False, True])
            for _ in range(50):
                _, tags = await s.call("GET", f"{s.base}/api/tags?source=fdr21", "t-view")
                if {t["tag_id"]: t["v"] for t in tags}["ps-west.fdr21.cb"] == 1:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("breaker report did not arrive")
            status, res = await s.call("POST", f"{s.base}/api/sources/fdr21/admin/read_now", "t-eng",
                                       {"addresses": [{"address": {"object_ref": f"{LD}/LLN0.NamPlt.vendor", "fc": "DC"}, "data_type": "string"}]})
            assert status == 200 and res["result"]["values"][0]["v"] == "Opcare"
            status, res = await s.call("POST", f"{s.base}/api/sources-test?mode=update&source_id=fdr21", "t-eng", relay_cfg(s.ied.port))
            assert status == 200 and res["result"]["devices"][0]["ok"]
            writes = {r for r in s.ied.requests if r.startswith(("WRITE", "Write"))}
            assert writes <= {f"WRITE_RCB:{a}" for a in ("Resv", "ResvTms", "RptEna", "GI", "TrgOps", "IntgPd", "BufTm", "OptFlds", "EntryID")}
    asyncio.run(run())


def test_screens_create_iec61850_source_in_a_browser():
    pw = pytest.importorskip("playwright.async_api")

    async def run():
        async with Stack() as s, pw.async_playwright() as p:
            browser = await p.chromium.launch()
            pg = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))

            async def open_row(name):
                await pg.locator(f".browse-list tr.group:has(b:text-matches('{name}$')) button:has-text('Open')").first.click()
                await pg.wait_for_function("!document.querySelector('#discover').disabled", timeout=10000)
            await pg.goto(f"{s.base}/?token=t-eng")
            await pg.wait_for_selector("#source-list")
            await pg.goto(f"{s.base}/#new-source")
            await pg.click(".type:has-text('Energy')")
            await pg.click(".proto:has-text('IEC 61850 MMS')")
            await pg.click("#next")
            await pg.locator(".demo-dev:has-text('Feeder 21') button").click()
            await pg.click("#test-btn")
            await pg.wait_for_selector(".test-results .q-good", timeout=10000)
            await pg.click("#next")
            await pg.click("#discover")
            await pg.wait_for_selector(".browse-list tr", timeout=10000)
            await open_row(LD)
            await open_row("LLN0")
            await pg.click(f"button[data-use-report='{LD}/LLN0.BR.brcbEV']")
            await pg.locator(f".crumbs button:has-text('{LD}')").click()
            await pg.wait_for_function("!document.querySelector('#discover').disabled")
            await open_row("XCBR1")
            await open_row("Pos")
            await pg.locator(".browse-list tbody tr:not(.group) button:has-text('Add')").first.click()
            assert await pg.locator(".tags-edit tbody tr").count() == 1
            await pg.click("#next")
            await pg.wait_for_selector(".ok-line", timeout=10000)
            assert "1 report(s): LLN0.BR.brcbEV" in await pg.locator("dl.summary").inner_text()
            await pg.click("#save")
            await pg.wait_for_selector("#view-live:not([hidden])")
            await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 1", timeout=15000)
            await pg.goto(f"{s.base}/#sources")
            await pg.wait_for_selector("tr[data-source='feeder-21-relay']")
            assert "IEC 61850 · FDR21" in await pg.locator("#sources-table").inner_text()
            assert errors == []
            await browser.close()
    asyncio.run(run())
