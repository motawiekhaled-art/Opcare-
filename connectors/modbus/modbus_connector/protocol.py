"""Read-only Modbus client: TCP (MBAP) and RTU (CRC-16) framing for function codes 1-4 ONLY.

There is deliberately no code here that can build a write request (spec OSS-DAQ-CON-001 section 12).
Every request also passes the ReadOnlyGuard before it is sent.
"""
from __future__ import annotations

import asyncio
import struct
import time

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

FC = {"coil": 1, "discrete_input": 2, "holding_register": 3, "input_register": 4}
MAX_QTY = {1: 2000, 2: 2000, 3: 125, 4: 125}
EXCEPTION_NAMES = {1: "ILLEGAL_FUNCTION", 2: "ILLEGAL_DATA_ADDRESS", 3: "ILLEGAL_DATA_VALUE",
                   4: "SERVER_DEVICE_FAILURE", 5: "ACKNOWLEDGE", 6: "SERVER_DEVICE_BUSY",
                   10: "GATEWAY_PATH_UNAVAILABLE", 11: "GATEWAY_TARGET_NO_RESPONSE"}


class ModbusException(Exception):
    """The device answered with a Modbus exception response."""

    def __init__(self, code: int):
        super().__init__(f"Modbus exception {code} {EXCEPTION_NAMES.get(code, '')}".strip())
        self.code = code


class DeviceTimeout(Exception):
    """No (valid) answer from one unit in time; the link itself may still be fine."""


# ------------------------------------------------------------------ PDU
def read_pdu(fc: int, start: int, qty: int) -> bytes:
    if fc not in MAX_QTY:
        raise ConnectorError("OP_FORBIDDEN", f"function code {fc} is not a read")
    if not (0 <= start <= 0xFFFF and 1 <= qty <= MAX_QTY[fc] and start + qty <= 0x10000):
        raise ConnectorError("ADDR_INVALID", f"FC{fc} start={start} qty={qty} out of range")
    return struct.pack(">BHH", fc, start, qty)


