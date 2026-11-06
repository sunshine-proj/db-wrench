"""SQL dump of one table: optional ``CREATE TABLE`` plus batched ``INSERT`` statements.

A dump has to put values into SQL text, so everything that reaches the output is escaped for the
target dialect: identifiers are quoted, text is escaped, numbers and temporal values are rendered
from typed Python values (never from client input), binary becomes a hex literal. Text that ends
up in ``--`` comments is stripped of control characters so a hostile table or column name cannot
break out of the comment into executable SQL. Masked columns are written as ``NULL`` (and listed in
the header) so a dump can never carry values the exporting user may not see.

The DDL is best effort for restoring on the *same engine*: types come from the source catalog,
sequences/serial defaults and secondary objects (indexes, foreign keys, triggers) are not included.
"""

from __future__ import annotations

import datetime as dt
import decimal
import ipaddress
import math
import re
import uuid
from typing import Any

from app.database.relational.models import ColumnInfo, TableStructure
from app.editor.export import Formatter

ROWS_PER_INSERT = 100
SQL_ENGINES = frozenset({"postgresql", "mysql", "mariadb", "mssql", "clickhouse", "sqlite"})
MYSQL_LIKE = frozenset({"mysql", "mariadb"})


def comment_text(text: str) -> str:
    """Printable, single-line text for use inside ``--`` comments."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return " ".join(cleaned.split())[:200]


def quote_ident(engine: str, ident: str) -> str:
    if "\x00" in ident or not ident:
        raise ValueError("invalid identifier")
    if engine == "mssql":
        return "[" + ident.replace("]", "]]") + "]"
    if engine in MYSQL_LIKE:
        return "`" + ident.replace("`", "``") + "`"
    if engine == "clickhouse":
        return "`" + ident.replace("\\", "\\\\").replace("`", "\\`") + "`"
    return '"' + ident.replace('"', '""') + '"'


def _text(engine: str, s: str) -> str:
    if engine in MYSQL_LIKE:
        return "'" + s.replace("\\", "\\\\").replace("'", "''").replace("\x00", "\\0") + "'"
    if engine == "clickhouse":
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\x00", "\\0") + "'"
    s = s.replace("'", "''")
    if engine == "postgresql":
        s = s.replace("\x00", "")  # PostgreSQL cannot store NUL in text
        return "'" + s + "'"
    if engine == "mssql":
        return "N'" + s + "'"
    return "'" + s + "'"


def _hex(engine: str, raw: bytes) -> str:
    h = raw.hex()
    if engine == "postgresql":
        return f"'\\x{h}'::bytea"
    if engine == "mssql":
        return "0x" + h if h else "0x"
    return f"X'{h}'"


def _pg_array(items: list[Any]) -> str:
    out: list[str] = []
    for v in items:
        if v is None:
            out.append("NULL")
        elif isinstance(v, list):
            out.append(_pg_array(v))
        elif isinstance(v, bool):
            out.append("true" if v else "false")
        elif isinstance(v, int | float | decimal.Decimal):
            out.append(str(v))
        else:
            out.append('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "{" + ",".join(out) + "}"


def literal(engine: str, value: Any, col_type: str = "") -> str:  # noqa: PLR0911, PLR0912
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE") if engine == "postgresql" else ("1" if value else "0")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "NULL" if math.isnan(value) or math.isinf(value) else repr(value)
    if isinstance(value, decimal.Decimal):
        return str(value) if value.is_finite() else "NULL"
    if isinstance(value, bytes | bytearray | memoryview):
        return _hex(engine, bytes(value))
    if isinstance(value, dt.datetime):
        return _text(engine, value.isoformat(sep=" "))
    if isinstance(value, dt.date | dt.time):
        return _text(engine, value.isoformat())
    if isinstance(value, list | tuple) and engine == "postgresql":
        t = col_type.lower()
        if t.endswith("[]") or t.startswith("_") or "array" in t:
            return _text(engine, _pg_array(list(value)))
    if isinstance(value, dict | list | tuple):
        import json

        return _text(engine, json.dumps(value, ensure_ascii=False, default=str))
    if isinstance(value, str):
        return _text(engine, value)
    if isinstance(value, uuid.UUID | ipaddress.IPv4Address | ipaddress.IPv6Address):
        return _text(engine, str(value))
    return _text(engine, str(value))


def table_ref(engine: str, schema: str | None, table: str) -> str:
    q = quote_ident(engine, table)
    return (
        f"{quote_ident(engine, schema)}.{q}" if schema and engine in ("postgresql", "mssql") else q
    )


def create_table_sql(engine: str, schema: str | None, st: TableStructure) -> str:
    lines: list[str] = []
    for c in st.columns:
        if c.is_generated:
            continue
        lines.append("  " + _column_sql(engine, c))
    if st.primary_key and engine != "clickhouse":
        cols = ", ".join(quote_ident(engine, k) for k in st.primary_key)
        lines.append(f"  PRIMARY KEY ({cols})")
    tail = " ENGINE = MergeTree ORDER BY tuple()" if engine == "clickhouse" else ""
    return (
        f"CREATE TABLE {table_ref(engine, schema, st.name)} (\n"
        + ",\n".join(lines)
        + f"\n){tail};\n"
    )


def _column_sql(engine: str, c: ColumnInfo) -> str:
    parts = [quote_ident(engine, c.name), c.data_type or "TEXT"]
    if c.is_identity:
        if engine in MYSQL_LIKE:
            parts.append("AUTO_INCREMENT")
        elif engine == "mssql":
            parts.append("IDENTITY(1,1)")
        elif engine == "postgresql":
            parts.append("GENERATED BY DEFAULT AS IDENTITY")
    if engine != "clickhouse" and not c.nullable:
        parts.append("NOT NULL")
    default = c.default
    if (
        default is not None
        and not c.is_identity
        and "nextval(" not in str(default).lower()
        and "\n" not in str(default)
    ):
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


_NUMBER = re.compile(r"-?[0-9]{1,80}(\.[0-9]{1,80})?([eE][+-]?[0-9]{1,4})?")


def _numeric_type(col_type: str) -> bool:
    t = col_type
    for wrapper in ("Nullable(", "LowCardinality("):
        while t.startswith(wrapper) and t.endswith(")"):
            t = t[len(wrapper) : -1]
    return t.startswith(("Int", "UInt", "Float", "Decimal"))


class SqlDump(Formatter):
    def __init__(  # noqa: PLR0913
        self,
        engine: str,
        schema: str | None,
        structure: TableStructure,
        columns: list[str],
        masked: frozenset[str],
        include_ddl: bool,
    ):
        if engine not in SQL_ENGINES:
            raise ValueError("SQL dumps are not available for this engine")
        self.engine, self.schema, self.st = engine, schema, structure
        self.columns, self.masked, self.include_ddl = columns, masked, include_ddl
        self.types = {c.name: c.data_type for c in structure.columns}
        self.ref = table_ref(engine, schema, structure.name)
        self.cols_sql = ", ".join(quote_ident(engine, c) for c in columns)
        self.identity = engine == "mssql" and any(
            c.is_identity and c.name in columns for c in structure.columns
        )

    def header(self) -> str:
        e = self.engine
        lines = [
            f"-- DB Wrench SQL dump of {comment_text(self.st.name)} ({e})",
            f"-- Generated {dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "-- Review before running: the DDL is best effort and recreates only columns,",
            "-- NOT NULL, defaults and the primary key (no indexes, foreign keys or triggers).",
        ]
        hidden = sorted(self.masked & set(self.columns))
        if hidden:
            lines.append(
                "-- Masked columns are written as NULL: "
                + ", ".join(comment_text(h) for h in hidden)
            )
        out = "\n".join(lines) + "\n\n"
        if self.include_ddl:
            out += create_table_sql(e, self.schema, self.st) + "\n"
        if self.identity:
            out += f"SET IDENTITY_INSERT {self.ref} ON;\n"
        return out

    def _value(self, name: str, value: Any) -> str:
        if name in self.masked:
            return "NULL"
        col_type = self.types.get(name, "")
        # ClickHouse sends 64-bit integers and decimals as JSON strings: keep them numbers.
        if (
            self.engine == "clickhouse"
            and isinstance(value, str)
            and _numeric_type(col_type)
            and _NUMBER.fullmatch(value)
        ):
            return value
        return literal(self.engine, value, col_type)

    def rows(self, batch: list[Any], first: bool) -> str:
        out: list[str] = []
        for i in range(0, len(batch), ROWS_PER_INSERT):
            chunk = batch[i : i + ROWS_PER_INSERT]
            values = ",\n".join(
                "  ("
                + ", ".join(self._value(n, v) for n, v in zip(self.columns, row, strict=True))
                + ")"
                for row in chunk
            )
            out.append(f"INSERT INTO {self.ref} ({self.cols_sql}) VALUES\n{values};\n")
        return "".join(out)

    def footer(self) -> str:
        return (
            f"SET IDENTITY_INSERT {self.ref} OFF;\n" if self.identity else ""
        ) + "-- end of dump\n"
