import datetime as dt
import decimal
import uuid

import pytest

from app.database.core.base import Limits
from app.database.core.serialization import MAX_SAFE_INT, to_json_value
from app.database.relational.models import (
    ColumnInfo,
    FilterSpec,
    RowQuery,
    SortSpec,
    TableStructure,
)
from app.database.relational.sql_builder import (
    MySQLDialect,
    PostgresDialect,
    build_select,
    decode_cursor,
    encode_cursor,
    escape_like,
)
from app.errors import ValidationFailed

PG = PostgresDialect()
MY = MySQLDialect()
LIM = Limits()


def structure(pk=("id",), id_type="integer") -> TableStructure:
    cols = [
        ColumnInfo("id", id_type, False),
        ColumnInfo("name", "text", True),
        ColumnInfo("blob", "bytea", True),
        ColumnInfo("a%b", "text", True),
    ]
    return TableStructure("public", "t", "table", cols, list(pk), [], [], [])


def build(q: RowQuery, st=None, d=PG):
    return build_select(d, "public", st or structure(), q, LIM)


def test_default_is_keyset_on_primary_key():
    b = build(RowQuery())
    assert b.pagination == "keyset"
    assert 'ORDER BY "id" ASC' in b.sql and b.sql.endswith("LIMIT 101")
    assert b.params == []


def test_keyset_cursor_adds_row_comparison_with_bound_values():
    cursor = encode_cursor({"k": [10]})
    b = build(RowQuery(cursor=cursor))
    assert '"id" > %s' in b.sql and b.params == [10]


def test_composite_key_uses_tuple_comparison():
    b = build(RowQuery(cursor=encode_cursor({"k": [1, 2]})), structure(pk=("id", "name")))
    assert '("id", "name") > (%s, %s)' in b.sql and b.params == [1, 2]


def test_sort_switches_to_offset_with_pk_tiebreaker():
    b = build(RowQuery(sort=[SortSpec("name", "desc")], cursor=encode_cursor({"o": 200})))
    assert b.pagination == "offset"
    assert 'ORDER BY "name" DESC, "id" ASC' in b.sql and b.sql.endswith("OFFSET 200")


def test_no_primary_key_warns_about_unstable_order():
    b = build(RowQuery(), structure(pk=()))
    assert b.pagination == "unordered_offset" and b.warnings


def test_non_keyset_safe_pk_type_falls_back_to_offset():
    b = build(RowQuery(), structure(id_type="bytea"))
    assert b.pagination == "offset"


def test_hidden_pk_column_is_selected_for_the_cursor_only():
    b = build(RowQuery(columns=["name"]))
    assert b.visible_columns == ["name"] and b.hidden_pk_columns == ["id"]
    assert b.sql.startswith('SELECT "name", "id" FROM')


def test_page_size_is_clamped_with_warning():
    b = build(RowQuery(limit=10**6))
    assert b.limit == LIM.max_page_size and b.requested_limit_clamped and b.warnings


def test_offset_beyond_limit_rejected():
    with pytest.raises(ValidationFailed):
        build(RowQuery(sort=[SortSpec("name")], cursor=encode_cursor({"o": LIM.max_offset + 1})))


@pytest.mark.security
@pytest.mark.parametrize(
    "evil",
    ['id"; DROP TABLE t; --', "id` OR 1=1", "nonexistent", "id, name", "1", "*", "id\x00"],
)
def test_unknown_or_malicious_identifiers_never_reach_sql(evil):
    for q in (
        RowQuery(columns=[evil]),
        RowQuery(sort=[SortSpec(evil)]),
        RowQuery(filters=[FilterSpec(evil, "eq", 1)]),
        RowQuery(search="x", search_columns=[evil]),
    ):
        with pytest.raises(ValidationFailed):
            build(q)


@pytest.mark.security
def test_values_are_always_bound_never_interpolated():
    payload = "x'; DROP TABLE t; --"
    b = build(RowQuery(filters=[FilterSpec("name", "eq", payload)]))
    assert payload not in b.sql and b.params == [payload]
    b = build(RowQuery(filters=[FilterSpec("name", "in", [payload, "b"])]))
    assert payload not in b.sql and payload in b.params


