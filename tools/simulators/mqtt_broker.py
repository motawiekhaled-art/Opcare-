"""Small MQTT broker (3.1.1 and 5.0, QoS 0/1, retained messages) for connector tests and demos (Opcare DAQ).

Records every packet type each client sends, so tests can prove the connector never PUBLISHes (CT-13).
Optional user name / password check. `publish()` injects messages as if a device had sent them; real clients
(e.g. mosquitto_pub, paho) may also connect and publish.

    python tools/simulators/mqtt_broker.py --port 18830
"""
from __future__ import annotations

import argparse
import asyncio
import struct

NAMES = {1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK", 5: "PUBREC", 6: "PUBREL", 7: "PUBCOMP",
         8: "SUBSCRIBE", 9: "SUBACK", 10: "UNSUBSCRIBE", 11: "UNSUBACK", 12: "PINGREQ", 13: "PINGRESP",
         14: "DISCONNECT", 15: "AUTH"}


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def _pkt(t: int, flags: int, body: bytes) -> bytes:
    return bytes([(t << 4) | flags]) + _varint(len(body)) + body


def matches(f: str, topic: str) -> bool:
    if topic.startswith("$") and not f.startswith("$"):
        return False
    fp, tp = f.split("/"), topic.split("/")
    for i, part in enumerate(fp):
        if part == "#":
            return True
        if i >= len(tp) or (part != "+" and part != tp[i]):
            return False
    return len(fp) == len(tp)


class _Client:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer, self.id, self.version, self.subs, self.next_id = writer, "?", 4, {}, 0


