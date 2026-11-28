"""OPC UA Connection Protocol and Secure Conversation framing (IEC 62541-6 sections 6.7 and 7.1).

HEL/ACK/ERR, and OPN/MSG/CLO chunks with asymmetric (OpenSecureChannel) or symmetric security: padding, RSA or
HMAC signatures, RSA-OAEP or AES-CBC encryption, and splitting large messages into chunks. Used by the client and
by the test server (tools/simulators/opcua_server.py), so it is written for both roles.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import security as S
from .uabin import Reader, UaError, Writer

HEADER = struct.Struct("<3scI")
PROTOCOL_VERSION = 0
BUFFER = 65536


def hello(endpoint_url: str, buffer: int = BUFFER) -> bytes:
    w = Writer()
    w.u32(PROTOCOL_VERSION), w.u32(buffer), w.u32(buffer), w.u32(0), w.u32(0)
    w.string(endpoint_url)
    body = w.bytes()
    return HEADER.pack(b"HEL", b"F", 8 + len(body)) + body


def parse_hello(body: bytes) -> dict:
    r = Reader(body)
    return {"version": r.u32(), "receive_buffer": r.u32(), "send_buffer": r.u32(), "max_message": r.u32(),
            "max_chunks": r.u32(), "endpoint_url": r.string()}


def ack(buffer: int = BUFFER) -> bytes:
    w = Writer()
    w.u32(PROTOCOL_VERSION), w.u32(buffer), w.u32(buffer), w.u32(0), w.u32(0)
    body = w.bytes()
    return HEADER.pack(b"ACK", b"F", 8 + len(body)) + body


def parse_ack(body: bytes) -> dict:
    r = Reader(body)
    return {"version": r.u32(), "receive_buffer": r.u32(), "send_buffer": r.u32(), "max_message": r.u32(),
            "max_chunks": r.u32()}


def error(status: int, reason: str) -> bytes:
    w = Writer()
    w.u32(status)
    w.string(reason)
    body = w.bytes()
    return HEADER.pack(b"ERR", b"F", 8 + len(body)) + body


def parse_error(body: bytes) -> tuple[int, str]:
    r = Reader(body)
    return r.u32(), r.string() or ""


@dataclass
class SecureChannel:
    """Security state of one secure channel, for either side."""
    policy: S.Policy = field(default_factory=lambda: S.POLICIES["None"])
    mode: int = S.MODE_NONE
    local_cert: bytes | None = None          # DER
    local_key: object = None
    remote_cert: bytes | None = None         # DER
    channel_id: int = 0
    token_id: int = 0
    local_keys: S.Keys | None = None
    remote_keys: S.Keys | None = None
    old_remote: dict = field(default_factory=dict)   # token id -> keys still accepted after a renewal
    seq: int = 0
    remote_buffer: int = BUFFER

    @property
    def secure(self) -> bool:
        return not self.policy.none

    def remote_public(self):
        return S.load_cert(self.remote_cert).public_key()

    def next_seq(self) -> int:
        self.seq = self.seq + 1 if self.seq < 4294966271 else 1
        return self.seq

    def set_keys(self, local_nonce: bytes, remote_nonce: bytes, token_id: int) -> None:
        """Our sending keys come from (secret = remote nonce, seed = our nonce), and the other way round."""
        if self.remote_keys is not None and self.token_id:
            self.old_remote[self.token_id] = self.remote_keys
        self.token_id = token_id
        if self.secure:
            self.local_keys = self.policy.derive(remote_nonce, local_nonce)
            self.remote_keys = self.policy.derive(local_nonce, remote_nonce)

    # ------------------------------------------------------------ asymmetric (OPN)
    def encode_open(self, request_id: int, body: bytes) -> bytes:
        asym = Writer()
        asym.string(self.policy.uri)
        asym.bytestring(self.local_cert if self.secure else None)
        asym.bytestring(bytes.fromhex(S.thumbprint(self.remote_cert)) if self.secure else None)
        asym_b = asym.bytes()
        seq = struct.pack("<II", self.next_seq(), request_id)
        if not self.secure:
            payload = seq + body
            return HEADER.pack(b"OPN", b"F", 12 + len(asym_b) + len(payload)) + struct.pack("<I", self.channel_id) + asym_b + payload
        pub = self.remote_public()
        rk, sk = pub.key_size // 8, self.local_key.key_size // 8
        block = rk - self.policy.oaep_overhead()
        extra = 1 if rk > 256 else 0
        pad = (-(8 + len(body) + 1 + extra + sk)) % block
        padding = bytes([pad & 0xFF]) * (pad + 1) + (bytes([pad >> 8]) if extra else b"")
        plain = seq + body + padding
        enc_len = (len(plain) + sk) // block * rk
        head = HEADER.pack(b"OPN", b"F", 12 + len(asym_b) + enc_len) + struct.pack("<I", self.channel_id) + asym_b
        sig = self.policy.asym_sign(self.local_key, head + plain)
        return head + self.policy.asym_encrypt(pub, plain + sig)

    def decode_open(self, chunk: bytes) -> tuple[int, bytes, dict]:
        """Returns (request_id, body, info) and learns the sender's certificate and policy (server side)."""
        r = Reader(chunk, 12)
        uri, sender_cert, thumb = r.string(), r.bytestring(), r.bytestring()
        policy = S.BY_URI.get(uri)
        if policy is None:
            raise S.SecurityError(f"security policy {uri} is not supported")
        head_len = r.pos
        info = {"policy": policy, "sender_cert": sender_cert, "receiver_thumbprint": thumb}
        if policy.none:
            rest = Reader(chunk, head_len)
            _seq, req = rest.u32(), rest.u32()
            return req, chunk[rest.pos:], info
        if self.local_key is None or self.local_cert is None:
            raise S.SecurityError("a secure OpenSecureChannel needs our certificate")
        if thumb and thumb.hex().upper() != S.thumbprint(self.local_cert):
            raise S.SecurityError("the message is encrypted for another certificate")
        plain = policy.asym_decrypt(self.local_key, chunk[head_len:])
        pub = S.load_cert(sender_cert).public_key()
        sk = pub.key_size // 8
        sig, data = plain[-sk:], plain[:-sk]
        policy.asym_verify(pub, sig, chunk[:head_len] + data)
        rk = self.local_key.key_size // 8
        if rk > 256:
            pad = data[-2] | (data[-1] << 8)
            data = data[:-(pad + 2)]
        else:
            data = data[:-(data[-1] + 1)]
        _seq, req = struct.unpack_from("<II", data, 0)
        return req, data[8:], info

    # ------------------------------------------------------------ symmetric (MSG, CLO)
    def max_body(self) -> int:
        room = self.remote_buffer - 16
        if self.mode == S.MODE_SIGN_ENCRYPT and self.secure:
            return room // 16 * 16 - 8 - 32 - 16
        if self.mode == S.MODE_SIGN and self.secure:
            return room - 8 - 32
        return room - 8

    def encode_msg(self, kind: bytes, request_id: int, body: bytes) -> list[bytes]:
        size = max(1024, self.max_body())
        parts = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
        return [self._chunk(kind, b"F" if i == len(parts) - 1 else b"C", request_id, p) for i, p in enumerate(parts)]

    def _chunk(self, kind: bytes, ctype: bytes, request_id: int, body: bytes) -> bytes:
        seq = struct.pack("<II", self.next_seq(), request_id)
        ids = struct.pack("<II", self.channel_id, self.token_id)
        if not self.secure or self.mode == S.MODE_NONE:
            return HEADER.pack(kind, ctype, 16 + len(seq) + len(body)) + ids + seq + body
        if self.mode == S.MODE_SIGN:
            head = HEADER.pack(kind, ctype, 16 + 8 + len(body) + 32) + ids
            return head + seq + body + self.local_keys.sign(head + seq + body)
        pad = (-(8 + len(body) + 1 + 32)) % 16
        plain = seq + body + bytes([pad]) * (pad + 1)
        head = HEADER.pack(kind, ctype, 16 + len(plain) + 32) + ids
        sig = self.local_keys.sign(head + plain)
        return head + self.local_keys.encrypt(plain + sig)

    def decode_msg(self, chunk: bytes) -> tuple[int, bytes]:
        _chan, token = struct.unpack_from("<II", chunk, 8)
        head, rest = chunk[:16], chunk[16:]
        if self.secure and self.mode != S.MODE_NONE:
            keys = self.remote_keys if token == self.token_id else self.old_remote.get(token)
            if keys is None:
                raise S.SecurityError(f"unknown security token {token}")
            if self.mode == S.MODE_SIGN_ENCRYPT:
                plain = keys.decrypt(rest)
                data, sig = plain[:-32], plain[-32:]
                keys.verify(sig, head + data)
                data = data[:-(data[-1] + 1)]
            else:
                data, sig = rest[:-32], rest[-32:]
                keys.verify(sig, head + data)
        else:
            data = rest
        if len(data) < 8:
            raise UaError("chunk too short")
        _seq, req = struct.unpack_from("<II", data, 0)
        return req, data[8:]
