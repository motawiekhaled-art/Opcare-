"""Modbus TCP device simulator for connector tests and demos (Opcare DAQ, Phase 0).

Records every request (unit, function code, start, quantity) so the conformance kit can prove that a
connector only ever sends reads (CT-13). Supports several unit ids, missing addresses (strict mode),
forced exceptions, silent (offline) units, response delay and stop/start for communication-loss tests.

    python tools/simulators/modbus_sim.py --port 5020 --map registers.json
    registers.json: {"1": {"holding_register": {"0": 1234}, "coil": {"0": 1}}}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct

TABLES = {1: "coil", 2: "discrete_input", 3: "holding_register", 4: "input_register"}


class ModbusSimulator:
    def __init__(self, strict: bool = True):
        self.units: dict[int, dict[str, dict[int, int]]] = {}
        self.strict = strict                     # unknown address -> exception 2 (like real devices)
        self.offline: set[int] = set()           # units that never answer
        self.exceptions: dict[tuple, int] = {}   # (unit, table, address) -> exception code
        self.delay_s = 0.0
        self.log: list[tuple[int, int, int, int]] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self._conns: set[asyncio.StreamWriter] = set()
        self.accepted = 0                       # TCP connections accepted (detects reconnects)

    # ------------------------------------------------------------ data helpers
    def unit(self, uid: int) -> dict:
        return self.units.setdefault(uid, {t: {} for t in TABLES.values()})

    def set_regs(self, uid: int, table: str, address: int, regs: list[int]) -> None:
        for i, r in enumerate(regs):
            self.unit(uid)[table][address + i] = r & 0xFFFF

    def set_value(self, uid: int, table: str, address: int, fmt: str, value, word_order: str = "big") -> None:
        raw = struct.pack(fmt, value)
        regs = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        self.set_regs(uid, table, address, regs[::-1] if word_order == "little" else regs)

    def set_bit(self, uid: int, table: str, address: int, value: bool) -> None:
        self.unit(uid)[table][address] = int(bool(value))

    def function_codes(self) -> set[int]:
        return {fc for _, fc, _, _ in self.log}

    # ------------------------------------------------------------ server
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, host, port or self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            for w in list(self._conns):
                w.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        self.accepted += 1
        try:
            while True:
                head = await reader.readexactly(7)
                tid, _, length, uid = struct.unpack(">HHHB", head)
                pdu = await reader.readexactly(length - 1)
                fc = pdu[0]
                start, qty = struct.unpack(">HH", pdu[1:5]) if len(pdu) >= 5 else (0, 0)
                self.log.append((uid, fc, start, qty))
                if uid in self.offline:
                    continue
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                resp = self._respond(uid, fc, start, qty)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, uid) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._conns.discard(writer)
            writer.close()

    def _respond(self, uid: int, fc: int, start: int, qty: int) -> bytes:
        if fc not in TABLES:                         # any write or other function: illegal function
            return bytes([fc | 0x80, 1])
        table = TABLES[fc]
        data = self.unit(uid)[table]
        for a in range(start, start + qty):
            code = self.exceptions.get((uid, table, a))
            if code:
                return bytes([fc | 0x80, code])
            if self.strict and a not in data:
                return bytes([fc | 0x80, 2])
        values = [data.get(a, 0) for a in range(start, start + qty)]
        if fc in (1, 2):
            out = bytearray((qty + 7) // 8)
            for i, v in enumerate(values):
                if v:
                    out[i // 8] |= 1 << (i % 8)
            return bytes([fc, len(out)]) + bytes(out)
        return bytes([fc, 2 * qty]) + struct.pack(f">{qty}H", *values)


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--map", help="JSON register map {unit: {table: {address: value}}}")
    ap.add_argument("--lenient", action="store_true", help="unknown addresses read as 0 instead of exception 2")
    args = ap.parse_args()
    sim = ModbusSimulator(strict=not args.lenient)
    if args.map:
        for uid, tables in json.load(open(args.map)).items():
            for table, regs in tables.items():
                for addr, val in regs.items():
                    sim.unit(int(uid))[table][int(addr)] = int(val)
    port = await sim.start(args.host, args.port)
    print(f"Modbus simulator listening on {args.host}:{port}")
    try:
        await asyncio.Event().wait()
    finally:
        print("function codes received:", sorted(sim.function_codes()))


if __name__ == "__main__":
    asyncio.run(_main())
