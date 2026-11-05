"""Connection CRUD, access resolution (grants -> effective permissions) and adapter lookup."""

from __future__ import annotations

import dataclasses
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
from app.connections.scope import SCOPABLE, ObjectScope, ScopedGrant, scoped_permissions
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
from app.errors import (
    Conflict,
    CredentialsRequired,
    Forbidden,
    NotFound,
    UnsupportedCapability,
    ValidationFailed,
)
from app.models import (
    AppSetting,
    Connection,
    ConnectionGrant,
    Role,
    ScopeGrant,
    User,
    UserCredential,
    utcnow,
)
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


def user_spec_id(conn_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(conn_id, f"user:{user_id}")


def build_user_spec(conn: Connection, cred: UserCredential) -> ConnectionSpec:
    """The connection's settings with the *user's own* login. The shared service account in
    ``conn.secret`` is never used for a per-user connection."""
    secret = get_envelope().decrypt_json(cred.secret, f"usercred:{cred.user_id}:{conn.id}")
    base = dataclasses.replace(build_spec(conn), password="")
    return dataclasses.replace(
        base,
        id=user_spec_id(conn.id, cred.user_id),
        parent_id=conn.id,
        username=cred.username,
        password=str(secret.get("password", "")),
        version_key=f"{base.version_key}|{cred.updated_at.isoformat()}",
    )


async def load_user_credential(
    db: AsyncSession, user_id: uuid.UUID, conn_id: uuid.UUID
) -> UserCredential | None:
    return (
        await db.execute(
            select(UserCredential).where(
                UserCredential.user_id == user_id, UserCredential.connection_id == conn_id
            )
        )
    ).scalar_one_or_none()


async def spec_for(db: AsyncSession, ctx: AuthContext, conn: Connection) -> ConnectionSpec:
    if conn.credential_model != "per_user":
        return build_spec(conn)
    cred = await load_user_credential(db, ctx.user.id, conn.id)
    if cred is None:
        raise CredentialsRequired(
            "This connection uses personal database credentials. Add yours under "
            "'Your credentials' for this connection."
        )
    return build_user_spec(conn, cred)


async def set_user_credentials(
    db: AsyncSession,
    settings: Settings,
    ctx: AuthContext,
    conn: Connection,
    *,
    username: str,
    password: str,
) -> UserCredential:
    """Store the caller's own login after proving it works against the server."""
    if conn.credential_model != "per_user":
        raise Conflict("This connection uses a shared service account.")
    if not username:
        raise ValidationFailed("A username is required.")
    row = await load_user_credential(db, ctx.user.id, conn.id)
    now = utcnow()
    blob = get_envelope().encrypt_json({"password": password}, f"usercred:{ctx.user.id}:{conn.id}")
    probe = UserCredential(
        user_id=ctx.user.id, connection_id=conn.id, username=username, secret=blob, updated_at=now
    )
    policy = await get_network_policy(db, settings)
    manager = get_adapter_manager()
    spec = build_user_spec(conn, probe)
    adapter = manager.get(spec, policy, Limits.from_settings(settings))
    result = await run_blocking(adapter.test)
    if not result.ok:
        manager.invalidate(spec.id)
        raise ValidationFailed(
            f"The credentials were rejected: {result.error or 'connection failed'}"
        )
    if row is None:
        db.add(probe)
        row = probe
    else:
        row.username, row.secret, row.updated_at = username, blob, now
    manager.invalidate(spec.id)  # the next use builds the adapter from the stored row
    await db.flush()
    return row


async def delete_user_credentials(db: AsyncSession, user_id: uuid.UUID, conn: Connection) -> bool:
    row = await load_user_credential(db, user_id, conn.id)
    if row is None:
        return False
    await db.delete(row)
    get_adapter_manager().invalidate(user_spec_id(conn.id, user_id))
    return True


# ------------------------------------------------------------------------- access control


@dataclass
class Access:
    connection: Connection
    permissions: frozenset[str]  # effective for the requested target (scope included)
    grant_role: str | None
    connection_level: frozenset[str] = frozenset()  # without scoped grants
    scoped: tuple[ScopedGrant, ...] = ()

    @property
    def has_scoped(self) -> bool:
        return bool(self.scoped)


async def _scoped_grants(
    db: AsyncSession, ctx: AuthContext, conn: Connection
) -> tuple[ScopedGrant, ...]:
    if ctx.is_super_admin:
        return ()
    rows = (
        await db.execute(
            select(ScopeGrant).where(
                ScopeGrant.user_id == ctx.user.id, ScopeGrant.connection_id == conn.id
            )
        )
    ).scalars()
    out: list[ScopedGrant] = []
    for g in rows:
        perms = effective_connection_permissions(
            set(g.role.permissions) & SCOPABLE, read_only=conn.read_only, is_super_admin=False
        )
        out.append(ScopedGrant(g.database, g.schema, g.object, perms))
    return tuple(out)


async def resolve_access(
    db: AsyncSession, ctx: AuthContext, conn: Connection, scope: ObjectScope | None = None
) -> Access:
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
    base = effective_connection_permissions(
        granted, read_only=conn.read_only, is_super_admin=ctx.is_super_admin
    )
    scoped = await _scoped_grants(db, ctx, conn)
    perms = base
    if scoped:
        perms = perms | {Perm.CONNECTION_VIEW.value}  # the connection is visible to them
        if scope is not None:
            perms = perms | scoped_permissions(scoped, scope)
    if ctx.token is not None:
        # An API token is the intersection of its scopes and its owner's rights, optionally
        # limited to some connections (others do not exist for it).
        allowed = ctx.token.connection_ids
        if allowed is not None and str(conn.id) not in allowed:
            return Access(conn, frozenset(), None)
        token_scopes = frozenset(ctx.token.scopes)
        perms, base = perms & token_scopes, base & token_scopes
    return Access(conn, frozenset(perms), role_name, frozenset(base), scoped)


async def require_access(
    db: AsyncSession,
    ctx: AuthContext,
    connection_id: uuid.UUID,
    perm: Perm,
    scope: ObjectScope | None = None,
) -> Access:
    """Load the connection and check ``perm`` (for ``scope`` when given). Users with no grant at
    all get 404 (no existence oracle); users with a grant but too few rights get 403."""
    conn = await load_connection(db, connection_id)
    access = await resolve_access(db, ctx, conn, scope)
    if access.grant_role is None and not access.scoped and not ctx.is_super_admin:
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
    scope: ObjectScope | None = None,
) -> tuple[Access, DatabaseAdapter]:
    access = await require_access(db, ctx, connection_id, perm, scope)
    conn = access.connection
    if not conn.enabled:
        raise Conflict("This connection is disabled.")
    if family == "relational" and DbType(conn.db_type) == DbType.MONGODB:
        raise UnsupportedCapability("This is a MongoDB connection; use the /mongo endpoints.")
    if family == "document" and DbType(conn.db_type) != DbType.MONGODB:
        raise UnsupportedCapability("MongoDB endpoints require a MongoDB connection.")
    policy = await get_network_policy(db, settings)
    spec = await spec_for(db, ctx, conn)
    adapter = get_adapter_manager().get(spec, policy, Limits.from_settings(settings))
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


