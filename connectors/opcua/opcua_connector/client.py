"""Asyncio OPC UA client (opc.tcp, UA Binary): discovery, secure channel with renewal, session, and the read-only
services Read, Browse, BrowseNext, CreateSubscription, CreateMonitoredItems, Publish, DeleteSubscriptions.

Every service request passes the read-only guard; Write, Call, HistoryUpdate and node management do not even
have an encoder (uabin.py).
"""
from __future__ import annotations

import asyncio
import os
import struct
from datetime import datetime, timezone
from urllib.parse import urlparse

from opcare_connector_sdk import ConnectorError, ReadOnlyGuard

from . import security as S
from .channel import HEADER, SecureChannel, hello, parse_ack, parse_error
from .uabin import NULL_NODE, NodeId, UaError, decode_message, encode_message, extension
from . import uabin as U

APPLICATION_URI = "urn:opcare:daq:connector"
PRODUCT_URI = "urn:opcare:daq"
ATTR_NODECLASS, ATTR_BROWSENAME, ATTR_DISPLAYNAME, ATTR_VALUE, ATTR_DATATYPE = 2, 3, 4, 13, 14
STATUS_NAMES = {
    0x80010000: "Bad_UnexpectedError", 0x80020000: "Bad_InternalError", 0x80050000: "Bad_CommunicationError",
    0x800A0000: "Bad_Timeout", 0x800D0000: "Bad_ServerNotConnected", 0x80130000: "Bad_SecurityChecksFailed",
    0x80170000: "Bad_CertificateInvalid", 0x801A0000: "Bad_CertificateUntrusted", 0x801F0000: "Bad_UserAccessDenied",
    0x80200000: "Bad_IdentityTokenInvalid", 0x80210000: "Bad_IdentityTokenRejected",
    0x80220000: "Bad_SecureChannelIdInvalid", 0x80250000: "Bad_SessionIdInvalid",
    0x80260000: "Bad_SessionClosed", 0x80270000: "Bad_SessionNotActivated", 0x80280000: "Bad_SubscriptionIdInvalid",
    0x80330000: "Bad_NodeIdInvalid", 0x80340000: "Bad_NodeIdUnknown", 0x80350000: "Bad_AttributeIdInvalid",
    0x803A0000: "Bad_NotReadable", 0x80740000: "Bad_TypeMismatch", 0x80790000: "Bad_NoSubscription",
    0x808A0000: "Bad_NotConnected", 0x808B0000: "Bad_DeviceFailure", 0x808D0000: "Bad_OutOfService",
    0x80AB0000: "Bad_RequestTooLarge", 0x80B80000: "Bad_ResponseTooLarge", 0x80320000: "Bad_WaitingForInitialData",
    0x80100000: "Bad_TooManyOperations", 0x80390000: "Bad_SecurityPolicyRejected", 0x80480000: "Bad_TooManyPublishRequests",
}
AUTH_STATUS = {0x801F0000, 0x80200000, 0x80210000}
SECURITY_STATUS = {0x80130000, 0x80170000, 0x801A0000, 0x80390000}
GUARD_NAMES = {"GetEndpointsRequest": "GetEndpoints", "OpenSecureChannelRequest": "OpenSecureChannel",
               "CloseSecureChannelRequest": "CloseSecureChannel", "CreateSessionRequest": "CreateSession",
               "ActivateSessionRequest": "ActivateSession", "CloseSessionRequest": "CloseSession",
               "ReadRequest": "Read", "BrowseRequest": "Browse", "BrowseNextRequest": "BrowseNext",
               "CreateSubscriptionRequest": "CreateSubscription", "CreateMonitoredItemsRequest": "CreateMonitoredItems",
               "DeleteSubscriptionsRequest": "DeleteSubscriptions", "PublishRequest": "Publish"}


def status_name(code: int) -> str:
    return STATUS_NAMES.get(code & 0xFFFF0000, f"0x{code:08X}")


