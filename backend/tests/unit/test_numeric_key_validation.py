"""Numeric columns only accept numeric key/expected values (MariaDB coerces '1 OR 1=1' to 1)."""

from __future__ import annotations

import pytest

from app.database.adapters.mariadb import MariaDBAdapter
from app.database.relational.models import ColumnInfo


def col(data_type: str) -> ColumnInfo:
    return ColumnInfo("c", data_type, False)


def check(_unused, c, v):
    # only class attributes are used, so the class itself can stand in for the instance
    return MariaDBAdapter._typed_scalar_ok(MariaDBAdapter, c, v)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "t", ["int", "int(11) unsigned", "bigint", "integer", "smallint", "serial"]
)
@pytest.mark.parametrize("v", [1, -5, "7", "-12", "9007199254740993"])
def test_integer_columns_accept_integers(t, v):
    assert check(None, col(t), v)


@pytest.mark.parametrize("t", ["int", "bigint unsigned", "tinyint(1)"])
@pytest.mark.parametrize("v", ["1 OR 1=1", "1abc", "", " 1", "1.5", "0x10", True, ["1"], "1e3"])
def test_integer_columns_reject_other_text(t, v):
    assert not check(None, col(t), v)


@pytest.mark.parametrize("v", ["1.50", "-3", "2e5", 1.5, 3])
def test_decimal_columns_accept_numbers(v):
    assert check(None, col("decimal(12,2)"), v)


@pytest.mark.parametrize("v", ["1.5.5", "1 OR 1", "NaN", "abc"])
def test_decimal_columns_reject_other_text(v):
    assert not check(None, col("numeric(10,2)"), v)


@pytest.mark.parametrize(
    "t", ["varchar(10)", "text", "uuid", "enum('print','x')", "int4range", "interval", "multipoint"]
)
def test_non_numeric_columns_are_not_restricted(t):
    assert check(None, col(t), "anything at all")
    assert check(None, col(t), None)
