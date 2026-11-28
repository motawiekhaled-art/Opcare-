"""OPC UA Binary encoding (IEC 62541-6 section 5.2): built-in types and the service structures a read-only client
needs. Structures are described as field lists in STRUCTS and encoded/decoded generically.

There are deliberately no structures for Write, Call, HistoryUpdate, AddNodes/DeleteNodes or any other service
that changes a server (spec 12, 15.2): they cannot be encoded.
"""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


class UaError(ValueError):
    """Malformed OPC UA data."""


# ------------------------------------------------------------------ NodeId
@dataclass(frozen=True)
class NodeId:
    identifier: Any = 0            # int (numeric), str (string), uuid.UUID (guid), bytes (opaque)
    ns: int = 0
    nsu: str | None = None         # namespace URI (ExpandedNodeId / text form "nsu=")
    server_index: int = 0

    @staticmethod
    def parse(text: str) -> "NodeId":
        """'i=85', 'ns=2;s=Boiler.Temp', 'ns=3;g=…', 'ns=1;b=base64', 'nsu=urn:x;s=Temp'."""
        ns, nsu, rest = 0, None, text.strip()
        if rest.startswith("nsu="):
            nsu, _, rest = rest[4:].partition(";")
        elif rest.startswith("ns="):
            n, _, rest = rest[3:].partition(";")
            ns = int(n)
        kind, _, ident = rest.partition("=")
        if kind == "i":
            return NodeId(int(ident), ns, nsu)
        if kind == "s":
            return NodeId(ident, ns, nsu)
        if kind == "g":
            return NodeId(uuid.UUID(ident), ns, nsu)
        if kind == "b":
            import base64
            return NodeId(base64.b64decode(ident), ns, nsu)
        raise UaError(f"invalid NodeId {text!r}")

    def __str__(self) -> str:
        prefix = f"nsu={self.nsu};" if self.nsu else f"ns={self.ns};" if self.ns else ""
        i = self.identifier
        if isinstance(i, int):
            return f"{prefix}i={i}"
        if isinstance(i, uuid.UUID):
            return f"{prefix}g={i}"
        if isinstance(i, bytes):
            import base64
            return f"{prefix}b={base64.b64encode(i).decode()}"
        return f"{prefix}s={i}"


NULL_NODE = NodeId(0, 0)


@dataclass
class QualifiedName:
    name: str | None = None
    ns: int = 0


@dataclass
class LocalizedText:
    text: str | None = None
    locale: str | None = None


@dataclass
class ExtensionObject:
    type_id: NodeId = NULL_NODE
    body: bytes | None = None      # binary body (decoded lazily by the caller)


@dataclass
class Variant:
    type: int = 0                  # built-in type id (0 = null)
    value: Any = None
    is_array: bool = False


@dataclass
class DataValue:
    value: Variant | None = None
    status: int = 0
    source_ts: datetime | None = None
    server_ts: datetime | None = None


# built-in type ids
BOOLEAN, SBYTE, BYTE, INT16, UINT16, INT32, UINT32, INT64, UINT64, FLOAT, DOUBLE, STRING, DATETIME, GUID, \
    BYTESTRING, XMLELEMENT, NODEID, EXPANDEDNODEID, STATUSCODE, QUALIFIEDNAME, LOCALIZEDTEXT, EXTENSIONOBJECT, \
    DATAVALUE, VARIANT, DIAGNOSTICINFO = range(1, 26)
TYPE_NAMES = {1: "Boolean", 2: "SByte", 3: "Byte", 4: "Int16", 5: "UInt16", 6: "Int32", 7: "UInt32", 8: "Int64",
              9: "UInt64", 10: "Float", 11: "Double", 12: "String", 13: "DateTime", 14: "Guid", 15: "ByteString",
              16: "XmlElement", 17: "NodeId", 18: "ExpandedNodeId", 19: "StatusCode", 20: "QualifiedName",
              21: "LocalizedText", 22: "ExtensionObject", 23: "DataValue", 24: "Variant", 25: "DiagnosticInfo"}
