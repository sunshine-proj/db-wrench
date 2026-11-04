import datetime as dt
import json
import re

import pytest
from bson import Decimal128, Int64, ObjectId

from app.database.document.filters import (
    decode_id_cursor,
    decode_offset_cursor,
    encode_id_cursor,
    encode_offset_cursor,
    validate_filter,
    validate_projection,
    validate_sort,
)
from app.errors import ValidationFailed


def ok(doc) -> dict:
    return validate_filter(json.dumps(doc))


def bad(doc) -> None:
    with pytest.raises(ValidationFailed):
        validate_filter(doc if isinstance(doc, str) else json.dumps(doc))


def test_empty_filter():
    assert validate_filter(None) == {} and validate_filter("  ") == {}


def test_plain_and_operator_filters():
    f = ok({"age": {"$gte": 18, "$lt": 65}, "$or": [{"name": "a"}, {"tags": {"$in": ["x", "y"]}}]})
    assert f["age"] == {"$gte": 18, "$lt": 65}
    assert f["$or"][1] == {"tags": {"$in": ["x", "y"]}}


def test_extended_json_values_become_bson_types():
    oid = "507f1f77bcf86cd799439011"
    f = ok(
        {
            "_id": {"$oid": oid},
            "created": {"$gt": {"$date": "2024-01-01T00:00:00Z"}},
            "n": {"$numberLong": "9007199254740993"},
            "d": {"$numberDecimal": "1.10"},
        }
    )
    assert f["_id"] == ObjectId(oid)
    assert isinstance(f["created"]["$gt"], dt.datetime)
    assert f["n"] == Int64(9007199254740993)
    assert isinstance(f["d"], Decimal128)


def test_canonical_nested_date_wrapper_is_accepted():
    f = ok({"created": {"$date": {"$numberLong": "1704067200000"}}})
    assert isinstance(f["created"], dt.datetime)


@pytest.mark.security
@pytest.mark.parametrize(
    "doc",
    [
        {"$where": "sleep(1000)"},
        {"$expr": {"$function": {"body": "function(){}", "args": [], "lang": "js"}}},
        {"$expr": {"$gt": ["$a", "$b"]}},
        {"a": {"$where": "1"}},
        {"a": {"$function": {}}},
        {"a": {"$accumulator": {}}},
        {"$jsonSchema": {}},
        {"$text": {"$search": "x"}},
        {"a": {"$near": [0, 0]}},
        {"a": {"$code": "function(){}"}},
        {"a": {"$ne": {"$where": "1"}}},
        {"a": {"$in": [{"$where": "1"}]}},
        {"$or": [{"$where": "1"}]},
        {"$and": []},
        {"$or": "x"},
        {"a": {"$not": {"$where": "1"}}},
        {"a": {"$elemMatch": {"$where": "1"}}},
        {"a": {"$eq": {"nested": {"$where": "1"}}}},
        {"$set": {"a": 1}},
        {"$$ROOT": 1},
    ],
)
def test_dangerous_operators_are_rejected(doc):
    bad(doc)


@pytest.mark.security
def test_unknown_operators_rejected_everywhere():
    bad({"a": {"$bogus": 1}})
    bad({"$bogus": [{"a": 1}]})


@pytest.mark.security
def test_operator_argument_shapes_enforced():
    bad({"a": {"$exists": "yes"}})
    bad({"a": {"$in": "notalist"}})
    bad({"a": {"$size": -1}})
    bad({"a": {"$mod": [1]}})
    bad({"a": {"$options": "z"}})
    bad({"a": {"$regex": "x" * 300}})
    bad({"a": {"$regex": 5}})


def test_regex_with_options_allowed():
    rx = ok({"a": {"$regex": "^ab", "$options": "i"}})["a"]  # converted to a BSON Regex
    assert rx.pattern == "^ab" and rx.flags & re.IGNORECASE


@pytest.mark.security
def test_field_names_cannot_start_with_dollar_or_contain_nul():
    bad({"$a": 1})
    bad({"a.$b": 1})
    bad({"a\u0000b": 1})
    bad({"": 1})


@pytest.mark.security
def test_complexity_limits():
    deep: dict = {"a": 1}
    for _ in range(40):
        deep = {"$and": [deep]}
    bad(deep)
    bad({"a": {"$in": list(range(1001))}})
    bad({"$or": [{"a": i} for i in range(101)]})
    bad("x" * (70 * 1024))


def test_invalid_json_and_non_object():
    bad("{not json")
    bad("[1,2]")


def test_sort_and_projection_validation():
    assert validate_sort('{"name": 1, "age": -1}') == [("name", 1), ("age", -1)]
    for s in (
        '{"a": 2}',
        '{"$natural": 1}',
        '{"a": true}',
        "[1]",
        json.dumps({f"f{i}": 1 for i in range(6)}),
    ):
        with pytest.raises(ValidationFailed):
            validate_sort(s)
    assert validate_projection('{"a": 1, "_id": 0}') == {"a": 1, "_id": 0}
    for p in ('{"a": 1, "b": 0}', '{"a": {"$slice": 1}}', "{}", '{"$a": 1}'):
        with pytest.raises(ValidationFailed):
            validate_projection(p)


def test_id_cursor_roundtrip_preserves_bson_types():
    for value in (ObjectId(), 42, "str", Int64(2**60)):
        assert decode_id_cursor(encode_id_cursor(value)) == value


@pytest.mark.security
def test_id_cursor_cannot_smuggle_operators():
    import base64

    evil = base64.urlsafe_b64encode(json.dumps({"k": {"$where": "1"}}).encode()).decode()
    with pytest.raises(ValidationFailed):
        decode_id_cursor(evil)
    with pytest.raises(ValidationFailed):
        decode_id_cursor("%%%")


def test_offset_cursor_bounds():
    assert decode_offset_cursor(encode_offset_cursor(50), 100) == 50
    with pytest.raises(ValidationFailed):
        decode_offset_cursor(encode_offset_cursor(101), 100)
