"""SQL dump formatting: escaping per dialect, comment safety, masking (no server needed)."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest

from app.database.relational.models import ColumnInfo, TableStructure
from app.editor.sqldump import (
    SqlDump,
    comment_text,
    create_table_sql,
    literal,
    quote_ident,
    table_ref,
)


def st(name="t", cols=None, pk=("id",)):
    cols = cols or [
        ColumnInfo("id", "integer", False, None, True, False, None, 1),
        ColumnInfo("name", "text", True, "'x'", False, False, None, 2),
        ColumnInfo("gen", "integer", True, None, False, True, None, 3),
    ]
    return TableStructure(None, name, "table", cols, list(pk), [], [], [])


@pytest.mark.parametrize(
    ("engine", "value", "expected"),
    [
        ("postgresql", "it's", "'it''s'"),
        ("postgresql", "a\\b", "'a\\b'"),
        ("mysql", "a\\b'c", "'a\\\\b''c'"),
        ("mariadb", "x\x00y", "'x\\0y'"),
        ("clickhouse", "a'b\\", "'a\\'b\\\\'"),
        ("mssql", "it's", "N'it''s'"),
        ("sqlite", "it's", "'it''s'"),
        ("sqlite", "'; DROP TABLE t; --", "'''; DROP TABLE t; --'"),
    ],
)
def test_text_is_escaped_for_the_dialect(engine, value, expected):
    assert literal(engine, value) == expected


def test_scalars():
    assert literal("postgresql", True) == "TRUE" and literal("mysql", True) == "1"
    assert literal("mssql", None) == "NULL"
    assert literal("sqlite", 42) == "42" and literal("sqlite", 2**70) == str(2**70)
    assert literal("mysql", decimal.Decimal("1.50")) == "1.50"
    assert literal("mysql", float("nan")) == "NULL" and literal("mysql", float("inf")) == "NULL"
    assert literal("mysql", decimal.Decimal("NaN")) == "NULL"
    assert literal("mysql", dt.datetime(2024, 1, 2, 3, 4, 5)) == "'2024-01-02 03:04:05'"
    assert literal("mysql", dt.date(2024, 1, 2)) == "'2024-01-02'"
    assert literal("sqlite", uuid.UUID(int=1)).startswith("'00000000")


def test_binary_literals():
    raw = b"\xde\xad\x00"
    assert literal("postgresql", raw) == "'\\xdead00'::bytea"
    assert literal("mysql", raw) == "X'dead00'" and literal("sqlite", raw) == "X'dead00'"
    assert literal("mssql", raw) == "0xdead00" and literal("mssql", b"") == "0x"


def test_json_and_pg_arrays():
    assert literal("mysql", {"a": [1, "x'y"]}) == "'{\"a\": [1, \"x''y\"]}'"
    assert literal("postgresql", ["a", None, 'q"r', 1], "text[]") == '\'{"a",NULL,"q\\"r",1}\''
    assert literal("postgresql", [[1, 2], [3, 4]], "_int4") == "'{{1,2},{3,4}}'"
    assert literal("postgresql", [1, 2], "jsonb") == "'[1, 2]'"


@pytest.mark.security
def test_hostile_names_cannot_break_out_of_comments_or_quotes():
    assert comment_text("a\nDROP TABLE x;--") == "a DROP TABLE x;--"  # one line, no newline
    assert "\n" not in comment_text("x\r\ny z")
    s = st(name="t\nDROP TABLE users; --")
    dump = SqlDump("postgresql", "public", s, ["id"], frozenset(), True)
    head = dump.header()
    comment_block = head.split("\n\n", 1)[0]
    assert all(line.startswith("--") for line in comment_block.splitlines())
    assert "DROP TABLE users" in comment_block  # visible, but on one commented line
    assert '"t\nDROP TABLE users; --"' in head  # only inside the quoted identifier
    assert quote_ident("postgresql", 'a"b') == '"a""b"'
    assert quote_ident("mysql", "a`b") == "`a``b`"
    assert quote_ident("mssql", "a]b") == "[a]]b]"
    assert quote_ident("clickhouse", "a`b\\") == "`a\\`b\\\\`"
    with pytest.raises(ValueError):
        quote_ident("sqlite", "a\x00b")


def test_masked_values_never_reach_the_dump():
    d = SqlDump("mysql", None, st(), ["id", "name"], frozenset({"name"}), False)
    out = d.header() + d.rows([(1, "secret"), (2, None)], True)
    assert "secret" not in out and "Masked columns are written as NULL: name" in out
    assert "(1, NULL)" in out and "(2, NULL)" in out


def test_inserts_are_batched_and_column_listed():
    d = SqlDump("sqlite", None, st(), ["id", "name"], frozenset(), False)
    out = d.rows([(i, f"n{i}") for i in range(250)], True)
    assert out.count("INSERT INTO") == 3 and out.count("(0, 'n0')") == 1
    assert 'INSERT INTO "t" ("id", "name") VALUES' in out


def test_ddl_skips_generated_columns_and_serial_defaults():
    cols = [
        ColumnInfo("id", "integer", False, "nextval('t_id_seq'::regclass)", False, False, None, 1),
        ColumnInfo(
            "name", "character varying(10)", False, "'x'::character varying", False, False, None, 2
        ),
        ColumnInfo("gen", "integer", True, None, False, True, None, 3),
    ]
    sql = create_table_sql("postgresql", "public", st(cols=cols))
    assert "nextval" not in sql and '"gen"' not in sql
    assert """"name" character varying(10) NOT NULL DEFAULT 'x'::character varying""" in sql
    assert 'PRIMARY KEY ("id")' in sql and sql.startswith('CREATE TABLE "public"."t"')


def test_engine_specific_ddl():
    ident = [ColumnInfo("id", "int", False, None, True, False, None, 1)]
    assert "AUTO_INCREMENT" in create_table_sql("mysql", None, st(cols=ident))
    assert "IDENTITY(1,1)" in create_table_sql("mssql", "dbo", st(cols=ident))
    assert "GENERATED BY DEFAULT AS IDENTITY" in create_table_sql(
        "postgresql", "public", st(cols=ident)
    )
    ch = create_table_sql("clickhouse", None, st(cols=ident))
    assert "ENGINE = MergeTree" in ch and "PRIMARY KEY" not in ch and "NOT NULL" not in ch
    assert table_ref("mysql", "ignored", "t") == "`t`"
    assert table_ref("mssql", "dbo", "t") == "[dbo].[t]"


def test_mssql_identity_insert_is_wrapped():
    ident = [ColumnInfo("id", "int", False, None, True, False, None, 1)]
    d = SqlDump("mssql", "dbo", st(cols=ident), ["id"], frozenset(), False)
    assert "SET IDENTITY_INSERT [dbo].[t] ON;" in d.header()
    assert "SET IDENTITY_INSERT [dbo].[t] OFF;" in d.footer()


def test_unsupported_engines_are_rejected():
    with pytest.raises(ValueError):
        SqlDump("mongodb", None, st(), ["id"], frozenset(), False)


def test_clickhouse_numbers_sent_as_json_strings_stay_numbers():
    cols = [
        ColumnInfo("id", "UInt64", False, None, False, False, None, 1),
        ColumnInfo("amount", "Nullable(Decimal(10, 2))", True, None, False, False, None, 2),
        ColumnInfo("name", "String", False, None, False, False, None, 3),
    ]
    d = SqlDump(
        "clickhouse", None, st(cols=cols, pk=()), ["id", "amount", "name"], frozenset(), False
    )
    out = d.rows([("18446744073709551615", "-10.25", "7"), ("1", None, "1e5")], True)
    assert "(18446744073709551615, -10.25, '7')" in out and "(1, NULL, '1e5')" in out
    evil = d.rows([("1; DROP TABLE x", "1", "a")], True)
    assert "'1; DROP TABLE x'" in evil  # a numeric column never lets non-numbers through raw
