"""Protocol-independent building blocks of an Opcare DAQ connector (spec OSS-DAQ-CON-001, 1.0).

Contents: quality codes (section 8), error codes (14.1), time and id helpers, point table with
scaling and deadband (6.2, 8a), value pipeline with report-by-exception, integrity refresh and
communication-loss handling (7.2), batcher with boot/seq (7.1) and the read-only guard (12).
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

SPEC = "1.0"


# ------------------------------------------------------------------ quality (section 8)
class Q:
    GOOD = 192
    GOOD_LOCAL_OVERRIDE = 216
    UNCERTAIN = 64
    UNCERTAIN_LAST_USABLE = 68
    UNCERTAIN_SENSOR_NOT_ACCURATE = 80
    UNCERTAIN_EU_EXCEEDED = 84
    BAD = 0
    BAD_CONFIG_ERROR = 4
    BAD_DEVICE_FAILURE = 12
    BAD_COMM_FAILURE = 24
    BAD_OUT_OF_SERVICE = 28

    ALL = frozenset({192, 216, 64, 68, 80, 84, 0, 4, 12, 24, 28})

    @staticmethod
    def is_bad(q: int) -> bool:
        return q < 64

    @staticmethod
    def combine(*codes: int) -> int:
        """Several conditions apply: the lowest code wins (Bad < Uncertain < Good)."""
        return min(codes) if codes else Q.GOOD


# ------------------------------------------------------------------ errors (section 14.1)
ERROR_CODES = frozenset({
    "CFG_INVALID", "CFG_UNSUPPORTED_SPEC", "SECRET_MISSING", "CONN_REFUSED", "CONN_TIMEOUT", "CONN_CLOSED",
    "TLS_FAILED", "AUTH_FAILED", "ADDR_INVALID", "POINT_NOT_FOUND", "TYPE_MISMATCH", "DEVICE_BUSY",
    "OP_UNSUPPORTED", "OP_FORBIDDEN", "TIMEOUT", "PROTOCOL_ERROR", "INTERNAL"})


class ConnectorError(Exception):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message

    def as_dict(self, with_ts: bool = False) -> dict:
        d = {"code": self.code, "message": self.message[:1000]}
        if with_ts:
            d["ts"] = now_iso()
        return d


# ------------------------------------------------------------------ time and ids
def iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """26-character ULID: 48-bit millisecond time + 80 random bits, Crockford base32."""
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


# ------------------------------------------------------------------ points (section 6.2, 8a)
INT_LIMIT = 2 ** 53 - 1


@dataclass
class Point:
    tag_id: str
    address: dict
    data_type: str
    gain: float = 1.0
    offset: float = 0.0
    unit: str | None = None
    deadband_mode: str | None = None
    deadband_value: float = 0.0
    scan_ms: int = 1000
    enabled: bool = True
    device: str = "main"

    @classmethod
    def from_config(cls, p: dict, default_scan_ms: int, device_of: Callable[[dict], str]) -> "Point":
        sc, db = p.get("scaling") or {}, p.get("deadband") or {}
        return cls(tag_id=p["tag_id"], address=p["address"], data_type=p["data_type"],
                   gain=sc.get("gain", 1.0), offset=sc.get("offset", 0.0), unit=p.get("unit"),
                   deadband_mode=db.get("mode"), deadband_value=db.get("value", 0.0),
                   scan_ms=p.get("scan_ms", default_scan_ms), enabled=p.get("enabled", True),
                   device=device_of(p["address"]))

    def convert(self, raw: Any) -> tuple[Any, int | None, list[str]]:
        """Raw device value -> (published value, forced quality or None, flags)."""
        if raw is None:
            return None, None, []
        dt = self.data_type
        if dt == "bool":
            return bool(raw), None, []
        if dt == "string":
            return str(raw).rstrip("\x00")[:1024], None, []
        if dt == "enum":
            return int(raw), None, []
        v = raw
        if self.gain != 1 or self.offset != 0:
            v = float(raw) * self.gain + self.offset
            if math.isfinite(v):
                v = float(f"{v:.12g}")          # drop binary noise from scaling (4998 x 0.01 -> 49.98)
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None, Q.BAD, ["NAN"]
        if dt == "int":
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if isinstance(v, int) and abs(v) > INT_LIMIT:
                return float(v), None, []
            return v, None, []
        return float(v), None, []

    def changed(self, old: Any, new: Any) -> bool:
        if old is None or new is None or not isinstance(new, (int, float)) or isinstance(new, bool):
            return old != new
        if self.deadband_mode == "abs":
            return abs(new - old) > self.deadband_value
        if self.deadband_mode == "pct":
            return abs(new - old) > abs(old) * self.deadband_value / 100.0
        return new != old


class PointTable:
    def __init__(self, config: dict, device_of: Callable[[dict], str] | None = None):
        default_scan = (config.get("acquisition") or {}).get("default_scan_ms", 1000)
        device_of = device_of or (lambda a: str(a.get("device", "main")))
        self.points: dict[str, Point] = {}
        for p in config["points"]:
            pt = Point.from_config(p, default_scan, device_of)
            if pt.enabled:
                self.points[pt.tag_id] = pt

    def by_device(self, device: str) -> list[Point]:
        return [p for p in self.points.values() if p.device == device]

    def scan_classes(self) -> dict[int, list[Point]]:
        out: dict[int, list[Point]] = {}
        for p in self.points.values():
            out.setdefault(p.scan_ms, []).append(p)
        return out


# ------------------------------------------------------------------ value pipeline (section 7.2)
@dataclass
class _Last:
    v: Any
    q: int


class ValuePipeline:
    """Report by exception, integrity refresh and communication-loss values. Emits value dicts."""

    def __init__(self, table: PointTable, integrity_s: int, emit: Callable[[dict], None],
                 clock: Callable[[], float] = time.monotonic):
        self.table, self.integrity_s, self.emit, self.clock = table, integrity_s, emit, clock
        self.last: dict[str, _Last] = {}
        self.next_integrity = clock() + integrity_s if integrity_s else math.inf

    def offer(self, tag_id: str, raw: Any, q: int = Q.GOOD, ts: str | None = None,
              flags: Iterable[str] = (), rx: str | None = None) -> bool:
        pt = self.table.points.get(tag_id)
        if pt is None:
            return False
        if q not in Q.ALL:
            raise ValueError(f"quality {q} not in spec")
        v, forced_q, extra = pt.convert(raw)
        if forced_q is not None:
            q = Q.combine(q, forced_q)
        if v is None and not Q.is_bad(q):
            q = Q.BAD
        rx = rx or now_iso()
        prev = self.last.get(tag_id)
        if prev is not None and prev.q == q and not pt.changed(prev.v, v):
            return False
        self.last[tag_id] = _Last(v, q)
        f = list(flags) + extra
        val = {"t": tag_id, "v": v, "q": q, "ts": ts or rx, "rx": rx}
        if f:
            val["f"] = f[:16]
        self.emit(val)
        return True

    def device_lost(self, device: str) -> int:
        """Publish every point of the device once with q=24 (rule 7.2.4)."""
        n, now = 0, now_iso()
        for pt in self.table.by_device(device):
            prev = self.last.get(pt.tag_id)
            if prev and prev.q == Q.BAD_COMM_FAILURE:
                continue
            self.last[pt.tag_id] = _Last(None, Q.BAD_COMM_FAILURE)
            self.emit({"t": pt.tag_id, "v": None, "q": Q.BAD_COMM_FAILURE, "ts": now, "rx": now})
            n += 1
        return n

    def tick(self) -> int:
        """Call about once per second. Publishes the integrity refresh when due (rule 7.2.2)."""
        if self.clock() < self.next_integrity:
            return 0
        self.next_integrity = self.clock() + self.integrity_s
        n, now = 0, now_iso()
        for tag, last in self.last.items():
            if last.q == Q.BAD_COMM_FAILURE:
                continue
            self.emit({"t": tag, "v": last.v, "q": last.q, "ts": now, "rx": now, "f": ["INTEGRITY"]})
            n += 1
        return n


# ------------------------------------------------------------------ batcher (section 7.1, 7.2.3)
class Batcher:
    MAX_BYTES = 240 * 1024  # stay below the 256 KiB payload limit

    def __init__(self, src: str, boot: str, publish: Callable[[dict], None], max_values: int = 500,
                 max_ms: int = 1000, clock: Callable[[], float] = time.monotonic):
        self.src, self.boot, self.publish = src, boot, publish
        self.max_values, self.max_ms, self.clock = max_values, max_ms, clock
        self.seq = 0
        self.values: list[dict] = []
        self.bytes = 0
        self.oldest: float | None = None
        self.values_total = 0
        self.batches_total = 0

    def add(self, value: dict) -> None:
        size = len(json.dumps(value, separators=(",", ":"))) + 1
        if self.values and self.bytes + size > self.MAX_BYTES:
            self.flush()
        if not self.values:
            self.oldest = self.clock()
        self.values.append(value)
        self.bytes += size
        if len(self.values) >= self.max_values:
            self.flush()

    def poll(self) -> None:
        if self.values and (self.clock() - self.oldest) * 1000 >= self.max_ms:
            self.flush()

    def flush(self) -> None:
        if not self.values:
            return
        self.seq += 1
        msg = {"spec": SPEC, "src": self.src, "boot": self.boot, "seq": self.seq, "sent": now_iso(),
               "values": self.values}
        self.values_total += len(self.values)
        self.batches_total += 1
        self.values, self.bytes, self.oldest = [], 0, None
        self.publish(msg)


# ------------------------------------------------------------------ read-only guard (section 12)
@dataclass
class ReadOnlyGuard:
    """Allow-list of outgoing request types. Call check() in the lowest send layer."""

    protocol: str
    allowed: frozenset
    on_block: Callable[[str], None] = field(default=lambda request: None)
    blocked_total: int = 0

    def check(self, request: str) -> None:
        if request in self.allowed:
            return
        self.blocked_total += 1
        self.on_block(request)
        raise ConnectorError("OP_FORBIDDEN", f"{self.protocol} request '{request}' is not on the read-only allow-list")


# Allow-lists from spec section 15. Keep in sync with CONNECTOR_SPEC.md.
ALLOW = {
    "modbus": frozenset({"FC1", "FC2", "FC3", "FC4"}),
    "opcua": frozenset({"GetEndpoints", "FindServers", "OpenSecureChannel", "CloseSecureChannel", "CreateSession",
                        "ActivateSession", "CloseSession", "Browse", "BrowseNext", "TranslateBrowsePathsToNodeIds",
                        "Read", "RegisterNodes", "UnregisterNodes", "CreateSubscription", "ModifySubscription",
                        "DeleteSubscriptions", "SetPublishingMode", "CreateMonitoredItems", "ModifyMonitoredItems",
                        "DeleteMonitoredItems", "Publish", "Republish"}),
    "mqtt": frozenset({"CONNECT", "SUBSCRIBE", "UNSUBSCRIBE", "PINGREQ", "PUBACK", "PUBREC", "PUBREL", "PUBCOMP",
                       "DISCONNECT"}),
    "camera": frozenset({"GetSystemDateAndTime", "GetCapabilities", "GetServices", "GetDeviceInformation",
                         "GetNetworkInterfaces", "GetProfiles", "GetStreamUri", "GetSnapshotUri", "GetVideoSources",
                         "GetEventProperties", "CreatePullPointSubscription", "PullMessages", "Renew", "Unsubscribe",
                         "Probe", "RTSP_OPTIONS", "RTSP_DESCRIBE", "RTSP_SETUP", "RTSP_PLAY", "RTSP_TEARDOWN",
                         "RTSP_GET_PARAMETER"}),
    # iec61850: MMS services plus writes to these report-control-block attributes only (WRITE_RCB:<attr>)
    "iec61850": frozenset({"Initiate", "Conclude", "Abort", "GetNameList", "GetVariableAccessAttributes",
                           "GetNamedVariableListAttributes", "Read"}
                          | {f"WRITE_RCB:{a}" for a in ("Resv", "ResvTms", "RptEna", "GI", "TrgOps", "IntgPd",
                                                        "BufTm", "OptFlds", "EntryID")}),
    # dnp3: function codes; FC2 only for clearing IIN1.7 (request name 'FC2_CLEAR_RESTART')
    "dnp3": frozenset({"FC0", "FC1", "FC20", "FC21", "FC2_CLEAR_RESTART"}),
    # iec104 is a GPL connector and implements its own guard: U/S frames + type ids 100, 101, 102
}
