"""Phase 5: approvals for bulk data changes and for MongoDB $out / $merge write stages."""

from __future__ import annotations

import uuid

import pymongo
import pytest

from tests.integration.conftest import create_connection, grant
from tests.integration.test_admin import mongo_client


def sel_qty_ge(n: int) -> dict:
    return {"filters": [{"column": "qty", "op": "gte", "value": n}]}


async def two_dbas(admin, new_user, cid):
    requester, reviewer = await new_user("Database Admin"), await new_user("Database Admin")
    await grant(admin, cid, requester, "Database Admin")
    await grant(admin, cid, reviewer, "Database Admin")
    return requester, reviewer


async def test_bulk_delete_runs_only_with_an_approval(wr, admin, new_user):
    requester, reviewer = await two_dbas(admin, new_user, wr.cid)
    r = await admin.put(
        "governance/policies", {"connection_id": wr.cid, "operations": ["data.bulk_delete"]}
    )
    assert r.status_code == 200, r.text

    async def preview():
        p = await requester.post(
            f"{wr.base()}/rows/bulk/preview",
            {"operation": "delete", "selector": sel_qty_ge(4)},
        )
        assert p.status_code == 200, p.text
        return p.json()

    def body(pv):
        return {"selector": sel_qty_ge(4), "token": pv["token"], "expected_count": pv["count"]}

    pv = await preview()
    r = await requester.post(f"{wr.base()}/rows/bulk/delete", body(pv))
    assert r.status_code == 428 and r.json()["code"] == "approval_required", r.text
    aid = r.json()["approval_id"]
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5

    pending = (await reviewer.get("approvals", params={"scope": "review"})).json()
    mine = next(a for a in pending if a["id"] == aid)
    assert mine["operation"] == "data.bulk_delete" and mine["summary"]["rows"] == 2
    assert (await reviewer.post(f"approvals/{aid}/approve", {})).status_code == 200

    # a different selection cannot reuse the approval
    other = await requester.post(
        f"{wr.base()}/rows/bulk/preview", {"operation": "delete", "selector": sel_qty_ge(1)}
    )
    ob = other.json()
    r = await requester.post(
        f"{wr.base()}/rows/bulk/delete",
        {"selector": sel_qty_ge(1), "token": ob["token"], "expected_count": ob["count"]},
        headers={"Approval-Id": aid},
    )
    assert r.status_code == 422
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5

    pv = await preview()
    r = await requester.post(
        f"{wr.base()}/rows/bulk/delete", body(pv), headers={"Approval-Id": aid}
    )
    assert r.status_code == 200 and r.json()["affected"] == 2, r.text
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 3

    pv = await preview()
    again = await requester.post(
        f"{wr.base()}/rows/bulk/delete", body(pv), headers={"Approval-Id": aid}
    )
    assert again.status_code in (409, 422)  # single use (no rows left, or already consumed)


async def test_bulk_update_without_policy_is_unaffected(wr, admin):
    p = await admin.post(
        f"{wr.base()}/rows/bulk/preview",
        {"operation": "update", "selector": sel_qty_ge(4), "values": {"note": "x"}},
    )
    pv = p.json()
    r = await admin.post(
        f"{wr.base()}/rows/bulk/update",
        {
            "selector": sel_qty_ge(4),
            "values": {"note": "x"},
            "token": pv["token"],
            "expected_count": pv["count"],
        },
    )
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------------- MongoDB


@pytest.fixture
def scratch_db(mongo_managed):
    name = f"wr_{uuid.uuid4().hex[:8]}"
    c = mongo_client(mongo_managed)
    c[name].src.insert_many([{"_id": i, "n": i, "g": i % 2} for i in range(1, 7)])
    c[name].dst.insert_one({"_id": 1, "n": 100})
    yield name, c
    c.drop_database(name)
    c.close()


async def mexec(api, cid, db, pipeline, headers=None, **extra):
    return await api.post(
        f"connections/{cid}/mongo/queries/execute",
        {"database": db, "collection": "src", "kind": "aggregate", "pipeline": pipeline, **extra},
        headers=headers or {},
    )


