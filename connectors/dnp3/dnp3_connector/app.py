"""DNP3 application layer (IEEE 1815-2012 section 4.2 and Appendix A): fragment headers, object headers with their
qualifiers, and the object formats a read-only master receives.

Request builders exist only for what spec OSS-DAQ-CON-001 section 15.6 allows: CONFIRM, READ, ENABLE/DISABLE
UNSOLICITED and the WRITE that clears IIN1.7 (g80v1 index 7). There is no encoder for controls (g12, g41), freeze,
restart, time synchronisation or file services. The outstation encoders below are used by the test outstation.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

# function codes
FC_CONFIRM, FC_READ, FC_WRITE, FC_ENABLE_UNSOL, FC_DISABLE_UNSOL = 0, 1, 2, 20, 21
FC_RESPONSE, FC_UNSOLICITED = 129, 130
FC_NAMES = {0: "CONFIRM", 1: "READ", 2: "WRITE", 3: "SELECT", 4: "OPERATE", 5: "DIRECT_OPERATE",
            6: "DIRECT_OPERATE_NR", 7: "IMMED_FREEZE", 8: "IMMED_FREEZE_NR", 9: "FREEZE_CLEAR", 10: "FREEZE_CLEAR_NR",
            11: "FREEZE_AT_TIME", 12: "FREEZE_AT_TIME_NR", 13: "COLD_RESTART", 14: "WARM_RESTART",
            15: "INITIALIZE_DATA", 16: "INITIALIZE_APPL", 17: "START_APPL", 18: "STOP_APPL", 19: "SAVE_CONFIG",
            20: "ENABLE_UNSOLICITED", 21: "DISABLE_UNSOLICITED", 22: "ASSIGN_CLASS", 23: "DELAY_MEASURE",
            24: "RECORD_CURRENT_TIME", 25: "OPEN_FILE", 26: "CLOSE_FILE", 27: "DELETE_FILE", 28: "GET_FILE_INFO",
            29: "AUTHENTICATE_FILE", 30: "ABORT_FILE", 129: "RESPONSE", 130: "UNSOLICITED_RESPONSE"}
# application control bits
AC_FIR, AC_FIN, AC_CON, AC_UNS = 0x80, 0x40, 0x20, 0x10
# IIN bits (IIN1 in the low byte, IIN2 in the high byte)
IIN_NAMES = {0: "BROADCAST", 1: "CLASS_1_EVENTS", 2: "CLASS_2_EVENTS", 3: "CLASS_3_EVENTS", 4: "NEED_TIME",
             5: "LOCAL_CONTROL", 6: "DEVICE_TROUBLE", 7: "DEVICE_RESTART", 8: "NO_FUNC_CODE_SUPPORT",
             9: "OBJECT_UNKNOWN", 10: "PARAMETER_ERROR", 11: "EVENT_BUFFER_OVERFLOW", 12: "ALREADY_EXECUTING",
             13: "CONFIG_CORRUPT"}
IIN_RESTART, IIN_TROUBLE, IIN_OVERFLOW = 1 << 7, 1 << 6, 1 << 11
IIN_CLASS = {1: 1 << 1, 2: 1 << 2, 3: 1 << 3}

# point kinds (spec 15.6 address.group) and their static / event groups
KINDS = {"binary_input": (1, 2), "double_bit_binary_input": (3, 4), "binary_output_status": (10, 11),
         "counter": (20, 22), "frozen_counter": (21, 23), "analog_input": (30, 32), "analog_output_status": (40, 42)}
KIND_OF_GROUP = {g: k for k, gs in KINDS.items() for g in gs}
EVENT_GROUPS = {2, 4, 11, 22, 23, 32, 42}
LABEL = {"binary_input": "Binary input", "double_bit_binary_input": "Double-bit input",
         "binary_output_status": "Binary output status", "counter": "Counter", "frozen_counter": "Frozen counter",
         "analog_input": "Analog input", "analog_output_status": "Analog output status"}

# flag bits
ONLINE, RESTART, COMM_LOST, REMOTE_FORCED, LOCAL_FORCED = 0x01, 0x02, 0x04, 0x08, 0x10
CHATTER_FILTER = ROLLOVER = OVER_RANGE = 0x20
REFERENCE_ERR = DISCONTINUITY = 0x40
STATE = 0x80


class AppError(Exception):
    """Malformed fragment or an object we cannot size."""


def flag_names(kind: str, flags: int) -> list[str]:
    bit5 = {"counter": "ROLLOVER", "frozen_counter": "ROLLOVER", "analog_input": "OVER_RANGE",
            "analog_output_status": "OVER_RANGE"}.get(kind, "CHATTER_FILTER")
    bit6 = {"counter": "DISCONTINUITY", "frozen_counter": "DISCONTINUITY", "analog_input": "REFERENCE_ERR",
            "analog_output_status": "REFERENCE_ERR"}.get(kind)
    names = [(ONLINE, "ONLINE"), (RESTART, "RESTART"), (COMM_LOST, "COMM_LOST"), (REMOTE_FORCED, "REMOTE_FORCED"),
             (LOCAL_FORCED, "LOCAL_FORCED"), (0x20, bit5)]
    if bit6 and kind != "double_bit_binary_input":
        names.append((0x40, bit6))
    return [n for bit, n in names if flags & bit]


def quality(kind: str, flags: int | None) -> int:
    """DNP3 flags -> platform quality (spec 15.6). Objects without flags are good."""
    if flags is None:
        return 192
    if flags & COMM_LOST:
        return 24
    if not flags & ONLINE:
        return 0
    if flags & RESTART:
        return 68
    analog = kind in ("analog_input", "analog_output_status")
    counter = kind in ("counter", "frozen_counter")
    if flags & 0x20 and (analog or counter):
        return 84                                   # OVER_RANGE / ROLLOVER
    if flags & 0x40 and analog:
        return 80                                   # REFERENCE_ERR
    if flags & 0x20 and not (analog or counter):
        return 64                                   # CHATTER_FILTER
    if flags & (LOCAL_FORCED | REMOTE_FORCED):
        return 216
    return 192


def time48(raw: bytes) -> datetime:
    ms = int.from_bytes(raw[:6], "little")
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def pack_time48(dt: datetime) -> bytes:
    return int(dt.timestamp() * 1000).to_bytes(6, "little")


@dataclass
class Point:
    """One value from a response: kind + index, value, flags (None if the variation has none), time if any."""
    group: int
    variation: int
    index: int
    kind: str
    value: object
    flags: int | None = None
    time: datetime | None = None

    @property
    def event(self) -> bool:
        return self.group in EVENT_GROUPS

    @property
    def gv(self) -> str:
        return f"g{self.group}v{self.variation}"


@dataclass
class Fragment:
    control: int
    function: int
    iin: int = 0
    objects: list = field(default_factory=list)       # Points, plus ("header", g, v, qual, range) for requests

    @property
    def seq(self) -> int:
        return self.control & 0x0F

    @property
    def fir(self) -> bool:
        return bool(self.control & AC_FIR)

    @property
    def fin(self) -> bool:
        return bool(self.control & AC_FIN)

    @property
    def con(self) -> bool:
        return bool(self.control & AC_CON)

    @property
    def uns(self) -> bool:
        return bool(self.control & AC_UNS)

    def iin_names(self) -> list[str]:
        return [n for b, n in IIN_NAMES.items() if self.iin & (1 << b)]


# ------------------------------------------------------------------ object formats
# (group, variation) -> (size in bytes, decoder(raw) -> (value, flags, time)); size None = packed bits
def _f32(fmt: str, v):
    """32-bit floats carry about 7 significant digits: 183.7, not 183.6999969482422."""
    return float(f"{v:.7g}") if fmt == "f" else v


def _flags_value(fmt):
    s = struct.Struct("<B" + fmt)
    return s.size, lambda raw: (_f32(fmt, s.unpack(raw)[1]), s.unpack(raw)[0], None)


def _flags_value_time(fmt):
    s = struct.Struct("<B" + fmt)
    return s.size + 6, lambda raw: (_f32(fmt, s.unpack(raw[:s.size])[1]), s.unpack(raw[:s.size])[0], time48(raw[s.size:]))


def _value(fmt):
    s = struct.Struct("<" + fmt)
    return s.size, lambda raw: (_f32(fmt, s.unpack(raw)[0]), None, None)


def _bin_flags(raw):
    return bool(raw[0] & STATE), raw[0], None


def _bin_flags_time(raw):
    return bool(raw[0] & STATE), raw[0], time48(raw[1:7])


def _dbl_flags(raw):
    return raw[0] >> 6, raw[0], None


def _dbl_flags_time(raw):
    return raw[0] >> 6, raw[0], time48(raw[1:7])


def _relative(dec):
    """Relative-time event: value/flags plus a 16-bit ms offset; absolute time is added from the CTO later."""
    return lambda raw: (*dec(raw[:1])[:2], ("rel", struct.unpack_from("<H", raw, 1)[0]))


FORMATS: dict[tuple[int, int], tuple] = {
    (1, 2): (1, _bin_flags), (2, 1): (1, _bin_flags), (2, 2): (7, _bin_flags_time), (2, 3): (3, _relative(_bin_flags)),
    (3, 2): (1, _dbl_flags), (4, 1): (1, _dbl_flags), (4, 2): (7, _dbl_flags_time), (4, 3): (3, _relative(_dbl_flags)),
    (10, 2): (1, _bin_flags), (11, 1): (1, _bin_flags), (11, 2): (7, _bin_flags_time),
    (20, 1): _flags_value("I"), (20, 2): _flags_value("H"), (20, 5): _value("I"), (20, 6): _value("H"),
    (21, 1): _flags_value("I"), (21, 2): _flags_value("H"), (21, 5): _flags_value_time("I"),
    (21, 6): _flags_value_time("H"), (21, 9): _value("I"), (21, 10): _value("H"),
    (22, 1): _flags_value("I"), (22, 2): _flags_value("H"), (22, 5): _flags_value_time("I"), (22, 6): _flags_value_time("H"),
    (23, 1): _flags_value("I"), (23, 2): _flags_value("H"), (23, 5): _flags_value_time("I"), (23, 6): _flags_value_time("H"),
    (30, 1): _flags_value("i"), (30, 2): _flags_value("h"), (30, 3): _value("i"), (30, 4): _value("h"),
    (30, 5): _flags_value("f"), (30, 6): _flags_value("d"),
    (32, 1): _flags_value("i"), (32, 2): _flags_value("h"), (32, 3): _flags_value_time("i"), (32, 4): _flags_value_time("h"),
    (32, 5): _flags_value("f"), (32, 6): _flags_value("d"), (32, 7): _flags_value_time("f"), (32, 8): _flags_value_time("d"),
    (40, 1): _flags_value("i"), (40, 2): _flags_value("h"), (40, 3): _flags_value("f"), (40, 4): _flags_value("d"),
    (42, 1): _flags_value("i"), (42, 2): _flags_value("h"), (42, 3): _flags_value_time("i"), (42, 4): _flags_value_time("h"),
    (42, 5): _flags_value("f"), (42, 6): _flags_value("d"), (42, 7): _flags_value_time("f"), (42, 8): _flags_value_time("d"),
}
PACKED = {(1, 1): 1, (10, 1): 1, (3, 1): 2}          # packed binary (1 bit) / double-bit (2 bits)
# other objects an outstation may include: only their size matters (skipped)
SKIP = {(50, 1): 6, (50, 3): 6, (51, 1): 6, (51, 2): 6, (52, 1): 2, (52, 2): 2, (34, 1): 2, (34, 2): 4, (34, 3): 4}
INTEGER_VARIATIONS = {(30, 1), (30, 2), (30, 3), (30, 4), (32, 1), (32, 2), (32, 3), (32, 4), (40, 1), (40, 2),
                      (42, 1), (42, 2), (42, 3), (42, 4)}


# ------------------------------------------------------------------ parsing
def _range(data: bytes, pos: int, qual: int) -> tuple[list[int] | None, int, int, int]:
    """Returns (indexes or None if index-prefixed, count, index prefix size, new pos)."""
    prefix, code = qual >> 4, qual & 0x0F
    psize = {0: 0, 1: 1, 2: 2, 3: 4}.get(prefix)
    if psize is None:
        raise AppError(f"qualifier 0x{qual:02X}: size prefixes are not supported")
    if code in (0, 1, 2):
        n = (1, 2, 4)[code]
        start = int.from_bytes(data[pos:pos + n], "little")
        stop = int.from_bytes(data[pos + n:pos + 2 * n], "little")
        if stop < start:
            raise AppError("range stop before start")
        return list(range(start, stop + 1)), stop - start + 1, psize, pos + 2 * n
    if code in (7, 8, 9):
        n = (1, 2, 4)[code - 7]
        count = int.from_bytes(data[pos:pos + n], "little")
        return None if psize else list(range(count)), count, psize, pos + n
    if code == 6:
        return [], 0, 0, pos
    raise AppError(f"qualifier 0x{qual:02X} is not supported")


def parse(data: bytes, request: bool = False) -> Fragment:
    """Parses one application fragment. Requests carry object headers only (no data, except g80v1 writes)."""
    if len(data) < (2 if request else 4):
        raise AppError("fragment too short")
    frag = Fragment(data[0], data[1])
    pos = 2
    if not request:
        frag.iin = data[2] | data[3] << 8
        pos = 4
    cto: datetime | None = None
    while pos < len(data):
        if pos + 3 > len(data):
            raise AppError("truncated object header")
        g, v, qual = data[pos], data[pos + 1], data[pos + 2]
        pos += 3
        idx, count, psize, pos = _range(data, pos, qual)
        if request:
            frag.objects.append(("header", g, v, qual, idx if qual & 0x0F != 6 else None))
            if (g, v) == (80, 1):                      # data of a write: packed bits
                pos += (count + 7) // 8
            continue
        if (g, v) in PACKED:
            bits = PACKED[(g, v)]
            if psize:
                raise AppError("packed objects with index prefix")
            raw = data[pos:pos + (count * bits + 7) // 8]
            pos += (count * bits + 7) // 8
            kind = KIND_OF_GROUP[g]
            for k, i in enumerate(idx):
                b = (raw[k * bits // 8] >> (k * bits % 8)) & ((1 << bits) - 1)
                frag.objects.append(Point(g, v, i, kind, bool(b) if bits == 1 else b))
            continue
        if (g, v) == (80, 1):
            pos += (count + 7) // 8
            continue
        if (g, v) in SKIP:
            size = SKIP[(g, v)]
            if g == 51 and count:
                cto = time48(data[pos + psize:pos + psize + 6])
            pos += count * (psize + size)
            continue
        if g in (110, 111):                            # octet strings: variation = length
            pos += count * (psize + v)
            continue
        fmt = FORMATS.get((g, v))
        if fmt is None:
            raise AppError(f"object g{g}v{v} is not supported")
        size, dec = fmt
        kind = KIND_OF_GROUP[g]
        for k in range(count):
            if psize:
                i = int.from_bytes(data[pos:pos + psize], "little")
                pos += psize
            else:
                i = idx[k]
            raw = data[pos:pos + size]
            if len(raw) < size:
                raise AppError(f"truncated g{g}v{v} object")
            pos += size
            value, flags, t = dec(raw)
            if isinstance(t, tuple):                   # relative time + CTO
                t = datetime.fromtimestamp(cto.timestamp() + t[1] / 1000, timezone.utc) if cto else None
            frag.objects.append(Point(g, v, i, kind, value, flags, t))
    return frag


# ------------------------------------------------------------------ master requests (the only ones that exist)
def _ac(seq: int, uns: bool = False) -> int:
    return AC_FIR | AC_FIN | (AC_UNS if uns else 0) | (seq & 0x0F)


def confirm(seq: int, uns: bool = False) -> bytes:
    return bytes([_ac(seq, uns), FC_CONFIRM])


def read_classes(seq: int, classes: tuple[int, ...]) -> bytes:
    """READ of class objects g60v1 (class 0), g60v2-4 (classes 1-3), qualifier 0x06 (all)."""
    out = bytearray([_ac(seq), FC_READ])
    for c in classes:
        out += bytes([60, c + 1, 0x06])
    return bytes(out)


def read_range(seq: int, items: list[tuple[int, int, int]]) -> bytes:
    """READ of static points: (group, start, stop) with variation 0 and qualifier 0x01 (2-byte start/stop)."""
    out = bytearray([_ac(seq), FC_READ])
    for g, start, stop in items:
        out += bytes([g, 0, 0x01]) + struct.pack("<HH", start, stop)
    return bytes(out)


def unsolicited(seq: int, enable: bool, classes: tuple[int, ...] = (1, 2, 3)) -> bytes:
    out = bytearray([_ac(seq), FC_ENABLE_UNSOL if enable else FC_DISABLE_UNSOL])
    for c in classes:
        out += bytes([60, c + 1, 0x06])
    return bytes(out)


def clear_restart(seq: int) -> bytes:
    """WRITE g80v1 index 7 = 0 (clears IIN1.7 DEVICE_RESTART); the only write the spec allows."""
    return bytes([_ac(seq), FC_WRITE, 80, 1, 0x00, 7, 7, 0x00])


# ------------------------------------------------------------------ outstation encoders (test outstation)
def response(control: int, function: int, iin: int, body: bytes = b"") -> bytes:
    return bytes([control, function, iin & 0xFF, iin >> 8]) + body


def encode_points(g: int, v: int, points: list[tuple[int, object, int, datetime | None]], indexed: bool) -> bytes:
    """Encodes (index, value, flags, time) items: range 0x01 for consecutive static data, else 0x28 (2-byte count and
    index prefix)."""
    if (g, v) in PACKED:
        bits = PACKED[(g, v)]
        start, stop = points[0][0], points[-1][0]
        acc, nbits = bytearray((len(points) * bits + 7) // 8), 0
        for _, val, _, _ in points:
            acc[nbits // 8] |= (int(val) & ((1 << bits) - 1)) << (nbits % 8)
            nbits += bits
        return bytes([g, v, 0x01]) + struct.pack("<HH", start, stop) + bytes(acc)
    out = bytearray([g, v, 0x28 if indexed else 0x01])
    out += struct.pack("<H", len(points)) if indexed else struct.pack("<HH", points[0][0], points[-1][0])
    for i, val, flags, t in points:
        if indexed:
            out += struct.pack("<H", i)
        out += _encode_value(g, v, val, flags, t)
    return bytes(out)


def _encode_value(g: int, v: int, val, flags: int, t) -> bytes:
    if g in (1, 2, 10, 11):
        b = (flags & 0x7F) | (STATE if val else 0)
        return bytes([b]) + (pack_time48(t) if v == 2 and g in (2, 11) else b"")
    if g in (3, 4):
        b = (flags & 0x3F) | ((int(val) & 3) << 6)
        return bytes([b]) + (pack_time48(t) if v == 2 and g == 4 else b"")
    size, _ = FORMATS[(g, v)]
    fmt = {1: "I", 2: "H", 5: "I", 6: "H", 9: "I", 10: "H"}[v] if g in (20, 21, 22, 23) else \
        {1: "i", 2: "h", 3: "i", 4: "h", 5: "f", 6: "d", 7: "f", 8: "d"}[v] if g in (30, 32, 42) else \
        {1: "i", 2: "h", 3: "f", 4: "d"}[v]
    if fmt in "iIhH":
        val = int(round(val))
    has_flags = not ((g in (20, 21) and v in (5, 6, 9, 10) and not (g == 21 and v in (5, 6))) or (g == 30 and v in (3, 4)))
    body = (bytes([flags]) if has_flags else b"") + struct.pack("<" + fmt, val)
    timed = (g, v) in {(21, 5), (21, 6), (22, 5), (22, 6), (23, 5), (23, 6), (32, 3), (32, 4), (32, 7), (32, 8),
                       (42, 3), (42, 4), (42, 7), (42, 8)}
    if timed:
        body += pack_time48(t)
    assert len(body) == size, (g, v, len(body), size)
    return body
