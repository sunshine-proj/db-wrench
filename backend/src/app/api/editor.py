"""Data editor, bulk operations, cell fetch, export and sensitive-field markers (Phase 3).

The relational routes are built once and mounted under both path shapes (with and without a
schema segment), so each variant has exact path parameters in the OpenAPI contract.
"""

# NOTE: no `from __future__ import annotations` here on purpose: the route factory below uses
# annotations that reference factory-local names, which FastAPI must see evaluated.
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.api.schemas import ApprovalId, Strict
from app.audit import service as audit
from app.auth.deps import AppSettings, AuthContext, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.connections.scope import ObjectScope
from app.database.core.serialization import to_json_value
from app.database.document.filters import (
    check_field_name,
    filter_fields,
    mask_document,
    path_is_masked,
    validate_document,
    validate_filter,
    validate_projection,
    validate_set,
    validate_sort,
)
from app.database.relational.editing import Selector
from app.database.relational.models import FilterSpec, TableStructure
from app.editor import tokens
from app.editor.export import JsonDocs, make_row_formatter, stream_export
from app.editor.guards import (
    masked_fields,
    reject_masked,
    require_all,
    require_confirmation,
)
from app.errors import ConfirmationRequired, Forbidden, ValidationFailed
from app.explorer import service as ex
from app.governance import service as gov
from app.models import SensitiveMarker
from app.services.runtime import run_blocking

# ---------------------------------------------------------------------------- schemas


class FilterIn(Strict):
    column: str = Field(max_length=255)
    op: str = Field(max_length=20)
    value: Any = None


class SelectorBody(Strict):
    row_keys: list[str] = Field(default_factory=list, max_length=1000)
    filters: list[FilterIn] = Field(default_factory=list, max_length=20)
    search: str | None = Field(default=None, max_length=1000)
    search_columns: list[str] = Field(default_factory=list, max_length=20)
    all_rows: bool = False


class InsertBody(Strict):
    values: dict[str, Any] = Field(default_factory=dict)


class UpdateBody(Strict):
    values: dict[str, Any]
    expected: dict[str, Any]


class DeleteBody(Strict):
    expected: dict[str, Any] = Field(default_factory=dict)
    confirm: str | None = None


class BulkPreviewBody(Strict):
    operation: Literal["delete", "update"]
    selector: SelectorBody
    values: dict[str, Any] | None = None


class BulkExecBody(Strict):
    selector: SelectorBody
    values: dict[str, Any] | None = None
    token: str
    expected_count: int = Field(ge=0)
    confirm: str | None = None


class RowResult(BaseModel):
    columns: list[dict[str, str]]
    row: list[Any] | None
    row_key: str | None


class BulkPreviewOut(BaseModel):
    count: int
    columns: list[str]
    sample: list[list[Any]]
    token: str
    requires_confirmation: bool


class BulkResult(BaseModel):
    affected: int


class CellOut(BaseModel):
    value: Any
    length: int | None


class MarkerIn(Strict):
    database: str = Field(max_length=255)
    schema_name: str = Field(default="", alias="schema", max_length=255)
    object: str = Field(max_length=255)
    field: str = Field(max_length=500)


class MarkerOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    database: str
    schema_name: str = Field(alias="schema")
    object: str
    field: str


class MarkersBody(Strict):
    markers: list[MarkerIn] = Field(max_length=2000)


# ---------------------------------------------------------------------------- helpers

UNCHECKED = "$unchecked"


@dataclass
class Target:
    cid: uuid.UUID
    database: str
    schema: str | None
    table: str

    @property
    def path(self) -> str:
        return "/".join([str(self.cid), self.database, self.schema or "", self.table])

    def audit_target(self) -> dict[str, Any]:
        return {"database": self.database, "schema": self.schema, "table": self.table}


def target_with_schema(cid: uuid.UUID, database: str, schema: str, table: str) -> Target:
    return Target(cid, database, schema, table)


def target_flat(cid: uuid.UUID, database: str, table: str) -> Target:
    return Target(cid, database, None, table)


