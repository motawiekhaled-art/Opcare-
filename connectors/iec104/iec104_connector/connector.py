"""IEC 60870-5-104 master connector (spec OSS-DAQ-CON-001 section 15.5), built on opcare_connector_sdk and our own
IEC 104 stack (apci.py, asdu.py, client.py): no third-party IEC 104 library, so no GPL code (spec 2 rule 7).

Reads RTUs, substation gateways, protection relays and SCADA front ends in power grids, renewables, water and oil &
gas (ABB RTU500, Siemens SICAM, GE D20/D400, Schneider Saitel/Easergy, Hitachi Energy, Efacec, NARI, NR Electric,
Triangle MicroWorks and Kalkitech gateways ...): general interrogation on connect and periodically, spontaneous
data with the station's CP56Time2a time tags, counter interrogation and the read command.

Read-only (spec 12 and 15.5): the stack only builds STARTDT/STOPDT/TESTFR, S-frames, general interrogation, counter
interrogation without freeze or reset, and the read command. Clock synchronisation does not exist in the code.
"""
from __future__ import annotations

import asyncio
import os
import random
import ssl
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, PointTable, Q, ReadOnlyGuard

from . import asdu as A
from .client import Iec104Client

DEVICE = "station"
DATA_TYPES = {"SP": ("bool", "int", "string"), "DP": ("enum", "int", "string"), "ST": ("int", "float", "string"),
              "BO": ("int", "string"), "NA": ("float", "string"), "NB": ("int", "float", "string"),
              "NC": ("float", "int", "string"), "IT": ("int", "float", "string")}
DEFAULT_TYPE = {"SP": "bool", "DP": "enum", "ST": "int", "BO": "int", "NA": "float", "NB": "float", "NC": "float",
                "IT": "int"}


def zone(name: str | None):
    """IANA time zone; UTC needs no time zone database (Windows has none unless the tzdata package is installed)."""
    if name in (None, "", "UTC", "Etc/UTC", "Z"):
        return timezone.utc
    return ZoneInfo(name)


def iso(dt: datetime | None) -> str | None:
    if dt is None or dt.year < 1990:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def coerce(fam: str, value, data_type: str):
    """Value of an information object -> configured data type. Raises TypeError when it does not fit (spec 15.5)."""
    if data_type == "string":
        if fam == "DP":
            return A.DPI_NAMES[value]
        return ("on" if value else "off") if fam == "SP" else str(value)
    if data_type not in DATA_TYPES[fam]:
        raise TypeError(f"a {A.LABEL[fam]} is not '{data_type}'")
    if data_type == "bool":
        return bool(value)
    if data_type in ("int", "enum"):
        if isinstance(value, float) and not value.is_integer():
            raise TypeError("value is not a whole number")
        return int(value)
    return float(value)


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


