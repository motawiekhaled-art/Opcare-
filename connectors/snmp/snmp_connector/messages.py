"""SNMP messages: v1/v2c community messages (RFC 1157, RFC 1901) and SNMPv3 with USM (RFC 3412, RFC 3414),
HMAC-SHA-2 authentication (RFC 7860) and AES-128 privacy (RFC 3826).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from .ber import (INTEGER, OCTET_STRING, SEQUENCE, BerError, Pdu, dec_int, dec_pdu, enc_int, enc_str, items, read_tlv,
                  seq, tlv)

V1, V2C, V3 = 0, 1, 3
VERSION = {"1": V1, "2c": V2C, "3": V3}

# USM statistics reported by an agent (RFC 3414 section 5) -> meaning
USM_REPORTS = {
    "1.3.6.1.6.3.15.1.1.1.0": "usmStatsUnsupportedSecLevels",
    "1.3.6.1.6.3.15.1.1.2.0": "usmStatsNotInTimeWindows",
    "1.3.6.1.6.3.15.1.1.3.0": "usmStatsUnknownUserNames",
    "1.3.6.1.6.3.15.1.1.4.0": "usmStatsUnknownEngineIDs",
    "1.3.6.1.6.3.15.1.1.5.0": "usmStatsWrongDigests",
    "1.3.6.1.6.3.15.1.1.6.0": "usmStatsDecryptionErrors",
}


# ------------------------------------------------------------------ v1 / v2c
def encode_community(version: int, community: bytes, pdu: Pdu) -> bytes:
    return seq(enc_int(version), enc_str(community), pdu.encode())


def peek_version(data: bytes) -> int:
    tag, content, _ = read_tlv(data)
    if tag != SEQUENCE:
        raise BerError("not an SNMP message")
    t, v, _ = read_tlv(content)
    if t != INTEGER:
        raise BerError("no version")
    return dec_int(v)


def decode_community(data: bytes) -> tuple[int, bytes, Pdu]:
    tag, content, _ = read_tlv(data)
    parts = items(content)
    if tag != SEQUENCE or len(parts) != 3 or parts[1][0] != OCTET_STRING:
        raise BerError("bad community message")
    return dec_int(parts[0][1]), parts[1][1], dec_pdu(*parts[2])


# ------------------------------------------------------------------ USM keys (RFC 3414 A.2, RFC 7860)
AUTH = {  # name -> (hash, MAC length)
    "MD5": (hashlib.md5, 12), "SHA": (hashlib.sha1, 12), "SHA-224": (hashlib.sha224, 16),
    "SHA-256": (hashlib.sha256, 24), "SHA-384": (hashlib.sha384, 32), "SHA-512": (hashlib.sha512, 48)}


def password_to_key(password: str | bytes, hash_name: str) -> bytes:
    """Ku: hash of 1 MiB of the repeated password."""
    pw = password.encode() if isinstance(password, str) else password
    if len(pw) < 8:
        raise ValueError("SNMPv3 passwords must be at least 8 characters")
    h = AUTH[hash_name][0]()
    rep = (pw * (64 // len(pw) + 2))
    count, i = 0, 0
    while count < 1048576:
        chunk = rep[i:i + 64]
        if len(chunk) < 64:
            i = 0
            chunk = rep[:64]
        h.update(chunk)
        i = (i + 64) % len(pw)
        count += 64
    return h.digest()


def localize(ku: bytes, engine_id: bytes, hash_name: str) -> bytes:
    """Kul = H(Ku || engineID || Ku)."""
    return AUTH[hash_name][0](ku + engine_id + ku).digest()


def _ku_cached(password: str, hash_name: str, _cache: dict = {}) -> bytes:  # noqa: B006 - intentional cache
    key = (hashlib.sha256(password.encode()).digest(), hash_name)
    if key not in _cache:
        _cache[key] = password_to_key(password, hash_name)
    return _cache[key]


@dataclass
class UsmUser:
    """USM credentials. Keys are localized per authoritative engine."""
    username: str
    level: str = "authPriv"              # noAuthNoPriv | authNoPriv | authPriv
    auth_protocol: str = "SHA-256"
    auth_password: str | None = None
    priv_protocol: str = "AES"
    priv_password: str | None = None

    @property
    def auth(self) -> bool:
        return self.level in ("authNoPriv", "authPriv")

    @property
    def priv(self) -> bool:
        return self.level == "authPriv"

    def keys(self, engine_id: bytes) -> tuple[bytes | None, bytes | None]:
        ak = localize(_ku_cached(self.auth_password, self.auth_protocol), engine_id, self.auth_protocol) \
            if self.auth else None
        pk = localize(_ku_cached(self.priv_password, self.auth_protocol), engine_id, self.auth_protocol)[:16] \
            if self.priv else None
        return ak, pk


# ------------------------------------------------------------------ AES-128-CFB (RFC 3826)
def _aes(key: bytes, iv: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms   # Apache-2.0 OR BSD-3-Clause
    try:                                    # newer cryptography keeps CFB (needed by RFC 3826) under "decrepit"
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.modes import CFB
    return Cipher(algorithms.AES(key), CFB(iv))


def aes_iv(boots: int, time_: int, salt: bytes) -> bytes:
    return boots.to_bytes(4, "big") + time_.to_bytes(4, "big") + salt


def aes_encrypt(key: bytes, boots: int, time_: int, salt: bytes, plain: bytes) -> bytes:
    e = _aes(key, aes_iv(boots, time_, salt)).encryptor()
    return e.update(plain) + e.finalize()


def aes_decrypt(key: bytes, boots: int, time_: int, salt: bytes, data: bytes) -> bytes:
    d = _aes(key, aes_iv(boots, time_, salt)).decryptor()
    return d.update(data) + d.finalize()


# ------------------------------------------------------------------ SNMPv3 message
FLAG_AUTH, FLAG_PRIV, FLAG_REPORTABLE = 1, 2, 4
MAX_SIZE = 65507


@dataclass
class V3Message:
    msg_id: int
    flags: int
    engine_id: bytes
    boots: int
    time: int
    username: bytes
    auth_params: bytes
    priv_params: bytes
    context_engine_id: bytes = b""
    context_name: bytes = b""
    pdu: Pdu | None = None
    encrypted: bytes | None = None       # set when the scoped PDU could not be decrypted yet


def _usm_params(engine_id: bytes, boots: int, time_: int, user: bytes, auth: bytes, priv: bytes) -> bytes:
    return seq(enc_str(engine_id), enc_int(boots), enc_int(time_), enc_str(user), enc_str(auth), enc_str(priv))


def encode_v3(msg_id: int, pdu: Pdu, user: UsmUser | None, engine_id: bytes, boots: int, time_: int,
              keys: tuple[bytes | None, bytes | None], context_engine_id: bytes, context_name: bytes = b"",
              reportable: bool = True, salt: bytes | None = None) -> bytes:
    """user None = discovery message (noAuthNoPriv, empty user name)."""
    auth = bool(user and user.auth)
    priv = bool(user and user.priv)
    flags = (FLAG_AUTH if auth else 0) | (FLAG_PRIV if priv else 0) | (FLAG_REPORTABLE if reportable else 0)
    scoped = seq(enc_str(context_engine_id), enc_str(context_name), pdu.encode())
    priv_params = b""
    if priv:
        salt = salt or os.urandom(8)
        priv_params = salt
        scoped = enc_str(aes_encrypt(keys[1], boots, time_, salt, scoped))
    mac_len = AUTH[user.auth_protocol][1] if auth else 0
    header = seq(enc_int(msg_id), enc_int(MAX_SIZE), enc_str(bytes([flags])), enc_int(3))

    def build(auth_params: bytes) -> bytes:
        sec = enc_str(_usm_params(engine_id, boots, time_, (user.username.encode() if user else b""),
                                  auth_params, priv_params))
        return seq(enc_int(3), header, sec, scoped)

    whole = build(b"\x00" * mac_len)
    if not auth:
        return whole
    mac = hmac.new(keys[0], whole, AUTH[user.auth_protocol][0]).digest()[:mac_len]
    return build(mac)


def decode_v3(data: bytes) -> tuple[V3Message, bytes]:
    """Parse without checking authentication. Returns (message, whole data) for verify_v3/decrypt_v3."""
    tag, content, _ = read_tlv(data)
    parts = items(content)
    if tag != SEQUENCE or len(parts) != 4 or dec_int(parts[0][1]) != 3:
        raise BerError("not an SNMPv3 message")
    hdr = items(parts[1][1])
    msg_id, flags = dec_int(hdr[0][1]), hdr[2][1][0] if hdr[2][1] else 0
    if dec_int(hdr[3][1]) != 3:
        raise BerError("not the USM security model")
    usm = items(read_tlv(parts[2][1])[1])
    m = V3Message(msg_id, flags, usm[0][1], dec_int(usm[1][1]), dec_int(usm[2][1]), usm[3][1], usm[4][1], usm[5][1])
    st, sc = parts[3]
    if flags & FLAG_PRIV:
        if st != OCTET_STRING:
            raise BerError("encrypted PDU expected")
        m.encrypted = sc
    else:
        _scoped(m, st, sc)
    return m, data


def _scoped(m: V3Message, tag: int, content: bytes) -> None:
    if tag != SEQUENCE:
        raise BerError("bad scoped PDU")
    sp = items(content)
    m.context_engine_id, m.context_name = sp[0][1], sp[1][1]
    m.pdu = dec_pdu(*sp[2])


def verify_v3(m: V3Message, whole: bytes, user: UsmUser, auth_key: bytes) -> bool:
    """Check the HMAC: recompute over the message with authParams zeroed."""
    mac_len = AUTH[user.auth_protocol][1]
    if len(m.auth_params) != mac_len:
        return False
    i = whole.find(m.auth_params)
    if i < 0:
        return False
    zeroed = whole[:i] + b"\x00" * mac_len + whole[i + mac_len:]
    want = hmac.new(auth_key, zeroed, AUTH[user.auth_protocol][0]).digest()[:mac_len]
    return hmac.compare_digest(want, m.auth_params)


def decrypt_v3(m: V3Message, priv_key: bytes) -> None:
    if m.encrypted is None:
        return
    if len(m.priv_params) != 8:
        raise BerError("bad privacy parameters")
    plain = aes_decrypt(priv_key, m.boots, m.time, m.priv_params, m.encrypted)
    tag, content, _ = read_tlv(plain)
    m.encrypted = None
    _scoped(m, tag, content)


__all__ = ["V1", "V2C", "V3", "VERSION", "USM_REPORTS", "encode_community", "decode_community", "peek_version",
           "password_to_key", "localize", "UsmUser", "encode_v3", "decode_v3", "verify_v3", "decrypt_v3", "AUTH",
           "aes_encrypt", "aes_decrypt", "tlv"]
