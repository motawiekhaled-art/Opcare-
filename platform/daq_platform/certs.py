"""OPC UA application instance certificates for the connector (IEC 62541-6 section 6.2.2).

The platform creates the certificate and keeps the private key as a secret (never returned by the API). The public
certificate is shown so the client can add it to the OPC UA server's trust list.
"""
from __future__ import annotations

import datetime
import hashlib
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def make_opcua_certificate(name: str, days: int = 3650, key_size: int = 2048) -> tuple[str, str, dict]:
    """Returns (certificate PEM, private key PEM, summary)."""
    host = socket.gethostname() or "localhost"
    uri = f"urn:opcare:daq:{name}"
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Opcare DAQ {name}"[:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Opcare Smart Services")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri), x509.DNSName(host)]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                         data_encipherment=True, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert_pem, key_pem, summary(cert_pem)


def summary(cert_pem: str) -> dict:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    der = cert.public_bytes(serialization.Encoding.DER)
    try:
        uris = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound:
        uris = []
    return {"thumbprint": hashlib.sha1(der).hexdigest().upper(), "application_uri": uris[0] if uris else None,
            "subject": cert.subject.rfc4514_string(), "not_after": cert.not_valid_after_utc.strftime("%Y-%m-%d"),
            "certificate_pem": cert_pem}