def parse_response(fc: int, qty: int, pdu: bytes) -> list[int]:
    """Response PDU -> list of bits (FC1/2) or 16-bit registers (FC3/4)."""
    if not pdu:
        raise ConnectorError("PROTOCOL_ERROR", "empty response")
    if pdu[0] == fc | 0x80:
        if len(pdu) < 2:
            raise ConnectorError("PROTOCOL_ERROR", "short exception response")
        raise ModbusException(pdu[1])
    if pdu[0] != fc or len(pdu) < 2 or len(pdu) != 2 + pdu[1]:
        raise ConnectorError("PROTOCOL_ERROR", f"unexpected response to FC{fc}")
    data = pdu[2:]
    if fc in (1, 2):
        if len(data) != (qty + 7) // 8:
            raise ConnectorError("PROTOCOL_ERROR", "wrong bit count")
        return [(data[i // 8] >> (i % 8)) & 1 for i in range(qty)]
    if len(data) != 2 * qty:
        raise ConnectorError("PROTOCOL_ERROR", "wrong register count")
    return list(struct.unpack(f">{qty}H", data))


# ------------------------------------------------------------------ RTU CRC
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def rtu_frame(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + struct.pack("<H", crc16(body))


# ------------------------------------------------------------------ links
class _Link:
    """Common part: one outstanding request at a time, guard, counters."""

    def __init__(self, guard: ReadOnlyGuard, inter_request_delay_ms: int = 0):
        self.guard = guard
        self.lock = asyncio.Lock()
        self.delay = inter_request_delay_ms / 1000
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.requests = 0
        self.last_latency_ms: float | None = None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def close(self) -> None:
        w, self.writer, self.reader = self.writer, None, None
        if w is not None:
            w.close()
            try:
                await asyncio.wait_for(w.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass

    async def read(self, unit: int, table: str, start: int, qty: int, timeout_s: float) -> list[int]:
        fc = FC[table]
        self.guard.check(f"FC{fc}")
        pdu = read_pdu(fc, start, qty)
        if not self.connected:
            raise ConnectorError("CONN_CLOSED", "link is not connected")
        async with self.lock:
            t0 = time.monotonic()
            try:
                resp = await self._exchange(unit, pdu, fc, qty, timeout_s)
            except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
                await self.close()
                raise ConnectorError("CONN_CLOSED", f"link lost: {exc.__class__.__name__}") from exc
            except ConnectorError:                   # framing out of step: start again on a clean link
                await self.close()
                raise
            finally:
                self.requests += 1
                if self.delay:
                    await asyncio.sleep(self.delay)
            self.last_latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return parse_response(fc, qty, resp)


class TcpLink(_Link):
    def __init__(self, host: str, port: int, guard: ReadOnlyGuard, inter_request_delay_ms: int = 0):
        super().__init__(guard, inter_request_delay_ms)
        self.host, self.port, self.tid = host, port, 0

    async def connect(self, timeout_s: float) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port),
                                                              timeout_s)
        except asyncio.TimeoutError as exc:
            raise ConnectorError("CONN_TIMEOUT", f"no TCP connection to {self.host}:{self.port}") from exc
        except ConnectionRefusedError as exc:
            raise ConnectorError("CONN_REFUSED", f"{self.host}:{self.port} refused the connection") from exc
        except OSError as exc:
            raise ConnectorError("CONN_REFUSED", f"{self.host}:{self.port}: {exc.strerror or exc}") from exc

    async def _exchange(self, unit: int, pdu: bytes, fc: int, qty: int, timeout_s: float) -> bytes:
        self.tid = (self.tid + 1) & 0xFFFF
        self.writer.write(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu)
        await self.writer.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:                                  # skip late answers to earlier, timed-out requests
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DeviceTimeout(f"unit {unit}: no response within {timeout_s * 1000:.0f} ms")
            try:
                head = await asyncio.wait_for(self.reader.readexactly(7), remaining)
                tid, proto, length, r_unit = struct.unpack(">HHHB", head)
                if proto != 0 or not 2 <= length <= 254:
                    raise ConnectorError("PROTOCOL_ERROR", "invalid MBAP header")
                body = await asyncio.wait_for(self.reader.readexactly(length - 1), max(remaining, 0.5))
            except asyncio.TimeoutError as exc:
                raise DeviceTimeout(f"unit {unit}: no response within {timeout_s * 1000:.0f} ms") from exc
            if tid == self.tid and r_unit == unit:
                return body


class RtuLink(_Link):
    """RTU over any byte stream: a serial port (needs pyserial-asyncio, BSD) or a TCP serial server."""

    def __init__(self, serial: dict, guard: ReadOnlyGuard, inter_request_delay_ms: int = 20):
        super().__init__(guard, inter_request_delay_ms)
        self.serial = serial

    async def connect(self, timeout_s: float) -> None:
        try:
            import serial_asyncio  # optional dependency, only on edge nodes with RS-485
        except ImportError as exc:
            raise ConnectorError("INTERNAL", "pyserial-asyncio is not installed") from exc
        s = self.serial
        try:
            self.reader, self.writer = await asyncio.wait_for(serial_asyncio.open_serial_connection(
                url=s["device"], baudrate=s.get("baud", 9600), parity=s.get("parity", "E"),
                bytesize=s.get("data_bits", 8), stopbits=s.get("stop_bits", 1)), timeout_s)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectorError("CONN_REFUSED", f"cannot open {s['device']}: {exc}") from exc

    def _discard_input(self) -> None:
        buf = getattr(self.reader, "_buffer", None)  # asyncio.StreamReader keeps unread bytes here
        if buf:
            buf.clear()

    def attach(self, reader: asyncio.StreamReader, writer) -> None:
        """For tests and serial-over-TCP converters."""
        self.reader, self.writer = reader, writer

    async def _exchange(self, unit: int, pdu: bytes, fc: int, qty: int, timeout_s: float) -> bytes:
        self._discard_input()                        # drop noise or late bytes from a previous frame
        self.writer.write(rtu_frame(unit, pdu))
        await self.writer.drain()
        try:
            head = await asyncio.wait_for(self.reader.readexactly(3), timeout_s)
            if head[1] & 0x80:
                rest = await asyncio.wait_for(self.reader.readexactly(2), timeout_s)
            else:
                rest = await asyncio.wait_for(self.reader.readexactly(head[2] + 2), timeout_s)
        except asyncio.TimeoutError as exc:
            raise DeviceTimeout(f"unit {unit}: no response within {timeout_s * 1000:.0f} ms") from exc
        frame = head + rest
        if struct.unpack("<H", frame[-2:])[0] != crc16(frame[:-2]):
            raise DeviceTimeout(f"unit {unit}: CRC error")
        if frame[0] != unit:
            raise DeviceTimeout(f"unit {unit}: answer from unit {frame[0]}")
        return frame[1:-2]
