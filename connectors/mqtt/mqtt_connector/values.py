"""Payload values: RFC 6901 JSON pointers, timestamps and text -> typed raw values (spec 8a)."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any


class ParseError(Exception):
    """The message matched the point but its value could not be read."""


def iso_ms(ms: float) -> str:
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def json_pointer(doc: Any, pointer: str) -> Any:
    cur = doc
    for raw in pointer.split("/")[1:] if pointer else []:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
            cur = cur[int(key)]
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            raise ParseError(f"no field {pointer}")
    return cur


def timestamp(value: Any, fmt: str | None) -> str:
    try:
        if fmt == "epoch_ms":
            return iso_ms(float(value))
        if fmt == "epoch_s":
            return iso_ms(float(value) * 1000)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return iso_ms(dt.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ParseError(f"bad timestamp {str(value)[:30]}") from None


def raw_value(text: str) -> Any:
    t = text.strip()
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    try:
        return int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)
    except ValueError:
        return t


def coerce(value: Any, data_type: str) -> Any:
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
        if t in ("1", "true", "on", "yes", "open", "running"):
            return True
        if t in ("0", "false", "off", "no", "closed", "stopped"):
            return False
        raise ParseError(f"'{str(value)[:20]}' is not on/off")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        num = value
    else:
        try:
            t = str(value).strip()
            num = int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)
        except ValueError:
            raise ParseError(f"'{str(value)[:20]}' is not a number") from None
    if data_type in ("int", "enum") and isinstance(num, float) and math.isfinite(num) and num.is_integer():
        return int(num)
    return num


def leaves(doc: Any, limit: int = 200) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []

    def walk(node, ptr):
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
    walk(doc, "")
    return out
