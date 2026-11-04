"""Relational adapter contract and the shared row-fetch pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import Any

from app.database.core.base import DatabaseAdapter, TtlCache
from app.database.core.serialization import approx_size, to_json_value
from app.database.relational.editing import RelationalEditing
from app.database.relational.models import (
    DatabaseInfo,
    FunctionInfo,
    RawResult,
    RowPage,
    RowQuery,
    SchemaInfo,
    SequenceInfo,
    TableInfo,
    TableStructure,
)
from app.database.relational.sql_builder import Dialect, build_select, encode_cursor
from app.errors import NotFound, UnsupportedCapability


class RelationalAdapter(DatabaseAdapter, RelationalEditing, ABC):
    dialect: Dialect

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._meta = TtlCache(ttl=30.0)

    # ---- metadata ---------------------------------------------------------------------

    @abstractmethod
    def list_databases(self) -> list[DatabaseInfo]: ...

    @abstractmethod
    def list_schemas(self, database: str) -> list[SchemaInfo]: ...

    @abstractmethod
    def list_tables(self, database: str, schema: str | None) -> list[TableInfo]: ...

    @abstractmethod
    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure: ...

    def get_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        return self._meta.get_or_load(
            ("structure", database, schema, table),
            lambda: self._load_structure(database, schema, table),
        )

    def list_sequences(self, database: str, schema: str | None) -> list[SequenceInfo]:
        raise UnsupportedCapability("This engine does not expose sequences.")

    def list_functions(self, database: str, schema: str | None) -> list[FunctionInfo]:
        raise UnsupportedCapability("This engine does not expose functions here.")

    def get_function(self, database: str, schema: str | None, function_id: str) -> FunctionInfo:
        raise UnsupportedCapability("This engine does not expose functions here.")

    # ---- data -------------------------------------------------------------------------

    @abstractmethod
    def _namespace(self, database: str, schema: str | None) -> str | None:
        """Namespace used to qualify the table in SQL (schema for PG, database for MySQL)."""

    @abstractmethod
    def _query(
        self, database: str, sql: str, params: list[Any]
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run a SELECT in a read-only transaction with the statement timeout applied."""

    def fetch_rows(
        self,
        database: str,
        schema: str | None,
        table: str,
        q: RowQuery,
        masked: frozenset[str] = frozenset(),
    ) -> RowPage:
        st = self.get_structure(database, schema, table)
        if st.kind in ("sequence",):
            raise NotFound("Object has no rows.")
        built = build_select(self.dialect, self._namespace(database, schema), st, q, self.limits)
        _names, raw = self._query(database, built.sql, built.params)

        has_more = len(raw) > built.limit
        raw = raw[: built.limit]
        n_visible = len(built.visible_columns)
        max_chars = self.limits.max_cell_chars

        editable = bool(st.primary_key) and st.kind in ("table", "partitioned_table")
        select_names = built.visible_columns + built.hidden_pk_columns
        out: list[list[Any]] = []
        row_keys: list[str | None] = []
        size = 0
        clipped = False
        for row in raw:
            cells = [to_json_value(v, max_chars) for v in row[:n_visible]]
            for i, col in enumerate(built.visible_columns):
                if col in masked and cells[i] is not None:
                    cells[i] = {"$masked": True}
            size += approx_size(cells)
            if out and size > self.limits.max_response_bytes:
                clipped = has_more = True
                break
            out.append(cells)
            row_keys.append(self.row_key_for(st, row, select_names) if editable else None)

        next_cursor: str | None = None
        if has_more and out:
            last_raw = raw[len(out) - 1]
            if built.pagination == "keyset":
                next_cursor = encode_cursor(
                    {"k": [to_json_value(last_raw[i], 1 << 20) for i in built.pk_positions]}
                )
            else:
                base = 0
                if q.cursor:
                    from app.database.relational.sql_builder import decode_cursor

                    base = int(decode_cursor(q.cursor).get("o", 0))
                next_cursor = encode_cursor({"o": base + len(out)})

        types = {c.name: c.data_type for c in st.columns}
        warnings = list(built.warnings)
        if clipped:
            warnings.append("Response was cut at the size limit.")
        return RowPage(
            columns=[{"name": c, "type": types.get(c, "")} for c in built.visible_columns],
            rows=out,
            limit=built.limit,
            next_cursor=next_cursor,
            total_estimate=None if (q.filters or q.search) else st.estimated_rows,
            truncated=built.requested_limit_clamped or clipped,
            pagination=built.pagination,  # type: ignore[arg-type]
            warnings=warnings,
            row_keys=row_keys,
            editable=editable,
        )

    # ---- user-authored SQL (Phase 4) ---------------------------------------------------

    @abstractmethod
    def run_sql(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        statements: list[str],
        *,
        write: bool,
        script: bool,
        timeout_s: int,
        max_rows: int,
        rollback: bool,
        on_start: Callable[[dict[str, Any]], None],
    ) -> list[RawResult]:
        """Run on a dedicated, non-pooled connection (so session changes never leak to other
        users). Read-only unless ``write``; the transaction is committed only for ``write`` and
        not ``rollback``. ``on_start`` receives the backend reference used for cancellation."""

    @abstractmethod
    def cancel_backend(self, ref: dict[str, Any]) -> None: ...

    @abstractmethod
    def stream_query(
        self, database: str, schema: str | None, sql: str, batch: int = 1000
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        """Stream a single read-only statement (dedicated connection, closed with the
        iterator)."""
