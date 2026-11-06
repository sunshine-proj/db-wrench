"""Elasticsearch search-body validation (no server needed)."""

from __future__ import annotations

import pytest

from app.database.adapters.elasticsearch import flatten_mapping
from app.database.core.serialization import decode_opaque, encode_opaque
from app.database.search import dsl
from app.errors import StatementNotAllowed, ValidationFailed

OK = [
    {"query": {"match_all": {}}},
    {
        "query": {
            "bool": {"must": [{"match": {"name": "x"}}], "filter": [{"range": {"n": {"gte": 1}}}]}
        }
    },
    {"query": {"term": {"index": "logs-1"}}, "size": 5},  # a *field* called index is fine
    {"query": {"terms": {"city": ["Paris", "Rome"]}}},
    {"aggs": {"by_city": {"terms": {"field": "addr.city", "size": 20}}}, "size": 0},
    {
        "query": {"match_all": {}},
        "sort": [{"n": "desc"}],
        "_source": ["name"],
        "from": 10,
        "size": 10,
    },
    {"highlight": {"fields": {"name": {}}}},
]


@pytest.mark.parametrize("body", OK)
def test_ordinary_searches_pass(body):
    out = dsl.validate_search_body(body, max_size=1000)
    assert out["size"] == body.get("size", 10)


@pytest.mark.security
@pytest.mark.parametrize(
    "body",
    [
        {"query": {"script": {"script": {"source": "1"}}}},
        {"query": {"function_score": {"script_score": {"script": {"source": "1"}}}}},
        {"script_fields": {"x": {"script": "1"}}},
        {"aggs": {"a": {"scripted_metric": {"init_script": "x"}}}},
        {"aggs": {"a": {"bucket_script": {"buckets_path": {}, "script": "1"}}}},
        {"runtime_mappings": {"f": {"type": "keyword", "script": "emit('x')"}}},
        {"query": {"terms": {"user": {"index": "secrets", "id": "1", "path": "ids"}}}},
        {
            "query": {
                "more_like_this": {"fields": ["a"], "like": [{"_index": "secrets", "_id": "1"}]}
            }
        },
        {
            "query": {
                "geo_shape": {"loc": {"indexed_shape": {"index": "s", "id": "1", "path": "p"}}}
            }
        },
        {"query": {"wrapper": {"query": "eyJtYXRjaF9hbGwiOiB7fX0="}}},
        {"query": {"percolate": {"field": "q", "document": {}}}},
        {"pit": {"id": "abc"}},
        {"query": {"match_all": {}}, "stored_fields": ["x"]},
        {"query": {"match_all": {}}, "indices_boost": [{"other": 2}]},
        {"ext": {"x": 1}},
        {"query": {"match_all": {}}, "search_type": "dfs_query_then_fetch"},
    ],
)
def test_scripts_lookups_and_unknown_options_are_refused(body):
    with pytest.raises(StatementNotAllowed):
        dsl.validate_search_body(body, max_size=1000)


@pytest.mark.parametrize(
    "body",
    [
        {"size": 5000},
        {"size": -1},
        {"size": "10"},
        {"from": 9995, "size": 10},
        {"track_total_hits": 10_000_000},
        {"aggs": {"a": {"terms": {"field": "f", "size": 1_000_000}}}},
        "not an object",
        [],
    ],
)
def test_limits(body):
    with pytest.raises(ValidationFailed):
        dsl.validate_search_body(body, max_size=1000)


def test_depth_and_size_are_bounded():
    deep: dict = {"match_all": {}}
    for _ in range(40):
        deep = {"bool": {"must": [deep]}}
    with pytest.raises(ValidationFailed):
        dsl.validate_search_body({"query": deep}, max_size=10)
    with pytest.raises(ValidationFailed):
        dsl.validate_search_body({"query": {"match": {"a": "x" * 300_000}}}, max_size=10)


def test_total_hits_default_is_capped():
    assert dsl.validate_search_body({}, max_size=10)["track_total_hits"] == dsl.MAX_WINDOW


def test_mappings_cannot_carry_scripts():
    assert dsl.validate_mappings(None) == {}
    assert dsl.validate_mappings({"properties": {"a": {"type": "keyword"}}})
    with pytest.raises(StatementNotAllowed):
        dsl.validate_mappings({"runtime": {"f": {"type": "keyword", "script": "x"}}})
    with pytest.raises(ValidationFailed):
        dsl.validate_mappings([])


def test_mapping_is_flattened_with_subfields():
    fields = {
        f.path: f.type
        for f in flatten_mapping(
            {
                "name": {"type": "text", "fields": {"kw": {"type": "keyword"}}},
                "addr": {"properties": {"city": {"type": "keyword"}}},
            }  # fmt: skip
        )
    }
    assert fields == {
        "name": "text",
        "name.kw": "keyword",
        "addr": "object",
        "addr.city": "keyword",
    }


def test_opaque_ids_round_trip_any_document_id():
    for raw in (b"a/b", b"\xe2\x82\xac uro?&#", b"x" * 500):
        assert decode_opaque(encode_opaque(raw)) == raw
    for bad in ("", "!!!", "a" * 5000):
        with pytest.raises(ValidationFailed):
            decode_opaque(bad)
