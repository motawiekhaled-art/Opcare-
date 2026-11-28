"""Asyncio IEC 60870-5-104 controlling station (master) for one TCP or TLS connection (IEC 62351-3).

Section 5 of IEC 60870-5-104: STARTDT before data transfer, I-frame sequence checks, k / w flow control, t1
(acknowledgement), t2 (our S-frames) and t3 (TESTFR when idle). Application requests: general interrogation, counter
interrogation (read only, no freeze or reset) and the read command, one of each kind at a time.

Every frame passes the read-only guard first (spec 12 and 15.5). There is no clock synchronisation (spec 12 rule 3:
the feature does not exist in this code; C_CS_NA_1 is blocked by the guard like every other command).
"""
from __future__ import annotations

import asyncio
import ssl
import time
from datetime import timezone, tzinfo

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

from . import apci as P
from . import asdu as A


class Interrogation:
    """Collects the answer of one general or counter interrogation: con, the data, term."""

    def __init__(self, ca: int, type_id: int, loop: asyncio.AbstractEventLoop):
        self.ca, self.type_id = ca, type_id
        self.con = loop.create_future()
        self.term = loop.create_future()
        self.data: list[A.Asdu] = []


class Iec104Client:
    def __init__(self, host: str, port: int, guard: ReadOnlyGuard, *, k: int = 12, w: int = 8, t0: float = 30,
                 t1: float = 15, t2: float = 10, t3: float = 20, oa: int = 0, tls: ssl.SSLContext | None = None,
                 tz: tzinfo = timezone.utc, on_asdu=None):
        self.host, self.port, self.guard, self.tls, self.tz = host, port, guard, tls, tz
        self.k, self.w, self.t0, self.t1, self.t2, self.t3, self.oa = k, w, t0, t1, t2, t3, oa
        self.on_asdu = on_asdu
        self.reader = self.writer = None
        self.vs = self.vr = self.ack = 0                     # V(S), V(R), oldest unacknowledged V(S)
        self.sent: list[tuple[int, float]] = []              # (N(S), time) of unacknowledged I-frames
        self.recv_unacked = 0
        self.first_unacked_rx: float | None = None
        self.window = asyncio.Event()
        self.u_wait: dict[int, asyncio.Future] = {}
        self.testfr_sent: float | None = None
        self.last_rx = time.monotonic()
        self.tasks: list[asyncio.Task] = []
        self.closed = asyncio.Event()
        self.close_reason: ConnectorError | None = None
        self.started = False
        self.gi: dict[tuple[int, int], Interrogation] = {}  # (ca, type id) -> running interrogation
        self.reads: dict[tuple[int, int], asyncio.Future] = {}
        self.locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.stats = {"i_rx": 0, "i_tx": 0, "s_tx": 0, "testfr": 0, "asdus": 0, "unknown_types": 0,
                      "negative": 0, "timeouts": 0}

    # ------------------------------------------------------------ connection
    async def connect(self) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self.tls,
                                        server_hostname=self.host if self.tls else None), self.t0)
        except asyncio.TimeoutError:
            raise ConnectorError("CONN_TIMEOUT", f"no answer from {self.host}:{self.port} within t0 = {self.t0:g} s") from None
        except ssl.SSLError as exc:
            raise ConnectorError("TLS_FAILED", f"TLS handshake with {self.host}:{self.port} failed: {exc.reason or exc}") from None
        except OSError as exc:
            raise ConnectorError("CONN_REFUSED", f"cannot connect to {self.host}:{self.port}: {exc.strerror or exc}") from None
        self.closed.clear()
        self.tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._timers())]
        try:
            await self._u(P.STARTDT_ACT, P.STARTDT_CON)
        except ConnectorError:
            await self.close(stopdt=False)
            raise ConnectorError("CONN_TIMEOUT", f"{self.host}:{self.port} did not confirm STARTDT within t1 = "
                                                 f"{self.t1:g} s") from None
        self.started = True

    async def close(self, stopdt: bool = True) -> None:
        if stopdt and self.started and not self.closed.is_set():
            try:                                             # polite end of data transfer, then the socket
                await asyncio.wait_for(self._u(P.STOPDT_ACT, P.STOPDT_CON), 1.0)
            except (ConnectorError, asyncio.TimeoutError, OSError):
                pass
        self.started = False
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if self.writer and not self.writer.is_closing():
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ssl.SSLError):
                pass
        self._fail_waiters(self.close_reason or ConnectorError("CONN_CLOSED", "connection closed"))
        self.closed.set()

    def _lost(self, reason: ConnectorError) -> None:
        if self.close_reason is None:
            self.close_reason = reason
        if self.writer and not self.writer.is_closing():
            self.writer.close()
        self._fail_waiters(reason)
        self.closed.set()

    def _fail_waiters(self, exc: ConnectorError) -> None:
        futs = [f for g in self.gi.values() for f in (g.con, g.term)] + list(self.reads.values()) + list(self.u_wait.values())
        for f in futs:
            if not f.done():
                f.set_exception(exc)
                f.exception()                                # retrieved: no "never retrieved" warnings

    # ------------------------------------------------------------ sending
    def _write(self, raw: bytes) -> None:
        if self.writer is None or self.writer.is_closing():
            raise self.close_reason or ConnectorError("CONN_CLOSED", "not connected")
        self.writer.write(raw)

    async def _u(self, fn: int, con: int) -> None:
        self.guard.check(P.U_NAMES[fn])
        fut = asyncio.get_running_loop().create_future()
        self.u_wait[con] = fut
        self._write(P.u_frame(fn))
        try:
            await asyncio.wait_for(fut, self.t1)
        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            raise ConnectorError("CONN_TIMEOUT", f"no {P.U_NAMES[con]} within t1 = {self.t1:g} s") from None
        finally:
            self.u_wait.pop(con, None)

    async def send_asdu(self, asdu: bytes) -> None:
        self.guard.check(A.guard_name(asdu))                 # spec 12: checked before anything is sent
        while P.seq_distance(self.ack, self.vs) >= self.k:  # k unacknowledged I-frames: wait for the station
            self.window.clear()
            done = asyncio.ensure_future(self.closed.wait())
            win = asyncio.ensure_future(self.window.wait())
            await asyncio.wait({done, win}, timeout=self.t1, return_when=asyncio.FIRST_COMPLETED)
            done.cancel()
            win.cancel()
            if self.closed.is_set():
                raise self.close_reason or ConnectorError("CONN_CLOSED", "connection closed")
        self._write(P.i_frame(self.vs, self.vr, asdu))
        self.sent.append((self.vs, time.monotonic()))
        self.vs = (self.vs + 1) % P.MOD
        self.recv_unacked, self.first_unacked_rx = 0, None  # N(R) in our I-frame acknowledges what we received
        self.stats["i_tx"] += 1

    def _send_s(self) -> None:
        self.guard.check("S")
        self._write(P.s_frame(self.vr))
        self.recv_unacked, self.first_unacked_rx = 0, None
        self.stats["s_tx"] += 1

    # ------------------------------------------------------------ receiving
    def _acknowledged(self, nr: int) -> None:
        if P.seq_distance(self.ack, nr) > P.seq_distance(self.ack, self.vs):
            raise ConnectorError("PROTOCOL_ERROR", f"station acknowledged N(R) {nr} that was never sent (V(S) {self.vs})")
        self.ack = nr
        self.sent = [(ns, t) for ns, t in self.sent if P.seq_distance(nr, ns) < P.seq_distance(nr, self.vs)]
        self.window.set()

    async def _read_loop(self) -> None:
        try:
            while True:
                apdu = await P.read_apdu(self.reader)
                self.last_rx = time.monotonic()
                self.testfr_sent = None                      # any frame proves the link (t3 restarts)
                if apdu.kind == "U":
                    if apdu.u == P.TESTFR_ACT:               # the station tests the link: answer it
                        self.guard.check("TESTFR_CON")
                        self._write(P.u_frame(P.TESTFR_CON))
                    fut = self.u_wait.get(apdu.u)
                    if fut and not fut.done():
                        fut.set_result(True)
                    continue
                self._acknowledged(apdu.nr)
                if apdu.kind == "S":
                    continue
                if apdu.ns != self.vr:
                    raise ConnectorError("PROTOCOL_ERROR", f"I-frame N(S) {apdu.ns} out of sequence (expected {self.vr})")
                self.vr = (self.vr + 1) % P.MOD
                self.stats["i_rx"] += 1
                self.recv_unacked += 1
                if self.first_unacked_rx is None:
                    self.first_unacked_rx = time.monotonic()
                if self.recv_unacked >= self.w:
                    self._send_s()
                try:
                    a = A.parse(apdu.asdu, self.tz)
                except A.AsduError:
                    self.stats["unknown_types"] += 1
                    continue
                self._dispatch(a)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            self._lost(ConnectorError("CONN_CLOSED", f"connection to {self.host}:{self.port} lost"))
        except P.ApciError as exc:
            self._lost(ConnectorError("PROTOCOL_ERROR", f"bad APDU from {self.host}:{self.port}: {exc}"))
        except ConnectorError as exc:
            self._lost(exc)

    async def _timers(self) -> None:
        tick = min(0.5, self.t1 / 4, self.t2 / 4, self.t3 / 4)
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            if self.sent and now - self.sent[0][1] > self.t1:
                self.stats["timeouts"] += 1
                self._lost(ConnectorError("CONN_TIMEOUT", f"the station did not acknowledge I-frames within t1 = {self.t1:g} s"))
                return
            if self.testfr_sent is not None and now - self.testfr_sent > self.t1:
                self.stats["timeouts"] += 1
                self._lost(ConnectorError("CONN_TIMEOUT", f"the station did not answer TESTFR within t1 = {self.t1:g} s"))
                return
            if self.recv_unacked and self.first_unacked_rx is not None and now - self.first_unacked_rx >= self.t2:
                self._send_s()
            if self.started and self.testfr_sent is None and now - self.last_rx >= self.t3:
                self.guard.check("TESTFR_ACT")
                self._write(P.u_frame(P.TESTFR_ACT))
                self.testfr_sent = now
                self.stats["testfr"] += 1

    def _dispatch(self, a: A.Asdu) -> None:
        self.stats["asdus"] += 1
        if not a.known:
            self.stats["unknown_types"] += 1
        if a.negative:
            self.stats["negative"] += 1
        if a.type_id in (A.C_IC_NA_1, A.C_CI_NA_1):
            g = self.gi.get((a.ca, a.type_id))
            if g is None:
                return
            if a.cot == A.ACTCON or a.negative:
                if not g.con.done():
                    g.con.set_result(a)
                if a.negative and not g.term.done():
                    g.term.set_result(a)
            elif a.cot == A.ACTTERM and not g.term.done():
                g.term.set_result(a)
            return
        if a.type_id == A.C_RD_NA_1:                         # a negative answer to our read command
            fut = self.reads.get((a.ca, a.ioa))
            if fut and not fut.done():
                fut.set_result(a)
            return
        if a.objects:
            for g in self.gi.values():
                if g.ca == a.ca and ((g.type_id == A.C_IC_NA_1 and 20 <= a.cot <= 36) or
                                     (g.type_id == A.C_CI_NA_1 and 37 <= a.cot <= 41)):
                    g.data.append(a)
            if a.cot == A.REQUEST:
                for o in a.objects:
                    fut = self.reads.get((a.ca, o.ioa))
                    if fut and not fut.done():
                        fut.set_result(a)
        if self.on_asdu:
            self.on_asdu(a)

    # ------------------------------------------------------------ the requests
    async def interrogate(self, ca: int, counters: bool = False, timeout: float = 120) -> list[A.Asdu]:
        """General interrogation (QOI 20) or counter interrogation (QCC RQT 5, FRZ 0) of one common address.
        Returns the data ASDUs; raises ADDR_INVALID when the station answers negatively."""
        tid = A.C_CI_NA_1 if counters else A.C_IC_NA_1
        lock = self.locks.setdefault((ca, tid), asyncio.Lock())
        async with lock:
            g = Interrogation(ca, tid, asyncio.get_running_loop())
            self.gi[(ca, tid)] = g
            try:
                await self.send_asdu(A.counter_interrogation(ca, oa=self.oa) if counters
                                     else A.interrogation(ca, oa=self.oa))
                try:
                    con = await asyncio.wait_for(g.con, max(self.t1, 5))
                    if con.negative:
                        what = "counter interrogation" if counters else "general interrogation"
                        raise ConnectorError("ADDR_INVALID", f"the station rejected {what} of common address {ca} "
                                                             f"({A.cot_name(con.cot)})")
                    await asyncio.wait_for(g.term, timeout)
                except asyncio.TimeoutError:
                    self.stats["timeouts"] += 1
                    if not g.con.done():
                        raise ConnectorError("CONN_TIMEOUT", f"the station did not confirm the interrogation of common "
                                                             f"address {ca}") from None
                return g.data                                # no termination in time: what arrived so far
            finally:
                self.gi.pop((ca, tid), None)

    async def read(self, ca: int, ioa: int, timeout: float | None = None) -> A.Asdu | None:
        """Read command C_RD_NA_1. Returns the answering ASDU (data with cause 'request', or the negative C_RD), or
        None when the station does not answer."""
        fut = asyncio.get_running_loop().create_future()
        self.reads[(ca, ioa)] = fut
        try:
            await self.send_asdu(A.read_command(ca, ioa, self.oa))
            return await asyncio.wait_for(fut, timeout or max(self.t1, 5))
        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            return None
        finally:
            self.reads.pop((ca, ioa), None)
