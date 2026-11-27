"""SNMP manager connector (spec OSS-DAQ-CON-001 section 15.9), built on opcare_connector_sdk.

Polls IT and network equipment (switches, routers, firewalls, servers, UPSs, PDUs) with GetRequest, walks MIBs
for browsing, and turns SNMPv1/v2c traps and informs into platform events. Read-only: no SetRequest exists.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, Point, PointTable, Q, ReadOnlyGuard, now_iso

from . import mibs
from .ber import (COUNTER32, COUNTER64, END_OF_MIB_VIEW, ERROR_NAMES, GAUGE32, GET, GET_BULK, GET_NEXT, INFORM,
                  INTEGER, IP_ADDRESS, OCTET_STRING, OID, OPAQUE, RESPONSE, TIMETICKS, TRAP_V1, TRAP_V2, BerError,
                  Pdu, VarBind, in_subtree, oid_tuple)
from .client import SnmpAuthError, SnmpClient, SnmpError, SnmpTimeout, Target, resolve
from .messages import V1, V3, VERSION, UsmUser, decode_community, encode_community, peek_version

NUMERIC = (INTEGER, COUNTER32, GAUGE32, TIMETICKS, COUNTER64)
SKIP = object()                       # rate transform: nothing to publish this time
SYS_DESCR, SYS_NAME, SYS_UPTIME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.3.0"
GENERIC_TRAPS = {0: "1.3.6.1.6.3.1.1.5.1", 1: "1.3.6.1.6.3.1.1.5.2", 2: "1.3.6.1.6.3.1.1.5.3",
                 3: "1.3.6.1.6.3.1.1.5.4", 4: "1.3.6.1.6.3.1.1.5.5", 5: "1.3.6.1.6.3.1.1.5.6"}


def text(value) -> str:
    """OCTET STRING -> text when printable UTF-8, else hex (spec 15.9 data mapping)."""
    if isinstance(value, (bytes, bytearray)):
        try:
            t = bytes(value).decode("utf-8")
            if all(c.isprintable() or c in "\r\n\t" for c in t.rstrip("\x00")):
                return t.rstrip("\x00")
        except UnicodeDecodeError:
            pass
        return bytes(value).hex(":")
    return str(value)


def guess_type(vb: VarBind) -> str:
    if vb.tag in (COUNTER32, COUNTER64, GAUGE32, TIMETICKS, INTEGER):
        return "int"
    return "string"


@dataclass
class _Rate:
    value: int
    t: float


class SnmpConnector(Connector):
    type = "snmp"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": True}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.client: SnmpClient | None = None
        self.targets: dict[str, Target] = {}
        self.guard: ReadOnlyGuard | None = None
        self.tasks: dict[int, asyncio.Task] = {}
        self.dev_state: dict[str, str] = {}
        self.dev_max: dict[str, int] = {}             # max OIDs per request, per device (halved on tooBig)
        self.singles: set[str] = set()                # tags read alone after an error isolated them
        self.rates: dict[str, _Rate] = {}
        self.trap_rx = None
        self.trap_ips: dict[str, str] = {}
        self.trap_community: bytes | None = None
        self.stats = {"traps": 0, "traps_dropped": 0, "informs_acked": 0, "too_big": 0, "auth_errors": 0}

    # ---------------------------------------------------------------- configuration
    def devices(self, config: dict) -> list[str]:
        return [d["id"] for d in config["connection"]["devices"]]

    async def validate(self, config: dict) -> list[str]:
        conn, errs = config["connection"], []
        ids = self.devices(config)
        if len(set(ids)) != len(ids):
            errs.append("duplicate device id")
        for p in config["points"]:
            a, tag = p["address"], p["tag_id"]
            if a["device"] not in ids:
                errs.append(f"{tag}: unknown device '{a['device']}'")
            if a.get("transform") == "rate" and p["data_type"] not in ("float", "int"):
                errs.append(f"{tag}: a rate is a number; use data_type float")
        if conn["version"] == "3":
            v3 = conn["v3"]
            for k in ("auth_password_ref", "priv_password_ref"):
                if v3.get(k):
                    try:
                        if len(self.secret(v3[k])) < 8:
                            errs.append(f"v3 {k.split('_')[0]} password must have at least 8 characters")
                    except ConnectorError:
                        pass                          # reported as SECRET_MISSING by the runtime
        return errs

    def _make_client(self, config: dict, guard: ReadOnlyGuard) -> SnmpClient:
        conn = config["connection"]
        acq = config.get("acquisition") or {}
        version = VERSION[conn["version"]]
        timeout = acq.get("request_timeout_ms", 3000) / 1000
        retries = conn.get("retries", 1)
        if version == V3:
            v3 = conn["v3"]
            level = v3.get("security_level", "authPriv")
            user = UsmUser(v3["username"], level, v3.get("auth_protocol", "SHA-256"),
                           self.secret(v3["auth_password_ref"]) if level != "noAuthNoPriv" else None,
                           v3.get("priv_protocol", "AES"),
                           self.secret(v3["priv_password_ref"]) if level == "authPriv" else None)
            return SnmpClient(V3, guard, user=user, timeout_s=timeout, retries=retries,
                              context_name=v3.get("context_name", ""))
        return SnmpClient(version, guard, community=self.secret(conn["community_ref"]).encode(),
                          timeout_s=timeout, retries=retries)

    @staticmethod
    def _targets(config: dict) -> dict[str, Target]:
        conn = config["connection"]
        return {d["id"]: Target(d["id"], d["host"], d.get("port", conn.get("port", 161)))
                for d in conn["devices"]}

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self.guard = ReadOnlyGuard("snmp", ALLOW["snmp"], on_block=lambda r: sink.event(
            "guard.blocked", "critical", f"Blocked SNMP request {r}", attrs={"request": r}))
        self.client = self._make_client(config, self.guard)
        self.targets = self._targets(config)
        self.dev_state = {d: "connecting" for d in self.targets}
        self.dev_max = {d: config["connection"].get("max_oids_per_request", 20) for d in self.targets}
        self.singles, self.rates = set(), {}
        self._sync_scans()
        traps = config["connection"].get("traps")
        if traps:
            await self._start_traps(traps)

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        if config["connection"] != self.config.get("connection"):
            return False
        self.config, self.table = config, table
        self.rates = {k: v for k, v in self.rates.items() if k in table.points}
        self._sync_scans()
        return True

    async def stop(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks = {}
        if self.client:
            self.client.close()
        if self.trap_rx:
            self.trap_rx.close()
            self.trap_rx = None
            await asyncio.sleep(0)

    def _sync_scans(self) -> None:
        classes = self.table.scan_classes()
        for ms in [m for m in self.tasks if m not in classes]:
            self.tasks.pop(ms).cancel()
        for ms in classes:
            if ms not in self.tasks or self.tasks[ms].done():
                self.tasks[ms] = asyncio.create_task(self._scan(ms))

    @property
    def timeout_s(self) -> float:
        return (self.config.get("acquisition") or {}).get("request_timeout_ms", 3000) / 1000

    # ---------------------------------------------------------------- polling
    async def _scan(self, scan_ms: int) -> None:
        period = scan_ms / 1000
        next_run = time.time()
        backoff: dict[str, tuple[float, float]] = {}      # device -> (retry at, delay) while unreachable (spec 13)
        while True:
            await asyncio.sleep(max(0.0, next_run - time.time()))
            pts = self.table.scan_classes().get(scan_ms, [])
            by_dev: dict[str, list[Point]] = {}
            for p in pts:
                by_dev.setdefault(p.device, []).append(p)
            now = time.monotonic()
            due = [d for d in by_dev if backoff.get(d, (0, 0))[0] <= now]
            results = await asyncio.gather(*(self._poll_device(d, by_dev[d]) for d in due))
            for d, ok in zip(due, results):
                if ok:
                    backoff.pop(d, None)
                else:
                    delay = min(backoff.get(d, (0, 0.5))[1] * 2, 60.0)
                    backoff[d] = (time.monotonic() + delay * random.uniform(0.8, 1.2), delay)
            next_run += period
            if time.time() > next_run:
                self.sink.scan_overrun(scan_ms)
                next_run = time.time()

    def _chunks(self, device: str, points: list[Point]) -> list[list[Point]]:
        size = self.dev_max.get(device, 20)
        grouped = [p for p in points if p.tag_id not in self.singles]
        out = [grouped[i:i + size] for i in range(0, len(grouped), size)]
        out += [[p] for p in points if p.tag_id in self.singles]
        return out

    async def _poll_device(self, device: str, points: list[Point]) -> bool:
        target = self.targets[device]
        try:
            for chunk in self._chunks(device, points):
                await self._read_chunk(target, chunk)
        except SnmpTimeout as exc:
            self._device_down(device, ConnectorError("CONN_TIMEOUT", str(exc)))
            return False
        except SnmpAuthError as exc:
            self.stats["auth_errors"] += 1
            target.engine_id = None                       # re-discover after the user fixes the credentials
            self._device_down(device, ConnectorError("AUTH_FAILED", f"SNMPv3: {exc.report}"))
            return False
        except ConnectorError as exc:
            self._device_down(device, exc)
            return False
        return True

    async def _read_chunk(self, target: Target, chunk: list[Point]) -> None:
        try:
            pdu = await self.client.request(target, GET, [p.address["oid"] for p in chunk])
        except SnmpError as exc:
            self._device_ok(target.id)
            if exc.status == 1:                           # tooBig: smaller requests from now on
                self.stats["too_big"] += 1
                if len(chunk) > 1:
                    self.dev_max[target.id] = max(1, len(chunk) // 2)
                    half = len(chunk) // 2
                    await self._read_chunk(target, chunk[:half])
                    await self._read_chunk(target, chunk[half:])
                    return
                self._publish(chunk[0], None, Q.BAD_CONFIG_ERROR, ["tooBig"])
                return
            if len(chunk) > 1:                            # isolate the bad OID (spec 15.9, as Modbus 15.1)
                bad = chunk[exc.index - 1] if 0 < exc.index <= len(chunk) else None
                if bad is not None:
                    self.singles.add(bad.tag_id)
                    await self._read_chunk(target, [bad])
                    rest = [p for p in chunk if p is not bad]
                    if rest:
                        await self._read_chunk(target, rest)
                else:
                    for p in chunk:
                        self.singles.add(p.tag_id)
                        await self._read_chunk(target, [p])
                return
            q = Q.BAD_DEVICE_FAILURE if exc.status == 5 else Q.BAD_CONFIG_ERROR
            self._publish(chunk[0], None, q, [ERROR_NAMES.get(exc.status, f"error{exc.status}")])
            return
        self._device_ok(target.id)
        if len(pdu.varbinds) != len(chunk):
            raise ConnectorError("PROTOCOL_ERROR", f"{target.id}: {len(pdu.varbinds)} values for {len(chunk)} OIDs")
        for p, vb in zip(chunk, pdu.varbinds):
            v, q, flags = self.convert(p, vb)
            if v is not SKIP:
                self._publish(p, v, q, flags)

    def _publish(self, p: Point, v, q: int, flags=()) -> None:
        self.sink.value(p.tag_id, v, q, flags=list(flags))

    def convert(self, p: Point, vb: VarBind, use_rate: bool = True) -> tuple:
        """VarBind -> (raw value or SKIP, quality, flags) per spec 15.9 data mapping."""
        if vb.is_exception:
            return None, Q.BAD_CONFIG_ERROR, [vb.type_name]
        dt, v = p.data_type, vb.value
        if use_rate and p.address.get("transform") == "rate":
            if vb.tag not in NUMERIC:
                return None, Q.BAD_CONFIG_ERROR, ["TYPE_MISMATCH"]
            now, prev = time.monotonic(), self.rates.get(p.tag_id)
            self.rates[p.tag_id] = _Rate(v, now)
            if prev is None or now <= prev.t:
                return SKIP, Q.GOOD, []
            delta = v - prev.value
            if delta < 0:
                if vb.tag != COUNTER32:
                    return SKIP, Q.GOOD, []               # counter discontinuity (agent restarted)
                delta += 1 << 32
            return delta / (now - prev.t), Q.GOOD, []
        if vb.tag in NUMERIC:
            if dt == "bool":
                return v == 1, Q.GOOD, []
            if dt == "string":
                return str(v), Q.GOOD, []
            return v, Q.GOOD, []
        if vb.tag in (OCTET_STRING, OPAQUE, OID, IP_ADDRESS):
            t = text(v)
            if dt == "string":
                return t, Q.GOOD, []
            if dt in ("int", "float", "enum") and vb.tag == OCTET_STRING:
                try:                                      # some agents report numbers as text, e.g. "23.5"
                    num = float(t.strip())
                    return (int(num) if dt != "float" and num.is_integer() else num), Q.GOOD, []
                except ValueError:
                    pass
            return None, Q.BAD_CONFIG_ERROR, ["TYPE_MISMATCH"]
        return None, Q.BAD_CONFIG_ERROR, ["TYPE_MISMATCH"]

    def _device_ok(self, device: str) -> None:
        if self.dev_state.get(device) != "connected":
            self.dev_state[device] = "connected"
            self.sink.device_state(device, "connected")
        else:
            self.sink.device_ok(device)

    def _device_down(self, device: str, exc: ConnectorError) -> None:
        if self.dev_state.get(device) != "unreachable":
            self.dev_state[device] = "unreachable"
            self.rates = {k: v for k, v in self.rates.items() if self.table.points.get(k) and
                          self.table.points[k].device != device}
            self.sink.device_state(device, "unreachable", exc)

    # ---------------------------------------------------------------- traps and informs
    async def _start_traps(self, traps: dict) -> None:
        conn = self.config["connection"]
        ref = traps.get("community_ref") or conn.get("community_ref")
        self.trap_community = self.secret(ref).encode() if ref else None
        for d in conn["devices"]:
            try:
                self.trap_ips.setdefault(await resolve(d["host"], 162), d["id"])   # same address twice: first wins
            except ConnectorError:
                pass
        loop = asyncio.get_running_loop()
        me = self

        class P(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                me._on_trap(data, addr)
        try:
            self.trap_rx, _ = await loop.create_datagram_endpoint(
                P, local_addr=(traps.get("bind_host", "0.0.0.0"), traps["bind_port"]))
        except OSError as exc:
            self.sink.event("snmp.trap_listener_failed", "medium",
                            f"Cannot receive traps on udp port {traps['bind_port']}: {exc.strerror or exc}")

    def _on_trap(self, data: bytes, addr) -> None:
        ip = addr[0][7:] if addr[0].startswith("::ffff:") else addr[0]
        device = self.trap_ips.get(ip)
        try:
            if device is None or self.trap_community is None or peek_version(data) == V3:
                raise ValueError("unknown sender, no community or SNMPv3 trap")
            version, community, pdu = decode_community(data)
            if community != self.trap_community:
                raise ValueError("wrong community")
        except (BerError, ValueError, IndexError):
            self.stats["traps_dropped"] += 1
            return
        if pdu.tag == TRAP_V1:
            trap_oid = GENERIC_TRAPS.get(pdu.generic_trap) or f"{pdu.enterprise}.0.{pdu.specific_trap}"
            uptime, vbs = pdu.timestamp, pdu.varbinds
        elif pdu.tag in (TRAP_V2, INFORM) and len(pdu.varbinds) >= 2:
            uptime = pdu.varbinds[0].value
            trap_oid, vbs = str(pdu.varbinds[1].value), pdu.varbinds[2:]
            if pdu.tag == INFORM:                          # acknowledge: the only Response a manager sends
                self.guard.check("Response(Inform)")
                ack = Pdu(RESPONSE, pdu.request_id, 0, 0, pdu.varbinds)
                self.trap_rx.sendto(encode_community(version, community, ack), addr)
                self.stats["informs_acked"] += 1
        else:
            self.stats["traps_dropped"] += 1
            return
        self.stats["traps"] += 1
        name = mibs.TRAPS.get(trap_oid, trap_oid)
        detail = ", ".join(f"{mibs.name_of(v.oid)}={text(v.value)}" for v in vbs[:3])
        self.sink.event("snmp.trap", mibs.TRAP_SEVERITY.get(name, "info"),
                        f"{name} from {device}" + (f": {detail}" if detail else ""), device=device,
                        attrs={"trap_oid": trap_oid, "trap": name, "uptime": uptime,
                               "varbinds": [{"oid": v.oid, "name": mibs.name_of(v.oid), "value": text(v.value)[:200]}
                                            for v in vbs[:20]]})

    # ---------------------------------------------------------------- admin operations
    async def test_connection(self, config: dict) -> dict:
        guard = ReadOnlyGuard("snmp", ALLOW["snmp"])
        try:
            client = self._make_client(config, guard)
        except ConnectorError as exc:
            return {"devices": [{"id": d["id"], "ok": False, "error": exc.as_dict()} for d in config["connection"]["devices"]]}
        targets = self._targets(config)

        async def one(t: Target) -> dict:
            t0 = time.monotonic()
            entry = {"id": t.id}
            try:
                pdu = await client.request(t, GET, [SYS_NAME, SYS_DESCR, SYS_UPTIME])
                vals = {vb.oid: vb for vb in pdu.varbinds}
                info = {"sysName": text(vals[SYS_NAME].value) if not vals[SYS_NAME].is_exception else None,
                        "sysDescr": text(vals[SYS_DESCR].value)[:160] if not vals[SYS_DESCR].is_exception else None}
                if not vals[SYS_UPTIME].is_exception:
                    info["uptime_s"] = vals[SYS_UPTIME].value // 100
                entry.update(ok=True, info=info)
            except SnmpError as exc:                      # answered (e.g. v1 noSuchName): reachable, credentials fine
                entry.update(ok=True, info={"error_status": ERROR_NAMES.get(exc.status, exc.status)})
            except SnmpTimeout as exc:
                entry.update(ok=False, error={"code": "CONN_TIMEOUT", "message":
                                              f"{exc} — check the address, port, community or firewall"})
            except SnmpAuthError as exc:
                entry.update(ok=False, error={"code": "AUTH_FAILED", "message": f"SNMPv3: {exc.report}"})
            except ConnectorError as exc:
                entry.update(ok=False, error=exc.as_dict())
            entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            return entry
        try:
            return {"devices": await asyncio.gather(*(one(t) for t in targets.values()))}
        finally:
            client.close()

    async def read_now(self, args: dict) -> dict:
        if not self.client:
            raise ConnectorError("CONN_CLOSED", "the source is not running")
        jobs: list[tuple[dict, Point]] = []
        for tag in args.get("tag_ids", []):
            p = self.table.points.get(tag)
            if p is None:
                raise ConnectorError("POINT_NOT_FOUND", f"unknown tag {tag}")
            jobs.append(({"t": tag}, p))
        for item in args.get("addresses", []):
            a = item["address"]
            if a.get("device") not in self.targets:
                raise ConnectorError("ADDR_INVALID", f"unknown device {a.get('device')}")
            jobs.append(({"address": a}, Point("adhoc", a, item["data_type"], device=a["device"])))
        out = []
        for head, p in jobs:
            try:
                pdu = await self.client.request(self.targets[p.device], GET, [p.address["oid"]])
                v, q, _ = self.convert(p, pdu.varbinds[0], use_rate=False)
                if v is not None:
                    v, fq, _ = p.convert(v)
                    q = Q.combine(q, fq) if fq is not None else q
            except SnmpError as exc:
                v, q = None, Q.BAD_DEVICE_FAILURE if exc.status == 5 else Q.BAD_CONFIG_ERROR
            except (SnmpTimeout, SnmpAuthError):
                v, q = None, Q.BAD_COMM_FAILURE
            out.append({**head, "v": v, "q": q, "ts": now_iso()})
        return {"values": out}

    async def browse(self, args: dict) -> dict:
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        path = args.get("path") or ""
        dev, _, rest = path.partition("/")
        dev = dev or cfg["connection"]["devices"][0]["id"]
        root, _, after = (rest or "1.3.6.1.2.1").partition(">")
        root = root.strip(".")
        try:
            oid_tuple(root)
            if after:
                oid_tuple(after)
        except ValueError:
            raise ConnectorError("ADDR_INVALID", f"'{rest}' is not an OID") from None
        own = cfg is self.config and self.client is not None
        client = self.client if own else self._make_client(cfg, ReadOnlyGuard("snmp", ALLOW["snmp"]))
        targets = self.targets if own else self._targets(cfg)
        if dev not in targets:
            raise ConnectorError("ADDR_INVALID", f"unknown device '{dev}'")
        target = targets[dev]
        max_items = args.get("max_items", 500)
        reps = cfg["connection"].get("max_repetitions", 25)
        cur, items, done = after or root, [], False
        try:
            while len(items) < max_items and not done:
                try:
                    if client.version == V1:
                        pdu = await client.request(target, GET_NEXT, [cur])
                    else:
                        pdu = await client.request(target, GET_BULK, [cur], 0, min(reps, max_items - len(items)))
                except SnmpError as exc:
                    if exc.status == 2:                   # v1 end of MIB
                        break
                    raise
                if not pdu.varbinds:
                    break
                for vb in pdu.varbinds:
                    if vb.tag == END_OF_MIB_VIEW or not in_subtree(vb.oid, root) or \
                            oid_tuple(vb.oid) <= oid_tuple(cur):
                        done = True
                        break
                    items.append({"path": f"{dev}/{vb.oid}", "name": mibs.name_of(vb.oid), "kind": "variable",
                                  "has_children": False, "data_type": guess_type(vb),
                                  "address": {"device": dev, "oid": vb.oid},
                                  "info": {"type": vb.type_name, "sample": text(vb.value)[:120]}})
                    cur = vb.oid
                    if len(items) >= max_items:
                        break
                await asyncio.sleep(0.02)                 # paced: never starve the polling (spec 11 rule 3)
        except SnmpTimeout as exc:
            raise ConnectorError("CONN_TIMEOUT", str(exc)) from None
        except SnmpAuthError as exc:
            raise ConnectorError("AUTH_FAILED", f"SNMPv3: {exc.report}") from None
        finally:
            if not own:
                client.close()
        result = {"items": items}
        if not done and items:
            result["next_path"] = f"{dev}/{root}>{items[-1]['address']['oid']}"
        return result

    async def diagnostics(self) -> dict:
        c = self.client
        return {"requests": c.requests if c else 0, "bad_datagrams": c.bad_datagrams if c else 0,
                "isolated_oids": len(self.singles), **self.stats,
                "devices": {d: {"state": self.dev_state.get(d), "max_oids_per_request": self.dev_max.get(d),
                                "last_latency_ms": t.last_latency_ms,
                                "engine_id": t.engine_id.hex() if t.engine_id else None}
                            for d, t in self.targets.items()}}
