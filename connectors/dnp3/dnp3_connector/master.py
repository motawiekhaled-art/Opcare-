"""Asyncio DNP3 master over TCP (or TLS, IEC 62351-3): one association, solicited requests one at a time, multi-
fragment responses with confirms, unsolicited responses with confirms, and link-status keep-alive.

Every frame passes the read-only guard first (spec 12 and 15.6): application requests by function code, link-layer
frames by name. Time synchronisation is never performed (spec 12 rule 3): NEED_TIME is reported, nothing is written.
"""
from __future__ import annotations

import asyncio
import ssl
import time

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

from . import app as A
from . import link as L

GUARD_FC = {A.FC_CONFIRM: "FC0", A.FC_READ: "FC1", A.FC_ENABLE_UNSOL: "FC20", A.FC_DISABLE_UNSOL: "FC21"}


class Dnp3Master:
    def __init__(self, host: str, port: int, master_address: int, outstation_address: int, guard: ReadOnlyGuard,
                 timeout: float = 5.0, keep_alive_s: float = 60, tls: ssl.SSLContext | None = None,
                 on_unsolicited=None):
        self.host, self.port, self.tls = host, port, tls
        self.master, self.outstation = master_address, outstation_address
        self.guard, self.timeout, self.keep_alive_s = guard, timeout, keep_alive_s
        self.on_unsolicited = on_unsolicited
        self.reader = self.writer = None
        self.tseq = 0                         # transport sequence (ours)
        self.aseq = 0                         # solicited application sequence
        self.reasm = L.Reassembly()
        self.lock = asyncio.Lock()            # one solicited request at a time
        self.pending: asyncio.Queue | None = None
        self.link_status: asyncio.Future | None = None
        self.task = self.keep_task = None
        self.closed = asyncio.Event()
        self.close_reason: ConnectorError | None = None
        self.last_rx = time.monotonic()
        self.stats = {"requests": 0, "fragments": 0, "unsolicited": 0, "confirms": 0, "link_status": 0,
                      "crc_errors": 0, "timeouts": 0}
        self.last_iin = 0

    # ------------------------------------------------------------ connection
    async def connect(self) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self.tls,
                                        server_hostname=self.host if self.tls else None), self.timeout)
        except asyncio.TimeoutError:
            raise ConnectorError("CONN_TIMEOUT", f"no answer from {self.host}:{self.port}") from None
        except ssl.SSLError as exc:
            raise ConnectorError("TLS_FAILED", f"TLS handshake with {self.host}:{self.port} failed: {exc.reason or exc}") from None
        except OSError as exc:
            raise ConnectorError("CONN_REFUSED", f"cannot connect to {self.host}:{self.port}: {exc.strerror or exc}") from None
        self.pending = asyncio.Queue()
        self.closed.clear()
        self.task = asyncio.create_task(self._read_loop())
        if self.keep_alive_s:
            self.keep_task = asyncio.create_task(self._keep_alive())

    async def close(self) -> None:
        for t in (self.keep_task, self.task):
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in (self.keep_task, self.task) if t), return_exceptions=True)
        if self.writer and not self.writer.is_closing():
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ssl.SSLError):
                pass
        self.closed.set()

    # ------------------------------------------------------------ sending
    def _frame(self, ctrl: int, data: bytes = b"") -> bytes:
        return L.encode(L.Frame(ctrl, self.outstation, self.master, data))

    def _send_fragment(self, fragment: bytes) -> None:
        if self.writer is None or self.writer.is_closing():
            raise ConnectorError("CONN_CLOSED", "not connected")
        segs, self.tseq = L.segments(fragment, self.tseq)
        for s in segs:
            self.writer.write(self._frame(L.DIR | L.PRM | L.P_UNCONFIRMED_DATA, s))

    def _guard_fragment(self, fragment: bytes) -> None:
        fc = fragment[1]
        if fc == A.FC_WRITE:
            name = "FC2_CLEAR_RESTART" if fragment[2:] == A.clear_restart(0)[2:] else "FC2"
        else:
            name = GUARD_FC.get(fc, f"FC{fc}")
        self.guard.check(name)

    def send_confirm(self, seq: int, uns: bool) -> None:
        frag = A.confirm(seq, uns)
        self._guard_fragment(frag)
        self._send_fragment(frag)
        self.stats["confirms"] += 1

    async def request(self, build, timeout: float | None = None) -> list[A.Fragment]:
        """Sends a request built by build(seq) and returns all response fragments (confirming as asked)."""
        async with self.lock:
            seq = self.aseq
            self.aseq = (self.aseq + 1) & 0x0F
            frag = build(seq)
            self._guard_fragment(frag)                   # spec 12: checked before anything is sent
            while not self.pending.empty():
                self.pending.get_nowait()
            self._send_fragment(frag)
            self.stats["requests"] += 1
            out: list[A.Fragment] = []
            while True:
                try:
                    item = await asyncio.wait_for(self.pending.get(), timeout or self.timeout)
                except asyncio.TimeoutError:
                    self.stats["timeouts"] += 1
                    raise ConnectorError("CONN_TIMEOUT", f"no response from outstation {self.outstation} within "
                                                         f"{(timeout or self.timeout) * 1000:.0f} ms") from None
                if isinstance(item, Exception):
                    raise item
                if item.seq != seq:
                    continue                             # a late answer to an earlier, timed-out request
                out.append(item)
                if item.con:
                    self.send_confirm(item.seq, False)
                if item.fin:
                    return out

    # ------------------------------------------------------------ receiving
    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    frame = await L.read_frame(self.reader)
                except L.LinkError:
                    self.stats["crc_errors"] += 1
                    continue
                self.last_rx = time.monotonic()
                if frame.src != self.outstation or frame.dest != self.master:
                    continue
                if not frame.primary:
                    if frame.function == L.S_LINK_STATUS and self.link_status and not self.link_status.done():
                        self.link_status.set_result(True)
                    continue
                fc = frame.function
                if fc == L.P_REQUEST_LINK_STATUS:          # the outstation checks the link: answer it
                    self.guard.check("LINK_STATUS")
                    self.writer.write(self._frame(L.DIR | L.S_LINK_STATUS))
                    continue
                if fc in (L.P_RESET_LINK, L.P_TEST_LINK, L.P_CONFIRMED_DATA):
                    self.guard.check("LINK_ACK")
                    self.writer.write(self._frame(L.DIR | L.S_ACK))
                    if fc != L.P_CONFIRMED_DATA:
                        continue
                elif fc != L.P_UNCONFIRMED_DATA:
                    continue
                fragment = self.reasm.add(frame.data)
                if fragment is None:
                    continue
                try:
                    f = A.parse(fragment)
                except A.AppError as exc:
                    self.stats["fragments"] += 1
                    if len(fragment) >= 4:                 # still confirm and pass the IIN on
                        f = A.Fragment(fragment[0], fragment[1], fragment[2] | fragment[3] << 8)
                        f.error = str(exc)
                    else:
                        continue
                self.stats["fragments"] += 1
                self.last_iin = f.iin
                if f.function == A.FC_UNSOLICITED:
                    self.stats["unsolicited"] += 1
                    if f.con:
                        self.send_confirm(f.seq, True)
                    if self.on_unsolicited:
                        self.on_unsolicited(f)
                elif f.function == A.FC_RESPONSE:
                    self.pending.put_nowait(f)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            self.close_reason = ConnectorError("CONN_CLOSED", f"connection to {self.host}:{self.port} lost")
        except ConnectorError as exc:
            self.close_reason = exc
        finally:
            if self.pending is not None:
                self.pending.put_nowait(self.close_reason or ConnectorError("CONN_CLOSED", "connection closed"))
            self.closed.set()

    async def _keep_alive(self) -> None:
        """Link-status request when nothing was received for keep_alive_s (IEEE 1815 section 9.2.6.3)."""
        while True:
            await asyncio.sleep(max(0.2, self.keep_alive_s - (time.monotonic() - self.last_rx)))
            if time.monotonic() - self.last_rx < self.keep_alive_s:
                continue
            if not await self.link_check():
                self.close_reason = ConnectorError("CONN_TIMEOUT", "the outstation did not answer the link-status request")
                self.writer.close()
                return

    async def link_check(self) -> bool:
        self.guard.check("LINK_STATUS_REQUEST")
        self.link_status = asyncio.get_running_loop().create_future()
        self.writer.write(self._frame(L.DIR | L.PRM | L.P_REQUEST_LINK_STATUS))
        self.stats["link_status"] += 1
        try:
            await asyncio.wait_for(self.link_status, self.timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------ the requests
    async def integrity(self) -> list[A.Fragment]:
        """Class 1, 2, 3 events then class 0 static data, in one READ (IEEE 1815 section 5.1.2)."""
        return await self.request(lambda s: A.read_classes(s, (1, 2, 3, 0)))

    async def poll(self, classes: tuple[int, ...]) -> list[A.Fragment]:
        return await self.request(lambda s: A.read_classes(s, classes))

    async def read_points(self, items: list[tuple[int, int, int]]) -> list[A.Fragment]:
        return await self.request(lambda s: A.read_range(s, items))

    async def clear_restart(self) -> list[A.Fragment]:
        return await self.request(A.clear_restart)

    async def set_unsolicited(self, enable: bool) -> list[A.Fragment]:
        return await self.request(lambda s: A.unsolicited(s, enable))
