"""Asyncio MQTT subscriber (3.1.1 and 5.0) over TCP or TLS. Every packet it sends passes the read-only guard."""
from __future__ import annotations

import asyncio
import ssl
import struct
import time
import uuid
from urllib.parse import urlparse

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

from . import packets as P


def parse_url(url: str) -> tuple[str, int, bool]:
    u = urlparse(url)
    if u.scheme not in ("mqtt", "mqtts") or not u.hostname:
        raise ConnectorError("ADDR_INVALID", f"broker_url must be mqtt://host[:port] or mqtts://host[:port], not {url!r}")
    tls = u.scheme == "mqtts"
    return u.hostname, u.port or (8883 if tls else 1883), tls


def tls_context(ca_pem: str | None, cert_pem: str | None, key_pem: str | None, insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    if cert_pem and key_pem:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:                 # ssl only loads client keys from files
            cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
            for path, pem in ((cp, cert_pem), (kp, key_pem)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(pem)
            ctx.load_cert_chain(cp, kp)
    if insecure:                                                 # spec 14: only with insecure_skip_verify, warned
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MqttClient:
    def __init__(self, url: str, guard: ReadOnlyGuard, version: int = P.V5, client_id: str | None = None,
                 username: str | None = None, password: str | None = None, keepalive: int = 60,
                 ssl_ctx: ssl.SSLContext | None = None, on_message=None):
        self.host, self.port, self.tls = parse_url(url)
        self.guard, self.version, self.keepalive = guard, version, keepalive
        self.client_id = client_id or f"opcare-{uuid.uuid4().hex[:12]}"
        self.username, self.password, self.ssl_ctx = username, password, ssl_ctx
        self.on_message = on_message
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._waiting: dict[tuple[int, int], asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._ping: asyncio.Task | None = None
        self.closed = asyncio.Event()
        self.close_reason: ConnectorError | None = None
        self.received = 0
        self.last_rx = 0.0
        self.sent: list[str] = []                                 # packet names sent (diagnostics / tests)

    # ------------------------------------------------------------ connect
    async def connect(self, timeout: float) -> dict:
        try:
            ctx = (self.ssl_ctx or ssl.create_default_context()) if self.tls else None
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=ctx, server_hostname=self.host if ctx else None), timeout)
        except asyncio.TimeoutError:
            raise ConnectorError("CONN_TIMEOUT", f"no answer from {self.host}:{self.port}") from None
        except ssl.SSLError as exc:
            raise ConnectorError("TLS_FAILED", f"TLS with {self.host}:{self.port} failed: {exc.reason or exc}") from None
        except OSError as exc:
            code = "CONN_TIMEOUT" if isinstance(exc, TimeoutError) else "CONN_REFUSED"
            raise ConnectorError(code, f"cannot connect to {self.host}:{self.port}: {exc.strerror or exc}") from None
        self._send("CONNECT", P.connect(self.client_id, self.version, self.keepalive, True, self.username, self.password))
        try:
            ptype, flags, body = await asyncio.wait_for(self._read_packet(), timeout)
        except asyncio.TimeoutError:
            await self.close()
            raise ConnectorError("CONN_TIMEOUT", "the broker did not answer CONNECT") from None
        except (asyncio.IncompleteReadError, ConnectionError):
            await self.close()
            raise ConnectorError("CONN_CLOSED", "the broker closed the connection (wrong protocol version?)") from None
        if ptype != P.CONNACK:
            await self.close()
            raise ConnectorError("PROTOCOL_ERROR", f"expected CONNACK, got {P.NAMES.get(ptype, ptype)}")
        ack = P.decode(ptype, flags, body, self.version)
        if ack.return_code:
            await self.close()
            text = (P.CONNACK_5 if self.version == P.V5 else P.CONNACK_311).get(ack.return_code, f"code {ack.return_code}")
            raise ConnectorError("AUTH_FAILED" if ack.return_code in P.AUTH_CODES else "CONN_REFUSED",
                                 f"broker refused the connection: {text}")
        self.last_rx = time.monotonic()
        self._task = asyncio.create_task(self._read_loop())
        self._ping = asyncio.create_task(self._keepalive())
        return {"broker": f"{self.host}:{self.port}", "tls": self.tls, "protocol": "5.0" if self.version == P.V5 else "3.1.1"}

    async def subscribe(self, filters: list[tuple[str, int]], timeout: float) -> list[int]:
        pid = self._id()
        fut = asyncio.get_running_loop().create_future()
        self._waiting[(P.SUBACK, pid)] = fut
        self._send("SUBSCRIBE", P.subscribe(pid, filters, self.version))
        try:
            ack = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise ConnectorError("CONN_TIMEOUT", "the broker did not answer SUBSCRIBE") from None
        finally:
            self._waiting.pop((P.SUBACK, pid), None)
        return ack.codes

    async def close(self) -> None:
        for t in (self._ping, self._task):
            if t and t is not asyncio.current_task():
                t.cancel()
        if self.writer:
            try:
                if not self.writer.is_closing():
                    self._send("DISCONNECT", P.disconnect(self.version))
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ConnectorError):
                pass
        self.writer = None
        self.closed.set()

    # ------------------------------------------------------------ plumbing
    def _id(self) -> int:
        self._next_id = self._next_id % 65535 + 1
        return self._next_id

    def _send(self, name: str, data: bytes) -> None:
        self.guard.check(name)                                    # read-only guard (spec 12): never PUBLISH
        if not self.writer or self.writer.is_closing():
            raise ConnectorError("CONN_CLOSED", "not connected")
        self.writer.write(data)
        self.sent.append(name)

    async def _read_packet(self) -> tuple[int, int, bytes]:
        first = await self.reader.readexactly(1)
        mult, length = 1, 0
        for _ in range(4):
            b = (await self.reader.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        else:
            raise P.PacketError("bad remaining length")
        body = await self.reader.readexactly(length) if length else b""
        return first[0] >> 4, first[0] & 0x0F, body

    async def _read_loop(self) -> None:
        try:
            while True:
                ptype, flags, body = await self._read_packet()
                self.last_rx = time.monotonic()
                p = P.decode(ptype, flags, body, self.version)
                if ptype == P.PUBLISH:
                    self.received += 1
                    if p.qos == 1:
                        self._send("PUBACK", P.puback(p.packet_id, self.version))
                    elif p.qos == 2:                               # never subscribed with QoS 2; complete politely
                        self._send("PUBREC", P.packet(P.PUBREC, 0, struct.pack(">H", p.packet_id)))
                    if self.on_message:
                        self.on_message(p.topic, p.payload, p.retain)
                elif ptype == P.PUBREL:
                    self._send("PUBCOMP", P.packet(P.PUBCOMP, 0, struct.pack(">H", p.packet_id)))
                elif ptype in (P.SUBACK, P.UNSUBACK):
                    fut = self._waiting.get((ptype, p.packet_id))
                    if fut and not fut.done():
                        fut.set_result(p)
                elif ptype == P.DISCONNECT:
                    raise ConnectorError("CONN_CLOSED", f"the broker disconnected (reason {p.return_code})")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.close_reason = ConnectorError("CONN_CLOSED", "connection to the broker lost")
        except (P.PacketError, ConnectorError) as exc:
            self.close_reason = exc if isinstance(exc, ConnectorError) else ConnectorError("PROTOCOL_ERROR", str(exc))
        finally:
            self.closed.set()

    async def _keepalive(self) -> None:
        if not self.keepalive:
            return
        while True:
            await asyncio.sleep(max(1.0, self.keepalive / 2))
            if time.monotonic() - self.last_rx > self.keepalive * 1.5:
                self.close_reason = ConnectorError("CONN_TIMEOUT", "the broker stopped answering (keep-alive)")
                self.closed.set()
                if self.writer:
                    self.writer.close()
                return
            try:
                self._send("PINGREQ", P.pingreq())
            except ConnectorError:
                return