@pytest.mark.security
def test_direction_and_operator_are_allowlisted():
    with pytest.raises(ValidationFailed):
        build(RowQuery(sort=[SortSpec("name", "asc; DROP")]))  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        build(RowQuery(filters=[FilterSpec("name", "= 1 OR 1=1 --", "x")]))


def test_like_wildcards_in_user_input_are_escaped():
    assert escape_like("50%_off\\") == "50\\%\\_off\\\\"
    b = build(RowQuery(filters=[FilterSpec("name", "contains", "50%")]))
    assert b.params == ["%50\\%%"] and "ILIKE" in b.sql
    assert "LIKE" in build(RowQuery(filters=[FilterSpec("name", "contains", "x")]), d=MY).sql


def test_percent_in_identifier_is_doubled_for_driver_placeholders():
    assert PG.quote("a%b") == '"a%%b"'
    b = build(RowQuery(columns=["a%b"]))
    assert '"a%%b"' in b.sql


def test_identifier_quoting_escapes_quote_chars():
    assert PG.quote('we"ird') == '"we""ird"'
    assert MY.quote("we`ird") == "`we``ird`"
    with pytest.raises(ValidationFailed):
        PG.quote("a\x00b")


def test_null_handling_requires_explicit_operators():
    with pytest.raises(ValidationFailed):
        build(RowQuery(filters=[FilterSpec("name", "eq", None)]))
    assert "IS NULL" in build(RowQuery(filters=[FilterSpec("name", "is_null")])).sql


def test_search_requires_columns_and_ors_them():
    with pytest.raises(ValidationFailed):
        build(RowQuery(search="x"))
    b = build(RowQuery(search="x", search_columns=["name", "a%b"]))
    assert " OR " in b.sql and len(b.params) == 2


def test_limits_on_filter_count_and_value_shape():
    with pytest.raises(ValidationFailed):
        build(RowQuery(filters=[FilterSpec("name", "eq", "x")] * 21))
    with pytest.raises(ValidationFailed):
        build(RowQuery(filters=[FilterSpec("name", "eq", {"$ne": 1})]))


@pytest.mark.parametrize(
    "bad", ["!!!", "e30", encode_cursor({"k": [1, 2]}), encode_cursor({"k": [None]})]
)
def test_bad_cursors_are_rejected(bad):
    with pytest.raises(ValidationFailed):
        build(RowQuery(cursor=bad))
    assert decode_cursor(encode_cursor({"o": 1})) == {"o": 1}


# ---- serialization ---------------------------------------------------------------------


def test_integers_beyond_js_safe_range_become_strings():
    assert to_json_value(MAX_SAFE_INT) == MAX_SAFE_INT
    assert to_json_value(MAX_SAFE_INT + 2) == str(MAX_SAFE_INT + 2)
    assert to_json_value(-(2**63)) == str(-(2**63))


def test_decimals_are_strings_preserving_precision():
    assert (
        to_json_value(decimal.Decimal("12345678901234567890.123456789"))
        == "12345678901234567890.123456789"
    )


def test_special_floats_and_temporal_types():
    assert to_json_value(float("nan")) == "nan" and to_json_value(1.5) == 1.5
    assert (
        to_json_value(dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC))
        == "2024-01-02T03:04:05+00:00"
    )
    assert to_json_value(dt.date(2024, 1, 2)) == "2024-01-02"
    u = uuid.uuid4()
    assert to_json_value(u) == str(u)


def test_binary_and_long_text_are_truncated_with_markers():
    b = to_json_value(b"\x00" * 1000)
    assert b["$binary"] and b["length"] == 1000 and b["truncated"]
    t = to_json_value("x" * 5000, max_chars=100)
    assert t["$truncated"] and len(t["preview"]) == 100 and t["length"] == 5000
    assert to_json_value("short", 100) == "short"


def test_nested_structures_recurse():
    assert to_json_value({"a": [decimal.Decimal("1.10"), None]}) == {"a": ["1.10", None]}
