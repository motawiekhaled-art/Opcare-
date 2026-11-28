"""The OSI upper layers MMS runs on, for both roles (client and the test server):

TPKT (RFC 1006) → COTP class 0 (ISO 8073) → session kernel (ISO 8327) → presentation (ISO 8823, BER) → ACSE
(ISO 8650, AARQ/AARE) → MMS PDUs. Only what IEC 61850-8-1 uses: one presentation context for ACSE (2.2.1.0.1) and one
for MMS (1.0.9506.2.1), both with BER transfer syntax (2.1.1); password authentication (mechanism 2.2.3.1).
"""
from __future__ import annotations

import asyncio
import struct

from . import ber as B

ACSE_AS, MMS_AS, BER_TS = "2.2.1.0.1", "1.0.9506.2.1", "2.1.1"
MMS_CONTEXT = "1.0.9506.2.3"
PASSWORD_MECH = "2.2.3.1"
CTX_ACSE, CTX_MMS = 1, 3
TPDU_SIZE_CODE = 0x0B                      # 2048 bytes
MAX_DT = 2048 - 3


class IsoError(Exception):
    """A layer below MMS refused or broke the association."""


# ------------------------------------------------------------------ TPKT / COTP
def tpkt(payload: bytes) -> bytes:
    return struct.pack(">BBH", 3, 0, 4 + len(payload)) + payload


async def read_tpkt(reader) -> bytes:
    head = await reader.readexactly(4)
    if head[0] != 3:
        raise IsoError("not a TPKT packet (is this an IEC 61850 / MMS port?)")
    size = struct.unpack(">H", head[2:])[0]
    if size < 7:
        raise IsoError("TPKT packet too short")
    return await reader.readexactly(size - 4)


def cotp_cr(src_ref: int = 1) -> bytes:
    params = bytes([0xC0, 1, TPDU_SIZE_CODE, 0xC2, 2, 0, 1, 0xC1, 2, 0, 1])
    body = bytes([0xE0, 0, 0]) + struct.pack(">H", src_ref) + bytes([0]) + params
    return tpkt(bytes([len(body)]) + body)


def cotp_cc(dst_ref: int, src_ref: int = 2) -> bytes:
    params = bytes([0xC0, 1, TPDU_SIZE_CODE, 0xC2, 2, 0, 1, 0xC1, 2, 0, 1])
    body = bytes([0xD0]) + struct.pack(">HH", dst_ref, src_ref) + bytes([0]) + params
    return tpkt(bytes([len(body)]) + body)


def cotp_dt(data: bytes) -> list[bytes]:
    parts = [data[i:i + MAX_DT] for i in range(0, len(data), MAX_DT)] or [b""]
    return [tpkt(bytes([2, 0xF0, 0x80 if i == len(parts) - 1 else 0x00]) + p) for i, p in enumerate(parts)]


async def read_cotp(reader) -> tuple[int, bytes]:
    """Returns (TPDU code, data); DT TPDUs are reassembled until EOT."""
    buf = bytearray()
    while True:
        p = await read_tpkt(reader)
        li, code = p[0], p[1] & 0xF0
        if code != 0xF0:
            return code, p[1:1 + li]
        buf += p[1 + li:]
        if p[2] & 0x80:
            return 0xF0, bytes(buf)


# ------------------------------------------------------------------ session (ISO 8327 kernel)
SPDU_CONNECT, SPDU_ACCEPT, SPDU_REFUSE, SPDU_FINISH, SPDU_DISCONNECT, SPDU_ABORT, SPDU_DATA = 13, 14, 12, 9, 10, 25, 1


def _spdu(code: int, params: bytes) -> bytes:
    return bytes([code]) + _len(len(params)) + params


def _len(n: int) -> bytes:
    return bytes([n]) if n < 255 else bytes([255]) + struct.pack(">H", n)


def _param(code: int, value: bytes) -> bytes:
    return bytes([code]) + _len(len(value)) + value


def session_connect(user_data: bytes) -> bytes:
    params = (_param(5, _param(19, b"\x00") + _param(22, b"\x02"))       # connect/accept item: options, version 2
              + _param(20, b"\x00\x02")                                  # session requirements: duplex
              + _param(51, b"\x00\x01") + _param(52, b"\x00\x01")        # calling / called session selector
              + _param(193, user_data))
    return _spdu(SPDU_CONNECT, params)


def session_accept(user_data: bytes) -> bytes:
    params = (_param(5, _param(19, b"\x00") + _param(22, b"\x02")) + _param(20, b"\x00\x02")
              + _param(193, user_data))
    return _spdu(SPDU_ACCEPT, params)


