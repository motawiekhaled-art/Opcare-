"""OPC UA security policies (IEC 62541-7 / -6 section 6.7): None, Basic256Sha256, Aes128_Sha256_RsaOaep,
Aes256_Sha256_RsaPss. Asymmetric RSA for OpenSecureChannel and session signatures, AES-CBC + HMAC-SHA256 for
messages, P_SHA256 key derivation. Uses `cryptography` (Apache-2.0 OR BSD-3-Clause) only for the primitives.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BASE = "http://opcfoundation.org/UA/SecurityPolicy#"
NONE_URI = BASE + "None"
MODE_NONE, MODE_SIGN, MODE_SIGN_ENCRYPT = 1, 2, 3
MODES = {"None": MODE_NONE, "Sign": MODE_SIGN, "SignAndEncrypt": MODE_SIGN_ENCRYPT}
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
RSA_PSS_SHA256 = "http://opcfoundation.org/UA/security/rsa-pss-sha2-256"
RSA_OAEP = "http://www.w3.org/2001/04/xmlenc#rsa-oaep"
RSA_OAEP_SHA256 = "http://opcfoundation.org/UA/security/rsa-oaep-sha2-256"


class SecurityError(Exception):
    """Signature or decryption failed, or a certificate is not acceptable."""


@dataclass(frozen=True)
class Policy:
    name: str
    sym_key: int            # AES key length (bytes); 0 = no security
    sig_key: int = 32       # HMAC-SHA256 key length
    oaep_sha256: bool = False
    pss: bool = False

    @property
    def uri(self) -> str:
        return BASE + self.name

    @property
    def none(self) -> bool:
        return self.sym_key == 0

    # ---------------------------------------------------------- asymmetric
    def _oaep(self):
        h = hashes.SHA256() if self.oaep_sha256 else hashes.SHA1()
        return padding.OAEP(mgf=padding.MGF1(h), algorithm=h, label=None)

    def oaep_overhead(self) -> int:
        return 66 if self.oaep_sha256 else 42

    def asym_sign(self, key, data: bytes) -> bytes:
        if self.pss:
            return key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def asym_verify(self, pub, sig: bytes, data: bytes) -> None:
        try:
            if self.pss:
                pub.verify(sig, data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
            else:
                pub.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        except Exception:                                   # noqa: BLE001 - InvalidSignature and friends
            raise SecurityError("asymmetric signature is not valid") from None

    def asym_encrypt(self, pub, data: bytes) -> bytes:
        size = pub.key_size // 8
        block = size - self.oaep_overhead()
        return b"".join(pub.encrypt(data[i:i + block], self._oaep()) for i in range(0, len(data), block))

    def asym_decrypt(self, key, data: bytes) -> bytes:
        size = key.key_size // 8
        if len(data) % size:
            raise SecurityError("encrypted block size mismatch")
        try:
            return b"".join(key.decrypt(data[i:i + size], self._oaep()) for i in range(0, len(data), size))
        except Exception:                                   # noqa: BLE001
            raise SecurityError("cannot decrypt (wrong certificate?)") from None

    @property
    def signature_algorithm(self) -> str:
        return RSA_PSS_SHA256 if self.pss else RSA_SHA256

    @property
    def password_algorithm(self) -> str:
        return RSA_OAEP_SHA256 if self.oaep_sha256 else RSA_OAEP

    # ---------------------------------------------------------- symmetric
    def derive(self, secret: bytes, seed: bytes) -> "Keys":
        n = self.sig_key + self.sym_key + 16
        out, a = b"", seed
        while len(out) < n:                                  # P_SHA256 (RFC 5246 section 5)
            a = hmac.new(secret, a, hashlib.sha256).digest()
            out += hmac.new(secret, a + seed, hashlib.sha256).digest()
        return Keys(out[:self.sig_key], out[self.sig_key:self.sig_key + self.sym_key],
                    out[self.sig_key + self.sym_key:n])


@dataclass
class Keys:
    signing: bytes
    encrypting: bytes
    iv: bytes

    def sign(self, data: bytes) -> bytes:
        return hmac.new(self.signing, data, hashlib.sha256).digest()

    def verify(self, sig: bytes, data: bytes) -> None:
        if not hmac.compare_digest(self.sign(data), sig):
            raise SecurityError("message signature is not valid")

    def encrypt(self, data: bytes) -> bytes:
        e = Cipher(algorithms.AES(self.encrypting), modes.CBC(self.iv)).encryptor()
        return e.update(data) + e.finalize()

    def decrypt(self, data: bytes) -> bytes:
        if len(data) % 16:
            raise SecurityError("encrypted message is not a multiple of the block size")
        d = Cipher(algorithms.AES(self.encrypting), modes.CBC(self.iv)).decryptor()
        return d.update(data) + d.finalize()


POLICIES = {p.name: p for p in (Policy("None", 0), Policy("Basic256Sha256", 32), Policy("Aes128_Sha256_RsaOaep", 16),
                                Policy("Aes256_Sha256_RsaPss", 32, oaep_sha256=True, pss=True))}
BY_URI = {p.uri: p for p in POLICIES.values()}


def nonce(policy: Policy) -> bytes:
    return os.urandom(32) if not policy.none else b""


# ------------------------------------------------------------------ certificates
def load_cert(der_or_pem: bytes | str) -> x509.Certificate:
    data = der_or_pem.encode() if isinstance(der_or_pem, str) else der_or_pem
    return x509.load_pem_x509_certificate(data) if data.lstrip().startswith(b"-----") else x509.load_der_x509_certificate(data)


def load_key(pem: bytes | str):
    data = pem.encode() if isinstance(pem, str) else pem
    return serialization.load_pem_private_key(data, password=None)


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def thumbprint(cert_der: bytes) -> str:
    return hashlib.sha1(cert_der).hexdigest().upper()


def application_uri(cert: x509.Certificate) -> str | None:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        return uris[0] if uris else None
    except x509.ExtensionNotFound:
        return None


def make_application_certificate(application_uri: str, common_name: str, hostname: str = "localhost",
                                 days: int = 3650, key_size: int = 2048) -> tuple[str, str]:
    """Self-signed OPC UA application instance certificate (Part 6 section 6.2.2). Returns (cert PEM, key PEM)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Opcare Smart Services")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(application_uri),
                                                        x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                         data_encipherment=True, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())
