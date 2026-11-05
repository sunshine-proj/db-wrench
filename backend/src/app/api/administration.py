"""Database, schema, collection and index administration (Phase 5).

Every operation: permission -> typed confirmation -> approval policy -> dedicated writable
connection -> audit. Names of existing objects are validated against fetched metadata inside
the adapter; names of new objects against a conservative pattern.
"""

# NOTE: no `from __future__ import annotations`: the index route factory uses factory-local
# annotations that FastAPI must see evaluated (same reason as api/editor.py).
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictInt, StrictStr

from app.api.schemas import ApprovalId, Strict
from app.auth.deps import AppSettings, AuthContext, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.connections.scope import ObjectScope
from app.database.document.filters import validate_filter
from app.editor.guards import require_confirmation, require_typed_name
from app.errors import ValidationFailed
from app.explorer import service as ex
from app.governance import service as gov
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections/{cid}", tags=["administration"])
mongo = APIRouter(prefix="/connections/{cid}/mongo", tags=["administration"])


# ---------------------------------------------------------------------------- schemas


class CreateDatabaseBody(Strict):
    name: str = Field(max_length=64)
    options: dict[str, Any] = Field(default_factory=dict)


class CreateNamedBody(Strict):
    name: str = Field(max_length=64)


class DropBody(Strict):
    confirm: str | None = Field(default=None, max_length=255)


class IndexBody(Strict):
    name: str = Field(max_length=63)
    columns: list[str] = Field(min_length=1, max_length=16)
    unique: bool = False


class MongoDatabaseBody(Strict):
    name: str = Field(max_length=64)
    collection: str = Field(max_length=64, description="First collection (creates the database)")


class MongoCollectionBody(Strict):
    name: str = Field(max_length=64)
    capped: bool = False
    size_bytes: int | None = Field(default=None, ge=1)
    max_docs: int | None = Field(default=None, ge=1)


class MongoIndexKey(Strict):
    field: str = Field(max_length=500)
    direction: StrictInt | StrictStr = 1


class MongoIndexBody(Strict):
    keys: list[MongoIndexKey] = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=63)
    unique: bool = False
    sparse: bool = False
    ttl_seconds: int | None = Field(default=None, ge=0)
    partial_filter: dict[str, Any] | None = None


class Created(BaseModel):
    name: str


# ---------------------------------------------------------------------------- shared flow


async def _execute(  # noqa: PLR0913
    ctx: AuthContext,
    access: conns.Access,
    db: DbSession,
    *,
    action: str,
    approval_op: str,
    target: dict[str, Any],
    params: dict[str, Any],
    approval_id: uuid.UUID | None,
    work: Callable[[], Awaitable[Any]],
) -> Any:
    """Approval gate, then the work under audit. ``action`` is the audit action."""
    # The digest binds the target too: an approval for one object never covers another.
    bound = {**target, **params}
    await gov.gate(db, ctx, access, approval_op, bound, approval_id, summary=bound)
    async with ex.audited(ctx, action, access.connection.id, target) as detail:
        detail["approval_id"] = str(approval_id) if approval_id else None
        return await work()


# ---------------------------------------------------------------------------- relational


@router.post("/databases", status_code=201, response_model=Created)
async def create_database(
    cid: uuid.UUID,
    body: CreateDatabaseBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> Created:
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.ADMIN_MANAGE_DATABASES, "relational"
    )
    rel = conns.as_relational(adapter)
    await _execute(
        ctx, access, db, action="admin.database.create", approval_op="admin.create_database",
        target={"database": body.name}, params={"options": body.options},
        approval_id=approval_id,
        work=lambda: run_blocking(rel.create_database, body.name, body.options),
    )  # fmt: skip
    return Created(name=body.name)


@router.delete("/databases/{database}", status_code=204)
async def drop_database(
    cid: uuid.UUID,
    database: str,
    body: DropBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> None:
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.ADMIN_MANAGE_DATABASES, "relational"
    )
    require_typed_name(body.confirm, database)
    rel = conns.as_relational(adapter)
    await _execute(
        ctx, access, db, action="admin.database.drop", approval_op="admin.drop_database",
        target={"database": database}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(rel.drop_database, database),
    )  # fmt: skip


@router.post("/databases/{database}/schemas", status_code=201, response_model=Created)
async def create_schema(
    cid: uuid.UUID,
    database: str,
    body: CreateNamedBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> Created:
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.ADMIN_MANAGE_SCHEMAS, "relational", ObjectScope(database)
    )
    rel = conns.as_relational(adapter)
    await _execute(
        ctx, access, db, action="admin.schema.create", approval_op="admin.create_schema",
        target={"database": database, "schema": body.name}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(rel.create_schema, database, body.name),
    )  # fmt: skip
    return Created(name=body.name)


