"""Import with validation and preview (Phase 5): CSV into tables, JSON / NDJSON into collections.

Two steps, like bulk edits: ``preview`` validates everything against the real database
(relational: a rolled-back trial insert of every row) and returns a signed token bound to the
exact payload; ``commit`` needs that token (and approval where policy demands it).
"""

# NOTE: no `from __future__ import annotations` (route factory with factory-local annotations,
# see api/editor.py).
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.editor import Target, target_flat, target_with_schema
from app.api.schemas import ApprovalId, Strict
from app.auth.deps import AppSettings, AuthContext, CurrentAuth, DbSession
from app.auth.permissions import Perm
from app.connections import service as conns
from app.connections.scope import ObjectScope
from app.database.document.filters import filter_fields, path_is_masked, validate_document
from app.editor import tokens
from app.editor.guards import masked_fields, reject_masked, require_all, require_confirmation
from app.errors import Forbidden, ValidationFailed
from app.explorer import service as ex
from app.governance import service as gov
from app.importing import parse
from app.services.runtime import run_blocking

MAX_CONTENT = 8_000_000


def _ejson(doc: dict[str, Any]) -> Any:
    import json

    from bson import json_util

    return json.loads(json_util.dumps(doc, json_options=json_util.CANONICAL_JSON_OPTIONS))


SAMPLE_ROWS = 5

# ---------------------------------------------------------------------------- schemas


class CsvImport(Strict):
    content: str = Field(max_length=MAX_CONTENT)
    delimiter: Literal[",", ";", "tab", "|"] = ","
    has_header: bool = True
    mapping: dict[str, str] | None = Field(
        default=None,
        max_length=500,
        description="CSV column (header name, or 0-based index without a header) -> table column. "
        "Omit to use the header names as column names.",
    )
    empty_as_null: bool = True


class CsvCommit(CsvImport):
    token: str
    confirm: str | None = Field(default=None, max_length=255)


class RowProblem(BaseModel):
    row: int
    message: str


class CsvPreviewOut(BaseModel):
    rows: int
    valid_rows: int
    errors: list[RowProblem]
    columns: list[str]
    sample: list[list[str | None]]
    token: str | None
    requires_confirmation: bool


class ImportDone(BaseModel):
    imported: int


class DocImport(Strict):
    content: str = Field(max_length=MAX_CONTENT)
    format: Literal["json", "ndjson"] = "json"


class DocCommit(DocImport):
    token: str
    confirm: str | None = Field(default=None, max_length=255)


class DocProblem(BaseModel):
    index: int
    message: str


class DocPreviewOut(BaseModel):
    documents: int
    valid_documents: int
    errors: list[DocProblem]
    sample: list[Any]
    token: str | None
    requires_confirmation: bool


# ---------------------------------------------------------------------------- relational


def _resolve_columns(
    body: CsvImport, header: list[str] | None, width: int
) -> list[tuple[int, str]]:
    """(csv column index, table column) pairs in file order."""
    pairs: list[tuple[int, str]] = []
    if header is not None:
        for i, name in enumerate(header):
            target = body.mapping.get(name) if body.mapping is not None else name
            if target:
                pairs.append((i, target))
        if body.mapping is not None:
            unknown = sorted(set(body.mapping) - set(header))
            if unknown:
                raise ValidationFailed(f"Mapping names CSV column(s) not in the header: {unknown}.")
    else:
        if not body.mapping:
            raise ValidationFailed("Without a header row a mapping by column index is required.")
        for key, target in body.mapping.items():
            if not key.isdigit() or int(key) >= width:
                raise ValidationFailed(f"Mapping key {key!r} is not a column index of the file.")
            pairs.append((int(key), target))
        pairs.sort()
    if not pairs:
        raise ValidationFailed("No CSV column is mapped to a table column.")
    return pairs


def _cells(
    rows: list[list[str]], pairs: list[tuple[int, str]], empty_as_null: bool
) -> list[list[str | None]]:
    idx = [i for i, _ in pairs]
    out: list[list[str | None]] = []
    for r in rows:
        out.append([(None if (empty_as_null and r[i] == "") else r[i]) for i in idx])
    return out


