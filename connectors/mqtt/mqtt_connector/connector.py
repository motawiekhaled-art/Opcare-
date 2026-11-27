"""MQTT subscriber connector (spec OSS-DAQ-CON-001 section 15.3), built on opcare_connector_sdk.

Subscribes to a client's broker (smart meters, IoT gateways, Sparkplug B edge nodes such as Ignition Edge) and
turns JSON, plain values and Sparkplug B metrics into platform tags. Read-only: it never publishes, not even
Sparkplug rebirth commands; it waits for the next NBIRTH instead.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

from opcare_connector_sdk import ALLOW, Connector, ConnectorError, PointTable, Q, ReadOnlyGuard

from . import packets as P
from . import sparkplug as SP
from . import values as V
from .client import MqttClient, parse_url, tls_context

DEVICE = "broker"


@dataclass
class _Node:
    """What we know about one Sparkplug edge node."""
    aliases: dict[int, tuple[str | None, str]] = field(default_factory=dict)   # alias -> (device, metric)
    online: bool = False
    devices_online: set = field(default_factory=set)
    last_seq: int | None = None
    births: int = 0
    deaths: int = 0


@dataclass
class _Seen:
    count: int = 0
    sample: bytes = b""
    retained: bool = False
    metrics: dict = field(default_factory=dict)       # sparkplug: name -> (datatype, sample)


class MqttConnector(Connector):
    type = "mqtt"
    version = "1.0.0"
    capabilities = {"browse": True, "read_now": False}

    def __init__(self):
        self.sink = None
        self.config: dict = {}
        self.table: PointTable | None = None
        self.client: MqttClient | None = None
        self.task: asyncio.Task | None = None
        self.by_topic: dict[str, list] = {}
        self.wild: list = []
        self.by_metric: dict[tuple, list] = {}
        self.nodes: dict[tuple[str, str], _Node] = {}
        self.state = "connecting"
        self.stats = {"messages": 0, "parse_errors": 0, "unknown_alias": 0, "reconnects": 0, "unmatched": 0}

    # ---------------------------------------------------------------- configuration
    def device_of(self, address: dict) -> str:
        return DEVICE

    def devices(self, config: dict) -> list[str]:
        return [DEVICE]

    async def validate(self, config: dict) -> list[str]:
        conn, errs = config["connection"], []
        try:
            _, _, tls = parse_url(conn["broker_url"])
        except ConnectorError as exc:
            return [exc.message]
        auth = conn.get("auth", "anonymous")
        if auth == "username" and not (conn.get("username") and conn.get("password_ref")):
            errs.append("auth 'username' needs username and password_ref")
        if auth == "certificate" and not (conn.get("client_cert_ref") and conn.get("client_key_ref")):
            errs.append("auth 'certificate' needs client_cert_ref and client_key_ref")
        if auth == "certificate" and not tls:
            errs.append("auth 'certificate' needs an mqtts:// broker_url")
        filters = [s["topic_filter"] for s in conn["subscriptions"]]
        for f in filters:
            parts = f.split("/")
            if any("#" in x and x != "#" for x in parts) or ("#" in parts and parts.index("#") != len(parts) - 1):
                errs.append(f"topic filter '{f}': # must be a whole last level")
            if any("+" in x and x != "+" for x in parts):
                errs.append(f"topic filter '{f}': + must be a whole level")
        for p in config["points"]:
            a, tag = p["address"], p["tag_id"]
            if a["format"] == "sparkplug_b":
                probe = f"spBv1.0/{a['group_id']}/{'DDATA' if a.get('device_id') else 'NDATA'}/{a['edge_node_id']}" + \
                        (f"/{a['device_id']}" if a.get("device_id") else "")
                if not any(P.topic_matches(f, probe) for f in filters):
                    errs.append(f"{tag}: no subscription covers Sparkplug node {a['group_id']}/{a['edge_node_id']} "
                                f"(add e.g. spBv1.0/{a['group_id']}/#)")
            elif not any(P.topic_matches(f, a["topic"]) or f == a["topic"] for f in filters):
                errs.append(f"{tag}: topic '{a['topic']}' is not covered by any subscription")
        return errs

    def _client(self, config: dict, on_message=None, suffix: str = "") -> MqttClient:
        conn = config["connection"]
        _, _, tls = parse_url(conn["broker_url"])
        ctx = None
        if tls:
            read = lambda k: self.secret(conn[k]) if conn.get(k) else None  # noqa: E731
            ctx = tls_context(read("ca_cert_ref"), read("client_cert_ref"), read("client_key_ref"),
                              conn.get("insecure_skip_verify", False))
        auth = conn.get("auth", "anonymous")
        user = conn.get("username") if auth == "username" else None
        pw = self.secret(conn["password_ref"]) if auth == "username" else None
        cid = conn.get("client_id") or f"opcare-{config['source_id']}"
        return MqttClient(conn["broker_url"], ReadOnlyGuard("mqtt", ALLOW["mqtt"], on_block=self._blocked),
                          P.V5 if conn.get("protocol_version", "5") == "5" else P.V311, cid + suffix, user, pw,
                          conn.get("keepalive_s", 60), ctx, on_message)

    def _blocked(self, request: str) -> None:
        if self.sink:
            self.sink.event("guard.blocked", "critical", f"Blocked MQTT {request}", attrs={"request": request})

    def _index(self) -> None:
        self.by_topic, self.wild, self.by_metric = {}, [], {}
        for p in self.table.points.values():
            a = p.address
            if a["format"] == "sparkplug_b":
                self.by_metric.setdefault((a["group_id"], a["edge_node_id"], a.get("device_id"), a["metric"]), []).append(p)
            elif "+" in a["topic"] or "#" in a["topic"]:
                self.wild.append(p)
            else:
                self.by_topic.setdefault(a["topic"], []).append(p)

    @property
    def timeout_s(self) -> float:
        return (self.config.get("acquisition") or {}).get("request_timeout_ms", 3000) / 1000

    # ---------------------------------------------------------------- lifecycle
    async def start(self, config: dict, table: PointTable, sink) -> None:
        self.sink, self.config, self.table = sink, config, table
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
        if self.client:
            await self.client.close()

    async def _run(self) -> None:
        delay = 1.0
        while True:
            connected_at = None
            try:
                self.client = self._client(self.config, self._on_message)
                await self.client.connect(self.timeout_s)
                codes = await self.client.subscribe([(s["topic_filter"], s.get("qos", 1)) for s in
                                                     self.config["connection"]["subscriptions"]], self.timeout_s)
                refused = [s["topic_filter"] for s, c in zip(self.config["connection"]["subscriptions"], codes) if c >= 0x80]
                if refused:
                    self.sink.event("mqtt.subscribe_refused", "medium", f"Broker refused subscription {', '.join(refused)}",
                                    attrs={"filters": refused})
                connected_at = time.monotonic()
                self._set_state("connected")
                await self.client.closed.wait()
                raise self.client.close_reason or ConnectorError("CONN_CLOSED", "connection to the broker closed")
            except ConnectorError as exc:
                self._set_state("unreachable", exc)
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

    # ---------------------------------------------------------------- messages
    def _on_message(self, topic: str, payload: bytes, retained: bool) -> None:
        self.stats["messages"] += 1
        self.sink.device_ok(DEVICE)
        sp = SP.parse_topic(topic) if self.by_metric else None
        if sp:
            self._sparkplug(sp, payload)
            return
        pts = self.by_topic.get(topic, []) + [p for p in self.wild if P.topic_matches(p.address["topic"], topic)]
        if not pts:
            self.stats["unmatched"] += 1
            return
        doc, doc_error = None, None
        if any(p.address["format"] == "json" for p in pts):
            try:
                doc = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                doc_error = True
        for p in pts:
            a = p.address
            try:
                if a["format"] == "json":
                    if doc_error:
                        raise V.ParseError("not JSON")
                    raw = V.json_pointer(doc, a.get("value_pointer", ""))
                    ts = V.timestamp(V.json_pointer(doc, a["timestamp_pointer"]), a.get("timestamp_format")) \
                        if a.get("timestamp_pointer") else None
                else:
                    raw, ts = V.raw_value(payload.decode("utf-8", errors="replace")), None
                self.sink.value(p.tag_id, V.coerce(raw, p.data_type), Q.GOOD, ts=ts)
            except V.ParseError:
                self.stats["parse_errors"] += 1
                self.sink.value(p.tag_id, None, Q.BAD, flags=["PARSE_ERROR"])

    def _sparkplug(self, sp: tuple, payload: bytes) -> None:
        group, mtype, node_id, device = sp
        if mtype in ("NCMD", "DCMD", "STATE"):
            return
        node = self.nodes.setdefault((group, node_id), _Node())
        if mtype == "NDEATH":
            node.online, node.deaths = False, node.deaths + 1
            node.devices_online.clear()
            self._offline(group, node_id, None)
            return
        if mtype == "DDEATH":
            node.devices_online.discard(device)
            self._offline(group, node_id, device)
            return
        try:
            pl = SP.decode(payload)
        except SP.SparkplugError:
            self.stats["parse_errors"] += 1
            return
        if mtype == "NBIRTH":
            node.aliases = {a: v for a, v in node.aliases.items() if v[0] is not None}
            node.online, node.births = True, node.births + 1
        elif mtype == "DBIRTH":
            node.devices_online.add(device)
        for m in pl.metrics:
            if mtype in ("NBIRTH", "DBIRTH") and m.alias is not None and m.name:
                node.aliases[m.alias] = (device, m.name)
            name = m.name
            if name is None and m.alias is not None:
                known = node.aliases.get(m.alias)
                if not known:
                    self.stats["unknown_alias"] += 1             # births not seen yet: wait for the next NBIRTH
                    continue
                name = known[1]
            pts = self.by_metric.get((group, node_id, device, name))
            if not pts:
                continue
            ms = m.timestamp or pl.timestamp
            ts = V.iso_ms(ms) if ms else None
            for p in pts:
                if not m.supported:
                    self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, flags=["UNSUPPORTED_TYPE"])
                elif m.is_null or m.value is None:
                    self.sink.value(p.tag_id, None, Q.BAD, ts=ts, flags=["NULL"])
                else:
                    try:
                        self.sink.value(p.tag_id, V.coerce(m.value, p.data_type), Q.GOOD, ts=ts)
                    except V.ParseError:
                        self.sink.value(p.tag_id, None, Q.BAD_CONFIG_ERROR, flags=["TYPE_MISMATCH"])

    def _offline(self, group: str, node_id: str, device: str | None) -> None:
        """NDEATH / DDEATH: every metric of that node (and its devices) or of that device -> q 24 (spec 15.3)."""
        for (g, n, d, _), pts in self.by_metric.items():
            if g == group and n == node_id and (device is None or d == device):
                for p in pts:
                    self.sink.value(p.tag_id, None, Q.BAD_COMM_FAILURE, flags=["DEATH"])

    # ---------------------------------------------------------------- admin operations
    async def test_connection(self, config: dict) -> dict:
        t0 = time.monotonic()
        timeout = (config.get("acquisition") or {}).get("request_timeout_ms", 3000) / 1000
        try:
            client = self._client(config, suffix="-test")
        except ConnectorError as exc:
            return {"devices": [{"id": DEVICE, "ok": False, "error": exc.as_dict()}]}
        try:
            info = await client.connect(timeout)
            subs = config["connection"]["subscriptions"]
            codes = await client.subscribe([(s["topic_filter"], s.get("qos", 1)) for s in subs], timeout)
            info["subscriptions"] = {s["topic_filter"]: ("refused" if c >= 0x80 else f"QoS {c}") for s, c in zip(subs, codes)}
            ok = all(c < 0x80 for c in codes)
            entry = {"id": DEVICE, "ok": ok, "info": info}
            if not ok:
                entry["error"] = {"code": "AUTH_FAILED", "message": "the broker refused a subscription (access rights?)"}
        except ConnectorError as exc:
            entry = {"id": DEVICE, "ok": False, "error": exc.as_dict()}
        finally:
            await client.close()
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return {"devices": [entry]}

    async def browse(self, args: dict) -> dict:
        cfg = args.get("config") or self.config
        if not cfg:
            raise ConnectorError("CFG_INVALID", "no configuration to browse")
        filters = [args["path"]] if args.get("path") else [s["topic_filter"] for s in cfg["connection"]["subscriptions"]]
        listen = min(60, max(1, args.get("listen_s", 10)))
        seen: dict[str, _Seen] = {}

        aliases: dict[tuple, dict[int, str]] = {}

        def got(topic: str, payload: bytes, retained: bool) -> None:
            sp = SP.parse_topic(topic)
            if sp:                                    # one entry per edge node / device, all message types merged
                group, mtype, node_id, device = sp
                if mtype not in ("NBIRTH", "DBIRTH", "NDATA", "DDATA"):
                    return
                key = f"spBv1.0/{group}/{node_id}" + (f"/{device}" if device else "")
                s = seen.setdefault(key, _Seen())
                s.count += 1
                amap = aliases.setdefault((group, node_id), {})
                try:
                    for m in SP.decode(payload).metrics:
                        if m.name and m.alias is not None:
                            amap[m.alias] = m.name
                        name = m.name or amap.get(m.alias)
                        if name and name != "bdSeq":
                            s.metrics[name] = (m.datatype, m.value)
                except SP.SparkplugError:
                    pass
                s.sample = f"Sparkplug B {group} / {node_id}{' / ' + device if device else ''}: {len(s.metrics)} metric(s)".encode()
                return
            s = seen.setdefault(topic, _Seen())
            s.count += 1
            s.sample, s.retained = payload[:1024], retained
        client = self._client(cfg, got, suffix="-browse")
        try:
            await client.connect(self.timeout_s if self.config else 3)
            await client.subscribe([(f, 0) for f in filters], 5)
            end = time.monotonic() + listen
            while time.monotonic() < end and len(seen) < args.get("max_items", 500):
                await asyncio.sleep(0.2)
        finally:
            await client.close()
        items = []
        for topic, s in sorted(seen.items()):
            sp = topic.split("/") if topic.startswith("spBv1.0/") else None      # merged node / device entry
            text = s.sample.decode("utf-8", errors="replace")
            fmt = "sparkplug_b" if sp else "json" if _is_json(s.sample) else "raw"
            items.append({"path": topic, "name": topic, "kind": "object", "has_children": fmt != "raw",
                          "info": {"messages": s.count, "format": fmt, "retained": s.retained, "sample": text[:256]}})
            if fmt == "json":
                for ptr, v in V.leaves(json.loads(s.sample)):
                    items.append(_var(topic, ptr, v, {"topic": topic, "format": "json", "value_pointer": ptr}))
            elif fmt == "raw":
                items[-1]["address"] = {"topic": topic, "format": "raw"}
                items[-1]["data_type"] = _kind(V.raw_value(text))
            elif sp:
                group, node_id, device = sp[1], sp[2], sp[3] if len(sp) > 3 else None
                for name, (dt, sample) in sorted(s.metrics.items()):
                    addr = {"format": "sparkplug_b", "group_id": group, "edge_node_id": node_id, "metric": name}
                    if device:
                        addr["device_id"] = device
                    items.append({**_var(topic, name, sample, addr), "info": {"sample": str(sample)[:80],
                                                                               "type": SP.TYPE_NAMES.get(dt, str(dt))}})
                    if dt in SP.SIGNED_BITS or dt in (SP.UINT8, SP.UINT16, SP.UINT32, SP.UINT64):
                        items[-1]["data_type"] = "int"
        return {"items": items[:args.get("max_items", 500)]}

    async def diagnostics(self) -> dict:
        c = self.client
        return {"connected": self.state == "connected", "packets_sent": sorted(set(c.sent)) if c else [], **self.stats,
                "sparkplug_nodes": {f"{g}/{n}": {"online": x.online, "devices_online": sorted(x.devices_online),
                                                 "aliases": len(x.aliases), "births": x.births, "deaths": x.deaths}
                                    for (g, n), x in self.nodes.items()}}


def _is_json(data: bytes) -> bool:
    try:
        return isinstance(json.loads(data), (dict, list))
    except (ValueError, UnicodeDecodeError):
        return False


def _kind(v) -> str:
    return "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float) else "string"


def _var(topic: str, name: str, sample, address: dict) -> dict:
    return {"path": f"{topic}|{name}", "name": name, "kind": "variable", "has_children": False,
            "data_type": _kind(sample), "address": address, "info": {"sample": str(sample)[:80]}}