@router.delete("/databases/{database}/schemas/{schema}", status_code=204)
async def drop_schema(
    cid: uuid.UUID,
    database: str,
    schema: str,
    body: DropBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> None:
    access, adapter = await conns.open_adapter(
        db,
        ctx,
        settings,
        cid,
        Perm.ADMIN_MANAGE_SCHEMAS,
        "relational",
        ObjectScope(database, schema),
    )
    require_typed_name(body.confirm, schema)
    rel = conns.as_relational(adapter)
    await _execute(
        ctx, access, db, action="admin.schema.drop", approval_op="admin.drop_schema",
        target={"database": database, "schema": schema}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(rel.drop_schema, database, schema),
    )  # fmt: skip


def _idx_schema(database: str, schema: str, table: str) -> tuple[str, str | None, str]:
    return database, schema, table


def _idx_flat(database: str, table: str) -> tuple[str, str | None, str]:
    return database, None, table


def build_index_router(dep: Callable[..., tuple[str, str | None, str]]) -> APIRouter:
    r = APIRouter()
    T = Annotated[tuple[str, str | None, str], Depends(dep)]

    @r.post("/indexes", status_code=201, response_model=Created)
    async def create_index(
        cid: uuid.UUID,
        t: T,
        body: IndexBody,
        db: DbSession,
        settings: AppSettings,
        ctx: CurrentAuth,
        approval_id: ApprovalId = None,
    ) -> Created:
        database, schema, table = t
        access, adapter = await conns.open_adapter(
            db,
            ctx,
            settings,
            cid,
            Perm.ADMIN_MANAGE_INDEXES,
            "relational",
            ObjectScope(database, schema, table),
        )
        rel = conns.as_relational(adapter)
        target = {"database": database, "schema": schema, "table": table, "index": body.name}
        await _execute(
            ctx, access, db, action="admin.index.create", approval_op="admin.create_index",
            target=target, params={"columns": body.columns, "unique": body.unique},
            approval_id=approval_id,
            work=lambda: run_blocking(
                rel.create_index, database, schema, table, body.name, body.columns,
                unique=body.unique,
            ),
        )  # fmt: skip
        return Created(name=body.name)

    @r.delete("/indexes/{index}", status_code=204)
    async def drop_index(
        cid: uuid.UUID,
        t: T,
        index: str,
        db: DbSession,
        settings: AppSettings,
        ctx: CurrentAuth,
        body: DropBody | None = None,
        approval_id: ApprovalId = None,
    ) -> None:
        database, schema, table = t
        access, adapter = await conns.open_adapter(
            db,
            ctx,
            settings,
            cid,
            Perm.ADMIN_MANAGE_INDEXES,
            "relational",
            ObjectScope(database, schema, table),
        )
        require_confirmation(access, body.confirm if body else None, index)
        rel = conns.as_relational(adapter)
        target = {"database": database, "schema": schema, "table": table, "index": index}
        await _execute(
            ctx, access, db, action="admin.index.drop", approval_op="admin.drop_index",
            target=target, params={}, approval_id=approval_id,
            work=lambda: run_blocking(rel.drop_index, database, schema, table, index),
        )  # fmt: skip

    return r


_tables = "/databases/{database}"
router.include_router(
    build_index_router(_idx_schema), prefix=_tables + "/schemas/{schema}/tables/{table}"
)
router.include_router(build_index_router(_idx_flat), prefix=_tables + "/tables/{table}")


# ---------------------------------------------------------------------------- MongoDB


@mongo.post("/databases", status_code=201, response_model=Created)
async def mongo_create_database(
    cid: uuid.UUID,
    body: MongoDatabaseBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> Created:
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.ADMIN_MANAGE_DATABASES, "document"
    )
    doc = conns.as_document(adapter)
    await _execute(
        ctx, access, db, action="admin.database.create", approval_op="admin.create_database",
        target={"database": body.name}, params={"collection": body.collection},
        approval_id=approval_id,
        work=lambda: run_blocking(doc.create_database, body.name, body.collection),
    )  # fmt: skip
    return Created(name=body.name)


@mongo.delete("/databases/{database}", status_code=204)
async def mongo_drop_database(
    cid: uuid.UUID,
    database: str,
    body: DropBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> None:
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.ADMIN_MANAGE_DATABASES, "document"
    )
    require_typed_name(body.confirm, database)
    doc = conns.as_document(adapter)
    await _execute(
        ctx, access, db, action="admin.database.drop", approval_op="admin.drop_database",
        target={"database": database}, params={}, approval_id=approval_id,
        work=lambda: run_blocking(doc.drop_database, database),
    )  # fmt: skip


@mongo.post("/databases/{database}/collections", status_code=201, response_model=Created)
async def mongo_create_collection(
    cid: uuid.UUID,
    database: str,
    body: MongoCollectionBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> Created:
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.ADMIN_MANAGE_SCHEMAS, "document", ObjectScope(database)
    )
    doc = conns.as_document(adapter)
    options: dict[str, Any] = {}
    if body.capped:
        options = {"capped": True, "size_bytes": body.size_bytes, "max_docs": body.max_docs}
    elif body.size_bytes is not None or body.max_docs is not None:
        raise ValidationFailed("size_bytes and max_docs apply to capped collections only.")
    await _execute(
        ctx, access, db, action="admin.collection.create", approval_op="admin.create_collection",
        target={"database": database, "collection": body.name}, params={"options": options},
        approval_id=approval_id,
        work=lambda: run_blocking(doc.create_collection, database, body.name, options),
    )  # fmt: skip
    return Created(name=body.name)


@mongo.delete("/databases/{database}/collections/{collection}", status_code=204)
async def mongo_drop_collection(
    cid: uuid.UUID,
    database: str,
    collection: str,
    body: DropBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> None:
    access, adapter = await conns.open_adapter(
        db,
        ctx,
        settings,
        cid,
        Perm.ADMIN_MANAGE_SCHEMAS,
        "document",
        ObjectScope(database, None, collection),
    )
    require_typed_name(body.confirm, collection)
    doc = conns.as_document(adapter)
    await _execute(
        ctx, access, db, action="admin.collection.drop", approval_op="admin.drop_collection",
        target={"database": database, "collection": collection}, params={},
        approval_id=approval_id,
        work=lambda: run_blocking(doc.drop_collection, database, collection),
    )  # fmt: skip


@mongo.post(
    "/databases/{database}/collections/{collection}/indexes",
    status_code=201,
    response_model=Created,
)
async def mongo_create_index(
    cid: uuid.UUID,
    database: str,
    collection: str,
    body: MongoIndexBody,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    approval_id: ApprovalId = None,
) -> Created:
    access, adapter = await conns.open_adapter(
        db,
        ctx,
        settings,
        cid,
        Perm.ADMIN_MANAGE_INDEXES,
        "document",
        ObjectScope(database, None, collection),
    )
    doc = conns.as_document(adapter)
    keys = [(k.field, k.direction) for k in body.keys]
    partial = validate_filter(json.dumps(body.partial_filter)) if body.partial_filter else None
    params = {
        "keys": [[f, d] for f, d in keys], "name": body.name, "unique": body.unique,
        "sparse": body.sparse, "ttl_seconds": body.ttl_seconds, "partial": body.partial_filter,
    }  # fmt: skip
    created = await _execute(
        ctx, access, db, action="admin.index.create", approval_op="admin.create_index",
        target={"database": database, "collection": collection}, params=params,
        approval_id=approval_id,
        work=lambda: run_blocking(
            lambda: doc.create_index(
                database, collection, keys, name=body.name, unique=body.unique,
                sparse=body.sparse, ttl_seconds=body.ttl_seconds, partial_filter=partial,
            )
        ),
    )  # fmt: skip
    return Created(name=created)


@mongo.delete("/databases/{database}/collections/{collection}/indexes/{index}", status_code=204)
async def mongo_drop_index(
    cid: uuid.UUID,
    database: str,
    collection: str,
    index: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    body: DropBody | None = None,
    approval_id: ApprovalId = None,
) -> None:
    access, adapter = await conns.open_adapter(
        db,
        ctx,
        settings,
        cid,
        Perm.ADMIN_MANAGE_INDEXES,
        "document",
        ObjectScope(database, None, collection),
    )
    require_confirmation(access, body.confirm if body else None, index)
    doc = conns.as_document(adapter)
    await _execute(
        ctx, access, db, action="admin.index.drop", approval_op="admin.drop_index",
        target={"database": database, "collection": collection, "index": index}, params={},
        approval_id=approval_id,
        work=lambda: run_blocking(doc.drop_index, database, collection, index),
    )  # fmt: skip
