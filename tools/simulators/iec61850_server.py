"""Small IEC 61850 MMS server for tests and the demo, built from the connector's own stack in the server role and an
SCL file (ICD/CID) for its data model.

Serves what a read-only client uses: Initiate (optional ACSE password), GetNameList, GetVariableAccessAttributes,
GetNamedVariableListAttributes, Read, Write of report-control-block attributes, unbuffered and buffered reports
(data change, quality change, general interrogation, integrity period, EntryID resynchronisation), Conclude.
Every other write is refused with object-access-denied; every other service with a confirmed error.

It records each service a client used in `requests` (Write as WRITE_RCB:<attr> or Write:<item>), so tests can prove
the client sends nothing outside the read-only allow-list.

    srv = Iec61850Server("tools/simulators/scl/feeder_relay.icd")
    await srv.start()                                            # srv.port
    srv.set("FDR21LD0/MMXU1.TotW.mag.f", "MX", 3.2)               # reports go out if a dataset holds it
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "connectors" / "iec61850"))

from iec61850_connector import ber as B  # noqa: E402
from iec61850_connector import mms as M  # noqa: E402
from iec61850_connector.iso import IsoError, IsoLink  # noqa: E402

NS = {"s": "http://www.iec.ch/61850/2003/SCL"}
BTYPES = {"BOOLEAN": M.TypeSpec("boolean"), "INT8": M.TypeSpec("integer", 8), "INT16": M.TypeSpec("integer", 16),
          "INT32": M.TypeSpec("integer", 32), "INT64": M.TypeSpec("integer", 64), "INT8U": M.TypeSpec("unsigned", 8),
          "INT16U": M.TypeSpec("unsigned", 16), "INT32U": M.TypeSpec("unsigned", 32), "FLOAT32": M.TypeSpec("floating-point", 32),
          "FLOAT64": M.TypeSpec("floating-point", 64), "Enum": M.TypeSpec("integer", 8), "Dbpos": M.TypeSpec("bit-string", 2),
          "Tcmd": M.TypeSpec("bit-string", 2), "Quality": M.TypeSpec("bit-string", 13), "Timestamp": M.TypeSpec("utc-time"),
          "VisString32": M.TypeSpec("visible-string", 32), "VisString64": M.TypeSpec("visible-string", 64),
          "VisString129": M.TypeSpec("visible-string", 129), "VisString255": M.TypeSpec("visible-string", 255),
          "Unicode255": M.TypeSpec("mms-string", 255), "Octet64": M.TypeSpec("octet-string", 64),
          "EntryTime": M.TypeSpec("binary-time")}
FC_ORDER = ["ST", "MX", "SP", "SV", "CF", "DC", "SG", "SE", "SR", "OR", "BL", "EX", "CO"]
OPT_SEQ, OPT_TIME, OPT_REASON, OPT_DATSET, OPT_DATAREF, OPT_OVFL, OPT_ENTRY, OPT_CONFREV = 1, 2, 3, 4, 5, 6, 7, 8
RCB_WRITABLE = {"Resv", "ResvTms", "RptEna", "GI", "TrgOps", "IntgPd", "BufTm", "OptFlds", "EntryID", "RptID", "DatSet", "PurgeBuf"}


def default_value(spec: M.TypeSpec):
    k = spec.kind
    if k == "structure":
        return [default_value(s) for _, s in spec.components]
    if k == "array":
        return [default_value(spec.element) for _ in range(spec.count)]
    return {"boolean": False, "integer": 0, "unsigned": 0, "floating-point": 0.0, "visible-string": "", "mms-string": "",
            "octet-string": b"", "utc-time": M.UtcTime(datetime.fromtimestamp(0, timezone.utc)),
            "binary-time": datetime.fromtimestamp(441763200, timezone.utc)}.get(k) if k != "bit-string" else M.BitString([False] * spec.size)


def binary_time(dt: datetime) -> bytes:
    epoch = datetime(1984, 1, 1, tzinfo=timezone.utc)
    delta = dt - epoch
    ms = (delta.seconds * 1000) + delta.microseconds // 1000
    return B.ctx_prim(12, ms.to_bytes(4, "big") + delta.days.to_bytes(2, "big"))


@dataclass
class Rcb:
    ld: str
    ln: str
    name: str
    buffered: bool
    rpt_id: str
    dataset: str
    conf_rev: int
    opt: list
    trg: list
    intg_pd: int
    buf_tm: int = 0
    enabled: bool = False
    owner: object = None
    seq: int = 0
    entry: int = 0
    buffer: list = field(default_factory=list)     # (entry id, time, [(member idx, reason)])
    resync_from: int | None = None
    overflow: bool = False
    integrity_task: object = None

    @property
    def fc(self) -> str:
        return "BR" if self.buffered else "RP"

    @property
    def item(self) -> str:
        return f"{self.ln}${self.fc}${self.name}"


class Iec61850Server:
    def __init__(self, scl: str | pathlib.Path, host: str = "127.0.0.1", port: int = 0, password: str | None = None,
                 ssl=None, buffer_size: int = 200):
        self.host, self.port, self.password, self.ssl, self.buffer_size = host, port, password, ssl, buffer_size
        self.types: dict[tuple[str, str], M.TypeSpec] = {}        # (ld, ln) -> LN type (components = FCs)
        self.values: dict[tuple[str, str], list] = {}             # (ld, ln) -> value list per FC (same order)
        self.datasets: dict[tuple[str, str], list] = {}           # (ld, "LLN0$Name") -> [(ld, item)]
        self.rcbs: dict[tuple[str, str], Rcb] = {}                # (ld, item) -> Rcb
        self.lds: list[str] = []
        self.requests: list[str] = []
        self.writers: set = set()
        self.server = None
        self._load(pathlib.Path(scl))

    # ------------------------------------------------------------ model from SCL
    def _load(self, path: pathlib.Path) -> None:
        root = ET.parse(path).getroot()
        tt = root.find("s:DataTypeTemplates", NS)
        lnt = {e.get("id"): e for e in tt.findall("s:LNodeType", NS)}
        dot = {e.get("id"): e for e in tt.findall("s:DOType", NS)}
        dat = {e.get("id"): e for e in tt.findall("s:DAType", NS)}

        def da_type(el) -> M.TypeSpec:
            bt = el.get("bType")
            if bt == "Struct":
                return M.TypeSpec("structure", components=[(b.get("name"), da_type(b)) for b in dat[el.get("type")].findall("s:BDA", NS)])
            return BTYPES.get(bt, M.TypeSpec("visible-string", 255))

        def do_parts(do_id: str, fc: str) -> list:
            out = []
            for el in dot[do_id]:
                tag = el.tag.split("}")[1]
                if tag == "SDO":
                    sub = do_parts(el.get("type"), fc)
                    if sub:
                        out.append((el.get("name"), M.TypeSpec("structure", components=sub)))
                elif tag == "DA" and el.get("fc") == fc:
                    out.append((el.get("name"), da_type(el)))
            return out

        ied = root.find("s:IED", NS)
        ied_name = ied.get("name")
        for ld_el in ied.iter(f"{{{NS['s']}}}LDevice"):
            ld = ied_name + ld_el.get("inst")
            self.lds.append(ld)
            for ln_el in [*ld_el.findall("s:LN0", NS), *ld_el.findall("s:LN", NS)]:
                ln = (ln_el.get("prefix") or "") + ln_el.get("lnClass") + (ln_el.get("inst") or "")
                fcs = []
                for fc in FC_ORDER:
                    comps = []
                    for do in lnt[ln_el.get("lnType")].findall("s:DO", NS):
                        parts = do_parts(do.get("type"), fc)
                        if parts:
                            comps.append((do.get("name"), M.TypeSpec("structure", components=parts)))
                    if comps:
                        fcs.append((fc, M.TypeSpec("structure", components=comps)))
                rcb_specs = []
                for rc in ln_el.findall("s:ReportControl", NS):
                    rcb_specs.append(self._rcb(ld, ln, rc))
                for fcname in ("BR", "RP"):
                    blocks = [r for r in rcb_specs if r.fc == fcname]
                    if blocks:
                        fcs.append((fcname, M.TypeSpec("structure", components=[(r.name, self._rcb_type(r)) for r in blocks])))
                spec = M.TypeSpec("structure", components=fcs)
                self.types[(ld, ln)] = spec
                self.values[(ld, ln)] = default_value(spec)
                for ds in ln_el.findall("s:DataSet", NS):
                    members = []
                    for f in ds.findall("s:FCDA", NS):
                        mln = (f.get("prefix") or "") + f.get("lnClass") + (f.get("lnInst") or "")
                        item = "$".join([mln, f.get("fc")] + f.get("doName").split(".") + (f.get("daName").split(".") if f.get("daName") else []))
                        members.append((ied_name + f.get("ldInst"), item))
                    self.datasets[(ld, f"{ln}${ds.get('name')}")] = members
                for doi in ln_el.findall("s:DOI", NS):
                    for dai in doi.iter(f"{{{NS['s']}}}DAI"):
                        val = dai.find("s:Val", NS)
                        if val is not None:
                            self._set_initial(ld, ln, doi.get("name"), dai.get("name"), val.text or "")
        for r in self.rcbs.values():
            self._sync_rcb_values(r)

    def _rcb(self, ld: str, ln: str, rc) -> Rcb:
        trg_el, opt_el = rc.find("s:TrgOps", NS), rc.find("s:OptFields", NS)
        trg = [False, *(trg_el is not None and trg_el.get(k) == "true" for k in ("dchg", "qchg", "dupd", "period", "gi"))]
        opt = [False] * 10
        for k, i in (("seqNum", 1), ("timeStamp", 2), ("reasonCode", 3), ("dataSet", 4), ("dataRef", 5), ("bufOvfl", 6),
                     ("entryID", 7), ("configRef", 8)):
            opt[i] = opt_el is not None and opt_el.get(k) == "true"
        r = Rcb(ld, ln, rc.get("name"), rc.get("buffered") == "true", rc.get("rptID") or f"{ld}/{ln}.{rc.get('name')}",
                f"{ld}/{ln}${rc.get('datSet')}", int(rc.get("confRev", "1")), opt, trg, int(rc.get("intgPd", "0")),
                int(rc.get("bufTime", "0")))
        self.rcbs[(ld, r.item)] = r
        return r

    @staticmethod
    def _rcb_type(r: Rcb) -> M.TypeSpec:
        T = M.TypeSpec
        comps = [("RptID", T("visible-string", 129)), ("RptEna", T("boolean"))]
        if not r.buffered:
            comps.append(("Resv", T("boolean")))
        comps += [("DatSet", T("visible-string", 129)), ("ConfRev", T("unsigned", 32)), ("OptFlds", T("bit-string", 10)),
                  ("BufTm", T("unsigned", 32)), ("SqNum", T("unsigned", 16 if r.buffered else 8)), ("TrgOps", T("bit-string", 6)),
                  ("IntgPd", T("unsigned", 32)), ("GI", T("boolean"))]
        if r.buffered:
            comps += [("PurgeBuf", T("boolean")), ("EntryID", T("octet-string", 8)), ("TimeOfEntry", T("binary-time")),
                      ("ResvTms", T("integer", 16))]
        comps.append(("Owner", T("octet-string", 64)))
        return T("structure", components=comps)

    def _sync_rcb_values(self, r: Rcb) -> None:
        spec = self.types[(r.ld, r.ln)]
        fcv = self.values[(r.ld, r.ln)][[n for n, _ in spec.components].index(r.fc)]
        idx = [n for n, _ in spec.component(r.fc).components].index(r.name)
        names = [n for n, _ in spec.component(r.fc).component(r.name).components]
        vals = {"RptID": r.rpt_id, "RptEna": r.enabled, "Resv": r.owner is not None, "DatSet": r.dataset, "ConfRev": r.conf_rev,
                "OptFlds": M.BitString(r.opt), "BufTm": r.buf_tm, "SqNum": r.seq & 0xFF, "TrgOps": M.BitString(r.trg),
                "IntgPd": r.intg_pd, "GI": False, "PurgeBuf": False, "EntryID": r.entry.to_bytes(8, "big"),
                "TimeOfEntry": datetime.now(timezone.utc), "ResvTms": 0, "Owner": b""}
        fcv[idx] = [vals[n] for n in names]

    def _locate(self, ld: str, item: str):
        """-> (spec, container list, index) of an MMS item, or None."""
        parts = item.split("$")
        ln = parts[0]
        if (ld, ln) not in self.types:
            return None
        spec, holder, idx = self.types[(ld, ln)], None, None
        value = self.values[(ld, ln)]
        if len(parts) == 1:
            return spec, None, value
        for p in parts[1:]:
            if spec.kind != "structure":
                return None
            names = [n for n, _ in spec.components]
            if p not in names:
                return None
            i = names.index(p)
            spec, holder, idx = spec.components[i][1], value, i
            value = value[i]
        return spec, (holder, idx), value

    def _set_initial(self, ld, ln, do, da, text) -> None:
        for fc, _ in self.types[(ld, ln)].components:
            loc = self._locate(ld, f"{ln}${fc}${do}${da}")
            if loc:
                spec, (holder, idx), _ = loc
                holder[idx] = self._parse_text(spec, text)

    @staticmethod
    def _parse_text(spec: M.TypeSpec, text: str):
        if spec.kind == "floating-point":
            return float(text)
        if spec.kind in ("integer", "unsigned"):
            enums = {"on": 1, "blocked": 2, "test": 3, "off": 5, "Ok": 1, "Warning": 2, "Alarm": 3, "status-only": 0}
            return int(text) if text.lstrip("-").isdigit() else enums.get(text, 0)
        if spec.kind == "boolean":
            return text.lower() == "true"
        return text

    # ------------------------------------------------------------ changing values (tests and demo)
    def set(self, object_ref: str, fc: str, value=None, quality: list | None = None, t: datetime | None = None,
            clock_bad: bool = False) -> None:
        ld, rest = object_ref.split("/", 1)
        ln, *path = rest.split(".")
        loc = self._locate(ld, "$".join([ln, fc] + path))
        if loc is None:
            raise KeyError(object_ref)
        spec, (holder, idx), old = loc
        changed_data = value is not None and value != old
        if value is not None:
            holder[idx] = M.BitString(value) if spec.kind == "bit-string" and isinstance(value, list) else value
        # the data object: deepest ancestor with q
        do_path = None
        for n in range(len(path), 0, -1):
            if self._locate(ld, "$".join([ln, fc] + path[:n] + ["q"])):
                do_path = path[:n]
                break
        changed_q = False
        if do_path is not None:
            if quality is not None:
                q = self._locate(ld, "$".join([ln, fc] + do_path + ["q"]))
                bits = [False] * 13
                for name in quality:
                    bits[{"invalid": 1, "questionable": 0, "overflow": 2, "outOfRange": 3, "badReference": 4, "oscillatory": 5,
                          "failure": 6, "oldData": 7, "inconsistent": 8, "inaccurate": 9, "substituted": 10, "test": 11,
                          "operatorBlocked": 12}[name]] = True
                if "questionable" in quality:
                    bits[0] = bits[1] = True
                changed_q = q[2].bits != bits
                q[1][0][q[1][1]] = M.BitString(bits)
            tl = self._locate(ld, "$".join([ln, fc] + do_path + ["t"]))
            if tl and (changed_data or changed_q or t):
                tl[1][0][tl[1][1]] = M.UtcTime(t or datetime.now(timezone.utc), True, False, clock_bad, 10)
        if changed_data or changed_q:
            self._changed(ld, ln, fc, path, "dchg" if changed_data else "qchg")

    def get(self, object_ref: str, fc: str):
        ld, rest = object_ref.split("/", 1)
        ln, *path = rest.split(".")
        return self._locate(ld, "$".join([ln, fc] + path))[2]

    def _changed(self, ld, ln, fc, path, reason) -> None:
        for r in self.rcbs.values():
            ds_ld, ds_item = r.dataset.split("/", 1)
            members = self.datasets.get((ds_ld, ds_item), [])
            for i, (mld, mitem) in enumerate(members):
                mparts = mitem.split("$")
                if mld == ld and mparts[0] == ln and mparts[1] == fc and (
                        path[:len(mparts) - 2] == mparts[2:] or mparts[2:][:len(path)] == path):
                    if r.trg[1 if reason == "dchg" else 2]:
                        self._queue(r, [(i, reason)])

    # ------------------------------------------------------------ reports
    def _queue(self, r: Rcb, members: list) -> None:
        r.entry += 1
        if r.buffered:
            r.buffer.append((r.entry, datetime.now(timezone.utc), members))
            if len(r.buffer) > self.buffer_size:
                r.buffer.pop(0)
                r.overflow = True
        if r.enabled and r.owner is not None:
            self._send_report(r, r.entry, members)

    def _send_report(self, r: Rcb, entry: int, members: list) -> None:
        link = r.owner
        ds_ld, ds_item = r.dataset.split("/", 1)
        ds = self.datasets[(ds_ld, ds_item)]
        values = [B.ctx_prim(10, r.rpt_id.encode()), M.encode_data(M.BitString(r.opt))]
        if r.opt[OPT_SEQ]:
            values.append(M.encode_data(r.seq & (0xFFFF if r.buffered else 0xFF), M.TypeSpec("unsigned")))
            r.seq += 1
        if r.opt[OPT_TIME]:
            values.append(binary_time(datetime.now(timezone.utc)))
        if r.opt[OPT_DATSET]:
            values.append(B.ctx_prim(10, r.dataset.encode()))
        if r.opt[OPT_OVFL] and r.buffered:
            values.append(M.encode_data(r.overflow))
            r.overflow = False
        if r.opt[OPT_ENTRY] and r.buffered:
            values.append(B.ctx_prim(9, entry.to_bytes(8, "big")))
        if r.opt[OPT_CONFREV]:
            values.append(M.encode_data(r.conf_rev, M.TypeSpec("unsigned")))
        reasons = {}
        for i, why in members:
            reasons.setdefault(i, set()).add(why)
        incl = [i in reasons for i in range(len(ds))]
        values.append(M.encode_data(M.BitString(incl)))
        idx = sorted(reasons)
        if r.opt[OPT_DATAREF]:
            values += [B.ctx_prim(10, f"{ds[i][0]}/{ds[i][1]}".encode()) for i in idx]
        for i in idx:
            spec, _, v = self._locate(*ds[i])
            values.append(M.encode_data(v, spec))
        if r.opt[OPT_REASON]:
            names = ["reserved", "dchg", "qchg", "dupd", "integrity", "gi", "application"]
            values += [M.encode_data(M.BitString([n in reasons[i] for n in names])) for i in idx]
        asyncio.get_running_loop().create_task(self._safe_send(link, M.information_report("RPT", values)))

    @staticmethod
    async def _safe_send(link, pdu: bytes) -> None:
        try:
            await link.send(pdu)
        except (ConnectionError, OSError, IsoError):
            pass

    def _gi(self, r: Rcb) -> None:
        ds_ld, ds_item = r.dataset.split("/", 1)
        n = len(self.datasets[(ds_ld, ds_item)])
        r.entry += 1
        self._send_report(r, r.entry, [(i, "gi") for i in range(n)])

    async def _integrity(self, r: Rcb) -> None:
        while r.enabled and r.intg_pd > 0:
            await asyncio.sleep(r.intg_pd / 1000)
            if r.enabled and r.trg[4]:
                ds_ld, ds_item = r.dataset.split("/", 1)
                r.entry += 1
                self._send_report(r, r.entry, [(i, "integrity") for i in range(len(self.datasets[(ds_ld, ds_item)]))])

    def _write_rcb(self, r: Rcb, attr: str, value, link) -> int | None:
        """-> None on success or a DataAccessError code."""
        if r.owner is not None and r.owner is not link:
            return 3                                                     # object-access-denied: reserved by another
        if attr not in RCB_WRITABLE:
            return 3
        if r.enabled and attr not in ("RptEna", "GI", "Resv", "ResvTms"):
            return 2                                                     # temporarily-unavailable while enabled
        if attr == "RptEna":
            r.enabled = bool(value)
            r.owner = link if r.enabled else (r.owner if not r.buffered else link)
            if r.enabled:
                if r.buffered:
                    start = r.resync_from
                    for entry, _, members in list(r.buffer):
                        if start is not None and entry > start:
                            self._send_report(r, entry, members)
                    r.resync_from = None
                if r.intg_pd and r.trg[4]:
                    r.integrity_task = asyncio.get_running_loop().create_task(self._integrity(r))
            elif r.integrity_task:
                r.integrity_task.cancel()
        elif attr == "GI":
            if value and r.enabled:
                self._gi(r)
        elif attr in ("Resv", "ResvTms"):
            r.owner = link if value else None
        elif attr == "OptFlds":
            r.opt = list(value.bits) + [False] * (10 - len(value.bits))
        elif attr == "TrgOps":
            r.trg = list(value.bits) + [False] * (6 - len(value.bits))
        elif attr == "IntgPd":
            r.intg_pd = int(value)
        elif attr == "BufTm":
            r.buf_tm = int(value)
        elif attr == "EntryID":
            eid = int.from_bytes(value, "big")
            if not any(e == eid for e, _, _ in r.buffer) and eid != 0:
                return 11                                               # object-value-invalid: not in the buffer
            r.resync_from = eid
        elif attr == "PurgeBuf":
            return 3                                                     # spec 15.4: a client must never purge
        else:
            return 3
        if r.owner is None and not r.buffered and attr != "Resv":
            r.owner = link
        self._sync_rcb_values(r)
        return None

    # ------------------------------------------------------------ network
    async def start(self) -> "Iec61850Server":
        self.server = await asyncio.start_server(self._conn, self.host, self.port, ssl=self.ssl)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.kick_all()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def kick_all(self) -> None:
        for w in list(self.writers):
            w.close()

    async def _conn(self, reader, writer) -> None:
        self.writers.add(writer)
        link = IsoLink(reader, writer)
        try:
            a = await link.accept()
            self.requests.append("Initiate")
            if self.password is not None and a["password"] != self.password:
                link.answer(None, diagnostic=1)
                await writer.drain()
                return
            link.answer(M.initiate_response())
            while True:
                pdu = M.parse_pdu(await link.recv())
                if pdu.kind == "conclude-request":
                    self.requests.append("Conclude")
                    await link.send(B.ctx_prim(12, b""))
                    continue
                if pdu.kind != "confirmed-request":
                    continue
                await link.send(self._service(pdu, link))
        except (IsoError, asyncio.IncompleteReadError, ConnectionError, OSError, B.BerError):
            pass
        finally:
            for r in self.rcbs.values():
                if r.owner is link:
                    r.enabled = False if not r.buffered else r.enabled and False
                    r.owner = None
                    if r.integrity_task:
                        r.integrity_task.cancel()
                    self._sync_rcb_values(r)
            self.writers.discard(writer)
            writer.close()

    def _service(self, pdu: M.Pdu, link) -> bytes:
        inv, svc, body = pdu.invoke_id, pdu.service, pdu.body
        name = M.SVC_NAMES.get(svc, f"Service{svc}")
        if svc == M.SVC_GET_NAME_LIST:
            self.requests.append(name)
            cls = body.child(0).children()[0].int()
            scope = body.child(1).children()[0]
            after = body.child(2).text() if body.child(2) else None
            if cls == M.CLASS_DOMAIN:
                names = list(self.lds)
            elif cls == M.CLASS_NAMED_VARIABLE:
                names = self._names(scope.text())
            elif cls == M.CLASS_NAMED_VARIABLE_LIST:
                names = sorted(i for (d, i) in self.datasets if d == scope.text())
            else:
                names = []
            if after:
                names = names[names.index(after) + 1:] if after in names else []
            return M.confirmed_response(inv, M.name_list_response(names[:300], len(names) > 300))
        if svc == M.SVC_GET_VAR_ATTR:
            self.requests.append(name)
            on = body.child(0).children()[0]
            d, i = (c.text() for c in on.children())
            loc = self._locate(d, i)
            if loc is None:
                return M.confirmed_error(inv, 7, 2)                     # access: object-non-existent
            return M.confirmed_response(inv, M.var_attr_response(loc[0]))
        if svc == M.SVC_GET_NVL_ATTR:
            self.requests.append(name)
            d, i = (c.text() for c in body.children()[0].children())
            if (d, i) not in self.datasets:
                return M.confirmed_error(inv, 7, 2)
            return M.confirmed_response(inv, M.nvl_attr_response(self.datasets[(d, i)]))
        if svc == M.SVC_READ:
            self.requests.append(name)
            out = []
            for d, i in M.parse_request_names(body):
                loc = self._locate(d, i)
                out.append(M.access_failure(10) if loc is None else M.encode_data(loc[2], loc[0]))
            return M.confirmed_response(inv, M.read_response(out))
        if svc == M.SVC_WRITE:
            names = M.parse_request_names(body)
            data = body.children()[1].children()
            results = []
            for (d, i), dv in zip(names, data):
                parts = i.split("$")
                r = self.rcbs.get((d, "$".join(parts[:3])))
                if r is not None and len(parts) == 4:
                    self.requests.append(f"WRITE_RCB:{parts[3]}")
                    results.append(self._write_rcb(r, parts[3], M.decode_data(dv), link))
                else:
                    self.requests.append(f"Write:{i}")
                    results.append(3)                                    # object-access-denied
            return M.confirmed_response(inv, M.write_response(results))
        self.requests.append(name)
        return M.confirmed_error(inv, 4, 1)                              # service: primitives-out-of-sequence

    def _names(self, ld: str) -> list[str]:
        out = []

        def walk(prefix: str, spec: M.TypeSpec):
            out.append(prefix)
            if spec.kind == "structure":
                for n, s in spec.components:
                    walk(f"{prefix}${n}", s)
        for (d, ln), spec in self.types.items():
            if d == ld:
                walk(ln, spec)
        return out


async def _main() -> None:                     # python tools/simulators/iec61850_server.py [port]
    import math
    import random
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 102
    srv = Iec61850Server(pathlib.Path(__file__).parent / "scl" / "feeder_relay.icd", host="0.0.0.0", port=port)
    await srv.start()
    print(f"IEC 61850 server FDR21 on tcp port {srv.port}", flush=True)
    t = 0
    while True:
        await asyncio.sleep(1)
        t += 1
        srv.set("FDR21LD0/MMXU1.TotW.mag.f", "MX", round(3.2 + 0.3 * math.sin(t / 60) + random.uniform(-0.02, 0.02), 3))


if __name__ == "__main__":
    asyncio.run(_main())
