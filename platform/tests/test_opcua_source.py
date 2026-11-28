"""OPC UA sources through the platform: client certificate API (key never leaves the secrets folder), test before
saving with the server-certificate trust flow, browse, save and live values; plus the configuration screens in a real
browser (skipped without Playwright).

Run from the repository root:  python -m pytest platform/tests/test_opcua_source.py -q
"""
import asyncio
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/opcua", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from edge_agent import LocalAgent  # noqa: E402
from opcua_connector import OpcUaConnector  # noqa: E402
from test_config_screens import TOKENS, VALIDATOR, free_port, http  # noqa: E402

NS = "urn:opcare:demo:bottling-line"


class Stack:
    """Bottling line OPC UA server + agent + platform + HTTP server, like the demo."""

    async def __aenter__(self):
        import run_demo
        self.secrets = pathlib.Path(tempfile.mkdtemp())
        self.ua = run_demo.bottling_line_server(0)
        await self.ua.start()
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR, available_types={"opcua"}, secrets_dir=self.secrets)
        await self.platform.start()
        self.agent = LocalAgent(self.bus, {"opcua": OpcUaConnector}, VALIDATOR, secrets_dir=self.secrets)
        await self.agent.start()
        self.demo = {"site": "ps-west", "devices": [
            {"type": "opcua", "name": "Bottling line 1 PLC", "endpoint_url": self.ua.url, "security_policy": "Basic256Sha256",
             "security_mode": "SignAndEncrypt", "thumbprint": self.ua.thumbprint, **run_demo.OPCUA_DEMO}]}
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
        await self.ua.stop()
        self.store.close()

    async def call(self, *a, **k):
        return await asyncio.to_thread(http, *a, **k)


def ua_cfg(url: str, trusted=None) -> dict:
    return {"site": "ps-west", "source_id": "line1", "name": "Bottling line 1", "type": "opcua", "enabled": True,
            "connection": {"endpoint_url": url, "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
                           "auth": "username", "username": "operator", "password_ref": "line1-password",
                           "client_cert_ref": "opcua-client.cert.pem", "client_key_ref": "opcua-client.key.pem",
                           "trusted_server_thumbprints": trusted or []},
            "acquisition": {"default_scan_ms": 1000, "request_timeout_ms": 3000, "publishing_interval_ms": 200,
                            "batch_max_ms": 200, "integrity_s": 0},
            "points": [{"tag_id": "ps-west.line1.filler-speed", "address": {"node_id": f"nsu={NS};s=Line1.Filler.Speed"},
                        "data_type": "float", "unit": "bph"},
                       {"tag_id": "ps-west.line1.filler-running", "address": {"node_id": f"nsu={NS};s=Line1.Filler.Running"},
                        "data_type": "bool"},
                       {"tag_id": "ps-west.line1.bottles", "address": {"node_id": f"nsu={NS};s=Line1.Filler.BottleCount"},
                        "data_type": "int"}]}


