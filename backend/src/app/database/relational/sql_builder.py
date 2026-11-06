"""Safe SELECT construction for relational tables.

Every identifier is validated against metadata fetched from the database itself *and*
quoted per dialect; every value is a bound parameter. Nothing client-supplied is ever
interpolated into SQL text.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from app.database.core.base import Limits
from app.database.relational.models import (
    FILTER_OPS,
    FilterSpec,
    RowQuery,
    SortSpec,
    TableStructure,
)
from app.errors import ValidationFailed

MAX_FILTERS = 20
MAX_SORT = 5
MAX_IN_VALUES = 100
MAX_VALUE_CHARS = 4096

# Primary-key column types whose JSON round-trip is exact -> eligible for keyset pagination.
KEYSET_SAFE_PREFIXES = (
    "smallint", "integer", "int", "bigint", "tinyint", "mediumint", "serial", "bigserial",
    "numeric", "decimal", "text", "character", "varchar", "char", "citext", "uuid",
    "date", "timestamp", "datetime", "time", "nvarchar", "nchar", "uniqueidentifier",
    "datetime2", "smalldatetime",
)  # fmt: skip


class Dialect:
    name = "ansi"
    quote_char = '"'
    like_op = "LIKE"
    text_type = "TEXT"

    def quote(self, ident: str) -> str:
        if not ident or "\x00" in ident:
            raise ValidationFailed("Invalid identifier.")
        q = self.quote_char
        # '%' is doubled because drivers treat it as a placeholder marker in the SQL text.
        return (q + ident.replace(q, q + q) + q).replace("%", "%%")

    def qualified(self, schema: str | None, table: str) -> str:
        return f"{self.quote(schema)}.{self.quote(table)}" if schema else self.quote(table)

    def null_safe_eq(self, col_sql: str) -> str:
        return f"{col_sql} IS NOT DISTINCT FROM %s"

    # -- hooks for dialects whose paging / row-value syntax differs (SQL Server) -------------

    def like_expr(self, col_sql: str) -> str:
        """``col LIKE %s`` with the backslash escaping used by ``escape_like``."""
        return f"{col_sql} {self.like_op} %s"

    def limit_sql(self, *, ordered: bool, limit: int, offset: int = 0) -> str:
        """Suffix appended after ORDER BY (``ordered`` tells whether one was emitted)."""
        sql = f" LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
        return sql

    def keyset_after(self, pk: list[str], values: list[Any]) -> tuple[str, list[Any]]:
        """Condition selecting rows strictly after the given primary-key tuple."""
        if len(pk) == 1:
            return f"{self.quote(pk[0])} > %s", list(values)
        cols = ", ".join(self.quote(c) for c in pk)
        return f"({cols}) > ({', '.join(['%s'] * len(pk))})", list(values)

    def capped_count(self, inner_from_where: str, cap: int) -> str:
        """``COUNT(*)`` limited to ``cap + 1`` rows; ``inner_from_where`` is ``FROM ... WHERE``."""
        return f"SELECT COUNT(*) FROM (SELECT 1 {inner_from_where} LIMIT {int(cap) + 1}) AS capped"  # noqa: S608

    def text(self, col_sql: str) -> str:
        return f"CAST({col_sql} AS {self.text_type})"


class PostgresDialect(Dialect):
    name = "postgresql"
    like_op = "ILIKE"


class MySQLDialect(Dialect):
    name = "mysql"

    def null_safe_eq(self, col_sql: str) -> str:
        return f"{col_sql} <=> %s"

    quote_char = "`"
    text_type = "CHAR"


class MSSQLDialect(Dialect):
    """T-SQL: ``[bracket]`` quoting, ``OFFSET .. FETCH`` paging, no row-value comparison."""

    name = "mssql"
    text_type = "NVARCHAR(MAX)"

    def quote(self, ident: str) -> str:
        if not ident or "\x00" in ident:
            raise ValidationFailed("Invalid identifier.")
        return ("[" + ident.replace("]", "]]") + "]").replace("%", "%%")

    def null_safe_eq(self, col_sql: str) -> str:
        # INTERSECT compares NULLs as equal and takes a single bound value.
        return f"EXISTS (SELECT {col_sql} INTERSECT SELECT %s)"

    def limit_sql(self, *, ordered: bool, limit: int, offset: int = 0) -> str:
        head = "" if ordered else " ORDER BY (SELECT NULL)"
        return f"{head} OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY"

    def keyset_after(self, pk: list[str], values: list[Any]) -> tuple[str, list[Any]]:
        q = [self.quote(c) for c in pk]
        clauses: list[str] = []
        params: list[Any] = []
        for i, col in enumerate(q):
            eq = [f"{q[j]} = %s" for j in range(i)]
            clauses.append("(" + " AND ".join([*eq, f"{col} > %s"]) + ")")
            params.extend([*values[:i], values[i]])
        return "(" + " OR ".join(clauses) + ")", params

    def capped_count(self, inner_from_where: str, cap: int) -> str:
        top = int(cap) + 1
        return f"SELECT COUNT(*) FROM (SELECT TOP ({top}) 1 AS x {inner_from_where}) AS capped"  # noqa: S608


class ClickHouseDialect(Dialect):
    """ClickHouse: backtick identifiers (backslash escapes), ILIKE, no row identity."""

    name = "clickhouse"
    quote_char = "`"
    like_op = "ILIKE"
    text_type = "String"

    def quote(self, ident: str) -> str:
        if not ident or "\x00" in ident:
            raise ValidationFailed("Invalid identifier.")
        return ("`" + ident.replace("\\", "\\\\").replace("`", "\\`") + "`").replace("%", "%%")


class SQLiteDialect(Dialect):
    name = "sqlite"
    text_type = "TEXT"

    def null_safe_eq(self, col_sql: str) -> str:
        return f"{col_sql} IS %s"  # SQLite's IS compares NULLs as equal

    def like_expr(self, col_sql: str) -> str:
        # SQLite has no default escape character: say it is the backslash used by escape_like.
        return f"{col_sql} LIKE %s ESCAPE '\\'"


class MariaDBDialect(MySQLDialect):
    name = "mariadb"


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Invalid cursor.") from exc
    if not isinstance(data, dict):
        raise ValidationFailed("Invalid cursor.")
    return data


@dataclass
class BuiltQuery:
    sql: str
    params: list[Any]
    pagination: str  # keyset | offset | unordered_offset
    limit: int
    visible_columns: list[str]
    hidden_pk_columns: list[str]
    pk_positions: list[int]  # positions of pk columns in the selected row
    warnings: list[str]
    requested_limit_clamped: bool


def _scalar(value: Any, what: str) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            raise ValidationFailed(f"{what} value is too long.")
        if "\x00" in value:
            raise ValidationFailed(f"{what} value contains a NUL character.")
        return value
    raise ValidationFailed(f"{what} must be a scalar value.")


def build_where(
    d: Dialect,
    st: TableStructure,
    filters: list[FilterSpec],
    search: str | None = None,
    search_columns: list[str] | None = None,
) -> tuple[list[str], list[Any]]:
    """WHERE fragments for filters + search. Shared by browsing, bulk edits and exports so
    every path validates columns against metadata and binds every value."""
    known = set(st.column_names())

    def need(col: str, what: str) -> str:
        if col not in known:
            raise ValidationFailed(f"Unknown {what} column: {col!r}")
        return col

    where: list[str] = []
    params: list[Any] = []
    if len(filters) > MAX_FILTERS:
        raise ValidationFailed(f"At most {MAX_FILTERS} filters are allowed.")
    for f in filters:
        col = d.quote(need(f.column, "filter"))
        if f.op not in FILTER_OPS:
            raise ValidationFailed(f"Unsupported filter operator: {f.op!r}")
        if f.op == "is_null":
            where.append(f"{col} IS NULL")
        elif f.op == "is_not_null":
            where.append(f"{col} IS NOT NULL")
        elif f.op == "in":
            if not isinstance(f.value, list) or not 1 <= len(f.value) <= MAX_IN_VALUES:
                raise ValidationFailed(f"'in' needs a list of 1-{MAX_IN_VALUES} values.")
            vals = [_scalar(v, "filter") for v in f.value]
            if any(v is None for v in vals):
                raise ValidationFailed("Use is_null to match NULL.")
            where.append(f"{col} IN ({', '.join(['%s'] * len(vals))})")
            params.extend(vals)
        elif f.op in ("contains", "starts_with", "ends_with"):
            v = _scalar(f.value, "filter")
            if not isinstance(v, str) or not v:
                raise ValidationFailed(f"'{f.op}' needs a non-empty text value.")
            esc = escape_like(v)
            pattern = {
                "contains": f"%{esc}%",
                "starts_with": f"{esc}%",
                "ends_with": f"%{esc}",
            }[f.op]
            where.append(d.like_expr(d.text(col)))
            params.append(pattern)
        else:
            v = _scalar(f.value, "filter")
            if v is None:
                raise ValidationFailed("Use is_null / is_not_null to compare with NULL.")
            sql_op = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[f.op]
            where.append(f"{col} {sql_op} %s")
            params.append(v)

    if search:
        term = _scalar(search, "search")
        if not search_columns:
            raise ValidationFailed("search_columns is required when search is used.")
        cols = [need(c, "search") for c in search_columns][:20]
        like = f"%{escape_like(str(term))}%"
        ors = [d.like_expr(d.text(d.quote(c))) for c in cols]
        where.append("(" + " OR ".join(ors) + ")")
        params.extend([like] * len(cols))
    return where, params


def encode_row_key(values: list[Any]) -> str:
    """Opaque, URL-safe identifier of a row (its primary-key values). Clients never build it."""
    return encode_cursor({"r": values})


def decode_row_key(key: str, expected_len: int) -> list[Any]:
    data = decode_cursor(key)
    vals = data.get("r")
    if not isinstance(vals, list) or len(vals) != expected_len:
        raise ValidationFailed("Row key does not match this table.")
    return [_scalar(v, "row key") for v in vals]


def pk_condition(d: Dialect, pk: list[str], values: list[Any]) -> tuple[str, list[Any]]:
    if any(v is None for v in values):
        raise ValidationFailed("Invalid row key.")
    return " AND ".join(f"{d.quote(c)} = %s" for c in pk), list(values)


def build_export_select(  # noqa: PLR0913
    d: Dialect,
    ns: str | None,
    st: TableStructure,
    columns: list[str],
    where: list[str],
    sort: list[SortSpec],
) -> str:
    known = set(st.column_names())
    order: list[str] = []
    for s in sort:
        if s.column not in known or s.direction not in ("asc", "desc"):
            raise ValidationFailed("Invalid sort.")
        order.append(f"{d.quote(s.column)} {s.direction.upper()}")
    order.extend(f"{d.quote(c)} ASC" for c in st.primary_key if c not in {s.column for s in sort})
    sql = f"SELECT {', '.join(d.quote(c) for c in columns)} FROM {d.qualified(ns, st.name)}"  # noqa: S608
    if where:
        sql += " WHERE " + " AND ".join(where)
    if order:
        sql += " ORDER BY " + ", ".join(order)
    return sql


def build_capped_count(
    d: Dialect, ns: str | None, st: TableStructure, where: list[str], cap: int
) -> str:
    inner = f"FROM {d.qualified(ns, st.name)}"
    if where:
        inner += " WHERE " + " AND ".join(where)
    return d.capped_count(inner, cap)


def keyset_eligible(st: TableStructure) -> bool:
    if not st.primary_key:
        return False
    types = {c.name: c.data_type.lower() for c in st.columns}
    return all(types.get(pk, "").startswith(KEYSET_SAFE_PREFIXES) for pk in st.primary_key)


def build_select(  # noqa: PLR0912, PLR0915
    d: Dialect, database_ns: str | None, st: TableStructure, q: RowQuery, lim: Limits
) -> BuiltQuery:
    known = set(st.column_names())

    def need(col: str, what: str) -> str:
        if col not in known:
            raise ValidationFailed(f"Unknown {what} column: {col!r}")
        return col

    visible = list(dict.fromkeys(need(c, "selected") for c in (q.columns or st.column_names())))
    if not visible:
        raise ValidationFailed("At least one column must be selected.")

    where, params = build_where(d, st, q.filters, q.search, q.search_columns)

    # --- ordering / pagination ---------------------------------------------------------
    if len(q.sort) > MAX_SORT:
        raise ValidationFailed(f"At most {MAX_SORT} sort columns are allowed.")
    warnings: list[str] = []
    limit_req = max(1, q.limit)
    limit = min(limit_req, lim.max_page_size, lim.max_result_rows)
    pk = st.primary_key
    cursor = decode_cursor(q.cursor) if q.cursor else None

    order: list[str] = []
    offset = 0
    pagination = "offset"
    hidden: list[str] = []

    if not q.sort and keyset_eligible(st):
        pagination = "keyset"
        order = [f"{d.quote(c)} ASC" for c in pk]
        if cursor is not None:
            keys = cursor.get("k")
            if not isinstance(keys, list) or len(keys) != len(pk):
                raise ValidationFailed("Cursor does not match this table.")
            vals = [_scalar(k, "cursor") for k in keys]
            if any(v is None for v in vals):
                raise ValidationFailed("Invalid cursor.")
            cond, cond_params = d.keyset_after(pk, vals)
            where.append(cond)
            params.extend(cond_params)
        hidden = [c for c in pk if c not in visible]
    else:
        hidden = [c for c in pk if c not in visible]
        for s in q.sort:
            if s.direction not in ("asc", "desc"):
                raise ValidationFailed("Sort direction must be 'asc' or 'desc'.")
            order.append(f"{d.quote(need(s.column, 'sort'))} {s.direction.upper()}")
        sorted_cols = {s.column for s in q.sort}
        order.extend(f"{d.quote(c)} ASC" for c in pk if c not in sorted_cols)
        if not order:
            pagination = "unordered_offset"
            warnings.append(
                "This table has no primary key; row order is not guaranteed to be stable "
                "across pages."
            )
        if cursor is not None:
            off = cursor.get("o")
            if not isinstance(off, int) or isinstance(off, bool) or off < 0:
                raise ValidationFailed("Invalid cursor.")
            if off > lim.max_offset:
                raise ValidationFailed(
                    f"Offset pagination is limited to {lim.max_offset} rows; add a filter."
                )
            offset = off

    select_cols = visible + hidden
    # Identifiers were validated against fetched metadata and are dialect-quoted; every value
    # is a bound parameter (see module docstring).
    sql = (
        f"SELECT {', '.join(d.quote(c) for c in select_cols)} "  # noqa: S608
        f"FROM {d.qualified(database_ns, st.name)}"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    if order:
        sql += " ORDER BY " + ", ".join(order)
    sql += d.limit_sql(
        ordered=bool(order), limit=int(limit) + 1, offset=offset if pagination != "keyset" else 0
    )

    pk_positions = [select_cols.index(c) for c in pk]
    if limit < limit_req:
        warnings.append(f"Page size was limited to {limit} rows.")
    return BuiltQuery(
        sql, params, pagination, limit, visible, hidden, pk_positions, warnings, limit < limit_req
    )
