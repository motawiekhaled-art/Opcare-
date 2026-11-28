"""IEC 60870-5-101/104 application layer as used by IEC 104 (IEC 60870-5-104 section 9): ASDU header with two-octet
cause of transmission, two-octet common address and three-octet information object addresses; the monitor-direction
information elements of spec 15.5, CP56Time2a time tags and the quality descriptors.

Commands: this module builds only the three requests the read-only guard allows (spec 12 and 15.5): general
interrogation C_IC_NA_1, counter interrogation C_CI_NA_1 without freeze or reset, and the read command C_RD_NA_1.
There is no builder for process commands, set-points, clock synchronisation, reset, parameters or file transfer.
The monitor-direction encoders below are used by the test station (tools/simulators/iec104_station.py) only.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo

# ------------------------------------------------------------------ type identifications
TYPES = {1: "M_SP_NA_1", 3: "M_DP_NA_1", 5: "M_ST_NA_1", 7: "M_BO_NA_1", 9: "M_ME_NA_1", 11: "M_ME_NB_1",
         13: "M_ME_NC_1", 15: "M_IT_NA_1", 21: "M_ME_ND_1", 30: "M_SP_TB_1", 31: "M_DP_TB_1", 32: "M_ST_TB_1",
         33: "M_BO_TB_1", 34: "M_ME_TD_1", 35: "M_ME_TE_1", 36: "M_ME_TF_1", 37: "M_IT_TB_1", 70: "M_EI_NA_1",
         100: "C_IC_NA_1", 101: "C_CI_NA_1", 102: "C_RD_NA_1"}
TYPE_IDS = {v: k for k, v in TYPES.items()}
C_IC_NA_1, C_CI_NA_1, C_RD_NA_1, M_EI_NA_1 = 100, 101, 102, 70

# monitor types: type id -> (family, element octets without time tag, CP56Time2a tag)
LAYOUT = {1: ("SP", 1, False), 3: ("DP", 1, False), 5: ("ST", 2, False), 7: ("BO", 5, False), 9: ("NA", 3, False),
          11: ("NB", 3, False), 13: ("NC", 5, False), 15: ("IT", 5, False), 21: ("NA", 2, False),
          30: ("SP", 1, True), 31: ("DP", 1, True), 32: ("ST", 2, True), 33: ("BO", 5, True), 34: ("NA", 3, True),
          35: ("NB", 3, True), 36: ("NC", 5, True), 37: ("IT", 5, True)}
FAMILY_TYPE = {"SP": 1, "DP": 3, "ST": 5, "BO": 7, "NA": 9, "NB": 11, "NC": 13, "IT": 15}   # family -> type w/o time
LABEL = {"SP": "Single point", "DP": "Double point", "ST": "Step position", "BO": "Bitstring 32",
         "NA": "Normalised value", "NB": "Scaled value", "NC": "Short float", "IT": "Integrated total"}
FAMILIES = list(LABEL)
TIMED = {1: 30, 3: 31, 5: 32, 7: 33, 9: 34, 11: 35, 13: 36, 15: 37}                       # type -> with CP56Time2a


def family(type_name_or_id) -> str | None:
    tid = TYPE_IDS.get(type_name_or_id) if isinstance(type_name_or_id, str) else type_name_or_id
    return LAYOUT.get(tid, (None,))[0]


# ------------------------------------------------------------------ causes of transmission
COT = {1: "periodic", 2: "background", 3: "spontaneous", 4: "initialised", 5: "request", 6: "activation",
       7: "activation con", 8: "deactivation", 9: "deactivation con", 10: "activation term", 20: "interrogated",
       37: "counter interrogated", 44: "unknown type", 45: "unknown cause", 46: "unknown common address",
       47: "unknown object address"}
PERIODIC, BACKGROUND, SPONTANEOUS, INITIALISED, REQUEST, ACT, ACTCON, DEACT, DEACTCON, ACTTERM = 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
INROGEN, REQCOGEN = 20, 37
UNKNOWN_TYPE, UNKNOWN_COT, UNKNOWN_CA, UNKNOWN_IOA = 44, 45, 46, 47


def cot_name(cot: int) -> str:
    if 21 <= cot <= 36:
        return f"interrogated group {cot - 20}"
    if 38 <= cot <= 41:
        return f"counter group {cot - 37}"
    return COT.get(cot, f"cause {cot}")


# ------------------------------------------------------------------ quality descriptors
IV, NT, SB, BL, OV = 0x80, 0x40, 0x20, 0x10, 0x01
BCR_IV, BCR_CA, BCR_CY = 0x80, 0x40, 0x20
DPI_NAMES = {0: "intermediate", 1: "off", 2: "on", 3: "indeterminate"}


class AsduError(ValueError):
    pass


@dataclass
class Obj:
    ioa: int
    type_id: int
    value: object
    qual: int | None = None             # QDS / SIQ / DIQ quality bits, or the BCR sequence octet for counters
    time: datetime | None = None
    time_invalid: bool = False
    transient: bool = False

    @property
    def family(self) -> str:
        return LAYOUT[self.type_id][0]

    @property
    def type_name(self) -> str:
        return TYPES[self.type_id]


@dataclass
class Asdu:
    type_id: int
    cot: int
    ca: int
    oa: int = 0
    negative: bool = False
    test: bool = False
    sq: bool = False
    objects: list = field(default_factory=list)       # [Obj] for monitor types
    ioa: int | None = None                            # first IOA (commands, end of initialisation)
    qualifier: int | None = None                      # QOI / QCC / COI
    known: bool = True

    @property
    def type_name(self) -> str:
        return TYPES.get(self.type_id, f"type {self.type_id}")


def quality(fam: str, qual: int | None, transient: bool = False) -> tuple[int, list[str]]:
    """Quality bits -> (platform quality, flags), spec 15.5."""
    flags: list[str] = []
    if fam == "IT":
        q = qual or 0
        if q & BCR_IV:
            flags.append("INVALID")
        if q & BCR_CA:
            flags.append("ADJUSTED")
        if q & BCR_CY:
            flags.append("CARRY")
        flags.append(f"SEQ:{q & 0x1F}")
        return (0 if q & BCR_IV else 64 if q & BCR_CA else 192), flags
    if transient:
        flags.append("TRANSIENT")
    if qual is None:
        return 192, flags
    for bit, name in ((IV, "INVALID"), (NT, "NOT_TOPICAL"), (SB, "SUBSTITUTED"), (BL, "BLOCKED")):
        if qual & bit:
            flags.append(name)
    overflow = fam not in ("SP", "DP") and bool(qual & OV)        # bit 0 of SIQ/DIQ is the value itself
    if overflow:
        flags.append("OVERFLOW")
    if qual & IV:
        return 0, flags
    if qual & NT:
        return 68, flags
    if overflow:
        return 84, flags
    if qual & BL:
        return 64, flags
    if qual & SB:
        return 216, flags
    return 192, flags


# ------------------------------------------------------------------ CP56Time2a
def parse_cp56(b: bytes, tz: tzinfo) -> tuple[datetime | None, bool]:
    """-> (UTC datetime or None, invalid). The tag is local time of the station (spec 15.5 device_time_zone); the
    SU bit picks the summer-time reading in the hour that repeats when clocks go back."""
    ms = b[0] | b[1] << 8
    invalid = bool(b[2] & 0x80)
    try:
        local = datetime(2000 + (b[6] & 0x7F), b[5] & 0x0F, b[4] & 0x1F, b[3] & 0x1F, b[2] & 0x3F, ms // 1000,
                         (ms % 1000) * 1000, tzinfo=tz, fold=0 if b[3] & 0x80 else 1)
    except ValueError:
        return None, True
    return local.astimezone(timezone.utc), invalid


def cp56(dt: datetime, tz: tzinfo = timezone.utc, invalid: bool = False) -> bytes:
    local = dt.astimezone(tz)
    ms = local.second * 1000 + local.microsecond // 1000
    su = bool(local.dst())
    return bytes([ms & 0xFF, ms >> 8, local.minute | (0x80 if invalid else 0), local.hour | (0x80 if su else 0),
                  local.day | (local.isoweekday() << 5), local.month, local.year % 100])


# ------------------------------------------------------------------ decoding
def _round7(x: float) -> float:
    return float(f"{x:.7g}")


def _element(tid: int, b: bytes):
    """-> (value, quality bits, transient)"""
    fam = LAYOUT[tid][0]
    if fam == "SP":
        return bool(b[0] & 0x01), b[0] & 0xF0, False
    if fam == "DP":
        return b[0] & 0x03, b[0] & 0xF0, False
    if fam == "ST":
        v = b[0] & 0x7F
        return (v - 0x80 if v & 0x40 else v), b[1], bool(b[0] & 0x80)
    if fam == "BO":
        return struct.unpack_from("<I", b)[0], b[4], False
    if fam == "IT":
        return struct.unpack_from("<i", b)[0], b[4], False
    if fam == "NC":
        return _round7(struct.unpack_from("<f", b)[0]), b[4], False
    raw = struct.unpack_from("<h", b)[0]
    if fam == "NA":
        return raw / 32768.0, (b[2] if tid != 21 else None), False
    return raw, b[2], False                                           # NB scaled


def parse(data: bytes, tz: tzinfo = timezone.utc) -> Asdu:
    if len(data) < 6:
        raise AsduError("ASDU shorter than its header")
    tid, vsq, cot, oa = data[0], data[1], data[2], data[3]
    a = Asdu(tid, cot & 0x3F, data[4] | data[5] << 8, oa, bool(cot & 0x40), bool(cot & 0x80), bool(vsq & 0x80))
    n = vsq & 0x7F
    body = data[6:]
    if tid in LAYOUT:
        size, timed = LAYOUT[tid][1], LAYOUT[tid][2]
        step = size + (7 if timed else 0)
        need = (3 + n * step) if a.sq else n * (3 + step)
        if len(body) < need:
            raise AsduError(f"{TYPES[tid]} with {n} objects needs {need} octets, got {len(body)}")
        pos, ioa = 0, 0
        for i in range(n):
            if not a.sq or i == 0:
                ioa = body[pos] | body[pos + 1] << 8 | body[pos + 2] << 16
                pos += 3
            else:
                ioa += 1
            value, qual, transient = _element(tid, body[pos:pos + size])
            t, bad = parse_cp56(body[pos + size:pos + size + 7], tz) if timed else (None, False)
            pos += step
            a.objects.append(Obj(ioa, tid, value, qual, t, bad, transient))
        return a
    if tid in (C_IC_NA_1, C_CI_NA_1, C_RD_NA_1, M_EI_NA_1):
        if len(body) < 3:
            raise AsduError(f"{TYPES[tid]} without an information object")
        a.ioa = body[0] | body[1] << 8 | body[2] << 16
        a.qualifier = body[3] if tid != C_RD_NA_1 and len(body) > 3 else None
        return a
    a.known = False                                  # other types: header only (counted, never interpreted)
    if len(body) >= 3:
        a.ioa = body[0] | body[1] << 8 | body[2] << 16
    return a


# ------------------------------------------------------------------ encoding
def header(tid: int, n: int, cot: int, ca: int, oa: int = 0, sq: bool = False, negative: bool = False,
           test: bool = False) -> bytes:
    if not 0 < n < 128:
        raise AsduError("an ASDU carries 1..127 information objects")
    return bytes([tid, (0x80 if sq else 0) | n, cot | (0x40 if negative else 0) | (0x80 if test else 0), oa,
                  ca & 0xFF, ca >> 8])


def ioa_bytes(ioa: int) -> bytes:
    if not 0 <= ioa <= 0xFFFFFF:
        raise AsduError(f"information object address {ioa} out of range")
    return bytes([ioa & 0xFF, (ioa >> 8) & 0xFF, ioa >> 16])


# the three requests of the read-only guard (spec 15.5)
def interrogation(ca: int, qoi: int = 20, oa: int = 0, cot: int = ACT) -> bytes:
    if not 20 <= qoi <= 36:
        raise AsduError("QOI must be 20 (station) or 21..36 (group)")
    return header(C_IC_NA_1, 1, cot, ca, oa) + ioa_bytes(0) + bytes([qoi])


def counter_interrogation(ca: int, rqt: int = 5, oa: int = 0) -> bytes:
    """QCC with FRZ = 0: read the counters, no freeze and no reset (spec 15.5: freeze and reset change the device)."""
    if not 1 <= rqt <= 5:
        raise AsduError("RQT must be 1..4 (group) or 5 (general)")
    return header(C_CI_NA_1, 1, ACT, ca, oa) + ioa_bytes(0) + bytes([rqt])


def read_command(ca: int, ioa: int, oa: int = 0) -> bytes:
    return header(C_RD_NA_1, 1, REQUEST, ca, oa) + ioa_bytes(ioa)


def guard_name(asdu: bytes) -> str:
    """Request name the read-only guard checks for an I-frame (spec 12 and 15.5)."""
    tid = asdu[0]
    name = TYPES.get(tid, f"TYPE_{tid}")
    if tid == C_CI_NA_1 and (len(asdu) < 10 or asdu[9] >> 6):
        return "C_CI_NA_1:FREEZE"                    # FRZ 1..3 = freeze and/or reset counters: not allowed
    return name


# monitor direction (test station only)
def element(tid: int, value, qual: int = 0, t: datetime | None = None, tz: tzinfo = timezone.utc,
            time_invalid: bool = False, transient: bool = False) -> bytes:
    fam, _, timed = LAYOUT[tid]
    if fam == "SP":
        b = bytes([(1 if value else 0) | (qual & 0xF0)])
    elif fam == "DP":
        b = bytes([(int(value) & 3) | (qual & 0xF0)])
    elif fam == "ST":
        b = bytes([(int(value) & 0x7F) | (0x80 if transient else 0), qual])
    elif fam == "BO":
        b = struct.pack("<IB", int(value) & 0xFFFFFFFF, qual)
    elif fam == "IT":
        b = struct.pack("<iB", int(value), qual)
    elif fam == "NC":
        b = struct.pack("<fB", float(value), qual)
    elif fam == "NA":
        raw = max(-32768, min(32767, round(float(value) * 32768)))
        b = struct.pack("<h", raw) + (b"" if tid == 21 else bytes([qual]))
    else:
        b = struct.pack("<hB", int(value), qual)
    if timed:
        b += cp56(t or datetime.now(timezone.utc), tz, time_invalid)
    return b


def monitor(tid: int, cot: int, ca: int, objects: list[tuple], oa: int = 0, tz: tzinfo = timezone.utc,
            negative: bool = False, test: bool = False) -> bytes:
    """objects: [(ioa, value, qual, time, time_invalid, transient)] (trailing items optional)."""
    body = b""
    for o in objects:
        ioa, value, *rest = o
        qual, t, bad, transient = (list(rest) + [0, None, False, False][len(rest):])[:4]
        body += ioa_bytes(ioa) + element(tid, value, qual, t, tz, bad, transient)
    return header(tid, len(objects), cot, ca, oa, negative=negative, test=test) + body


def end_of_init(ca: int, coi: int = 0) -> bytes:
    return header(M_EI_NA_1, 1, INITIALISED, ca) + ioa_bytes(0) + bytes([coi])


def mirror(asdu: bytes, cot: int, negative: bool = False) -> bytes:
    """A command ASDU sent back with another cause (station side: con, term, negative answers)."""
    b = bytearray(asdu)
    b[2] = cot | (0x40 if negative else 0)
    return bytes(b)
