"""Database users, roles and privileges for relational adapters (Phase 6).

Rules enforced here, independent of the HTTP layer:

* privileges come from **allowlists** per object kind; global/superuser-level rights (superuser,
  replication, ``FILE``, ``SUPER``, ``GRANT OPTION``, ...) can never be handed out;
* the objects a grant targets (database, schema, table) and the principal itself must exist in
  fetched metadata; new principal names match a conservative pattern;
* superusers, system accounts and **the account DB Wrench itself connects with** are protected:
  they can be listed but never altered, dropped or granted to (no self-lockout, no tampering with
  the account that holds the keys);
* passwords are never stored by DB Wrench; on PostgreSQL they are hashed client-side (SCRAM) before
  they reach the server, so the plaintext never appears in server logs;
* writes use a dedicated, autocommit, writable connection (ADR-0011) and need a writable connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.errors import Forbidden, ValidationFailed

MIN_PASSWORD = 12
MAX_PASSWORD = 256
SCOPE_KINDS = ("database", "schema", "table", "all_tables")


@dataclass
class PrincipalInfo:
    name: str
    kind: str  # "role" | "user"
    can_login: bool = True
    protected: bool = False  # superuser / system account / the connection's own account
    host: str | None = None  # MySQL account host
    member_of: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class GrantEntry:
    kind: str  # database | schema | table | global
    database: str | None
    schema: str | None
    object: str | None
    privileges: list[str]
    grantable: bool = False


@dataclass(frozen=True)
class GrantTarget:
    kind: str
    database: str | None = None
    schema: str | None = None
    object: str | None = None


def check_password(password: str) -> str:
    if not isinstance(password, str) or "\x00" in password:
        raise ValidationFailed("Invalid password.")
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValidationFailed(f"Passwords need {MIN_PASSWORD}-{MAX_PASSWORD} characters.")
    return password


def normalise_privileges(requested: list[str], allowed: frozenset[str]) -> list[str]:
    """Upper-case, de-duplicate and check against the allowlist. ``ALL`` expands to the allowlist
    (never to an engine's own ``ALL``, which may carry more than DB Wrench hands out)."""
    if not requested:
        raise ValidationFailed("Choose at least one privilege.")
    out: list[str] = []
    for p in requested:
        key = " ".join(str(p).upper().split())
        if key in ("ALL", "ALL PRIVILEGES"):
            return sorted(allowed)
        if key not in allowed:
            raise ValidationFailed(f"Privilege {p!r} cannot be granted or revoked here.")
        if key not in out:
            out.append(key)
    return out


def refuse_protected(info: PrincipalInfo | None, name: str) -> PrincipalInfo:
    if info is None:
        from app.errors import NotFound

        raise NotFound(f"{name!r} was not found.")
    if info.protected:
        raise Forbidden(
            f"{name!r} is a superuser, a system account or the account DB Wrench connects "
            "with; it cannot be changed here."
        )
    return info
