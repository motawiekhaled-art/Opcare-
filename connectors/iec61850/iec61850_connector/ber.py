"""Minimal ASN.1 BER (ITU-T X.690) for the ISO presentation, ACSE and MMS layers: tags of any class and number,
definite lengths, constructed values. Values are kept as (tag, bytes) pairs; callers decode what they need.

A tag is written as (class, constructed, number), with class 0 universal, 1 application, 2 context, 3 private.
"""
from __future__ import annotations

UNIVERSAL, APPLICATION, CONTEXT = 0, 1, 2


class BerError(Exception):
    """Malformed BER."""


def tag(cls: int, constructed: bool, number: int) -> bytes:
    first = (cls << 6) | (0x20 if constructed else 0)
    if number < 31:
        return bytes([first | number])
    out = [number & 0x7F]
    number >>= 7
    while number:
        out.append(0x80 | (number & 0x7F))
        number >>= 7
    return bytes([first | 0x1F]) + bytes(reversed(out))


def length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(cls: int, constructed: bool, number: int, content: bytes) -> bytes:
    return tag(cls, constructed, number) + length(len(content)) + content


def ctx(number: int, content: bytes, constructed: bool = True) -> bytes:
    return tlv(CONTEXT, constructed, number, content)


def ctx_prim(number: int, content: bytes) -> bytes:
    return tlv(CONTEXT, False, number, content)


def app(number: int, content: bytes, constructed: bool = True) -> bytes:
    return tlv(APPLICATION, constructed, number, content)


def seq(*items: bytes) -> bytes:
    return tlv(UNIVERSAL, True, 16, b"".join(items))


def integer_bytes(v: int) -> bytes:
    n = max(1, (v + (v < 0)).bit_length() // 8 + 1)
    return v.to_bytes(n, "big", signed=True)


def integer(v: int, number: int | None = None) -> bytes:
    return tlv(CONTEXT, False, number, integer_bytes(v)) if number is not None else tlv(UNIVERSAL, False, 2, integer_bytes(v))


def oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return tlv(UNIVERSAL, False, 6, bytes(out))


def bitstring_content(bits: list[bool]) -> bytes:
    """Content octets of a BIT STRING (first octet = unused bits), bit 0 = most significant bit of octet 1."""
    n = (len(bits) + 7) // 8
    data = bytearray(n)
    for i, b in enumerate(bits):
        if b:
            data[i // 8] |= 0x80 >> (i % 8)
    return bytes([n * 8 - len(bits)]) + bytes(data)


def bits_of(content: bytes) -> list[bool]:
    if not content:
        return []
    unused, data = content[0], content[1:]
    total = len(data) * 8 - unused
    return [bool(data[i // 8] & (0x80 >> (i % 8))) for i in range(total)]


class Tlv:
    """A decoded BER element: cls, constructed, number, value bytes; children() parses constructed content."""
    __slots__ = ("cls", "constructed", "number", "value")

    def __init__(self, cls: int, constructed: bool, number: int, value: bytes):
        self.cls, self.constructed, self.number, self.value = cls, constructed, number, value

    def children(self) -> list["Tlv"]:
        return parse_all(self.value)

    def child(self, number: int, cls: int = CONTEXT) -> "Tlv | None":
        for c in self.children():
            if c.number == number and c.cls == cls:
                return c
        return None

    def int(self) -> int:
        return int.from_bytes(self.value, "big", signed=True) if self.value else 0

    def uint(self) -> int:
        return int.from_bytes(self.value, "big")

    def text(self) -> str:
        return self.value.decode("latin-1")

    def __repr__(self) -> str:
        return f"Tlv({self.cls},{int(self.constructed)},{self.number},{self.value.hex()[:40]})"


def parse(data: bytes, pos: int = 0) -> tuple[Tlv, int]:
    if pos + 2 > len(data):
        raise BerError("truncated element")
    first = data[pos]
    cls, constructed, number = first >> 6, bool(first & 0x20), first & 0x1F
    pos += 1
    if number == 0x1F:
        number = 0
        while True:
            if pos >= len(data):
                raise BerError("truncated tag")
            b = data[pos]
            pos += 1
            number = (number << 7) | (b & 0x7F)
            if not b & 0x80:
                break
    if pos >= len(data):
        raise BerError("truncated length")
    ln = data[pos]
    pos += 1
    if ln == 0x80:
        raise BerError("indefinite lengths are not used by MMS")
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(data[pos:pos + k], "big")
        pos += k
    if pos + ln > len(data):
        raise BerError("element longer than its container")
    return Tlv(cls, constructed, number, data[pos:pos + ln]), pos + ln


def parse_all(data: bytes) -> list[Tlv]:
    out, pos = [], 0
    while pos < len(data):
        t, pos = parse(data, pos)
        out.append(t)
    return out
