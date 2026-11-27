"""Asyncio SNMP manager: GetRequest, GetNextRequest and GetBulkRequest over UDP, v1/v2c/v3 (USM).

Every request passes the read-only guard (spec 12): SetRequest has no code path at all, and any other request
type is refused before it is encoded.
"""
from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
import time
from dataclasses import dataclass, field

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

from .ber import GET, GET_BULK, GET_NEXT, PDU_NAMES, REPORT, RESPONSE, BerError, Pdu, VarBind
from .messages import (USM_REPORTS, V3, UsmUser, decode_community, decode_v3, decrypt_v3, encode_community,
                       encode_v3, peek_version, verify_v3)


class SnmpTimeout(Exception):
    """No (valid) response after all retries."""


class SnmpError(Exception):
    """The agent answered with a non-zero error-status."""

    def __init__(self, status: int, index: int):
        super().__init__(f"error-status {status} at index {index}")
        self.status, self.index = status, index


class SnmpAuthError(Exception):
    """SNMPv3 USM report: wrong user, password, security level or privacy key."""

    def __init__(self, report: str):
        super().__init__(report)
        self.report = report


@dataclass
class Target:
    id: str
    host: str
    port: int
    ip: str | None = None
    # SNMPv3 authoritative engine (discovered)
    engine_id: bytes | None = None
    boots: int = 0
    time: int = 0
    t_rec: float = 0.0
    keys: tuple = (None, None)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_latency_ms: float | None = None

    def engine_time(self) -> int:
        return self.time + int(time.monotonic() - self.t_rec)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, client: "SnmpClient"):
        self.client = client

    def datagram_received(self, data: bytes, addr) -> None:
        self.client._received(data, addr[0])

    def error_received(self, exc: Exception) -> None:   # e.g. ICMP port unreachable: the request simply times out
        pass


async def resolve(host: str, port: int) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError as exc:
        raise ConnectorError("CONN_REFUSED", f"cannot resolve {host}: {exc}") from None
    return infos[0][4][0]


