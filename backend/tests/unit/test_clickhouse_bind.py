"""ClickHouse parameter binding: values never reach the SQL text."""

from __future__ import annotations

import decimal
import uuid

import pytest

from app.database.adapters.clickhouse import bind
from app.database.relational.sql_builder import ClickHouseDialect
from app.errors import ValidationFailed


def test_values_become_typed_server_side_parameters():
    sql, p = bind(
        "SELECT * FROM t WHERE a = %s AND b = %s AND c = %s", [1, "x'; DROP TABLE t;--", 1.5]
    )
    assert sql == "SELECT * FROM t WHERE a = {p0:Int64} AND b = {p1:String} AND c = {p2:Float64}"
    assert p == {"param_p0": "1", "param_p1": "x'; DROP TABLE t;--", "param_p2": "1.5"}
    assert "DROP" not in sql


def test_none_is_inline_null_and_percent_pairs_collapse():
    sql, p = bind("SELECT `a%%b` FROM t WHERE x IS %s OR y = %s", [None, 2])
    assert sql == "SELECT `a%b` FROM t WHERE x IS NULL OR y = {p0:Int64}" and p == {"param_p0": "2"}


@pytest.mark.parametrize(
    ("value", "typ"),
    [(True, "UInt8"), (2**63, "UInt64"), (2**64, "String"), (-5, "Int64"),
     (decimal.Decimal("1.50"), "String"), (uuid.UUID(int=1), "String")],
)  # fmt: skip
def test_types(value, typ):
    sql, _ = bind("%s", [value])
    assert sql == f"{{p0:{typ}}}"


@pytest.mark.parametrize("bad", [b"bytes", {"a": 1}, ["x"], "a\x00b"])
def test_unsupported_values_are_rejected(bad):
    with pytest.raises(ValidationFailed):
        bind("%s", [bad])


def test_placeholder_count_must_match():
    with pytest.raises(ValidationFailed):
        bind("%s %s", [1])
    with pytest.raises(ValidationFailed):
        bind("%s", [1, 2])


def test_identifier_quoting():
    d = ClickHouseDialect()
    assert d.quote("a`b") == "`a\\`b`" and d.quote("c\\d") == "`c\\\\d`"
    assert d.qualified("db", "t") == "`db`.`t`"
    with pytest.raises(ValidationFailed):
        d.quote("")