_FMT = {BOOLEAN: "<?", SBYTE: "<b", BYTE: "<B", INT16: "<h", UINT16: "<H", INT32: "<i", UINT32: "<I", INT64: "<q",
        UINT64: "<Q", FLOAT: "<f", DOUBLE: "<d", STATUSCODE: "<I"}

EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def dt_to_ticks(dt: datetime | None) -> int:
    if dt is None:
        return 0
    delta = dt.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def ticks_to_dt(t: int) -> datetime | None:
    if t <= 0 or t >= 0x7FFFFFFFFFFFFFFF:
        return None
    try:
        return EPOCH + timedelta(microseconds=t // 10)
    except OverflowError:
        return None


# ------------------------------------------------------------------ writer / reader
class Writer:
    def __init__(self):
        self.parts: list[bytes] = []

    def bytes(self) -> bytes:
        return b"".join(self.parts)

    def raw(self, b: bytes) -> None:
        self.parts.append(b)

    def fmt(self, f: str, v) -> None:
        self.parts.append(struct.pack(f, v))

    def u8(self, v): self.fmt("<B", v)
    def u16(self, v): self.fmt("<H", v)
    def u32(self, v): self.fmt("<I", v)
    def i32(self, v): self.fmt("<i", v)
    def f64(self, v): self.fmt("<d", v)

    def string(self, s: str | None) -> None:
        if s is None:
            self.i32(-1)
        else:
            b = s.encode()
            self.i32(len(b))
            self.raw(b)

    def bytestring(self, b: bytes | None) -> None:
        if b is None:
            self.i32(-1)
        else:
            self.i32(len(b))
            self.raw(b)

    def datetime(self, dt: datetime | None) -> None:
        self.fmt("<q", dt_to_ticks(dt))

    def guid(self, g: uuid.UUID) -> None:
        self.raw(g.bytes_le)

    def nodeid(self, n: NodeId, expanded: bool = False) -> None:
        flags = (0x80 if expanded and n.nsu else 0) | (0x40 if expanded and n.server_index else 0)
        i, ns = n.identifier, (0 if expanded and n.nsu else n.ns)
        if isinstance(i, int):
            if ns == 0 and i <= 0xFF:
                self.u8(0x00 | flags)
                self.u8(i)
            elif ns <= 0xFF and i <= 0xFFFF:
                self.u8(0x01 | flags)
                self.u8(ns)
                self.u16(i)
            else:
                self.u8(0x02 | flags)
                self.u16(ns)
                self.u32(i)
        elif isinstance(i, str):
            self.u8(0x03 | flags)
            self.u16(ns)
            self.string(i)
        elif isinstance(i, uuid.UUID):
            self.u8(0x04 | flags)
            self.u16(ns)
            self.guid(i)
        else:
            self.u8(0x05 | flags)
            self.u16(ns)
            self.bytestring(bytes(i))
        if flags & 0x80:
            self.string(n.nsu)
        if flags & 0x40:
            self.u32(n.server_index)

    def qualifiedname(self, q: QualifiedName) -> None:
        self.u16(q.ns)
        self.string(q.name)

    def localizedtext(self, t: LocalizedText) -> None:
        mask = (1 if t.locale is not None else 0) | (2 if t.text is not None else 0)
        self.u8(mask)
        if t.locale is not None:
            self.string(t.locale)
        if t.text is not None:
            self.string(t.text)

    def extensionobject(self, e: ExtensionObject | None) -> None:
        if e is None or e.body is None:
            self.nodeid(e.type_id if e else NULL_NODE)
            self.u8(0)
        else:
            self.nodeid(e.type_id)
            self.u8(1)
            self.bytestring(e.body)

    def scalar(self, t: int, v) -> None:
        if t in _FMT:
            self.fmt(_FMT[t], v)
        elif t in (STRING, XMLELEMENT):
            self.string(v)
        elif t == DATETIME:
            self.datetime(v)
        elif t == GUID:
            self.guid(v)
        elif t == BYTESTRING:
            self.bytestring(v)
        elif t == NODEID:
            self.nodeid(v)
        elif t == EXPANDEDNODEID:
            self.nodeid(v, expanded=True)
        elif t == QUALIFIEDNAME:
            self.qualifiedname(v)
        elif t == LOCALIZEDTEXT:
            self.localizedtext(v)
        elif t == EXTENSIONOBJECT:
            self.extensionobject(v)
        elif t == DATAVALUE:
            self.datavalue(v)
        elif t == VARIANT:
            self.variant(v)
        else:
            raise UaError(f"cannot encode built-in type {t}")

    def variant(self, v: Variant | None) -> None:
        if v is None or v.type == 0:
            self.u8(0)
            return
        if v.is_array:
            self.u8(v.type | 0x80)
            self.i32(len(v.value))
            for x in v.value:
                self.scalar(v.type, x)
        else:
            self.u8(v.type)
            self.scalar(v.type, v.value)

    def datavalue(self, d: DataValue) -> None:
        mask = (1 if d.value is not None else 0) | (2 if d.status else 0) | (4 if d.source_ts else 0) | \
               (8 if d.server_ts else 0)
        self.u8(mask)
        if d.value is not None:
            self.variant(d.value)
        if d.status:
            self.u32(d.status)
        if d.source_ts:
            self.datetime(d.source_ts)
        if d.server_ts:
            self.datetime(d.server_ts)


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise UaError("truncated OPC UA message")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def fmt(self, f: str):
        return struct.unpack(f, self.take(struct.calcsize(f)))[0]

    def u8(self): return self.fmt("<B")
    def u16(self): return self.fmt("<H")
    def u32(self): return self.fmt("<I")
    def i32(self): return self.fmt("<i")
    def f64(self): return self.fmt("<d")

    def string(self) -> str | None:
        n = self.i32()
        return None if n < 0 else self.take(n).decode("utf-8", errors="replace")

    def bytestring(self) -> bytes | None:
        n = self.i32()
        return None if n < 0 else self.take(n)

    def datetime(self) -> datetime | None:
        return ticks_to_dt(self.fmt("<q"))

    def guid(self) -> uuid.UUID:
        return uuid.UUID(bytes_le=self.take(16))

    def nodeid(self) -> NodeId:
        enc = self.u8()
        kind, ns = enc & 0x3F, 0
        if kind == 0:
            ident = self.u8()
        elif kind == 1:
            ns, ident = self.u8(), self.u16()
        elif kind == 2:
            ns, ident = self.u16(), self.u32()
        elif kind == 3:
            ns, ident = self.u16(), self.string()
        elif kind == 4:
            ns, ident = self.u16(), self.guid()
        elif kind == 5:
            ns, ident = self.u16(), self.bytestring()
        else:
            raise UaError(f"bad NodeId encoding {enc}")
        nsu = self.string() if enc & 0x80 else None
        si = self.u32() if enc & 0x40 else 0
        return NodeId(ident, ns, nsu, si)

    def qualifiedname(self) -> QualifiedName:
        ns = self.u16()
        return QualifiedName(self.string(), ns)

    def localizedtext(self) -> LocalizedText:
        mask = self.u8()
        locale = self.string() if mask & 1 else None
        text = self.string() if mask & 2 else None
        return LocalizedText(text, locale)

    def extensionobject(self) -> ExtensionObject:
        tid = self.nodeid()
        enc = self.u8()
        if enc == 0:
            return ExtensionObject(tid, None)
        body = self.bytestring()
        return ExtensionObject(tid, body if enc == 1 else None)

    def diagnosticinfo(self) -> None:
        mask = self.u8()
        for bit in (0x01, 0x02, 0x08, 0x04):
            if mask & bit:
                self.i32()
        if mask & 0x10:
            self.string()
        if mask & 0x20:
            self.u32()
        if mask & 0x40:
            self.diagnosticinfo()

    def scalar(self, t: int):
        if t in _FMT:
            return self.fmt(_FMT[t])
        if t in (STRING, XMLELEMENT):
            return self.string()
        if t == DATETIME:
            return self.datetime()
        if t == GUID:
            return self.guid()
        if t == BYTESTRING:
            return self.bytestring()
        if t in (NODEID, EXPANDEDNODEID):
            return self.nodeid()
        if t == QUALIFIEDNAME:
            return self.qualifiedname()
        if t == LOCALIZEDTEXT:
            return self.localizedtext()
        if t == EXTENSIONOBJECT:
            return self.extensionobject()
        if t == DATAVALUE:
            return self.datavalue()
        if t == VARIANT:
            return self.variant()
        if t == DIAGNOSTICINFO:
            return self.diagnosticinfo()
        raise UaError(f"unknown built-in type {t}")

    def variant(self) -> Variant:
        enc = self.u8()
        t = enc & 0x3F
        if t == 0:
            return Variant()
        if enc & 0x80:
            n = self.i32()
            vals = [self.scalar(t) for _ in range(max(n, 0))]
            if enc & 0x40:                                    # array dimensions
                for _ in range(max(self.i32(), 0)):
                    self.i32()
            return Variant(t, vals, True)
        return Variant(t, self.scalar(t))

    def datavalue(self) -> DataValue:
        mask = self.u8()
        d = DataValue()
        if mask & 0x01:
            d.value = self.variant()
        if mask & 0x02:
            d.status = self.u32()
        if mask & 0x04:
            d.source_ts = self.datetime()
        if mask & 0x10:
            self.u16()
        if mask & 0x08:
            d.server_ts = self.datetime()
        if mask & 0x20:
            self.u16()
        return d


# ------------------------------------------------------------------ structures
# field type: a built-in name, a struct name, "[X]" for arrays of X, or "Enum" (Int32)
STRUCTS: dict[str, list[tuple[str, str]]] = {
    "RequestHeader": [("authentication_token", "NodeId"), ("timestamp", "DateTime"), ("request_handle", "UInt32"),
                      ("return_diagnostics", "UInt32"), ("audit_entry_id", "String"), ("timeout_hint", "UInt32"),
                      ("additional_header", "ExtensionObject")],
    "ResponseHeader": [("timestamp", "DateTime"), ("request_handle", "UInt32"), ("service_result", "StatusCode"),
                       ("service_diagnostics", "DiagnosticInfo"), ("string_table", "[String]"),
                       ("additional_header", "ExtensionObject")],
    "ChannelSecurityToken": [("channel_id", "UInt32"), ("token_id", "UInt32"), ("created_at", "DateTime"),
                             ("revised_lifetime", "UInt32")],
    "OpenSecureChannelRequest": [("header", "RequestHeader"), ("client_protocol_version", "UInt32"),
                                 ("request_type", "Enum"), ("security_mode", "Enum"), ("client_nonce", "ByteString"),
                                 ("requested_lifetime", "UInt32")],
    "OpenSecureChannelResponse": [("header", "ResponseHeader"), ("server_protocol_version", "UInt32"),
                                  ("security_token", "ChannelSecurityToken"), ("server_nonce", "ByteString")],
    "CloseSecureChannelRequest": [("header", "RequestHeader")],
    "ServiceFault": [("header", "ResponseHeader")],
    "ApplicationDescription": [("application_uri", "String"), ("product_uri", "String"),
                               ("application_name", "LocalizedText"), ("application_type", "Enum"),
                               ("gateway_server_uri", "String"), ("discovery_profile_uri", "String"),
                               ("discovery_urls", "[String]")],
    "UserTokenPolicy": [("policy_id", "String"), ("token_type", "Enum"), ("issued_token_type", "String"),
                        ("issuer_endpoint_url", "String"), ("security_policy_uri", "String")],
    "EndpointDescription": [("endpoint_url", "String"), ("server", "ApplicationDescription"),
                            ("server_certificate", "ByteString"), ("security_mode", "Enum"),
                            ("security_policy_uri", "String"), ("user_identity_tokens", "[UserTokenPolicy]"),
                            ("transport_profile_uri", "String"), ("security_level", "Byte")],
    "GetEndpointsRequest": [("header", "RequestHeader"), ("endpoint_url", "String"), ("locale_ids", "[String]"),
                            ("profile_uris", "[String]")],
    "GetEndpointsResponse": [("header", "ResponseHeader"), ("endpoints", "[EndpointDescription]")],
    "SignedSoftwareCertificate": [("certificate_data", "ByteString"), ("signature", "ByteString")],
    "SignatureData": [("algorithm", "String"), ("signature", "ByteString")],
    "CreateSessionRequest": [("header", "RequestHeader"), ("client_description", "ApplicationDescription"),
                             ("server_uri", "String"), ("endpoint_url", "String"), ("session_name", "String"),
                             ("client_nonce", "ByteString"), ("client_certificate", "ByteString"),
                             ("requested_session_timeout", "Double"), ("max_response_message_size", "UInt32")],
    "CreateSessionResponse": [("header", "ResponseHeader"), ("session_id", "NodeId"),
                              ("authentication_token", "NodeId"), ("revised_session_timeout", "Double"),
                              ("server_nonce", "ByteString"), ("server_certificate", "ByteString"),
                              ("server_endpoints", "[EndpointDescription]"),
                              ("server_software_certificates", "[SignedSoftwareCertificate]"),
                              ("server_signature", "SignatureData"), ("max_request_message_size", "UInt32")],
    "AnonymousIdentityToken": [("policy_id", "String")],
    "UserNameIdentityToken": [("policy_id", "String"), ("user_name", "String"), ("password", "ByteString"),
                              ("encryption_algorithm", "String")],
    "X509IdentityToken": [("policy_id", "String"), ("certificate_data", "ByteString")],
    "ActivateSessionRequest": [("header", "RequestHeader"), ("client_signature", "SignatureData"),
                               ("client_software_certificates", "[SignedSoftwareCertificate]"),
                               ("locale_ids", "[String]"), ("user_identity_token", "ExtensionObject"),
                               ("user_token_signature", "SignatureData")],
    "ActivateSessionResponse": [("header", "ResponseHeader"), ("server_nonce", "ByteString"),
                                ("results", "[StatusCode]"), ("diagnostic_infos", "[DiagnosticInfo]")],
    "CloseSessionRequest": [("header", "RequestHeader"), ("delete_subscriptions", "Boolean")],
    "CloseSessionResponse": [("header", "ResponseHeader")],
    "ReadValueId": [("node_id", "NodeId"), ("attribute_id", "UInt32"), ("index_range", "String"),
                    ("data_encoding", "QualifiedName")],
    "ReadRequest": [("header", "RequestHeader"), ("max_age", "Double"), ("timestamps_to_return", "Enum"),
                    ("nodes_to_read", "[ReadValueId]")],
    "ReadResponse": [("header", "ResponseHeader"), ("results", "[DataValue]"), ("diagnostic_infos", "[DiagnosticInfo]")],
    "ViewDescription": [("view_id", "NodeId"), ("timestamp", "DateTime"), ("view_version", "UInt32")],
    "BrowseDescription": [("node_id", "NodeId"), ("browse_direction", "Enum"), ("reference_type_id", "NodeId"),
                          ("include_subtypes", "Boolean"), ("node_class_mask", "UInt32"), ("result_mask", "UInt32")],
    "ReferenceDescription": [("reference_type_id", "NodeId"), ("is_forward", "Boolean"), ("node_id", "ExpandedNodeId"),
                             ("browse_name", "QualifiedName"), ("display_name", "LocalizedText"), ("node_class", "Enum"),
                             ("type_definition", "ExpandedNodeId")],
    "BrowseResult": [("status_code", "StatusCode"), ("continuation_point", "ByteString"),
                     ("references", "[ReferenceDescription]")],
    "BrowseRequest": [("header", "RequestHeader"), ("view", "ViewDescription"),
                      ("requested_max_references_per_node", "UInt32"), ("nodes_to_browse", "[BrowseDescription]")],
    "BrowseResponse": [("header", "ResponseHeader"), ("results", "[BrowseResult]"), ("diagnostic_infos", "[DiagnosticInfo]")],
    "BrowseNextRequest": [("header", "RequestHeader"), ("release_continuation_points", "Boolean"),
                          ("continuation_points", "[ByteString]")],
    "BrowseNextResponse": [("header", "ResponseHeader"), ("results", "[BrowseResult]"),
                           ("diagnostic_infos", "[DiagnosticInfo]")],
    "CreateSubscriptionRequest": [("header", "RequestHeader"), ("requested_publishing_interval", "Double"),
                                  ("requested_lifetime_count", "UInt32"), ("requested_max_keep_alive_count", "UInt32"),
                                  ("max_notifications_per_publish", "UInt32"), ("publishing_enabled", "Boolean"),
                                  ("priority", "Byte")],
    "CreateSubscriptionResponse": [("header", "ResponseHeader"), ("subscription_id", "UInt32"),
                                   ("revised_publishing_interval", "Double"), ("revised_lifetime_count", "UInt32"),
                                   ("revised_max_keep_alive_count", "UInt32")],
    "MonitoringParameters": [("client_handle", "UInt32"), ("sampling_interval", "Double"), ("filter", "ExtensionObject"),
                             ("queue_size", "UInt32"), ("discard_oldest", "Boolean")],
    "MonitoredItemCreateRequest": [("item_to_monitor", "ReadValueId"), ("monitoring_mode", "Enum"),
                                   ("requested_parameters", "MonitoringParameters")],
    "MonitoredItemCreateResult": [("status_code", "StatusCode"), ("monitored_item_id", "UInt32"),
                                  ("revised_sampling_interval", "Double"), ("revised_queue_size", "UInt32"),
                                  ("filter_result", "ExtensionObject")],
    "CreateMonitoredItemsRequest": [("header", "RequestHeader"), ("subscription_id", "UInt32"),
                                    ("timestamps_to_return", "Enum"), ("items_to_create", "[MonitoredItemCreateRequest]")],
    "CreateMonitoredItemsResponse": [("header", "ResponseHeader"), ("results", "[MonitoredItemCreateResult]"),
                                     ("diagnostic_infos", "[DiagnosticInfo]")],
    "DeleteSubscriptionsRequest": [("header", "RequestHeader"), ("subscription_ids", "[UInt32]")],
    "DeleteSubscriptionsResponse": [("header", "ResponseHeader"), ("results", "[StatusCode]"),
                                    ("diagnostic_infos", "[DiagnosticInfo]")],
    "SubscriptionAcknowledgement": [("subscription_id", "UInt32"), ("sequence_number", "UInt32")],
    "PublishRequest": [("header", "RequestHeader"), ("subscription_acknowledgements", "[SubscriptionAcknowledgement]")],
    "NotificationMessage": [("sequence_number", "UInt32"), ("publish_time", "DateTime"),
                            ("notification_data", "[ExtensionObject]")],
    "PublishResponse": [("header", "ResponseHeader"), ("subscription_id", "UInt32"),
                        ("available_sequence_numbers", "[UInt32]"), ("more_notifications", "Boolean"),
                        ("notification_message", "NotificationMessage"), ("results", "[StatusCode]"),
                        ("diagnostic_infos", "[DiagnosticInfo]")],
    "MonitoredItemNotification": [("client_handle", "UInt32"), ("value", "DataValue")],
    "DataChangeNotification": [("monitored_items", "[MonitoredItemNotification]"),
                               ("diagnostic_infos", "[DiagnosticInfo]")],
    "StatusChangeNotification": [("status", "StatusCode"), ("diagnostic_info", "DiagnosticInfo")],
}

# binary encoding NodeIds (namespace 0) of the messages and extension objects used
ENCODING_IDS = {
    "ServiceFault": 397, "GetEndpointsRequest": 428, "GetEndpointsResponse": 431,
    "OpenSecureChannelRequest": 446, "OpenSecureChannelResponse": 449, "CloseSecureChannelRequest": 452,
    "CreateSessionRequest": 461, "CreateSessionResponse": 464, "ActivateSessionRequest": 467,
    "ActivateSessionResponse": 470, "CloseSessionRequest": 473, "CloseSessionResponse": 476,
    "BrowseRequest": 527, "BrowseResponse": 530, "BrowseNextRequest": 533, "BrowseNextResponse": 536,
    "ReadRequest": 631, "ReadResponse": 634,
    "CreateMonitoredItemsRequest": 751, "CreateMonitoredItemsResponse": 754,
    "CreateSubscriptionRequest": 787, "CreateSubscriptionResponse": 790,
    "PublishRequest": 826, "PublishResponse": 829,
    "DeleteSubscriptionsRequest": 847, "DeleteSubscriptionsResponse": 850,
    "AnonymousIdentityToken": 321, "UserNameIdentityToken": 324, "X509IdentityToken": 327,
    "DataChangeNotification": 811, "StatusChangeNotification": 820,
}
BY_ENCODING_ID = {v: k for k, v in ENCODING_IDS.items()}
_SIMPLE = {"Boolean": BOOLEAN, "Byte": BYTE, "UInt16": UINT16, "UInt32": UINT32, "Int32": INT32, "Double": DOUBLE,
           "String": STRING, "DateTime": DATETIME, "ByteString": BYTESTRING, "NodeId": NODEID,
           "ExpandedNodeId": EXPANDEDNODEID, "StatusCode": STATUSCODE, "QualifiedName": QUALIFIEDNAME,
           "LocalizedText": LOCALIZEDTEXT, "ExtensionObject": EXTENSIONOBJECT, "DataValue": DATAVALUE,
           "Variant": VARIANT, "Enum": INT32}
_DEFAULTS = {BOOLEAN: False, BYTE: 0, UINT16: 0, UINT32: 0, INT32: 0, DOUBLE: 0.0, STRING: None, DATETIME: None,
             BYTESTRING: None, NODEID: NULL_NODE, EXPANDEDNODEID: NULL_NODE, STATUSCODE: 0,
             QUALIFIEDNAME: QualifiedName(), LOCALIZEDTEXT: LocalizedText(), EXTENSIONOBJECT: None,
             DATAVALUE: DataValue(), VARIANT: Variant()}


def encode_struct(w: Writer, name: str, obj: dict) -> None:
    for field, ftype in STRUCTS[name]:
        v = obj.get(field)
        if ftype.startswith("["):
            inner = ftype[1:-1]
            if v is None:
                w.i32(-1)
                continue
            w.i32(len(v))
            for x in v:
                _enc_one(w, inner, x)
        else:
            _enc_one(w, ftype, v)


def _enc_one(w: Writer, ftype: str, v) -> None:
    if ftype in STRUCTS:
        encode_struct(w, ftype, v or {})
    elif ftype == "DiagnosticInfo":
        w.u8(0)
    else:
        t = _SIMPLE[ftype]
        w.scalar(t, _DEFAULTS.get(t) if v is None else v)


def decode_struct(r: Reader, name: str) -> dict:
    out = {}
    for field, ftype in STRUCTS[name]:
        if ftype.startswith("["):
            n = r.i32()
            out[field] = [_dec_one(r, ftype[1:-1]) for _ in range(max(n, 0))] if n >= 0 else None
        else:
            out[field] = _dec_one(r, ftype)
    return out


def _dec_one(r: Reader, ftype: str):
    if ftype in STRUCTS:
        return decode_struct(r, ftype)
    if ftype == "DiagnosticInfo":
        r.diagnosticinfo()
        return None
    return r.scalar(_SIMPLE[ftype])


def encode_message(name: str, obj: dict) -> bytes:
    """A service message: its encoding NodeId followed by the structure."""
    if name not in ENCODING_IDS:
        raise UaError(f"{name} cannot be encoded")
    w = Writer()
    w.nodeid(NodeId(ENCODING_IDS[name]))
    encode_struct(w, name, obj)
    return w.bytes()


def decode_message(data: bytes) -> tuple[str, dict]:
    r = Reader(data)
    nid = r.nodeid()
    name = BY_ENCODING_ID.get(nid.identifier) if nid.ns == 0 else None
    if name is None or name not in STRUCTS:
        raise UaError(f"unknown message type {nid}")
    return name, decode_struct(r, name)


def extension(name: str, obj: dict) -> ExtensionObject:
    w = Writer()
    encode_struct(w, name, obj)
    return ExtensionObject(NodeId(ENCODING_IDS[name]), w.bytes())


def from_extension(e: ExtensionObject | None) -> tuple[str | None, dict | None]:
    if e is None or e.body is None or e.type_id.ns != 0:
        return None, None
    name = BY_ENCODING_ID.get(e.type_id.identifier)
    if name is None or name not in STRUCTS:
        return None, None
    return name, decode_struct(Reader(e.body), name)
