"""Datagram parsing for the UDP connector (spec OSS-DAQ-CON-001 section 15.8).

Pure functions, no I/O: JSON pointers, delimited telegrams, NMEA 0183 sentences, binary layouts,
raw values and syslog (RFC 5424 and RFC 3164). Used by connector.py and its tests.
"""
from __future__ import annotations

import json
import math
import re
import struct
from datetime import datetime, timezone
from typing import Any


class ParseError(Exception):
    """The datagram matched the point but its value could not be read."""


# ------------------------------------------------------------------ helpers
def text_of(data: bytes, encoding: str = "utf-8") -> str:
    return data.decode({"utf-8": "utf-8", "ascii": "ascii", "latin-1": "latin-1"}[encoding], errors="replace")


def lines_of(text: str) -> list[str]:
    return [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]


def coerce(value: Any, data_type: str) -> Any:
    """Text or JSON value -> raw value for Point.convert (section 8a)."""
    if value is None:
        raise ParseError("no value")
    if data_type == "string":
        return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    if data_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        t = str(value).strip().lower()
        if t in ("1", "true", "on", "yes", "a", "ok", "up", "closed"):
            return True
        if t in ("0", "false", "off", "no", "v", "down", "open"):
            return False
        raise ParseError(f"'{str(value)[:20]}' is not on/off")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        num = value
    else:
        t = str(value).strip()
        try:
            num = int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)
        except ValueError:
            raise ParseError(f"'{t[:20]}' is not a number") from None
    if isinstance(num, float) and not math.isfinite(num):
        return num                                    # Point.convert publishes NaN as q=0 with f=["NAN"]
    if data_type in ("int", "enum") and isinstance(num, float):
        return num if not num.is_integer() else int(num)
    return num


# ------------------------------------------------------------------ JSON (RFC 6901 pointers)
def json_pointer(doc: Any, pointer: str) -> Any:
    if pointer == "":
        return doc
    cur = doc
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not key.isdigit() or int(key) >= len(cur):
                raise ParseError(f"no element {pointer}")
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            if key not in cur:
                raise ParseError(f"no field {pointer}")
            cur = cur[key]
        else:
            raise ParseError(f"no field {pointer}")
    return cur