class SnmpClient:
    def __init__(self, version: int, guard: ReadOnlyGuard, community: bytes = b"", user: UsmUser | None = None,
                 timeout_s: float = 3.0, retries: int = 1, context_name: str = ""):
        self.version, self.guard, self.community, self.user = version, guard, community, user
        self.timeout_s, self.retries, self.context_name = timeout_s, retries, context_name.encode()
        self.transports: dict[int, asyncio.DatagramTransport] = {}
        self.pending: dict[int, tuple[asyncio.Future, str]] = {}
        self.requests = 0
        self.bad_datagrams = 0
        self._next_id = random.randint(1, 1 << 30)

    async def _transport(self, ip: str) -> asyncio.DatagramTransport:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        if fam not in self.transports:
            t, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Proto(self), local_addr=("::" if fam == socket.AF_INET6 else "0.0.0.0", 0), family=fam)
            self.transports[fam] = t
        return self.transports[fam]

    def close(self) -> None:
        for t in self.transports.values():
            t.close()
        self.transports = {}
        for fut, _ in self.pending.values():
            if not fut.done():
                fut.cancel()
        self.pending = {}

    def _id(self) -> int:
        self._next_id = self._next_id % 2147483646 + 1
        return self._next_id

    # ------------------------------------------------------------ receive
    def _received(self, data: bytes, ip: str) -> None:
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        try:
            if peek_version(data) == V3:
                m, whole = decode_v3(data)
                key, payload = m.msg_id, (m, whole)
            else:
                _, community, pdu = decode_community(data)
                if community != self.community:
                    self.bad_datagrams += 1
                    return
                key, payload = pdu.request_id, pdu
        except (BerError, IndexError, ValueError):
            self.bad_datagrams += 1
            return
        entry = self.pending.get(key)
        if not entry or entry[1] != ip or entry[0].done():
            self.bad_datagrams += 1
            return
        entry[0].set_result(payload)

    # ------------------------------------------------------------ send
    async def _exchange(self, target: Target, key: int, data: bytes) -> object:
        """Send and wait for the reply matching `key`, with retries."""
        if target.ip is None:
            target.ip = await resolve(target.host, target.port)
        tr = await self._transport(target.ip)
        loop = asyncio.get_running_loop()
        for _attempt in range(self.retries + 1):
            fut = loop.create_future()
            self.pending[key] = (fut, target.ip)
            t0 = time.monotonic()
            self.requests += 1
            tr.sendto(data, (target.ip, target.port))
            try:
                res = await asyncio.wait_for(fut, self.timeout_s)
                target.last_latency_ms = round((time.monotonic() - t0) * 1000, 1)
                return res
            except asyncio.TimeoutError:
                continue
            finally:
                self.pending.pop(key, None)
        raise SnmpTimeout(f"no response from {target.host}:{target.port} "
                          f"({self.retries + 1} x {self.timeout_s:g} s)")

    async def request(self, target: Target, tag: int, oids: list[str], non_repeaters: int = 0,
                      max_repetitions: int = 0) -> Pdu:
        self.guard.check(PDU_NAMES[tag])                       # read-only guard (spec 12): before encoding
        if tag not in (GET, GET_NEXT, GET_BULK):
            raise ConnectorError("OP_FORBIDDEN", f"{PDU_NAMES[tag]} is not a read request")
        if tag == GET_BULK and self.version == 0:
            raise ConnectorError("ADDR_INVALID", "GetBulkRequest needs SNMP v2c or v3")
        vbs = [VarBind(o) for o in oids]
        async with target.lock:
            if self.version == V3:
                pdu = await self._request_v3(target, tag, vbs, non_repeaters, max_repetitions)
            else:
                rid = self._id()
                out = Pdu(tag, rid, non_repeaters, max_repetitions, vbs) if tag == GET_BULK else Pdu(tag, rid, 0, 0, vbs)
                pdu = await self._exchange(target, rid, encode_community(self.version, self.community, out))
        if pdu.tag != RESPONSE:
            raise ConnectorError("PROTOCOL_ERROR", f"unexpected {pdu.name}")
        if pdu.error_status:
            raise SnmpError(pdu.error_status, pdu.error_index)
        return pdu

    # ------------------------------------------------------------ SNMPv3 / USM
    async def discover(self, target: Target) -> None:
        """Learn the agent's engine id, boots and time (RFC 3414 section 4)."""
        self.guard.check("GetRequest")
        mid = self._id()
        data = encode_v3(mid, Pdu(GET, self._id()), None, b"", 0, 0, (None, None), b"")
        m, _ = await self._exchange(target, mid, data)
        if not m.engine_id:
            raise ConnectorError("PROTOCOL_ERROR", "agent did not report its engine id")
        target.engine_id, target.boots, target.time, target.t_rec = m.engine_id, m.boots, m.time, time.monotonic()
        target.keys = self.user.keys(m.engine_id)

    async def _request_v3(self, target: Target, tag: int, vbs: list[VarBind], nr: int, mr: int) -> Pdu:
        if target.engine_id is None:
            await self.discover(target)
        for attempt in range(2):
            mid = self._id()
            pdu = Pdu(tag, self._id(), nr if tag == GET_BULK else 0, mr if tag == GET_BULK else 0, vbs)
            boots, etime = (target.boots, target.engine_time()) if self.user.auth else (0, 0)
            data = encode_v3(mid, pdu, self.user, target.engine_id, boots, etime, target.keys, target.engine_id,
                             self.context_name)
            m, whole = await self._exchange(target, mid, data)
            authed = bool(m.flags & 1)
            if authed:
                if not verify_v3(m, whole, self.user, target.keys[0]):
                    raise SnmpAuthError("bad response digest")
                if m.encrypted is not None:
                    try:
                        decrypt_v3(m, target.keys[1])
                    except (BerError, ValueError):
                        raise SnmpAuthError("cannot decrypt the response") from None
                target.boots, target.time, target.t_rec = m.boots, m.time, time.monotonic()
            if m.pdu is None:
                raise SnmpAuthError("encrypted response without privacy")
            if m.pdu.tag == REPORT:
                oid = m.pdu.varbinds[0].oid if m.pdu.varbinds else ""
                report = USM_REPORTS.get(oid, f"report {oid}")
                if report == "usmStatsNotInTimeWindows" and attempt == 0:
                    target.boots, target.time, target.t_rec = m.boots, m.time, time.monotonic()
                    continue                                       # re-synchronised: try once more
                if report == "usmStatsUnknownEngineIDs" and attempt == 0:
                    await self.discover(target)
                    continue
                raise SnmpAuthError(report)
            if self.user.auth and not authed:
                raise SnmpAuthError("unauthenticated response to an authenticated request")
            return m.pdu
        raise SnmpAuthError("usmStatsNotInTimeWindows")