def _claims(t: Target, body: CsvImport, n: int) -> dict[str, Any]:
    return {
        "op": "import.csv",
        "t": t.path,
        "d": parse.digest(
            body.content, body.delimiter, body.has_header, body.mapping, body.empty_as_null
        ),
        "n": n,
    }


def build_router(dep: Callable[..., Target]) -> APIRouter:
    r = APIRouter()
    T = Annotated[Target, Depends(dep)]

    async def _prepare(
        t: Target, body: CsvImport, db: DbSession, settings: AppSettings, ctx: AuthContext
    ) -> tuple[conns.Access, Any, list[str], list[list[str | None]]]:
        access, adapter = await conns.open_adapter(
            db, ctx, settings, t.cid, Perm.EXPORT_IMPORT, "relational",
            ObjectScope(t.database, t.schema, t.table),
        )  # fmt: skip
        require_all(access, [Perm.DATA_INSERT])
        rel = conns.as_relational(adapter)
        masked = await masked_fields(db, access, t.database, t.schema, t.table)
        header, rows = parse.parse_csv(
            body.content,
            delimiter=body.delimiter,
            has_header=body.has_header,
            max_rows=settings.import_max_rows,
        )
        pairs = _resolve_columns(body, header, len(rows[0]))
        columns = [c for _, c in pairs]
        reject_masked(columns, masked, "write")
        return access, rel, columns, _cells(rows, pairs, body.empty_as_null)

    @r.post("/import/preview", response_model=CsvPreviewOut)
    async def preview(
        t: T, body: CsvImport, db: DbSession, settings: AppSettings, ctx: CurrentAuth
    ) -> CsvPreviewOut:
        access, rel, columns, cells = await _prepare(t, body, db, settings, ctx)
        async with ex.audited(ctx, "import.preview", t.cid, t.audit_target()) as detail:
            res = await run_blocking(
                rel.import_rows, t.database, t.schema, t.table, columns, cells, commit=False
            )
            detail.update(rows=res.rows, errors=len(res.errors), columns=columns)
        ok = not res.errors
        return CsvPreviewOut(
            rows=res.rows,
            valid_rows=res.inserted,
            errors=[RowProblem(row=e.row, message=e.message) for e in res.errors],
            columns=columns,
            sample=cells[:SAMPLE_ROWS],
            token=tokens.issue(ctx.user.id, _claims(t, body, res.rows)) if ok else None,
            requires_confirmation=access.connection.environment == "production",
        )

    @r.post("/import/commit", response_model=ImportDone)
    async def commit(
        t: T,
        body: CsvCommit,
        db: DbSession,
        settings: AppSettings,
        ctx: CurrentAuth,
        approval_id: ApprovalId = None,
    ) -> ImportDone:
        access, rel, columns, cells = await _prepare(t, body, db, settings, ctx)
        tokens.verify(body.token, ctx.user.id, _claims(t, body, len(cells)))
        require_confirmation(access, body.confirm, t.table)
        claims = _claims(t, body, len(cells))
        await gov.gate(
            db, ctx, access, "import.commit", claims, approval_id,
            summary={**t.audit_target(), "rows": len(cells), "columns": columns},
        )  # fmt: skip
        async with ex.audited(ctx, "import.commit", t.cid, t.audit_target()) as detail:
            res = await run_blocking(
                rel.import_rows, t.database, t.schema, t.table, columns, cells, commit=True
            )
            detail.update(rows=res.inserted, columns=columns, digest=claims["d"])
        return ImportDone(imported=res.inserted)

    return r


router = APIRouter(prefix="/connections/{cid}/databases/{database}", tags=["import"])
router.include_router(build_router(target_with_schema), prefix="/schemas/{schema}/tables/{table}")
router.include_router(build_router(target_flat), prefix="/tables/{table}")

# ---------------------------------------------------------------------------- MongoDB

mongo = APIRouter(
    prefix="/connections/{cid}/mongo/databases/{database}/collections/{collection}",
    tags=["import"],
)


def _doc_claims(
    cid: uuid.UUID, database: str, collection: str, body: DocImport, n: int
) -> dict[str, Any]:
    return {
        "op": "import.docs",
        "t": f"{cid}/{database}/{collection}",
        "d": parse.digest(body.content, body.format),
        "n": n,
    }