def json_timestamp(value: Any, fmt: str | None) -> str:
    try:
        if fmt == "epoch_ms":
            dt = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        elif fmt == "epoch_s":
            dt = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ParseError(f"bad timestamp {str(value)[:30]}") from None
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def json_leaves(doc: Any, prefix: str = "", limit: int = 200) -> list[tuple[str, Any]]:
    """(pointer, value) of every scalar in a JSON document, for browse."""
    out: list[tuple[str, Any]] = []

    def walk(node: Any, ptr: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{ptr}/{str(k).replace('~', '~0').replace('/', '~1')}")
        elif isinstance(node, list):
            for i, v in enumerate(node[:50]):
                walk(v, f"{ptr}/{i}")
        else:
            out.append((ptr, node))
    walk(doc, prefix)
    return out


# ------------------------------------------------------------------ delimited
def split_fields(line: str, delimiter: str = ",") -> list[str]:
    return line.split() if delimiter == " " else [f.strip() for f in line.split(delimiter)]


# ------------------------------------------------------------------ NMEA 0183
def nmea(line: str) -> tuple[str, list[str]] | None:
    """'$GPRMC,...*hh' -> ('RMC', [fields after the address]). None if not NMEA. ParseError on a bad checksum."""
    if not line or line[0] not in "$!":
        return None
    body, star, check = line[1:].partition("*")
    if star:
        try:
            want = int(check[:2], 16)
        except ValueError:
            raise ParseError("bad NMEA checksum field") from None
        got = 0
        for ch in body:
            got ^= ord(ch)
        if got != want:
            raise ParseError("NMEA checksum mismatch")
    parts = body.split(",")
    addr = parts[0]
    if not re.fullmatch(r"[A-Z0-9]{3,6}", addr):
        return None
    kind = addr if addr.startswith("P") else addr[2:] if len(addr) >= 5 else addr
    return kind, parts[1:]


def nmea_coordinate(value: str, hemisphere: str) -> float:
    """'3001.2345','N' -> 30.0205750 decimal degrees; S and W are negative."""
    try:
        v = float(value)
    except ValueError:
        raise ParseError(f"bad coordinate '{value[:15]}'") from None
    deg = int(v // 100)
    dd = deg + (v - deg * 100) / 60
    if hemisphere.upper() in ("S", "W"):
        dd = -dd
    elif hemisphere.upper() not in ("N", "E"):
        raise ParseError(f"bad hemisphere '{hemisphere[:3]}'")
    return round(dd, 7)


# ------------------------------------------------------------------ binary
FORMATS = {"int8": "b", "uint8": "B", "int16": "h", "uint16": "H", "int32": "i", "uint32": "I",
           "int64": "q", "uint64": "Q", "float32": "f", "float64": "d", "bool": "B", "bit": "B"}


def binary_width(address: dict) -> int:
    enc = address["encoding"]
    return address.get("length", 1) if enc == "string" else struct.calcsize(FORMATS[enc])


def binary_value(data: bytes, address: dict) -> Any:
    off, enc = address["offset"], address["encoding"]
    w = binary_width(address)
    if len(data) < off + w:
        raise ParseError(f"datagram has {len(data)} bytes, needs {off + w}")
    chunk = data[off:off + w]
    if enc == "string":
        return chunk.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    v = struct.unpack(("<" if address.get("byte_order") == "little" else ">") + FORMATS[enc], chunk)[0]
    if enc == "bool":
        return v != 0
    if enc == "bit":
        return bool((v >> address["bit"]) & 1)
    return v


# ------------------------------------------------------------------ raw
def raw_value(line: str) -> Any:
    t = line.strip()
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    try:
        return int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)
    except ValueError:
        return t


# ------------------------------------------------------------------ syslog
SEVERITY_NAMES = ["emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"]
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_RFC5424 = re.compile(r"^<(\d{1,3})>1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|(?:\[.*?\])+) ?(.*)$", re.S)
_RFC3164 = re.compile(r"^<(\d{1,3})>(?:([A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (\S+) )?(?:([^:\[\s]+)(?:\[\d+\])?: )?(.*)$", re.S)


def syslog(line: str) -> dict | None:
    """RFC 5424 or RFC 3164 message -> {severity, facility, hostname, app_name, message}. None if not syslog."""
    if not line.startswith("<"):
        return None
    m = _RFC5424.match(line)
    if m:
        pri, host, app, msg = int(m.group(1)), m.group(3), m.group(4), m.group(8)
        if msg.startswith("\ufeff"):
            msg = msg[1:]
    else:
        m = _RFC3164.match(line)
        if not m:
            return None
        pri, host, app, msg = int(m.group(1)), m.group(3), m.group(4), m.group(5)
    if pri > 191:
        return None
    return {"severity": pri % 8, "facility": pri // 8, "hostname": None if host in (None, "-") else host,
            "app_name": None if app in (None, "-") else app, "message": msg.strip()}


# ------------------------------------------------------------------ format detection (browse)
def detect(data: bytes, encoding: str = "utf-8") -> str:
    if not data:
        return "binary"
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in data) / len(data)
    if printable < 0.9:
        return "binary"
    first = lines_of(text_of(data, encoding))[:1]
    if not first:
        return "binary"
    line = first[0]
    if line.startswith("<") and syslog(line):
        return "syslog"
    try:
        if nmea(line):
            return "nmea"
    except ParseError:
        return "nmea"
    try:
        doc = json.loads(line)
        if isinstance(doc, (dict, list)):
            return "json"
    except ValueError:
        pass
    for d in (",", ";", "\t"):
        if d in line:
            return "delimited"
    return "raw"
