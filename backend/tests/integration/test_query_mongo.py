"""Phase 4: MongoDB query workspace (find, aggregation allowlist, history, cancel)."""

import json
import uuid

import pytest
import pytest_asyncio

from tests.integration.conftest import create_connection, grant


@pytest_asyncio.fixture
async def mq(admin, mongo_managed):
    conn = await create_connection(admin, "mongodb", mongo_managed)
    return conn["id"]


async def execute(api, cid, **body):
    body = {"database": "shop", "collection": "customers", **body}
    return await api.post(f"connections/{cid}/mongo/queries/execute", body)


async def test_find_with_filter_sort_projection(admin, mq):
    r = await execute(
        admin,
        mq,
        kind="find",
        filter={"age": {"$gte": 60}},
        sort={"age": -1},
        projection={"name": 1, "age": 1, "_id": 0},
        limit=5,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["count"] == 5 and out["operation_id"] and out["elapsed_ms"] >= 0
    ages = [int(d["age"]["$numberInt"]) for d in out["documents"]]
    assert ages == sorted(ages, reverse=True) and all(a >= 60 for a in ages)
    assert set(out["documents"][0]) == {"name", "age"}


async def test_filter_may_be_sent_as_text_and_is_validated(admin, mq):
    ok = await execute(admin, mq, kind="find", filter='{"name": "Customer 001"}')
    assert ok.status_code == 200 and ok.json()["count"] == 1
    for bad in ({"$where": "1"}, "{nope"):
        assert (await execute(admin, mq, kind="find", filter=bad)).status_code == 422


async def test_aggregation_group_and_lookup(admin, mq):
    group = await execute(
        admin,
        mq,
        kind="aggregate",
        pipeline=[{"$group": {"_id": "$address.city", "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}],
    )
    assert group.status_code == 200, group.text
    docs = group.json()["documents"]
    assert [d["_id"] for d in docs] == ["Paris", "Rome"]
    assert sum(int(d["n"]["$numberInt"]) for d in docs) == 250
    look = await execute(
        admin,
        mq,
        kind="aggregate",
        collection="events",
        pipeline=[
            {"$lookup": {"from": "customers", "pipeline": [{"$limit": 1}], "as": "c"}},
            {"$limit": 2},
        ],
    )
    assert look.status_code == 200 and len(look.json()["documents"][0]["c"]) == 1


async def test_pipeline_output_is_always_bounded(admin, mq):
    r = await execute(admin, mq, kind="aggregate", pipeline=[{"$match": {}}], limit=3)
    out = r.json()
    assert out["count"] == 3 and out["truncated"] is True
    full = await execute(admin, mq, kind="aggregate", pipeline=[{"$match": {}}], limit=1000)
    assert full.json()["count"] == 250 and full.json()["truncated"] is False


@pytest.mark.security
@pytest.mark.parametrize("stage", [{"$out": "stolen"}, {"$merge": {"into": "x"}}])
async def test_write_stages_are_refused_on_read_only_connections(admin, mq, stage):
    """Phase 5 allows $out/$merge only with query.execute_write on a writable connection and
    an approval; a read-only connection refuses them at the permission layer."""
    r = await execute(admin, mq, kind="aggregate", pipeline=[stage])
    assert r.status_code == 403, r.text
    denied = (
        await admin.get(
            "audit/events",
            params={"connection_id": mq, "action": "query.execute", "result": "denied"},
        )
    ).json()["events"]
    assert denied and denied[0]["detail"]["kind"] == "aggregate"


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$addFields": {"x": {"$function": {"body": "function(){}", "args": [], "lang": "js"}}}}],
        [{"$match": {"$where": "sleep(5000)"}}],
        [{"$lookup": {"from": "events", "pipeline": [{"$out": "x"}], "as": "o"}}],
        [{"$currentOp": {}}],
        [
            {
                "$lookup": {
                    "from": "no_such_collection",
                    "localField": "a",
                    "foreignField": "b",
                    "as": "x",
                }
            }
        ],
        [{"$lookup": {"from": "system.users", "localField": "a", "foreignField": "b", "as": "x"}}],
        [{"$unionWith": {"db": "admin", "coll": "system.users"}}],
    ],
)
async def test_unsafe_pipelines_are_rejected_and_audited(admin, mq, pipeline):
    r = await execute(admin, mq, kind="aggregate", pipeline=pipeline)
    assert r.status_code == 422, r.text
    denied = (
        await admin.get(
            "audit/events",
            params={"connection_id": mq, "action": "query.execute", "result": "denied"},
        )
    ).json()["events"]
    assert denied and denied[0]["detail"]["kind"] == "aggregate"
    # nothing was written anywhere
    check = await execute(admin, mq, kind="find", collection="customers", limit=1)
    assert check.status_code == 200
    colls = [
        c["name"]
        for c in (await admin.get(f"connections/{mq}/mongo/databases/shop/collections")).json()
    ]
    assert "stolen" not in colls and "x" not in colls


async def test_unknown_collection_and_missing_pipeline(admin, mq):
    assert (await execute(admin, mq, kind="find", collection="nope")).status_code == 404
    assert (await execute(admin, mq, kind="aggregate")).status_code == 422


async def test_history_audit_and_saved_queries_for_mongo(admin, mq, new_user):
    marker = f"needle-{uuid.uuid4().hex[:8]}"
    await execute(admin, mq, kind="find", filter={"name": marker})
    h = (await admin.get(f"connections/{mq}/queries/history", params={"kind": "mongo"})).json()[0]
    stored = json.loads(h["statement"])
    assert (
        h["kind"] == "mongo" and stored["kind"] == "find" and stored["filter"] == {"name": marker}
    )
    ev = (
        await admin.get("audit/events", params={"connection_id": mq, "action": "query.execute"})
    ).json()["events"][0]
    assert ev["detail"]["digest"] and marker not in json.dumps(ev)
    assert (
        await admin.get(f"connections/{mq}/queries/history", params={"kind": "sql"})
    ).json() == []
    saved = await admin.post(
        f"connections/{mq}/queries/saved",
        {"kind": "mongo", "name": "recent", "database": "shop", "statement": json.dumps(stored)},
    )
    assert saved.status_code == 201 and saved.json()["kind"] == "mongo"


@pytest.mark.security
async def test_mongo_history_can_drop_literal_values(admin, mq):
    await admin.put("settings/query-history", {"retention_days": 30, "store_literals": False})
    try:
        await execute(
            admin, mq, kind="find", filter={"email": "pii@example.com", "age": {"$gt": 41}}
        )
        h = (await admin.get(f"connections/{mq}/queries/history")).json()[0]
        assert (
            h["redacted"] and "pii@example.com" not in h["statement"] and "41" not in h["statement"]
        )
        assert json.loads(h["statement"])["filter"] == {"email": "?", "age": {"$gt": "?"}}
    finally:
        await admin.put("settings/query-history", {"retention_days": 90, "store_literals": True})


@pytest.mark.security
async def test_roles_and_sensitive_gate(admin, mq, new_user, mongo_managed):
    viewer = await new_user("Viewer")
    await grant(admin, mq, viewer, "Viewer")
    assert (await execute(viewer, mq, kind="find", limit=1)).status_code == 200
    role = (
        await admin.post(
            "roles",
            {
                "name": f"nq-{uuid.uuid4().hex[:6]}",
                "permissions": ["connection.view", "metadata.view", "data.read"],
            },
        )
    ).json()
    no_query = await new_user("Viewer")
    grants = [
        {"user_id": g["user_id"], "role_id": g["role_id"]}
        for g in (await admin.get(f"connections/{mq}/grants")).json()
    ]
    await admin.put(
        f"connections/{mq}/grants",
        {"grants": [*grants, {"user_id": no_query.user_id, "role_id": role["id"]}]},
    )
    assert (await execute(no_query, mq, kind="find", limit=1)).status_code == 403
    # sensitive markers close free-form queries to users without data.read_sensitive
    marker = {"database": "shop", "schema": "", "object": "customers", "field": "email"}
    assert (
        await admin.put(f"connections/{mq}/sensitive", {"markers": [marker]})
    ).status_code == 200
    r = await execute(viewer, mq, kind="aggregate", pipeline=[{"$project": {"e": "$email"}}])
    assert r.status_code == 403 and "sensitive" in r.json()["detail"]
    assert (await execute(admin, mq, kind="find", limit=1)).status_code == 200
    await admin.put(f"connections/{mq}/sensitive", {"markers": []})


async def test_sql_endpoints_reject_mongo_and_vice_versa(admin, mq, pg_managed):
    sql = await admin.post(
        f"connections/{mq}/queries/execute", {"sql": "SELECT 1", "database": "shop"}
    )
    assert sql.status_code == 422 and sql.json()["code"] == "unsupported_capability"
    pg = await create_connection(admin, "postgresql", pg_managed)
    r = await execute(admin, pg["id"], kind="find")
    assert r.status_code == 422


async def test_cancel_marks_the_operation_and_is_best_effort(admin, mq, app_sql):
    op = str(uuid.uuid4())
    app_sql(
        "INSERT INTO operations (id, connection_id, user_id, kind, status, database, backend_ref, started_at) "
        "VALUES (%s, %s, (SELECT id FROM users WHERE username = 'admin'), 'mongo', 'running', 'shop', '{}', now())",
        (op, mq),
    )
    r = await admin.post(f"connections/{mq}/queries/{op}/cancel")
    assert r.status_code == 202 and r.json()["best_effort"] is True  # nothing was actually running
    assert app_sql("SELECT status FROM operations WHERE id = %s", (op,))[0][0] == "cancelled"
    assert (await admin.post(f"connections/{mq}/queries/{op}/cancel")).status_code == 409
    assert (await admin.post(f"connections/{mq}/queries/{uuid.uuid4()}/cancel")).status_code == 404
