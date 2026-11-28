"""Small OPC UA server for tests and the demo, built from the connector's own stack in the server role.

Supports the services a read-only client uses: GetEndpoints, OpenSecureChannel (issue and renew), Create/Activate/
CloseSession (anonymous, username with or without an encrypted password, X509 user certificate), Read, Browse,
BrowseNext, CreateSubscription, CreateMonitoredItems, Publish (data changes and keep-alives), DeleteSubscriptions,
CloseSecureChannel. Security policies: None, Basic256Sha256, Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss, in Sign
and SignAndEncrypt.

It records the name of every request it receives (`requests`), so tests can prove that the client sends nothing
outside the read-only allow-list. Any other service is answered with Bad_ServiceUnsupported.

    srv = OpcUaServer(namespace="urn:opcare:sim:plant")
    srv.add_variable("Boiler.Temp", 21.5, folder="Boiler")
    await srv.start()                 # srv.url -> opc.tcp://127.0.0.1:<port>/opcare
    srv.set("Boiler.Temp", 22.0)
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "connectors" / "opcua"))

from opcua_connector import security as S  # noqa: E402
from opcua_connector import uabin as U  # noqa: E402
from opcua_connector.channel import HEADER, SecureChannel, ack, error, parse_hello  # noqa: E402
from opcua_connector.uabin import DataValue, LocalizedText, NodeId, QualifiedName, Variant  # noqa: E402

GOOD, BAD_NODE_UNKNOWN, BAD_ATTRIBUTE, BAD_SERVICE = 0, 0x80340000, 0x80350000, 0x800B0000
BAD_SESSION, BAD_USER, BAD_IDENTITY, BAD_SECURITY = 0x80250000, 0x801F0000, 0x80210000, 0x80130000
BAD_NO_SUB, BAD_CONTINUATION = 0x80790000, 0x804A0000
HIERARCHICAL = {33, 35, 47, 46}            # HierarchicalReferences, Organizes, HasComponent, HasProperty
OBJECT, VARIABLE = 1, 2
TYPE_OF = {bool: U.BOOLEAN, int: U.INT32, float: U.DOUBLE, str: U.STRING}
TOKEN_IDS = {"anonymous": 0, "username": 1, "certificate": 2}


@dataclass
class Node:
    node_id: NodeId
    node_class: int
    name: str
    value: Variant | None = None
    status: int = GOOD
    source_ts: datetime | None = None
    data_type: NodeId | None = None
    type_definition: NodeId = field(default_factory=lambda: NodeId(58))       # BaseObjectType
    children: list = field(default_factory=list)
    ref_type: int = 35


@dataclass
class Item:
    handle: int
    node: NodeId
    attribute: int
    last: tuple | None = None
    dirty: bool = True


@dataclass
class Subscription:
    sub_id: int
    interval: float
    keepalive: int
    items: dict = field(default_factory=dict)          # monitored item id -> Item
    seq: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Session:
    token: NodeId
    nonce: bytes
    client_cert: bytes | None
    activated: bool = False
    user: str | None = None
    subs: dict = field(default_factory=dict)
    channel: object = None


class OpcUaServer:
    def __init__(self, namespace: str = "urn:opcare:sim", host: str = "127.0.0.1", port: int = 0, path: str = "/opcare",
                 policies: list[tuple[str, str]] | None = None, users: dict[str, str] | None = None,
                 anonymous: bool = True, x509_users: list[str] | None = None, key_size: int = 2048,
                 product: str = "Opcare OPC UA test server", min_lifetime_ms: int = 1000):
        self.host, self.port, self.path = host, port, path
        self.namespaces = ["http://opcfoundation.org/UA/", namespace]
        self.policies = policies or [("None", "None")]
        self.users, self.anonymous = users or {}, anonymous
        self.x509_users = {t.upper() for t in (x509_users or [])}
        self.product, self.min_lifetime_ms = product, min_lifetime_ms
        self.cert_pem, self.key_pem = S.make_application_certificate("urn:opcare:sim:server", "Opcare OPC UA test server",
                                                                     key_size=key_size)
        self.cert_der, self.key = S.der(S.load_cert(self.cert_pem)), S.load_key(self.key_pem)
        self.thumbprint = S.thumbprint(self.cert_der)
        self.nodes: dict[NodeId, Node] = {}
        self.requests: list[str] = []
        self.sessions: dict[NodeId, Session] = {}
        self.writers: set = set()
        self.server: asyncio.base_events.Server | None = None
        self.next_id = 1
        self.channel_ids = 0
        self.renewals = 0
        self.browse_page = 0                              # >0: force continuation points of that size
        self._continuations: dict[bytes, list] = {}
        self._build_base()

    # ------------------------------------------------------------ address space
    def _add(self, node: Node, parent: NodeId | None) -> Node:
        self.nodes[node.node_id] = node
        if parent is not None:
            self.nodes[parent].children.append(node.node_id)
        return node

    def _build_base(self) -> None:
        self._add(Node(NodeId(84), OBJECT, "Root", type_definition=NodeId(61)), None)
        self._add(Node(NodeId(85), OBJECT, "Objects", type_definition=NodeId(61), ref_type=35), NodeId(84))
        self._add(Node(NodeId(2253), OBJECT, "Server", type_definition=NodeId(2004)), NodeId(85))
        self._add(Node(NodeId(2255), VARIABLE, "NamespaceArray", Variant(U.STRING, self.namespaces, True),
                       data_type=NodeId(12), ref_type=46), NodeId(2253))
        self._add(Node(NodeId(2256), VARIABLE, "ServerStatus", data_type=NodeId(862)), NodeId(2253))
        self._add(Node(NodeId(2259), VARIABLE, "State", Variant(U.INT32, 0), data_type=NodeId(852)), NodeId(2256))
        self._add(Node(NodeId(2261), VARIABLE, "ProductName", Variant(U.STRING, self.product), data_type=NodeId(12)),
                  NodeId(2256))

    def folder(self, path: str) -> NodeId:
        """Create (or find) folders 'A/B' under Objects. Returns the NodeId of the last one."""
        parent = NodeId(85)
        for i, part in enumerate(path.split("/")):
            nid = NodeId("/".join(path.split("/")[:i + 1]), 1)
            if nid not in self.nodes:
                self._add(Node(nid, OBJECT, part, type_definition=NodeId(61)), parent)
            parent = nid
        return parent

    def add_variable(self, ident, value, folder: str | None = None, vtype: int | None = None, name: str | None = None,
                     status: int = GOOD) -> NodeId:
        nid = ident if isinstance(ident, NodeId) else NodeId(ident, 1)
        parent = self.folder(folder) if folder else NodeId(85)
        t = vtype or (U.DOUBLE if isinstance(value, float) else TYPE_OF.get(type(value), U.DOUBLE))
        is_array = isinstance(value, list)
        self._add(Node(nid, VARIABLE, name or str(nid.identifier).split(".")[-1], Variant(t, value, is_array),
                       status=status, source_ts=datetime.now(timezone.utc), data_type=NodeId(t),
                       type_definition=NodeId(63)), parent)
        return nid

    def set(self, ident, value=None, status: int = GOOD, ts: datetime | None = None) -> None:
        nid = ident if isinstance(ident, NodeId) else NodeId(ident, 1)
        n = self.nodes[nid]
        if value is not None:
            n.value = Variant(n.value.type if n.value and n.value.type else TYPE_OF.get(type(value), U.DOUBLE), value,
                              isinstance(value, list))
        n.status, n.source_ts = status, ts or datetime.now(timezone.utc)
        for s in self.sessions.values():
            for sub in s.subs.values():
                if any(i.node == nid for i in sub.items.values()):
                    sub.changed.set()

    # ------------------------------------------------------------ network
    @property
    def url(self) -> str:
        return f"opc.tcp://{self.host}:{self.port}{self.path}"

    async def start(self) -> "OpcUaServer":
        self.server = await asyncio.start_server(self._conn, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for w in list(self.writers):
            w.close()
        if self.server:
            await self.server.wait_closed()

    def kick_all(self) -> None:
        for w in list(self.writers):
            w.close()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    async def _read(self, reader) -> tuple[bytes, bytes, bytes]:
        head = await reader.readexactly(8)
        mtype, ctype, size = HEADER.unpack(head)
        return mtype, ctype, head + await reader.readexactly(size - 8)

    async def _conn(self, reader, writer) -> None:
        self.writers.add(writer)
        ch = SecureChannel(local_cert=self.cert_der, local_key=self.key)
        parts: dict[int, list] = {}
        lock = asyncio.Lock()
        tasks: set = set()
        try:
            mtype, _, chunk = await self._read(reader)
            if mtype != b"HEL":
                writer.write(error(0x80020000, "expected Hello"))
                return
            ch.remote_buffer = min(parse_hello(chunk[8:])["receive_buffer"], 65536)
            writer.write(ack())
            while True:
                mtype, ctype, chunk = await self._read(reader)
                if mtype == b"OPN":
                    req, body, info = ch.decode_open(chunk)
                    await self._open(ch, writer, req, body, info, lock)
                elif mtype == b"CLO":
                    ch.decode_msg(chunk)
                    self.requests.append("CloseSecureChannel")
                    return
                elif mtype == b"MSG":
                    req, body = ch.decode_msg(chunk)
                    parts.setdefault(req, []).append(body)
                    if ctype == b"F":
                        t = asyncio.create_task(self._service(ch, writer, req, b"".join(parts.pop(req)), lock))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
                    elif ctype == b"A":
                        parts.pop(req, None)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except S.SecurityError as exc:
            writer.write(error(BAD_SECURITY, str(exc)))
        finally:
            for t in tasks:
                t.cancel()
            self.writers.discard(writer)
            for s in list(self.sessions.values()):
                if s.channel is ch:
                    s.channel = None
            writer.close()

    async def _send(self, ch: SecureChannel, writer, req: int, name: str, obj: dict, lock, kind=b"MSG") -> None:
        body = U.encode_message(name, obj)
        async with lock:
            if writer.is_closing():
                return
            for c in ch.encode_msg(kind, req, body):
                writer.write(c)
            await writer.drain()

    @staticmethod
    def _rh(header: dict, status: int = GOOD) -> dict:
        return {"timestamp": datetime.now(timezone.utc), "request_handle": header["request_handle"],
                "service_result": status}

    async def _open(self, ch: SecureChannel, writer, req: int, body: bytes, info: dict, lock) -> None:
        name, obj = U.decode_message(body)
        self.requests.append("OpenSecureChannel")
        policy = info["policy"]
        offered = {(S.POLICIES[p].uri, S.MODES[m]) for p, m in self.policies}
        if (policy.uri, obj["security_mode"]) not in offered and not policy.none:
            writer.write(error(0x80550000, "security policy/mode not offered"))        # Bad_SecurityPolicyRejected
            return
        ch.policy, ch.mode = policy, obj["security_mode"]
        if not policy.none:
            ch.remote_cert = info["sender_cert"]
        renew = obj["request_type"] == 1
        if renew:
            self.renewals += 1
        else:
            self.channel_ids += 1
            ch.channel_id = self.channel_ids
        token = (ch.token_id or 0) + 1
        server_nonce = S.nonce(policy)
        lifetime = max(self.min_lifetime_ms, obj["requested_lifetime"]) if obj["requested_lifetime"] else 600000
        res = {"header": self._rh(obj["header"]), "server_protocol_version": 0,
               "security_token": {"channel_id": ch.channel_id, "token_id": token,
                                  "created_at": datetime.now(timezone.utc), "revised_lifetime": lifetime},
               "server_nonce": server_nonce if not policy.none else None}
        async with lock:
            writer.write(ch.encode_open(req, U.encode_message("OpenSecureChannelResponse", res)))
            ch.set_keys(server_nonce, obj["client_nonce"] or b"", token)

    # ------------------------------------------------------------ services
    def _endpoints(self) -> list[dict]:
        out = []
        tokens = []
        if self.anonymous:
            tokens.append({"policy_id": "anonymous", "token_type": 0})
        if self.users:
            tokens.append({"policy_id": "username", "token_type": 1})
        if self.x509_users:
            tokens.append({"policy_id": "certificate", "token_type": 2})
        for p, m in self.policies:
            pol = S.POLICIES[p]
            toks = [dict(t) for t in tokens]
            for t in toks:      # on an unsecured endpoint passwords are encrypted with Basic256Sha256
                if t["token_type"] in (1, 2) and pol.none:
                    t["security_policy_uri"] = S.POLICIES["Basic256Sha256"].uri
            out.append({"endpoint_url": self.url,
                        "server": {"application_uri": "urn:opcare:sim:server", "product_uri": "urn:opcare:sim",
                                   "application_name": LocalizedText(self.product), "application_type": 0,
                                   "discovery_urls": [self.url]},
                        "server_certificate": self.cert_der, "security_mode": S.MODES[m],
                        "security_policy_uri": pol.uri, "user_identity_tokens": toks,
                        "transport_profile_uri": "http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary",
                        "security_level": 0 if pol.none else 3})
        return out

    async def _service(self, ch: SecureChannel, writer, req: int, body: bytes, lock) -> None:
        try:
            name, obj = U.decode_message(body)
        except U.UaError:
            nid = U.Reader(body).nodeid()
            self.requests.append(f"unknown:{nid}")
            await self._send(ch, writer, req, "ServiceFault", {"header": {
                "timestamp": datetime.now(timezone.utc), "service_result": BAD_SERVICE}}, lock)
            return
        short = name.removesuffix("Request")
        self.requests.append(short)
        h = obj["header"]
        try:
            handler = getattr(self, f"_svc_{short}")
        except AttributeError:
            await self._send(ch, writer, req, "ServiceFault", {"header": self._rh(h, BAD_SERVICE)}, lock)
            return
        session = self.sessions.get(h["authentication_token"])
        if short not in ("GetEndpoints", "CreateSession", "ActivateSession"):
            if session is None or not session.activated or session.channel is not ch:
                await self._send(ch, writer, req, "ServiceFault", {"header": self._rh(h, BAD_SESSION)}, lock)
                return
        try:
            rname, res = await handler(ch, obj, session)
        except _Fault as f:
            await self._send(ch, writer, req, "ServiceFault", {"header": self._rh(h, f.status)}, lock)
            return
        res["header"] = self._rh(h)
        await self._send(ch, writer, req, rname, res, lock)

    async def _svc_GetEndpoints(self, ch, obj, session):
        return "GetEndpointsResponse", {"endpoints": self._endpoints()}

    async def _svc_CreateSession(self, ch, obj, session):
        token = NodeId(os.urandom(16), 0)
        nonce = os.urandom(32)
        s = Session(token, nonce, obj["client_certificate"], channel=ch)
        self.sessions[token] = s
        sig = {}
        if ch.secure:
            sig = {"algorithm": ch.policy.signature_algorithm,
                   "signature": ch.policy.asym_sign(self.key, (obj["client_certificate"] or b"") + (obj["client_nonce"] or b""))}
        return "CreateSessionResponse", {
            "session_id": NodeId(self.next_session_id(), 1), "authentication_token": token,
            "revised_session_timeout": obj["requested_session_timeout"], "server_nonce": nonce,
            "server_certificate": self.cert_der if ch.secure else None, "server_endpoints": self._endpoints(),
            "server_signature": sig, "max_request_message_size": 0}

    def next_session_id(self) -> int:
        self.next_id += 1
        return self.next_id

    async def _svc_ActivateSession(self, ch, obj, session):
        if session is None:
            raise _Fault(BAD_SESSION)
        if ch.secure:
            sig = obj["client_signature"] or {}
            try:
                ch.policy.asym_verify(S.load_cert(session.client_cert).public_key(), sig.get("signature") or b"",
                                      self.cert_der + session.nonce)
            except (S.SecurityError, ValueError, TypeError):
                raise _Fault(0x80580000) from None                    # Bad_ApplicationSignatureInvalid
        tname, tok = U.from_extension(obj["user_identity_token"])
        if tname == "AnonymousIdentityToken" and self.anonymous:
            session.user = None
        elif tname == "UserNameIdentityToken" and self.users:
            pw = tok["password"] or b""
            if tok["encryption_algorithm"]:
                p = next((x for x in S.POLICIES.values() if not x.none and x.password_algorithm == tok["encryption_algorithm"]),
                         None)
                if p is None:
                    raise _Fault(BAD_IDENTITY)
                try:
                    plain = p.asym_decrypt(self.key, pw)
                except S.SecurityError:
                    raise _Fault(BAD_IDENTITY) from None
                n = struct.unpack_from("<I", plain)[0]
                if plain[4 + n - len(session.nonce):4 + n] != session.nonce:
                    raise _Fault(BAD_IDENTITY)
                pw = plain[4:4 + n - len(session.nonce)]
            if self.users.get(tok["user_name"]) != pw.decode(errors="replace"):
                raise _Fault(BAD_USER)
            session.user = tok["user_name"]
        elif tname == "X509IdentityToken" and self.x509_users:
            cert = tok["certificate_data"]
            if S.thumbprint(cert) not in self.x509_users:
                raise _Fault(BAD_USER)
            usig = obj["user_token_signature"] or {}
            p = next((x for x in S.POLICIES.values() if not x.none and x.signature_algorithm == usig.get("algorithm")),
                     S.POLICIES["Basic256Sha256"])
            try:
                p.asym_verify(S.load_cert(cert).public_key(), usig.get("signature") or b"", self.cert_der + session.nonce)
            except S.SecurityError:
                raise _Fault(BAD_IDENTITY) from None
            session.user = "x509:" + S.thumbprint(cert)
        else:
            raise _Fault(BAD_IDENTITY)
        session.activated, session.channel = True, ch
        session.nonce = os.urandom(32)
        return "ActivateSessionResponse", {"server_nonce": session.nonce, "results": []}

    async def _svc_CloseSession(self, ch, obj, session):
        self.sessions.pop(session.token, None)
        return "CloseSessionResponse", {}

    def _attr(self, rv: dict) -> DataValue:
        n = self.nodes.get(rv["node_id"])
        if n is None:
            return DataValue(status=BAD_NODE_UNKNOWN)
        a = rv["attribute_id"]
        now = datetime.now(timezone.utc)
        if a == 1:
            return DataValue(Variant(U.NODEID, n.node_id))
        if a == 2:
            return DataValue(Variant(U.INT32, n.node_class))
        if a == 3:
            return DataValue(Variant(U.QUALIFIEDNAME, QualifiedName(n.name, n.node_id.ns)))
        if a == 4:
            return DataValue(Variant(U.LOCALIZEDTEXT, LocalizedText(n.name)))
        if n.node_class != VARIABLE:
            return DataValue(status=BAD_ATTRIBUTE)
        if a == 13:
            return DataValue(n.value, n.status, n.source_ts, now)
        if a == 14:
            return DataValue(Variant(U.NODEID, n.data_type or NodeId(24)))
        return DataValue(status=BAD_ATTRIBUTE)

    async def _svc_Read(self, ch, obj, session):
        return "ReadResponse", {"results": [self._attr(rv) for rv in obj["nodes_to_read"] or []]}

    def _refs(self, n: Node) -> list[dict]:
        out = []
        for cid in n.children:
            c = self.nodes[cid]
            out.append({"reference_type_id": NodeId(c.ref_type), "is_forward": True, "node_id": c.node_id,
                        "browse_name": QualifiedName(c.name, c.node_id.ns), "display_name": LocalizedText(c.name),
                        "node_class": c.node_class, "type_definition": c.type_definition})
        return out

    def _page(self, refs: list, limit: int) -> dict:
        size = min(x for x in (limit, self.browse_page, 1 << 30) if x)
        if len(refs) <= size:
            return {"status_code": GOOD, "continuation_point": None, "references": refs}
        cp = os.urandom(8)
        self._continuations[cp] = refs[size:]
        return {"status_code": GOOD, "continuation_point": cp, "references": refs[:size]}

    async def _svc_Browse(self, ch, obj, session):
        results = []
        limit = obj["requested_max_references_per_node"]
        for d in obj["nodes_to_browse"] or []:
            n = self.nodes.get(d["node_id"])
            if n is None:
                results.append({"status_code": BAD_NODE_UNKNOWN, "references": []})
                continue
            refs = [r for r in self._refs(n) if d["browse_direction"] in (0, 2)]
            results.append(self._page(refs, limit))
        return "BrowseResponse", {"results": results}

    async def _svc_BrowseNext(self, ch, obj, session):
        results = []
        for cp in obj["continuation_points"] or []:
            rest = self._continuations.pop(cp, None)
            if rest is None:
                results.append({"status_code": BAD_CONTINUATION, "references": []})
            elif obj["release_continuation_points"]:
                results.append({"status_code": GOOD, "references": []})
            else:
                results.append(self._page(rest, 0))
        return "BrowseNextResponse", {"results": results}

    async def _svc_CreateSubscription(self, ch, obj, session):
        self.next_id += 1
        interval = max(20.0, obj["requested_publishing_interval"])
        sub = Subscription(self.next_id, interval, max(1, obj["requested_max_keep_alive_count"]))
        session.subs[sub.sub_id] = sub
        return "CreateSubscriptionResponse", {"subscription_id": sub.sub_id, "revised_publishing_interval": interval,
                                              "revised_lifetime_count": obj["requested_lifetime_count"],
                                              "revised_max_keep_alive_count": sub.keepalive}

    async def _svc_CreateMonitoredItems(self, ch, obj, session):
        sub = session.subs.get(obj["subscription_id"])
        if sub is None:
            raise _Fault(0x80280000)
        results = []
        for it in obj["items_to_create"] or []:
            rv = it["item_to_monitor"]
            n = self.nodes.get(rv["node_id"])
            if n is None or n.node_class != VARIABLE:
                results.append({"status_code": BAD_NODE_UNKNOWN})
                continue
            self.next_id += 1
            sub.items[self.next_id] = Item(it["requested_parameters"]["client_handle"], rv["node_id"], rv["attribute_id"])
            results.append({"status_code": GOOD, "monitored_item_id": self.next_id,
                            "revised_sampling_interval": max(sub.interval, it["requested_parameters"]["sampling_interval"]),
                            "revised_queue_size": 1})
        sub.changed.set()
        return "CreateMonitoredItemsResponse", {"results": results}

    async def _svc_DeleteSubscriptions(self, ch, obj, session):
        res = [GOOD if session.subs.pop(i, None) else 0x80280000 for i in obj["subscription_ids"] or []]
        return "DeleteSubscriptionsResponse", {"results": res}

    async def _svc_Publish(self, ch, obj, session):
        if not session.subs:
            raise _Fault(BAD_NO_SUB)
        subs = list(session.subs.values())
        deadline = asyncio.get_running_loop().time() + min(s.interval * s.keepalive for s in subs) / 1000
        while True:
            for sub in subs:
                changes = []
                for item in sub.items.values():
                    n = self.nodes[item.node]
                    key = (n.value.value if n.value else None, n.status, n.source_ts)
                    if item.dirty or key != item.last:
                        item.last, item.dirty = key, False
                        changes.append({"client_handle": item.handle,
                                        "value": DataValue(n.value, n.status, n.source_ts, datetime.now(timezone.utc))})
                if changes:
                    sub.seq += 1
                    return "PublishResponse", self._notification(sub, [U.extension("DataChangeNotification",
                                                                                   {"monitored_items": changes})])
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                sub = subs[0]
                return "PublishResponse", self._notification(sub, [], keepalive=True)
            for sub in subs:
                sub.changed.clear()
            await asyncio.sleep(min(subs[0].interval / 1000, remaining))

    @staticmethod
    def _notification(sub: Subscription, data: list, keepalive: bool = False) -> dict:
        return {"subscription_id": sub.sub_id, "available_sequence_numbers": [],
                "more_notifications": False,
                "notification_message": {"sequence_number": sub.seq + 1 if keepalive else sub.seq,
                                          "publish_time": datetime.now(timezone.utc), "notification_data": data},
                "results": []}


class _Fault(Exception):
    def __init__(self, status: int):
        self.status = status


async def _main() -> None:                     # python tools/simulators/opcua_server.py [port]
    import math
    import random
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4840
    srv = OpcUaServer("urn:opcare:sim:plant", host="0.0.0.0", port=port,
                      policies=[("None", "None"), ("Basic256Sha256", "SignAndEncrypt")], users={"operator": "operator"})
    for name, v in (("Boiler1.Temperature", 80.0), ("Boiler1.Pressure", 6.5), ("Boiler1.Running", True),
                    ("Pump1.Speed", 1450.0), ("Pump1.Mode", 2)):
        srv.add_variable(name, v, folder="Plant/" + name.split(".")[0])
    await srv.start()
    print(f"OPC UA test server on {srv.url}  (server thumbprint {srv.thumbprint})", flush=True)
    t = 0
    while True:
        await asyncio.sleep(1)
        t += 1
        srv.set("Boiler1.Temperature", round(80 + 5 * math.sin(t / 30) + random.uniform(-0.2, 0.2), 2))
        srv.set("Boiler1.Pressure", round(6.5 + 0.3 * math.sin(t / 45), 3))
        srv.set("Pump1.Speed", round(1450 + random.uniform(-5, 5), 1))


if __name__ == "__main__":
    asyncio.run(_main())
