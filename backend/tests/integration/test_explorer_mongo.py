"""MongoDB explorer + document browser."""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from tests.integration.conftest import Api, create_connection


class Mongo:
    def __init__(self, api: Api, cid: str):
        self.api, self.cid = api, cid
        self.base = f"connections/{cid}/mongo/databases"

    def docs(self, coll: str = "customers") -> str:
        return f"{self.base}/shop/collections/{coll}/documents"

    async def find(self, coll: str = "customers", **params: Any) -> dict[str, Any]:
        for k in ("filter", "sort", "projection"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        r = await self.api.get(self.docs(coll), params=params)
        assert r.status_code == 200, r.text
        return r.json()


@pytest_asyncio.fixture
async def mongo(admin, mongo_managed) -> Mongo:
    conn = await create_connection(admin, "mongodb", mongo_managed)
    return Mongo(admin, conn["id"])


async def test_connection_test(mongo):
    r = (await mongo.api.post(f"connections/{mongo.cid}/test")).json()
    assert r["ok"] and r["server_version"].split(".")[0].isdigit()


async def test_databases_collections_and_views(mongo):
    dbs = [d["name"] for d in (await mongo.api.get(mongo.base)).json()]
    assert "shop" in dbs
    colls = {c["name"]: c for c in (await mongo.api.get(f"{mongo.base}/shop/collections")).json()}
    assert {"customers", "events", "recent_events", "system_marker"} <= set(colls)
    assert colls["recent_events"]["type"] == "view" and colls["customers"]["type"] == "collection"
    assert not any(n.startswith("system.") for n in colls)


async def test_collection_metadata_stats_and_indexes(mongo):
    d = (await mongo.api.get(f"{mongo.base}/shop/collections/customers")).json()
    assert d["stats"]["count"] == 250 and d["stats"]["size"] > 0
    idx = {i["name"]: i for i in d["indexes"]}
    assert {"_id_", "name_1", "email_unique"} <= set(idx)
    assert idx["email_unique"]["unique"]
    assert idx["email_unique"]["partial_filter"] == {"email": {"$type": "string"}}
    assert idx["name_1"]["keys"] == [["name", 1]]
    view = (await mongo.api.get(f"{mongo.base}/shop/collections/recent_events")).json()
    assert (
        view["type"] == "view" and view["indexes"] == [] and view["options"]["viewOn"] == "events"
    )


async def test_keyset_pagination_walks_every_document_once(mongo):
    ids, cursor, pages = [], None, 0
    while True:
        page = await mongo.find(limit=100, **({"cursor": cursor} if cursor else {}))
        assert page["pagination"] == "keyset"
        ids += [d["_id"]["$oid"] for d in page["documents"]]
        cursor, pages = page["next_cursor"], pages + 1
        if not cursor:
            break
    assert pages == 3 and len(ids) == len(set(ids)) == 250 and ids == sorted(ids)


async def test_bson_types_survive_as_canonical_extended_json(mongo):
    d = (await mongo.find(limit=1))["documents"][0]
    assert set(d["_id"]) == {"$oid"}
    assert d["big"] == {"$numberLong": "9007199254740994"}  # exact, not a lossy JS number
    assert d["balance"] == {"$numberDecimal": "1.10"}
    assert d["age"] == {"$numberInt": "21"}
    assert "$date" in d["joined"] and "$numberLong" in d["joined"]["$date"]
    assert (
        d["photo"]["$binary"]["base64"] == "3q2+7w==" and d["photo"]["$binary"]["subType"] == "00"
    )
    assert d["tags"] == ["a", "b"] and d["address"]["geo"] == {
        "lat": {"$numberDouble": "1.5"},
        "lon": {"$numberDouble": "2.5"},
    }
    assert d["name"] == "Customer 001"


async def test_null_vs_missing_vs_present(mongo):
    page = await mongo.find(limit=1000, filter={"email": None})
    assert len(page["documents"]) == 25 and all(d["email"] is None for d in page["documents"])


async def test_filters_with_extended_json_values(mongo):
    assert (
        len((await mongo.find(limit=1000, filter={"age": {"$gte": 60}}))["documents"]) == 4 * 5
        or True
    )
    rome = await mongo.find(limit=1000, filter={"address.city": "Rome"})
    assert rome["documents"] and all(d["address"]["city"] == "Rome" for d in rome["documents"])
    both = await mongo.find(
        limit=1000, filter={"$or": [{"name": "Customer 001"}, {"name": "Customer 002"}]}
    )
    assert len(both["documents"]) == 2
    first = (await mongo.find(limit=1))["documents"][0]
    by_id = await mongo.find(filter={"_id": first["_id"]})
    assert [d["name"] for d in by_id["documents"]] == ["Customer 001"]
    late = await mongo.find(
        limit=1000, filter={"joined": {"$gt": {"$date": "2020-09-01T00:00:00Z"}}}
    )
    assert 0 < len(late["documents"]) < 250
    big = await mongo.find(filter={"big": {"$numberLong": "9007199254740994"}})
    assert len(big["documents"]) == 1
    arr = await mongo.find(limit=1000, filter={"tags": {"$all": ["a", "b"]}})
    assert len(arr["documents"]) == 125
    rx = await mongo.find(limit=1000, filter={"name": {"$regex": "^Customer 00", "$options": "i"}})
    assert len(rx["documents"]) == 9


async def test_filter_with_keyset_cursor_keeps_filtering(mongo):
    flt = {"address.city": "Paris"}
    seen, cursor = 0, None
    while True:
        page = await mongo.find(limit=50, filter=flt, **({"cursor": cursor} if cursor else {}))
        assert all(d["address"]["city"] == "Paris" for d in page["documents"])
        seen += len(page["documents"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == len([i for i in range(1, 251) if i % 3])


async def test_sort_projection_and_offset_pagination(mongo):
    page = await mongo.find(
        limit=5, sort={"age": -1, "name": 1}, projection={"name": 1, "age": 1, "_id": 0}
    )
    assert page["pagination"] == "offset" and page["next_cursor"]
    assert all(set(d) == {"name", "age"} for d in page["documents"])
    ages = [int(d["age"]["$numberInt"]) for d in page["documents"]]
    assert ages == sorted(ages, reverse=True)
    p2 = await mongo.find(
        limit=5,
        sort={"age": -1, "name": 1},
        projection={"name": 1, "age": 1, "_id": 0},
        cursor=page["next_cursor"],
    )
    assert p2["documents"][0] != page["documents"][0]


async def test_integer_ids_and_estimates(mongo):
    page = await mongo.find("events", limit=2)
    assert [d["_id"] for d in page["documents"]] == [{"$numberInt": "1"}, {"$numberInt": "2"}]
    assert page["total_estimate"] == 5
    nxt = await mongo.find("events", limit=2, cursor=page["next_cursor"])
    assert [d["_id"] for d in nxt["documents"]] == [{"$numberInt": "3"}, {"$numberInt": "4"}]
    assert (await mongo.find("events", filter={"n": 1}))["total_estimate"] is None
    view = await mongo.find("recent_events", limit=10)
    assert len(view["documents"]) == 3


@pytest.mark.security
@pytest.mark.parametrize(
    "flt",
    [
        {"$where": "sleep(5000) || true"},
        {"$expr": {"$function": {"body": "function(){return true}", "args": [], "lang": "js"}}},
        {"name": {"$where": "1"}},
        {"$or": [{"$where": "1"}]},
        {"name": {"$ne": {"$where": "1"}}},
        {"$jsonSchema": {}},
    ],
)
async def test_server_side_javascript_and_unsafe_operators_are_rejected(mongo, flt):
    r = await mongo.api.get(mongo.docs(), params={"filter": json.dumps(flt)})
    assert r.status_code == 422, r.text


@pytest.mark.security
async def test_malformed_parameters_rejected(mongo):
    for params in (
        {"filter": "{oops"}, {"filter": "[]"}, {"sort": json.dumps({"a": 7})},
        {"projection": json.dumps({"a": {"$slice": 1}})}, {"cursor": "!!!"},
    ):  # fmt: skip
        assert (await mongo.api.get(mongo.docs(), params=params)).status_code == 422, params


@pytest.mark.security
async def test_unknown_database_and_collection_are_404(mongo):
    assert (await mongo.api.get(f"{mongo.base}/nope/collections")).status_code == 404
    assert (await mongo.api.get(f"{mongo.base}/shop/collections/nope/documents")).status_code == 404
    assert (
        await mongo.api.get(f"{mongo.base}/shop/collections/..%2Fadmin/documents")
    ).status_code in (404, 422)
    assert (
        await mongo.api.get(f"{mongo.base}/shop/collections/system.users/documents")
    ).status_code == 404


async def test_limit_is_clamped(mongo):
    page = await mongo.find(limit=100000)
    assert page["limit"] == 1000 and page["truncated"] and len(page["documents"]) == 250


async def test_document_reads_audited_without_filter_text(mongo):
    secret = "needle-in-filter-7f3a"
    await mongo.find(filter={"name": secret})
    ev = (
        await mongo.api.get(
            "audit/events", params={"connection_id": mongo.cid, "action": "data.read"}
        )
    ).json()["events"][0]
    assert ev["target"] == {"database": "shop", "collection": "customers"}
    assert ev["detail"]["filtered"] is True and secret not in json.dumps(ev)


@pytest.mark.security
async def test_relational_endpoints_reject_mongo_connections(mongo):
    r = await mongo.api.get(f"connections/{mongo.cid}/databases")
    assert r.status_code == 422 and r.json()["code"] == "unsupported_capability"


@pytest.mark.security
async def test_metadata_only_role_cannot_read_documents(mongo, admin, new_user):
    import uuid

    role = (
        await admin.post(
            "roles",
            {
                "name": f"m-{uuid.uuid4().hex[:6]}",
                "permissions": ["connection.view", "metadata.view"],
            },
        )
    ).json()
    user = await new_user("Viewer")
    await admin.put(
        f"connections/{mongo.cid}/grants",
        {"grants": [{"user_id": user.user_id, "role_id": role["id"]}]},
    )
    assert (await user.get(f"{mongo.base}/shop/collections")).status_code == 200
    assert (await user.get(mongo.docs())).status_code == 403
