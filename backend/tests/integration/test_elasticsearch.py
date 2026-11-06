"""Elasticsearch / OpenSearch (ADR-0022): indices, documents, editing, validated console."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.connections.network import NetworkPolicy
from app.database.adapters.elasticsearch import ElasticsearchAdapter
from app.database.core.base import ConnectionSpec, DbType, Limits
from app.database.core.serialization import encode_opaque
from app.errors import ConnectionFailed
from tests.integration.conftest import create_connection, es_request, grant

PREFIX = "connections"


def kid(doc_id: str) -> str:
    return encode_opaque(doc_id.encode())


class Es:
    def __init__(self, api: Any, cid: str, info: dict[str, Any]):
        self.api, self.cid, self.info = api, cid, info

    def url(self, path: str = "") -> str:
        return f"{PREFIX}/{self.cid}/search{path}"

    def raw(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        return es_request(self.info, method, path, body, **params)

    async def docs(self, index: str = "shop", **params: Any) -> dict[str, Any]:
        r = await self.api.get(self.url(f"/indices/{index}/documents"), params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def run(self, index: str, body: Any, **kw: Any):
        return await self.api.post(self.url("/execute"), {"index": index, "body": body, **kw})


@pytest.fixture
async def es(admin, es_managed) -> Es:
    c = await create_connection(admin, "elasticsearch", es_managed, read_only=False)
    return Es(admin, c["id"], es_managed)


@pytest.fixture
async def es_ro(admin, es_managed) -> Es:
    c = await create_connection(admin, "elasticsearch", es_managed)
    return Es(admin, c["id"], es_managed)


@pytest.fixture
def scratch(es_managed) -> str:
    name = f"scratch-{uuid.uuid4().hex[:8]}"
    es_request(
        es_managed,
        "PUT",
        f"/{name}",
        {"settings": {"number_of_shards": 1, "number_of_replicas": 0}},
    )
    yield name
    es_request(es_managed, "DELETE", f"/{name}")


# ------------------------------------------------------------------------ explorer


async def test_connection_and_capabilities(es):
    r = await es.api.post(f"{PREFIX}/{es.cid}/test", {})
    assert r.status_code == 200 and r.json()["server_version"].split()[0] in (
        "Elasticsearch",
        "OpenSearch",
    )
    caps = (await es.api.get(f"{PREFIX}/{es.cid}")).json()["capabilities"]
    assert caps["family"] == "search" and caps["engine"] == "elasticsearch"
    other = await es.api.get(f"{PREFIX}/{es.cid}/databases")
    assert other.status_code in (400, 409, 422) and "search" in other.text


async def test_indices_hide_system_indices(es):
    items = (await es.api.get(es.url("/indices"))).json()
    names = [i["name"] for i in items]
    assert "shop" in names and not [n for n in names if n.startswith(".")]
    shop = next(i for i in items if i["name"] == "shop")
    assert shop["docs"] == 250 and shop["primaries"] == 1


async def test_index_detail_lists_fields_and_settings(es):
    d = (await es.api.get(es.url("/indices/shop"))).json()
    fields = {f["path"]: f["type"] for f in d["fields"]}
    assert fields["name"] == "text" and fields["name.kw"] == "keyword"
    assert fields["addr.city"] == "keyword" and fields["big"] == "long"
    assert d["settings"]["number_of_shards"] == "1"
    assert (await es.api.get(es.url("/indices/nope"))).status_code == 404
    assert (await es.api.get(es.url("/indices/.security"))).status_code == 404
    assert (await es.api.get(es.url("/indices/shop*"))).status_code == 404  # no wildcards


# ------------------------------------------------------------------------ browser


async def test_paging_and_totals(es):
    first = await es.docs(limit=100)
    assert len(first["hits"]) == 100 and first["next_cursor"] == "100" and first["total"] == 250
    seen = [h["id"] for h in first["hits"]]
    cursor = first["next_cursor"]
    while cursor:
        page = await es.docs(limit=100, cursor=cursor)
        seen += [h["id"] for h in page["hits"]]
        cursor = page["next_cursor"]
    assert len(seen) == 250 == len(set(seen))


async def test_text_search_sorting_and_fidelity(es):
    hit = await es.docs(q="Customer 007")
    assert hit["total"] >= 1 and any(h["id"] == "7" for h in hit["hits"])
    asc = await es.docs(sort="n:desc", limit=3)
    assert [h["source"]["n"] for h in asc["hits"]] == [250, 249, 248]
    h = asc["hits"][0]
    assert h["source"]["big"] == "9007199254741243"  # beyond 2**53 stays exact
    assert h["seq_no"] is not None and h["primary_term"] >= 1
    assert (
        await es.api.get(es.url("/indices/shop/documents"), params={"sort": "nope:asc"})
    ).status_code == 422
    assert (
        await es.api.get(es.url("/indices/shop/documents"), params={"sort": "n:sideways"})
    ).status_code == 422
    deep = await es.api.get(
        es.url("/indices/shop/documents"), params={"cursor": "9999", "limit": 50}
    )
    assert deep.status_code == 422  # beyond the result window


async def test_get_document_by_opaque_id(es, es_managed, scratch):
    es_request(es_managed, "PUT", f"/{scratch}/_doc/a%2Fb%20c?refresh=true", {"x": "<b>bold</b>"})
    d = (await es.api.get(es.url(f"/indices/{scratch}/documents/{kid('a/b c')}"))).json()
    assert d["id"] == "a/b c" and d["source"] == {"x": "<b>bold</b>"}  # text, never HTML
    assert (
        await es.api.get(es.url(f"/indices/{scratch}/documents/{kid('nope')}"))
    ).status_code == 404
    assert (await es.api.get(es.url(f"/indices/{scratch}/documents/!!!"))).status_code == 422


# ------------------------------------------------------------------------ editor


async def test_create_replace_delete_with_native_concurrency(es, scratch):
    base = es.url(f"/indices/{scratch}/documents")
    r = await es.api.post(base, {"source": {"name": "a", "n": 1}})
    assert r.status_code == 201, r.text
    d = r.json()
    named = await es.api.post(base, {"id": "fixed", "source": {"name": "b"}})
    assert named.status_code == 201 and named.json()["id"] == "fixed"
    dup = await es.api.post(base, {"id": "fixed", "source": {"name": "again"}})
    assert dup.status_code == 409
    ok = await es.api.put(
        f"{base}/{d['key']}",
        {
            "source": {"name": "a2", "n": 2},
            "seq_no": d["seq_no"],
            "primary_term": d["primary_term"],
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["source"]["name"] == "a2" and ok.json()["seq_no"] > d["seq_no"]
    stale = await es.api.put(
        f"{base}/{d['key']}",
        {"source": {"name": "x"}, "seq_no": d["seq_no"], "primary_term": d["primary_term"]},
    )
    assert stale.status_code == 409
    cur = ok.json()
    gone = await es.api.request(
        "DELETE",
        f"{base}/{d['key']}",
        json={"seq_no": d["seq_no"], "primary_term": d["primary_term"]},
    )
    assert gone.status_code == 409
    done = await es.api.request(
        "DELETE",
        f"{base}/{d['key']}",
        json={"seq_no": cur["seq_no"], "primary_term": cur["primary_term"]},
    )
    assert done.status_code == 204
    assert (await es.api.get(f"{base}/{d['key']}")).status_code == 404


async def test_document_validation(es, scratch):
    base = es.url(f"/indices/{scratch}/documents")
    assert (await es.api.post(base, {"source": [1, 2]})).status_code == 422
    assert (await es.api.post(base, {"source": {"big": "x" * (1 << 20)}})).status_code == 422
    assert (await es.api.post(base, {"id": "a\u0000b", "source": {}})).status_code == 422
    assert (
        await es.api.put(f"{base}/{kid('x')}", {"source": {}, "seq_no": 0, "primary_term": 0})
    ).status_code == 422


async def test_production_and_read_only(admin, es_managed, es_ro, scratch):
    c = await create_connection(
        admin, "elasticsearch", es_managed, read_only=False, environment="production"
    )
    prod = Es(admin, c["id"], es_managed)
    es_request(es_managed, "PUT", f"/{scratch}/_doc/p1?refresh=true", {"a": 1})
    path = prod.url(f"/indices/{scratch}/documents/{kid('p1')}")
    r = await admin.request("DELETE", path, json={})
    assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert (await admin.request("DELETE", path, json={"confirm": scratch})).status_code == 204
    base = es_ro.url(f"/indices/{scratch}/documents")
    assert (await es_ro.api.post(base, {"source": {"a": 1}})).status_code == 403
    assert (
        await es_ro.api.put(f"{base}/{kid('x')}", {"source": {}, "seq_no": 0, "primary_term": 1})
    ).status_code == 403
    assert (await es_ro.api.request("DELETE", f"{base}/{kid('x')}", json={})).status_code == 403
    assert (await es_ro.api.post(es_ro.url("/indices"), {"name": "nope-idx"})).status_code == 403


# ------------------------------------------------------------------------ console


async def test_console_search_and_aggregations(es):
    r = await es.run(
        "shop", {"query": {"term": {"addr.city": "Rome"}}, "size": 2, "_source": ["n"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()["body"]
    assert body["hits"]["total"]["value"] == 83 and len(body["hits"]["hits"]) == 2
    agg = await es.run("shop", {"size": 0, "aggs": {"c": {"terms": {"field": "addr.city"}}}})
    assert {b["key"] for b in agg.json()["body"]["aggregations"]["c"]["buckets"]} == {
        "Paris",
        "Rome",
    }
    bad = await es.run("shop", {"query": {"nonsense_query": {}}})
    assert bad.status_code == 400 and bad.json()["code"] == "query_failed"
    assert (await es.run("nope", {"query": {"match_all": {}}})).status_code == 404
    assert (await es.run("shop*", {"query": {"match_all": {}}})).status_code == 404


@pytest.mark.security
@pytest.mark.parametrize(
    "body",
    [
        {"query": {"script": {"script": {"source": "true"}}}},
        {"script_fields": {"x": {"script": {"source": "1"}}}},
        {"query": {"terms": {"v": {"index": "secret_sink", "id": "1", "path": "v"}}}},
        {
            "query": {
                "more_like_this": {"fields": ["v"], "like": [{"_index": "secret_sink", "_id": "1"}]}
            }
        },
        {"query": {"wrapper": {"query": "eyJtYXRjaF9hbGwiOiB7fX0="}}},
        {"runtime_mappings": {"f": {"type": "keyword", "script": {"source": "emit('x')"}}}},
        {"pit": {"id": "x"}},
        {"size": 100000},
    ],
)
async def test_dangerous_search_bodies_never_reach_the_cluster(es, body):
    r = await es.run("shop", body)
    assert r.status_code in (400, 422), (body, r.status_code, r.text)
    assert es.raw("GET", "/secret_sink/_doc/1")["_source"] == {"v": "must-not-change"}


@pytest.mark.security
async def test_the_console_cannot_write_or_reach_another_index(es):
    # `_search` is the only endpoint the console can call: these are not search options.
    for body in (
        {"query": {"match_all": {}}, "script": {"source": "ctx._source.v = 'pwned'"}},
        {"delete": {"_index": "secret_sink", "_id": "1"}},
        {"index": {"_index": "secret_sink"}},
    ):
        r = await es.run("shop", body)
        assert r.status_code in (400, 422), body
    assert es.raw("GET", "/secret_sink/_doc/1")["_source"] == {"v": "must-not-change"}
    r = await es.run("secret_sink", {"query": {"match_all": {}}})
    assert r.status_code == 200  # reading another listed index is the account's own business...
    assert r.json()["body"]["hits"]["total"]["value"] == 1  # ...and is limited to that one index


# ------------------------------------------------------------------------ administration


async def test_create_and_delete_index(es):
    name = f"idx-{uuid.uuid4().hex[:8]}"
    r = await es.api.post(
        es.url("/indices"),
        {
            "name": name,
            "shards": 1,
            "replicas": 0,
            "mappings": {"properties": {"t": {"type": "keyword"}}},
        },
    )
    assert r.status_code == 201, r.text
    assert name in [i["name"] for i in (await es.api.get(es.url("/indices"))).json()]
    d = (await es.api.get(es.url(f"/indices/{name}"))).json()
    assert {f["path"]: f["type"] for f in d["fields"]} == {"t": "keyword"}
    assert (await es.api.post(es.url("/indices"), {"name": name})).status_code == 409
    for bad in ("UPPER", ".hidden", "a b", "../x", "_x", "x" * 300):
        assert (await es.api.post(es.url("/indices"), {"name": bad})).status_code == 422, bad
    scripted = await es.api.post(
        es.url("/indices"),
        {"name": f"idx-{uuid.uuid4().hex[:8]}", "mappings": {"runtime": {"f": {"script": "x"}}}},
    )
    assert scripted.status_code in (400, 422)
    for body in ({}, {"confirm": "nope"}):
        assert (await es.api.delete(es.url(f"/indices/{name}"), json=body)).status_code == 428
    assert (
        await es.api.delete(es.url(f"/indices/{name}"), json={"confirm": name})
    ).status_code == 204
    assert name not in [i["name"] for i in (await es.api.get(es.url("/indices"))).json()]
    assert (
        await es.api.delete(es.url("/indices/shop*"), json={"confirm": "shop*"})
    ).status_code == 404


async def test_roles_gate_every_operation(admin, new_user, es, scratch):
    viewer, editor = await new_user("Viewer"), await new_user("Editor")
    await grant(admin, es.cid, viewer, "Viewer")
    await grant(admin, es.cid, editor, "Editor")
    outsider = await new_user("Viewer")
    assert (await outsider.get(es.url("/indices"))).status_code == 404
    assert (await viewer.get(es.url("/indices/shop/documents"))).status_code == 200
    base = es.url(f"/indices/{scratch}/documents")
    assert (await viewer.post(base, {"source": {"a": 1}})).status_code == 403
    assert (await editor.post(base, {"source": {"a": 1}})).status_code == 201
    assert (await editor.post(es.url("/indices"), {"name": "nope-idx"})).status_code == 403


# ------------------------------------------------------------------------ adapter level


def spec(info: dict[str, Any], **over: Any) -> ConnectionSpec:
    return ConnectionSpec(
        id=uuid.uuid4(), db_type=DbType.ELASTICSEARCH, host=info["host"], port=info["port"],
        database=None, username=over.pop("username", info["username"]),
        password=over.pop("password", info["password"]), pool_max=2, **over,
    )  # fmt: skip


@pytest.mark.security
def test_wrong_credentials_do_not_leak_the_password(es_managed):
    if not es_managed["username"]:
        pytest.skip("security is disabled on this cluster")
    bad = "Wr0ng-Credential-Value"
    a = ElasticsearchAdapter(
        spec(es_managed, password=bad), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "connection_failed"
        assert bad not in (res.error or "")
    finally:
        a.close()


@pytest.mark.security
def test_network_policy_is_enforced(es_managed):
    strict = NetworkPolicy(allow_cidrs=("10.0.0.0/8",), allow_loopback=False)
    a = ElasticsearchAdapter(spec(es_managed), strict, Limits())
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "network_policy_denied"
    finally:
        a.close()


def test_unreachable_cluster_is_a_typed_failure(es_managed):
    a = ElasticsearchAdapter(
        spec({**es_managed, "port": 1}), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        with pytest.raises(ConnectionFailed):
            a.list_indices()
    finally:
        a.close()
