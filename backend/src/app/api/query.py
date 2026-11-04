"""Query workspaces (Phase 4): SQL and MongoDB execution, cancellation, export, history and
saved queries."""

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

from app.api.schemas import Strict
from app.audit import service as audit
from app.auth.deps import AppSettings, AuthContext, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.database.core.serialization import approx_size, to_json_value
from app.database.document.filters import (
    parse_json,
    validate_filter,
    validate_projection,
    validate_sort,
)
from app.database.document.models import DocQuery
from app.database.document.pipeline import validate_pipeline
from app.editor.export import make_row_formatter, stream_export
from app.editor.guards import require_all
from app.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    OperationCancelled,
    StatementTimeout,
    ValidationFailed,
)
from app.models import Operation, QueryHistory, SavedQuery
from app.query import service as qs
from app.query.classify import classify
from app.services.runtime import run_blocking

router = APIRouter(prefix="/connections/{cid}", tags=["query"])
settings_router = APIRouter(tags=["administration"])

# ---------------------------------------------------------------------------- models


class ExecuteBody(Strict):
    sql: str = Field(min_length=1, max_length=qs.MAX_SQL_CHARS)
    database: str = Field(min_length=1, max_length=255)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    timeout_s: int | None = Field(default=None, ge=1, le=3600)
    max_rows: int | None = Field(default=None, ge=1, le=100_000)
    confirm_destructive: bool = False
    confirm: str | None = None
    rollback: bool = False
    operation_id: uuid.UUID | None = None


class ExportQueryBody(Strict):
    sql: str = Field(min_length=1, max_length=qs.MAX_SQL_CHARS)
    database: str = Field(min_length=1, max_length=255)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    format: Literal["csv", "json", "ndjson"] = "csv"


class ResultSetOut(BaseModel):
    columns: list[dict[str, str]]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class ExecuteOut(BaseModel):
    operation_id: uuid.UUID
    results: list[ResultSetOut]
    statements: list[str]
    elapsed_ms: int
    committed: bool


class MongoExecuteBody(Strict):
    database: str = Field(min_length=1, max_length=255)
    collection: str = Field(min_length=1, max_length=255)
    kind: Literal["find", "aggregate"]
    filter: Any | None = None
    projection: Any | None = None
    sort: Any | None = None
    pipeline: Any | None = None
    limit: int | None = Field(default=None, ge=1, le=100_000)
    timeout_s: int | None = Field(default=None, ge=1, le=3600)
    operation_id: uuid.UUID | None = None


class MongoExecuteOut(BaseModel):
    operation_id: uuid.UUID
    documents: list[dict[str, Any]]
    count: int
    truncated: bool
    elapsed_ms: int
    pagination: str | None = None
    next_cursor: str | None = None


class HistoryOut(BaseModel):
    id: int
    kind: str
    database: str | None
    schema_name: str | None = Field(alias="schema")
    statement: str
    redacted: bool
    status: str
    error_code: str | None
    duration_ms: int
    row_count: int | None
    created_at: str

    model_config = {"populate_by_name": True}


class SavedIn(Strict):
    kind: Literal["sql", "mongo"]
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    database: str | None = Field(default=None, max_length=255)
    schema_name: str | None = Field(default=None, alias="schema", max_length=255)
    statement: str = Field(min_length=1, max_length=qs.MAX_SQL_CHARS)
    visibility: Literal["private", "shared"] = "private"


class SavedPatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    database: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    statement: str | None = Field(default=None, min_length=1, max_length=qs.MAX_SQL_CHARS)
    visibility: Literal["private", "shared"] | None = None


class SavedOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    description: str
    database: str | None
    schema_name: str | None = Field(alias="schema")
    statement: str
    visibility: str
    owner_id: uuid.UUID
    mine: bool
    updated_at: str

    model_config = {"populate_by_name": True}


class HistorySettings(Strict):
    retention_days: int = Field(ge=1, le=3650)
    store_literals: bool


# ---------------------------------------------------------------------------- helpers


def _serialize(rows: list[tuple[Any, ...]], settings: AppSettings) -> tuple[list[list[Any]], bool]:
    out: list[list[Any]] = []
    size = 0
    for r in rows:
        cells = [to_json_value(v, settings.max_cell_chars) for v in r]
        size += approx_size(cells)
        if out and size > settings.max_response_bytes:
            return out, True
        out.append(cells)
    return out, False


def _max_rows(requested: int | None, settings: AppSettings) -> int:
    return max(1, min(requested or settings.max_page_size, settings.max_result_rows))


