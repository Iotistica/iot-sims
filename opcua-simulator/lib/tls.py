"""Self-signed PEM certificate bootstrap for the admin REST/WS API's HTTPS
listener. Separate from lib/opcua_security.py because uvicorn needs PEM
(not the DER asyncua's own cert helper produces) and this has nothing to do
with the OPC UA protocol itself.
"""
from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_VALIDITY_DAYS = 730


def _is_valid(cert_path: Path) -> bool:
    if not cert_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (ValueError, OSError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return cert.not_valid_before_utc <= now < cert.not_valid_after_utc


def ensure_admin_tls_certificate(cert_dir: Path, common_name: str) -> tuple[Path, Path]:
    """Generate (or reuse, if still valid) a self-signed PEM cert+key pair
    for the admin API's HTTPS listener. Returns (key_path, cert_path)."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    key_path = cert_dir / "admin_key.pem"
    cert_path = cert_dir / "admin_cert.pem"

    if key_path.exists() and _is_valid(cert_path):
        return key_path, cert_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(common_name),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path
