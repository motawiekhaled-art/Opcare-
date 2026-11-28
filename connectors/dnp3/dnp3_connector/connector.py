"""DNP3 master connector (spec OSS-DAQ-CON-001 section 15.6), built on opcare_connector_sdk and our own DNP3 stack
(link.py, app.py, master.py): no third-party DNP3 library.

Reads RTUs, protection relays, IEDs and gateways in substations, water and oil & gas (SEL, GE Multilin, ABB, Siemens
SICAM, Schneider Saitel/SCADAPack, NovaTech, Survalent, Triangle MicroWorks gateways ...): integrity poll, class
1/2/3 event polls and unsolicited reports, with the outstation's event timestamps.

Read-only (spec 12 and 15.6): the stack can only build CONFIRM, READ, ENABLE/DISABLE UNSOLICITED and the WRITE that
clears IIN1.7. Controls, freeze, restart, file services and time synchronisation do not exist in the code (spec 12
rule 3: there is no time-sync feature to switch off; NEED_TIME is reported as device.time_invalid, nothing is written).
"""
from __future__ import annotations

import asyncio
import os
import random
import ssl
import tempfile
import time
from datetime import datetime, timezone

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, PointTable, Q, ReadOnlyGuard

from . import app as A
from .master import Dnp3Master

DEVICE = "outstation"
STATE_NAMES = {0: "intermediate", 1: "off", 2: "on", 3: "indeterminate"}


def iso(dt: datetime | None) -> str | None:
    if dt is None or dt.year < 1990:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def coerce(kind: str, value, data_type: str):
    """Value of a DNP3 point -> configured data type. Raises TypeError when it does not fit (spec 15.6 mapping)."""
    if data_type == "string":
        return STATE_NAMES.get(value, str(value)) if kind == "double_bit_binary_input" else str(value)
    if data_type == "bool":
        if kind in ("binary_input", "binary_output_status"):
            return bool(value)
        raise TypeError(f"{kind} is not an on/off value")
    if data_type in ("int", "enum"):
        if isinstance(value, bool) or isinstance(value, int):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise TypeError("value is not a whole number")
    if data_type == "float":
        if isinstance(value, (bool, int, float)) and kind != "double_bit_binary_input":
            return float(value)
        raise TypeError(f"{kind} is not a number")
    raise TypeError(f"unknown data type {data_type}")