class MqttBroker:
    def __init__(self, users: dict[str, str] | None = None):
        self.users = users                      # None = anonymous allowed
        self.clients: set[_Client] = set()
        self.retained: dict[str, bytes] = {}
        self.log: list[tuple[str, str]] = []    # (client_id, packet name) for every packet received
        self.server = None
        self.port = 0
        self.accepted = 0
        self.on_subscribe = None                # callback(filter) — demos: an edge node republishes its births

    async def start(self, host: str = "127.0.0.1", port: int = 0, ssl=None) -> int:
        self.server = await asyncio.start_server(self._serve, host, port, ssl=ssl)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for c in list(self.clients):
            c.writer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def kick_all(self) -> None:
        """Drop every connection (tests: broker restart / network loss)."""
        for c in list(self.clients):
            c.writer.close()

    def sent_by(self, client_prefix: str = "") -> set[str]:
        return {name for cid, name in self.log if cid.startswith(client_prefix)}

    # ------------------------------------------------------------ inject messages
    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        if retain:
            if data:
                self.retained[topic] = data
            else:
                self.retained.pop(topic, None)
        for c in list(self.clients):
            granted = max((q for f, q in c.subs.items() if matches(f, topic)), default=None)
            if granted is not None:
                self._deliver(c, topic, data, min(qos, granted), False)

    def _deliver(self, c: _Client, topic: str, data: bytes, qos: int, retain: bool) -> None:
        body = _str(topic)
        if qos:
            c.next_id = c.next_id % 65535 + 1
            body += struct.pack(">H", c.next_id)
        if c.version == 5:
            body += b"\x00"
        try:
            c.writer.write(_pkt(3, (qos << 1) | (1 if retain else 0), body + data))
        except (ConnectionError, RuntimeError):
            pass

    # ------------------------------------------------------------ connection
    async def _read(self, r: asyncio.StreamReader) -> tuple[int, int, bytes]:
        first = (await r.readexactly(1))[0]
        mult, length = 1, 0
        while True:
            b = (await r.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return first >> 4, first & 15, (await r.readexactly(length)) if length else b""

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        c = _Client(w)
        self.accepted += 1
        try:
            t, _, body = await self._read(r)
            if t != 1:
                return
            self.log.append(("?", "CONNECT"))
            pos = 2 + struct.unpack_from(">H", body, 0)[0]
            c.version, flags = body[pos], body[pos + 1]
            pos += 4
            if c.version == 5:
                n, pos2, mult = 0, pos, 1
                while True:
                    b = body[pos2]
                    pos2 += 1
                    n += (b & 0x7F) * mult
                    mult *= 128
                    if not b & 0x80:
                        break
                pos = pos2 + n
            ln = struct.unpack_from(">H", body, pos)[0]
            c.id = body[pos + 2:pos + 2 + ln].decode()
            pos += 2 + ln
            user = pw = None
            if flags & 0x80:
                ln = struct.unpack_from(">H", body, pos)[0]
                user = body[pos + 2:pos + 2 + ln].decode()
                pos += 2 + ln
            if flags & 0x40:
                ln = struct.unpack_from(">H", body, pos)[0]
                pw = body[pos + 2:pos + 2 + ln].decode()
            self.log[-1] = (c.id, "CONNECT")
            if self.users is not None and self.users.get(user or "") != pw:
                w.write(_pkt(2, 0, bytes([0, 0x86 if c.version == 5 else 4]) + (b"\x00" if c.version == 5 else b"")))
                await w.drain()
                return
            w.write(_pkt(2, 0, b"\x00\x00" + (b"\x00" if c.version == 5 else b"")))
            self.clients.add(c)
            while True:
                t, flags, body = await self._read(r)
                self.log.append((c.id, NAMES.get(t, str(t))))
                if t == 8:                                            # SUBSCRIBE
                    pid, pos = struct.unpack_from(">H", body, 0)[0], 2
                    if c.version == 5:
                        pos += 1 + body[pos]
                    codes, new = [], []
                    while pos < len(body):
                        ln = struct.unpack_from(">H", body, pos)[0]
                        f = body[pos + 2:pos + 2 + ln].decode()
                        q = min(body[pos + 2 + ln] & 3, 1)
                        pos += 3 + ln
                        c.subs[f] = q
                        codes.append(q)
                        new.append((f, q))
                    w.write(_pkt(9, 0, struct.pack(">H", pid) + (b"\x00" if c.version == 5 else b"") + bytes(codes)))
                    for f, q in new:
                        for topic, data in list(self.retained.items()):
                            if matches(f, topic):
                                self._deliver(c, topic, data, q, True)
                        if self.on_subscribe:
                            self.on_subscribe(f)
                elif t == 10:                                         # UNSUBSCRIBE
                    pid, pos = struct.unpack_from(">H", body, 0)[0], 2
                    if c.version == 5:
                        pos += 1 + body[pos]
                    while pos < len(body):
                        ln = struct.unpack_from(">H", body, pos)[0]
                        c.subs.pop(body[pos + 2:pos + 2 + ln].decode(), None)
                        pos += 2 + ln
                    w.write(_pkt(11, 0, struct.pack(">H", pid) + (b"\x00" if c.version == 5 else b"")))
                elif t == 12:
                    w.write(_pkt(13, 0, b""))
                elif t == 3:                                          # a client publishes: route it
                    qos, retain = (flags >> 1) & 3, bool(flags & 1)
                    ln = struct.unpack_from(">H", body, 0)[0]
                    topic, pos = body[2:2 + ln].decode(), 2 + ln
                    if qos:
                        pid = struct.unpack_from(">H", body, pos)[0]
                        pos += 2
                        w.write(_pkt(4, 0, struct.pack(">H", pid)))
                    if c.version == 5:
                        n, mult = 0, 1
                        while True:
                            b = body[pos]
                            pos += 1
                            n += (b & 0x7F) * mult
                            mult *= 128
                            if not b & 0x80:
                                break
                        pos += n
                    self.publish(topic, body[pos:], qos, retain)
                elif t == 14:
                    return
        except (asyncio.IncompleteReadError, ConnectionError, IndexError, struct.error):
            pass
        finally:
            self.clients.discard(c)
            w.close()


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Small MQTT broker for tests and demos")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18830)
    args = ap.parse_args()
    b = MqttBroker()
    port = await b.start(args.host, args.port)
    print(f"MQTT broker on {args.host}:{port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
