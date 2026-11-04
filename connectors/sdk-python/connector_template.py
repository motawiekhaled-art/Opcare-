"""Template for a new Python connector. Copy to connectors/<type>/connector.py and fill in the TODOs.

Shows the patterns the spec requires: one guard in the send layer (section 12), poll loop per scan
class with overrun handling (section 13), reconnect backoff with jitter, device states and
communication loss (section 7.2), and admin operations (section 11).
"""
import asyncio
import random
import time

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, PointTable, Q, ReadOnlyGuard, Sink, run


class TemplateConnector(Connector):
    type = "modbus"            # TODO: your type; must match manifest.json
    version = "0.1.0"
    capabilities = {"browse": False, "read_now": True}

    def __init__(self):
        self.tasks: list[asyncio.Task] = []
        self.sink: Sink | None = None
        self.guard: ReadOnlyGuard | None = None
        self.link = None       # TODO: protocol client object

    def devices(self, config):
        return [d["id"] for d in config["connection"].get("devices", [{"id": "main"}])]

    async def validate(self, config):
        errs = []
        ids = set(self.devices(config))
        for p in config["points"]:
            if p["address"].get("device", "main") not in ids:
                errs.append(f"{p['tag_id']}: unknown device {p['address'].get('device')}")
        return errs

    # ------------------------------------------------------------- send layer: EVERY request passes here
    async def _request(self, request_type: str, *args):
        self.guard.check(request_type)          # raises OP_FORBIDDEN + publishes guard.blocked
        # TODO: perform the protocol request with self.link and return the raw result
        raise NotImplementedError

    # ------------------------------------------------------------- lifecycle
    async def start(self, config, table: PointTable, sink: Sink):
        self.sink = sink
        self.guard = ReadOnlyGuard(self.type, ALLOW[self.type],
                                   on_block=lambda r: sink.event("guard.blocked", "critical",
                                                                 f"Blocked request {r}", attrs={"request": r}))
        self.config, self.table = config, table
        self.tasks = [asyncio.create_task(self._device_loop(dev)) for dev in self.devices(config)]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        # TODO: close self.link

    async def _device_loop(self, device: str):
        delay = 1.0
        while True:
            self.sink.device_state(device, "connecting")
            connected_at = None
            try:
                await self._connect(device)                      # TODO
                self.sink.device_state(device, "connected")
                connected_at = time.monotonic()
                await self._poll_forever(device)                 # returns or raises on link loss
                raise ConnectorError("CONN_CLOSED", "link closed")
            except ConnectorError as exc:
                self.sink.device_state(device, "unreachable", exc)
            except (OSError, asyncio.TimeoutError) as exc:
                self.sink.device_state(device, "unreachable", ConnectorError("CONN_TIMEOUT", str(exc)[:200]))
            if connected_at is not None and time.monotonic() - connected_at > 60:
                delay = 1.0                                       # reset after 60 s connected
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 60.0)

    async def _connect(self, device: str):
        raise ConnectorError("CONN_REFUSED", "TODO: implement _connect")

    async def _poll_forever(self, device: str):
        classes = {ms: [p for p in pts if p.device == device] for ms, pts in self.table.scan_classes().items()}
        await asyncio.gather(*(self._scan_class(ms, pts) for ms, pts in classes.items() if pts))

    async def _scan_class(self, scan_ms: int, points):
        period = scan_ms / 1000
        next_run = (time.time() // period + 1) * period          # align to wall-clock multiples
        while True:
            await asyncio.sleep(max(0.0, next_run - time.time()))
            started = time.time()
            for p in points:                                      # TODO: merge reads into blocks
                raw = await self._request("FC3", p.address)       # TODO: correct request type per table
                self.sink.value(p.tag_id, raw, Q.GOOD)
            next_run += period
            if time.time() > next_run:                            # overrun: start again at once, no queue
                self.sink.scan_overrun(scan_ms)
                next_run = time.time()

    # ------------------------------------------------------------- admin
    async def test_connection(self, config):
        # TODO: open a separate connection for each device in `config`, measure latency, close it
        return {"devices": [{"id": d, "ok": False, "error": {"code": "INTERNAL", "message": "TODO"}}
                            for d in self.devices(config)]}

    async def read_now(self, args):
        # TODO: read the given tag_ids/addresses once through self._request
        raise ConnectorError("OP_UNSUPPORTED", "TODO")


if __name__ == "__main__":
    run(TemplateConnector())
