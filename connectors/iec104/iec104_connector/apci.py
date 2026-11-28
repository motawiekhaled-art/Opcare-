"""IEC 60870-5-104 APCI (section 5): APDU framing and the I, S and U control fields.

APDU = 0x68, length (4..253), four control octets, then (I-format only) one ASDU. Sequence numbers are 15 bits.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

START = 0x68
MAX_LEN = 253                       # length octet: control field + ASDU
MOD = 32768                         # 15-bit sequence numbers

STARTDT_ACT, STARTDT_CON = 0x07, 0x0B
STOPDT_ACT, STOPDT_CON = 0x13, 0x23
TESTFR_ACT, TESTFR_CON = 0x43, 0x83
U_NAMES = {STARTDT_ACT: "STARTDT_ACT", STARTDT_CON: "STARTDT_CON", STOPDT_ACT: "STOPDT_ACT",
           STOPDT_CON: "STOPDT_CON", TESTFR_ACT: "TESTFR_ACT", TESTFR_CON: "TESTFR_CON"}


class ApciError(ValueError):
    pass


@dataclass
class Apdu:
    kind: str                       # "I", "S" or "U"
    ns: int = 0                     # send sequence number N(S) (I only)
    nr: int = 0                     # receive sequence number N(R) (I and S)
    u: int = 0                      # U function (U only)
    asdu: bytes = b""

    @property
    def name(self) -> str:
        return U_NAMES.get(self.u, f"U{self.u:02X}") if self.kind == "U" else self.kind


def i_frame(ns: int, nr: int, asdu: bytes) -> bytes:
    if not asdu or len(asdu) > MAX_LEN - 4:
        raise ApciError(f"ASDU of {len(asdu)} octets does not fit an APDU")
    return bytes([START, 4 + len(asdu), (ns << 1) & 0xFE, (ns >> 7) & 0xFF, (nr << 1) & 0xFE, (nr >> 7) & 0xFF]) + asdu


def s_frame(nr: int) -> bytes:
    return bytes([START, 4, 0x01, 0x00, (nr << 1) & 0xFE, (nr >> 7) & 0xFF])


def u_frame(function: int) -> bytes:
    if function not in U_NAMES:
        raise ApciError(f"unknown U function {function:#x}")
    return bytes([START, 4, function, 0, 0, 0])


def decode(body: bytes) -> Apdu:
    """body: the octets after the length octet (control field + ASDU)."""
    if len(body) < 4:
        raise ApciError("APDU shorter than its control field")
    c1, c2, c3, c4 = body[:4]
    if c1 & 0x01 == 0:
        return Apdu("I", ns=(c1 >> 1) | (c2 << 7), nr=(c3 >> 1) | (c4 << 7), asdu=bytes(body[4:]))
    if len(body) != 4:
        raise ApciError("S or U frame with an ASDU")
    if c1 & 0x03 == 0x01:
        return Apdu("S", nr=(c3 >> 1) | (c4 << 7))
    if c1 not in U_NAMES:
        raise ApciError(f"unknown U function {c1:#x}")
    return Apdu("U", u=c1)


async def read_apdu(reader: asyncio.StreamReader) -> Apdu:
    head = await reader.readexactly(2)
    if head[0] != START:
        raise ApciError(f"APDU does not start with 0x68 (got {head[0]:#04x})")
    if not 4 <= head[1] <= MAX_LEN:
        raise ApciError(f"APDU length {head[1]} out of range")
    return decode(await reader.readexactly(head[1]))


def seq_distance(a: int, b: int) -> int:
    """How many frames b is ahead of a, modulo 2^15."""
    return (b - a) % MOD
