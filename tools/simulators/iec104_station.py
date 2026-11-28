"""Small IEC 60870-5-104 controlled station (slave) for tests and the demo, built from the connector's own APCI and
ASDU code.

Supports what a read-only master uses: STARTDT/STOPDT/TESTFR, k / w flow control with sequence checks, general
interrogation (con, data by type, term), counter interrogation, the read command, spontaneous data with CP56Time2a
time tags, end of initialisation, and negative answers (unknown common address, unknown IOA, unknown type). Every
other type is answered negatively with cause 44 (unknown type).

Every frame received is recorded in `requests` (U-frame names, "S", and I-frame type names; counter interrogation
with freeze or reset as "C_CI_NA_1:FREEZE"), so tests can prove the master sends nothing outside the allow-list.

    st = Iec104Station(cas=(1,))
    st.add(1, 1001, "M_ME_NC_1", 231.4)
    await st.start()                         # st.port
    st.set(1, 1001, 232.0)                   # spontaneous M_ME_TF_1 with the time tag
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "connectors" / "iec104"))

from iec104_connector import apci as P  # noqa: E402
from iec104_connector import asdu as A  # noqa: E402


@dataclass
class Item:
    type_id: int                    # type without time tag (as sent in interrogation)
    value: object
    qual: int = 0


class _Conn:
    def __init__(self, writer, k: int, w: int):
        self.writer, self.k, self.w = writer, k, w
        self.vs = self.vr = self.ack = 0
        self.recv_unacked = 0
        self.started = False
        self.window = asyncio.Event()
        self.out: asyncio.Queue = asyncio.Queue()


class Iec104Station:
    def __init__(self, cas=(1,), host: str = "127.0.0.1", port: int = 0, ssl=None, tz: tzinfo = timezone.utc,
                 k: int = 12, w: int = 8, read_support: bool = True, objects_per_asdu: int = 20):
        self.cas, self.host, self.port, self.ssl, self.tz = set(cas), host, port, ssl, tz
        self.k, self.w, self.read_support, self.per_asdu = k, w, read_support, objects_per_asdu
        self.db: dict[tuple[int, int], Item] = {}
        self.requests: list[str] = []
        self.connections = 0
        self.conns: set[_Conn] = set()
        self.server = None
        self.respond = True             # tests: False = stop answering anything (link loss without TCP close)

    # ------------------------------------------------------------ database
    def add(self, ca: int, ioa: int, type_name: str, value, qual: int = 0) -> None:
        self.db[(ca, ioa)] = Item(A.TYPE_IDS[type_name], value, qual)

    def get(self, ca: int, ioa: int):
        return self.db[(ca, ioa)].value

    def set(self, ca: int, ioa: int, value=None, qual: int | None = None, t: datetime | None = None,
            time_invalid: bool = False, transient: bool = False, cot: int = A.SPONTANEOUS, timed: bool = True,
            test: bool = False) -> None:
        """Changes an object and sends it spontaneously (with a CP56Time2a time tag unless timed=False)."""
        it = self.db[(ca, ioa)]
        if value is not None:
            it.value = value
        if qual is not None:
            it.qual = qual
        tid = A.TIMED[it.type_id] if timed else it.type_id
        self._broadcast(A.monitor(tid, cot, ca, [(ioa, it.value, it.qual, t or datetime.now(timezone.utc), time_invalid,
                                                  transient)], tz=self.tz, test=test))

    def restart(self, ca: int | None = None) -> None:
        """The station re-initialised: end of initialisation (M_EI_NA_1, cause 4) on every started connection."""
        for c in sorted(self.cas) if ca is None else (ca,):
            self._broadcast(A.end_of_init(c))

    def _broadcast(self, asdu: bytes) -> None:
        for c in list(self.conns):
            if c.started:
                c.out.put_nowait(asdu)

    # ------------------------------------------------------------ server
    async def start(self) -> "Iec104Station":
        self.server = await asyncio.start_server(self._conn, self.host, self.port, ssl=self.ssl)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.kick_all()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def kick_all(self) -> None:
        for c in list(self.conns):
            c.writer.close()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    async def _conn(self, reader, writer) -> None:
        self.connections += 1
        c = _Conn(writer, self.k, self.w)
        self.conns.add(c)
        sender = asyncio.create_task(self._sender(c))
        try:
            while True:
                apdu = await P.read_apdu(reader)
                self.requests.append(A.guard_name(apdu.asdu) if apdu.kind == "I" else apdu.name)
                if not self.respond:
                    continue
                if apdu.kind == "U":
                    answer = {P.STARTDT_ACT: P.STARTDT_CON, P.STOPDT_ACT: P.STOPDT_CON, P.TESTFR_ACT: P.TESTFR_CON}
                    if apdu.u in answer:
                        writer.write(P.u_frame(answer[apdu.u]))
                    if apdu.u == P.STARTDT_ACT:
                        c.started = True
                    elif apdu.u == P.STOPDT_ACT:
                        c.started = False
                    continue
                if P.seq_distance(c.ack, apdu.nr) > P.seq_distance(c.ack, c.vs):
                    break                                       # acknowledges frames never sent: close
                c.ack = apdu.nr
                c.window.set()
                if apdu.kind == "S":
                    continue
                if apdu.ns != c.vr:
                    break                                       # sequence error: close (IEC 60870-5-104 5.1)
                c.vr = (c.vr + 1) % P.MOD
                c.recv_unacked += 1
                if c.recv_unacked >= c.w:
                    writer.write(P.s_frame(c.vr))
                    c.recv_unacked = 0
                if c.started:
                    for out in self._answer(apdu.asdu):
                        c.out.put_nowait(out)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, P.ApciError):
            pass
        finally:
            sender.cancel()
            self.conns.discard(c)
            writer.close()

    async def _sender(self, c: _Conn) -> None:
        while True:
            asdu = await c.out.get()
            while P.seq_distance(c.ack, c.vs) >= c.k:            # k unacknowledged: wait for the master's ack
                c.window.clear()
                await c.window.wait()
            if not self.respond:
                continue
            c.writer.write(P.i_frame(c.vs, c.vr, asdu))
            c.vs = (c.vs + 1) % P.MOD
            c.recv_unacked = 0

    # ------------------------------------------------------------ application
    def _answer(self, raw: bytes) -> list[bytes]:
        try:
            a = A.parse(raw, self.tz)
        except A.AsduError:
            return []
        if a.type_id == A.C_IC_NA_1:
            if a.ca not in self.cas:
                return [A.mirror(raw, A.UNKNOWN_CA, negative=True)]
            return [A.mirror(raw, A.ACTCON), *self._data(a.ca, counters=False, cot=a.qualifier or A.INROGEN),
                    A.mirror(raw, A.ACTTERM)]
        if a.type_id == A.C_CI_NA_1:
            if a.ca not in self.cas:
                return [A.mirror(raw, A.UNKNOWN_CA, negative=True)]
            if (a.qualifier or 0) >> 6:
                return [A.mirror(raw, A.ACTCON, negative=True)]  # freeze / reset: never done by this station
            return [A.mirror(raw, A.ACTCON), *self._data(a.ca, counters=True, cot=A.REQCOGEN), A.mirror(raw, A.ACTTERM)]
        if a.type_id == A.C_RD_NA_1:
            if not self.read_support:
                return [A.mirror(raw, A.UNKNOWN_TYPE, negative=True)]
            if a.ca not in self.cas:
                return [A.mirror(raw, A.UNKNOWN_CA, negative=True)]
            it = self.db.get((a.ca, a.ioa))
            if it is None:
                return [A.mirror(raw, A.UNKNOWN_IOA, negative=True)]
            return [A.monitor(it.type_id, A.REQUEST, a.ca, [(a.ioa, it.value, it.qual)], tz=self.tz)]
        return [A.mirror(raw, A.UNKNOWN_TYPE, negative=True)]  # every other type, commands included

    def _data(self, ca: int, counters: bool, cot: int) -> list[bytes]:
        by_type: dict[int, list] = {}
        for (c, ioa), it in sorted(self.db.items()):
            if c == ca and (A.family(it.type_id) == "IT") == counters:
                by_type.setdefault(it.type_id, []).append((ioa, it.value, it.qual))
        out = []
        for tid, objs in by_type.items():
            for i in range(0, len(objs), self.per_asdu):
                out.append(A.monitor(tid, cot, ca, objs[i:i + self.per_asdu], tz=self.tz))
        return out


async def _main() -> None:                       # python tools/simulators/iec104_station.py [port]
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2404
    st = Iec104Station(cas=(1,), host="0.0.0.0", port=port)
    st.add(1, 1001, "M_ME_NC_1", 132.4)
    st.add(1, 2001, "M_DP_NA_1", 2)
    st.add(1, 3001, "M_IT_NA_1", 104500)
    await st.start()
    print(f"IEC 104 station on port {st.port}, common address 1", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
