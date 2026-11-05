"""Connection management endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.schemas import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionTestOut,
    ConnectionUpdate,
    CredentialsIn,
    CredentialsOut,
    GrantOut,
    GrantsUpdate,
    ScopeGrantOut,
    ScopeGrantsUpdate,
)
from app.audit import service as audit
from app.auth.deps import AppSettings, AuthContext, CurrentAuth, DbSession, require_app_permission
from app.auth.permissions import Perm
from app.connections import service as svc
from app.database.adapters import capabilities_for
from app.database.core.base import DbType, Limits
from app.database.core.manager import get_adapter_manager
from app.errors import AppError, Forbidden
from app.models import Connection
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections", tags=["connections"])
CanCreate = Annotated[AuthContext, Depends(require_app_permission(Perm.CONNECTION_CREATE))]


def connection_out(c: Connection, permissions: frozenset[str]) -> ConnectionOut:
    return ConnectionOut(
        id=c.id,
        name=c.name,
        db_type=DbType(c.db_type),
        environment=c.environment,
        host=c.host,
        port=c.port,
        database=c.database,
        username=c.username,
        has_password=True,
        credential_model=c.credential_model,
        tls_mode=c.tls_mode,
        has_tls_ca=bool(c.tls_ca),
        connect_timeout_s=c.connect_timeout_s,
        pool_max=c.pool_max,
        read_only=c.read_only,
        enabled=c.enabled,
        options=c.options or {},
        created_at=c.created_at,
        updated_at=c.updated_at,
        permissions=sorted(permissions),
        capabilities=capabilities_for(DbType(c.db_type)),
    )


@router.get("", response_model=list[ConnectionOut])
async def list_connections(db: DbSession, ctx: CurrentAuth) -> list[ConnectionOut]:
    return [connection_out(a.connection, a.permissions) for a in await svc.list_visible(db, ctx)]


@router.post("", status_code=201, response_model=ConnectionOut)
async def create_connection(
    body: ConnectionCreate, db: DbSession, settings: AppSettings, ctx: CanCreate
) -> ConnectionOut:
    data = body.model_dump(exclude={"password"})
    data["db_type"] = body.db_type.value
    data["tls_mode"] = body.tls_mode.value
    conn = await svc.create_connection(
        db, settings, ctx, data=data, password=body.password.get_secret_value()
    )
    await audit.record(
        "connection.create",
        actor=ctx.actor,
        connection_id=conn.id,
        target={"name": conn.name, "type": conn.db_type, "host": conn.host, "port": conn.port},
        detail={"environment": conn.environment, "read_only": conn.read_only},
    )
    access = await svc.resolve_access(db, ctx, conn)
    return connection_out(conn, access.permissions)


@router.get("/{cid}", response_model=ConnectionOut)
async def get_connection(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> ConnectionOut:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_VIEW)
    return connection_out(a.connection, a.permissions)


@router.patch("/{cid}", response_model=ConnectionOut)
async def update_connection(
    cid: uuid.UUID, body: ConnectionUpdate, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> ConnectionOut:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_EDIT)
    changes = body.model_dump(exclude_unset=True, exclude={"password"})
    if "tls_mode" in changes and changes["tls_mode"] is not None:
        changes["tls_mode"] = changes["tls_mode"].value
    changes = {k: v for k, v in changes.items() if v is not None or k in ("database", "tls_ca")}
    password = body.password.get_secret_value() if body.password is not None else None
    changed = await svc.update_connection(
        db, settings, a.connection, changes=changes, password=password
    )
    await audit.record(
        "connection.update",
        actor=ctx.actor,
        connection_id=cid,
        target={"name": a.connection.name},
        detail={"changed": changed},  # field names only, never values
    )
    a = await svc.resolve_access(db, ctx, a.connection)
    return connection_out(a.connection, a.permissions)


@router.delete("/{cid}", status_code=204)
async def delete_connection(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> None:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_DELETE)
    name = a.connection.name
    await svc.delete_connection(db, a.connection)
    await audit.record(
        "connection.delete", actor=ctx.actor, connection_id=cid, target={"name": name}
    )


@router.post("/{cid}/test", response_model=ConnectionTestOut)
async def test_connection(
    cid: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> ConnectionTestOut:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_TEST)
    policy = await svc.get_network_policy(db, settings)
    adapter = get_adapter_manager().get(
        await svc.spec_for(db, ctx, a.connection), policy, Limits.from_settings(settings)
    )
    result = await run_blocking(adapter.test)
    await audit.record(
        "connection.test",
        actor=ctx.actor,
        connection_id=cid,
        target={"name": a.connection.name},
        result="success" if result.ok else "failure",
        detail={"error_code": result.error_code} if not result.ok else {},
    )
    return ConnectionTestOut(**result.__dict__)


@router.get("/{cid}/credentials/me", response_model=CredentialsOut)
async def my_credentials(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> CredentialsOut:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_VIEW)
    cred = await svc.load_user_credential(db, ctx.user.id, cid)
    return CredentialsOut(
        required=a.connection.credential_model == "per_user",
        configured=cred is not None,
        username=cred.username if cred else None,
    )


@router.put("/{cid}/credentials/me", response_model=CredentialsOut)
async def put_my_credentials(
    cid: uuid.UUID, body: CredentialsIn, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> CredentialsOut:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_VIEW)
    try:
        cred = await svc.set_user_credentials(
            db, settings, ctx, a.connection,
            username=body.username, password=body.password.get_secret_value(),
        )  # fmt: skip
    except AppError as exc:
        await audit.record(
            "connection.credentials.set", actor=ctx.actor, connection_id=cid,
            target={"name": a.connection.name}, result="failure", detail={"error": exc.code},
        )  # fmt: skip
        raise
    await audit.record(
        "connection.credentials.set",
        actor=ctx.actor,
        connection_id=cid,
        target={"name": a.connection.name, "db_username": cred.username},
    )
    return CredentialsOut(required=True, configured=True, username=cred.username)


@router.delete("/{cid}/credentials/me", status_code=204)
async def delete_my_credentials(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> None:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_VIEW)
    if await svc.delete_user_credentials(db, ctx.user.id, a.connection):
        await audit.record(
            "connection.credentials.delete",
            actor=ctx.actor,
            connection_id=cid,
            target={"name": a.connection.name},
        )


@router.get("/{cid}/grants", response_model=list[GrantOut])
async def get_grants(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> list[GrantOut]:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_MANAGE_ACCESS)
    return [
        GrantOut(user_id=u.id, username=u.username, role_id=r.id, role=r.name)
        for u, r in await svc.list_grants(db, a.connection)
    ]


@router.put("/{cid}/grants", response_model=list[GrantOut])
async def put_grants(
    cid: uuid.UUID, body: GrantsUpdate, db: DbSession, ctx: CurrentAuth
) -> list[GrantOut]:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_MANAGE_ACCESS)
    if any(g.user_id == ctx.user.id for g in body.grants) is False and not ctx.is_super_admin:
        raise Forbidden("You cannot remove your own access to a connection you manage.")
    await svc.replace_grants(db, ctx, a.connection, [(g.user_id, g.role_id) for g in body.grants])
    await db.flush()
    await audit.record(
        "connection.grants.update",
        actor=ctx.actor,
        connection_id=cid,
        target={"name": a.connection.name},
        detail={"grants": [{"user": str(g.user_id), "role": str(g.role_id)} for g in body.grants]},
    )
    return [
        GrantOut(user_id=u.id, username=u.username, role_id=r.id, role=r.name)
        for u, r in await svc.list_grants(db, a.connection)
    ]


def _scope_out(rows: list[tuple[Any, Any, Any]]) -> list[ScopeGrantOut]:
    return [
        ScopeGrantOut(
            user_id=u.id,
            username=u.username,
            role_id=r.id,
            role=r.name,
            database=g.database,
            schema_name=g.schema,
            object=g.object,
        )
        for u, r, g in rows
    ]


@router.get("/{cid}/scope-grants", response_model=list[ScopeGrantOut], response_model_by_alias=True)
async def get_scope_grants(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> list[ScopeGrantOut]:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_MANAGE_ACCESS)
    return _scope_out(await svc.list_scope_grants(db, a.connection))


@router.put("/{cid}/scope-grants", response_model=list[ScopeGrantOut], response_model_by_alias=True)
async def put_scope_grants(
    cid: uuid.UUID, body: ScopeGrantsUpdate, db: DbSession, ctx: CurrentAuth
) -> list[ScopeGrantOut]:
    a = await svc.require_access(db, ctx, cid, Perm.CONNECTION_MANAGE_ACCESS)
    await svc.replace_scope_grants(
        db, ctx, a,
        [svc.ScopeGrantSpec(g.user_id, g.role_id, g.database, g.schema_name, g.object)
         for g in body.grants],
    )  # fmt: skip
    await db.flush()
    await audit.record(
        "connection.scope_grants.update",
        actor=ctx.actor,
        connection_id=cid,
        target={"name": a.connection.name},
        detail={
            "grants": [
                {"user": str(g.user_id), "role": str(g.role_id), "database": g.database,
                 "schema": g.schema_name, "object": g.object}
                for g in body.grants
            ]
        },
    )  # fmt: skip
    return _scope_out(await svc.list_scope_grants(db, a.connection))
