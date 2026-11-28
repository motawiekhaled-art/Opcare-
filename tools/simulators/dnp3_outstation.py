"""Small DNP3 outstation (TCP) for tests and the demo, built from the connector's own link/transport/application code.

Supports what a read-only master uses: READ of class 0/1/2/3 and of static point ranges, CONFIRM, ENABLE/DISABLE
UNSOLICITED, WRITE of g80v1 index 7 (clear restart), link-status requests; event buffers per class, multi-fragment
responses that wait for confirms, unsolicited responses (including the null unsolicited at startup) that are
retried until confirmed. Any other function code is answered with IIN2.0 (function code not supported).

Every request is recorded in `requests` (function code names) and `link_requests`, so tests can prove that the
master sends nothing outside the read-only allow-list.

    os = Dnp3Outstation(address=10)
    os.add("analog_input", 0, 231.4, event_class=2)
    await os.start()                      # os.port
    os.set("analog_input", 0, 232.0)      # creates a class 2 event
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "connectors" / "dnp3"))

from dnp3_connector import app as A  # noqa: E402
from dnp3_connector import link as L  # noqa: E402

STATIC = {"binary_input": (1, 2), "double_bit_binary_input": (3, 2), "binary_output_status": (10, 2),
          "counter": (20, 1), "frozen_counter": (21, 1), "analog_input": (30, 5), "analog_output_status": (40, 3)}
EVENT = {"binary_input": (2, 2), "double_bit_binary_input": (4, 2), "binary_output_status": (11, 2),
         "counter": (22, 5), "frozen_counter": (23, 5), "analog_input": (32, 7), "analog_output_status": (42, 7)}
DEFAULT_CLASS = {"binary_input": 1, "double_bit_binary_input": 1, "binary_output_status": 1, "counter": 3,
                 "frozen_counter": 3, "analog_input": 2, "analog_output_status": 2}


@dataclass
class Pt:
    value: object
    flags: int
    event_class: int
    static: tuple
    event: tuple
    deadband: float = 0.0
    time: datetime | None = None


@dataclass
class Ev:
    kind: str
    index: int
    value: object
    flags: int
    time: datetime
    event_class: int


class Dnp3Outstation:
    def __init__(self, address: int = 10, master: int = 1, host: str = "127.0.0.1", port: int = 0,
                 fragment_size: int = 2048, unsolicited: bool = True, confirm_timeout: float = 2.0,
                 event_buffer: int = 1000, ssl=None):
        self.address, self.master, self.host, self.port = address, master, host, port
        self.fragment_size, self.allow_unsol, self.confirm_timeout = fragment_size, unsolicited, confirm_timeout
        self.event_buffer = event_buffer
        self.ssl = ssl
        self.connections = 0
        self.db: dict[str, dict[int, Pt]] = {k: {} for k in STATIC}
        self.events: list[Ev] = []
        self.overflow = False
        self.restart = True
        self.unsol_enabled: set[int] = set()
        self.requests: list[str] = []
        self.link_requests: list[str] = []
        self.server = None
        self.writers: set = set()
        self.respond = True                     # tests: False = stop answering (link loss without TCP close)
        self.changed = asyncio.Event()

    # ------------------------------------------------------------ database
    def add(self, kind: str, index: int, value, flags: int = A.ONLINE, event_class: int | None = None,
            static: tuple | None = None, event: tuple | None = None, deadband: float = 0.0) -> None:
        self.db[kind][index] = Pt(value, flags, DEFAULT_CLASS[kind] if event_class is None else event_class,
                                  static or STATIC[kind], event or EVENT[kind], deadband)

    def set(self, kind: str, index: int, value=None, flags: int | None = None, ts: datetime | None = None) -> None:
        p = self.db[kind][index]
        old_v, old_f = p.value, p.flags
        if value is not None:
            p.value = value
        if flags is not None:
            p.flags = flags
        p.time = ts or datetime.now(timezone.utc)
        moved = (abs(p.value - old_v) > p.deadband if isinstance(p.value, (int, float)) and not isinstance(p.value, bool)
                 and isinstance(old_v, (int, float)) else p.value != old_v)
        if p.event_class and (moved or p.flags != old_f):
            if len(self.events) >= self.event_buffer:
                self.events.pop(0)
                self.overflow = True
            self.events.append(Ev(kind, index, p.value, p.flags, p.time, p.event_class))
            self.changed.set()

    def iin(self) -> int:
        out = A.IIN_RESTART if self.restart else 0
        for c in (1, 2, 3):
            if any(e.event_class == c for e in self.events):
                out |= A.IIN_CLASS[c]
        if self.overflow:
            out |= A.IIN_OVERFLOW
        return out

    # ------------------------------------------------------------ network
    async def start(self) -> "Dnp3Outstation":
        self.server = await asyncio.start_server(self._conn, self.host, self.port, ssl=self.ssl)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.kick_all()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def kick_all(self) -> None:
        for w in list(self.writers):
            w.close()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    def _frame(self, ctrl: int, data: bytes = b"") -> bytes:
        return L.encode(L.Frame(ctrl, self.master, self.address, data))

    async def _conn(self, reader, writer) -> None:
        self.writers.add(writer)
        self.connections += 1
        state = {"tseq": 0, "useq": 0, "confirm": None, "uconfirm": None}
        reasm = L.Reassembly()
        lock = asyncio.Lock()

        async def send(fragment: bytes) -> None:
            async with lock:
                segs, state["tseq"] = L.segments(fragment, state["tseq"])
                for s in segs:
                    writer.write(self._frame(L.PRM | L.P_UNCONFIRMED_DATA, s))
                await writer.drain()

        unsol = asyncio.create_task(self._unsolicited(send, state)) if self.allow_unsol else None
        try:
            while True:
                frame = await L.read_frame(reader)
                if frame.dest != self.address or not self.respond:
                    continue
                if frame.primary and frame.function == L.P_REQUEST_LINK_STATUS:
                    self.link_requests.append("REQUEST_LINK_STATUS")
                    writer.write(self._frame(L.S_LINK_STATUS))
                    continue
                if not frame.primary:
                    self.link_requests.append({0: "ACK", 11: "LINK_STATUS"}.get(frame.function, f"SEC{frame.function}"))
                    continue
                if frame.function != L.P_UNCONFIRMED_DATA:
                    self.link_requests.append(f"PRI{frame.function}")
                    writer.write(self._frame(L.S_NOT_SUPPORTED))
                    continue
                fragment = reasm.add(frame.data)
                if fragment is None:
                    continue
                if len(fragment) >= 2 and fragment[1] == A.FC_CONFIRM:
                    await self._request(fragment, send, state)          # confirms are handled right here
                else:                                                   # a new request ends the previous one
                    if state.get("task") and not state["task"].done():
                        state["task"].cancel()
                    state["task"] = asyncio.create_task(self._request(fragment, send, state))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, L.LinkError):
            pass
        finally:
            if unsol:
                unsol.cancel()
            if state.get("task"):
                state["task"].cancel()
            self.writers.discard(writer)
            writer.close()

    async def _request(self, fragment: bytes, send, state) -> None:
        try:
            req = A.parse(fragment, request=True)
        except A.AppError:
            self.requests.append("MALFORMED")
            await send(A.response(A.AC_FIR | A.AC_FIN | (fragment[0] & 0x0F), A.FC_RESPONSE, self.iin() | 1 << 10))
            return
        fc, seq = req.function, req.seq
        self.requests.append(A.FC_NAMES.get(fc, f"FC{fc}"))
        if fc == A.FC_CONFIRM:
            key = "uconfirm" if req.uns else "confirm"
            fut = state[key]
            if fut and not fut.done() and fut.seq == seq:
                fut.set_result(True)
                if getattr(fut, "events", None):           # confirmed events leave the buffer now
                    self.events = [e for e in self.events if e not in fut.events]
                    if not self.events:
                        self.overflow = False
            return
        ac = A.AC_FIR | A.AC_FIN | seq
        if fc == A.FC_READ:
            await self._read(req, seq, send, state)
        elif fc == A.FC_WRITE and [o[1:4] for o in req.objects] == [(80, 1, 0)] and fragment[-1] == 0 and fragment[5:7] == b"\x07\x07":
            self.restart = False
            await send(A.response(ac, A.FC_RESPONSE, self.iin()))
        elif fc in (A.FC_ENABLE_UNSOL, A.FC_DISABLE_UNSOL):
            classes = {o[2] - 1 for o in req.objects if o[1] == 60 and o[2] in (2, 3, 4)}
            if fc == A.FC_ENABLE_UNSOL:
                self.unsol_enabled |= classes
                self.changed.set()
            else:
                self.unsol_enabled -= classes
            await send(A.response(ac, A.FC_RESPONSE, self.iin()))
        else:
            await send(A.response(ac, A.FC_RESPONSE, self.iin() | 1 << 8))      # IIN2.0 NO_FUNC_CODE_SUPPORT

    def _static(self, kind: str, indexes=None) -> list[bytes]:
        pts = sorted((i, p) for i, p in self.db[kind].items() if indexes is None or i in indexes)
        chunks, run = [], []
        for i, p in pts:
            if run and (i != run[-1][0] + 1 or p.static != run[-1][1].static or len(run) >= 50):
                chunks.append(A.encode_points(*run[0][1].static, [(j, q.value, q.flags, None) for j, q in run], False))
                run = []
            run.append((i, p))
        if run:
            chunks.append(A.encode_points(*run[0][1].static, [(j, q.value, q.flags, None) for j, q in run], False))
        return chunks

    def _event_chunks(self, evs: list[Ev]) -> list[bytes]:
        chunks = []
        for e in sorted(evs, key=lambda x: x.time):
            g, v = self.db[e.kind][e.index].event
            chunks.append(A.encode_points(g, v, [(e.index, e.value, e.flags, e.time)], True))
        return chunks

    async def _read(self, req, seq: int, send, state) -> None:
        chunks: list[bytes] = []
        evs: list[Ev] = []
        unknown = False
        for _, g, v, qual, idx in req.objects:
            if g == 60 and v in (2, 3, 4):
                sel = [e for e in self.events if e.event_class == v - 1 and e not in evs]
                evs += sel
                chunks += self._event_chunks(sel)
            elif g == 60 and v == 1:
                for kind in STATIC:
                    chunks += self._static(kind)
            elif g in A.KIND_OF_GROUP and g not in A.EVENT_GROUPS:
                chunks += self._static(A.KIND_OF_GROUP[g], set(idx) if idx else None)
            else:
                unknown = True
        frags: list[list[bytes]] = [[]]
        size = 4
        for c in chunks:
            if size + len(c) > self.fragment_size and frags[-1]:
                frags.append([])
                size = 4
            frags[-1].append(c)
            size += len(c)
        for n, body in enumerate(frags):
            last = n == len(frags) - 1
            con = not last or bool(evs)
            ac = (A.AC_FIR if n == 0 else 0) | (A.AC_FIN if last else 0) | (A.AC_CON if con else 0) | seq
            iin = self.iin() | (1 << 9 if unknown else 0)
            fut = None
            if con:
                fut = asyncio.get_running_loop().create_future()
                fut.seq = seq
                fut.events = evs if last else None
                state["confirm"] = fut
            await send(A.response(ac, A.FC_RESPONSE, iin, b"".join(body)))
            if fut:
                try:
                    await asyncio.wait_for(fut, self.confirm_timeout)
                except asyncio.TimeoutError:
                    return                               # no confirm: events stay buffered, stop the response

    async def _unsolicited(self, send, state) -> None:
        """Null unsolicited at startup, then events of enabled classes; each retried until confirmed."""
        first = True
        while True:
            evs = [e for e in self.events if e.event_class in self.unsol_enabled]
            if not first and not evs:
                self.changed.clear()
                await self.changed.wait()
                await asyncio.sleep(0.05)                # let a burst of changes collect
                continue
            seq = state["useq"]
            body = b"".join(self._event_chunks(evs)) if not first else b""
            for _attempt in range(3):
                fut = asyncio.get_running_loop().create_future()
                fut.seq = seq
                fut.events = evs
                state["uconfirm"] = fut
                await send(A.response(A.AC_FIR | A.AC_FIN | A.AC_CON | A.AC_UNS | seq, A.FC_UNSOLICITED, self.iin(), body))
                try:
                    await asyncio.wait_for(fut, self.confirm_timeout)
                    break
                except asyncio.TimeoutError:
                    continue
            else:
                await asyncio.sleep(self.confirm_timeout)
                continue
            state["useq"] = (seq + 1) & 0x0F
            first = False


async def _main() -> None:                      # python tools/simulators/dnp3_outstation.py [port]
    import math
    import random
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    os_ = Dnp3Outstation(address=10, host="0.0.0.0", port=port)
    for i, v in enumerate((231.4, 230.9, 232.1, 152.0, 0.98)):
        os_.add("analog_input", i, v)
    os_.add("binary_input", 0, True)
    os_.add("double_bit_binary_input", 0, 2)
    os_.add("counter", 0, 104500)
    await os_.start()
    print(f"DNP3 outstation 10 on tcp port {os_.port}", flush=True)
    t = 0
    while True:
        await asyncio.sleep(1)
        t += 1
        os_.set("analog_input", 0, round(231 + 2 * math.sin(t / 20) + random.uniform(-0.2, 0.2), 1))
        os_.set("counter", 0, 104500 + t)


if __name__ == "__main__":
    asyncio.run(_main())