async def _prepare_docs(
    cid: uuid.UUID, database: str, collection: str, body: DocImport,
    db: DbSession, settings: AppSettings, ctx: AuthContext,
) -> tuple[conns.Access, Any, list[dict[str, Any]], list[DocProblem]]:  # fmt: skip
    access, adapter = await conns.open_adapter(
        db, ctx, settings, cid, Perm.EXPORT_IMPORT, "document",
        ObjectScope(database, None, collection),
    )  # fmt: skip
    require_all(access, [Perm.DATA_INSERT])
    doc = conns.as_document(adapter)
    masked = await masked_fields(db, access, database, None, collection)
    raw = parse.parse_documents(
        body.content, fmt=body.format, max_docs=settings.import_max_documents
    )
    docs: list[dict[str, Any]] = []
    problems: list[DocProblem] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, start=1):
        try:
            d = validate_document(item)
            hit = sorted(p for p in filter_fields(d) if path_is_masked(p, masked))
            if hit:
                raise Forbidden(f"Sensitive field(s) cannot be written: {', '.join(hit)}.")
            if "_id" in d:
                key = repr(d["_id"])
                if key in seen:
                    raise ValidationFailed("Duplicate _id within the file.")
                seen.add(key)
        except Forbidden:
            raise
        except ValidationFailed as exc:
            if len(problems) < 20:
                problems.append(DocProblem(index=i, message=exc.detail))
            continue
        docs.append(d)
    return access, doc, docs, problems


@mongo.post("/import/preview", response_model=DocPreviewOut)
async def preview_documents(
    cid: uuid.UUID, database: str, collection: str, body: DocImport,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth,
) -> DocPreviewOut:  # fmt: skip
    access, adapter, docs, problems = await _prepare_docs(
        cid, database, collection, body, db, settings, ctx
    )
    total = len(
        parse.parse_documents(body.content, fmt=body.format, max_docs=settings.import_max_documents)
    )
    async with ex.audited(
        ctx, "import.preview", cid, {"database": database, "collection": collection}
    ) as detail:
        ids = [d["_id"] for d in docs if "_id" in d]
        clash = (
            await run_blocking(adapter.existing_ids, database, collection, ids) if ids else set()
        )
        if clash and len(problems) < 20:
            problems.append(
                DocProblem(
                    index=0, message=f"{len(clash)} _id value(s) already exist in the collection."
                )
            )
        detail.update(documents=total, errors=len(problems))
    ok = not problems and not clash
    return DocPreviewOut(
        documents=total,
        valid_documents=len(docs) - len(clash),
        errors=problems,
        sample=[_ejson(d) for d in docs[:SAMPLE_ROWS]],
        token=tokens.issue(ctx.user.id, _doc_claims(cid, database, collection, body, total))
        if ok
        else None,
        requires_confirmation=access.connection.environment == "production",
    )


@mongo.post("/import/commit", response_model=ImportDone)
async def commit_documents(
    cid: uuid.UUID, database: str, collection: str, body: DocCommit,
    db: DbSession, settings: AppSettings, ctx: CurrentAuth, approval_id: ApprovalId = None,
) -> ImportDone:  # fmt: skip
    access, adapter, docs, problems = await _prepare_docs(
        cid, database, collection, body, db, settings, ctx
    )
    if problems:
        raise ValidationFailed("The file has invalid documents; run a preview and fix them first.")
    tokens.verify(body.token, ctx.user.id, _doc_claims(cid, database, collection, body, len(docs)))
    require_confirmation(access, body.confirm, collection)
    claims = _doc_claims(cid, database, collection, body, len(docs))
    await gov.gate(
        db, ctx, access, "import.commit", claims, approval_id,
        summary={"database": database, "collection": collection, "documents": len(docs)},
    )  # fmt: skip
    async with ex.audited(
        ctx, "import.commit", cid, {"database": database, "collection": collection}
    ) as detail:
        inserted, errors = await run_blocking(adapter.import_documents, database, collection, docs)
        detail.update(documents=inserted, failed=len(errors), digest=claims["d"])
    if errors:
        raise ValidationFailed(
            f"{inserted} document(s) were imported, {len(errors)} were rejected by the server "
            "(MongoDB imports are not atomic)."
        )
    return ImportDone(imported=inserted)