# ------------------------------------------------------------------------- scoped grants


@dataclass
class ScopeGrantSpec:
    user_id: uuid.UUID
    role_id: uuid.UUID
    database: str
    schema: str = ""
    object: str = ""


async def list_scope_grants(
    db: AsyncSession, conn: Connection
) -> list[tuple[User, Role, ScopeGrant]]:
    rows = (
        await db.execute(
            select(User, ScopeGrant)
            .join(ScopeGrant, ScopeGrant.user_id == User.id)
            .where(ScopeGrant.connection_id == conn.id)
            .order_by(User.username, ScopeGrant.database, ScopeGrant.schema, ScopeGrant.object)
        )
    ).all()
    return [(u, g.role, g) for u, g in rows]


async def replace_scope_grants(
    db: AsyncSession,
    ctx: AuthContext,
    access: Access,
    specs: list[ScopeGrantSpec],
) -> None:
    """Replace the connection's scoped grants. A granter cannot hand out more than they hold."""
    conn = access.connection
    has_schemas = DbType(conn.db_type) == DbType.POSTGRESQL
    roles: dict[uuid.UUID, Role] = {}
    seen: set[tuple[uuid.UUID, str, str, str]] = set()
    for sp in specs:
        if not sp.database or any("\x00" in v for v in (sp.database, sp.schema, sp.object)):
            raise ValidationFailed("A scoped grant needs a database name.")
        if sp.schema and not has_schemas:
            raise ValidationFailed("This engine has no schema level; leave schema empty.")
        if sp.object and has_schemas and not sp.schema:
            raise ValidationFailed("A table grant on PostgreSQL needs its schema.")
        key = (sp.user_id, sp.database, sp.schema, sp.object)
        if key in seen:
            raise ValidationFailed("Each user/scope pair may appear only once.")
        seen.add(key)
        role = roles.get(sp.role_id) or await db.get(Role, sp.role_id)
        if role is None:
            raise NotFound("Role not found.")
        roles[sp.role_id] = role
        if await db.get(User, sp.user_id) is None:
            raise NotFound("User not found.")
        if not ctx.is_super_admin:
            wanted = set(role.permissions) & SCOPABLE
            if role.name == SUPER_ADMIN or not wanted <= set(access.connection_level):
                raise Forbidden(
                    "You cannot grant more permissions than you hold on this connection."
                )
    for g in (
        await db.execute(select(ScopeGrant).where(ScopeGrant.connection_id == conn.id))
    ).scalars():
        await db.delete(g)
    await db.flush()
    for sp in specs:
        db.add(
            ScopeGrant(
                user_id=sp.user_id,
                connection_id=conn.id,
                database=sp.database,
                schema=sp.schema,
                object=sp.object,
                role_id=sp.role_id,
            )
        )


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
    counts["user_credentials"] = 0
    for cred in (await db.execute(select(UserCredential))).scalars():
        ctxt = f"usercred:{cred.user_id}:{cred.connection_id}"
        if env.kek_id(cred.secret) != target:
            cred.secret = env.rewrap(cred.secret, ctxt, target)
            counts["user_credentials"] += 1
    for user in (await db.execute(select(User))).scalars():
        for attr in ("mfa_secret", "mfa_pending_secret"):
            blob = getattr(user, attr)
            if blob and env.kek_id(blob) != target:
                setattr(user, attr, env.rewrap(blob, f"mfa:{user.id}", target))
                counts["mfa_secrets"] += 1
    return counts
