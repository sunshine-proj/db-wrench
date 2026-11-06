"""Write operations for relational adapters (Phase 3).

Rules enforced here, independent of the HTTP layer:

* writes need ``spec.allow_writes`` (False for read-only connections) and use a **dedicated,
  non-pooled, writable** connection that is closed afterwards, so pooled readers stay read-only;
* only real tables with a primary key can be edited; rows are addressed by an opaque row key
  built from the primary-key values;
* updates and deletes carry the previous values of the touched columns and use null-safe
  equality, so a concurrent change is detected (optimistic concurrency);
* bulk operations re-count inside the transaction and roll back if the count differs from the
  one the user previewed.
"""

from __future__ import annotations

import base64
import binascii
import re
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.database.core.base import ConnectionSpec, Limits
from app.database.core.serialization import binary_marker, to_json_value
from app.database.relational.models import (
    ColumnInfo,
    FilterSpec,
    SortSpec,
    TableStructure,
)
from app.database.relational.sql_builder import (
    Dialect,
    build_capped_count,
    build_export_select,
    build_where,
    decode_row_key,
    encode_row_key,
)
from app.errors import Conflict, Forbidden, NotEditable, NotFound, ValidationFailed

MAX_ROW_KEYS = 1000
MAX_COLUMNS_WRITTEN = 500
PREVIEW_ROWS = 10


@dataclass
class Selector:
    """Which rows a bulk operation touches: explicit row keys *or* filters (never both)."""

    row_keys: list[str] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    search: str | None = None
    search_columns: list[str] = field(default_factory=list)
    all_rows: bool = False


@dataclass
class BulkPreview:
    count: int
    columns: list[str]
    sample: list[list[Any]]