async def _open(
    db: DbSession, ctx: AuthContext, settings: AppSettings, cid: uuid.UUID, family: str
) -> tuple[conns.Access, Any]:
    return await conns.open_adapter(db, ctx, settings, cid, Perm.CONNECTION_VIEW, family)


# ---------------------------------------------------------------------------- SQL execute


@router.post("/queries/execute", response_model=ExecuteOut)
async def execute_sql(  # noqa: PLR0915
    cid: uuid.UUID, body: ExecuteBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> ExecuteOut:
    access, adapter = await _open(db, ctx, settings, cid, "relational")
    adapter = conns.as_relational(adapter)
    target = {"database": body.database, "schema": body.schema_name}
    try:
        plan = qs.plan_sql(
            access, body.sql, confirm_destructive=body.confirm_destructive,
            confirm=body.confirm, rollback=body.rollback,
        )  # fmt: skip
        await qs.check_sensitive_gate(db, access)
    except AppError as exc:
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="denied", detail={"reason": exc.code},
        )  # fmt: skip
        raise
    timeout = qs.clamp_timeout(body.timeout_s, settings)
    max_rows = _max_rows(body.max_rows, settings)
    op_id = await qs.begin_operation(ctx, cid, "sql", body.database, settings, body.operation_id)
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    kinds = [s.kind for s in plan.statements]

    async def finish(status: str, rows: int | None, error: str | None) -> int:
        ms = int((time.perf_counter() - started) * 1000)
        final = await qs.finish_operation(op_id, status)
        await qs.log_history(
            ctx=ctx, connection_id=cid, kind="sql", database=body.database,
            schema=body.schema_name, statement=body.sql, status=final,
            duration_ms=ms, row_count=rows, error_code=error,
        )  # fmt: skip
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="success" if final == "succeeded" else "failure",
            detail={
                "statements": len(plan.statements), "kinds": kinds, "write": plan.write,
                "destructive": plan.destructive, "rollback": body.rollback,
                "duration_ms": ms, "rows": rows, "status": final,
                "digest": hashlib.sha256(body.sql.encode()).hexdigest(),
                **({"error": error} if error else {}),
            },
        )  # fmt: skip
        return ms

    try:
        raw = await run_blocking(
            adapter.run_sql, body.database, body.schema_name, plan.texts,
            write=plan.write, script=plan.script, timeout_s=timeout, max_rows=max_rows,
            rollback=body.rollback, on_start=qs.make_ref_callback(op_id, loop),
        )  # fmt: skip
    except AppError as exc:
        cancelled = await qs.was_cancelled(op_id)
        status = (
            "cancelled"
            if cancelled
            else "timeout"
            if isinstance(exc, StatementTimeout)
            else "failed"
        )
        await finish(status, None, "cancelled" if cancelled else exc.code)
        if cancelled:
            raise OperationCancelled() from exc
        raise
    except BaseException:
        await finish("failed", None, "internal_error")
        raise

    results: list[ResultSetOut] = []
    total = 0
    for rs in raw:
        rows, clipped = _serialize(rs.rows, settings)
        results.append(
            ResultSetOut(
                columns=rs.columns,
                rows=rows,
                row_count=rs.row_count,
                truncated=rs.truncated or clipped,
            )
        )
        total += len(rows)
    ms = await finish("succeeded", total, None)
    return ExecuteOut(
        operation_id=op_id, results=results, statements=plan.texts, elapsed_ms=ms,
        committed=plan.write and not body.rollback,
    )  # fmt: skip