def _filters(items: list[FilterIn]) -> list[FilterSpec]:
    return [FilterSpec(f.column, f.op, f.value) for f in items]


def _selector(body: SelectorBody) -> Selector:
    return Selector(
        row_keys=body.row_keys,
        filters=_filters(body.filters),
        search=body.search,
        search_columns=body.search_columns,
        all_rows=body.all_rows,
    )


def _split_expected(
    values: dict[str, Any], expected: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Every edited column needs its previous value (optimistic concurrency). A column whose
    old value cannot be compared (JSON, binary, truncated) is explicitly marked unchecked."""
    missing = [c for c in values if c not in expected]
    if missing:
        raise ValidationFailed(
            f"Missing expected (previous) value for: {', '.join(missing)}. "
            "Send the value you saw, or {'$unchecked': true} if it cannot be compared."
        )
    checked: dict[str, Any] = {}
    unchecked: list[str] = []
    for col, old in expected.items():
        if (
            isinstance(old, dict)
            and old.get(UNCHECKED) is True
            or isinstance(old, dict)
            and ("$truncated" in old or "$binary" in old or "$masked" in old)
        ):
            unchecked.append(col)
        else:
            checked[col] = old
    return checked, unchecked


def _row_out(
    adapter: Any,
    st: TableStructure,
    raw: tuple[Any, ...] | None,
    masked: frozenset[str],
    max_chars: int,
) -> RowResult:
    cols = [{"name": c.name, "type": c.data_type} for c in st.columns]
    if raw is None:
        return RowResult(columns=cols, row=None, row_key=None)
    cells = [
        {"$masked": True} if c.name in masked and v is not None else to_json_value(v, max_chars)
        for c, v in zip(st.columns, raw, strict=True)
    ]
    return RowResult(
        columns=cols, row=cells, row_key=adapter.row_key_for(st, raw, st.column_names())
    )


async def _open(
    db: DbSession, ctx: AuthContext, settings: AppSettings, t: Target, perms: list[Perm]
) -> tuple[conns.Access, Any]:
    scope = ObjectScope(t.database, t.schema, t.table)
    access, adapter = await conns.open_adapter(
        db, ctx, settings, t.cid, perms[0], "relational", scope
    )
    require_all(access, perms[1:])
    return access, conns.as_relational(adapter)


def _bulk_claims(
    op: str, t: Target, sel: SelectorBody, values: dict[str, Any] | None, n: int
) -> dict[str, Any]:
    return {
        "op": op,
        "t": t.path,
        "sel": tokens.digest(sel.model_dump()),
        "v": tokens.digest(values),
        "n": n,
    }


# ---------------------------------------------------------------------------- relational routes


def build_relational_router(dep: Callable[..., Target]) -> APIRouter:  # noqa: PLR0915
    r = APIRouter()
    T = Annotated[Target, Depends(dep)]

    @r.post("/rows", status_code=201, response_model=RowResult)
    async def insert_row(
        t: T, body: InsertBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
    ) -> RowResult:
        access, adapter = await _open(db, ctx, settings, t, [Perm.DATA_INSERT])
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        reject_masked(body.values, masked, "write")
        async with ex.audited(ctx, "data.insert", t.cid, t.audit_target()) as detail:
            st, raw = await run_blocking(
                adapter.insert_row, t.database, t.schema, t.table, body.values
            )
            detail.update(columns=sorted(body.values))
        return _row_out(adapter, st, raw, masked, settings.max_cell_chars)

    @r.patch("/rows/{row_key}", response_model=RowResult)
    async def update_row(
        t: T, row_key: str, body: UpdateBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
    ) -> RowResult:
        access, adapter = await _open(db, ctx, settings, t, [Perm.DATA_UPDATE])
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        reject_masked([*body.values, *body.expected], masked, "write")
        checked, unchecked = _split_expected(body.values, body.expected)
        async with ex.audited(ctx, "data.update", t.cid, t.audit_target()) as detail:
            st, raw = await run_blocking(
                adapter.update_row, t.database, t.schema, t.table, row_key, body.values, checked
            )
            detail.update(columns=sorted(body.values), unchecked=sorted(unchecked))
        return _row_out(adapter, st, raw, masked, settings.max_cell_chars)

    @r.delete("/rows/{row_key}", status_code=204)
    async def delete_row(
        t: T, row_key: str, body: DeleteBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
    ) -> None:
        access, adapter = await _open(db, ctx, settings, t, [Perm.DATA_DELETE])
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        reject_masked(body.expected, masked, "compare")
        try:
            require_confirmation(access, body.confirm, t.table)
        except ConfirmationRequired as exc:
            await audit.record(
                "data.delete", actor=ctx.actor, connection_id=t.cid, target=t.audit_target(),
                result="denied", detail={"reason": exc.code},
            )  # fmt: skip
            raise
        expected = {
            k: v
            for k, v in body.expected.items()
            if not (isinstance(v, dict) and (UNCHECKED in v or "$truncated" in v or "$binary" in v))
        }
        async with ex.audited(ctx, "data.delete", t.cid, t.audit_target()) as detail:
            await run_blocking(adapter.delete_row, t.database, t.schema, t.table, row_key, expected)
            detail.update(rows=1)

    # ---- bulk -----------------------------------------------------------------------------

    @r.post("/rows/bulk/preview", response_model=BulkPreviewOut)
    async def bulk_preview(
        t: T, body: BulkPreviewBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
    ) -> BulkPreviewOut:
        need = [
            Perm.DATA_DELETE if body.operation == "delete" else Perm.DATA_UPDATE,
            Perm.DATA_BULK_WRITE,
        ]
        access, adapter = await _open(db, ctx, settings, t, need)
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        sel = _selector(body.selector)
        reject_masked([f.column for f in sel.filters] + sel.search_columns, masked, "filter by")
        if body.operation == "update":
            if not body.values:
                raise ValidationFailed("values are required for a bulk update.")
            reject_masked(body.values, masked, "write")
        prev = await run_blocking(adapter.bulk_preview, t.database, t.schema, t.table, sel)
        sample = [
            [
                {"$masked": True} if c in masked and v is not None else v
                for c, v in zip(prev.columns, row, strict=True)
            ]
            for row in prev.sample
        ]
        token = tokens.issue(
            ctx.user.id, _bulk_claims(body.operation, t, body.selector, body.values, prev.count)
        )
        return BulkPreviewOut(
            count=prev.count,
            columns=prev.columns,
            sample=sample,
            token=token,
            requires_confirmation=access.connection.environment == "production",
        )

    async def _bulk_exec(
        op: Literal["delete", "update"],
        t: Target,
        body: BulkExecBody,
        db: DbSession,
        settings: AppSettings,
        ctx: AuthContext,
        approval_id: uuid.UUID | None = None,
    ) -> BulkResult:
        need = [Perm.DATA_DELETE if op == "delete" else Perm.DATA_UPDATE, Perm.DATA_BULK_WRITE]
        access, adapter = await _open(db, ctx, settings, t, need)
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        sel = _selector(body.selector)
        reject_masked([f.column for f in sel.filters] + sel.search_columns, masked, "filter by")
        if op == "update":
            if not body.values:
                raise ValidationFailed("values are required for a bulk update.")
            reject_masked(body.values, masked, "write")
        tokens.verify(
            body.token,
            ctx.user.id,
            _bulk_claims(op, t, body.selector, body.values, body.expected_count),
        )
        require_confirmation(access, body.confirm, t.table)
        claims = _bulk_claims(op, t, body.selector, body.values, body.expected_count)
        await gov.gate(
            db, ctx, access, f"data.bulk_{op}", claims, approval_id,
            summary={**t.audit_target(), "operation": op, "rows": body.expected_count},
        )  # fmt: skip
        action = f"data.bulk_{op}"
        async with ex.audited(ctx, action, t.cid, t.audit_target()) as detail:
            if op == "delete":
                n = await run_blocking(
                    adapter.bulk_delete, t.database, t.schema, t.table, sel, body.expected_count
                )
            else:
                n = await run_blocking(
                    adapter.bulk_update, t.database, t.schema, t.table, sel,
                    body.values or {}, body.expected_count,
                )  # fmt: skip
            detail.update(
                rows=n,
                selector="keys" if sel.row_keys else "filters",
                columns=sorted(body.values or {}),
            )
        return BulkResult(affected=n)

    @r.post("/rows/bulk/delete", response_model=BulkResult)
    async def bulk_delete(
        t: T, body: BulkExecBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth,
        approval_id: ApprovalId = None,
    ) -> BulkResult:  # fmt: skip
        return await _bulk_exec("delete", t, body, db, settings, ctx, approval_id)

    @r.post("/rows/bulk/update", response_model=BulkResult)
    async def bulk_update(
        t: T, body: BulkExecBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth,
        approval_id: ApprovalId = None,
    ) -> BulkResult:  # fmt: skip
        return await _bulk_exec("update", t, body, db, settings, ctx, approval_id)

    # ---- cell + export --------------------------------------------------------------------

    @r.get("/rows/{row_key}/cells/{column}", response_model=CellOut)
    async def get_cell(
        t: T, row_key: str, column: str, db: DbSession, settings: AppSettings, ctx: CurrentAuth
    ) -> CellOut:
        access, adapter = await _open(db, ctx, settings, t, [Perm.DATA_READ])
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        reject_masked([column], masked, "read")
        async with ex.audited(
            ctx, "data.read.cell", t.cid, {**t.audit_target(), "column": column}
        ) as detail:
            out = await run_blocking(
                adapter.fetch_cell, t.database, t.schema, t.table, row_key, column
            )
            detail.update(length=out.get("length"))
        return CellOut(**out)

    @r.get("/export")
    async def export_rows(  # noqa: PLR0913
        t: T,
        db: DbSession,
        settings: AppSettings,
        ctx: CurrentAuth,
        format: Annotated[Literal["csv", "json", "ndjson"], Query()] = "csv",  # noqa: A002
        sort: ex_JsonParam = None,
        filters: ex_JsonParam = None,
        columns: ex_JsonParam = None,
        search: Annotated[str | None, Query(max_length=1000)] = None,
        search_columns: ex_JsonParam = None,
    ) -> Any:
        access, adapter = await _open(db, ctx, settings, t, [Perm.EXPORT_EXPORT, Perm.DATA_READ])
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        q = ex.parse_row_query(
            limit=1, cursor=None, sort=sort, filters=filters, columns=columns,
            search=search, search_columns=search_columns,
        )  # fmt: skip
        reject_masked(
            [f.column for f in q.filters] + [s.column for s in q.sort] + q.search_columns,
            masked,
            "filter or sort by",
        )
        meta, batches = await run_blocking(
            adapter.export_rows, t.database, t.schema, t.table,
            columns=q.columns, filters=q.filters, search=q.search,
            search_columns=q.search_columns, sort=q.sort,
        )  # fmt: skip
        names = [c["name"] for c in meta]
        return stream_export(
            ctx=ctx,
            cid=t.cid,
            fmt=format,
            filename=t.table,
            formatter=make_row_formatter(format, names, masked),
            batches=batches,
            target=t.audit_target(),
            detail={"filters": ex.filter_summary(q), "masked": sorted(set(names) & masked)},
        )

    return r


ex_JsonParam = Annotated[str | None, Query(max_length=65536)]  # noqa: N816

router = APIRouter(prefix="/connections/{cid}/databases/{database}", tags=["editor"])
router.include_router(
    build_relational_router(target_with_schema), prefix="/schemas/{schema}/tables/{table}"
)
router.include_router(build_relational_router(target_flat), prefix="/tables/{table}")


# ---------------------------------------------------------------------------- MongoDB

mongo = APIRouter(
    prefix="/connections/{cid}/mongo/databases/{database}/collections/{collection}",
    tags=["editor"],
)


class DocInsert(Strict):
    document: Any


class DocReplace(Strict):
    original: Any
    document: Any


class DocDelete(Strict):
    original: Any | None = None
    confirm: str | None = None


class DocBulkPreview(Strict):
    operation: Literal["delete", "update"]
    filter: Any
    set: Any | None = None


class DocBulkExec(Strict):
    filter: Any
    set: Any | None = None
    token: str
    expected_count: int = Field(ge=0)
    confirm: str | None = None


class DocBulkPreviewOut(BaseModel):
    count: int
    sample: list[dict[str, Any]]
    token: str
    requires_confirmation: bool


class DocOut(BaseModel):
    document: dict[str, Any]
    doc_key: str | None


async def _mongo_open(  # noqa: PLR0913
    db: DbSession,
    ctx: AuthContext,
    settings: AppSettings,
    cid: uuid.UUID,
    perms: list[Perm],
    database: str,
    collection: str,
) -> tuple[conns.Access, Any]:
    scope = ObjectScope(database, None, collection)
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perms[0], "document", scope)
    require_all(access, perms[1:])
    return access, conns.as_document(adapter)


def _doc_out(doc: dict[str, Any], masked: frozenset[str]) -> DocOut:
    from app.database.document.filters import encode_id_cursor, to_bson

    key = encode_id_cursor(to_bson(doc["_id"])) if "_id" in doc else None
    return DocOut(document=mask_document(doc, masked), doc_key=key)


def _json_filter(raw: Any) -> dict[str, Any]:
    import json

    text = raw if isinstance(raw, str) else json.dumps(raw if raw is not None else {})
    return validate_filter(text)


def _guard_paths(masked: frozenset[str], paths: set[str], action: str) -> None:
    hit = sorted(p for p in paths if path_is_masked(p, masked))
    if hit:
        raise Forbidden(
            f"You cannot {action} sensitive field(s): {', '.join(hit)}. "
            "The data.read_sensitive permission is required."
        )


@mongo.post("/documents", status_code=201, response_model=DocOut)
async def insert_document(
    cid: uuid.UUID, database: str, collection: str, body: DocInsert,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> DocOut:  # fmt: skip
    access, adapter = await _mongo_open(
        db, ctx, settings, cid, [Perm.DATA_INSERT], database, collection
    )
    masked = await masked_fields(db, access, database, None, collection)
    doc = validate_document(body.document)
    _guard_paths(masked, filter_fields(doc), "write")
    async with ex.audited(
        ctx, "data.insert", cid, {"database": database, "collection": collection}
    ) as detail:
        stored = await run_blocking(adapter.insert_document, database, collection, doc)
        detail.update(fields=len(doc))
    return _doc_out(stored, masked)


@mongo.put("/documents/{doc_key}", response_model=DocOut)
async def replace_document(
    cid: uuid.UUID, database: str, collection: str, doc_key: str, body: DocReplace,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> DocOut:  # fmt: skip
    access, adapter = await _mongo_open(
        db, ctx, settings, cid, [Perm.DATA_UPDATE], database, collection
    )
    masked = await masked_fields(db, access, database, None, collection)
    if masked:
        raise Forbidden(
            "This collection has sensitive fields you cannot see; "
            "replacing whole documents needs data.read_sensitive."
        )
    original = validate_document(body.original)
    new = validate_document(body.document)
    async with ex.audited(
        ctx, "data.update", cid, {"database": database, "collection": collection}
    ) as detail:
        stored = await run_blocking(
            adapter.replace_document, database, collection, doc_key, original, new
        )
        detail.update(fields=len(new))
    return _doc_out(stored, masked)


@mongo.delete("/documents/{doc_key}", status_code=204)
async def delete_document(
    cid: uuid.UUID, database: str, collection: str, doc_key: str, body: DocDelete,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> None:  # fmt: skip
    access, adapter = await _mongo_open(
        db, ctx, settings, cid, [Perm.DATA_DELETE], database, collection
    )
    require_confirmation(access, body.confirm, collection)
    original = validate_document(body.original) if body.original is not None else None
    async with ex.audited(
        ctx, "data.delete", cid, {"database": database, "collection": collection}
    ) as detail:
        await run_blocking(adapter.delete_document, database, collection, doc_key, original)
        detail.update(rows=1)


def _doc_claims(
    op: str, cid: uuid.UUID, database: str, collection: str, flt: Any, setv: Any, n: int
) -> dict[str, Any]:
    return {
        "op": op,
        "t": f"{cid}/{database}/{collection}",
        "sel": tokens.digest(flt),
        "v": tokens.digest(setv),
        "n": n,
    }


@mongo.post("/documents/bulk/preview", response_model=DocBulkPreviewOut)
async def documents_bulk_preview(
    cid: uuid.UUID, database: str, collection: str, body: DocBulkPreview,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> DocBulkPreviewOut:  # fmt: skip
    need = [
        Perm.DATA_DELETE if body.operation == "delete" else Perm.DATA_UPDATE,
        Perm.DATA_BULK_WRITE,
    ]
    access, adapter = await _mongo_open(db, ctx, settings, cid, need, database, collection)
    masked = await masked_fields(db, access, database, None, collection)
    flt = _json_filter(body.filter)
    _guard_paths(masked, filter_fields(flt), "filter by")
    if body.operation == "update":
        setv = validate_set(body.set)
        _guard_paths(masked, set(setv), "write")
    count, sample = await run_blocking(adapter.bulk_preview, database, collection, flt)
    token = tokens.issue(
        ctx.user.id,
        _doc_claims(body.operation, cid, database, collection, body.filter, body.set, count),
    )
    return DocBulkPreviewOut(
        count=count,
        sample=[mask_document(d, masked) for d in sample],
        token=token,
        requires_confirmation=access.connection.environment == "production",
    )


async def _doc_bulk(
    op: Literal["delete", "update"], cid: uuid.UUID, database: str, collection: str,
    body: DocBulkExec, db: DbSession, settings: AppSettings, ctx: AuthContext,
    approval_id: uuid.UUID | None = None,
) -> BulkResult:  # fmt: skip
    need = [Perm.DATA_DELETE if op == "delete" else Perm.DATA_UPDATE, Perm.DATA_BULK_WRITE]
    access, adapter = await _mongo_open(db, ctx, settings, cid, need, database, collection)
    masked = await masked_fields(db, access, database, None, collection)
    flt = _json_filter(body.filter)
    _guard_paths(masked, filter_fields(flt), "filter by")
    setv = None
    if op == "update":
        setv = validate_set(body.set)
        _guard_paths(masked, set(setv), "write")
    tokens.verify(
        body.token, ctx.user.id,
        _doc_claims(op, cid, database, collection, body.filter, body.set, body.expected_count),
    )  # fmt: skip
    require_confirmation(access, body.confirm, collection)
    claims = _doc_claims(op, cid, database, collection, body.filter, body.set, body.expected_count)
    await gov.gate(
        db, ctx, access, f"data.bulk_{op}", claims, approval_id,
        summary={"database": database, "collection": collection, "operation": op,
                 "documents": body.expected_count},
    )  # fmt: skip
    async with ex.audited(
        ctx, f"data.bulk_{op}", cid, {"database": database, "collection": collection}
    ) as detail:
        if op == "delete":
            n = await run_blocking(
                adapter.bulk_delete, database, collection, flt, body.expected_count
            )
        else:
            n = await run_blocking(
                adapter.bulk_set, database, collection, flt, setv or {}, body.expected_count
            )
        detail.update(rows=n)
    return BulkResult(affected=n)


@mongo.post("/documents/bulk/delete", response_model=BulkResult)
async def documents_bulk_delete(
    cid: uuid.UUID, database: str, collection: str, body: DocBulkExec,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> BulkResult:  # fmt: skip
    return await _doc_bulk(
        "delete", cid, database, collection, body, db, settings, ctx, approval_id
    )


@mongo.post("/documents/bulk/update", response_model=BulkResult)
async def documents_bulk_update(
    cid: uuid.UUID, database: str, collection: str, body: DocBulkExec,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> BulkResult:  # fmt: skip
    return await _doc_bulk(
        "update", cid, database, collection, body, db, settings, ctx, approval_id
    )


@mongo.get("/export")
async def export_documents(  # noqa: PLR0913
    cid: uuid.UUID, database: str, collection: str,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
    format: Annotated[Literal["json", "ndjson"], Query()] = "json",  # noqa: A002
    filter: ex_JsonParam = None,  # noqa: A002
    sort: ex_JsonParam = None,
    projection: ex_JsonParam = None,
) -> Any:  # fmt: skip
    access, adapter = await _mongo_open(
        db, ctx, settings, cid, [Perm.EXPORT_EXPORT, Perm.DATA_READ], database, collection
    )
    masked = await masked_fields(db, access, database, None, collection)
    flt = validate_filter(filter)
    srt = validate_sort(sort)
    proj = validate_projection(projection)
    _guard_paths(masked, filter_fields(flt) | {f for f, _ in srt}, "filter or sort by")
    batches = await run_blocking(adapter.export_documents, database, collection, flt, srt, proj)
    return stream_export(
        ctx=ctx,
        cid=cid,
        fmt=format,
        filename=collection,
        formatter=JsonDocs(masked, ndjson=format == "ndjson"),
        batches=batches,
        target={"database": database, "collection": collection},
        detail={"filtered": bool(flt), "masked": sorted(masked)},
    )


# ---------------------------------------------------------------------------- markers

markers = APIRouter(prefix="/connections/{cid}/sensitive", tags=["editor"])


@markers.get("", response_model=list[MarkerOut])
async def list_markers(cid: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> list[MarkerOut]:
    await conns.require_access(db, ctx, cid, Perm.METADATA_VIEW)
    rows = (
        (
            await db.execute(
                select(SensitiveMarker)
                .where(SensitiveMarker.connection_id == cid)
                .order_by(SensitiveMarker.database, SensitiveMarker.object, SensitiveMarker.field)
            )
        )
        .scalars()
        .all()
    )
    return [
        MarkerOut(database=m.database, schema_name=m.schema, object=m.object, field=m.field)
        for m in rows
    ]


@markers.put("", response_model=list[MarkerOut])
async def replace_markers(
    cid: uuid.UUID, body: MarkersBody, db: DbSession, settings: AppSettings, ctx: CurrentAuth
) -> list[MarkerOut]:
    """Replace the connection's sensitive-field markers. Every entry is validated against the
    live metadata; primary-key columns cannot be marked (row keys and cursors expose them)."""
    a = await conns.require_access(db, ctx, cid, Perm.CONNECTION_EDIT)
    is_doc = a.connection.db_type == "mongodb"
    _, adapter2 = await conns.open_adapter(
        db, ctx, settings, cid, Perm.CONNECTION_EDIT, "document" if is_doc else "relational"
    )
    seen: set[tuple[str, str, str, str]] = set()
    for m in body.markers:
        key = (m.database, m.schema_name, m.object, m.field)
        if key in seen:
            raise ValidationFailed("Duplicate marker.")
        seen.add(key)
        if is_doc:
            check_field_name(m.field)
            if m.field == "_id" or m.field.startswith("_id."):
                raise ValidationFailed("_id cannot be marked sensitive.")
            await run_blocking(conns.as_document(adapter2).get_collection, m.database, m.object)
        else:
            rel = conns.as_relational(adapter2)
            st = await run_blocking(rel.get_structure, m.database, m.schema_name or None, m.object)
            if m.field not in st.column_names():
                raise ValidationFailed(f"Unknown column: {m.field!r}")
            if m.field in st.primary_key:
                raise ValidationFailed("Primary-key columns cannot be marked sensitive.")
    await db.execute(delete(SensitiveMarker).where(SensitiveMarker.connection_id == cid))
    for m in body.markers:
        db.add(
            SensitiveMarker(
                connection_id=cid, database=m.database, schema=m.schema_name, object=m.object,
                field=m.field, created_by=ctx.user.id,
            )
        )  # fmt: skip
    await db.flush()
    await audit.record(
        "connection.sensitive.update", actor=ctx.actor, connection_id=cid,
        target={"name": a.connection.name}, detail={"markers": len(body.markers)},
    )  # fmt: skip
    return [
        MarkerOut(database=m.database, schema_name=m.schema_name, object=m.object, field=m.field)
        for m in body.markers
    ]
