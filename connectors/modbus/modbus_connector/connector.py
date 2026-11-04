"""Modbus TCP / RTU connector (spec OSS-DAQ-CON-001 section 15.1), built on opcare_connector_sdk."""
from __future__ import annotations

import asyncio
import random
import time

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, Point, PointTable, Q, ReadOnlyGuard, now_iso

from .decode import BIT_TABLES, Block, decode, plan, width
from .protocol import DeviceTimeout, ModbusException, RtuLink, TcpLink

NUMERIC = {"int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"}
TYPE_OK = {"bool": {"bool", "bit"}, "string": {"string"}, "int": NUMERIC, "float": NUMERIC, "enum": NUMERIC}


class ModbusConnector(Connector):
    type = "modbus"
    version = "1.0.0"
    capabilities = {"browse": False, "read_now": True}

    def __init__(self):
        self.sink = None
        self.link: TcpLink | RtuLink | None = None
        self.guard: ReadOnlyGuard | None = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.blocks: dict[int, list[Block]] = {}
        self.scan_tasks: dict[int, asyncio.Task] = {}
        self.main_task: asyncio.Task | None = None
        self.dev_state: dict[str, str] = {}
        self.dev_stats: dict[str, dict] = {}
        self._lost: asyncio.Event | None = None
        self._lost_error: ConnectorError | None = None

    # ---------------------------------------------------------------- configuration
    def devices(self, config: dict) -> list[str]:
        return [d["id"] for d in config["connection"]["devices"]]

    async def validate(self, config: dict) -> list[str]:
        conn, errs = config["connection"], []
        ids = [d["id"] for d in conn["devices"]]
        if len(set(ids)) != len(ids):
            errs.append("duplicate device id")
        units = [d["unit_id"] for d in conn["devices"]]
        if len(set(units)) != len(units):
            errs.append("two devices share a unit_id")
        max_regs = conn.get("max_registers_per_read", 100)
        for p in config["points"]:
            a, tag = p["address"], p["tag_id"]
            if a["device"] not in ids:
                errs.append(f"{tag}: unknown device '{a['device']}'")
            enc = a["encoding"]
            if a["table"] not in BIT_TABLES:
                if enc == "bool":
                    errs.append(f"{tag}: use encoding 'bit' (with 'bit') or a number type for registers")
                w = width(a)
                if w > max_regs:
                    errs.append(f"{tag}: needs {w} registers, more than max_registers_per_read {max_regs}")
                if a["address"] + w > 0x10000:
                    errs.append(f"{tag}: address range beyond 65535")
            if enc not in TYPE_OK[p["data_type"]]:
                errs.append(f"{tag}: encoding '{enc}' cannot be published as data_type '{p['data_type']}'")
        return errs

    def _make_link(self, conn: dict, guard: ReadOnlyGuard):
        if conn["transport"] == "tcp":
            return TcpLink(conn["host"], conn.get("port", 502), guard, conn.get("inter_request_delay_ms", 0))
        return RtuLink(conn["serial"], guard, conn.get("inter_request_delay_ms", 20))

    def _replan(self) -> None:
        conn = self.config["connection"]
        units = {d["id"]: d["unit_id"] for d in conn["devices"]}
        self.blocks = {ms: plan(pts, units, conn.get("max_registers_per_read", 100),
                                conn.get("max_bits_per_read", 1000), conn.get("max_gap", 10))
                       for ms, pts in self.table.scan_classes().items()}

    @property
    def timeout_s(self) -> float:
        return (self.config.get("acquisition") or {}).get("request_timeout_ms", 3000) / 1000

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self.guard = ReadOnlyGuard("modbus", ALLOW["modbus"], on_block=lambda r: sink.event(
            "guard.blocked", "critical", f"Blocked Modbus request {r}", attrs={"request": r}))
        self.link = self._make_link(config["connection"], self.guard)
        self.dev_state = {d: "connecting" for d in self.devices(config)}
        self.dev_stats = {d: {"requests": 0, "timeouts": 0, "exceptions": 0} for d in self.dev_state}
        self._replan()
        self.main_task = asyncio.create_task(self._run())

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        if self.devices(config) != self.devices(self.config):
            return False
        self.config, self.table = config, table
        self._replan()
        if self.link and self.link.connected:
            self._sync_scans()
        return True

    async def stop(self) -> None:
        tasks = [*self.scan_tasks.values(), *([self.main_task] if self.main_task else [])]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.scan_tasks, self.main_task = {}, None
        if self.link:
            await self.link.close()

    async def _run(self) -> None:
        delay = 1.0
        while True:
            connected_at = None
            try:
                await self.link.connect(self.timeout_s)
                connected_at = time.monotonic()
                self._lost, self._lost_error = asyncio.Event(), None
                self._sync_scans()
                await self._lost.wait()
                raise self._lost_error or ConnectorError("CONN_CLOSED", "link closed")
            except ConnectorError as exc:
                for d in self.dev_state:
                    self._device_down(d, exc)
            finally:
                for t in self.scan_tasks.values():
                    t.cancel()
                await asyncio.gather(*self.scan_tasks.values(), return_exceptions=True)
                self.scan_tasks = {}
                await self.link.close()
            if connected_at is not None and time.monotonic() - connected_at > 60:
                delay = 1.0
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 60.0)

    def _sync_scans(self) -> None:
        for ms in [m for m in self.scan_tasks if m not in self.blocks]:
            self.scan_tasks.pop(ms).cancel()
        for ms in self.blocks:
            if ms not in self.scan_tasks or self.scan_tasks[ms].done():
                self.scan_tasks[ms] = asyncio.create_task(self._scan(ms))

    def _link_lost(self, exc: ConnectorError) -> None:
        if self._lost and not self._lost.is_set():
            self._lost_error = exc
            self._lost.set()

    # ---------------------------------------------------------------- polling
    async def _scan(self, scan_ms: int) -> None:
        period = scan_ms / 1000
        next_run = time.time()
        while True:
            await asyncio.sleep(max(0.0, next_run - time.time()))
            down: set[str] = set()                      # devices that failed in this cycle: skip the rest
            for block in list(self.blocks.get(scan_ms, [])):
                if block.device in down:
                    continue
                try:
                    await self._read_block(block)
                except DeviceTimeout as exc:
                    down.add(block.device)
                    self.dev_stats[block.device]["timeouts"] += 1
                    self._device_down(block.device, ConnectorError("CONN_TIMEOUT", str(exc)))
                except ConnectorError as exc:
                    if exc.code in ("CONN_CLOSED", "PROTOCOL_ERROR"):
                        self._link_lost(exc)
                        return
                    raise
            next_run += period
            if time.time() > next_run:                  # overrun: run again at once, never queue up
                self.sink.scan_overrun(scan_ms)
                next_run = time.time()

    async def _read(self, unit: int, table: str, start: int, count: int, device: str) -> list[int]:
        self.dev_stats[device]["requests"] += 1
        return await self.link.read(unit, table, start, count, self.timeout_s)

    async def _read_block(self, block: Block, retry_busy: bool = True) -> None:
        if block.sub is not None:
            for sub in block.sub:
                await self._read_block(sub)
            return
        try:
            words = await self._read(block.unit_id, block.table, block.start, block.count, block.device)
        except ModbusException as exc:
            self.dev_stats[block.device]["exceptions"] += 1
            code = exc.code
            if code in (10, 11):                        # gateway cannot reach the device
                raise DeviceTimeout(str(exc)) from exc
            self._device_ok(block.device)               # the device answered, so it is reachable
            if code in (1, 2, 3, 4) and len(block.items) > 1:
                block.make_split()                      # isolate the bad address (spec 15.1)
                await self._read_block(block)
                return
            if code in (5, 6) and retry_busy:
                await asyncio.sleep(0.1)
                await self._read_block(block, retry_busy=False)
                return
            q = {1: Q.BAD_CONFIG_ERROR, 2: Q.BAD_CONFIG_ERROR, 3: Q.BAD_CONFIG_ERROR,
                 4: Q.BAD_DEVICE_FAILURE}.get(code, Q.BAD_COMM_FAILURE)
            for p, _, _ in block.items:
                self.sink.value(p.tag_id, None, q, flags=[f"EXC{code}"])
            return
        self._device_ok(block.device)
        for p, off, w in block.items:
            self.sink.value(p.tag_id, decode(p.address, words[off:off + w]), Q.GOOD)

    def _device_ok(self, device: str) -> None:
        if self.dev_state.get(device) != "connected":
            self.dev_state[device] = "connected"
            self.sink.device_state(device, "connected")
        elif hasattr(self.sink, "device_ok"):
            self.sink.device_ok(device)

    def _device_down(self, device: str, exc: ConnectorError) -> None:
        if self.dev_state.get(device) != "unreachable":
            self.dev_state[device] = "unreachable"
            self.sink.device_state(device, "unreachable", exc)

    # ---------------------------------------------------------------- admin operations
    async def test_connection(self, config: dict) -> dict:
        conn = config["connection"]
        guard = ReadOnlyGuard("modbus", ALLOW["modbus"])
        reuse = (conn["transport"] == "rtu" and self.link is not None and self.link.connected
                 and self.config.get("connection", {}).get("serial") == conn.get("serial"))
        link = self.link if reuse else self._make_link(conn, guard)   # a serial port cannot be opened twice
        timeout = (config.get("acquisition") or {}).get("request_timeout_ms", 3000) / 1000
        results = []
        try:
            if not reuse:
                try:
                    await link.connect(timeout)
                except ConnectorError as exc:
                    return {"devices": [{"id": d["id"], "ok": False, "error": exc.as_dict()} for d in conn["devices"]]}
            for d in conn["devices"]:
                probe = next((p["address"] for p in config["points"] if p["address"]["device"] == d["id"]),
                             {"table": "holding_register", "address": 0, "encoding": "uint16"})
                t0 = time.monotonic()
                entry = {"id": d["id"]}
                try:
                    await link.read(d["unit_id"], probe["table"], probe["address"], 1, timeout)
                    entry.update(ok=True)
                except ModbusException as exc:
                    entry.update(ok=exc.code not in (10, 11), info={"exception": exc.code})
                except DeviceTimeout as exc:
                    entry.update(ok=False, error={"code": "CONN_TIMEOUT", "message": str(exc)})
                except ConnectorError as exc:
                    entry.update(ok=False, error=exc.as_dict())
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                results.append(entry)
        finally:
            if not reuse:
                await link.close()
        return {"devices": results}

    async def read_now(self, args: dict) -> dict:
        if not (self.link and self.link.connected):
            raise ConnectorError("CONN_CLOSED", "not connected to the device")
        units = {d["id"]: d["unit_id"] for d in self.config["connection"]["devices"]}
        jobs: list[tuple[dict, Point]] = []
        for tag in args.get("tag_ids", []):
            p = self.table.points.get(tag)
            if p is None:
                raise ConnectorError("POINT_NOT_FOUND", f"unknown tag {tag}")
            jobs.append(({"t": tag}, p))
        for item in args.get("addresses", []):
            a = item["address"]
            if a.get("device") not in units:
                raise ConnectorError("ADDR_INVALID", f"unknown device {a.get('device')}")
            jobs.append(({"address": a}, Point("adhoc", a, item["data_type"], device=a["device"])))
        out = []
        for head, p in jobs:
            a = p.address
            try:
                words = await self.link.read(units[p.device], a["table"], a["address"], width(a), self.timeout_s)
                v, fq, flags = p.convert(decode(a, words))
                q = fq if fq is not None else Q.GOOD
            except ModbusException as exc:
                v, q = None, Q.BAD_DEVICE_FAILURE if exc.code == 4 else Q.BAD_CONFIG_ERROR
            except DeviceTimeout:
                v, q = None, Q.BAD_COMM_FAILURE
            out.append({**head, "v": v, "q": q, "ts": now_iso()})
        return {"values": out}

    async def diagnostics(self) -> dict:
        return {
            "link": {"connected": bool(self.link and self.link.connected),
                     "requests": self.link.requests if self.link else 0,
                     "last_latency_ms": self.link.last_latency_ms if self.link else None},
            "scan_classes": {str(ms): {"blocks": len(b), "split_blocks": sum(x.split for x in b),
                                       "points": sum(len(x.items) for x in b)} for ms, b in self.blocks.items()},
            "devices": {d: {"state": self.dev_state.get(d), **s} for d, s in self.dev_stats.items()},
        }
