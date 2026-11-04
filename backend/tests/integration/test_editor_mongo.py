"""Phase 3 for MongoDB: document editing (compare-and-swap), bulk, masking and export."""

import json
import uuid
from typing import Any

import pymongo
import pytest
import pytest_asyncio
from bson import ObjectId

from tests.integration.conftest import Api, create_connection, grant


class MongoW:
    def __init__(self, api: Api, cid: str, coll: str, info: dict[str, Any]):
        self.api, self.cid, self.coll, self.info = api, cid, coll, info

    @property
    def base(self) -> str:
        return f"connections/{self.cid}/mongo/databases/shop/collections/{self.coll}"

    def raw(self) -> Any:
        c = pymongo.MongoClient(
            host=self.info["host"], port=self.info["port"], username="root", password="rootpw"
        )
        return c["shop"][self.coll], c

    def count(self, flt: dict[str, Any] | None = None) -> int:
        coll, c = self.raw()
        try:
            return coll.count_documents(flt or {})
        finally:
            c.close()

    async def docs(self, **params: Any) -> dict[str, Any]:
        for k in ("filter", "sort", "projection"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        r = await self.api.get(f"{self.base}/documents", params={"limit": 100, **params})
        assert r.status_code == 200, r.text
        return r.json()

    async def one(self, name: str) -> tuple[dict[str, Any], str]:
        page = await self.docs(filter={"name": name})
        return page["documents"][0], page["doc_keys"][0]


@pytest_asyncio.fixture
async def mw(admin, mongo_managed) -> MongoW:
    name = f"c_{uuid.uuid4().hex[:8]}"
    probe = MongoW(admin, "", name, mongo_managed)
    coll, c = probe.raw()
    coll.insert_many(
        [
            {"name": n, "qty": i, "email": f"{n}@example.com", "tags": ["x", n], "meta": {"k": i}}
            for i, n in enumerate("abcde", start=1)
        ]
    )
    c.close()
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    probe.cid = conn["id"]
    return probe


async def test_pages_expose_document_keys(mw):
    page = await mw.docs()
    assert len(page["doc_keys"]) == len(page["documents"]) == 5 and all(page["doc_keys"])


async def test_insert_document(mw):
    r = await mw.api.post(
        f"{mw.base}/documents",
        {
            "document": {
                "name": "new",
                "n": {"$numberLong": "9007199254740993"},
                "when": {"$date": "2024-01-01T00:00:00Z"},
            }
        },
    )
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert doc["name"] == "new" and "$oid" in doc["_id"] and r.json()["doc_key"]
    assert doc["n"] == {"$numberLong": "9007199254740993"}  # BSON type preserved
    assert mw.count({"name": "new"}) == 1


@pytest.mark.security
@pytest.mark.parametrize(
    "doc",
    [
        {"$set": {"a": 1}},
        {"a": {"$where": "1"}},
        {"a.b": 1, "$x": 1},
        {"nested": {"$ne": 1}},
        "not an object",
        {"big": "x" * (1024 * 1024 + 10)},
    ],
)
async def test_invalid_documents_are_rejected(mw, doc):
    r = await mw.api.post(f"{mw.base}/documents", {"document": doc})
    assert r.status_code == 422, r.text
    assert mw.count() == 5


async def test_duplicate_id_is_a_conflict(mw):
    doc, _ = await mw.one("a")
    r = await mw.api.post(f"{mw.base}/documents", {"document": {"_id": doc["_id"], "name": "dup"}})
    assert r.status_code == 409


async def test_replace_is_compare_and_swap(mw):
    doc, key = await mw.one("a")
    new = {**doc, "qty": {"$numberInt": "100"}, "added": "yes"}
    r = await mw.api.put(f"{mw.base}/documents/{key}", {"original": doc, "document": new})
    assert r.status_code == 200, r.text
    assert r.json()["document"]["added"] == "yes"
    # the same (now stale) original can no longer be used
    again = await mw.api.put(
        f"{mw.base}/documents/{key}",
        {"original": doc, "document": {**doc, "qty": {"$numberInt": "5"}}},
    )
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    stored, c = mw.raw()
    try:
        assert stored.find_one({"name": "a"})["qty"] == 100
    finally:
        c.close()


async def test_a_concurrent_change_to_another_field_is_also_detected(mw):
    doc, key = await mw.one("b")
    coll, c = mw.raw()
    coll.update_one({"name": "b"}, {"$set": {"meta.k": 99}})  # someone else edits a nested field
    c.close()
    r = await mw.api.put(
        f"{mw.base}/documents/{key}",
        {"original": doc, "document": {**doc, "qty": {"$numberInt": "7"}}},
    )
    assert r.status_code == 409


async def test_id_cannot_be_changed(mw):
    doc, key = await mw.one("c")
    r = await mw.api.put(
        f"{mw.base}/documents/{key}",
        {"original": doc, "document": {**doc, "_id": {"$oid": str(ObjectId())}}},
    )
    assert r.status_code == 422


async def test_replace_can_remove_fields_and_keeps_types(mw):
    doc, key = await mw.one("d")
    new = {k: v for k, v in doc.items() if k != "tags"}
    r = await mw.api.put(f"{mw.base}/documents/{key}", {"original": doc, "document": new})
    assert r.status_code == 200 and "tags" not in r.json()["document"]
    assert (
        isinstance(r.json()["document"]["qty"], dict)
        and "$numberInt" in r.json()["document"]["qty"]
    )


async def test_delete_with_original_and_stale_delete(mw):
    doc, key = await mw.one("e")
    coll, c = mw.raw()
    coll.update_one({"name": "e"}, {"$set": {"qty": 500}})
    c.close()
    stale = await mw.api.request("DELETE", f"{mw.base}/documents/{key}", json={"original": doc})
    assert stale.status_code == 409 and mw.count({"name": "e"}) == 1
    doc2, key2 = await mw.one("e")
    ok = await mw.api.request("DELETE", f"{mw.base}/documents/{key2}", json={"original": doc2})
    assert ok.status_code == 204 and mw.count({"name": "e"}) == 0
    assert (
        await mw.api.request("DELETE", f"{mw.base}/documents/{key2}", json={})
    ).status_code == 409


@pytest.mark.security
async def test_forged_document_keys_are_rejected(mw):
    import base64

    for key in (
        "!!!",
        base64.urlsafe_b64encode(b'{"k":{"$ne":null}}').decode(),
        base64.urlsafe_b64encode(b'{"k":{"$where":"1"}}').decode(),
    ):
        r = await mw.api.request("DELETE", f"{mw.base}/documents/{key}", json={"confirm": mw.coll})
        assert r.status_code in (409, 422), key
    assert mw.count() == 5  # a forged operator-bearing key matches nothing and deletes nothing


@pytest.mark.security
async def test_read_only_connections_and_roles(mw, admin, new_user, mongo_managed):
    ro = await create_connection(admin, "mongodb", mongo_managed)
    ro_w = MongoW(admin, ro["id"], mw.coll, mongo_managed)
    _, key = await ro_w.one("a")
    for call in (
        admin.post(f"{ro_w.base}/documents", {"document": {"name": "x"}}),
        admin.put(f"{ro_w.base}/documents/{key}", {"original": {}, "document": {}}),
        admin.request("DELETE", f"{ro_w.base}/documents/{key}", json={}),
    ):
        assert (await call).status_code == 403
    viewer = await new_user("Viewer")
    await grant(admin, mw.cid, viewer, "Viewer")
    assert (
        await viewer.post(f"{mw.base}/documents", {"document": {"name": "x"}})
    ).status_code == 403
    assert mw.count() == 5


async def test_collections_that_are_views_cannot_be_edited(mw, admin, mongo_managed):
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    base = f"connections/{conn['id']}/mongo/databases/shop/collections/recent_events"
    r = await admin.post(f"{base}/documents", {"document": {"x": 1}})
    assert r.status_code == 422 and r.json()["code"] == "not_editable"


@pytest.mark.security
async def test_production_requires_the_collection_name(mw, admin, mongo_managed):
    prod = await create_connection(
        admin, "mongodb", mongo_managed, read_only=False, environment="production"
    )
    p = MongoW(admin, prod["id"], mw.coll, mongo_managed)
    _, key = await p.one("a")
    for confirm in (None, "nope"):
        r = await admin.request("DELETE", f"{p.base}/documents/{key}", json={"confirm": confirm})
        assert r.status_code == 428
    assert mw.count() == 5
    assert (
        await admin.request("DELETE", f"{p.base}/documents/{key}", json={"confirm": mw.coll})
    ).status_code == 204


# ------------------------------------------------------------------------------- bulk


async def test_bulk_delete_with_preview_and_token(mw):
    flt = {"qty": {"$gte": 4}}
    pv = (
        await mw.api.post(
            f"{mw.base}/documents/bulk/preview", {"operation": "delete", "filter": flt}
        )
    ).json()
    assert pv["count"] == 2 and len(pv["sample"]) == 2
    base = {"filter": flt, "expected_count": 2, "token": pv["token"]}
    for bad in (
        {**base, "token": "x"},
        {**base, "expected_count": 1},
        {**base, "filter": {"qty": {"$gte": 1}}},
    ):
        assert (await mw.api.post(f"{mw.base}/documents/bulk/delete", bad)).status_code == 422
    assert mw.count() == 5
    ok = await mw.api.post(f"{mw.base}/documents/bulk/delete", base)
    assert ok.status_code == 200 and ok.json() == {"affected": 2} and mw.count() == 3


async def test_bulk_aborts_if_the_selection_grew_and_never_touches_new_documents(mw):
    flt = {"qty": {"$gte": 4}}
    pv = (
        await mw.api.post(
            f"{mw.base}/documents/bulk/preview", {"operation": "delete", "filter": flt}
        )
    ).json()
    coll, c = mw.raw()
    coll.insert_one({"name": "late", "qty": 100})
    c.close()
    r = await mw.api.post(
        f"{mw.base}/documents/bulk/delete",
        {"filter": flt, "expected_count": 2, "token": pv["token"]},
    )
    assert r.status_code == 409 and mw.count({"name": "late"}) == 1 and mw.count() == 6


async def test_bulk_update_sets_fields(mw):
    flt = {"qty": {"$lte": 2}}
    setv = {"flag": True, "meta.reviewed": "yes"}
    pv = (
        await mw.api.post(
            f"{mw.base}/documents/bulk/preview", {"operation": "update", "filter": flt, "set": setv}
        )
    ).json()
    r = await mw.api.post(
        f"{mw.base}/documents/bulk/update",
        {"filter": flt, "set": setv, "expected_count": pv["count"], "token": pv["token"]},
    )
    assert r.status_code == 200 and r.json()["affected"] == 2
    assert mw.count({"flag": True, "meta.reviewed": "yes"}) == 2
    # a token cannot be reused with a different update
    r2 = await mw.api.post(
        f"{mw.base}/documents/bulk/update",
        {"filter": flt, "set": {"flag": "evil"}, "expected_count": 2, "token": pv["token"]},
    )
    assert r2.status_code == 422


@pytest.mark.security
@pytest.mark.parametrize("bad_set", [{"_id": 1}, {"$where": "1"}, {"a": {"$ne": 1}}, {}])
async def test_bulk_update_rejects_unsafe_sets(mw, bad_set):
    r = await mw.api.post(
        f"{mw.base}/documents/bulk/preview",
        {"operation": "update", "filter": {"qty": 1}, "set": bad_set},
    )
    assert r.status_code == 422


@pytest.mark.security
async def test_bulk_filters_use_the_operator_allowlist(mw):
    r = await mw.api.post(
        f"{mw.base}/documents/bulk/preview",
        {"operation": "delete", "filter": {"$where": "sleep(1000)"}},
    )
    assert r.status_code == 422 and mw.count() == 5


# ------------------------------------------------------------------------------- masking


async def mark(admin, mw, *fields):
    r = await admin.put(
        f"connections/{mw.cid}/sensitive",
        {
            "markers": [
                {"database": "shop", "schema": "", "object": mw.coll, "field": f} for f in fields
            ]
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.security
async def test_sensitive_fields_are_masked_and_cannot_be_probed(mw, admin, new_user):
    await mark(admin, mw, "email", "meta.k")
    editor = await new_user("Editor")
    await grant(admin, mw.cid, editor, "Editor")
    page = (await editor.get(f"{mw.base}/documents", params={"limit": 100})).json()
    d = page["documents"][0]
    assert d["email"] == {"$masked": True} and d["meta"]["k"] == {"$masked": True}
    assert d["name"] == "a" and "a@example.com" not in json.dumps(page)
    for params in (
        {"filter": json.dumps({"email": {"$regex": "^a"}})},
        {"sort": json.dumps({"email": 1})},
        {"filter": json.dumps({"$or": [{"meta.k": 1}]})},
    ):
        r = await editor.get(f"{mw.base}/documents", params=params)
        assert r.status_code == 403 and "sensitive" in r.json()["detail"], params
    assert (await mw.docs())["documents"][0]["email"] == "a@example.com"  # super admin sees it


@pytest.mark.security
async def test_users_who_cannot_see_sensitive_fields_cannot_overwrite_whole_documents(
    mw, admin, new_user
):
    await mark(admin, mw, "email")
    editor = await new_user("Editor")
    await grant(admin, mw.cid, editor, "Editor")
    doc, key = await mw.one("a")
    r = await editor.put(
        f"{mw.base}/documents/{key}",
        {"original": doc, "document": {**doc, "qty": {"$numberInt": "9"}}},
    )
    assert r.status_code == 403
    assert (
        await editor.post(f"{mw.base}/documents", {"document": {"name": "n", "email": "x"}})
    ).status_code == 403
    ins = await editor.post(f"{mw.base}/documents", {"document": {"name": "fine"}})
    assert ins.status_code == 201


async def test_mongo_markers_are_validated(mw, admin):
    base = {"database": "shop", "schema": "", "object": mw.coll}
    for field in ("_id", "$bad", ""):
        r = await admin.put(
            f"connections/{mw.cid}/sensitive", {"markers": [{**base, "field": field}]}
        )
        assert r.status_code == 422, field
    ghost = await admin.put(
        f"connections/{mw.cid}/sensitive", {"markers": [{**base, "object": "ghost", "field": "x"}]}
    )
    assert ghost.status_code in (404, 422)


# ------------------------------------------------------------------------------- export


async def test_json_and_ndjson_export(mw):
    j = (await mw.api.get(f"{mw.base}/export", params={"format": "json"})).json()
    assert len(j) == 5 and "$oid" in j[0]["_id"]
    r = await mw.api.get(f"{mw.base}/export", params={"format": "ndjson"})
    lines = r.text.strip().splitlines()
    assert r.headers["content-type"].startswith("application/x-ndjson") and len(lines) == 5
    assert json.loads(lines[0])["name"] == "a"
    ev = (
        await mw.api.get("audit/events", params={"connection_id": mw.cid, "action": "data.export"})
    ).json()["events"][0]
    assert ev["detail"]["rows"] == 5 and ev["target"]["collection"] == mw.coll


async def test_export_filter_sort_projection_and_limits(mw, admin, mongo_managed, monkeypatch):
    q = {
        "filter": json.dumps({"qty": {"$gte": 3}}),
        "sort": json.dumps({"qty": -1}),
        "projection": json.dumps({"name": 1, "_id": 0}),
    }
    j = (await mw.api.get(f"{mw.base}/export", params=q)).json()
    assert [x["name"] for x in j] == ["e", "d", "c"] and set(j[0]) == {"name"}
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "export_max_rows", 3)
    capped = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    base = f"connections/{capped['id']}/mongo/databases/shop/collections/{mw.coll}"
    r = await admin.get(f"{base}/export")
    assert r.status_code == 422 and "exceed 3" in r.json()["detail"]


@pytest.mark.security
async def test_export_permissions_and_masking(mw, admin, new_user):
    await mark(admin, mw, "email")
    viewer = await new_user("Viewer")
    await grant(admin, mw.cid, viewer, "Viewer")
    assert (await viewer.get(f"{mw.base}/export")).status_code == 403
    role = (
        await admin.post(
            "roles",
            {
                "name": f"exp-{uuid.uuid4().hex[:6]}",
                "permissions": ["connection.view", "metadata.view", "data.read", "export.export"],
            },
        )
    ).json()
    limited = await new_user("Viewer")
    grants = [
        {"user_id": g["user_id"], "role_id": g["role_id"]}
        for g in (await admin.get(f"connections/{mw.cid}/grants")).json()
    ]
    await admin.put(
        f"connections/{mw.cid}/grants",
        {"grants": [*grants, {"user_id": limited.user_id, "role_id": role["id"]}]},
    )
    out = (await limited.get(f"{mw.base}/export", params={"format": "json"})).json()
    assert out[0]["email"] == {"$masked": True} and "@example.com" not in json.dumps(out)
    r = await limited.get(
        f"{mw.base}/export", params={"filter": json.dumps({"email": "a@example.com"})}
    )
    assert r.status_code == 403
