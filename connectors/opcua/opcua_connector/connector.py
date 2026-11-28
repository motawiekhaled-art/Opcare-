"""OPC UA client connector (spec OSS-DAQ-CON-001 section 15.2), built on opcare_connector_sdk and our own OPC UA
stack (uabin, security, channel, client): no third-party OPC UA library.

Reads PLCs, DCS, SCADA and historians that expose an OPC UA server (Siemens S7-1500, Rockwell via FactoryTalk
Linx Gateway, Kepware, Ignition, ABB 800xA, Emerson DeltaV, Honeywell Experion ...). Subscribe mode (default) uses
monitored items; poll mode reads per scan class.

Read-only (spec 12): Write, Call, HistoryUpdate and node management have no encoder at all, every request passes the
guard, and nothing is written on reconnect (spec 12 rule 3: there is no "write on reconnect" feature here to switch
off — subscriptions are simply created again).
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, PointTable, Q, ReadOnlyGuard

from . import security as S
from . import uabin as U
from .client import ATTR_DATATYPE, ATTR_VALUE, UaClient, UaServiceError, status_name
from .uabin import NodeId, UaError

DEVICE = "server"
GOOD_LOCAL_OVERRIDE = 0x00960000
UNCERTAIN_CODES = {0x408F0000: Q.UNCERTAIN_LAST_USABLE, 0x40930000: Q.UNCERTAIN_SENSOR_NOT_ACCURATE,
                   0x40940000: Q.UNCERTAIN_EU_EXCEEDED}
BAD_CONFIG = {0x80330000, 0x80340000, 0x80740000, 0x80350000, 0x803A0000}     # NodeIdInvalid/Unknown, TypeMismatch ...
BAD_COMM = {0x80050000, 0x808A0000, 0x800D0000, 0x80250000, 0x80260000, 0x80320000}
INT_TYPES = {U.SBYTE, U.BYTE, U.INT16, U.UINT16, U.INT32, U.UINT32, U.INT64, U.UINT64}
# ns0 DataType node ids -> platform data type (browse); 29 Enumeration
DATATYPE_KIND = {1: "bool", 2: "int", 3: "int", 4: "int", 5: "int", 6: "int", 7: "int", 8: "int", 9: "int",
                 10: "float", 11: "float", 12: "string", 13: "string", 21: "string", 26: "float", 27: "int",
                 28: "int", 29: "enum", 19: "int"}
NODE_CLASS = {1: "Object", 2: "Variable", 4: "Method", 8: "ObjectType", 16: "VariableType", 32: "ReferenceType",
              64: "DataType", 128: "View"}
FOLDER_TYPE = 61
SERVER_STATE = NodeId(2259)              # Server.ServerStatus.State
PRODUCT_NAME = NodeId(2261)              # Server.ServerStatus.BuildInfo.ProductName


def quality(status: int) -> int:
    """StatusCode -> platform quality (spec 15.2)."""
    sev, code = status >> 30, status & 0xFFFF0000
    if sev == 0:
        return Q.GOOD_LOCAL_OVERRIDE if code == GOOD_LOCAL_OVERRIDE else Q.GOOD
    if sev == 1:
        return UNCERTAIN_CODES.get(code, Q.UNCERTAIN)
    if code in BAD_CONFIG:
        return Q.BAD_CONFIG_ERROR
    if code == 0x808B0000:
        return Q.BAD_DEVICE_FAILURE
    if code in BAD_COMM:
        return Q.BAD_COMM_FAILURE
    if code == 0x808D0000:
        return Q.BAD_OUT_OF_SERVICE
    return Q.BAD


def iso(dt: datetime | None) -> str | None:
    if dt is None or dt.year < 1990:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def plain(v):
    """Variant value -> JSON-friendly Python value."""
    if isinstance(v, list):
        return [plain(x) for x in v]
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, (U.LocalizedText,)):
        return v.text
    if isinstance(v, U.QualifiedName):
        return v.name
    if isinstance(v, NodeId):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return str(v)                                    # Guid, ExtensionObject ...


def coerce(var: U.Variant | None, data_type: str):
    """(value, flags). Raises TypeError if the value cannot be represented as data_type."""
    if var is None or var.type == 0 or var.value is None:
        return None, ["NULL"]
    v = var.value
    if var.is_array:
        if data_type == "string":
            return json.dumps(plain(v)), []
        raise TypeError("array value")
    if data_type == "bool":
        if isinstance(v, (bool, int, float)):
            return bool(v), []
        raise TypeError("not a boolean")
    if data_type in ("int", "enum"):
        if isinstance(v, bool) or isinstance(v, int):
            return int(v), []
        if isinstance(v, float) and v.is_integer():
            return int(v), []
        raise TypeError("not an integer")
    if data_type == "float":
        if isinstance(v, (bool, int, float)):
            return float(v), []
        raise TypeError("not a number")
    return (v if isinstance(v, str) else json.dumps(plain(v)) if not isinstance(plain(v), str) else plain(v)), []


class OpcUaConnector(Connector):
    type = "opcua"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": True}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.client: UaClient | None = None
        self.task: asyncio.Task | None = None
        self.state = "connecting"
        self.handles: dict[int, object] = {}            # client handle -> Point
        self.subs: list[int] = []
        self.stats = {"notifications": 0, "publish": 0, "keepalives": 0, "reads": 0, "reconnects": 0,
                      "bad_items": 0, "type_errors": 0}
        self.bad_items: dict[str, str] = {}

    # ---------------------------------------------------------------- configuration
    def device_of(self, address: dict) -> str:
        return DEVICE

    def devices(self, config: dict) -> list[str]:
        return [DEVICE]

    async def validate(self, config: dict) -> list[str]:
        conn, errs = config["connection"], []
        pol, mode = conn.get("security_policy", "Basic256Sha256"), conn.get("security_mode", "SignAndEncrypt")
        if (pol == "None") != (mode == "None"):
            errs.append("security_mode 'None' goes with security_policy 'None', and only with it")
        for p in config["points"]:
            try:
                NodeId.parse(p["address"]["node_id"])
            except (UaError, ValueError) as exc:
                errs.append(f"{p['tag_id']}: {exc}")
        return errs

    def _acq(self, config: dict | None = None) -> dict:
        return (config or self.config).get("acquisition") or {}

    def _client(self, config: dict) -> UaClient:
        conn = config["connection"]
        pol = conn.get("security_policy", "Basic256Sha256")
        mode = conn.get("security_mode", "SignAndEncrypt")
        cert = key = None
        if pol != "None":
            cert, key = self.secret(conn["client_cert_ref"]), self.secret(conn["client_key_ref"])
        return UaClient(conn["endpoint_url"], ReadOnlyGuard("opcua", ALLOW["opcua"], on_block=self._blocked), pol, mode,
                        cert, key, conn.get("trusted_server_thumbprints") or [], conn.get("insecure_skip_verify", False),
                        self._acq(config).get("request_timeout_ms", 3000) / 1000, conn.get("session_timeout_ms", 60000))

    def _user(self, config: dict) -> dict:
        conn = config["connection"]
        auth = conn.get("auth", "anonymous")
        if auth == "username":
            return {"type": "username", "username": conn["username"], "password": self.secret(conn["password_ref"])}
        if auth == "certificate":
            return {"type": "certificate", "cert_pem": self.secret(conn["user_cert_ref"]),
                    "key_pem": self.secret(conn["user_key_ref"])}
        return {"type": "anonymous"}

    def _blocked(self, request: str) -> None:
        if self.sink:
            self.sink.event("guard.blocked", "critical", f"Blocked OPC UA {request}", attrs={"request": request})

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
        self.task = asyncio.create_task(self._run())

    async def apply_points(self, config: dict, table: PointTable) -> bool:
        return False            # monitored items are rebuilt on a fresh session; reconnecting is quick and simple

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
                self.client = self._client(self.config)
                await self.client.connect(self._user(self.config))
                connected_at = time.monotonic()
                self._set_state("connected")
                if self._acq().get("mode", "subscribe") == "poll":
                    await self._poll()
                else:
                    await self._subscribe()
            except UaServiceError as exc:
                self._set_state("unreachable", exc.as_connector_error())
            except ConnectorError as exc:
                self._set_state("unreachable", exc)
            except (UaError, S.SecurityError) as exc:
                self._set_state("unreachable", ConnectorError("PROTOCOL_ERROR", str(exc)))
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

    # ---------------------------------------------------------------- values
    def _report(self, p, dv: U.DataValue) -> None:
        q = quality(dv.status)
        ts = iso(dv.source_ts) or iso(dv.server_ts)
        if Q.is_bad(q):
            self.sink.value(p.tag_id, None, q, ts=ts, flags=[status_name(dv.status)])
            return
        try:
            v, flags = coerce(dv.value, p.data_type)
        except TypeError as exc:
            self.stats["type_errors"] += 1
            self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, ts=ts, flags=["TYPE_MISMATCH"])
            self.bad_items[p.tag_id] = f"TYPE_MISMATCH: {exc}"
            return
        self.sink.value(p.tag_id, v, Q.BAD if v is None else q, ts=ts, flags=flags)

    async def _poll(self) -> None:
        classes = self.table.scan_classes()
        tasks = [asyncio.create_task(self._poll_class(ms, pts)) for ms, pts in classes.items()]
        tasks.append(asyncio.create_task(self.client.closed.wait()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    raise t.exception()
            raise self.client.close_reason or ConnectorError("CONN_CLOSED", "connection to the OPC UA server closed")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_class(self, scan_ms: int, pts: list) -> None:
        nodes = [NodeId.parse(p.address["node_id"]) for p in pts]
        while True:
            now = time.time() * 1000
            await asyncio.sleep(((-now) % scan_ms) / 1000)
            t0 = time.monotonic()
            results = await self._read_resolved(nodes)
            self.stats["reads"] += 1
            for p, dv in zip(pts, results):
                self._report(p, dv)
            self.sink.device_ok(DEVICE)
            if (time.monotonic() - t0) * 1000 > scan_ms:
                self.sink.scan_overrun(scan_ms)

    async def _read_resolved(self, nodes: list[NodeId]) -> list[U.DataValue]:
        """Read, answering Bad_NodeIdUnknown locally for nsu= URIs the server does not know."""
        known = [i for i, n in enumerate(nodes) if n.nsu is None or n.nsu in self.client.namespaces]
        out = [U.DataValue(status=0x80340000) for _ in nodes]
        for i, dv in zip(known, await self.client.read([nodes[i] for i in known]) if known else []):
            out[i] = dv
        return out

    async def _subscribe(self) -> None:
        acq = self._acq()
        interval = acq.get("publishing_interval_ms", 1000)
        sampling = acq.get("sampling_interval_ms", -1)
        queue = acq.get("queue_size", 10)
        per_sub = acq.get("max_items_per_subscription", 1000)
        pts = list(self.table.points.values())
        self.handles, self.subs, self.bad_items = {}, [], {}
        revised = interval
        for i in range(0, len(pts), per_sub):
            sub_id, revised = await self.client.create_subscription(interval)
            self.subs.append(sub_id)
            items = []
            for j, p in enumerate(pts[i:i + per_sub], start=i + 1):
                node = NodeId.parse(p.address["node_id"])
                if node.nsu is not None and node.nsu not in self.client.namespaces:
                    self._item_failed(p, 0x80340000)
                    continue
                self.handles[j] = p
                items.append((j, node, sampling, queue))
            for (h, *_), res in zip(items, await self.client.monitor(sub_id, items) if items else []):
                if res["status_code"] & 0x80000000:
                    self._item_failed(self.handles.pop(h), res["status_code"])
        if not self.subs:
            await self.client.closed.wait()
            raise self.client.close_reason or ConnectorError("CONN_CLOSED", "connection closed")
        keepalive_s = revised / 1000 * 10
        acks: list[tuple[int, int]] = []
        while True:
            try:
                res = await self.client.publish(acks, timeout=keepalive_s * 3 + self.client.timeout)
            except UaServiceError as exc:
                if exc.status & 0xFFFF0000 == 0x80790000:          # Bad_NoSubscription: server dropped them
                    raise ConnectorError("CONN_CLOSED", "the server deleted the subscriptions") from None
                raise
            self.stats["publish"] += 1
            self.sink.device_ok(DEVICE)
            msg = res["notification_message"]
            data = msg["notification_data"] or []
            acks = [(res["subscription_id"], msg["sequence_number"])] if data else []
            if not data:
                self.stats["keepalives"] += 1
            for ext in data:
                name, body = U.from_extension(ext)
                if name == "DataChangeNotification":
                    for item in body["monitored_items"] or []:
                        p = self.handles.get(item["client_handle"])
                        if p is not None:
                            self.stats["notifications"] += 1
                            self._report(p, item["value"])
                elif name == "StatusChangeNotification":
                    raise ConnectorError("CONN_CLOSED", f"subscription ended: {status_name(body['status'])}")

    def _item_failed(self, p, status: int) -> None:
        self.stats["bad_items"] += 1
        self.bad_items[p.tag_id] = status_name(status)
        self.sink.value(p.tag_id, None, quality(status), flags=[status_name(status)])

    # ---------------------------------------------------------------- admin operations
    async def _session(self, config: dict) -> UaClient:
        client = self._client(config)
        try:
            await client.connect(self._user(config))
        except BaseException:
            await client.close()
            raise
        return client

    async def test_connection(self, config: dict) -> dict:
        t0 = time.monotonic()
        entry: dict = {"id": DEVICE}
        client = None
        try:
            client = self._client(config)
            info = await client.connect(self._user(config))
            dvs = await client.read([SERVER_STATE, PRODUCT_NAME])
            states = {0: "Running", 1: "Failed", 2: "NoConfiguration", 3: "Suspended", 4: "Shutdown", 5: "Test",
                      6: "CommunicationFault", 7: "Unknown"}
            info["server_state"] = states.get(dvs[0].value.value if dvs[0].value else 7, "Unknown")
            if dvs[1].value and dvs[1].value.value:
                info["product"] = plain(dvs[1].value.value)
            info["namespaces"] = client.namespaces[1:]
            entry.update(ok=info["server_state"] == "Running", info=info)
            if not entry["ok"]:
                entry["error"] = {"code": "PROTOCOL_ERROR", "message": f"server state is {info['server_state']}"}
        except (ConnectorError, UaServiceError, UaError, S.SecurityError) as exc:
            err = exc.as_connector_error() if isinstance(exc, UaServiceError) else \
                exc if isinstance(exc, ConnectorError) else ConnectorError("PROTOCOL_ERROR", str(exc))
            entry.update(ok=False, error=err.as_dict())
            if client and client.endpoints:                      # lets the user check and trust the server
                entry["info"] = {"endpoints": _offered(client.endpoints)}
                if client.server_cert:
                    entry["info"]["server_thumbprint"] = S.thumbprint(client.server_cert)
                    entry["info"]["server_certificate"] = _cert_summary(client.server_cert)
        finally:
            if client:
                await client.close()
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return {"devices": [entry]}

    async def browse(self, args: dict) -> dict:
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        max_items = min(args.get("max_items", 500), 5000)
        depth = min(max(args.get("depth", 1), 1), 3)
        start = NodeId.parse(args["path"]) if args.get("path") else NodeId(85)
        client = await self._session(cfg)
        try:
            items: list[dict] = []
            await self._browse_level(client, start, depth, items, max_items)
            return {"items": items[:max_items]}
        except UaServiceError as exc:
            raise exc.as_connector_error() from None
        finally:
            await client.close()

    async def _browse_level(self, client: UaClient, node: NodeId, depth: int, items: list, max_items: int) -> None:
        refs = [r for r in await client.browse(node) if r["node_class"] in (1, 2)]
        variables = [r for r in refs if r["node_class"] == 2]
        info: dict[int, tuple] = {}
        if variables:
            ids = [NodeId(r["node_id"].identifier, r["node_id"].ns) for r in variables]
            types = await client.read(ids, ATTR_DATATYPE)
            values = await client.read(ids, ATTR_VALUE)
            info = {id(r): (t, v) for r, t, v in zip(variables, types, values)}
        for r in refs:
            if len(items) >= max_items:
                return
            nid = client.with_uri(NodeId(r["node_id"].identifier, r["node_id"].ns))
            name = r["display_name"].text or r["browse_name"].name or str(nid)
            if r["node_class"] == 1:
                folder = r["type_definition"].ns == 0 and r["type_definition"].identifier == FOLDER_TYPE
                items.append({"path": str(nid), "name": name, "kind": "folder" if folder else "object",
                              "has_children": True, "info": {"node_class": "Object"}})
                if depth > 1:
                    await self._browse_level(client, nid, depth - 1, items, max_items)
                continue
            t, v = info.get(id(r), (None, None))
            item = {"path": str(nid), "name": name, "kind": "variable", "has_children": False,
                    "address": {"node_id": str(nid)}, "info": {"node_class": "Variable"}}
            dt = t.value.value if t and t.value and isinstance(t.value.value, NodeId) else None
            if dt is not None:
                item["info"]["data_type"] = U.TYPE_NAMES.get(dt.identifier, str(dt)) if dt.ns == 0 else str(dt)
                kind = DATATYPE_KIND.get(dt.identifier) if dt.ns == 0 else None
                if v and v.value and v.value.is_array:
                    kind = "string"
                if kind:
                    item["data_type"] = kind
            if v is not None:
                if v.status & 0x80000000:
                    item["info"]["status"] = status_name(v.status)
                elif v.value is not None:
                    item["info"]["sample"] = str(plain(v.value.value))[:80]
            items.append(item)

    async def read_now(self, args: dict) -> dict:
        wanted: list[tuple[str | None, str, str]] = []           # (tag id, node id, data type)
        for t in args.get("tag_ids") or []:
            p = self.table.points.get(t) if self.table else None
            if p is None:
                raise ConnectorError("POINT_NOT_FOUND", f"no point {t}")
            wanted.append((t, p.address["node_id"], p.data_type))
        for a in args.get("addresses") or []:
            wanted.append((None, a["address"]["node_id"], a.get("data_type", "float")))
        if len(wanted) > 100:
            raise ConnectorError("CFG_INVALID", "read_now takes at most 100 points")
        own = self.client is not None and self.state == "connected" and not self.client.closed.is_set()
        client = self.client if own else await self._session(self.config)
        saved = self.client
        try:
            self.client = client
            dvs = await self._read_resolved([NodeId.parse(n) for _, n, _ in wanted])
        except UaServiceError as exc:
            raise exc.as_connector_error() from None
        finally:
            self.client = saved
            if not own:
                await client.close()
        now = iso(datetime.now(timezone.utc))
        out = []
        for (tag, node, dt), dv in zip(wanted, dvs):
            q = quality(dv.status)
            try:
                v = None if Q.is_bad(q) else coerce(dv.value, dt)[0]
            except TypeError:
                v, q = None, Q.BAD_CONFIG_ERROR
            entry = {"v": v, "q": q, "ts": iso(dv.source_ts) or iso(dv.server_ts) or now}
            entry.update({"t": tag} if tag else {"address": {"node_id": node}})
            out.append(entry)
        return {"values": out}

    async def diagnostics(self) -> dict:
        c = self.client
        return {"connected": self.state == "connected", **self.stats, "subscriptions": len(self.subs),
                "monitored_items": len(self.handles), "bad_items": self.bad_items,
                "security": f"{c.policy.name}/{c.mode}" if c else None, "requests": c.requests if c else 0,
                "channel_renewals": c.renewals if c else 0, "namespaces": c.namespaces if c else []}


def _offered(endpoints: list[dict]) -> list[str]:
    out = []
    for e in endpoints:
        p = S.BY_URI.get(e["security_policy_uri"])
        mode = {1: "None", 2: "Sign", 3: "SignAndEncrypt"}.get(e["security_mode"], "?")
        out.append(f"{p.name if p else e['security_policy_uri']}/{mode}")
    return sorted(set(out))


def _cert_summary(der: bytes) -> dict:
    try:
        c = S.load_cert(der)
        return {"subject": c.subject.rfc4514_string(), "issuer": c.issuer.rfc4514_string(),
                "not_after": c.not_valid_after_utc.strftime("%Y-%m-%d"), "application_uri": S.application_uri(c)}
    except ValueError:
        return {}
