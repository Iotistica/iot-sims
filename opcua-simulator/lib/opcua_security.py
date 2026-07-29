"""OPC UA transport security: policy parsing, server certificate bootstrap,
and username/password authentication backed by the existing admin `users` table.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from asyncua import ua
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.user_managers import UserManager
from cryptography.x509.oid import ExtendedKeyUsageOID

import lib.state as state
from lib.db import verify_password

log = logging.getLogger("opcua-sim.security")

# Every policy asyncua's SECURITY_POLICY_TYPE_MAP knows about (see
# asyncua/crypto/security_policies.py), keyed by the plain enum member name
# so it can be selected via a simple comma-separated env var.
SECURITY_POLICY_MAP: dict[str, ua.SecurityPolicyType] = {
    name: member for name, member in ua.SecurityPolicyType.__members__.items()
}


def parse_security_policies(env_value: str) -> list[ua.SecurityPolicyType]:
    """Parse a comma-separated list of SecurityPolicyType names.

    Raises ValueError with the offending name(s) so a typo in the env var
    fails loudly at startup instead of silently running with no policies.
    """
    names = [n.strip() for n in env_value.split(",") if n.strip()]
    if not names:
        raise ValueError("OPCUA_SECURITY_POLICIES must not be empty")

    unknown = [n for n in names if n not in SECURITY_POLICY_MAP]
    if unknown:
        raise ValueError(
            f"Unknown OPC UA security policy name(s): {unknown!r}. "
            f"Valid names: {sorted(SECURITY_POLICY_MAP)}"
        )
    return [SECURITY_POLICY_MAP[n] for n in names]


async def ensure_opcua_certificate(cert_dir: Path, app_uri: str, host_name: str) -> tuple[Path, Path]:
    """Generate (or reuse/regenerate-if-invalid) the OPC UA server's own
    application certificate + private key. Returns (key_path, cert_path)."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    key_path = cert_dir / "server_private_key.pem"
    cert_path = cert_dir / "server_certificate.der"

    await setup_self_signed_certificate(
        key_file=key_path,
        cert_file=cert_path,
        app_uri=app_uri,
        host_name=host_name,
        cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
        subject_attrs={"organizationName": "Iotistica"},
    )
    return key_path, cert_path


class SimUserManager(UserManager):
    """Authenticates OPC UA sessions against the same `users` table used by
    the admin web UI's JWT login (lib/db.py), instead of a second store."""

    def get_user(
        self,
        iserver,
        username: Optional[str] = None,
        password: Optional[str] = None,
        certificate=None,
    ) -> Optional[User]:
        if username is None and password is None:
            # Anonymous session — asyncua only calls get_user() for a token
            # type that's actually enabled (see set_identity_tokens below),
            # so this path is unreachable when OPCUA_REQUIRE_AUTH is set.
            return User(role=UserRole.User)

        if username is None or password is None:
            return None

        row = state.db.get_user_by_username(username)
        if row is None or not verify_password(password, row["password_hash"]):
            log.warning("OPC UA auth failed for username=%r", username)
            return None
        return User(role=UserRole.Admin, name=username)
