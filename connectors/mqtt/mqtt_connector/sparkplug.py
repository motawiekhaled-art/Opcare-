"""Eclipse Sparkplug B 3.0 payloads (Google Protocol Buffers) — a small decoder and encoder with no dependencies.

Topic: spBv1.0/{group_id}/{message_type}/{edge_node_id}[/{device_id}], message types NBIRTH, NDATA, NDEATH, DBIRTH,
DDATA, DDEATH (read) and NCMD, DCMD, STATE (ignored; the connector never sends commands).
Only scalar metrics are decoded; DataSet, Template and arrays are reported by name with q=4 (not supported).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Sparkplug B data types
INT8, INT16, INT32, INT64, UINT8, UINT16, UINT32, UINT64 = 1, 2, 3, 4, 5, 6, 7, 8
FLOAT, DOUBLE, BOOLEAN, STRING, DATETIME, TEXT, UUID, DATASET, BYTES, FILE, TEMPLATE = range(9, 20)
TYPE_NAMES = {1: "Int8", 2: "Int16", 3: "Int32", 4: "Int64", 5: "UInt8", 6: "UInt16", 7: "UInt32", 8: "UInt64",
              9: "Float", 10: "Double", 11: "Boolean", 12: "String", 13: "DateTime", 14: "Text", 15: "UUID",
              16: "DataSet", 17: "Bytes", 18: "File", 19: "Template"}
SIGNED_BITS = {INT8: 8, INT16: 16, INT32: 32, INT64: 64}
MESSAGE_TYPES = {"NBIRTH", "NDATA", "NDEATH", "DBIRTH", "DDATA", "DDEATH", "NCMD", "DCMD", "STATE"}


class SparkplugError(ValueError):
    """Not a valid Sparkplug B payload."""


@dataclass
class Metric:
    name: str | None = None
    alias: int | None = None
    timestamp: int | None = None
    datatype: int = 0
    is_null: bool = False
    is_historical: bool = False
    value: object = None
    supported: bool = True


@dataclass
class Payload:
    timestamp: int | None = None
    seq: int | None = None
    metrics: list[Metric] = field(default_factory=list)


# ------------------------------------------------------------------ protobuf wire format
def _varint(data: bytes, pos: int) -> tuple[int, int]:
    shift, value = 0, 0
    while True:
        if pos >= len(data):
            raise SparkplugError("truncated varint")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise SparkplugError("varint too long")


def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = _varint(data, pos)
        elif wire == 1:
            val, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            n, pos = _varint(data, pos)
            val, pos = data[pos:pos + n], pos + n
        elif wire == 5:
            val, pos = data[pos:pos + 4], pos + 4
        else:
            raise SparkplugError(f"unsupported wire type {wire}")
        if pos > len(data):
            raise SparkplugError("truncated field")
        yield num, wire, val


def _signed(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def _metric(data: bytes) -> Metric:
    m = Metric()
    raw = None
    for num, wire, val in _fields(data):
        if num == 1:
            m.name = val.decode("utf-8", errors="replace")
        elif num == 2:
            m.alias = val
        elif num == 3:
            m.timestamp = val
        elif num == 4:
            m.datatype = val
        elif num == 5:
            m.is_historical = bool(val)
        elif num == 7:
            m.is_null = bool(val)
        elif num in (10, 11, 14):
            raw = val
        elif num == 12:
            raw = struct.unpack("<f", val)[0]
        elif num == 13:
            raw = struct.unpack("<d", val)[0]
        elif num == 15:
            raw = val.decode("utf-8", errors="replace")
        elif num in (16, 17, 18):
            m.supported = False                               # bytes, DataSet, Template
    dt = m.datatype
    if m.is_null or raw is None:
        m.value = None
    elif dt in SIGNED_BITS:
        m.value = _signed(raw, SIGNED_BITS[dt])
    elif dt in (UINT8, UINT16, UINT32, UINT64, DATETIME):
        m.value = raw
    elif dt == BOOLEAN:
        m.value = bool(raw)
    elif dt in (FLOAT, DOUBLE):
        m.value = float(raw)
    elif dt in (STRING, TEXT, UUID):
        m.value = raw if isinstance(raw, str) else str(raw)
    else:
        m.supported = False
    return m


def decode(data: bytes) -> Payload:
    p = Payload()
    try:
        for num, _wire, val in _fields(data):
            if num == 1:
                p.timestamp = val
            elif num == 2:
                p.metrics.append(_metric(val))
            elif num == 3:
                p.seq = val
    except (struct.error, UnicodeDecodeError) as exc:
        raise SparkplugError(str(exc)) from None
    return p


def parse_topic(topic: str) -> tuple[str, str, str, str | None] | None:
    """'spBv1.0/g/NDATA/node[/dev]' -> (group, type, node, device). None if not Sparkplug B."""
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "spBv1.0" or parts[2] not in MESSAGE_TYPES:
        return None
    return parts[1], parts[2], parts[3], parts[4] if len(parts) > 4 else None


# ------------------------------------------------------------------ encoder (simulators and tests)
def _enc_varint(v: int) -> bytes:
    v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _key(num: int, wire: int) -> bytes:
    return _enc_varint((num << 3) | wire)


def _ld(num: int, data: bytes) -> bytes:
    return _key(num, 2) + _enc_varint(len(data)) + data


def encode_metric(name: str | None, datatype: int, value, alias: int | None = None, timestamp: int | None = None) -> bytes:
    out = b""
    if name is not None:
        out += _ld(1, name.encode())
    if alias is not None:
        out += _key(2, 0) + _enc_varint(alias)
    if timestamp is not None:
        out += _key(3, 0) + _enc_varint(timestamp)
    out += _key(4, 0) + _enc_varint(datatype)
    if value is None:
        return out + _key(7, 0) + b"\x01"
    if datatype in (INT8, INT16, INT32, UINT8, UINT16, UINT32):
        out += _key(10, 0) + _enc_varint(int(value) & 0xFFFFFFFF)
    elif datatype in (INT64, UINT64, DATETIME):
        out += _key(11, 0) + _enc_varint(int(value))
    elif datatype == FLOAT:
        out += _key(12, 5) + struct.pack("<f", value)
    elif datatype == DOUBLE:
        out += _key(13, 1) + struct.pack("<d", value)
    elif datatype == BOOLEAN:
        out += _key(14, 0) + _enc_varint(1 if value else 0)
    elif datatype in (STRING, TEXT, UUID):
        out += _ld(15, str(value).encode())
    return out


def encode(metrics: list[bytes], timestamp: int | None = None, seq: int | None = None) -> bytes:
    out = b""
    if timestamp is not None:
        out += _key(1, 0) + _enc_varint(timestamp)
    for m in metrics:
        out += _ld(2, m)
    if seq is not None:
        out += _key(3, 0) + _enc_varint(seq)
    return out
