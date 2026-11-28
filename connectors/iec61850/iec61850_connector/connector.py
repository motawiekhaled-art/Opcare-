"""IEC 61850 MMS client connector (spec OSS-DAQ-CON-001 section 15.4), built on opcare_connector_sdk and our own
stack (ber, iso, mms, client, model): no third-party IEC 61850 or MMS library.

Reads protection relays, bay controllers, merging-unit gateways and station computers (SEL, Siemens SIPROTEC,
ABB Relion, GE UR/Multilin, Schneider Easergy MiCOM, NR Electric ...): buffered and unbuffered reports on datasets
the IED already has, general interrogation, and polling of values no report carries.

Read-only (spec 12 and 15.4): the only writes are to report-control-block attributes (Resv, ResvTms, RptEna, GI,
TrgOps, IntgPd, BufTm, OptFlds, EntryID); controls, setting groups, datasets, files and logs have no code.
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

from . import mms as M
from . import model as X
from .client import MmsClient

DEVICE = "ied"
ERROR_QUALITY = {2: 24, 0: 0, 1: 12}                        # temporarily-unavailable, invalidated, hardware fault


def tls_context(ca_pem, cert_pem, key_pem, insecure: bool) -> ssl.SSLContext:
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


class Iec61850Connector(Connector):
    type = "iec61850"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": True}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.client: MmsClient | None = None
        self.task: asyncio.Task | None = None
        self.state = "connecting"
        self.types: dict[tuple[str, str], M.TypeSpec] = {}
        self.cache: dict[tuple, object] = {}
        self.by_do: dict[tuple, list] = {}            # (ld, ln, fc, do path) -> [(point, Ref)]
        self.reports: dict[str, dict] = {}            # rpt id -> {rcb, members, buffered}
        self.entry_ids: dict[str, bytes] = {}         # rcb ref -> last EntryID (buffered resynchronisation)
        self.reported: set[tuple] = set()
        self.missing: list[str] = []
        self.stats = {"reads": 0, "reports": 0, "report_values": 0, "polls": 0, "reconnects": 0, "type_errors": 0,
                      "overflows": 0}
        self.rcb_state: dict[str, str] = {}
        self.report_seen: set[str] = set()             # report ids that delivered at least one report

    # ---------------------------------------------------------------- configuration
    def device_of(self, address: dict) -> str:
        return DEVICE

    def devices(self, config: dict) -> list[str]:
        return [DEVICE]

    async def validate(self, config: dict) -> list[str]:
        errs = []
        for p in config["points"]:
            try:
                X.parse_ref(p["address"]["object_ref"], p["address"]["fc"])
            except X.RefError as exc:
                errs.append(f"{p['tag_id']}: {exc}")
        for r in (config.get("acquisition") or {}).get("reports") or []:
            try:
                ld, item = X.parse_rcb(r["rcb_ref"])
                if (".BR." in r["rcb_ref"]) != r.get("buffered", True):
                    errs.append(f"{r['rcb_ref']}: 'buffered' must be {'true' if '.BR.' in r['rcb_ref'] else 'false'} for this control block")
            except X.RefError as exc:
                errs.append(str(exc))
        return errs

    def _acq(self, config: dict | None = None) -> dict:
        return (config or self.config).get("acquisition") or {}

    def _make_client(self, config: dict, on_report=None) -> MmsClient:
        conn = config["connection"]
        tls = conn.get("tls") or {}
        ctx = None
        if tls.get("enabled"):
            ctx = tls_context(self.secret(tls["ca_cert_ref"]) if tls.get("ca_cert_ref") else None,
                              self.secret(tls["client_cert_ref"]) if tls.get("client_cert_ref") else None,
                              self.secret(tls["client_key_ref"]) if tls.get("client_key_ref") else None,
                              tls.get("insecure_skip_verify", False))
        auth = conn.get("acse_auth") or {}
        return MmsClient(conn["host"], conn.get("port", 102), ReadOnlyGuard("iec61850", ALLOW["iec61850"], on_block=self._blocked),
                         conn.get("timeout_ms", 10000) / 1000, ctx, self.secret(auth["password_ref"]) if auth.get("password_ref") else None,
                         on_report)

    def _blocked(self, request: str) -> None:
        if self.sink:
            self.sink.event("guard.blocked", "critical", f"Blocked MMS {request}", attrs={"request": request})

    def _same_connection(self, config: dict) -> bool:
        return (self.client is not None and not self.client.closed.is_set() and self.state == "connected"
                and config.get("connection") == self.config.get("connection"))

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self.task = asyncio.create_task(self._run())

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        return False                                              # reports and datasets are set up again: reconnect

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
        while True:
            connected_at = None
            try:
                self.client = self._make_client(self.config, self._on_report)
                await self.client.connect()
                connected_at = time.monotonic()
                await self._session()
            except ConnectorError as exc:
                self._set_state("unreachable", exc)
            except M.MmsError as exc:
                self._set_state("unreachable", ConnectorError(exc.code, str(exc)))
            finally:
                if self.client:
                    await self.client.close()
            self.stats["reconnects"] += 1
            if connected_at is not None and time.monotonic() - connected_at > 60:
                delay = 1.0
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 60.0)

    def _set_state(self, state: str, exc: ConnectorError | None = None) -> None:
        if state != self.state:
            self.state = state
            self.sink.device_state(DEVICE, state, exc)

    async def _ln_type(self, client: MmsClient, ld: str, ln: str) -> M.TypeSpec | None:
        key = (ld, ln)
        if key not in self.types:
            try:
                self.types[key] = await client.type_of(ld, ln)
            except M.MmsError:
                self.types[key] = None
        return self.types[key]

    async def _session(self) -> None:
        c = self.client
        self.types, self.reports, self.reported, self.by_do, self.missing = {}, {}, set(), {}, []
        for p in self.table.points.values():
            ref = X.parse_ref(p.address["object_ref"], p.address["fc"])
            t = await self._ln_type(c, ref.ld, ref.ln)
            if t is None or X.subtype(t.component(ref.fc), ref.path) is None:
                self.missing.append(p.tag_id)
                self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, flags=["NOT_IN_IED"])
                continue
            do = X.data_object_path(t, ref)
            self.by_do.setdefault((ref.ld, ref.ln, ref.fc, do), []).append((p, ref))
        acq = self._acq()
        self.report_seen = set()
        gi = acq.get("gi_on_connect", True)
        for r in acq.get("reports") or []:
            await self._enable_report(c, r, gi)
        if gi and self.reports:                                  # a report that never comes is treated as absent
            deadline = time.monotonic() + c.timeout
            while time.monotonic() < deadline and not set(self.reports) <= self.report_seen:
                await asyncio.sleep(0.05)
        for rpt_id, info in self.reports.items():
            if gi and rpt_id not in self.report_seen:
                self.rcb_state[info["rcb"]] = "enabled, but the IED sent no report: its values are polled"
                self.sink.event("config.rejected", "medium", f"Report {info['rcb']} was enabled but the IED sent no "
                                "report after general interrogation; its values are polled instead", attrs={"rcb": info["rcb"]})
                continue
            for m in info["members"]:
                for key in self.by_do:
                    kref = X.Ref(*key)
                    if kref.under(m) or m.under(kref):
                        self.reported.add(key)
        self._set_state("connected")
        await self._read_dos(list(self.by_do))                     # first values for everything
        unreported = [k for k in self.by_do if k not in self.reported] if acq.get("poll_unreported", True) else []
        scan = acq.get("default_scan_ms", 1000) / 1000
        while True:
            try:
                await asyncio.wait_for(c.closed.wait(), scan if unreported else None)
            except asyncio.TimeoutError:
                t0 = time.monotonic()
                await self._read_dos(unreported)
                self.stats["polls"] += 1
                if time.monotonic() - t0 > scan:
                    self.sink.scan_overrun(int(scan * 1000))
                continue
            raise c.close_reason or ConnectorError("CONN_CLOSED", "association with the IED closed")

    async def _read_dos(self, keys: list[tuple]) -> None:
        if not keys:
            return
        values = await self.client.read([(ld, "$".join((ln, fc) + do)) for ld, ln, fc, do in keys])
        self.stats["reads"] += 1
        self.sink.device_ok(DEVICE)
        for key, v in zip(keys, values):
            ld, ln, fc, do = key
            if isinstance(v, M.DataError):
                for p, _ in self.by_do.get(key, []):
                    self.sink.value(p.tag_id, None, ERROR_QUALITY.get(v.code, Q.BAD_CONFIG_ERROR), flags=[v.name])
                continue
            spec = X.subtype(self.types[(ld, ln)].component(fc), do)
            for path, leaf in X.flatten(v, spec, do).items():
                self.cache[(ld, ln, fc, path)] = leaf
            self._publish(key)

    def _publish(self, key: tuple, touched: set | None = None) -> None:
        ld, ln, fc, do = key
        q_raw = self.cache.get((ld, ln, fc, do + ("q",)))
        t_raw = self.cache.get((ld, ln, fc, do + ("t",)))
        q, qflags = X.quality(q_raw) if q_raw is not None else (192, [])
        ts, tflags = X.timestamp(t_raw)
        quality_touched = touched is None or (ld, ln, fc, do + ("q",)) in touched or (ld, ln, fc, do + ("t",)) in touched
        for p, ref in self.by_do.get(key, []):
            k = (ld, ln, fc, ref.path)
            if touched is not None and k not in touched and not quality_touched:
                continue
            if k not in self.cache:
                continue
            flags = qflags + tflags
            if Q.is_bad(q):
                self.sink.value(p.tag_id, None, q, ts=ts, flags=flags)
                continue
            try:
                v = X.coerce(self.cache[k], p.data_type)
            except TypeError:
                self.stats["type_errors"] += 1
                self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, ts=ts, flags=["TYPE_MISMATCH"])
                continue
            self.sink.value(p.tag_id, v, q, ts=ts, flags=flags)

    # ---------------------------------------------------------------- reports
    async def _enable_report(self, c: MmsClient, r: dict, gi: bool) -> None:
        ref = r["rcb_ref"]
        ld, item = X.parse_rcb(ref)
        try:
            rtype = await c.type_of(ld, item)
            cur = dict(zip([n for n, _ in rtype.components], (await c.read([(ld, item)]))[0]))
        except (M.MmsError, TypeError):
            self.rcb_state[ref] = "not found"
            self.sink.event("config.rejected", "medium", f"Report control block {ref} was not found in the IED; "
                            "its values are polled instead", attrs={"rcb": ref})
            return
        buffered = ".BR." in ref
        datset = str(cur.get("DatSet") or "").replace("$", "$")
        if "/" not in datset:
            self.rcb_state[ref] = "no dataset"
            return
        ds_ld, ds_item = datset.split("/", 1)
        members = [X.ref_of_item(d or ld, i) for d, i in await c.dataset(ds_ld, ds_item)]
        for m in members:
            await self._ln_type(c, m.ld, m.ln)
        spec = {n: s for n, s in rtype.components}
        rpt_id = str(cur.get("RptID") or "") or f"{ld}/{item.replace('$', '.')}"
        # registered before enabling: the general-interrogation report can arrive before the write is answered
        self.reports[rpt_id] = {"rcb": ref, "members": members, "buffered": buffered, "dataset": datset}

        async def put(attr: str, value) -> None:
            if attr not in spec:
                return
            err = await c.write_rcb(ld, f"{item}${attr}", M.encode_data(value, spec[attr]))
            if err is not None:
                raise M.MmsError(f"{ref}.{attr}: {err.name}", "OP_FORBIDDEN" if err.code == 3 else "PROTOCOL_ERROR")

        try:
            if cur.get("RptEna"):
                await put("RptEna", False)                         # owned by us from a previous association
            if r.get("reserve", True):
                if "ResvTms" in spec and buffered:
                    await put("ResvTms", 60)
                elif "Resv" in spec:
                    await put("Resv", True)
            opts = ["seqNum", "timeStamp", "reasonCode", "dataSet", "confRev"] + (["bufOvfl", "entryID"] if buffered else [])
            await put("OptFlds", X.bits(opts, X.OPT, 10))
            trg = r.get("trigger_options") or ["dchg", "qchg", "gi"]
            await put("TrgOps", X.bits(trg, X.TRG, 6))
            if r.get("integrity_period_ms") is not None:
                await put("IntgPd", int(r["integrity_period_ms"]))
            if r.get("buf_time_ms") is not None:
                await put("BufTm", int(r["buf_time_ms"]))
            if buffered and self.entry_ids.get(ref):
                try:
                    await put("EntryID", self.entry_ids[ref])          # resynchronise: send what we missed
                except M.MmsError:
                    pass                                           # entry no longer in the buffer: GI covers it
            await put("RptEna", True)
            if gi:
                await put("GI", True)
        except M.MmsError as exc:
            self.reports.pop(rpt_id, None)
            self.rcb_state[ref] = str(exc)
            self.sink.event("config.rejected", "medium", f"Report {ref} could not be enabled ({exc}); its values are "
                            "polled instead", attrs={"rcb": ref})
            return
        self.rcb_state[ref] = "enabled"

    def _on_report(self, name: str | None, values: list) -> None:
        try:
            rep = X.parse_report(values, None)
        except (ValueError, IndexError):
            return
        info = self.reports.get(rep.rpt_id)
        if info is None:
            return
        self.stats["reports"] += 1
        self.report_seen.add(rep.rpt_id)
        if rep.entry_id:
            self.entry_ids[info["rcb"]] = rep.entry_id
        if rep.overflow:
            self.stats["overflows"] += 1
            self.sink.event("device.events_lost", "medium", f"The IED's report buffer of {info['rcb']} overflowed: "
                            "some changes were lost", device=DEVICE)
        touched_by_do: dict[tuple, set] = {}
        for idx, value, _reasons in rep.entries:
            if idx >= len(info["members"]) or isinstance(value, M.DataError):
                continue
            m = info["members"][idx]
            t = self.types.get((m.ld, m.ln))
            spec = X.subtype(t.component(m.fc), m.path) if t else None
            for path, leaf in X.flatten(value, spec, m.path).items():
                k = (m.ld, m.ln, m.fc, path)
                self.cache[k] = leaf
                self.stats["report_values"] += 1
                for key in self.by_do:
                    if key[:3] == k[:3] and path[:len(key[3])] == key[3]:
                        touched_by_do.setdefault(key, set()).add(k)
        for key, touched in touched_by_do.items():
            self._publish(key, touched)
        if self.sink:
            self.sink.device_ok(DEVICE)

    # ---------------------------------------------------------------- admin operations
    async def _with_client(self, config: dict, fn):
        if self._same_connection(config):
            return await fn(self.client)
        c = self._make_client(config)
        await c.connect()
        try:
            return await fn(c)
        finally:
            await c.close()

    async def test_connection(self, config: dict) -> dict:
        t0 = time.monotonic()
        entry: dict = {"id": DEVICE}

        async def probe(c: MmsClient):
            lds = await c.get_name_list(M.CLASS_DOMAIN)
            info = {"logical_devices": lds}
            ied = config["connection"].get("ied_name")
            if ied and lds and not any(ld.startswith(ied) for ld in lds):
                raise ConnectorError("ADDR_INVALID", f"this server is not IED '{ied}': its logical devices are {', '.join(lds[:6])}")
            if lds:
                try:
                    v = await c.read([(lds[0], "LLN0$DC$NamPlt$vendor"), (lds[0], "LLN0$DC$NamPlt$swRev")])
                    if isinstance(v[0], str):
                        info["vendor"] = v[0]
                    if isinstance(v[1], str):
                        info["software"] = v[1]
                except (M.MmsError, ConnectorError):
                    pass
                names = await c.get_name_list(M.CLASS_NAMED_VARIABLE, lds[0])
                info["report_control_blocks"] = len([n for n in names if n.count("$") == 2 and n.split("$")[1] in ("BR", "RP")])
                info["datasets"] = len(await c.get_name_list(M.CLASS_NAMED_VARIABLE_LIST, lds[0]))
            return info
        try:
            entry.update(ok=True, info=await self._with_client(config, probe))
        except ConnectorError as exc:
            entry.update(ok=False, error=exc.as_dict())
        except M.MmsError as exc:
            entry.update(ok=False, error={"code": exc.code, "message": str(exc)})
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return {"devices": [entry]}

    async def browse(self, args: dict) -> dict:
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        path = (args.get("path") or "").strip()
        max_items = min(args.get("max_items", 1000), 5000)
        try:
            return {"items": (await self._with_client(cfg, lambda c: self._browse(c, path)))[:max_items]}
        except M.MmsError as exc:
            raise ConnectorError(exc.code, str(exc)) from None

    async def _browse(self, c: MmsClient, path: str) -> list[dict]:
        if not path:
            return [{"path": ld, "name": ld, "kind": "folder", "has_children": True, "info": {"node": "logical device"}}
                    for ld in await c.get_name_list(M.CLASS_DOMAIN)]
        if "/" not in path:
            names = await c.get_name_list(M.CLASS_NAMED_VARIABLE, path)
            lns = sorted({n.split("$")[0] for n in names}, key=lambda x: (x != "LLN0", x))
            return [{"path": f"{path}/{ln}", "name": ln, "kind": "object", "has_children": True, "info": {"node": "logical node"}}
                    for ln in lns]
        ld, rest = path.split("/", 1)
        ln, *do = rest.split(".")
        t = await c.type_of(ld, ln)
        if not do:
            items, seen = [], {}
            for fc, fspec in t.components:
                if fc in X.READ_FCS:
                    for name, _ in fspec.components:
                        seen.setdefault(name, []).append(fc)
                elif fc in ("BR", "RP"):
                    for name, _ in fspec.components:
                        items.append(await self._rcb_item(c, ld, ln, fc, name))
            items = [{"path": f"{ld}/{ln}.{n}", "name": n, "kind": "object", "has_children": True,
                      "info": {"node": "data object", "fc": "/".join(fcs)}} for n, fcs in seen.items()] + items
            return items
        out = []
        for fc, fspec in t.components:
            if fc not in X.READ_FCS:
                continue
            sub = X.subtype(fspec, tuple(do))
            if sub is None:
                continue
            value = (await c.read([(ld, "$".join([ln, fc] + do))]))[0]
            flat = X.flatten(value, sub, tuple(do))
            for lpath, lspec in X.leaves(sub, tuple(do)):
                dt = X.data_type_of(lspec)
                if dt is None or lpath[-1] in ("q", "t"):
                    continue
                ref = f"{ld}/{ln}." + ".".join(lpath)
                q_raw = flat.get(X.data_object_path(t, X.Ref(ld, ln, fc, lpath)) + ("q",))
                item = {"path": f"{ref}#{fc}", "name": ".".join(lpath[len(do):]) or lpath[-1], "kind": "variable",
                        "has_children": False, "address": {"object_ref": ref, "fc": fc}, "data_type": dt,
                        "info": {"fc": fc, "type": lspec.kind + (f"({lspec.size})" if lspec.size else ""),
                                 "sample": X.sample(flat.get(lpath))}}
                if q_raw is not None:
                    item["info"]["quality"] = X.quality(q_raw)[0]
                out.append(item)
        return out

    async def _rcb_item(self, c: MmsClient, ld: str, ln: str, fc: str, name: str) -> dict:
        item = f"{ln}${fc}${name}"
        info = {"node": "report control block", "buffered": fc == "BR"}
        try:
            rtype = await c.type_of(ld, item)
            cur = dict(zip([n for n, _ in rtype.components], (await c.read([(ld, item)]))[0]))
            info.update(dataset=str(cur.get("DatSet") or ""), enabled=bool(cur.get("RptEna")), rpt_id=str(cur.get("RptID") or ""))
            ds = info["dataset"]
            if "/" in ds:
                info["members"] = [f"{d}/{i.replace('$', '.')}" for d, i in await c.dataset(*ds.split("/", 1))]
        except (M.MmsError, TypeError):
            pass
        return {"path": f"{ld}/{ln}.{fc}.{name}", "name": name, "kind": "report", "has_children": False, "info": info,
                "report": {"rcb_ref": f"{ld}/{ln}.{fc}.{name}", "buffered": fc == "BR"}}

    async def read_now(self, args: dict) -> dict:
        wanted: list[tuple[str | None, X.Ref, str]] = []
        for tag in args.get("tag_ids") or []:
            p = self.table.points.get(tag) if self.table else None
            if p is None:
                raise ConnectorError("POINT_NOT_FOUND", f"no point {tag}")
            wanted.append((tag, X.parse_ref(p.address["object_ref"], p.address["fc"]), p.data_type))
        for a in args.get("addresses") or []:
            wanted.append((None, X.parse_ref(a["address"]["object_ref"], a["address"]["fc"]), a.get("data_type", "float")))
        if len(wanted) > 100:
            raise ConnectorError("CFG_INVALID", "read_now takes at most 100 points")

        async def go(c: MmsClient):
            out = []
            now = X.iso(datetime.now(timezone.utc))
            for tag, ref, dt in wanted:
                t = await c.type_of(ref.ld, ref.ln) if (ref.ld, ref.ln) not in self.types or self.types[(ref.ld, ref.ln)] is None else self.types[(ref.ld, ref.ln)]
                do = X.data_object_path(t, ref)
                v = (await c.read([(ref.ld, "$".join((ref.ln, ref.fc) + do))]))[0]
                entry: dict = {"ts": now}
                if isinstance(v, M.DataError):
                    entry.update(v=None, q=ERROR_QUALITY.get(v.code, Q.BAD_CONFIG_ERROR))
                else:
                    flat = X.flatten(v, X.subtype(t.component(ref.fc), do), do)
                    q = X.quality(flat[do + ("q",)])[0] if do + ("q",) in flat else 192
                    try:
                        entry.update(v=None if Q.is_bad(q) else X.coerce(flat[ref.path], dt), q=q)
                    except (TypeError, KeyError):
                        entry.update(v=None, q=Q.BAD_CONFIG_ERROR)
                    ts, _ = X.timestamp(flat.get(do + ("t",)))
                    entry["ts"] = ts or now
                entry.update({"t": tag} if tag else {"address": {"object_ref": ref.object_ref, "fc": ref.fc}})
                out.append(entry)
            return out
        try:
            return {"values": await self._with_client(self.config, go)}
        except M.MmsError as exc:
            raise ConnectorError(exc.code, str(exc)) from None

    async def diagnostics(self) -> dict:
        c = self.client
        return {"connected": self.state == "connected", **self.stats, "reports": self.rcb_state,
                "reported_objects": len(self.reported), "polled_objects": len(self.by_do) - len(self.reported),
                "not_in_ied": self.missing, "mms": c.stats if c else {}}