def session_data(user_data: bytes) -> bytes:
    return bytes([1, 0, 1, 0]) + user_data            # GIVE TOKENS + DATA TRANSFER (category 0 concatenation)


def parse_spdu(data: bytes) -> tuple[int, bytes]:
    """Returns (SPDU code, user data)."""
    if not data:
        raise IsoError("empty session data")
    code = data[0]
    if code == SPDU_DATA:                              # GIVE TOKENS (01 00) then DATA TRANSFER (01 00 ...)
        if data[:2] == b"\x01\x00" and len(data) >= 4 and data[2] == 1:
            ln, pos = _read_len(data, 3)
            return SPDU_DATA, data[pos + ln:] if ln else data[pos:]
        return SPDU_DATA, data[2:]
    ln, pos = _read_len(data, 1)
    end, user = pos + ln, b""
    while pos < end:
        pc = data[pos]
        pl, vpos = _read_len(data, pos + 1)
        if pc == 193:
            user = data[vpos:vpos + pl]
        pos = vpos + pl
    return code, user


def _read_len(data: bytes, pos: int) -> tuple[int, int]:
    if data[pos] == 255:
        return struct.unpack(">H", data[pos + 1:pos + 3])[0], pos + 3
    return data[pos], pos + 1


# ------------------------------------------------------------------ presentation (ISO 8823)
def _pdv(context: int, content: bytes) -> bytes:
    return B.app(1, B.seq(B.integer(context), B.ctx(0, content)))


def presentation_connect(aarq: bytes) -> bytes:
    ctxs = (B.seq(B.integer(CTX_ACSE), B.oid(ACSE_AS), B.seq(B.oid(BER_TS)))
            + B.seq(B.integer(CTX_MMS), B.oid(MMS_AS), B.seq(B.oid(BER_TS))))
    normal = (B.ctx_prim(1, b"\x00\x00\x00\x01") + B.ctx_prim(2, b"\x00\x00\x00\x01") + B.ctx(4, ctxs)
              + _pdv(CTX_ACSE, aarq))
    return B.tlv(B.UNIVERSAL, True, 17, B.ctx(0, B.ctx_prim(0, b"\x01")) + B.ctx(2, normal))


def presentation_accept(aare: bytes) -> bytes:
    results = B.seq(B.ctx_prim(0, b"\x00"), B.ctx_prim(1, bytes.fromhex("5101"))) * 2     # acceptance + transfer syntax
    normal = B.ctx_prim(3, b"\x00\x00\x00\x01") + B.ctx(5, results) + _pdv(CTX_ACSE, aare)
    return B.tlv(B.UNIVERSAL, True, 17, B.ctx(0, B.ctx_prim(0, b"\x01")) + B.ctx(2, normal))


def presentation_data(mms: bytes) -> bytes:
    return _pdv(CTX_MMS, mms)


def user_pdv(data: bytes) -> tuple[int, bytes]:
    """Finds the fully-encoded-data PDV in a CP/CPA/data PPDU. Returns (context id, single-ASN1-type content)."""
    t, _ = B.parse(data)
    if t.cls == B.APPLICATION and t.number == 1:
        fed = t
    else:
        normal = t.child(2)
        if normal is None:
            raise IsoError("presentation: no normal-mode parameters (the peer refused or aborted)")
        fed = next((c for c in normal.children() if c.cls == B.APPLICATION and c.number == 1), None)
        if fed is None:
            raise IsoError("presentation: no user data")
    pdv = fed.children()[0].children()
    cid = next(c.int() for c in pdv if c.cls == B.UNIVERSAL and c.number == 2)
    single = next(c for c in pdv if c.cls == B.CONTEXT and c.number == 0)
    return cid, single.value


# ------------------------------------------------------------------ ACSE (ISO 8650)
def aarq(mms_initiate: bytes, password: str | None = None) -> bytes:
    body = (B.ctx(1, B.oid(MMS_CONTEXT))
            + B.ctx(2, B.oid("1.1.1.999.1")) + B.ctx(3, B.integer(12))
            + B.ctx(6, B.oid("1.1.1.999")) + B.ctx(7, B.integer(12)))
    if password is not None:
        body += (B.ctx_prim(10, b"\x07\x80")                                  # sender-acse-requirements: authentication
                 + B.ctx_prim(11, B.oid(PASSWORD_MECH)[2:])                    # mechanism-name
                 + B.ctx(12, B.ctx_prim(0, password.encode())))                # calling-authentication-value
    external = B.tlv(B.UNIVERSAL, True, 8, B.integer(CTX_MMS) + B.ctx(0, mms_initiate))
    return B.app(0, body + B.ctx(30, external))