def tls_context(ca_pem: str | None, cert_pem: str | None, key_pem: str | None, insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    if cert_pem and key_pem:
        with tempfile.TemporaryDirectory() as d:                 # ssl only loads client keys from files
            cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
            for path, pem in ((cp, cert_pem), (kp, key_pem)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(pem)
            ctx.load_cert_chain(cp, kp)
    if insecure:                                                 # spec 14: only with insecure_skip_verify
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Dnp3Connector(Connector):
    type = "dnp3"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": True}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.master: Dnp3Master | None = None
        self.task: asyncio.Task | None = None
        self.state = "connecting"
        self.by_address: dict[tuple[str, int], list] = {}
        self.stats = {"integrity_polls": 0, "event_polls": 0, "unsolicited": 0, "values": 0, "events": 0,
                      "restarts": 0, "reconnects": 0, "type_errors": 0}
        self.iin_seen: set[str] = set()
        self.last_integrity: str | None = None
        self.missing: list[str] = []
        self._wake = asyncio.Event()

    # ---------------------------------------------------------------- configuration
    def device_of(self, address: dict) -> str:
        return DEVICE

    def devices(self, config: dict) -> list[str]:
        return [DEVICE]

    async def validate(self, config: dict) -> list[str]:
        errs = []
        conn = config["connection"]
        if conn.get("master_address", 1) == conn["outstation_address"]:
            errs.append("connection: master_address and outstation_address must differ")
        tls = conn.get("tls") or {}
        if tls.get("enabled") and bool(tls.get("client_cert_ref")) != bool(tls.get("client_key_ref")):
            errs.append("connection/tls: client_cert_ref and client_key_ref go together")
        seen = {}
        for p in config["points"]:
            key = (p["address"]["group"], p["address"]["index"])
            kind, dt = key[0], p["data_type"]
            ok = {"binary_input": ("bool", "int", "string"), "binary_output_status": ("bool", "int", "string"),
                  "double_bit_binary_input": ("enum", "int", "string")}.get(kind, ("float", "int", "string"))
            if dt not in ok:
                errs.append(f"{p['tag_id']}: a {kind} point cannot be '{dt}' (use {' or '.join(ok)})")
            if key in seen:
                errs.append(f"{p['tag_id']}: {kind} {key[1]} is already tag {seen[key]}")
            seen[key] = p["tag_id"]
        return errs

    def _acq(self, config: dict | None = None) -> dict:
        return (config or self.config).get("acquisition") or {}

    def _make_master(self, config: dict, on_unsolicited=None) -> Dnp3Master:
        conn = config["connection"]
        tls = conn.get("tls") or {}
        ctx = None
        if tls.get("enabled"):
            ctx = tls_context(self.secret(tls["ca_cert_ref"]) if tls.get("ca_cert_ref") else None,
                              self.secret(tls["client_cert_ref"]) if tls.get("client_cert_ref") else None,
                              self.secret(tls["client_key_ref"]) if tls.get("client_key_ref") else None,
                              tls.get("insecure_skip_verify", False))
        return Dnp3Master(conn["host"], conn.get("port", 20000), conn.get("master_address", 1), conn["outstation_address"],
                          ReadOnlyGuard("dnp3", ALLOW["dnp3"], on_block=self._blocked),
                          conn.get("response_timeout_ms", 5000) / 1000, conn.get("keep_alive_s", 60), ctx, on_unsolicited)

    def _blocked(self, request: str) -> None:
        if self.sink:
            self.sink.event("guard.blocked", "critical", f"Blocked DNP3 {request}", attrs={"request": request})

    def _same_connection(self, config: dict) -> bool:
        """True when config talks to the outstation we are already connected to (DNP3 outstations usually accept
        one master connection, so tests, browse and reads reuse it instead of opening a second one)."""
        return (self.master is not None and not self.master.closed.is_set() and self.state == "connected"
                and config.get("connection") == self.config.get("connection"))

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self._index()
        self.task = asyncio.create_task(self._run())

    def _index(self) -> None:
        self.by_address = {}
        for p in self.table.points.values():
            self.by_address.setdefault((p.address["group"], p.address["index"]), []).append(p)

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        if config.get("connection") != self.config.get("connection") or self._acq(config) != self._acq():
            return False
        self.config, self.table = config, table
        self._index()
        self._wake.set()                                       # integrity poll so new points get values now
        return True

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.master:
            await self.master.close()
            self.master = None

    async def _run(self) -> None:
        delay = 1.0
        while True:
            connected_at = None
            try:
                self.master = self._make_master(self.config, self._unsolicited)
                await self.master.connect()
                connected_at = time.monotonic()
                await self._session()
            except ConnectorError as exc:
                self._set_state("unreachable", exc)
            finally:
                if self.master:
                    await self.master.close()
            self.stats["reconnects"] += 1
            if connected_at is not None and time.monotonic() - connected_at > 60:
                delay = 1.0
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 60.0)

    def _set_state(self, state: str, exc: ConnectorError | None = None) -> None:
        if state != self.state:
            self.state = state
            self.sink.device_state(DEVICE, state, exc)

    async def _session(self) -> None:
        acq = self._acq()
        m = self.master
        if acq.get("integrity_on_connect", True):
            await self._integrity()
        else:
            await self._handle(await m.poll((1, 2, 3)), "poll")
        self._set_state("connected")
        if acq.get("unsolicited", False):
            await self._handle(await m.set_unsolicited(True), "poll")
        polls = acq.get("class_polls") or {}
        period = {c: polls.get(f"class{c}_ms", 5000) / 1000 for c in (1, 2, 3)}
        integrity_s = acq.get("integrity_period_s", 3600)
        now = time.monotonic()
        due = {c: now + p for c, p in period.items() if p > 0}
        next_integrity = now + integrity_s if integrity_s else None
        while True:
            now = time.monotonic()
            soonest = min([*due.values(), *( [next_integrity] if next_integrity else [])], default=now + 60)
            closed = asyncio.create_task(m.closed.wait())
            woken = asyncio.create_task(self._wake.wait())
            done, pending = await asyncio.wait({closed, woken}, timeout=max(0.0, soonest - now),
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if m.closed.is_set():
                raise m.close_reason or ConnectorError("CONN_CLOSED", "connection to the outstation closed")
            now = time.monotonic()
            if self._wake.is_set() or (next_integrity and now >= next_integrity):
                self._wake.clear()
                await self._integrity()
                next_integrity = time.monotonic() + integrity_s if integrity_s else None
                continue
            ready = tuple(c for c, t in due.items() if now >= t)
            if ready:
                t0 = time.monotonic()
                await self._handle(await m.poll(ready), "poll")
                self.stats["event_polls"] += 1
                for c in ready:
                    due[c] = time.monotonic() + period[c]
                if (time.monotonic() - t0) * 1000 > min(period[c] for c in ready) * 1000:
                    self.sink.scan_overrun(int(min(period[c] for c in ready) * 1000))

    async def _integrity(self) -> None:
        frags = await self.master.integrity()
        self.stats["integrity_polls"] += 1
        self.last_integrity = iso(datetime.now(timezone.utc))
        seen = await self._handle(frags, "integrity")
        self.missing = []
        for key, pts in self.by_address.items():                  # configured but not in the outstation
            if key not in seen:
                for p in pts:
                    self.missing.append(p.tag_id)
                    self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, flags=["NOT_IN_OUTSTATION"])

    def _unsolicited(self, frag: A.Fragment) -> None:
        self.stats["unsolicited"] += 1
        asyncio.get_running_loop().create_task(self._handle([frag], "unsolicited"))

    async def _handle(self, frags: list[A.Fragment], how: str) -> set:
        """Publishes the values of response fragments and reacts to IIN bits. Returns the addresses seen."""
        seen = set()
        for f in frags:
            for o in f.objects:
                if not isinstance(o, A.Point):
                    continue
                seen.add((o.kind, o.index))
                self._report(o)
        if frags:
            self.sink.device_ok(DEVICE)
            await self._iin(frags[-1].iin, how)
        return seen

    def _report(self, o: A.Point) -> None:
        pts = self.by_address.get((o.kind, o.index))
        if not pts:
            return
        if o.event:
            self.stats["events"] += 1
        flags = A.flag_names(o.kind, o.flags) if o.flags is not None else []
        q = A.quality(o.kind, o.flags)
        ts = iso(o.time)
        for p in pts:
            self.stats["values"] += 1
            if Q.is_bad(q):
                self.sink.value(p.tag_id, None, q, ts=ts, flags=flags)
                continue
            try:
                v = coerce(o.kind, o.value, p.data_type)
            except TypeError:
                self.stats["type_errors"] += 1
                self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, ts=ts, flags=["TYPE_MISMATCH"])
                continue
            self.sink.value(p.tag_id, v, q, ts=ts, flags=[f for f in flags if f != "ONLINE"])

    async def _iin(self, iin: int, how: str) -> None:
        names = {n for b, n in A.IIN_NAMES.items() if iin & (1 << b)}
        new = names - self.iin_seen
        self.iin_seen = names
        if "DEVICE_RESTART" in names:
            self.stats["restarts"] += 1
            self.sink.event("device.restart", "low", "The outstation reports it restarted (IIN1.7)", device=DEVICE)
            await self.master.clear_restart()                    # the only WRITE allowed (spec 15.6)
            if how != "integrity":
                self._wake.set()                                 # values may have reset: integrity poll
        if "NEED_TIME" in new:
            self.sink.event("device.time_invalid", "low", "The outstation's clock is not set (IIN1.4 NEED_TIME); "
                            "the connector does not synchronise time (read-only), event times may be wrong",
                            device=DEVICE)
        if "EVENT_BUFFER_OVERFLOW" in new:
            self.sink.event("device.events_lost", "medium", "The outstation's event buffer overflowed (IIN2.3): "
                            "some changes were lost; poll classes more often", device=DEVICE)
        if "DEVICE_TROUBLE" in new or "CONFIG_CORRUPT" in new:
            self.sink.event("device.trouble", "medium", f"The outstation reports {', '.join(sorted(new & {'DEVICE_TROUBLE', 'CONFIG_CORRUPT'}))}",
                            device=DEVICE)
        if not self._acq().get("unsolicited") and how != "integrity":
            ready = tuple(c for c in (1, 2, 3) if iin & A.IIN_CLASS[c])
            polls = self._acq().get("class_polls") or {}
            ready = tuple(c for c in ready if polls.get(f"class{c}_ms", 5000) > 0)
            if ready and how == "poll":
                await self._handle(await self.master.poll(ready), "events")   # events still waiting: fetch them

    # ---------------------------------------------------------------- admin operations
    async def _with_master(self, config: dict, fn):
        """Runs fn(master) on the running association when it is the same outstation, else on a short one."""
        if self._same_connection(config):
            return await fn(self.master)
        m = self._make_master(config)
        try:
            await m.connect()
            return await fn(m)
        finally:
            await m.close()

    async def test_connection(self, config: dict) -> dict:
        t0 = time.monotonic()
        entry: dict = {"id": DEVICE}
        try:
            frags = await self._with_master(config, lambda m: m.poll((0,)))
            counts: dict[str, int] = {}
            for f in frags:
                for o in f.objects:
                    if isinstance(o, A.Point):
                        counts[A.LABEL[o.kind]] = counts.get(A.LABEL[o.kind], 0) + 1
            conn = config["connection"]
            iin = frags[-1].iin_names() if frags else []
            entry.update(ok=True, info={"outstation": conn["outstation_address"], "master": conn.get("master_address", 1),
                                        "points": counts, "iin": iin or ["none"],
                                        "tls": bool((conn.get("tls") or {}).get("enabled"))})
        except ConnectorError as exc:
            entry.update(ok=False, error=exc.as_dict())
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return {"devices": [entry]}

    async def browse(self, args: dict) -> dict:
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        frags = await self._with_master(cfg, lambda m: m.poll((0,)))
        only = args.get("path") or None
        max_items = min(args.get("max_items", 2000), 5000)
        found: dict[str, list[A.Point]] = {}
        for f in frags:
            for o in f.objects:
                if isinstance(o, A.Point) and (only is None or o.kind == only.split("/")[0]):
                    found.setdefault(o.kind, []).append(o)
        items = []
        for kind in A.KINDS:
            pts = sorted(found.get(kind, []), key=lambda o: o.index)
            if not pts:
                continue
            items.append({"path": kind, "name": f"{A.LABEL[kind]}s", "kind": "object", "has_children": True,
                          "info": {"count": len(pts), "sample": f"{len(pts)} point(s), {pts[0].gv}"}})
            for o in pts:
                if len(items) >= max_items:
                    break
                dt = "bool" if kind in ("binary_input", "binary_output_status") else \
                    "enum" if kind == "double_bit_binary_input" else \
                    "int" if kind in ("counter", "frozen_counter") or (o.group, o.variation) in A.INTEGER_VARIATIONS else "float"
                sample = STATE_NAMES.get(o.value, o.value) if kind == "double_bit_binary_input" else \
                    ("on" if o.value else "off") if isinstance(o.value, bool) else o.value
                info = {"variation": o.gv, "sample": str(sample)[:40], "quality": A.quality(kind, o.flags)}
                if o.flags is not None:
                    info["flags"] = ", ".join(A.flag_names(kind, o.flags)) or "none"
                items.append({"path": f"{kind}/{o.index}", "name": f"{A.LABEL[kind]} {o.index}", "kind": "variable",
                              "has_children": False, "address": {"group": kind, "index": o.index}, "data_type": dt,
                              "info": info})
        return {"items": items}

    async def read_now(self, args: dict) -> dict:
        wanted: list[tuple[str | None, dict, str]] = []
        for t in args.get("tag_ids") or []:
            p = self.table.points.get(t) if self.table else None
            if p is None:
                raise ConnectorError("POINT_NOT_FOUND", f"no point {t}")
            wanted.append((t, p.address, p.data_type))
        for a in args.get("addresses") or []:
            wanted.append((None, a["address"], a.get("data_type", "float")))
        if len(wanted) > 100:
            raise ConnectorError("CFG_INVALID", "read_now takes at most 100 points")
        items = [(A.KINDS[a["group"]][0], a["index"], a["index"]) for _, a, _ in wanted]
        frags = await self._with_master(self.config, lambda m: m.read_points(items))
        got = {(o.kind, o.index): o for f in frags for o in f.objects if isinstance(o, A.Point)}
        now = iso(datetime.now(timezone.utc))
        out = []
        for tag, a, dt in wanted:
            o = got.get((a["group"], a["index"]))
            if o is None:
                v, q = None, Q.BAD_CONFIG_ERROR
            else:
                q = A.quality(o.kind, o.flags)
                try:
                    v = None if Q.is_bad(q) else coerce(o.kind, o.value, dt)
                except TypeError:
                    v, q = None, Q.BAD_CONFIG_ERROR
            entry = {"v": v, "q": q, "ts": now}
            entry.update({"t": tag} if tag else {"address": a})
            out.append(entry)
        return {"values": out}

    async def diagnostics(self) -> dict:
        m = self.master
        return {"connected": self.state == "connected", **self.stats, "iin": sorted(self.iin_seen),
                "last_integrity": self.last_integrity, "not_in_outstation": self.missing,
                "link": m.stats if m else {}}