class UaServiceError(Exception):
    def __init__(self, status: int, service: str):
        super().__init__(f"{service}: {status_name(status)}")
        self.status, self.service = status, service

    def as_connector_error(self) -> ConnectorError:
        s = self.status & 0xFFFF0000
        code = "AUTH_FAILED" if s in AUTH_STATUS else "TLS_FAILED" if s in SECURITY_STATUS else \
            "CONN_CLOSED" if s in (0x80220000, 0x80250000, 0x80260000) else "PROTOCOL_ERROR"
        return ConnectorError(code, str(self))


def parse_endpoint(url: str) -> tuple[str, int]:
    u = urlparse(url)
    if u.scheme != "opc.tcp" or not u.hostname:
        raise ConnectorError("ADDR_INVALID", f"endpoint must be opc.tcp://host:port/path, not {url!r}")
    return u.hostname, u.port or 4840


class UaClient:
    def __init__(self, endpoint_url: str, guard: ReadOnlyGuard, policy: str = "None", mode: str = "None",
                 cert_pem: str | None = None, key_pem: str | None = None, trusted: list[str] | None = None,
                 insecure: bool = False, timeout: float = 5.0, session_timeout_ms: int = 60000,
                 channel_lifetime_ms: int = 600000):
        self.url = endpoint_url
        self.host, self.port = parse_endpoint(endpoint_url)
        self.guard, self.timeout, self.session_timeout_ms = guard, timeout, session_timeout_ms
        self.channel_lifetime_ms = channel_lifetime_ms
        self.renewals = 0
        self.endpoints: list[dict] = []
        self.policy = S.POLICIES[policy]
        self.mode = S.MODES[mode]
        if self.policy.none != (self.mode == S.MODE_NONE):
            raise ConnectorError("CFG_INVALID", "security mode None goes with policy None, and only with it")
        self.cert = S.load_cert(cert_pem) if cert_pem else None
        self.key = S.load_key(key_pem) if key_pem else None
        self.trusted = {t.upper() for t in (trusted or [])}
        self.insecure = insecure
        self.reader = self.writer = None
        self.ch = SecureChannel()
        self._req_id = 0
        self._handle = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._parts: dict[int, list[bytes]] = {}
        self._task = self._renew_task = None
        self.closed = asyncio.Event()
        self.close_reason: ConnectorError | None = None
        self.auth_token = NULL_NODE
        self.server_cert: bytes | None = None
        self.server_nonce = b""
        self.namespaces: list[str] = []
        self.endpoint: dict | None = None
        self.requests = 0

    # ------------------------------------------------------------ transport
    async def _open_tcp(self) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except asyncio.TimeoutError:
            raise ConnectorError("CONN_TIMEOUT", f"no answer from {self.host}:{self.port}") from None
        except OSError as exc:
            raise ConnectorError("CONN_REFUSED", f"cannot connect to {self.host}:{self.port}: {exc.strerror or exc}") from None
        self.writer.write(hello(self.url))
        try:
            mtype, _ctype, body = await asyncio.wait_for(self._read_chunk(), self.timeout)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            raise ConnectorError("CONN_TIMEOUT", "the server did not answer Hello (not an OPC UA server?)") from None
        if mtype == b"ERR":
            st, reason = parse_error(body[8:])
            raise ConnectorError("CONN_REFUSED", f"server refused the connection: {status_name(st)} {reason}")
        if mtype != b"ACK":
            raise ConnectorError("PROTOCOL_ERROR", f"expected ACK, got {mtype!r}")
        self.ch.remote_buffer = parse_ack(body[8:])["receive_buffer"]
        self._task = asyncio.create_task(self._read_loop())

    async def _read_chunk(self) -> tuple[bytes, bytes, bytes]:
        head = await self.reader.readexactly(8)
        mtype, ctype, size = HEADER.unpack(head)
        if size < 8 or size > 16 * 1024 * 1024:
            raise UaError("bad chunk size")
        return mtype, ctype, head + await self.reader.readexactly(size - 8)

    async def _read_loop(self) -> None:
        try:
            while True:
                mtype, ctype, chunk = await self._read_chunk()
                if mtype == b"ERR":
                    st, reason = parse_error(chunk[8:])
                    raise ConnectorError("CONN_CLOSED", f"server closed the channel: {status_name(st)} {reason}")
                if mtype == b"OPN":
                    req, body, _ = self.ch.decode_open(chunk)
                    self._resolve(req, body)
                elif mtype == b"MSG":
                    req, body = self.ch.decode_msg(chunk)
                    if ctype == b"A":
                        self._fail(req, ConnectorError("PROTOCOL_ERROR", "server aborted the response"))
                        continue
                    self._parts.setdefault(req, []).append(body)
                    if ctype == b"F":
                        self._resolve(req, b"".join(self._parts.pop(req)))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.close_reason = ConnectorError("CONN_CLOSED", "connection to the OPC UA server lost")
        except S.SecurityError as exc:
            self.close_reason = ConnectorError("TLS_FAILED", str(exc))
        except (UaError, struct.error, IndexError) as exc:
            self.close_reason = ConnectorError("PROTOCOL_ERROR", f"bad message from the server: {exc}")
        except ConnectorError as exc:
            self.close_reason = exc
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(self.close_reason or ConnectorError("CONN_CLOSED", "connection closed"))
            self.closed.set()

    def _resolve(self, req: int, body: bytes) -> None:
        fut = self._pending.pop(req, None)
        if fut and not fut.done():
            fut.set_result(body)

    def _fail(self, req: int, exc: Exception) -> None:
        fut = self._pending.pop(req, None)
        if fut and not fut.done():
            fut.set_exception(exc)

    def _header(self, timeout_hint: float | None = None) -> dict:
        self._handle = self._handle % 4294967295 + 1
        return {"authentication_token": self.auth_token, "timestamp": datetime.now(timezone.utc),
                "request_handle": self._handle, "timeout_hint": int((timeout_hint or self.timeout) * 1000)}

    async def request(self, name: str, obj: dict, timeout: float | None = None) -> dict:
        self.guard.check(GUARD_NAMES.get(name, name))           # read-only guard (spec 12): before encoding
        if self.writer is None or self.writer.is_closing():
            raise ConnectorError("CONN_CLOSED", "not connected")
        obj = {"header": self._header(timeout), **obj}
        body = encode_message(name, obj)
        self._req_id = self._req_id % 4294967295 + 1
        req = self._req_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        self.requests += 1
        if name == "OpenSecureChannelRequest":
            self.writer.write(self.ch.encode_open(req, body))
        else:
            for chunk in self.ch.encode_msg(b"MSG", req, body):
                self.writer.write(chunk)
        try:
            data = await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req, None)
            raise ConnectorError("CONN_TIMEOUT", f"{GUARD_NAMES.get(name, name)}: no answer from the server") from None
        rname, res = decode_message(data)
        status = res["header"]["service_result"]
        if rname == "ServiceFault" or status & 0x80000000:
            raise UaServiceError(status, GUARD_NAMES.get(name, name))
        return res

    # ------------------------------------------------------------ secure channel
    async def _open_channel(self, renew: bool = False) -> None:
        local_nonce = S.nonce(self.ch.policy)
        res = await self.request("OpenSecureChannelRequest", {
            "client_protocol_version": 0, "request_type": 1 if renew else 0, "security_mode": self.ch.mode,
            "client_nonce": local_nonce if self.ch.secure else None, "requested_lifetime": self.channel_lifetime_ms})
        tok = res["security_token"]
        self.ch.channel_id = tok["channel_id"]
        self.ch.set_keys(local_nonce, res["server_nonce"] or b"", tok["token_id"])
        if renew:
            self.renewals += 1
        lifetime = max(1.0, tok["revised_lifetime"] / 1000)
        if self._renew_task is None or self._renew_task.done() or renew:
            self._renew_task = asyncio.create_task(self._renew_after(lifetime * 0.75))

    async def _renew_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        try:
            await self._open_channel(renew=True)
        except (ConnectorError, UaServiceError, S.SecurityError):
            pass

    async def get_endpoints(self) -> list[dict]:
        """Discovery on an unsecured channel (always allowed by the standard)."""
        saved = (self.ch, self.reader, self.writer, self._task)
        self.ch = SecureChannel()
        try:
            await self._open_tcp()
            await self._open_channel()
            res = await self.request("GetEndpointsRequest", {"endpoint_url": self.url})
            return res["endpoints"] or []
        finally:
            await self._close_transport()
            self.ch, self.reader, self.writer, self._task = saved
            self.closed.clear()

    def _check_trust(self, cert_der: bytes) -> None:
        tp = S.thumbprint(cert_der)
        if self.insecure or tp in self.trusted:
            return
        raise ConnectorError("TLS_FAILED", f"the server certificate (thumbprint {tp}) is not trusted yet: compare the "
                                           f"thumbprint with the one on the server, then trust it")

    async def connect(self, user: dict | None = None) -> dict:
        """Discover, open the secure channel, create and activate the session. Returns information for tests."""
        endpoints = self.endpoints = await self.get_endpoints()
        match = [e for e in endpoints if e["security_policy_uri"] == self.policy.uri and e["security_mode"] == self.mode]
        if not match:
            offered = sorted({f"{S.BY_URI[e['security_policy_uri']].name}/{_mode_name(e['security_mode'])}"
                              for e in endpoints if e["security_policy_uri"] in S.BY_URI})
            raise ConnectorError("TLS_FAILED", f"the server offers no endpoint {self.policy.name}/{_mode_name(self.mode)}; "
                                               f"it offers {', '.join(offered) or 'none'}")
        self.endpoint = match[0]
        self.server_cert = self.endpoint["server_certificate"]
        self.ch = SecureChannel(self.policy, self.mode)
        if not self.policy.none:
            if not (self.cert and self.key):
                raise ConnectorError("CFG_INVALID", "a secure endpoint needs the client certificate and key")
            self._check_trust(self.server_cert)
            self.ch.local_cert, self.ch.local_key, self.ch.remote_cert = S.der(self.cert), self.key, self.server_cert
        await self._open_tcp()
        try:
            await self._open_channel()
        except S.SecurityError as exc:
            raise ConnectorError("TLS_FAILED", str(exc)) from None
        except UaServiceError as exc:
            raise exc.as_connector_error() from None
        try:
            await self._create_session()
            await self._activate(user or {"type": "anonymous"})
            self.namespaces = await self.read_namespaces()
        except UaServiceError as exc:
            raise exc.as_connector_error() from None
        return {"endpoint": self.url, "security": f"{self.policy.name}/{_mode_name(self.mode)}",
                "server": (self.endpoint["server"]["application_name"].text or self.endpoint["server"]["application_uri"]),
                "server_thumbprint": S.thumbprint(self.server_cert) if self.server_cert else None}

    async def _create_session(self) -> None:
        client_nonce = os.urandom(32)
        res = await self.request("CreateSessionRequest", {
            "client_description": {"application_uri": (S.application_uri(self.cert) if self.cert else None) or APPLICATION_URI,
                                   "product_uri": PRODUCT_URI,
                                   "application_name": U.LocalizedText("Opcare DAQ OPC UA connector"),
                                   "application_type": 1},
            "endpoint_url": self.url, "session_name": f"opcare-{os.getpid()}", "client_nonce": client_nonce,
            "client_certificate": S.der(self.cert) if self.cert and not self.policy.none else None,
            "requested_session_timeout": float(self.session_timeout_ms), "max_response_message_size": 0})
        self.auth_token = res["authentication_token"]
        self.server_nonce = res["server_nonce"] or b""
        if not self.policy.none:
            if res["server_certificate"] and res["server_certificate"] != self.server_cert:
                raise ConnectorError("TLS_FAILED", "the session certificate differs from the endpoint certificate")
            sig = res["server_signature"] or {}
            if sig.get("signature"):                             # proof that the server holds its private key
                try:
                    self.policy.asym_verify(S.load_cert(self.server_cert).public_key(), sig["signature"],
                                            S.der(self.cert) + client_nonce)
                except S.SecurityError:
                    raise ConnectorError("TLS_FAILED", "the server's session signature is not valid") from None

    async def _activate(self, user: dict) -> None:
        tokens = self.endpoint["user_identity_tokens"] or []
        kind = {"anonymous": 0, "username": 1, "certificate": 2}[user["type"]]
        tp = next((t for t in tokens if t["token_type"] == kind), None)
        if tp is None:
            raise ConnectorError("AUTH_FAILED", f"the server does not accept {user['type']} sign-in on this endpoint")
        token_policy = S.BY_URI.get(tp["security_policy_uri"] or "", self.policy) if tp["security_policy_uri"] else self.policy
        user_sig = {}
        if kind == 0:
            identity = extension("AnonymousIdentityToken", {"policy_id": tp["policy_id"]})
        elif kind == 1:
            pw = user["password"].encode()
            if token_policy.none:
                secret, algo = pw, None
            else:
                plain = struct.pack("<I", len(pw) + len(self.server_nonce)) + pw + self.server_nonce
                secret = token_policy.asym_encrypt(S.load_cert(self.server_cert).public_key(), plain)
                algo = token_policy.password_algorithm
            identity = extension("UserNameIdentityToken", {"policy_id": tp["policy_id"], "user_name": user["username"],
                                                           "password": secret, "encryption_algorithm": algo})
        else:
            ucert, ukey = S.load_cert(user["cert_pem"]), S.load_key(user["key_pem"])
            identity = extension("X509IdentityToken", {"policy_id": tp["policy_id"], "certificate_data": S.der(ucert)})
            p = token_policy if not token_policy.none else S.POLICIES["Basic256Sha256"]
            user_sig = {"algorithm": p.signature_algorithm,
                        "signature": p.asym_sign(ukey, (self.server_cert or b"") + self.server_nonce)}
        client_sig = {}
        if not self.policy.none:
            client_sig = {"algorithm": self.policy.signature_algorithm,
                          "signature": self.policy.asym_sign(self.key, self.server_cert + self.server_nonce)}
        res = await self.request("ActivateSessionRequest", {"client_signature": client_sig, "locale_ids": ["en"],
                                                            "user_identity_token": identity,
                                                            "user_token_signature": user_sig})
        self.server_nonce = res["server_nonce"] or self.server_nonce

    # ------------------------------------------------------------ services
    def resolve(self, node: NodeId) -> NodeId:
        """nsu= (namespace URI) -> namespace index of this server."""
        if node.nsu is None:
            return node
        if node.nsu not in self.namespaces:
            raise UaServiceError(0x80340000, "Read")
        return NodeId(node.identifier, self.namespaces.index(node.nsu))

    def with_uri(self, node: NodeId) -> NodeId:
        """Namespace index -> nsu= form (stable across server restarts, spec 15.2)."""
        if node.ns and node.ns < len(self.namespaces):
            return NodeId(node.identifier, 0, self.namespaces[node.ns])
        return node

    async def read(self, nodes: list[NodeId], attribute: int = ATTR_VALUE, timestamps: int = 2) -> list[U.DataValue]:
        out: list[U.DataValue] = []
        for i in range(0, len(nodes), 500):
            res = await self.request("ReadRequest", {"max_age": 0.0, "timestamps_to_return": timestamps,
                                                     "nodes_to_read": [{"node_id": self.resolve(n), "attribute_id": attribute}
                                                                       for n in nodes[i:i + 500]]})
            out += res["results"] or []
        return out

    async def read_namespaces(self) -> list[str]:
        dv = (await self.read([NodeId(2255)]))[0]
        return list(dv.value.value) if dv.value and dv.value.is_array else ["http://opcfoundation.org/UA/"]

    async def browse(self, node: NodeId, max_refs: int = 500) -> list[dict]:
        res = await self.request("BrowseRequest", {
            "view": {"view_id": NULL_NODE}, "requested_max_references_per_node": max_refs,
            "nodes_to_browse": [{"node_id": self.resolve(node), "browse_direction": 0, "reference_type_id": NodeId(33),
                                 "include_subtypes": True, "node_class_mask": 0, "result_mask": 63}]})
        result = res["results"][0]
        refs = list(result["references"] or [])
        cp = result["continuation_point"]
        while cp and len(refs) < 5000:
            nxt = await self.request("BrowseNextRequest", {"release_continuation_points": False, "continuation_points": [cp]})
            refs += nxt["results"][0]["references"] or []
            cp = nxt["results"][0]["continuation_point"]
        return refs

    async def create_subscription(self, interval_ms: float, keepalive: int = 10) -> tuple[int, float]:
        res = await self.request("CreateSubscriptionRequest", {
            "requested_publishing_interval": float(interval_ms), "requested_lifetime_count": keepalive * 3,
            "requested_max_keep_alive_count": keepalive, "max_notifications_per_publish": 1000,
            "publishing_enabled": True, "priority": 0})
        return res["subscription_id"], res["revised_publishing_interval"]

    async def monitor(self, sub_id: int, items: list[tuple[int, NodeId, float, int]]) -> list[dict]:
        """items: (client handle, node, sampling interval ms, queue size)."""
        out = []
        for i in range(0, len(items), 500):
            res = await self.request("CreateMonitoredItemsRequest", {
                "subscription_id": sub_id, "timestamps_to_return": 2,
                "items_to_create": [{"item_to_monitor": {"node_id": self.resolve(n), "attribute_id": ATTR_VALUE},
                                     "monitoring_mode": 2,
                                     "requested_parameters": {"client_handle": h, "sampling_interval": float(s),
                                                              "queue_size": q, "discard_oldest": True}}
                                    for h, n, s, q in items[i:i + 500]]})
            out += res["results"] or []
        return out

    async def publish(self, acks: list[tuple[int, int]], timeout: float) -> dict:
        return await self.request("PublishRequest", {"subscription_acknowledgements": [
            {"subscription_id": s, "sequence_number": n} for s, n in acks]}, timeout)

    # ------------------------------------------------------------ close
    async def close(self) -> None:
        if self._renew_task:
            self._renew_task.cancel()
        if self.writer and not self.writer.is_closing():
            try:
                if self.auth_token != NULL_NODE:
                    self.guard.check("CloseSession")
                    await asyncio.wait_for(self.request("CloseSessionRequest", {"delete_subscriptions": True}), 2)
            except (ConnectorError, UaServiceError, asyncio.TimeoutError, UaError, S.SecurityError):
                pass
        await self._close_transport()

    async def _close_transport(self) -> None:
        if self.writer and not self.writer.is_closing():
            try:
                self.guard.check("CloseSecureChannel")
                self._req_id += 1
                for c in self.ch.encode_msg(b"CLO", self._req_id, encode_message("CloseSecureChannelRequest",
                                                                                  {"header": self._header()})):
                    self.writer.write(c)
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ConnectorError, AttributeError):
                pass
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.writer = None
        self.closed.set()


def _mode_name(mode: int) -> str:
    return {1: "None", 2: "Sign", 3: "SignAndEncrypt"}.get(mode, str(mode))


__all__ = ["UaClient", "UaServiceError", "status_name", "parse_endpoint", "ATTR_VALUE", "ATTR_DATATYPE",
           "ATTR_NODECLASS", "ATTR_BROWSENAME", "ATTR_DISPLAYNAME"]
