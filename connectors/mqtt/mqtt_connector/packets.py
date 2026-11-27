"""MQTT 3.1.1 (OASIS) and MQTT 5.0 packet encoding and decoding — only what a read-only subscriber needs.

Encoders exist for CONNECT, SUBSCRIBE, UNSUBSCRIBE, PINGREQ, PUBACK and DISCONNECT. There is deliberately no
PUBLISH encoder in the connector (spec 12, 15.3); the test broker in tools/simulators has its own.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT, AUTH = 8, 9, 10, 11, 12, 13, 14, 15
NAMES = {1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK", 5: "PUBREC", 6: "PUBREL", 7: "PUBCOMP",
         8: "SUBSCRIBE", 9: "SUBACK", 10: "UNSUBSCRIBE", 11: "UNSUBACK", 12: "PINGREQ", 13: "PINGRESP",
         14: "DISCONNECT", 15: "AUTH"}
V311, V5 = 4, 5
CONNACK_311 = {1: "unacceptable protocol version", 2: "client identifier rejected", 3: "server unavailable",
               4: "bad user name or password", 5: "not authorized"}
CONNACK_5 = {0x80: "unspecified error", 0x84: "unsupported protocol version", 0x85: "client identifier not valid",
             0x86: "bad user name or password", 0x87: "not authorized", 0x88: "server unavailable", 0x89: "server busy",
             0x8A: "banned", 0x8C: "bad authentication method"}
AUTH_CODES = {4, 5, 0x86, 0x87, 0x8C}


class PacketError(ValueError):
    """Malformed MQTT data."""


# ------------------------------------------------------------------ primitives
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    mult, value = 1, 0
    for _ in range(4):
        if pos >= len(data):
            raise PacketError("truncated length")
        b = data[pos]
        pos += 1
        value += (b & 0x7F) * mult
        if not b & 0x80:
            return value, pos
        mult *= 128
    raise PacketError("length too long")


def utf8(s: str) -> bytes:
    b = s.encode()
    if len(b) > 65535:
        raise PacketError("string too long")
    return struct.pack(">H", len(b)) + b


def binary(b: bytes) -> bytes:
    return struct.pack(">H", len(b)) + b


def read_utf8(data: bytes, pos: int) -> tuple[str, int]:
    if pos + 2 > len(data):
        raise PacketError("truncated string")
    n = struct.unpack_from(">H", data, pos)[0]
    if pos + 2 + n > len(data):
        raise PacketError("truncated string")
    return data[pos + 2:pos + 2 + n].decode("utf-8", errors="replace"), pos + 2 + n


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + varint(len(body)) + body


def skip_properties(data: bytes, pos: int) -> int:
    n, pos = read_varint(data, pos)
    return pos + n


# ------------------------------------------------------------------ client -> server (no PUBLISH)
def connect(client_id: str, version: int, keepalive: int, clean: bool = True, username: str | None = None,
            password: str | None = None) -> bytes:
    flags = (0x02 if clean else 0) | (0x80 if username is not None else 0) | (0x40 if password is not None else 0)
    body = utf8("MQTT") + bytes([version, flags]) + struct.pack(">H", keepalive)
    if version == V5:
        body += varint(0)                                    # no CONNECT properties
    body += utf8(client_id)
    if username is not None:
        body += utf8(username)
    if password is not None:
        body += binary(password.encode())
    return packet(CONNECT, 0, body)


def subscribe(packet_id: int, filters: list[tuple[str, int]], version: int) -> bytes:
    body = struct.pack(">H", packet_id) + (varint(0) if version == V5 else b"")
    for topic, qos in filters:
        body += utf8(topic) + bytes([min(qos, 1)])           # QoS 0 or 1 only (spec 15.3)
    return packet(SUBSCRIBE, 0x02, body)


def unsubscribe(packet_id: int, filters: list[str], version: int) -> bytes:
    body = struct.pack(">H", packet_id) + (varint(0) if version == V5 else b"")
    return packet(UNSUBSCRIBE, 0x02, body + b"".join(utf8(t) for t in filters))


def puback(packet_id: int, version: int) -> bytes:
    return packet(PUBACK, 0, struct.pack(">H", packet_id))   # MQTT 5: reason 0 and no properties may be omitted


def pingreq() -> bytes:
    return packet(PINGREQ, 0, b"")


def disconnect(version: int) -> bytes:
    return packet(DISCONNECT, 0, b"")


# ------------------------------------------------------------------ server -> client
@dataclass
class Packet:
    type: int
    flags: int
    body: bytes
    # decoded fields (by type)
    topic: str = ""
    payload: bytes = b""
    qos: int = 0
    retain: bool = False
    packet_id: int = 0
    return_code: int = 0
    codes: list[int] = field(default_factory=list)

    @property
    def name(self) -> str:
        return NAMES.get(self.type, str(self.type))


def decode(ptype: int, flags: int, body: bytes, version: int) -> Packet:
    p = Packet(ptype, flags, body)
    try:
        if ptype == CONNACK:
            p.return_code = body[1]
        elif ptype == PUBLISH:
            p.qos, p.retain = (flags >> 1) & 3, bool(flags & 1)
            p.topic, pos = read_utf8(body, 0)
            if p.qos:
                p.packet_id = struct.unpack_from(">H", body, pos)[0]
                pos += 2
            if version == V5:
                pos = skip_properties(body, pos)
            p.payload = body[pos:]
        elif ptype in (SUBACK, UNSUBACK):
            p.packet_id = struct.unpack_from(">H", body, 0)[0]
            pos = 2
            if version == V5:
                pos = skip_properties(body, pos)
            p.codes = list(body[pos:])
        elif ptype in (PUBACK, PUBREC, PUBREL, PUBCOMP):
            p.packet_id = struct.unpack_from(">H", body, 0)[0]
        elif ptype == DISCONNECT and body:
            p.return_code = body[0]
    except (IndexError, struct.error):
        raise PacketError(f"malformed {NAMES.get(ptype, ptype)}") from None
    return p


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT topic filter matching with + and # (topics starting with $ only match explicit filters)."""
    if topic.startswith("$") and not filter_.startswith("$"):
        return False
    f, t = filter_.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)