async def test_out_stage_needs_write_permission_and_a_second_person(
    admin, new_user, mongo_managed, scratch_db
):
    db, c = scratch_db
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    cid = conn["id"]
    requester, reviewer = await two_dbas(admin, new_user, cid)
    viewer = await new_user("Viewer")
    await grant(admin, cid, viewer, "Viewer")

    pipe = [{"$match": {"g": 1}}, {"$out": "evens_odds"}]
    assert (await mexec(viewer, cid, db, pipe)).status_code == 403  # no query.execute_write

    r = await mexec(requester, cid, db, pipe)
    assert r.status_code == 428 and r.json()["code"] == "approval_required", r.text
    aid = r.json()["approval_id"]
    assert "evens_odds" not in c[db].list_collection_names()

    assert (await mexec(requester, cid, db, pipe, {"Approval-Id": aid})).status_code == 409
    assert (await reviewer.post(f"approvals/{aid}/approve", {})).status_code == 200
    # the approval covers exactly this pipeline
    changed = await mexec(requester, cid, db, [{"$match": {"g": 0}}, {"$out": "evens_odds"}],
                          {"Approval-Id": aid})  # fmt: skip
    assert changed.status_code == 422

    r = await mexec(requester, cid, db, pipe, {"Approval-Id": aid})
    assert r.status_code == 200, r.text
    assert sorted(d["n"] for d in c[db]["evens_odds"].find()) == [1, 3, 5]
    assert (await mexec(requester, cid, db, pipe, {"Approval-Id": aid})).status_code == 409


async def test_merge_stage_with_approval(admin, new_user, mongo_managed, scratch_db):
    db, c = scratch_db
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    cid = conn["id"]
    requester, reviewer = await two_dbas(admin, new_user, cid)
    pipe = [{"$match": {"n": {"$lte": 2}}}, {"$merge": {"into": "dst", "whenMatched": "replace"}}]
    aid = (await mexec(requester, cid, db, pipe)).json()["approval_id"]
    await reviewer.post(f"approvals/{aid}/approve", {})
    r = await mexec(requester, cid, db, pipe, {"Approval-Id": aid})
    assert r.status_code == 200, r.text
    assert sorted(d["_id"] for d in c[db].dst.find()) == [1, 2]
    assert c[db].dst.find_one({"_id": 1})["g"] == 1  # replaced


@pytest.mark.security
@pytest.mark.parametrize(
    "pipe",
    [
        [{"$out": "x"}, {"$match": {}}],  # not the last stage
        [{"$match": {}}, {"$facet": {"a": [{"$out": "x"}]}}],  # nested
        [{"$match": {}}, {"$out": {"db": "admin", "coll": "x"}}],  # other database
        [{"$match": {}}, {"$out": "system.users"}],
        [{"$match": {}}, {"$out": "a$b"}],
        [{"$match": {}}, {"$out": 5}],
        [{"$match": {}}, {"$merge": {"into": "dst", "whenMatched": [{"$set": {"x": 1}}]}}],
        [{"$match": {}}, {"$merge": {"into": "dst", "let": {"a": 1}}}],
        [{"$match": {}}, {"$merge": {"into": {"db": "local", "coll": "x"}}}],
        [{"$match": {}}, {"$merge": {"into": "dst", "whenNotMatched": "evil"}}],
        [{"$match": {}}, {"$out": "x", "$limit": 1}],
        [{"$lookup": {"from": "src", "pipeline": [{"$merge": {"into": "dst"}}], "as": "a"}}],
    ],
)
async def test_write_stage_abuse_is_rejected(admin, mongo_managed, scratch_db, pipe):
    db, c = scratch_db
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    r = await mexec(admin, conn["id"], db, pipe)
    assert r.status_code == 422, (pipe, r.text)
    assert c[db].dst.count_documents({}) == 1 and "x" not in c[db].list_collection_names()


async def test_read_only_connections_refuse_write_stages(admin, mongo_managed, scratch_db):
    db, _ = scratch_db
    conn = await create_connection(admin, "mongodb", mongo_managed)  # read-only
    r = await mexec(admin, conn["id"], db, [{"$match": {}}, {"$out": "nope"}])
    assert r.status_code == 403, r.text


async def test_production_out_needs_the_target_name(admin, new_user, mongo_managed, scratch_db):
    db, c = scratch_db
    conn = await create_connection(
        admin, "mongodb", mongo_managed, read_only=False, environment="production"
    )
    cid = conn["id"]
    requester, reviewer = await two_dbas(admin, new_user, cid)
    pipe = [{"$match": {}}, {"$out": "dst"}]
    r = await mexec(requester, cid, db, pipe)
    assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    r = await mexec(requester, cid, db, pipe, confirm="dst")
    assert r.json()["code"] == "approval_required"
    aid = r.json()["approval_id"]
    await reviewer.post(f"approvals/{aid}/approve", {})
    ok = await mexec(requester, cid, db, pipe, {"Approval-Id": aid}, confirm="dst")
    assert ok.status_code == 200, ok.text
    assert c[db].dst.count_documents({}) == 6
    assert isinstance(c, pymongo.MongoClient)
