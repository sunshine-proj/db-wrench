"""Connection CRUD, access resolution (grants -> effective permissions) and adapter lookup."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext
from app.auth.permissions import (
    SUPER_ADMIN,
    Perm,
    effective_connection_permissions,
)
from app.config import Settings
from app.connections.network import NetworkPolicy, validate_policy_input
from app.database.core.base import (
    ConnectionSpec,
    DatabaseAdapter,
    DbType,
    Limits,
    TlsMode,
)
from app.database.core.manager import get_adapter_manager
from app.database.document.base import DocumentAdapter
from app.database.relational.base import RelationalAdapter
from app.errors import Conflict, Forbidden, NotFound, UnsupportedCapability, ValidationFailed
from app.models import AppSetting, Connection, ConnectionGrant, Role, User, utcnow
from app.services.runtime import get_envelope, run_blocking

NETWORK_POLICY_KEY = "network_policy"
DEFAULT_PORTS = {DbType.POSTGRESQL: 5432, DbType.MYSQL: 3306, DbType.MONGODB: 27017}


# ------------------------------------------------------------------------- network policy


async def get_network_policy(db: AsyncSession, settings: Settings) -> NetworkPolicy:
    row = await db.get(AppSetting, NETWORK_POLICY_KEY)
    if row is None:
        return NetworkPolicy(
            tuple(settings.network_allow_cidrs),
            tuple(settings.network_allow_hosts),
            settings.network_allow_loopback,
        )
    return NetworkPolicy(
        tuple(row.value.get("allow_cidrs", [])),
        tuple(row.value.get("allow_hosts", [])),
        settings.network_allow_loopback,  # loopback is a deployment decision, not a UI one
    )


async def set_network_policy(
    db: AsyncSession, user_id: uuid.UUID, cidrs: list[str], hosts: list[str]
) -> None:
    try:
        validate_policy_input(cidrs, hosts)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    row = await db.get(AppSetting, NETWORK_POLICY_KEY)
    value = {"allow_cidrs": cidrs, "allow_hosts": hosts}
    if row is None:
        db.add(AppSetting(key=NETWORK_POLICY_KEY, value=value, updated_by=user_id))
    else:
        row.value, row.updated_at, row.updated_by = value, utcnow(), user_id


async def check_destination(policy: NetworkPolicy, host: str, port: int) -> None:
    await run_blocking(policy.resolve, host, port)


# ------------------------------------------------------------------------- loading


async def load_connection(db: AsyncSession, connection_id: uuid.UUID) -> Connection:
    conn = await db.get(Connection, connection_id)
    if conn is None:
        raise NotFound("Connection not found.")
    return conn


def build_spec(conn: Connection) -> ConnectionSpec:
    secret = get_envelope().decrypt_json(conn.secret, f"connection:{conn.id}")
    return ConnectionSpec(
        id=conn.id,
        db_type=DbType(conn.db_type),
        host=conn.host,
        port=conn.port,
        database=conn.database,
        username=conn.username,
        password=str(secret.get("password", "")),
        tls_mode=TlsMode(conn.tls_mode),
        tls_ca=conn.tls_ca,
        connect_timeout_s=conn.connect_timeout_s,
        pool_max=conn.pool_max,
        options=dict(conn.options or {}),
        allow_writes=not conn.read_only,
        version_key=f"{conn.updated_at.isoformat()}|{conn.tls_ca is not None}",
    )


# ------------------------------------------------------------------------- access control


@dataclass
class Access:
    connection: Connection
    permissions: frozenset[str]
    grant_role: str | None


async def resolve_access(db: AsyncSession, ctx: AuthContext, conn: Connection) -> Access:
    if ctx.is_super_admin:
        granted: set[str] = {p.value for p in Perm}
        role_name: str | None = SUPER_ADMIN
    else:
        grant = (
            await db.execute(
                select(ConnectionGrant).where(
                    ConnectionGrant.user_id == ctx.user.id,
                    ConnectionGrant.connection_id == conn.id,
                )
            )
        ).scalar_one_or_none()
        granted = set(grant.role.permissions) if grant else set()
        role_name = grant.role.name if grant else None
    perms = effective_connection_permissions(
        granted, read_only=conn.read_only, is_super_admin=ctx.is_super_admin
    )
    return Access(conn, perms, role_name)


async def require_access(
    db: AsyncSession, ctx: AuthContext, connection_id: uuid.UUID, perm: Perm
) -> Access:
    """Load the connection and check ``perm``. Users with no grant at all get 404 (no
    existence oracle); users with a grant but too few rights get 403."""
    conn = await load_connection(db, connection_id)
    access = await resolve_access(db, ctx, conn)
    if access.grant_role is None and not ctx.is_super_admin:
        raise NotFound("Connection not found.")
    if perm.value not in access.permissions:
        raise Forbidden(f"Missing permission: {perm.value}")
    return access


async def list_visible(db: AsyncSession, ctx: AuthContext) -> list[Access]:
    conns = (await db.execute(select(Connection).order_by(Connection.name))).scalars().all()
    out: list[Access] = []
    for c in conns:
        a = await resolve_access(db, ctx, c)
        if Perm.CONNECTION_VIEW.value in a.permissions:
            out.append(a)
    return out


async def open_adapter(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    connection_id: uuid.UUID,
    perm: Perm,
    family: str,
) -> tuple[Access, DatabaseAdapter]:
    access = await require_access(db, ctx, connection_id, perm)
    conn = access.connection
    if not conn.enabled:
        raise Conflict("This connection is disabled.")
    if family == "relational" and DbType(conn.db_type) == DbType.MONGODB:
        raise UnsupportedCapability("This is a MongoDB connection; use the /mongo endpoints.")
    if family == "document" and DbType(conn.db_type) != DbType.MONGODB:
        raise UnsupportedCapability("MongoDB endpoints require a MongoDB connection.")
    policy = await get_network_policy(db, settings)
    adapter = get_adapter_manager().get(build_spec(conn), policy, Limits.from_settings(settings))
    return access, adapter


def as_relational(adapter: DatabaseAdapter) -> RelationalAdapter:
    assert isinstance(adapter, RelationalAdapter)
    return adapter


def as_document(adapter: DatabaseAdapter) -> DocumentAdapter:
    assert isinstance(adapter, DocumentAdapter)
    return adapter


# ------------------------------------------------------------------------- CRUD


async def create_connection(
    db: AsyncSession,
    settings: Settings,
    ctx: AuthContext,
    *,
    data: dict[str, Any],
    password: str,
) -> Connection:
    if (await db.execute(select(Connection).where(Connection.name == data["name"]))).first():
        raise Conflict("A connection with this name already exists.")
    policy = await get_network_policy(db, settings)
    await check_destination(policy, data["host"], data["port"])
    conn = Connection(id=uuid.uuid4(), created_by=ctx.user.id, secret="", **data)
    conn.secret = get_envelope().encrypt_json({"password": password}, f"connection:{conn.id}")
    db.add(conn)
    await db.flush()
    if not ctx.is_super_admin:
        role = (await db.execute(select(Role).where(Role.name == "Database Admin"))).scalar_one()
        db.add(ConnectionGrant(user_id=ctx.user.id, connection_id=conn.id, role_id=role.id))
        await db.flush()
    return conn


async def update_connection(
    db: AsyncSession,
    settings: Settings,
    conn: Connection,
    *,
    changes: dict[str, Any],
    password: str | None,
) -> list[str]:
    renamed = "name" in changes and changes["name"] != conn.name
    if (
        renamed
        and (await db.execute(select(Connection).where(Connection.name == changes["name"]))).first()
    ):
        raise Conflict("A connection with this name already exists.")
    host = changes.get("host", conn.host)
    port = changes.get("port", conn.port)
    if host != conn.host or port != conn.port:
        policy = await get_network_policy(db, settings)
        await check_destination(policy, host, port)
    changed: list[str] = []
    for key, value in changes.items():
        if getattr(conn, key) != value:
            setattr(conn, key, value)
            changed.append(key)
    if password is not None:
        conn.secret = get_envelope().encrypt_json({"password": password}, f"connection:{conn.id}")
        changed.append("password")
    conn.updated_at = utcnow()
    get_adapter_manager().invalidate(conn.id)
    return changed


async def delete_connection(db: AsyncSession, conn: Connection) -> None:
    get_adapter_manager().invalidate(conn.id)
    await db.delete(conn)


# ------------------------------------------------------------------------- grants


async def list_grants(db: AsyncSession, conn: Connection) -> list[tuple[User, Role]]:
    rows = (
        await db.execute(
            select(User, ConnectionGrant)
            .join(ConnectionGrant, ConnectionGrant.user_id == User.id)
            .where(ConnectionGrant.connection_id == conn.id)
            .order_by(User.username)
        )
    ).all()
    return [(u, g.role) for u, g in rows]


async def replace_grants(
    db: AsyncSession,
    ctx: AuthContext,
    conn: Connection,
    grants: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """Replace the connection's grants. Granted roles may not exceed the actor's own rights."""
    roles: dict[uuid.UUID, Role] = {}
    for _, role_id in grants:
        role = await db.get(Role, role_id)
        if role is None:
            raise NotFound("Role not found.")
        if not ctx.is_super_admin:
            mine = set(ctx.user.role.permissions) | set()
            if role.name == SUPER_ADMIN or not set(role.permissions) <= mine:
                raise Forbidden("You cannot grant a role with more permissions than your own.")
        roles[role_id] = role
    for user_id, _ in grants:
        if await db.get(User, user_id) is None:
            raise NotFound("User not found.")
    existing = (
        await db.execute(select(ConnectionGrant).where(ConnectionGrant.connection_id == conn.id))
    ).scalars()
    for g in existing:
        await db.delete(g)
    await db.flush()
    seen: set[uuid.UUID] = set()
    for user_id, role_id in grants:
        if user_id in seen:
            raise ValidationFailed("Each user may appear only once.")
        seen.add(user_id)
        db.add(ConnectionGrant(user_id=user_id, connection_id=conn.id, role_id=role_id))


# ------------------------------------------------------------------------- key rotation


async def rotate_keys(db: AsyncSession, settings: Settings) -> dict[str, int]:
    """Re-wrap every stored DEK under the active KEK (payloads are not re-encrypted)."""
    env = get_envelope()
    target = settings.active_kek_id
    counts = {"connections": 0, "mfa_secrets": 0}
    for conn in (await db.execute(select(Connection))).scalars():
        if env.kek_id(conn.secret) != target:
            conn.secret = env.rewrap(conn.secret, f"connection:{conn.id}", target)
            counts["connections"] += 1
    for user in (await db.execute(select(User))).scalars():
        for attr in ("mfa_secret", "mfa_pending_secret"):
            blob = getattr(user, attr)
            if blob and env.kek_id(blob) != target:
                setattr(user, attr, env.rewrap(blob, f"mfa:{user.id}", target))
                counts["mfa_secrets"] += 1
    return counts