class RelationalEditing:
    # Provided by the concrete adapter / RelationalAdapter.
    spec: ConnectionSpec
    limits: Limits
    dialect: Dialect

    @abstractmethod
    def get_structure(self, database: str, schema: str | None, table: str) -> TableStructure: ...

    @abstractmethod
    def _namespace(self, database: str, schema: str | None) -> str | None: ...

    @abstractmethod
    def _require_database(self, database: str) -> None: ...

    @abstractmethod
    def _query(
        self, database: str, sql: str, params: list[Any]
    ) -> tuple[list[str], list[tuple[Any, ...]]]: ...

    @abstractmethod
    def _errors(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def _connect_dedicated(self, database: str, *, writable: bool) -> Any:
        """A new, non-pooled DBAPI connection (transaction open, autocommit off)."""

    @abstractmethod
    def _insert(
        self, cur: Any, st: TableStructure, ns: str | None, cols: list[str], vals: list[Any]
    ) -> list[Any] | None:
        """Run the INSERT; return the new row's primary-key values (None if unknowable)."""

    @abstractmethod
    def _adapt_complex(self, col: ColumnInfo, value: dict[str, Any] | list[Any]) -> Any: ...

    @abstractmethod
    def _stream_batches(
        self, database: str, sql: str, params: list[Any], batch: int = 1000
    ) -> Iterator[list[tuple[Any, ...]]]:
        """Stream a SELECT in batches on a dedicated read-only connection (closed on exit)."""

    # ------------------------------------------------------------------ validation helpers

    def _assert_writable(self) -> None:
        if not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")

    @staticmethod
    def _assert_editable(st: TableStructure, *, need_pk: bool = True) -> None:
        if st.kind not in ("table", "partitioned_table"):
            raise NotEditable(f"{st.kind.replace('_', ' ').capitalize()}s cannot be edited.")
        if need_pk and not st.primary_key:
            raise NotEditable(
                "This table has no primary key, so a row cannot be identified reliably."
            )

    def _adapt(self, col: ColumnInfo, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            if "\x00" in value:
                raise ValidationFailed(f"Value for {col.name!r} contains a NUL character.")
            return value
        if isinstance(value, dict) and set(value) == {"$base64"}:
            try:
                return base64.b64decode(str(value["$base64"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValidationFailed(f"Value for {col.name!r} is not valid base64.") from exc
        if isinstance(value, dict | list):
            return self._adapt_complex(col, value)
        raise ValidationFailed(f"Unsupported value for {col.name!r}.")

    def _prepare(
        self, st: TableStructure, values: dict[str, Any], *, allow_pk: bool
    ) -> tuple[list[str], list[Any]]:
        if len(values) > MAX_COLUMNS_WRITTEN:
            raise ValidationFailed("Too many columns.")
        by_name = {c.name: c for c in st.columns}
        cols: list[str] = []
        vals: list[Any] = []
        for name, value in values.items():
            col = by_name.get(name)
            if col is None:
                raise ValidationFailed(f"Unknown column: {name!r}")
            if col.is_generated:
                raise ValidationFailed(f"Column {name!r} is generated and cannot be written.")
            if not allow_pk and name in st.primary_key:
                raise ValidationFailed(
                    f"Primary-key column {name!r} cannot be changed here; delete and re-insert."
                )
            cols.append(name)
            vals.append(self._adapt(col, value))
        return cols, vals

    def _expected_sql(self, st: TableStructure, expected: dict[str, Any]) -> tuple[str, list[Any]]:
        by_name = {c.name: c for c in st.columns}
        parts: list[str] = []
        params: list[Any] = []
        for name, value in expected.items():
            if name not in by_name:
                raise ValidationFailed(f"Unknown column: {name!r}")
            if isinstance(value, dict | list):
                raise ValidationFailed("Expected values must be scalars.")
            if not self._typed_scalar_ok(by_name[name], value):
                raise ValidationFailed(f"Expected value for {name!r} is not a valid number.")
            parts.append(self.dialect.null_safe_eq(self.dialect.quote(name)))
            params.append(value)
        return (" AND " + " AND ".join(parts)) if parts else "", params

    _INT_TYPES = frozenset(
        {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "serial", "bigserial",
         "smallserial", "int2", "int4", "int8"}
    )  # fmt: skip
    _NUM_TYPES = frozenset({"numeric", "decimal", "float", "double", "real", "float4", "float8"})
    _INT_RE = re.compile(r"-?[0-9]{1,40}")
    _NUM_RE = re.compile(r"-?[0-9]{1,40}(\.[0-9]{1,40})?([eE][+-]?[0-9]{1,4})?")

    def _typed_scalar_ok(self, col: ColumnInfo | None, value: Any) -> bool:
        """A value compared with a numeric column must itself be a number.

        Engines differ in what they do with ``WHERE int_col = '1 OR 1=1'``: PostgreSQL and
        MySQL strict mode raise, MariaDB silently coerces it to ``1`` (and would match a row).
        Never rely on the engine: reject non-numeric text for numeric columns up front.
        """
        if col is None or value is None:
            return True
        base = re.split(r"[\s(]", col.data_type.strip().lower(), maxsplit=1)[0]
        is_int = base in self._INT_TYPES
        if not is_int and base not in self._NUM_TYPES:
            return True
        if isinstance(value, bool):
            return False
        if isinstance(value, int | float):
            return True
        if not isinstance(value, str):
            return False
        pattern = self._INT_RE if is_int else self._NUM_RE
        return bool(pattern.fullmatch(value))

    def _check_key_values(self, st: TableStructure, vals: list[Any]) -> None:
        by_name = {c.name: c for c in st.columns}
        for name, v in zip(st.primary_key, vals, strict=True):
            if v is None or not self._typed_scalar_ok(by_name.get(name), v):
                raise ValidationFailed("Invalid row key.")

    def _pk_where(self, st: TableStructure, row_key: str) -> tuple[str, list[Any]]:
        vals = decode_row_key(row_key, len(st.primary_key))
        self._check_key_values(st, vals)
        sql = " AND ".join(f"{self.dialect.quote(c)} = %s" for c in st.primary_key)
        return sql, vals

    def row_key_for(
        self, st: TableStructure, raw_row: tuple[Any, ...], names: list[str]
    ) -> str | None:
        if not st.primary_key:
            return None
        idx = {n: i for i, n in enumerate(names)}
        return encode_row_key([to_json_value(raw_row[idx[c]], 1 << 20) for c in st.primary_key])

    @contextmanager
    def _write_tx(self, database: str) -> Iterator[Any]:
        self._assert_writable()
        self._require_database(database)
        with self._errors():
            conn = self._connect_dedicated(database, writable=True)
            try:
                cur = conn.cursor()
                yield cur
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _select_by_pk(
        self, cur: Any, st: TableStructure, ns: str | None, pk_vals: list[Any]
    ) -> tuple[Any, ...] | None:
        d = self.dialect
        cond = " AND ".join(f"{d.quote(c)} = %s" for c in st.primary_key)
        sql = (  # noqa: S608
            f"SELECT {', '.join(d.quote(c) for c in st.column_names())} "
            f"FROM {d.qualified(ns, st.name)} WHERE {cond}"
        )
        cur.execute(sql, pk_vals)
        row = cur.fetchone()
        return tuple(row) if row is not None else None

    # ------------------------------------------------------------------ single-row operations

    def insert_row(
        self, database: str, schema: str | None, table: str, values: dict[str, Any]
    ) -> tuple[TableStructure, tuple[Any, ...] | None]:
        st = self.get_structure(database, schema, table)
        self._assert_editable(st, need_pk=False)
        cols, vals = self._prepare(st, values, allow_pk=True)
        ns = self._namespace(database, schema)
        with self._write_tx(database) as cur:
            pk = self._insert(cur, st, ns, cols, vals)
            row = self._select_by_pk(cur, st, ns, pk) if pk and st.primary_key else None
        return st, row

    def update_row(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        table: str,
        row_key: str,
        values: dict[str, Any],
        expected: dict[str, Any],
    ) -> tuple[TableStructure, tuple[Any, ...]]:
        st = self.get_structure(database, schema, table)
        self._assert_editable(st)
        if not values:
            raise ValidationFailed("Nothing to update.")
        cols, vals = self._prepare(st, values, allow_pk=False)
        pk_sql, pk_vals = self._pk_where(st, row_key)
        exp_sql, exp_vals = self._expected_sql(st, expected)
        d = self.dialect
        ns = self._namespace(database, schema)
        sets = ", ".join(f"{d.quote(c)} = %s" for c in cols)
        sql = f"UPDATE {d.qualified(ns, st.name)} SET {sets} WHERE {pk_sql}{exp_sql}"  # noqa: S608
        with self._write_tx(database) as cur:
            cur.execute(sql, [*vals, *pk_vals, *exp_vals])
            if cur.rowcount != 1:
                raise Conflict(
                    "The row was changed or deleted by someone else. Reload it and try again."
                )
            row = self._select_by_pk(cur, st, ns, pk_vals)
        if row is None:
            raise NotFound("Row not found.")
        return st, row

    def delete_row(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        table: str,
        row_key: str,
        expected: dict[str, Any],
    ) -> None:
        st = self.get_structure(database, schema, table)
        self._assert_editable(st)
        pk_sql, pk_vals = self._pk_where(st, row_key)
        exp_sql, exp_vals = self._expected_sql(st, expected)
        d = self.dialect
        sql = f"DELETE FROM {d.qualified(self._namespace(database, schema), st.name)} WHERE {pk_sql}{exp_sql}"  # noqa: S608
        with self._write_tx(database) as cur:
            cur.execute(sql, [*pk_vals, *exp_vals])
            if cur.rowcount != 1:
                raise Conflict(
                    "The row was changed or deleted by someone else. Reload it and try again."
                )

    # ------------------------------------------------------------------ bulk operations

    def _selector_where(self, st: TableStructure, sel: Selector) -> tuple[list[str], list[Any]]:
        d = self.dialect
        if sel.row_keys:
            if sel.filters or sel.search:
                raise ValidationFailed("Use either row keys or filters, not both.")
            if len(sel.row_keys) > MAX_ROW_KEYS:
                raise ValidationFailed(f"At most {MAX_ROW_KEYS} rows can be selected by key.")
            parts: list[str] = []
            params: list[Any] = []
            for key in sel.row_keys:
                vals = decode_row_key(key, len(st.primary_key))
                self._check_key_values(st, vals)
                parts.append("(" + " AND ".join(f"{d.quote(c)} = %s" for c in st.primary_key) + ")")
                params.extend(vals)
            return ["(" + " OR ".join(parts) + ")"], params
        where, params = build_where(d, st, sel.filters, sel.search, sel.search_columns)
        if not where and not sel.all_rows:
            raise ValidationFailed(
                "Add at least one filter or select rows. Affecting every row needs all_rows=true."
            )
        return where, params

    def bulk_preview(
        self, database: str, schema: str | None, table: str, sel: Selector, *, writing: bool = True
    ) -> BulkPreview:
        st = self.get_structure(database, schema, table)
        if writing:
            self._assert_editable(st)
        where, params = self._selector_where(st, sel)
        d = self.dialect
        ns = self._namespace(database, schema)
        cap = self.limits.bulk_max_rows
        _, cnt = self._query(database, build_capped_count(d, ns, st, where, cap), params)
        count = int(cnt[0][0])
        if count > cap:
            raise ValidationFailed(
                f"This would affect more than {cap} rows. Narrow the selection and retry."
            )
        cols = st.column_names()[:20]
        sample_sql = f"SELECT {', '.join(d.quote(c) for c in cols)} FROM {d.qualified(ns, st.name)}"  # noqa: S608
        if where:
            sample_sql += " WHERE " + " AND ".join(where)
        sample_sql += f" LIMIT {PREVIEW_ROWS}"
        _, rows = self._query(database, sample_sql, params)
        return BulkPreview(count, cols, [[to_json_value(v, 200) for v in r] for r in rows])

    def bulk_delete(  # noqa: PLR0913
        self, database: str, schema: str | None, table: str, sel: Selector, expected_count: int
    ) -> int:
        st = self.get_structure(database, schema, table)
        self._assert_editable(st)
        where, params = self._selector_where(st, sel)
        d = self.dialect
        ns = self._namespace(database, schema)
        sql = f"DELETE FROM {d.qualified(ns, st.name)}"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._bulk_run(database, st, ns, where, params, sql, [], expected_count)

    def bulk_update(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        table: str,
        sel: Selector,
        values: dict[str, Any],
        expected_count: int,
    ) -> int:
        st = self.get_structure(database, schema, table)
        self._assert_editable(st)
        if not values:
            raise ValidationFailed("Nothing to update.")
        cols, vals = self._prepare(st, values, allow_pk=False)
        where, params = self._selector_where(st, sel)
        d = self.dialect
        ns = self._namespace(database, schema)
        sets = ", ".join(f"{d.quote(c)} = %s" for c in cols)
        sql = f"UPDATE {d.qualified(ns, st.name)} SET {sets}"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._bulk_run(database, st, ns, where, params, sql, vals, expected_count)

    def _bulk_run(  # noqa: PLR0913
        self,
        database: str,
        st: TableStructure,
        ns: str | None,
        where: list[str],
        params: list[Any],
        sql: str,
        set_params: list[Any],
        expected: int,
    ) -> int:
        cap = self.limits.bulk_max_rows
        with self._write_tx(database) as cur:
            cur.execute(build_capped_count(self.dialect, ns, st, where, cap), params)
            current = int(cur.fetchone()[0])
            if current != expected or current > cap:
                raise Conflict(
                    f"The selection changed since the preview ({current} rows now, "
                    f"{expected} previewed). Nothing was modified; preview again."
                )
            cur.execute(sql, [*set_params, *params])
            if cur.rowcount != expected:
                raise Conflict("The number of affected rows differs from the preview; rolled back.")
            return int(cur.rowcount)

    # ------------------------------------------------------------------ cell + export

    def fetch_cell(
        self, database: str, schema: str | None, table: str, row_key: str, column: str
    ) -> dict[str, Any]:
        """Full value of one cell (used when the grid shows a truncated preview)."""
        st = self.get_structure(database, schema, table)
        self._assert_editable(st)
        if column not in st.column_names():
            raise ValidationFailed(f"Unknown column: {column!r}")
        pk_sql, pk_vals = self._pk_where(st, row_key)
        d = self.dialect
        ns = self._namespace(database, schema)
        sql = f"SELECT {d.quote(column)} FROM {d.qualified(ns, st.name)} WHERE {pk_sql}"  # noqa: S608
        _, rows = self._query(database, sql, pk_vals)
        if not rows:
            raise NotFound("Row not found.")
        value = rows[0][0]
        cap = self.limits.max_cell_fetch_chars
        if isinstance(value, bytes | bytearray | memoryview):
            raw = bytes(value)
            m = binary_marker(raw)
            m["base64"] = base64.b64encode(raw[:cap]).decode()
            m["truncated"] = len(raw) > cap
            return {"value": m, "length": len(raw)}
        text_value = to_json_value(value, cap)
        length = len(value) if isinstance(value, str) else None
        return {"value": text_value, "length": length}

    def export_rows(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        table: str,
        *,
        columns: list[str] | None,
        filters: list[FilterSpec],
        search: str | None,
        search_columns: list[str],
        sort: list[SortSpec],
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        st = self.get_structure(database, schema, table)
        known = st.column_names()
        cols = list(dict.fromkeys(columns or known))
        bad = [c for c in cols if c not in known]
        if bad or not cols:
            raise ValidationFailed(f"Unknown column: {bad[0]!r}" if bad else "No columns selected.")
        where, params = build_where(self.dialect, st, filters, search, search_columns)
        ns = self._namespace(database, schema)
        cap = self.limits.export_max_rows
        _, cnt = self._query(database, build_capped_count(self.dialect, ns, st, where, cap), params)
        if int(cnt[0][0]) > cap:
            raise ValidationFailed(
                f"The export would exceed {cap:,} rows. Add filters to narrow it down."
            )
        sql = build_export_select(self.dialect, ns, st, cols, where, sort)
        types = {c.name: c.data_type for c in st.columns}
        meta = [{"name": c, "type": types.get(c, "")} for c in cols]
        return meta, self._stream_batches(database, sql, params)


__all__ = [
    "BulkPreview",
    "RelationalEditing",
    "Selector",
    "encode_row_key",
]
