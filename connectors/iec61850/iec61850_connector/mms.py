"""MMS (ISO 9506-2) PDUs used by a read-only IEC 61850 client, BER-encoded.

Requests that exist: Initiate, Conclude, GetNameList, GetVariableAccessAttributes, GetNamedVariableListAttributes,
Read, and Write — the Write builder is only reached through `write_rcb` in client.py, which the guard limits to the
report-control-block attributes of spec 15.4. There are no builders for controls, setting groups, datasets, files or
journals. The server-side encoders at the end are used by the test server.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import ber as B

# confirmed service tags (ISO 9506-2 ConfirmedServiceRequest)
SVC_GET_NAME_LIST, SVC_READ, SVC_WRITE, SVC_GET_VAR_ATTR, SVC_GET_NVL_ATTR = 1, 4, 5, 6, 12
SVC_NAMES = {0: "Status", 1: "GetNameList", 2: "Identify", 4: "Read", 5: "Write", 6: "GetVariableAccessAttributes",
             11: "DefineNamedVariableList", 12: "GetNamedVariableListAttributes", 13: "DeleteNamedVariableList",
             72: "FileOpen", 73: "FileRead", 74: "FileClose", 77: "FileDirectory"}
CLASS_NAMED_VARIABLE, CLASS_NAMED_VARIABLE_LIST, CLASS_DOMAIN = 0, 2, 9
DATA_ACCESS_ERRORS = {0: "object-invalidated", 1: "hardware-fault", 2: "temporarily-unavailable",
                      3: "object-access-denied", 4: "object-undefined", 5: "invalid-address", 6: "type-unsupported",
                      7: "type-inconsistent", 8: "object-attribute-inconsistent", 9: "object-access-unsupported",
                      10: "object-non-existent", 11: "object-value-invalid"}


class MmsError(Exception):
    def __init__(self, message: str, code: str = "PROTOCOL_ERROR"):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ values
@dataclass
class BitString:
    bits: list

    def __getitem__(self, i: int) -> bool:
        return i < len(self.bits) and bool(self.bits[i])


@dataclass
class UtcTime:
    """IEC 61850-8-1 TimeStamp: seconds, fraction (24-bit) and TimeQuality."""
    when: datetime
    leap_known: bool = False
    clock_failure: bool = False
    not_synchronized: bool = False
    accuracy: int = 0


@dataclass
class DataError:
    code: int

    @property
    def name(self) -> str:
        return DATA_ACCESS_ERRORS.get(self.code, f"error-{self.code}")


@dataclass
class TypeSpec:
    """Type description from GetVariableAccessAttributes: kind + components (structures) or element (arrays)."""
    kind: str                                         # structure, array, boolean, bit-string, integer, unsigned ...
    size: int = 0
    components: list = field(default_factory=list)    # [(name, TypeSpec)]
    element: "TypeSpec | None" = None
    count: int = 0

    def component(self, name: str) -> "TypeSpec | None":
        return next((t for n, t in self.components if n == name), None)


def decode_data(t: B.Tlv):
    """MMS Data -> Python: list (array/structure), bool, int, float, str, bytes, BitString, UtcTime."""
    n = t.number
    if n in (1, 2):
        return [decode_data(c) for c in t.children()]
    if n == 3:
        return t.value != b"\x00"
    if n == 4:
        return BitString(B.bits_of(t.value))
    if n == 5:
        return t.int()
    if n == 6:
        return t.uint()
    if n == 7:
        raw = t.value[1:]
        if len(raw) == 4:
            return float(f"{struct.unpack('>f', raw)[0]:.7g}")
        if len(raw) == 8:
            return struct.unpack(">d", raw)[0]
        raise MmsError("floating-point of unexpected size")
    if n == 9:
        return t.value
    if n in (10, 16):
        return t.value.decode("utf-8", "replace")
    if n == 17:
        v = t.value
        secs = int.from_bytes(v[0:4], "big")
        frac = int.from_bytes(v[4:7], "big") / (1 << 24)
        q = v[7] if len(v) > 7 else 0
        return UtcTime(datetime.fromtimestamp(secs + frac, timezone.utc), bool(q & 0x80), bool(q & 0x40), bool(q & 0x20), q & 0x1F)
    if n == 12:
        ms = int.from_bytes(t.value[:4], "big")
        days = int.from_bytes(t.value[4:6], "big") if len(t.value) >= 6 else 0
        return datetime.fromtimestamp(441763200 + days * 86400 + ms / 1000, timezone.utc)   # days since 1984-01-01
    if n == 11:
        return t.text()
    return t.value


def encode_data(v, spec: TypeSpec | None = None) -> bytes:
    """Python -> MMS Data (used for RCB writes and by the test server)."""
    if isinstance(v, bool):
        return B.ctx_prim(3, b"\xff" if v else b"\x00")
    if isinstance(v, BitString):
        return B.ctx_prim(4, B.bitstring_content(v.bits))
    if isinstance(v, UtcTime):
        ts = v.when.timestamp()
        secs = int(ts)
        frac = int((ts - secs) * (1 << 24))
        q = (0x80 if v.leap_known else 0) | (0x40 if v.clock_failure else 0) | (0x20 if v.not_synchronized else 0) | (v.accuracy & 0x1F)
        return B.ctx_prim(17, secs.to_bytes(4, "big") + frac.to_bytes(3, "big") + bytes([q]))
    if isinstance(v, datetime):                                  # binary-time (TimeOfEntry): ms of day, days since 1984
        delta = v - datetime(1984, 1, 1, tzinfo=timezone.utc)
        ms = delta.seconds * 1000 + delta.microseconds // 1000
        return B.ctx_prim(12, ms.to_bytes(4, "big") + delta.days.to_bytes(2, "big"))
    if isinstance(v, float):
        if spec is not None and spec.kind == "floating-point" and spec.size == 64:
            return B.ctx_prim(7, b"\x0b" + struct.pack(">d", v))
        return B.ctx_prim(7, b"\x08" + struct.pack(">f", v))
    if isinstance(v, int):
        if spec is not None and spec.kind == "unsigned":
            return B.ctx_prim(6, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big"))
        return B.ctx_prim(5, B.integer_bytes(v))
    if isinstance(v, str):
        return B.ctx_prim(16 if spec is not None and spec.kind == "mms-string" else 10, v.encode())
    if isinstance(v, bytes):
        return B.ctx_prim(9, v)
    if isinstance(v, list):
        if spec is not None and spec.kind == "array":
            return B.ctx(1, b"".join(encode_data(x, spec.element) for x in v))
        comps = spec.components if spec is not None else []
        return B.ctx(2, b"".join(encode_data(x, comps[i][1] if i < len(comps) else None) for i, x in enumerate(v)))
    raise MmsError(f"cannot encode {type(v).__name__}")


def decode_type(t: B.Tlv) -> TypeSpec:
    n = t.number
    if n == 2:                                                  # structure { packed [0]?, components [1] }
        comps = []
        holder = t.child(1)
        for c in holder.children() if holder else []:
            name = c.child(0)
            ctype = c.child(1)
            comps.append((name.text() if name else "", decode_type(ctype.children()[0]) if ctype else TypeSpec("?")))
        return TypeSpec("structure", components=comps)
    if n == 1:                                                  # array { packed?, numberOfElements [1], elementType [2] }
        num = t.child(1)
        el = t.child(2)
        return TypeSpec("array", count=num.uint() if num else 0, element=decode_type(el.children()[0]) if el else None)
    kinds = {3: "boolean", 4: "bit-string", 5: "integer", 6: "unsigned", 7: "floating-point", 9: "octet-string",
             10: "visible-string", 11: "generalized-time", 12: "binary-time", 16: "mms-string", 17: "utc-time"}
    kind = kinds.get(n, f"type-{n}")
    size = 0
    if n in (4, 5, 6, 9, 10, 16):
        size = abs(t.int()) if t.value else 0
    elif n == 7:
        parts = t.children()
        size = 32 if parts and parts[0].uint() == 32 else 64 if parts and parts[0].uint() == 64 else 32
    return TypeSpec(kind, size)


def encode_type(spec: TypeSpec) -> bytes:
    """TypeSpecification (test server)."""
    if spec.kind == "structure":
        comps = b"".join(B.seq(B.ctx_prim(0, n.encode()), B.ctx(1, encode_type(s))) for n, s in spec.components)
        return B.ctx(2, B.ctx(1, comps))
    if spec.kind == "array":
        return B.ctx(1, B.ctx_prim(1, B.integer_bytes(spec.count)) + B.ctx(2, encode_type(spec.element)))
    tag = {"boolean": 3, "bit-string": 4, "integer": 5, "unsigned": 6, "floating-point": 7, "octet-string": 9,
           "visible-string": 10, "binary-time": 12, "mms-string": 16, "utc-time": 17}[spec.kind]
    if spec.kind == "floating-point":
        return B.ctx(7, B.integer(spec.size or 32) + B.integer(8 if (spec.size or 32) == 32 else 11))
    if spec.kind in ("boolean", "utc-time"):
        return B.ctx_prim(tag, b"")
    if spec.kind == "binary-time":
        return B.ctx_prim(tag, b"\xff")
    return B.ctx_prim(tag, B.integer_bytes(spec.size))


# ------------------------------------------------------------------ requests
def object_name(domain: str | None, item: str) -> bytes:
    if domain is None:
        return B.ctx_prim(0, item.encode())                         # vmd-specific
    return B.ctx(1, B.tlv(B.UNIVERSAL, False, 26, domain.encode()) + B.tlv(B.UNIVERSAL, False, 26, item.encode()))


def initiate_request() -> bytes:
    detail = (B.ctx_prim(0, b"\x01")                                               # version 1
              + B.ctx_prim(1, bytes.fromhex("05f100"))                              # parameter CBB: str1 str2 vnam valt vlis
              + B.ctx_prim(2, bytes.fromhex("03ee1c00000408000079ef18")))          # services supported (calling)
    return B.ctx(8, B.ctx_prim(0, B.integer_bytes(65000)) + B.ctx_prim(1, b"\x05") + B.ctx_prim(2, b"\x05")
                 + B.ctx_prim(3, b"\x0a") + B.ctx(4, detail))


def confirmed(invoke_id: int, service: bytes) -> bytes:
    return B.ctx(0, B.integer(invoke_id) + service)


def get_name_list(object_class: int, domain: str | None, continue_after: str | None = None) -> bytes:
    scope = B.ctx_prim(0, b"") if domain is None else B.ctx_prim(1, domain.encode())
    body = B.ctx(0, B.ctx_prim(0, B.integer_bytes(object_class))) + B.ctx(1, scope)
    if continue_after:
        body += B.ctx_prim(2, continue_after.encode())
    return B.ctx(SVC_GET_NAME_LIST, body)


def _var_list(names: list[tuple[str, str]]) -> bytes:
    return B.ctx(0, b"".join(B.seq(B.ctx(0, object_name(d, i))) for d, i in names))


def read(names: list[tuple[str, str]]) -> bytes:
    return B.ctx(SVC_READ, B.ctx(1, _var_list(names)))


def write(names: list[tuple[str, str]], values: list[bytes]) -> bytes:
    """Write-Request: the VariableAccessSpecification CHOICE comes untagged (unlike Read, which wraps it in [1])."""
    return B.ctx(SVC_WRITE, _var_list(names) + B.ctx(0, b"".join(values)))


def get_variable_access_attributes(domain: str, item: str) -> bytes:
    return B.ctx(SVC_GET_VAR_ATTR, B.ctx(0, object_name(domain, item)))


def get_named_variable_list_attributes(domain: str, item: str) -> bytes:
    return B.ctx(SVC_GET_NVL_ATTR, object_name(domain, item))


def conclude_request() -> bytes:
    return B.ctx_prim(11, b"")


# ------------------------------------------------------------------ responses (client side)
@dataclass
class Pdu:
    kind: str                       # confirmed-response, confirmed-error, confirmed-request, unconfirmed, reject ...
    invoke_id: int | None = None
    service: int | None = None
    body: B.Tlv | None = None
    error: str | None = None


def parse_pdu(data: bytes) -> Pdu:
    t, _ = B.parse(data)
    kinds = {0: "confirmed-request", 1: "confirmed-response", 2: "confirmed-error", 3: "unconfirmed", 4: "reject",
             8: "initiate-request", 9: "initiate-response", 10: "initiate-error", 11: "conclude-request",
             12: "conclude-response", 13: "conclude-error"}
    kind = kinds.get(t.number, f"pdu-{t.number}")
    if kind in ("confirmed-request", "confirmed-response"):
        kids = t.children()
        svc = kids[1] if len(kids) > 1 else None
        return Pdu(kind, kids[0].int(), svc.number if svc else None, svc)
    if kind == "confirmed-error":
        kids = t.children()
        inv = next((c.int() for c in kids if c.number == 0 and c.cls == B.CONTEXT), None)
        err = next((c for c in kids if c.number == 2), None)
        return Pdu(kind, inv, None, err, _service_error(err))
    if kind == "reject":
        kids = t.children()
        inv = kids[0].int() if kids and kids[0].number == 0 else None
        return Pdu(kind, inv, None, t, "the server rejected the request")
    if kind == "unconfirmed":
        svc = t.children()[0]
        return Pdu(kind, None, svc.number, svc)
    return Pdu(kind, None, None, t)


def _service_error(err: B.Tlv | None) -> str:
    if err is None:
        return "service error"
    cls = err.child(0)
    if cls is None:
        return "service error"
    inner = cls.children()[0] if cls.children() else cls
    names = {0: "vmd-state", 1: "application-reference", 2: "definition", 3: "resource", 4: "service",
             5: "service-preempt", 6: "time-resolution", 7: "access", 8: "initiate", 9: "conclude", 10: "cancel",
             11: "file", 12: "others"}
    return f"{names.get(inner.number, inner.number)} error {inner.int() if inner.value else ''}".strip()


def parse_name_list(body: B.Tlv) -> tuple[list[str], bool]:
    names_t = body.child(0)
    more_t = body.child(1)
    names = [c.text() for c in names_t.children()] if names_t else []
    more = bool(more_t and more_t.value != b"\x00") if more_t else False
    return names, more


def parse_read(body: B.Tlv) -> list:
    """Read response -> list of decoded values or DataError."""
    results = body.child(1)
    out = []
    for r in results.children() if results else []:
        out.append(DataError(r.int()) if r.number == 0 else decode_data(r))
    return out


def parse_write(body: B.Tlv) -> list:
    """Write response -> list of None (success) or DataError."""
    return [DataError(r.int()) if r.number == 0 else None for r in body.children()]


def parse_var_attr(body: B.Tlv) -> TypeSpec:
    ts = body.child(2)
    if ts is None:
        raise MmsError("variable access attributes without a type description")
    return decode_type(ts.children()[0])


def parse_nvl_attr(body: B.Tlv) -> list[tuple[str, str]]:
    """Members of a named variable list (an IEC 61850 dataset): [(domain, item)]."""
    lst = body.child(1)
    out = []
    for m in lst.children() if lst else []:
        spec = m.child(0)
        if spec is None:
            continue
        name = spec.children()[0]
        if name.number == 1:
            d, i = name.children()
            out.append((d.text(), i.text()))
        elif name.number == 0:
            out.append(("", name.text()))
    return out


def parse_information_report(body: B.Tlv) -> tuple[str | None, list]:
    """Unconfirmed InformationReport -> (variable list name, list of AccessResults decoded)."""
    spec = body.children()[0]
    name = None
    if spec.number == 1:                                          # variableListName
        on = spec.children()[0]
        name = on.text() if on.number == 0 else "/".join(c.text() for c in on.children())
    results = body.child(0)
    values = []
    for r in results.children() if results else []:
        values.append(DataError(r.int()) if r.number == 0 else decode_data(r))
    return name, values


# ------------------------------------------------------------------ server-side encoders (test server)
def confirmed_response(invoke_id: int, service: bytes) -> bytes:
    return B.ctx(1, B.integer(invoke_id) + service)


def confirmed_error(invoke_id: int, error_class: int, code: int) -> bytes:
    return B.ctx(2, B.ctx_prim(0, B.integer_bytes(invoke_id)) + B.ctx(2, B.ctx(0, B.ctx_prim(error_class, B.integer_bytes(code)))))


def reject(invoke_id: int | None) -> bytes:
    return B.ctx(4, (B.ctx_prim(0, B.integer_bytes(invoke_id)) if invoke_id is not None else b"") + B.ctx_prim(1, b"\x01"))


def initiate_response() -> bytes:
    detail = (B.ctx_prim(0, b"\x01") + B.ctx_prim(1, bytes.fromhex("05f100"))
              + B.ctx_prim(2, bytes.fromhex("03ee1c00000408000079ef18")))
    return B.ctx(9, B.ctx_prim(0, B.integer_bytes(65000)) + B.ctx_prim(1, b"\x05") + B.ctx_prim(2, b"\x05")
                 + B.ctx_prim(3, b"\x0a") + B.ctx(4, detail))


def name_list_response(names: list[str], more: bool) -> bytes:
    return B.ctx(SVC_GET_NAME_LIST, B.ctx(0, b"".join(B.tlv(B.UNIVERSAL, False, 26, n.encode()) for n in names))
                 + B.ctx_prim(1, b"\xff" if more else b"\x00"))


def read_response(results: list[bytes]) -> bytes:
    """results: encoded Data, or failure [0] encodings."""
    return B.ctx(SVC_READ, B.ctx(1, b"".join(results)))


def access_failure(code: int) -> bytes:
    return B.ctx_prim(0, B.integer_bytes(code))


def write_response(results: list[int | None]) -> bytes:
    return B.ctx(SVC_WRITE, b"".join(B.ctx_prim(1, b"") if r is None else B.ctx_prim(0, B.integer_bytes(r)) for r in results))


def var_attr_response(spec: TypeSpec) -> bytes:
    return B.ctx(SVC_GET_VAR_ATTR, B.ctx_prim(0, b"\x00") + B.ctx(2, encode_type(spec)))


def nvl_attr_response(members: list[tuple[str, str]]) -> bytes:
    lst = b"".join(B.seq(B.ctx(0, object_name(d, i))) for d, i in members)
    return B.ctx(SVC_GET_NVL_ATTR, B.ctx_prim(0, b"\x00") + B.ctx(1, lst))


def information_report(rpt_name: str, values: list[bytes]) -> bytes:
    return B.ctx(3, B.ctx(0, B.ctx(1, object_name(None, rpt_name)) + B.ctx(0, b"".join(values))))


def parse_request_names(body: B.Tlv) -> list[tuple[str | None, str]]:
    """Variable names of a Read or Write request (test server). Read wraps the VariableAccessSpecification in [1];
    Write starts with it untagged (listOfVariable [0])."""
    lst = body.child(1).children()[0] if body.number == SVC_READ else body.children()[0]
    out = []
    for item in lst.children():
        on = item.child(0).children()[0]
        if on.number == 1:
            d, i = on.children()
            out.append((d.text(), i.text()))
        else:
            out.append((None, on.text()))
    return out