class Iec104Connector(Connector):
    type = "iec104"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": True}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.client: Iec104Client | None = None
        self.task: asyncio.Task | None = None
        self.state = "connecting"
        self.by_address: dict[tuple[int, int], list] = {}
        self.host_in_use: str | None = None
        self.stats = {"interrogations": 0, "counter_interrogations": 0, "spontaneous": 0, "values": 0,
                      "restarts": 0, "reconnects": 0, "failovers": 0, "type_errors": 0, "test_mode": 0}
        self.last_gi: str | None = None
        self.missing: list[str] = []
        self.rejected_cas: set[int] = set()                  # general interrogation rejected (unknown CA)
        self.ci_rejected: set[int] = set()                   # counter interrogation rejected (not supported)
        self._wake = asyncio.Event()

    # ---------------------------------------------------------------- configuration
    def device_of(self, address: dict) -> str:
        return DEVICE

    def devices(self, config: dict) -> list[str]:
        return [DEVICE]

    async def validate(self, config: dict) -> list[str]:
        errs = []
        conn = config["connection"]
        cas = set(conn["common_addresses"])
        if len(cas) != len(conn["common_addresses"]):
            errs.append("connection/common_addresses: a common address is listed twice")
        if conn.get("w", 8) > conn.get("k", 12):
            errs.append("connection: w must not be larger than k (IEC 60870-5-104 recommends w <= 2/3 k)")
        if conn.get("t1_s", 15) >= conn.get("t3_s", 20) or conn.get("t2_s", 10) >= conn.get("t1_s", 15):
            errs.append("connection: the timers must satisfy t2 < t1 < t3")
        try:
            zone(conn.get("device_time_zone", "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            errs.append(f"connection/device_time_zone: '{conn.get('device_time_zone')}' is not an IANA time zone "
                        "(or this computer has no time zone database: install the tzdata package)")
        tls = conn.get("tls") or {}
        if tls.get("enabled") and bool(tls.get("client_cert_ref")) != bool(tls.get("client_key_ref")):
            errs.append("connection/tls: client_cert_ref and client_key_ref go together")
        seen = {}
        for p in config["points"]:
            a = p["address"]
            key = (a["ca"], a["ioa"])
            if a["ca"] not in cas:
                errs.append(f"{p['tag_id']}: common address {a['ca']} is not in connection/common_addresses")
            fam = A.family(a["type"]) if a.get("type") else None
            if fam and p["data_type"] not in DATA_TYPES[fam]:
                errs.append(f"{p['tag_id']}: a {a['type']} ({A.LABEL[fam]}) cannot be '{p['data_type']}' "
                            f"(use {' or '.join(DATA_TYPES[fam])})")
            if key in seen:
                errs.append(f"{p['tag_id']}: CA {key[0]} IOA {key[1]} is already tag {seen[key]}")
            seen[key] = p["tag_id"]
        return errs

    def _acq(self, config: dict | None = None) -> dict:
        return (config or self.config).get("acquisition") or {}

    def _make_client(self, config: dict, host: dict, on_asdu=None) -> Iec104Client:
        conn = config["connection"]
        tls = conn.get("tls") or {}
        ctx = None
        if tls.get("enabled"):
            ctx = tls_context(self.secret(tls["ca_cert_ref"]) if tls.get("ca_cert_ref") else None,
                              self.secret(tls["client_cert_ref"]) if tls.get("client_cert_ref") else None,
                              self.secret(tls["client_key_ref"]) if tls.get("client_key_ref") else None,
                              tls.get("insecure_skip_verify", False))
        return Iec104Client(host["host"], host.get("port", 2404),
                            ReadOnlyGuard("iec104", ALLOW["iec104"], on_block=self._blocked),
                            k=conn.get("k", 12), w=conn.get("w", 8), t0=conn.get("t0_s", 30), t1=conn.get("t1_s", 15),
                            t2=conn.get("t2_s", 10), t3=conn.get("t3_s", 20), oa=conn.get("originator_address", 0),
                            tls=ctx, tz=zone(conn.get("device_time_zone", "UTC")), on_asdu=on_asdu)

    def _blocked(self, request: str) -> None:
        if self.sink:
            self.sink.event("guard.blocked", "critical", f"Blocked IEC 104 {request}", attrs={"request": request})

    def _same_connection(self, config: dict) -> bool:
        """True when config talks to the station we are connected to (stations often accept one master per
        redundancy group, so tests, browse and reads reuse the running connection)."""
        return (self.client is not None and not self.client.closed.is_set() and self.state == "connected"
                and config.get("connection") == self.config.get("connection"))

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self._index()
        self.task = asyncio.create_task(self._run())

    def _index(self) -> None:
        self.by_address = {}
        for p in self.table.points.values():
            self.by_address.setdefault((p.address["ca"], p.address["ioa"]), []).append(p)

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        if config.get("connection") != self.config.get("connection") or self._acq(config) != self._acq():
            return False
        self.config, self.table = config, table
        self._index()
        self._wake.set()                                       # interrogate so new points get values now
        return True

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client:
            await self.client.close()
            self.client = None

    async def _run(self) -> None:
        delay = 1.0
        hosts = self.config["connection"]["hosts"]
        while True:
            connected_at = None
            last_exc: ConnectorError | None = None
            for i, host in enumerate(hosts):                   # second host: only when the first fails (spec 15.5)
                try:
                    self.client = self._make_client(self.config, host, self._on_asdu)
                    await self.client.connect()
                except ConnectorError as exc:
                    last_exc = exc
                    continue
                if i:
                    self.stats["failovers"] += 1
                    self.sink.event("conn.failover", "medium", f"Using the redundant link {host['host']}:"
                                    f"{host.get('port', 2404)}: the first link does not answer", device=DEVICE)
                self.host_in_use = f"{host['host']}:{host.get('port', 2404)}"
                connected_at = time.monotonic()
                try:
                    await self._session()
                except ConnectorError as exc:
                    last_exc = exc
                finally:
                    await self.client.close()
                break
            self._set_state("unreachable", last_exc or ConnectorError("CONN_CLOSED", "connection closed"))
            self.host_in_use = None
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
        c = self.client
        gi_s = acq.get("gi_period_s", 900)
        ci_s = acq.get("counter_interrogation_period_s", 0)
        if acq.get("gi_on_connect", True):
            await self._interrogate_all()
        self._set_state("connected")
        if ci_s:
            await self._interrogate_all(counters=True)
        now = time.monotonic()
        next_gi = now + gi_s if gi_s else None
        next_ci = now + ci_s if ci_s else None
        while True:
            now = time.monotonic()
            soonest = min([t for t in (next_gi, next_ci) if t] or [now + 60])
            closed = asyncio.ensure_future(c.closed.wait())
            woken = asyncio.ensure_future(self._wake.wait())
            await asyncio.wait({closed, woken}, timeout=max(0.0, soonest - now), return_when=asyncio.FIRST_COMPLETED)
            closed.cancel()
            woken.cancel()
            if c.closed.is_set():
                raise c.close_reason or ConnectorError("CONN_CLOSED", "connection to the station closed")
            now = time.monotonic()
            if self._wake.is_set() or (next_gi and now >= next_gi):
                self._wake.clear()
                await self._interrogate_all()
                next_gi = time.monotonic() + gi_s if gi_s else None
            if next_ci and time.monotonic() >= next_ci:
                await self._interrogate_all(counters=True)
                next_ci = time.monotonic() + ci_s

    async def _interrogate_all(self, counters: bool = False) -> None:
        seen: set[tuple[int, int]] = set()
        for ca in self.config["connection"]["common_addresses"]:
            try:
                for a in await self.client.interrogate(ca, counters=counters):
                    seen.update((a.ca, o.ioa) for o in a.objects)
            except ConnectorError as exc:
                if exc.code != "ADDR_INVALID":
                    raise
                rejected = self.ci_rejected if counters else self.rejected_cas
                if ca not in rejected:                        # one event per common address, not per period
                    rejected.add(ca)
                    self.sink.event("config.rejected", "medium", exc.message, device=DEVICE,
                                    attrs={"ca": ca, "request": "C_CI_NA_1" if counters else "C_IC_NA_1"})
                continue
            (self.ci_rejected if counters else self.rejected_cas).discard(ca)
        self.sink.device_ok(DEVICE)
        if counters:
            self.stats["counter_interrogations"] += 1
            return
        self.stats["interrogations"] += 1
        self.last_gi = iso(datetime.now(timezone.utc))
        self.missing = []
        for key, pts in self.by_address.items():               # configured but not in the station's answer
            if key in seen:
                continue
            for p in pts:
                if A.family(p.address.get("type", "")) == "IT":
                    continue                                   # counters come with counter interrogation only
                self.missing.append(p.tag_id)
                flag = "UNKNOWN_CA" if key[0] in self.rejected_cas else "NOT_IN_STATION"
                self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, flags=[flag])

    def _on_asdu(self, a: A.Asdu) -> None:
        if a.type_id == A.M_EI_NA_1 and a.cot == A.INITIALISED:
            self.stats["restarts"] += 1
            self.sink.event("device.restart", "low", f"The station reports end of initialisation (common address "
                            f"{a.ca}): interrogating it again", device=DEVICE, attrs={"ca": a.ca})
            self._wake.set()
            return
        if not a.objects:
            return
        if a.cot == A.SPONTANEOUS:
            self.stats["spontaneous"] += 1
        if a.test:
            self.stats["test_mode"] += 1
        for o in a.objects:
            self._report(a, o)

    def _report(self, a: A.Asdu, o: A.Obj) -> None:
        pts = self.by_address.get((a.ca, o.ioa))
        if not pts:
            return
        q, flags = A.quality(o.family, o.qual, o.transient)
        ts = None
        if o.time is not None or o.time_invalid:
            ts = None if o.time_invalid else iso(o.time)
            if ts is None:
                flags.append("CLOCK")
        if a.test:                                            # T bit: test frames, not process data
            flags.append("TEST")
            q = min(q, Q.UNCERTAIN) if not Q.is_bad(q) else q
        for p in pts:
            self.stats["values"] += 1
            want = A.family(p.address.get("type", "")) if p.address.get("type") else None
            if want and want != o.family:
                self.stats["type_errors"] += 1
                self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, ts=ts, flags=["TYPE_MISMATCH"])
                continue
            if Q.is_bad(q):
                self.sink.value(p.tag_id, None, q, ts=ts, flags=flags)
                continue
            try:
                v = coerce(o.family, o.value, p.data_type)
            except TypeError:
                self.stats["type_errors"] += 1
                self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, ts=ts, flags=["TYPE_MISMATCH"])
                continue
            self.sink.value(p.tag_id, v, q, ts=ts, flags=flags)

    # ---------------------------------------------------------------- admin operations
    async def _with_client(self, config: dict, fn):
        """Runs fn(client) on the running connection when it is the same station, else on a short one (first host
        that answers)."""
        if self._same_connection(config):
            return await fn(self.client)
        last: ConnectorError | None = None
        for host in config["connection"]["hosts"]:
            c = self._make_client(config, host)
            try:
                await c.connect()
            except ConnectorError as exc:
                last = exc
                continue
            try:
                return await fn(c)
            finally:
                await c.close()
        raise last or ConnectorError("CONN_CLOSED", "no host to connect to")

    async def _station_objects(self, config: dict, counters: bool, only_ca: int | None = None):
        """GI (and counter interrogation) of every common address -> ({ca: {ioa: Obj}}, rejected CAs, host)."""
        async def fn(c: Iec104Client):
            found: dict[int, dict[int, A.Obj]] = {}
            rejected = []
            for ca in config["connection"]["common_addresses"]:
                if only_ca is not None and ca != only_ca:
                    continue
                for kind in ((False, True) if counters else (False,)):
                    try:
                        for a in await c.interrogate(ca, counters=kind):
                            for o in a.objects:
                                found.setdefault(ca, {})[o.ioa] = o
                    except ConnectorError as exc:
                        if exc.code != "ADDR_INVALID":
                            raise
                        if not kind:
                            rejected.append(ca)
                            break
            return found, rejected, f"{c.host}:{c.port}"
        return await self._with_client(config, fn)

    async def test_connection(self, config: dict) -> dict:
        t0 = time.monotonic()
        entry: dict = {"id": DEVICE}
        try:
            found, rejected, host = await self._station_objects(config, counters=False)
            if rejected:
                raise ConnectorError("ADDR_INVALID", f"the station does not know common address(es) "
                                                     f"{', '.join(map(str, rejected))}")
            counts: dict[str, int] = {}
            for objs in found.values():
                for o in objs.values():
                    counts[A.LABEL[o.family]] = counts.get(A.LABEL[o.family], 0) + 1
            conn = config["connection"]
            entry.update(ok=True, info={"host": host, "common_addresses": conn["common_addresses"],
                                        "objects": counts or {"none": 0},
                                        "tls": bool((conn.get("tls") or {}).get("enabled"))})
        except ConnectorError as exc:
            entry.update(ok=False, error=exc.as_dict())
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return {"devices": [entry]}

    async def browse(self, args: dict) -> dict:
        """Lists what the station returns to general and counter interrogation (spec 15.5, draft.4). path: '' (all),
        'CA' or 'CA/FAMILY' (e.g. '1/NC')."""
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        parts = [x for x in str(args.get("path") or "").split("/") if x]
        try:
            only_ca = int(parts[0]) if parts else None
        except ValueError:
            raise ConnectorError("ADDR_INVALID", f"'{parts[0]}' is not a common address") from None
        only_fam = parts[1] if len(parts) > 1 else None
        found, rejected, _ = await self._station_objects(cfg, counters=True, only_ca=only_ca)
        max_items = min(args.get("max_items", 2000), 5000)
        items = []
        for ca in sorted(found):
            by_fam: dict[str, list[A.Obj]] = {}
            for o in found[ca].values():
                by_fam.setdefault(o.family, []).append(o)
            for fam in A.FAMILIES:
                objs = sorted(by_fam.get(fam, []), key=lambda o: o.ioa)
                if not objs or (only_fam and fam != only_fam):
                    continue
                items.append({"path": f"{ca}/{fam}", "name": f"CA {ca} · {A.LABEL[fam]}s", "kind": "object",
                              "has_children": True,
                              "info": {"count": len(objs), "type": A.TYPES[A.FAMILY_TYPE[fam]],
                                       "sample": f"{len(objs)} object(s), IOA {objs[0].ioa}…{objs[-1].ioa}"}})
                for o in objs:
                    if len(items) >= max_items:
                        break
                    q, flags = A.quality(fam, o.qual, o.transient)
                    sample = A.DPI_NAMES[o.value] if fam == "DP" else ("on" if o.value else "off") if fam == "SP" else o.value
                    info = {"type": A.TYPES[A.FAMILY_TYPE[fam]], "sample": str(sample)[:40], "quality": q}
                    if flags:
                        info["flags"] = ", ".join(flags)
                    items.append({"path": f"{ca}/{o.ioa}", "name": f"IOA {o.ioa}", "kind": "variable",
                                  "has_children": False,
                                  "address": {"ca": ca, "ioa": o.ioa, "type": A.TYPES[A.FAMILY_TYPE[fam]]},
                                  "data_type": DEFAULT_TYPE[fam], "info": info})
        for ca in rejected:
            items.append({"path": str(ca), "name": f"CA {ca} · unknown to the station", "kind": "object",
                          "has_children": False, "info": {"error": "the station rejected interrogation"}})
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

        async def fn(c: Iec104Client):
            return [await c.read(a["ca"], a["ioa"]) for _, a, _ in wanted]
        answers = await self._with_client(self.config, fn)
        now = iso(datetime.now(timezone.utc))
        out = []
        for (tag, a, dt), ans in zip(wanted, answers):
            o = next((x for x in (ans.objects if ans else []) if x.ioa == a["ioa"]), None)
            entry: dict = {"ts": now}
            if o is None:                                     # no answer, or negative (unknown IOA, no read support)
                entry.update(v=None, q=Q.BAD_CONFIG_ERROR if ans is not None else Q.BAD_COMM_FAILURE)
            else:
                q, _ = A.quality(o.family, o.qual, o.transient)
                try:
                    v = None if Q.is_bad(q) else coerce(o.family, o.value, dt)
                except TypeError:
                    v, q = None, Q.BAD_CONFIG_ERROR
                entry.update(v=v, q=q)
                if o.time is not None:
                    entry["ts"] = iso(o.time) or now
            entry.update({"t": tag} if tag else {"address": a})
            out.append(entry)
        return {"values": out}

    async def diagnostics(self) -> dict:
        c = self.client
        return {"connected": self.state == "connected", "host": self.host_in_use, **self.stats,
                "last_interrogation": self.last_gi, "not_in_station": self.missing,
                "rejected_common_addresses": sorted(self.rejected_cas),
                "counter_interrogation_rejected": sorted(self.ci_rejected),
                "link": ({**c.stats, "vs": c.vs, "vr": c.vr, "unacknowledged": len(c.sent)} if c else {})}
