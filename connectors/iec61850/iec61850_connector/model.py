"""IEC 61850-8-1 mapping: object references to MMS names, Quality and TimeStamp to platform quality and time,
naming structured MMS values by their type description, and decoding InformationReports of report control blocks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timezone

from . import mms as M

READ_FCS = ("MX", "ST", "CF", "DC", "EX")                 # spec 15.4: values a point may address
REF = re.compile(r"^([A-Za-z0-9_]{1,64})/([A-Za-z0-9_]+)((?:\.[A-Za-z0-9_\[\]]+)*)$")
RCB_REF = re.compile(r"^([A-Za-z0-9_]{1,64})/([A-Za-z0-9_]+)\.(BR|RP)\.([A-Za-z0-9_]+)$")
DETAIL = ["overflow", "outOfRange", "badReference", "oscillatory", "failure", "oldData", "inconsistent", "inaccurate"]
OPT = {"seqNum": 1, "timeStamp": 2, "reasonCode": 3, "dataSet": 4, "dataRef": 5, "bufOvfl": 6, "entryID": 7,
       "confRev": 8, "segmentation": 9}
TRG = {"dchg": 1, "qchg": 2, "dupd": 3, "integrity": 4, "gi": 5}
REASONS = ["reserved", "dchg", "qchg", "dupd", "integrity", "gi", "application"]
DBPOS = {0: "intermediate", 1: "off", 2: "on", 3: "bad-state"}


class RefError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    """An FC-qualified reference: logical device, logical node, functional constraint and the path below the LN."""
    ld: str
    ln: str
    fc: str
    path: tuple = ()

    @property
    def item(self) -> str:
        return "$".join((self.ln, self.fc) + self.path)

    @property
    def object_ref(self) -> str:
        return f"{self.ld}/{self.ln}" + "".join(f".{p}" for p in self.path)

    def under(self, other: "Ref") -> bool:
        return (self.ld, self.ln, self.fc) == (other.ld, other.ln, other.fc) and self.path[:len(other.path)] == other.path


def parse_ref(object_ref: str, fc: str) -> Ref:
    m = REF.match(object_ref or "")
    if not m:
        raise RefError(f"'{object_ref}' is not an IEC 61850 reference LD/LN.DO[.DA]")
    path = tuple(p for p in m.group(3).split(".") if p)
    if not path:
        raise RefError(f"'{object_ref}' is not an IEC 61850 reference LD/LN.DO[.DA]: it names a logical node, not a value")
    return Ref(m.group(1), m.group(2), fc, path)


def ref_of_item(domain: str, item: str) -> Ref:
    parts = item.split("$")
    if len(parts) < 2:
        raise RefError(f"'{item}' is not a functionally constrained MMS name")
    return Ref(domain, parts[0], parts[1], tuple(parts[2:]))


def parse_rcb(rcb_ref: str) -> tuple[str, str]:
    m = RCB_REF.match(rcb_ref or "")
    if not m:
        raise RefError(f"'{rcb_ref}' is not a report control block reference LD/LN.BR|RP.name")
    return m.group(1), f"{m.group(2)}${m.group(3)}${m.group(4)}"


# ------------------------------------------------------------------ quality and time
def quality(q) -> tuple[int, list[str]]:
    """IEC 61850 Quality (BitString of 13) -> (platform quality, flag names), spec 15.4."""
    if not isinstance(q, M.BitString):
        return 192, []
    validity = (int(q[0]) << 1) | int(q[1])                       # 00 good, 01 invalid, 11 questionable
    details = [n for i, n in enumerate(DETAIL) if q[2 + i]]
    flags = list(details)
    if validity == 1:
        flags.insert(0, "invalid")
    elif validity == 3:
        flags.insert(0, "questionable")
    if q[10]:
        flags.append("substituted")
    if q[11]:
        flags.append("test")
    if q[12]:
        flags.append("operatorBlocked")
    if "failure" in details:
        return 12, flags
    if validity == 1:
        return 0, flags
    if "oldData" in details:
        return 68, flags
    if "inaccurate" in details or "badReference" in details:
        return 80, flags
    if "overflow" in details or "outOfRange" in details:
        return 84, flags
    if validity == 3 or q[11] or q[12]:
        return 64, flags
    if q[10]:
        return 216, flags
    return 192, flags


def iso(t) -> str | None:
    dt = t.when if isinstance(t, M.UtcTime) else t
    if dt is None or dt.year < 1990:
        return None
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def timestamp(t) -> tuple[str | None, list[str]]:
    """IEC 61850 TimeStamp -> (ts or None, flags); invalid clocks fall back to rx with flag CLOCK."""
    if not isinstance(t, M.UtcTime):
        return None, []
    if t.clock_failure or t.not_synchronized:
        return None, ["CLOCK"]
    return iso(t), []


# ------------------------------------------------------------------ types and values
def subtype(spec: M.TypeSpec, path: tuple) -> M.TypeSpec | None:
    for p in path:
        if spec is None:
            return None
        if spec.kind == "array":
            spec = spec.element
            continue
        spec = spec.component(p)
    return spec


def flatten(value, spec: M.TypeSpec | None, prefix: tuple = ()) -> dict:
    """Structured MMS value + its type -> {path tuple: leaf value}."""
    if spec is not None and spec.kind == "structure" and isinstance(value, list):
        out = {}
        for (name, sub), v in zip(spec.components, value):
            out.update(flatten(v, sub, prefix + (name,)))
        return out
    if spec is not None and spec.kind == "array" and isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(flatten(v, spec.element, prefix + (str(i),)))
        return out
    return {prefix: value}


def leaves(spec: M.TypeSpec, prefix: tuple = ()):
    """Yields (path, leaf type) for every basic attribute of a type."""
    if spec.kind == "structure":
        for name, sub in spec.components:
            yield from leaves(sub, prefix + (name,))
    elif spec.kind == "array":
        for i in range(min(spec.count, 64)):
            yield from leaves(spec.element, prefix + (str(i),))
    else:
        yield prefix, spec


def data_object_path(ln_type: M.TypeSpec, ref: Ref) -> tuple:
    """The data object (deepest ancestor with a 'q' attribute) that holds the quality and time of ref."""
    fc_spec = ln_type.component(ref.fc)
    best = ref.path[:1]
    spec = fc_spec
    for i, p in enumerate(ref.path):
        spec = spec.component(p) if spec is not None and spec.kind == "structure" else None
        if spec is None:
            break
        if spec.kind == "structure" and spec.component("q") is not None:
            best = ref.path[:i + 1]
    return best


def data_type_of(spec: M.TypeSpec) -> str | None:
    """Platform data type of a basic attribute (None: not a value, e.g. Quality / TimeStamp / octets)."""
    k = spec.kind
    if k == "floating-point":
        return "float"
    if k in ("integer", "unsigned"):
        return "int"
    if k == "boolean":
        return "bool"
    if k in ("visible-string", "mms-string"):
        return "string"
    if k == "bit-string" and spec.size == 2:
        return "enum"                                      # Dbpos / Tcmd
    return None


def coerce(value, data_type: str):
    """Raises TypeError if the value does not fit data_type."""
    if isinstance(value, M.BitString) and len(value.bits) == 2:
        value = (int(value[0]) << 1) | int(value[1])       # Dbpos: 0 intermediate, 1 off, 2 on, 3 bad
        if data_type == "string":
            return DBPOS[value]
    elif isinstance(value, M.BitString):
        value = sum(1 << (len(value.bits) - 1 - i) for i, b in enumerate(value.bits) if b)
    if data_type == "string":
        return value.hex() if isinstance(value, bytes) else str(value)
    if isinstance(value, (bytes, M.UtcTime, list)) or value is None:
        raise TypeError("not a basic value")
    if data_type == "bool":
        if isinstance(value, bool):
            return value
        raise TypeError("not a boolean")
    if data_type in ("int", "enum"):
        if isinstance(value, (bool, int)):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise TypeError("not a whole number")
    if data_type == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise TypeError("not a number")
    raise TypeError(f"unknown data type {data_type}")


def sample(value) -> str:
    if isinstance(value, M.BitString):
        return DBPOS[(int(value[0]) << 1) | int(value[1])] if len(value.bits) == 2 else "".join("1" if b else "0" for b in value.bits)
    if isinstance(value, M.UtcTime):
        return iso(value) or "not set"
    if isinstance(value, bytes):
        return value.hex()
    return str(value)[:60]


# ------------------------------------------------------------------ reports
@dataclass
class Report:
    rpt_id: str
    seq: int | None = None
    time: object = None
    dataset: str | None = None
    overflow: bool = False
    entry_id: bytes | None = None
    conf_rev: int | None = None
    entries: list = field(default_factory=list)            # [(member index, value, reasons)]


def parse_report(values: list, dataset_size_of) -> Report:
    """InformationReport values (IEC 61850-8-1 17.2.3) -> Report. dataset_size_of(rpt_id, dataset) gives the number
    of dataset members (the inclusion bit-string has one bit per member)."""
    if len(values) < 3 or not isinstance(values[0], str) or not isinstance(values[1], M.BitString):
        raise ValueError("not an IEC 61850 report")
    r = Report(values[0])
    opt = values[1]
    i = 2

    def take():
        nonlocal i
        v = values[i]
        i += 1
        return v
    if opt[OPT["seqNum"]]:
        r.seq = take()
    if opt[OPT["timeStamp"]]:
        r.time = take()
    if opt[OPT["dataSet"]]:
        r.dataset = take()
    if opt[OPT["bufOvfl"]]:
        r.overflow = bool(take())
    if opt[OPT["entryID"]]:
        r.entry_id = take()
    if opt[OPT["confRev"]]:
        r.conf_rev = take()
    if opt[OPT["segmentation"]]:
        take()
        take()
    inclusion = take()
    if not isinstance(inclusion, M.BitString):
        raise ValueError("report inclusion bit-string missing")
    included = [k for k, b in enumerate(inclusion.bits) if b]
    if opt[OPT["dataRef"]]:
        i += len(included)
    vals = values[i:i + len(included)]
    i += len(included)
    reasons = values[i:i + len(included)] if opt[OPT["reasonCode"]] else [None] * len(included)
    for k, v, why in zip(included, vals, reasons):
        names = [REASONS[b] for b, s in enumerate(why.bits) if s and b < len(REASONS)] if isinstance(why, M.BitString) else []
        r.entries.append((k, v, names))
    return r


def bits(names: list[str], table: dict, size: int) -> M.BitString:
    out = [False] * size
    for n in names:
        if n in table:
            out[table[n]] = True
    return M.BitString(out)
