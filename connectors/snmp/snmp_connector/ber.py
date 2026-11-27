"""BER encoding of SNMP messages (RFC 3416 PDUs, RFC 1157 / RFC 1901 / RFC 3412 messages, RFC 3414 USM).

Only what a read-only manager needs. There is deliberately no encoder for SetRequest (spec 12, 15.9).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# universal / application tags
INTEGER, OCTET_STRING, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
IP_ADDRESS, COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW = 0x80, 0x81, 0x82

# PDU tags
GET, GET_NEXT, RESPONSE, SET, TRAP_V1, GET_BULK, INFORM, TRAP_V2, REPORT = (
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8)
PDU_NAMES = {GET: "GetRequest", GET_NEXT: "GetNextRequest", RESPONSE: "Response", SET: "SetRequest",
             TRAP_V1: "Trap", GET_BULK: "GetBulkRequest", INFORM: "InformRequest", TRAP_V2: "SNMPv2-Trap",
             REPORT: "Report"}
TYPE_NAMES = {INTEGER: "INTEGER", OCTET_STRING: "OCTET STRING", NULL: "NULL", OID: "OBJECT IDENTIFIER",
              IP_ADDRESS: "IpAddress", COUNTER32: "Counter32", GAUGE32: "Gauge32", TIMETICKS: "TimeTicks",
              OPAQUE: "Opaque", COUNTER64: "Counter64", NO_SUCH_OBJECT: "noSuchObject",
              NO_SUCH_INSTANCE: "noSuchInstance", END_OF_MIB_VIEW: "endOfMibView"}
EXCEPTIONS = (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW)
ERROR_NAMES = {0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr",
               6: "noAccess", 7: "wrongType", 8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue",
               11: "noCreation", 12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed",
               15: "undoFailed", 16: "authorizationError", 17: "notWritable", 18: "inconsistentName"}


class BerError(ValueError):
    """Malformed BER data."""


# ------------------------------------------------------------------ encoding
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _len(len(content)) + content


def enc_int(v: int, tag: int = INTEGER) -> bytes:
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):           # unsigned: add a 0 byte if the top bit is set
        b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big")
    else:
        b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True)
    return tlv(tag, b)


def enc_str(v: bytes | str) -> bytes:
    return tlv(OCTET_STRING, v.encode() if isinstance(v, str) else v)


def enc_oid(oid: str) -> bytes:
    parts = [int(x) for x in oid.strip(".").split(".")]
    if len(parts) < 2 or parts[0] > 2 or (parts[0] < 2 and parts[1] > 39):
        raise BerError(f"invalid OID {oid}")
    out = bytearray()
    for i, n in enumerate([parts[0] * 40 + parts[1]] + parts[2:]):
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        out += bytes(reversed(chunk))
    return tlv(OID, bytes(out))


def enc_value(tag: int, value: Any) -> bytes:
    if tag in (INTEGER, COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return enc_int(int(value), tag)
    if tag == OCTET_STRING:
        return enc_str(value)
    if tag == OID:
        return enc_oid(value)
    if tag == IP_ADDRESS:
        return tlv(IP_ADDRESS, bytes(int(x) for x in str(value).split(".")))
    if tag == OPAQUE:
        return tlv(OPAQUE, value)
    return tlv(tag, b"")                                         # NULL and the exception values


def seq(*items: bytes) -> bytes:
    return tlv(SEQUENCE, b"".join(items))


@dataclass
class VarBind:
    oid: str
    tag: int = NULL
    value: Any = None

    def encode(self) -> bytes:
        return seq(enc_oid(self.oid), enc_value(self.tag, self.value))

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.tag, f"0x{self.tag:02x}")

    @property
    def is_exception(self) -> bool:
        return self.tag in EXCEPTIONS


@dataclass
class Pdu:
    tag: int
    request_id: int
    error_status: int = 0            # non-repeaters for GetBulkRequest
    error_index: int = 0             # max-repetitions for GetBulkRequest
    varbinds: list[VarBind] = field(default_factory=list)
    # SNMPv1 Trap only
    enterprise: str = ""
    agent_addr: str = "0.0.0.0"
    generic_trap: int = 0
    specific_trap: int = 0
    timestamp: int = 0

    def encode(self) -> bytes:
        if self.tag == SET:
            raise BerError("SetRequest is never encoded: the connector is read-only (spec 12)")
        vbs = seq(*(vb.encode() for vb in self.varbinds))
        if self.tag == TRAP_V1:
            body = (enc_oid(self.enterprise) + enc_value(IP_ADDRESS, self.agent_addr) + enc_int(self.generic_trap)
                    + enc_int(self.specific_trap) + enc_int(self.timestamp, TIMETICKS) + vbs)
        else:
            body = enc_int(self.request_id) + enc_int(self.error_status) + enc_int(self.error_index) + vbs
        return tlv(self.tag, body)

    @property
    def name(self) -> str:
        return PDU_NAMES.get(self.tag, f"0x{self.tag:02x}")


# ------------------------------------------------------------------ decoding
def read_tlv(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """(tag, content, position after) at data[pos]."""
    try:
        tag = data[pos]
        n = data[pos + 1]
        pos += 2
        if n & 0x80:
            k = n & 0x7F
            if k == 0 or k > 4:
                raise BerError("unsupported length")
            n = int.from_bytes(data[pos:pos + k], "big")
            pos += k
    except IndexError:
        raise BerError("truncated") from None
    if pos + n > len(data):
        raise BerError("truncated")
    return tag, data[pos:pos + n], pos + n


def items(content: bytes) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(content):
        tag, val, pos = read_tlv(content, pos)
        out.append((tag, val))
    return out


def dec_int(b: bytes, signed: bool = True) -> int:
    if not b:
        raise BerError("empty integer")
    return int.from_bytes(b, "big", signed=signed)


def dec_oid(b: bytes) -> str:
    if not b:
        raise BerError("empty OID")
    nums, n = [], 0
    for byte in b:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            nums.append(n)
            n = 0
    first = nums[0]
    head = [0, first] if first < 40 else [1, first - 40] if first < 80 else [2, first - 80]
    return ".".join(str(x) for x in head + nums[1:])


def dec_value(tag: int, b: bytes) -> Any:
    if tag == INTEGER:
        return dec_int(b)
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return dec_int(b, signed=False)
    if tag == OCTET_STRING or tag == OPAQUE:
        return bytes(b)
    if tag == OID:
        return dec_oid(b)
    if tag == IP_ADDRESS:
        return ".".join(str(x) for x in b)
    return None


def dec_pdu(tag: int, content: bytes) -> Pdu:
    parts = items(content)
    if tag == TRAP_V1:
        if len(parts) != 6:
            raise BerError("bad v1 trap")
        p = Pdu(tag, 0, enterprise=dec_oid(parts[0][1]), agent_addr=dec_value(IP_ADDRESS, parts[1][1]),
                generic_trap=dec_int(parts[2][1]), specific_trap=dec_int(parts[3][1]),
                timestamp=dec_int(parts[4][1], signed=False))
        vb_content = parts[5][1]
    else:
        if len(parts) != 4:
            raise BerError("bad PDU")
        p = Pdu(tag, dec_int(parts[0][1]), dec_int(parts[1][1]), dec_int(parts[2][1]))
        vb_content = parts[3][1]
    for t, vb in items(vb_content):
        if t != SEQUENCE:
            raise BerError("bad varbind")
        (ot, ob), (vt, vv) = items(vb)
        if ot != OID:
            raise BerError("bad varbind name")
        p.varbinds.append(VarBind(dec_oid(ob), vt, dec_value(vt, vv)))
    return p


def oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(x) for x in oid.strip(".").split("."))


def in_subtree(oid: str, root: str) -> bool:
    o, r = oid_tuple(oid), oid_tuple(root)
    return o[:len(r)] == r