@router.post("/queries/{operation_id}/cancel", status_code=202)
async def cancel_query(
    cid: uuid.UUID, operation_id: uuid.UUID, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> dict[str, Any]:
    access, adapter = await _open(
        db, ctx, settings, cid, _family_of(await conns.load_connection(db, cid))
    )
    op = await db.get(Operation, operation_id)
    if op is None or op.connection_id != cid:
        raise NotFound("Operation not found.")
    mine = op.user_id == ctx.user.id
    if not mine and Perm.CONNECTION_MANAGE_ACCESS.value not in access.permissions:
        raise NotFound("Operation not found.")
    if op.status != "running":
        raise Conflict(f"The operation is already {op.status}.")
    ref = dict(op.backend_ref or {})
    if op.kind == "sql" and not ref:
        raise Conflict("The query is still starting; try again in a moment.")
    if not await qs.mark_cancelled(operation_id):
        raise Conflict("The operation just finished.")
    best_effort = False
    if op.kind == "sql":
        await run_blocking(adapter.cancel_backend, ref)
    else:
        best_effort = not await run_blocking(adapter.cancel_operation, str(operation_id))
    await audit.record(
        "query.cancel", actor=ctx.actor, connection_id=cid,
        target={"operation": str(operation_id), "owner_is_actor": mine},
        detail={"best_effort": best_effort},
    )  # fmt: skip
    return {"operation_id": str(operation_id), "status": "cancelling", "best_effort": best_effort}


def _family_of(conn: Any) -> str:
    return "document" if conn.db_type == "mongodb" else "relational"


# ---------------------------------------------------------------------------- SQL export


@router.post("/queries/export")
async def export_query(
    cid: uuid.UUID, body: ExportQueryBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> Any:
    access, adapter = await _open(db, ctx, settings, cid, "relational")
    adapter = conns.as_relational(adapter)
    require_all(access, [Perm.EXPORT_EXPORT, Perm.QUERY_EXECUTE_READ])
    stmts = classify(body.sql)
    if len(stmts) != 1 or stmts[0].kind != "read":
        raise ValidationFailed("Only a single read statement can be exported.")
    await qs.check_sensitive_gate(db, access)
    cols, batches = await run_blocking(
        adapter.stream_query, body.database, body.schema_name, stmts[0].text
    )
    cap = settings.export_max_rows
    # Shared with the generator below: stream_export reads it when the download finishes.
    state: dict[str, Any] = {
        "truncated": False,
        "kind": "query",
        "digest": hashlib.sha256(body.sql.encode()).hexdigest(),
    }

    def capped() -> Iterator[list[tuple[Any, ...]]]:
        sent = 0
        try:
            for chunk in batches:
                if sent + len(chunk) > cap:
                    chunk = chunk[: cap - sent]
                    state["truncated"] = True
                    yield chunk
                    return
                sent += len(chunk)
                yield chunk
        finally:
            batches.close()  # type: ignore[attr-defined]

    names = [c["name"] for c in cols]
    return stream_export(
        ctx=ctx,
        cid=cid,
        fmt=body.format,
        filename="query-result",
        formatter=make_row_formatter(body.format, names, frozenset()),
        batches=capped(),
        target={"database": body.database, "schema": body.schema_name},
        detail=state,
    )


# ---------------------------------------------------------------------------- MongoDB


@router.post("/mongo/queries/execute", response_model=MongoExecuteOut)
async def execute_mongo(  # noqa: PLR0915
    cid: uuid.UUID, body: MongoExecuteBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> MongoExecuteOut:
    access, adapter = await _open(db, ctx, settings, cid, "document")
    adapter = conns.as_document(adapter)
    require_all(access, [Perm.QUERY_EXECUTE_READ])
    await qs.check_sensitive_gate(db, access)
    max_docs = _max_rows(body.limit, settings)
    timeout = qs.clamp_timeout(body.timeout_s, settings)
    target = {"database": body.database, "collection": body.collection}

    def _text(v: Any) -> str | None:
        return None if v is None else (v if isinstance(v, str) else json.dumps(v))

    pipeline: list[dict[str, Any]] = []
    query: DocQuery | None = None
    stored: dict[str, Any] = {"kind": body.kind, "collection": body.collection}
    try:
        if body.kind == "find":
            query = DocQuery(
                filter=validate_filter(_text(body.filter)),
                projection=validate_projection(_text(body.projection)),
                sort=validate_sort(_text(body.sort)),
                limit=max_docs,
            )
            stored |= {
                k: v
                for k, v in (
                    ("filter", body.filter),
                    ("projection", body.projection),
                    ("sort", body.sort),
                )
                if v
            }
        else:
            if body.pipeline is None:
                raise ValidationFailed("pipeline is required for aggregate.")
            raw = (
                parse_json(body.pipeline, "Pipeline")
                if isinstance(body.pipeline, str)
                else body.pipeline
            )
            pipeline, targets = validate_pipeline(raw, max_docs)
            known = {c.name for c in await run_blocking(adapter.list_collections, body.database)}
            missing = sorted(t for t in targets if t not in known)
            if missing:
                raise ValidationFailed(f"Unknown collection in pipeline: {missing[0]!r}")
            stored["pipeline"] = raw
    except AppError as exc:
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="denied", detail={"reason": exc.code, "kind": body.kind},
        )  # fmt: skip
        raise

    statement = json.dumps(stored, sort_keys=True)
    op_id = await qs.begin_operation(ctx, cid, "mongo", body.database, settings, body.operation_id)
    started = time.perf_counter()

    async def finish(status: str, n: int | None, error: str | None) -> int:
        ms = int((time.perf_counter() - started) * 1000)
        final = await qs.finish_operation(op_id, status)
        await qs.log_history(
            ctx=ctx, connection_id=cid, kind="mongo", database=body.database, schema=None,
            statement=statement, status=final, duration_ms=ms, row_count=n, error_code=error,
        )  # fmt: skip
        await audit.record(
            "query.execute", actor=ctx.actor, connection_id=cid, target=target,
            result="success" if final == "succeeded" else "failure",
            detail={"kind": body.kind, "duration_ms": ms, "rows": n, "status": final,
                    "digest": hashlib.sha256(statement.encode()).hexdigest(),
                    **({"error": error} if error else {})},
        )  # fmt: skip
        return ms

    try:
        if query is not None:
            page = await run_blocking(
                adapter.run_find, body.database, body.collection, query,
                timeout_s=timeout, comment=str(op_id),
            )  # fmt: skip
            docs, truncated = page.documents, page.truncated or page.next_cursor is not None
            extra = {"pagination": page.pagination, "next_cursor": page.next_cursor}
        else:
            docs, truncated = await run_blocking(
                adapter.run_aggregate, body.database, body.collection, pipeline,
                max_docs=max_docs, timeout_s=timeout, comment=str(op_id),
            )  # fmt: skip
            extra = {}
    except AppError as exc:
        cancelled = await qs.was_cancelled(op_id)
        status = (
            "cancelled"
            if cancelled
            else "timeout"
            if isinstance(exc, StatementTimeout)
            else "failed"
        )
        await finish(status, None, "cancelled" if cancelled else exc.code)
        if cancelled:
            raise OperationCancelled() from exc
        raise
    except BaseException:
        await finish("failed", None, "internal_error")
        raise
    ms = await finish("succeeded", len(docs), None)
    return MongoExecuteOut(
        operation_id=op_id,
        documents=docs,
        count=len(docs),
        truncated=truncated,
        elapsed_ms=ms,
        **extra,
    )


# ---------------------------------------------------------------------------- history


def _hist(h: QueryHistory) -> HistoryOut:
    return HistoryOut(
        id=h.id, kind=h.kind, database=h.database, schema_name=h.schema, statement=h.statement,
        redacted=h.redacted, status=h.status, error_code=h.error_code, duration_ms=h.duration_ms,
        row_count=h.row_count, created_at=h.created_at.isoformat(),
    )  # fmt: skip


@router.get("/queries/history", response_model=list[HistoryOut])
async def list_history(
    cid: uuid.UUID, db: DbSession, ctx: CurrentAuth,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    kind: Annotated[Literal["sql", "mongo"] | None, Query()] = None,
) -> list[HistoryOut]:  # fmt: skip
    await conns.require_access(db, ctx, cid, Perm.QUERY_VIEW_HISTORY)
    stmt = (
        select(QueryHistory)
        .where(QueryHistory.connection_id == cid, QueryHistory.user_id == ctx.user.id)
        .order_by(QueryHistory.id.desc())
        .limit(limit)
    )
    if before_id:
        stmt = stmt.where(QueryHistory.id < before_id)
    if kind:
        stmt = stmt.where(QueryHistory.kind == kind)
    return [_hist(h) for h in (await db.execute(stmt)).scalars()]


@router.delete("/queries/history/{history_id}", status_code=204)
async def delete_history_item(
    cid: uuid.UUID, history_id: int, db: DbSession, ctx: CurrentAuth
) -> None:
    await conns.require_access(db, ctx, cid, Perm.QUERY_VIEW_HISTORY)
    res = await db.execute(
        delete(QueryHistory).where(
            QueryHistory.id == history_id, QueryHistory.user_id == ctx.user.id,
            QueryHistory.connection_id == cid,
        )
    )  # fmt: skip
    if not res.rowcount:  # type: ignore[attr-defined]
        raise NotFound("History entry not found.")


@router.delete("/queries/history", status_code=204)
async def clear_history(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> None:
    await conns.require_access(db, ctx, cid, Perm.QUERY_VIEW_HISTORY)
    await db.execute(
        delete(QueryHistory).where(
            QueryHistory.user_id == ctx.user.id, QueryHistory.connection_id == cid
        )
    )


# ---------------------------------------------------------------------------- saved queries


def _saved(q: SavedQuery, me: uuid.UUID) -> SavedOut:
    return SavedOut(
        id=q.id,
        kind=q.kind,
        name=q.name,
        description=q.description,
        database=q.database,
        schema_name=q.schema,
        statement=q.statement,
        visibility=q.visibility,
        owner_id=q.owner_id,
        mine=q.owner_id == me,
        updated_at=q.updated_at.isoformat(),
    )


@router.get("/queries/saved", response_model=list[SavedOut])
async def list_saved(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> list[SavedOut]:
    await conns.require_access(db, ctx, cid, Perm.QUERY_EXECUTE_READ)
    rows = (
        await db.execute(
            select(SavedQuery)
            .where(
                SavedQuery.connection_id == cid,
                or_(SavedQuery.owner_id == ctx.user.id, SavedQuery.visibility == "shared"),
            )
            .order_by(SavedQuery.name)
        )
    ).scalars()
    return [_saved(q, ctx.user.id) for q in rows]


@router.post("/queries/saved", status_code=201, response_model=SavedOut)
async def create_saved(cid: uuid.UUID, body: SavedIn, db: DbSession, ctx: CurrentAuth) -> SavedOut:
    await conns.require_access(db, ctx, cid, Perm.QUERY_MANAGE_SAVED)
    exists = (
        await db.execute(
            select(SavedQuery.id).where(
                SavedQuery.owner_id == ctx.user.id, SavedQuery.connection_id == cid,
                SavedQuery.name == body.name,
            )
        )
    ).first()  # fmt: skip
    if exists:
        raise Conflict("You already have a saved query with this name.")
    q = SavedQuery(
        owner_id=ctx.user.id, connection_id=cid, kind=body.kind, name=body.name,
        description=body.description, database=body.database, schema=body.schema_name,
        statement=body.statement, visibility=body.visibility,
    )  # fmt: skip
    db.add(q)
    await db.flush()
    await audit.record(
        "query.saved.create", actor=ctx.actor, connection_id=cid,
        target={"name": body.name}, detail={"visibility": body.visibility},
    )  # fmt: skip
    return _saved(q, ctx.user.id)


async def _own_saved(db: DbSession, ctx: AuthContext, cid: uuid.UUID, sid: uuid.UUID) -> SavedQuery:
    await conns.require_access(db, ctx, cid, Perm.QUERY_MANAGE_SAVED)
    q = await db.get(SavedQuery, sid)
    if (
        q is None
        or q.connection_id != cid
        or (q.owner_id != ctx.user.id and q.visibility != "shared")
    ):
        raise NotFound("Saved query not found.")
    if q.owner_id != ctx.user.id:
        raise Forbidden("Only the owner can change a shared query.")
    return q


@router.patch("/queries/saved/{saved_id}", response_model=SavedOut)
async def update_saved(
    cid: uuid.UUID, saved_id: uuid.UUID, body: SavedPatch, db: DbSession, ctx: CurrentAuth
) -> SavedOut:
    q = await _own_saved(db, ctx, cid, saved_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None or field in ("database", "schema_name"):
            setattr(q, "schema" if field == "schema_name" else field, value)
    await db.flush()
    return _saved(q, ctx.user.id)


@router.delete("/queries/saved/{saved_id}", status_code=204)
async def delete_saved(
    cid: uuid.UUID, saved_id: uuid.UUID, db: DbSession, ctx: CurrentAuth
) -> None:
    q = await _own_saved(db, ctx, cid, saved_id)
    await db.delete(q)
    await audit.record(
        "query.saved.delete", actor=ctx.actor, connection_id=cid, target={"name": q.name}
    )


# ---------------------------------------------------------------------------- settings


@settings_router.get("/settings/query-history", response_model=HistorySettings)
async def get_history_settings(db: DbSession, ctx: CurrentAuth) -> HistorySettings:
    ctx.require_app(Perm.APP_MANAGE_SETTINGS)
    return HistorySettings(**await qs.history_settings(db))


@settings_router.put("/settings/query-history", response_model=HistorySettings)
async def put_history_settings(
    body: HistorySettings, db: DbSession, ctx: CurrentAuth
) -> HistorySettings:
    ctx.require_app(Perm.APP_MANAGE_SETTINGS)
    await qs.set_history_settings(db, ctx.user.id, body.retention_days, body.store_literals)
    await audit.record(
        "settings.query_history.update", actor=ctx.actor,
        detail={"retention_days": body.retention_days, "store_literals": body.store_literals},
    )  # fmt: skip
    return body
