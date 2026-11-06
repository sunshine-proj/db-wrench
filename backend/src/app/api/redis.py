"""Redis / Valkey endpoints (key-value family, ADR-0019).

Logical databases are addressed by index; keys by an opaque, URL-safe id of the raw key bytes
(clients never build it). Every operation goes through the same access checks, production typed
confirmation and audit as the other families. The console only runs allowlisted commands.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import Field

from app.api.schemas import Strict
from app.audit import service as audit
from app.auth.deps import AppSettings, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.connections.scope import ObjectScope
from app.database.keyvalue import commands as cmd
from app.database.keyvalue.base import KeyValueAdapter
from app.editor.guards import require_all, require_confirmation
from app.errors import AppError, ConfirmationRequired, Forbidden
from app.explorer import service as ex
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections/{cid}/redis", tags=["redis"])

KeyType = Literal["string", "hash", "list", "set", "zset"]
Cursor = Annotated[str | None, Query(max_length=128)]
TTL_MAX_MS = 10 * 365 * 86_400_000


class KeyCreate(Strict):
    name: str = Field(min_length=1, max_length=4096)
    type: KeyType
    value: Any
    ttl_ms: int | None = Field(default=None, gt=0, le=TTL_MAX_MS)


class KeyReplace(Strict):
    type: KeyType
    value: Any
    version: str = Field(min_length=1, max_length=64)
    ttl_ms: int | None = Field(default=None, gt=0, le=TTL_MAX_MS)
    keep_ttl: bool = True


class KeyDelete(Strict):
    version: str | None = Field(default=None, max_length=64)
    confirm: str | None = Field(default=None, max_length=4096)


class TtlBody(Strict):
    ttl_ms: int | None = Field(default=None, gt=0, le=TTL_MAX_MS)  # null = persist


class CommandBody(Strict):
    database: str = Field(max_length=3)
    command: str = Field(min_length=1, max_length=cmd.MAX_LINE)
    confirm_destructive: bool = False
    confirm: str | None = Field(default=None, max_length=255)


async def _open(
    db: DbSession,
    ctx: Any,
    settings: Any,
    cid: uuid.UUID,
    perms: list[Perm],
    database: str | None = None,
) -> tuple[conns.Access, KeyValueAdapter]:
    scope = ObjectScope(database) if database else None
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perms[0], "keyvalue", scope)
    require_all(access, perms[1:])
    assert isinstance(adapter, KeyValueAdapter)
    return access, adapter


@router.get("/databases")
async def redis_databases(
    cid: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    access, adapter = await _open(db, ctx, settings, cid, [Perm.CONNECTION_VIEW])
    items = await run_blocking(adapter.list_databases)
    return ex.apply_listing(access, items, lambda d: d.name, "databases")


@router.get("/databases/{database}/keys")
async def redis_keys(  # noqa: PLR0913
    cid: uuid.UUID,
    database: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    pattern: Annotated[str | None, Query(max_length=256)] = None,
    type: Annotated[str | None, Query(max_length=16)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_READ], database)
    async with ex.audited(ctx, "data.read", cid, {"database": database}) as detail:
        page = await run_blocking(
            adapter.scan_keys,
            database,
            pattern=pattern or None,
            key_type=type or None,
            cursor=cursor,
            limit=limit,
        )
        detail.update(keys=len(page.keys), filtered=bool(pattern or type))
    return page


@router.get("/databases/{database}/keys/{key}")
async def redis_key(  # noqa: PLR0913
    cid: uuid.UUID,
    database: str,
    key: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    full: bool = False,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_READ], database)
    async with ex.audited(ctx, "data.read", cid, {"database": database, "key": "(by id)"}) as d:
        detail = await run_blocking(
            adapter.get_key, database, key, cursor=cursor, limit=limit, full=full
        )
        d.update(type=detail.type, truncated=detail.value.truncated)
    return detail


@router.post("/databases/{database}/keys", status_code=201)
async def redis_create_key(
    cid: uuid.UUID,
    database: str,
    body: KeyCreate,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_INSERT], database)
    async with ex.audited(ctx, "data.insert", cid, {"database": database, "type": body.type}) as d:
        out = await run_blocking(
            adapter.put_key,
            database,
            None,
            new_name=body.name,
            type=body.type,
            value=body.value,
            ttl_ms=body.ttl_ms,
            keep_ttl=False,
            version=None,
        )
        d.update(rows=1)
    return out


@router.put("/databases/{database}/keys/{key}")
async def redis_replace_key(  # noqa: PLR0913
    cid: uuid.UUID,
    database: str,
    key: str,
    body: KeyReplace,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_UPDATE], database)
    async with ex.audited(ctx, "data.update", cid, {"database": database, "type": body.type}) as d:
        out = await run_blocking(
            adapter.put_key,
            database,
            key,
            new_name=None,
            type=body.type,
            value=body.value,
            ttl_ms=body.ttl_ms,
            keep_ttl=body.keep_ttl,
            version=body.version,
        )
        d.update(rows=1)
    return out


@router.put("/databases/{database}/keys/{key}/ttl", status_code=204)
async def redis_set_ttl(  # noqa: PLR0913
    cid: uuid.UUID,
    database: str,
    key: str,
    body: TtlBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> None:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_UPDATE], database)
    async with ex.audited(ctx, "data.update", cid, {"database": database, "ttl": True}):
        await run_blocking(adapter.set_ttl, database, key, body.ttl_ms)


@router.delete("/databases/{database}/keys/{key}", status_code=204)
async def redis_delete_key(  # noqa: PLR0913
    cid: uuid.UUID,
    database: str,
    key: str,
    body: KeyDelete,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> None:
    access, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_DELETE], database)
    # Production needs the key's name typed back; read it first (also proves the key exists).
    current = await run_blocking(adapter.get_key, database, key, cursor=None, limit=1)
    require_confirmation(access, body.confirm, current.name)
    async with ex.audited(
        ctx, "data.delete", cid, {"database": database, "type": current.type}
    ) as d:
        await run_blocking(adapter.delete_key, database, key, body.version)
        d.update(rows=1)


@router.post("/commands/execute")
async def redis_execute(
    cid: uuid.UUID, body: CommandBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    target: dict[str, Any] = {"database": body.database}
    try:
        args = cmd.tokenize(body.command)
        name, kind = cmd.classify(args)
        write = kind == "write"
        perm = Perm.QUERY_EXECUTE_WRITE if write else Perm.QUERY_EXECUTE_READ
        access, adapter = await _open(db, ctx, settings, cid, [Perm.CONNECTION_VIEW])
        if write and access.connection.read_only:
            raise Forbidden("This connection is read-only; only read commands can be run on it.")
        require_all(access, [perm])
        if write:
            if name in cmd.DESTRUCTIVE and not body.confirm_destructive:
                raise ConfirmationRequired(
                    f"{name} overwrites or removes data. Confirm explicitly to run it."
                )
            require_confirmation(access, body.confirm, access.connection.name)
    except AppError as exc:
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="denied", detail={"reason": exc.code},
        )  # fmt: skip
        raise
    target["command"] = name
    try:
        result = await run_blocking(adapter.run_command, body.database, args, write=write)
    except AppError as exc:
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="failure", detail={"error": exc.code, "args": len(args) - 1},
        )  # fmt: skip
        raise
    await audit.record(
        "query.execute", actor=ctx.actor, connection_id=cid, target=target,
        detail={"write": write, "args": len(args) - 1, "ms": result.elapsed_ms},
    )  # fmt: skip
    return result
