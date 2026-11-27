"""UDP datagram listener connector (spec OSS-DAQ-CON-001 section 15.8), built on opcare_connector_sdk.

Receives telegrams pushed by devices (NMEA 0183, CSV, JSON, binary layouts, syslog) on one UDP port.
It never sends anything: the read-only guard allow-list for `udp` is empty.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import re
import socket
import time
from collections import deque
from dataclasses import dataclass, field

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, PointTable, Q, ReadOnlyGuard

from . import parse
from .parse import ParseError

SYSLOG_EVENT_SEVERITY = {0: "critical", 1: "critical", 2: "critical", 3: "high", 4: "medium", 5: "low", 6: "info",
                         7: "info"}
MAX_SYSLOG_EVENTS_PER_S = 20


@dataclass
class _Seen:
    """What arrived from one source address (diagnostics, test_connection, browse)."""
    count: int = 0
    last: float = 0.0
    samples: deque = field(default_factory=lambda: deque(maxlen=8))


class _Endpoint(asyncio.DatagramProtocol):
    def __init__(self, on_datagram):
        self.on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr) -> None:
        self.on_datagram(data, addr[0])

    def error_received(self, exc: Exception) -> None:   # ICMP errors etc.: nothing to do for a pure listener
        pass


class Listener:
    """One bound UDP socket that records what arrives. Used by the running connector and by short tests."""

    def __init__(self, host: str, port: int, max_bytes: int, on_datagram=None):
        self.host, self.port, self.max_bytes, self.on_datagram = host, port, max_bytes, on_datagram
        self.transport = None
        self.seen: dict[str, _Seen] = {}
        self.too_big = 0

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        try:
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: _Endpoint(self._received), local_addr=(self.host, self.port), family=family)
        except OSError as exc:
            raise ConnectorError("CONN_REFUSED", f"cannot listen on udp {self.host}:{self.port}: "
                                                 f"{exc.strerror or exc}") from None

    def _received(self, data: bytes, ip: str) -> None:
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        s = self.seen.setdefault(ip, _Seen())
        if len(data) > self.max_bytes:
            self.too_big += 1
            return
        s.count += 1
        s.last = time.monotonic()
        s.samples.append(data[:2048])
        if self.on_datagram:
            self.on_datagram(data, ip)

    def close(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None

    async def aclose(self) -> None:
        """Close and let the loop release the socket, so the port can be bound again at once."""
        self.close()
        for _ in range(3):
            await asyncio.sleep(0)


class Senders:
    """Maps a source address to the configured sender id (first match wins)."""

    def __init__(self, senders: list[dict]):
        self.nets = [(s["id"], ipaddress.ip_network(s["host"], strict=False)) for s in senders]

    def match(self, ip: str) -> str | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for sid, net in self.nets:
            if addr.version == net.version and addr in net:
                return sid
        return None


def _matcher(addr: dict):
    """Pre-compiled per-point reader: (line text | None, datagram bytes) -> (matched, value, ts)."""
    fmt = addr["format"]
    prefix = addr.get("prefix")
    if fmt == "binary":
        want = bytes.fromhex(addr["prefix_hex"]) if addr.get("prefix_hex") else b""

        def read_binary(_line, data):
            if want and not data.startswith(want):
                return False, None, None
            return True, parse.binary_value(data, addr), None
        return read_binary
    if fmt == "json":
        ptr, tsp, tsf = addr["value_pointer"], addr.get("timestamp_pointer"), addr.get("timestamp_format")
        mp, mv = addr.get("match_pointer"), addr.get("match_value")

        def read_json(line, _data):
            if prefix and not line.startswith(prefix):
                return False, None, None
            try:
                doc = json.loads(line[len(prefix):] if prefix else line)
            except ValueError:
                return False, None, None                      # not JSON: another kind of telegram
            if mp is not None:
                try:
                    if str(parse.json_pointer(doc, mp)) != mv:
                        return False, None, None
                except ParseError:
                    return False, None, None
            ts = parse.json_timestamp(parse.json_pointer(doc, tsp), tsf) if tsp else None
            return True, parse.json_pointer(doc, ptr), ts
        return read_json
    if fmt == "delimited":
        idx, delim = addr["field"], addr.get("delimiter", ",")

        def read_delimited(line, _data):
            if prefix and not line.startswith(prefix):
                return False, None, None
            fields = parse.split_fields(line, delim)
            if idx >= len(fields):
                raise ParseError(f"line has {len(fields)} fields, field {idx} missing")
            return True, fields[idx], None
        return read_delimited
    if fmt == "nmea":
        sentence, idx, coord = addr["sentence"], addr["field"], addr.get("coordinate", False)

        def read_nmea(line, _data):
            if prefix and not line.startswith(prefix):
                return False, None, None
            try:
                got = parse.nmea(line)
            except ParseError:
                return False, None, None                      # bad checksum: the sentence is ignored (15.8)
            if not got or got[0] != sentence:
                return False, None, None
            fields = got[1]
            if idx > len(fields) or fields[idx - 1] == "":
                raise ParseError(f"{sentence} field {idx} is empty")
            if coord:
                if idx >= len(fields):
                    raise ParseError(f"{sentence} field {idx + 1} (hemisphere) missing")
                return True, parse.nmea_coordinate(fields[idx - 1], fields[idx]), None
            return True, fields[idx - 1], None
        return read_nmea
    if fmt == "syslog":
        what = addr["field"]
        rx = re.compile(addr["match"]) if addr.get("match") else None

        def read_syslog(line, _data):
            msg = parse.syslog(line)
            if not msg or (rx and not rx.search(msg["message"])):
                return False, None, None
            return True, msg[what], None
        return read_syslog

    def read_raw(line, _data):                               # fmt == "raw"
        if prefix and not line.startswith(prefix):
            return False, None, None
        return True, parse.raw_value(line[len(prefix):] if prefix else line), None
    return read_raw


class UdpConnector(Connector):
    type = "udp"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": False}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.listener: Listener | None = None
        self.senders: Senders | None = None
        self.readers: dict[str, list] = {}
        self.dev_state: dict[str, str] = {}
        self.last_seen: dict[str, float] = {}
        self.started = 0.0
        self.task: asyncio.Task | None = None
        self.dropped_unknown = 0
        self.parse_errors = 0
        self.syslog_events = 0
        self.syslog_suppressed = 0
        self._syslog_window = (0, 0)
        # Nothing is ever sent; the guard is here so that any future send path fails closed (spec 12, 15.8).
        self.guard = ReadOnlyGuard("udp", ALLOW["udp"])

    # ---------------------------------------------------------------- configuration
    def devices(self, config: dict) -> list[str]:
        return [s["id"] for s in config["connection"]["senders"]]

    async def validate(self, config: dict) -> list[str]:
        conn, errs = config["connection"], []
        ids = [s["id"] for s in conn["senders"]]
        if len(set(ids)) != len(ids):
            errs.append("duplicate sender id")
        for s in conn["senders"]:
            try:
                ipaddress.ip_network(s["host"], strict=False)
            except ValueError:
                errs.append(f"sender {s['id']}: '{s['host']}' is not an IP address or network")
        try:
            ipaddress.ip_address(conn.get("bind_host", "0.0.0.0"))
        except ValueError:
            errs.append(f"bind_host '{conn.get('bind_host')}' is not an IP address")
        for p in config["points"]:
            a, tag, dt = p["address"], p["tag_id"], p["data_type"]
            if a["device"] not in ids:
                errs.append(f"{tag}: unknown sender '{a['device']}'")
            fmt = a["format"]
            if fmt == "binary":
                enc = a["encoding"]
                if enc in ("bool", "bit") and dt != "bool":
                    errs.append(f"{tag}: encoding '{enc}' must be published as data_type 'bool'")
                if enc == "string" and dt != "string":
                    errs.append(f"{tag}: encoding 'string' must be published as data_type 'string'")
                if enc not in ("bool", "bit", "string") and dt in ("bool", "string"):
                    errs.append(f"{tag}: encoding '{enc}' is a number; use data_type int, float or enum")
            if fmt == "nmea" and a.get("coordinate") and dt != "float":
                errs.append(f"{tag}: an NMEA coordinate is published as data_type 'float'")
            if fmt == "syslog":
                if a["field"] in ("severity", "facility") and dt not in ("int", "enum", "float"):
                    errs.append(f"{tag}: syslog {a['field']} is a number; use data_type int")
                if a.get("match"):
                    try:
                        re.compile(a["match"])
                    except re.error as exc:
                        errs.append(f"{tag}: match is not a valid regular expression ({exc})")
        return errs

    @property
    def silence_s(self) -> float:
        return self.config["connection"].get("silence_timeout_s", 60)

    def _index(self) -> None:
        self.readers = {}
        for p in self.table.points.values():
            self.readers.setdefault(p.device, []).append((p, _matcher(p.address)))

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self.senders = Senders(config["connection"]["senders"])
        self.dev_state = {d: "connecting" for d in self.devices(config)}
        self.last_seen = {}
        self._index()
        self.task = asyncio.create_task(self._run())

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        if config["connection"] != self.config.get("connection"):
            return False
        self.config, self.table = config, table
        self._index()
        return True

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.listener:
            await self.listener.aclose()
            self.listener = None

    async def _run(self) -> None:
        conn = self.config["connection"]
        delay = 1.0
        while self.listener is None:                       # the port may be busy: retry with backoff (spec 13)
            lst = Listener(conn.get("bind_host", "0.0.0.0"), conn["bind_port"], conn.get("max_datagram_bytes", 8192),
                           self._on_datagram)
            try:
                await lst.open()
                self.listener = lst
            except ConnectorError as exc:
                for d in self.dev_state:
                    self._set_state(d, "unreachable", exc)
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
                delay = min(delay * 2, 60.0)
        self.started = time.monotonic()
        while True:                                        # silence watchdog (rule 7.2.4)
            await asyncio.sleep(0.5)
            if not self.silence_s:
                continue
            now = time.monotonic()
            for d in self.dev_state:
                last = self.last_seen.get(d, self.started)
                if now - last > self.silence_s and self.dev_state[d] != "unreachable":
                    self._set_state(d, "unreachable", ConnectorError(
                        "CONN_TIMEOUT", f"no datagram from sender {d} for {int(self.silence_s)} s"))

    def _set_state(self, device: str, state: str, exc: ConnectorError | None = None) -> None:
        if self.dev_state.get(device) != state:
            self.dev_state[device] = state
            self.sink.device_state(device, state, exc)

    # ---------------------------------------------------------------- receiving
    def _on_datagram(self, data: bytes, ip: str) -> None:
        device = self.senders.match(ip)
        if device is None:
            self.dropped_unknown += 1
            return
        self.last_seen[device] = time.monotonic()
        if self.dev_state.get(device) != "connected":
            self._set_state(device, "connected")
        else:
            self.sink.device_ok(device)
        conn = self.config["connection"]
        text_lines = None
        if any(p.address["format"] != "binary" for p, _ in self.readers.get(device, [])) or conn.get("syslog"):
            text = parse.text_of(data, conn.get("text_encoding", "utf-8"))
            text_lines = parse.lines_of(text)
            whole = text.strip()
            if whole and whole[0] in "{[" and len(text_lines) > 1:
                try:
                    json.loads(whole)
                    text_lines = [whole]                    # one pretty-printed JSON document, not JSON lines
                except ValueError:
                    pass
        if conn.get("syslog") and text_lines:
            self._syslog(device, text_lines, conn["syslog"])
        for p, read in self.readers.get(device, []):
            units = [None] if p.address["format"] == "binary" else text_lines or []
            for line in units:
                try:
                    matched, value, ts = read(line, data)
                    if not matched:
                        continue
                    self.sink.value(p.tag_id, parse.coerce(value, p.data_type), Q.GOOD, ts=ts)
                except ParseError:
                    self.parse_errors += 1
                    self.sink.value(p.tag_id, None, Q.BAD, flags=["PARSE_ERROR"])

    def _syslog(self, device: str, lines: list[str], opts: dict) -> None:
        limit = parse.SEVERITY_NAMES.index(opts.get("min_severity", "warning"))
        for line in lines:
            msg = parse.syslog(line)
            if not msg or msg["severity"] > limit:
                continue
            second = int(time.monotonic())
            n = self._syslog_window[1] + 1 if self._syslog_window[0] == second else 1
            self._syslog_window = (second, n)
            if n > MAX_SYSLOG_EVENTS_PER_S:                # a flood must not drown the event stream
                self.syslog_suppressed += 1
                continue
            self.syslog_events += 1
            attrs = {"severity": parse.SEVERITY_NAMES[msg["severity"]], "facility": msg["facility"]}
            attrs.update({k: msg[k] for k in ("hostname", "app_name") if msg[k]})
            self.sink.event("syslog.message", SYSLOG_EVENT_SEVERITY[msg["severity"]],
                            msg["message"][:1000] or "(empty syslog message)", device=device, attrs=attrs)

    # ---------------------------------------------------------------- admin operations
    def _live_listener(self, conn: dict) -> Listener | None:
        cur = self.config.get("connection") or {}
        if self.listener and cur.get("bind_port") == conn["bind_port"] and \
                cur.get("bind_host", "0.0.0.0") == conn.get("bind_host", "0.0.0.0"):
            return self.listener
        return None

    async def _listen(self, conn: dict, seconds: float, done=lambda seen: False) -> tuple[dict, float]:
        """Run a listener (or watch the live one) for up to `seconds`. Returns (seen, start time)."""
        live = self._live_listener(conn)
        t0 = time.monotonic()
        lst = live or Listener(conn.get("bind_host", "0.0.0.0"), conn["bind_port"], conn.get("max_datagram_bytes", 8192))
        if not live:
            await lst.open()
        try:
            while time.monotonic() - t0 < seconds and not done(lst.seen):
                await asyncio.sleep(0.1)
        finally:
            if not live:
                await lst.aclose()
        return lst.seen, (t0 - self.silence_window(conn) if live else t0)

    @staticmethod
    def silence_window(conn: dict) -> float:
        return min(conn.get("silence_timeout_s", 60) or 60, 60)

    async def test_connection(self, config: dict) -> dict:
        conn = config["connection"]
        senders = Senders(conn["senders"])
        timeout_s = (config.get("acquisition") or {}).get("request_timeout_ms", 3000) / 1000
        listen_s = min(30.0, max(3.0, timeout_s))

        def heard(seen: dict) -> set:
            return {senders.match(ip) for ip, s in seen.items() if s.count and s.last >= t_from}

        t_from = time.monotonic() - (self.silence_window(conn) if self._live_listener(conn) else 0)
        try:
            seen, t_from = await self._listen(conn, listen_s,
                                              lambda s: heard(s) >= {x["id"] for x in conn["senders"]})
        except ConnectorError as exc:
            return {"devices": [{"id": s["id"], "ok": False, "error": exc.as_dict()} for s in conn["senders"]]}
        out = []
        for s in conn["senders"]:
            hits = [(ip, x) for ip, x in seen.items() if senders.match(ip) == s["id"] and x.count and x.last >= t_from]
            if hits:
                ip, x = max(hits, key=lambda h: h[1].last)
                out.append({"id": s["id"], "ok": True, "latency_ms": 0,
                            "info": {"from": ip, "datagrams": sum(h[1].count for h in hits),
                                     "format": parse.detect(x.samples[-1], conn.get("text_encoding", "utf-8"))}})
            else:
                out.append({"id": s["id"], "ok": False, "latency_ms": round(listen_s * 1000),
                            "error": {"code": "CONN_TIMEOUT",
                                      "message": f"no datagram from {s['host']} on udp port {conn['bind_port']} "
                                                 f"within {listen_s:g} s"}})
        others = sorted(ip for ip in seen if senders.match(ip) is None)
        if others and out:
            out[0].setdefault("info", {})["other_senders"] = others[:10]
        return {"devices": out}

    async def browse(self, args: dict) -> dict:
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        conn = cfg["connection"]
        senders = Senders(conn["senders"])
        enc = conn.get("text_encoding", "utf-8")
        listen_s = min(60, max(1, args.get("listen_s", 10)))
        live = self._live_listener(conn)
        if live and live.seen:
            seen = live.seen
        else:
            seen, _ = await self._listen(conn, listen_s, lambda s: sum(x.count for x in s.values()) >= 3 and
                                         time.monotonic() - min(x.last for x in s.values() if x.count) > 1)
        path, depth = args.get("path", ""), args.get("depth", 1)
        items = []
        for ip, s in sorted(seen.items()):
            if not s.samples or (path and path != ip):
                continue
            sample, device = s.samples[-1], senders.match(ip)
            fmt = parse.detect(sample, enc)
            text = parse.text_of(sample, enc)
            items.append({"path": ip, "name": f"{ip}{'' if device else ' (not a configured sender)'}", "kind": "object",
                          "has_children": fmt != "raw",
                          "info": {"device": device, "datagrams": s.count, "format": fmt,
                                   "sample": sample[:256].hex(":") if fmt == "binary" else text[:256]}})
            if depth >= 2 or path == ip:
                latest: dict[str, bytes] = {}
                for smp in s.samples:                       # one sender may send several kinds of telegram
                    latest[parse.detect(smp, enc)] = smp
                formats = sorted(latest)
                items[-1]["info"]["format"] = ", ".join(formats)
                for f in formats:
                    items += _children(ip, device, f, latest[f], parse.text_of(latest[f], enc), s)
        return {"items": items[:args.get("max_items", 500)]}

    async def diagnostics(self) -> dict:
        lst = self.listener
        return {"listening": bool(lst), "port": self.config.get("connection", {}).get("bind_port"),
                "dropped_unknown_sender": self.dropped_unknown, "dropped_too_big": lst.too_big if lst else 0,
                "parse_errors": self.parse_errors, "syslog_events": self.syslog_events,
                "syslog_suppressed": self.syslog_suppressed,
                "senders": {d: {"state": st, "last_datagram_s_ago": round(time.monotonic() - self.last_seen[d], 1)
                                if d in self.last_seen else None} for d, st in self.dev_state.items()},
                "addresses": {ip: s.count for ip, s in list(lst.seen.items())[:50]} if lst else {}}


def _children(ip: str, device: str | None, fmt: str, sample: bytes, text: str, s: _Seen) -> list[dict]:
    """Variables found in a sample, each with an address ready to paste into a point (spec 15.8 browse)."""
    dev = device or "?"
    out = []

    def var(name, value, address, data_type):
        out.append({"path": f"{ip}/{name}", "name": name, "kind": "variable", "has_children": False,
                    "data_type": data_type, "address": {"device": dev, **address}, "info": {"sample": str(value)[:80]}})

    def kind(v):
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        try:
            parse.coerce(v, "float")
            return "float"
        except ParseError:
            return "string"

    lines = parse.lines_of(text)
    if fmt == "json":
        try:
            doc = json.loads(text)
        except ValueError:
            doc = json.loads(lines[0])
        for ptr, v in parse.json_leaves(doc):
            var(ptr, v, {"format": "json", "value_pointer": ptr}, kind(v))
    elif fmt == "nmea":
        sentences = {}
        for smp in s.samples:
            for ln in parse.lines_of(parse.text_of(smp)):
                try:
                    got = parse.nmea(ln)
                except ParseError:
                    continue
                if got:
                    sentences[got[0]] = got[1]
        for sent, fields in sentences.items():
            for i, v in enumerate(fields[:30], 1):
                if v != "":
                    var(f"{sent}.{i}", v, {"format": "nmea", "sentence": sent, "field": i}, kind(v))
    elif fmt == "delimited":
        line = lines[0]
        delim = next(d for d in (",", ";", "\t") if d in line)
        for i, v in enumerate(parse.split_fields(line, delim)[:50]):
            var(f"field {i}", v, {"format": "delimited", "delimiter": delim, "field": i}, kind(v))
    elif fmt == "syslog":
        for f in ("severity", "facility", "message", "app_name", "hostname"):
            msg = parse.syslog(lines[0]) or {}
            var(f, msg.get(f), {"format": "syslog", "field": f}, "int" if f in ("severity", "facility") else "string")
    return out