def test_certificate_api_trust_flow_browse_save_and_live_values():
    async def run():
        async with Stack() as s:
            _, cat = await s.call("GET", f"{s.base}/api/catalog", "t-view")
            assert cat["available"] == ["opcua"]

            # client certificate: viewers may not create it; GET does not create; POST creates once, then reuses
            assert (await s.call("POST", f"{s.base}/api/opcua-certificate", "t-view", {}))[0] == 403
            _, res = await s.call("GET", f"{s.base}/api/opcua-certificate", "t-eng")
            assert res["exists"] is False and not (s.secrets / "opcua-client.cert.pem").exists()
            status, cert = await s.call("POST", f"{s.base}/api/opcua-certificate", "t-eng", {})
            assert status == 200 and cert["created"] and cert["application_uri"] == "urn:opcare:daq:opcua-client"
            assert cert["cert_ref"] == "opcua-client.cert.pem" and "PRIVATE KEY" not in json.dumps(cert)
            assert "PRIVATE KEY" in (s.secrets / "opcua-client.key.pem").read_text()
            _, again = await s.call("POST", f"{s.base}/api/opcua-certificate", "t-eng", {})
            assert again["created"] is False and again["thumbprint"] == cert["thumbprint"]
            _, newer = await s.call("POST", f"{s.base}/api/opcua-certificate", "t-eng", {"regenerate": True})
            assert newer["created"] and newer["thumbprint"] != cert["thumbprint"]
            assert (await s.call("POST", f"{s.base}/api/opcua-certificate", "t-eng", {"name": "../x"}))[0] == 400
            _, names = await s.call("GET", f"{s.base}/api/secrets", "t-eng")
            assert {"opcua-client.cert.pem", "opcua-client.key.pem"} <= set(names["names"])
            _, audit = await s.call("GET", f"{s.base}/api/audit", "t-admin")
            assert sum(a["action"] == "create_opcua_certificate" for a in audit) == 2
            assert (await s.call("PUT", f"{s.base}/api/secrets/line1-password", "t-eng", {"value": "OpcUa@2026"}))[0] == 204

            # test before saving: the untrusted server certificate is shown, then trusted
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", ua_cfg(s.ua.url))
            dev = res["result"]["devices"][0]
            assert status == 200 and dev["ok"] is False and dev["error"]["code"] == "TLS_FAILED"
            assert dev["info"]["server_thumbprint"] == s.ua.thumbprint
            status, res = await s.call("POST", f"{s.base}/api/sources-test", "t-eng", ua_cfg(s.ua.url, [s.ua.thumbprint]))
            dev = res["result"]["devices"][0]
            assert dev["ok"] and dev["info"]["product"] == "Opcare demo · Bottling line 1 PLC"

            # browse before saving
            status, res = await s.call("POST", f"{s.base}/api/sources-browse", "t-eng",
                                       {"config": ua_cfg(s.ua.url, [s.ua.thumbprint]), "path": f"nsu={NS};s=Line1/Filler"})
            assert status == 200
            assert {i["name"]: i["address"]["node_id"] for i in res["result"]["items"]}["Speed"] == f"nsu={NS};s=Line1.Filler.Speed"

            # save; values arrive with Good quality
            assert (await s.call("POST", f"{s.base}/api/sources", "t-eng", ua_cfg(s.ua.url, [s.ua.thumbprint])))[0] == 201
            for _ in range(100):
                _, tags = await s.call("GET", f"{s.base}/api/tags", "t-view")
                good = {t["tag_id"]: t["v"] for t in tags if t["q"] == 192}
                if len(good) == 3:
                    break
                await asyncio.sleep(0.2)
            assert good["ps-west.line1.bottles"] >= 1_250_400 and isinstance(good["ps-west.line1.filler-running"], bool)

            # read_now on the running source
            status, res = await s.call("POST", f"{s.base}/api/sources/line1/admin/read_now", "t-eng",
                                       {"addresses": [{"address": {"node_id": f"nsu={NS};s=Line1.Oven.Setpoint"}, "data_type": "float"}]})
            assert status == 200 and res["result"]["values"][0]["v"] == 185.0
            assert set(s.ua.requests) <= {"GetEndpoints", "OpenSecureChannel", "CreateSession", "ActivateSession", "Read",
                                          "Browse", "BrowseNext", "CreateSubscription", "CreateMonitoredItems", "Publish",
                                          "CloseSession", "CloseSecureChannel"}
    asyncio.run(run())


def test_screens_create_opcua_source_in_a_browser():
    pw = pytest.importorskip("playwright.async_api")

    async def run():
        async with Stack() as s, pw.async_playwright() as p:
            browser = await p.chromium.launch()
            pg = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await pg.goto(f"{s.base}/?token=t-eng")
            await pg.wait_for_selector("#source-list")

            # Industrial PLC / SCADA > OPC UA > demo settings > certificate made > test > trust > test ok
            await pg.goto(f"{s.base}/#new-source")
            await pg.click(".type:has-text('Industrial PLC')")
            await pg.click(".proto:has-text('OPC UA')")
            await pg.click("#next")
            await pg.locator(".demo-dev:has-text('Bottling') button").click()
            await pg.wait_for_selector(".cert-box:has-text('urn:opcare:daq:opcua-client')", timeout=15000)
            await pg.click("#test-btn")
            await pg.wait_for_selector("#trust-btn", timeout=15000)
            assert s.ua.thumbprint[:2] in await pg.locator(".trust").inner_text()
            await pg.click("#trust-btn")
            await pg.wait_for_selector(".test-results .q-good", timeout=15000)
            assert "Basic256Sha256/SignAndEncrypt" in await pg.locator(".test-results").inner_text()

            # tags: browse Line1 > Filler, add all, then one from ShrinkOven; save; live values
            await pg.click("#next")
            await pg.click("#discover")
            await pg.locator(".browse-list tr:has-text('Line1') button:has-text('Open')").click()
            await pg.locator(".browse-list tr:has-text('Filler') button:has-text('Open')").click()
            await pg.click("#add-all")
            await pg.locator(".crumbs button:has-text('Line1')").click()
            await pg.locator(".browse-list tr:has-text('ShrinkOven') button:has-text('Open')").click()
            await pg.locator(".browse-list tr:has(td.mono:text-is('Temperature')) button").click()
            assert await pg.locator(".tags-edit tbody tr").count() == 5
            await pg.click("#next")
            await pg.wait_for_selector(".ok-line", timeout=10000)
            await pg.click("#save")
            await pg.wait_for_selector("#view-live:not([hidden])")
            await pg.wait_for_function("[...document.querySelectorAll('#tag-rows tr:not([hidden]) .q-good')].length === 5", timeout=20000)
            await pg.goto(f"{s.base}/#sources")
            await pg.wait_for_selector("tr[data-source='bottling-line-1-plc']")
            assert "OPC UA · 127.0.0.1" in await pg.locator("#sources-table").inner_text()
            assert errors == []
            await browser.close()
    asyncio.run(run())