def aare(mms_initiate_response: bytes, accepted: bool = True, diagnostic: int = 0) -> bytes:
    body = (B.ctx(1, B.oid(MMS_CONTEXT)) + B.ctx(2, B.integer(0 if accepted else 1))
            + B.ctx(3, B.ctx(1, B.integer(diagnostic))))
    if accepted:
        body += B.ctx(30, B.tlv(B.UNIVERSAL, True, 8, B.integer(CTX_MMS) + B.ctx(0, mms_initiate_response)))
    return B.app(1, body)


def parse_acse(data: bytes) -> dict:
    """AARQ or AARE -> {kind, result, diagnostic, password, mms}."""
    t, _ = B.parse(data)
    out = {"kind": {0: "AARQ", 1: "AARE"}.get(t.number), "result": 0, "diagnostic": 0, "password": None, "mms": None}
    for c in t.children():
        if c.cls != B.CONTEXT:
            continue
        if c.number == 2 and out["kind"] == "AARE":
            out["result"] = c.children()[0].int()
        elif c.number == 3 and out["kind"] == "AARE":
            inner = c.children()[0].children()
            out["diagnostic"] = inner[0].int() if inner else 0
        elif c.number == 12:
            out["password"] = c.children()[0].value.decode("latin-1")
        elif c.number == 30:
            ext = c.children()[0]
            enc = next((x for x in ext.children() if x.cls == B.CONTEXT and x.number == 0), None)
            out["mms"] = enc.value if enc else None
    return out


# ------------------------------------------------------------------ a connection, for either role
class IsoLink:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()

    async def send(self, mms: bytes) -> None:
        async with self.lock:
            for p in cotp_dt(session_data(presentation_data(mms))):
                self.writer.write(p)
            await self.writer.drain()

    async def recv(self) -> bytes:
        """Next MMS PDU (raises IsoError when the peer finishes or aborts)."""
        while True:
            code, data = await read_cotp(self.reader)
            if code == 0x80:                                   # COTP disconnect request
                raise IsoError("the peer disconnected the transport connection")
            if code != 0xF0:
                continue
            scode, user = parse_spdu(data)
            if scode in (SPDU_ABORT, SPDU_DISCONNECT, SPDU_FINISH, SPDU_REFUSE):
                raise IsoError({SPDU_ABORT: "the peer aborted the association", SPDU_REFUSE: "the peer refused the session"}
                               .get(scode, "the peer released the association"))
            if scode != SPDU_DATA:
                continue
            cid, mms = user_pdv(user)
            if cid == CTX_MMS:
                return mms

    # ---------------------------------------------------------------- client role
    async def associate(self, mms_initiate: bytes, password: str | None, timeout: float) -> bytes:
        self.writer.write(cotp_cr())
        code, _ = await asyncio.wait_for(read_cotp(self.reader), timeout)
        if code != 0xD0:
            raise IsoError("the transport connection was refused (COTP)")
        for p in cotp_dt(session_connect(presentation_connect(aarq(mms_initiate, password)))):
            self.writer.write(p)
        code, data = await asyncio.wait_for(read_cotp(self.reader), timeout)
        if code != 0xF0:
            raise IsoError("no session answer")
        scode, user = parse_spdu(data)
        if scode == SPDU_REFUSE:
            raise IsoError("the session was refused")
        if scode != SPDU_ACCEPT:
            raise IsoError(f"unexpected session answer (SPDU {scode})")
        _, acse = user_pdv(user)
        a = parse_acse(acse)
        if a["result"] != 0:
            raise PermissionError(a["diagnostic"])
        if not a["mms"]:
            raise IsoError("the association answer carries no MMS initiate response")
        return a["mms"]

    # ---------------------------------------------------------------- server role
    async def accept(self) -> dict:
        code, data = await read_cotp(self.reader)
        if code != 0xE0:
            raise IsoError("expected a COTP connection request")
        self.writer.write(cotp_cc(struct.unpack(">H", data[3:5])[0]))
        code, data = await read_cotp(self.reader)
        scode, user = parse_spdu(data)
        if scode != SPDU_CONNECT:
            raise IsoError("expected a session connect")
        _, acse = user_pdv(user)
        return parse_acse(acse)

    def answer(self, mms_initiate_response: bytes | None, diagnostic: int = 0) -> None:
        if mms_initiate_response is None:                       # rejected: AARE with result 1 (rejected-permanent)
            payload = session_accept(presentation_accept(aare(b"", False, diagnostic)))
        else:
            payload = session_accept(presentation_accept(aare(mms_initiate_response)))
        for p in cotp_dt(payload):
            self.writer.write(p)
