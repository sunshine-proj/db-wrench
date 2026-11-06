"""Elasticsearch / OpenSearch endpoints (search family, ADR-0022).

Indices are addressed by name (resolved against the cluster's own listing, never trusted),
documents by an opaque id. Writes use the engine's native optimistic concurrency
(``_seq_no`` / ``_primary_term``). The console only runs validated ``_search`` bodies.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import Field

from app.api.schemas import Strict
from app.audit import service as audit
from app.auth.deps import AppSettings, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.connections.scope import ObjectScope
from app.database.search.base import SearchAdapter
from app.editor.guards import require_all, require_confirmation, require_typed_name
from app.errors import AppError
from app.explorer import service as ex
from app.query import service as qs
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections/{cid}/search", tags=["search"])


class IndexCreate(Strict):
    name: str = Field(min_length=1, max_length=255)
    shards: int = Field(default=1, ge=1, le=20)
    replicas: int = Field(default=1, ge=0, le=5)
    mappings: dict[str, Any] | None = None


class IndexDelete(Strict):
    confirm: str | None = Field(default=None, max_length=255)


class DocCreate(Strict):
    id: str | None = Field(default=None, max_length=512)
    source: dict[str, Any]


class DocReplace(Strict):
    source: dict[str, Any]
    seq_no: int = Field(ge=0)
    primary_term: int = Field(ge=1)


class DocDelete(Strict):
    seq_no: int | None = Field(default=None, ge=0)
    primary_term: int | None = Field(default=None, ge=1)
    confirm: str | None = Field(default=None, max_length=255)


class SearchBody(Strict):
    index: str = Field(min_length=1, max_length=255)
    body: dict[str, Any]
    timeout_s: int | None = Field(default=None, ge=1, le=300)


async def _open(
    db: DbSession,
    ctx: Any,
    settings: Any,
    cid: uuid.UUID,
    perms: list[Perm],
    index: str | None = None,
) -> tuple[conns.Access, SearchAdapter]:
    scope = ObjectScope(index, None, None) if index else None
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perms[0], "search", scope)
    require_all(access, perms[1:])
    assert isinstance(adapter, SearchAdapter)
    return access, adapter


@router.get("/indices")
async def list_indices(
    cid: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    access, adapter = await _open(db, ctx, settings, cid, [Perm.CONNECTION_VIEW])
    items = await run_blocking(adapter.list_indices)
    return ex.apply_listing(access, items, lambda i: i.name, "databases")


@router.get("/indices/{index}")
async def get_index(
    cid: uuid.UUID, index: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.METADATA_VIEW], index)
    return await run_blocking(adapter.get_index, index)


@router.post("/indices", status_code=201)
async def create_index(
    cid: uuid.UUID, body: IndexCreate, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.ADMIN_MANAGE_DATABASES])
    async with ex.audited(ctx, "admin.index.create", cid, {"index": body.name}):
        await run_blocking(
            adapter.create_index,
            body.name,
            shards=body.shards,
            replicas=body.replicas,
            mappings=body.mappings,
        )
    return {"name": body.name}


@router.delete("/indices/{index}", status_code=204)
async def delete_index(
    cid: uuid.UUID,
    index: str,
    body: IndexDelete,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> None:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.ADMIN_MANAGE_DATABASES])
    require_typed_name(body.confirm, index)  # dropping an index always needs its name typed
    async with ex.audited(ctx, "admin.index.drop", cid, {"index": index}):
        await run_blocking(adapter.delete_index, index)


@router.get("/indices/{index}/documents")
async def search_documents(  # noqa: PLR0913
    cid: uuid.UUID,
    index: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
    q: Annotated[str | None, Query(max_length=1000)] = None,
    sort: Annotated[str | None, Query(max_length=300, description="field:asc|desc,...")] = None,
    cursor: Annotated[str | None, Query(max_length=12)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_READ], index)
    order: list[tuple[str, str]] = []
    for part in (sort or "").split(","):
        if part.strip():
            field, _, direction = part.strip().rpartition(":")
            order.append((field or part.strip(), direction if field else "asc"))
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    async with ex.audited(ctx, "data.read", cid, {"index": index}) as detail:
        page = await run_blocking(
            adapter.search_documents, index, text=q or None, sort=order, offset=offset, size=limit
        )
        detail.update(documents=len(page.hits), searched=bool(q), sorted=bool(order))
    return page


@router.get("/indices/{index}/documents/{key}")
async def get_document(
    cid: uuid.UUID, index: str, key: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_READ], index)
    async with ex.audited(ctx, "data.read", cid, {"index": index, "document": "(by id)"}):
        return await run_blocking(adapter.get_document, index, key)


@router.post("/indices/{index}/documents", status_code=201)
async def create_document(  # noqa: PLR0913
    cid: uuid.UUID,
    index: str,
    body: DocCreate,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_INSERT], index)
    async with ex.audited(ctx, "data.insert", cid, {"index": index}) as d:
        out = await run_blocking(
            adapter.put_document,
            index,
            None,
            new_id=body.id,
            source=body.source,
            seq_no=None,
            primary_term=None,
        )
        d.update(rows=1)
    return out


@router.put("/indices/{index}/documents/{key}")
async def replace_document(  # noqa: PLR0913
    cid: uuid.UUID,
    index: str,
    key: str,
    body: DocReplace,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    _, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_UPDATE], index)
    async with ex.audited(ctx, "data.update", cid, {"index": index}) as d:
        out = await run_blocking(
            adapter.put_document,
            index,
            key,
            new_id=None,
            source=body.source,
            seq_no=body.seq_no,
            primary_term=body.primary_term,
        )
        d.update(rows=1)
    return out


@router.delete("/indices/{index}/documents/{key}", status_code=204)
async def delete_document(  # noqa: PLR0913
    cid: uuid.UUID,
    index: str,
    key: str,
    body: DocDelete,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> None:
    access, adapter = await _open(db, ctx, settings, cid, [Perm.DATA_DELETE], index)
    require_confirmation(access, body.confirm, index)
    async with ex.audited(ctx, "data.delete", cid, {"index": index}) as d:
        await run_blocking(adapter.delete_document, index, key, body.seq_no, body.primary_term)
        d.update(rows=1)


@router.post("/execute")
async def execute(
    cid: uuid.UUID, body: SearchBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    target = {"index": body.index}
    try:
        _, adapter = await _open(db, ctx, settings, cid, [Perm.QUERY_EXECUTE_READ], body.index)
    except AppError as exc:
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="denied", detail={"reason": exc.code},
        )  # fmt: skip
        raise
    timeout = qs.clamp_timeout(body.timeout_s, settings)
    try:
        result = await run_blocking(adapter.run_search, body.index, body.body, timeout_s=timeout)
    except AppError as exc:
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="failure", detail={"error": exc.code, "keys": sorted(body.body)[:20]},
        )  # fmt: skip
        raise
    await audit.record(
        "query.execute", actor=ctx.actor, connection_id=cid, target=target,
        detail={"keys": sorted(body.body)[:20], "took_ms": result.took_ms},
    )  # fmt: skip
    return result
