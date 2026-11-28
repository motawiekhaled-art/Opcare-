"""DNP3 data link and transport layers (IEEE 1815-2012 sections 9 and 8), for both master and outstation roles.

Link frame: 0x05 0x64, LEN, CTRL, DEST (LE), SRC (LE), CRC, then user data in blocks of up to 16 bytes, each
followed by a CRC. CRC-16/DNP: polynomial 0x3D65 (reflected 0xA6BC), complemented, little-endian.
Transport: one header byte (FIN 0x80, FIR 0x40, 6-bit sequence) and up to 249 bytes of application data.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

START = b"\x05\x64"
MAX_USER_DATA = 250
MAX_SEGMENT = 249

# CTRL bits
DIR, PRM, FCB, FCV = 0x80, 0x40, 0x20, 0x10
# primary function codes
P_RESET_LINK, P_TEST_LINK, P_CONFIRMED_DATA, P_UNCONFIRMED_DATA, P_REQUEST_LINK_STATUS = 0, 2, 3, 4, 9
# secondary function codes
S_ACK, S_NACK, S_LINK_STATUS, S_NOT_SUPPORTED = 0, 1, 11, 15


class LinkError(Exception):
    """Bad frame: wrong start bytes, length or CRC."""


def _table() -> list[int]:
    out = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA6BC if c & 1 else c >> 1
        out.append(c)
    return out


_CRC = _table()


def crc(data: bytes) -> bytes:
    c = 0
    for b in data:
        c = (c >> 8) ^ _CRC[(c ^ b) & 0xFF]
    return struct.pack("<H", ~c & 0xFFFF)


@dataclass
class Frame:
    ctrl: int
    dest: int
    src: int
    data: bytes = b""

    @property
    def function(self) -> int:
        return self.ctrl & 0x0F

    @property
    def primary(self) -> bool:
        return bool(self.ctrl & PRM)

    @property
    def from_master(self) -> bool:
        return bool(self.ctrl & DIR)


def encode(frame: Frame) -> bytes:
    if len(frame.data) > MAX_USER_DATA:
        raise LinkError("user data longer than 250 bytes")
    head = START + bytes([5 + len(frame.data), frame.ctrl]) + struct.pack("<HH", frame.dest, frame.src)
    out = bytearray(head + crc(head))
    for i in range(0, len(frame.data), 16):
        block = frame.data[i:i + 16]
        out += block + crc(block)
    return bytes(out)


def body_size(length: int) -> int:
    """Bytes after the 10-byte header for a LEN field value (user data plus block CRCs)."""
    n = length - 5
    return n + 2 * ((n + 15) // 16)


def decode_header(head: bytes) -> tuple[int, int, int, int]:
    """Returns (LEN, CTRL, DEST, SRC); checks start bytes and header CRC."""
    if head[:2] != START:
        raise LinkError("frame does not start with 0x0564")
    if crc(head[:8]) != head[8:10]:
        raise LinkError("header CRC error")
    length, ctrl = head[2], head[3]
    if length < 5:
        raise LinkError("LEN below 5")
    dest, src = struct.unpack_from("<HH", head, 4)
    return length, ctrl, dest, src


def decode_body(length: int, body: bytes) -> bytes:
    n, out, pos = length - 5, bytearray(), 0
    while len(out) < n:
        size = min(16, n - len(out))
        block, check = body[pos:pos + size], body[pos + size:pos + size + 2]
        if crc(block) != check:
            raise LinkError("data block CRC error")
        out += block
        pos += size + 2
    return bytes(out)


def decode(raw: bytes) -> Frame:
    length, ctrl, dest, src = decode_header(raw[:10])
    return Frame(ctrl, dest, src, decode_body(length, raw[10:10 + body_size(length)]))


async def read_frame(reader) -> Frame:
    """Reads one frame from an asyncio stream, resynchronising on garbage before 0x0564."""
    while True:
        b = await reader.readexactly(1)
        if b != b"\x05":
            continue
        b2 = await reader.readexactly(1)
        if b2 != b"\x64":
            continue
        head = START + await reader.readexactly(8)
        length, ctrl, dest, src = decode_header(head)
        data = decode_body(length, await reader.readexactly(body_size(length)))
        return Frame(ctrl, dest, src, data)


# ------------------------------------------------------------------ transport
def segments(fragment: bytes, seq: int) -> tuple[list[bytes], int]:
    """Splits an application fragment into transport segments. Returns (segments, next sequence)."""
    parts = [fragment[i:i + MAX_SEGMENT] for i in range(0, len(fragment), MAX_SEGMENT)] or [b""]
    out = []
    for i, p in enumerate(parts):
        head = (0x40 if i == 0 else 0) | (0x80 if i == len(parts) - 1 else 0) | (seq & 0x3F)
        out.append(bytes([head]) + p)
        seq = (seq + 1) & 0x3F
    return out, seq


class Reassembly:
    """Collects transport segments into application fragments."""

    def __init__(self, max_size: int = 65536):
        self.buf: bytearray | None = None
        self.seq = 0
        self.max_size = max_size

    def add(self, segment: bytes) -> bytes | None:
        if not segment:
            return None
        head, data = segment[0], segment[1:]
        fir, fin, seq = bool(head & 0x40), bool(head & 0x80), head & 0x3F
        if fir:
            self.buf = bytearray()
        elif self.buf is None or seq != self.seq:
            self.buf = None                    # lost a segment: drop the fragment (spec: discard)
            return None
        self.buf += data
        self.seq = (seq + 1) & 0x3F
        if len(self.buf) > self.max_size:
            self.buf = None
            return None
        if fin:
            out, self.buf = bytes(self.buf), None
            return out
        return None
