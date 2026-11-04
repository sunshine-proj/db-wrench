"""Database explorer and data-browser endpoints (relational + MongoDB).

Objects are addressed by their full path below a connection. Every path segment is
resolved against metadata fetched from the managed database; unknown names are rejected
and are never interpolated into SQL.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.auth.deps import AppSettings, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.database.document.filters import (
    filter_fields,
    mask_document,
    path_is_masked,
    validate_filter,
    validate_projection,
    validate_sort,
)
from app.database.document.models import DocQuery
from app.database.relational.models import (
    DatabaseInfo,
    FunctionInfo,
    RowPage,
    SchemaInfo,
    SequenceInfo,
    TableInfo,
    TableStructure,
)
from app.editor.guards import masked_fields, reject_masked
from app.explorer import service as ex
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections/{cid}", tags=["explorer"])

Limit = Annotated[int, Query(ge=1, le=100_000, description="Page size (clamped to server limits)")]
Cursor = Annotated[str | None, Query(max_length=4096)]
JsonParam = Annotated[str | None, Query(max_length=65536)]


# ------------------------------------------------------------------------ relational: common


@router.get("/databases", response_model=list[DatabaseInfo])
async def list_databases(
    cid: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_databases)


async def _rows(
    cid: uuid.UUID, database: str, schema: str | None, table: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
    limit: int, cursor: str | None, sort: str | None, filters: str | None,
    columns: str | None, search: str | None, search_columns: str | None,
) -> RowPage:  # fmt: skip
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, Perm.DATA_READ, "relational")
    adapter = conns.as_relational(adapter)
    q = ex.parse_row_query(
        limit=limit or settings.default_page_size, cursor=cursor, sort=sort, filters=filters,
        columns=columns, search=search, search_columns=search_columns,
    )  # fmt: skip
    masked = await masked_fields(db, access, database, schema, table)
    reject_masked(
        [f.column for f in q.filters] + [s.column for s in q.sort] + q.search_columns,
        masked,
        "filter or sort by",
    )
    target = {"database": database, "schema": schema, "table": table}
    async with ex.audited(ctx, "data.read", cid, target) as detail:
        page = await run_blocking(adapter.fetch_rows, database, schema, table, q, masked)
        detail.update(
            rows=len(page.rows),
            filters=ex.filter_summary(q),
            sorted=bool(q.sort),
            searched=bool(q.search),
            pagination=page.pagination,
            masked=sorted(masked & {c["name"] for c in page.columns}),
        )
    return page


# ------------------------------------------------- relational: schema-aware (PostgreSQL)


@router.get("/databases/{database}/schemas", response_model=list[SchemaInfo])
async def list_schemas(
    cid: uuid.UUID, database: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_schemas, database)


@router.get("/databases/{database}/schemas/{schema}/tables", response_model=list[TableInfo])
async def list_tables_in_schema(
    cid: uuid.UUID,
    database: str,
    schema: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_tables, database, schema)


@router.get(
    "/databases/{database}/schemas/{schema}/tables/{table}/structure",
    response_model=TableStructure,
)
async def table_structure_in_schema(
    cid: uuid.UUID, database: str, schema: str, table: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> Any:  # fmt: skip
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.get_structure, database, schema, table)


@router.get("/databases/{database}/schemas/{schema}/tables/{table}/rows", response_model=RowPage)
async def table_rows_in_schema(
    cid: uuid.UUID, database: str, schema: str, table: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
    limit: Limit = 100, cursor: Cursor = None, sort: JsonParam = None, filters: JsonParam = None,
    columns: JsonParam = None, search: Annotated[str | None, Query(max_length=1000)] = None,
    search_columns: JsonParam = None,
) -> RowPage:  # fmt: skip
    return await _rows(
        cid, database, schema, table, db, settings, ctx,
        limit, cursor, sort, filters, columns, search, search_columns,
    )  # fmt: skip


@router.get("/databases/{database}/schemas/{schema}/sequences", response_model=list[SequenceInfo])
async def list_sequences(
    cid: uuid.UUID,
    database: str,
    schema: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_sequences, database, schema)


@router.get("/databases/{database}/schemas/{schema}/functions", response_model=list[FunctionInfo])
async def list_functions(
    cid: uuid.UUID,
    database: str,
    schema: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_functions, database, schema)


@router.get(
    "/databases/{database}/schemas/{schema}/functions/{function_id}", response_model=FunctionInfo
)
async def get_function(
    cid: uuid.UUID, database: str, schema: str, function_id: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> Any:  # fmt: skip
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.get_function, database, schema, function_id)


# ------------------------------------------------------------------- relational: no schema (MySQL)


@router.get("/databases/{database}/tables", response_model=list[TableInfo])
async def list_tables(
    cid: uuid.UUID, database: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_tables, database, None)


@router.get("/databases/{database}/tables/{table}/structure", response_model=TableStructure)
async def table_structure(
    cid: uuid.UUID,
    database: str,
    table: str,
    db: DbSession,
    settings: AppSettings,
    ctx: CurrentAuth,
) -> Any:
    adapter = await ex.relational(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.get_structure, database, None, table)


@router.get("/databases/{database}/tables/{table}/rows", response_model=RowPage)
async def table_rows(
    cid: uuid.UUID, database: str, table: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
    limit: Limit = 100, cursor: Cursor = None, sort: JsonParam = None, filters: JsonParam = None,
    columns: JsonParam = None, search: Annotated[str | None, Query(max_length=1000)] = None,
    search_columns: JsonParam = None,
) -> RowPage:  # fmt: skip
    return await _rows(
        cid, database, None, table, db, settings, ctx,
        limit, cursor, sort, filters, columns, search, search_columns,
    )  # fmt: skip


# ----------------------------------------------------------------------------------- MongoDB


@router.get("/mongo/databases")
async def mongo_databases(
    cid: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    adapter = await ex.document(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_databases)


@router.get("/mongo/databases/{database}/collections")
async def mongo_collections(
    cid: uuid.UUID, database: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    adapter = await ex.document(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.list_collections, database)


@router.get("/mongo/databases/{database}/collections/{collection}")
async def mongo_collection(
    cid: uuid.UUID, database: str, collection: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> Any:  # fmt: skip
    adapter = await ex.document(db, ctx, settings, cid, Perm.METADATA_VIEW)
    return await run_blocking(adapter.get_collection, database, collection)


@router.get("/mongo/databases/{database}/collections/{collection}/documents")
async def mongo_documents(
    cid: uuid.UUID, database: str, collection: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
    limit: Limit = 50, cursor: Cursor = None,
    filter: JsonParam = None, sort: JsonParam = None, projection: JsonParam = None,
) -> Any:  # fmt: skip
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, Perm.DATA_READ, "document")
    adapter = conns.as_document(adapter)
    q = DocQuery(
        filter=validate_filter(filter),
        projection=validate_projection(projection),
        sort=validate_sort(sort),
        limit=limit,
        cursor=cursor,
    )
    masked = await masked_fields(db, access, database, None, collection)
    if masked:
        touched = filter_fields(q.filter) | {f for f, _ in q.sort}
        hit = sorted(p for p in touched if path_is_masked(p, masked))
        if hit:
            reject_masked(hit, frozenset(hit), "filter or sort by")
    target = {"database": database, "collection": collection}
    async with ex.audited(ctx, "data.read", cid, target) as detail:
        page = await run_blocking(adapter.find_documents, database, collection, q)
        page.documents = [mask_document(d, masked) for d in page.documents]
        detail.update(
            documents=len(page.documents),
            filtered=bool(filter and filter.strip() not in ("", "{}")),
            sorted=bool(q.sort),
            pagination=page.pagination,
            masked=sorted(masked),
        )
    return page
