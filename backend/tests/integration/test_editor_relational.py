"""Phase 3: row editing, optimistic concurrency, bulk operations, masking, cells and export."""

import csv
import io
import json
import uuid

import pytest

from tests.integration.conftest import create_connection, grant, writable_conn

# ------------------------------------------------------------------------------- insert


async def test_insert_returns_the_row_and_a_row_key(wr):
    r = await wr.api.post(
        f"{wr.base()}/rows",
        {"values": {"name": "new", "qty": 9, "price": "12.50", "big": "9007199254740999"}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    row = dict(zip([c["name"] for c in body["columns"]], body["row"], strict=True))
    assert row["name"] == "new" and row["qty"] == 9 and row["price"] == "12.50"
    assert row["big"] == "9007199254740999"  # exact, no float rounding
    assert body["row_key"]
    assert wr.sql(f"SELECT big FROM {wr.table} WHERE name = 'new'")[0][0] == 9007199254740999


async def test_insert_uses_defaults_and_json_and_null(wr):
    r = await wr.api.post(
        f"{wr.base()}/rows", {"values": {"name": "j", "meta": {"a": [1, 2]}, "note": None}}
    )
    assert r.status_code == 201, r.text
    cols = [c["name"] for c in r.json()["columns"]]
    row = dict(zip(cols, r.json()["row"], strict=True))
    assert row["qty"] == 0  # column default applied
    assert row["note"] is None
    meta = row["meta"]
    assert (json.loads(meta) if isinstance(meta, str) else meta) == {"a": [1, 2]}


async def test_insert_binary_via_base64(wr):
    r = await wr.api.post(
        f"{wr.base()}/rows", {"values": {"name": "bin", "photo": {"$base64": "3q2+7w=="}}}
    )
    assert r.status_code == 201, r.text
    assert wr.sql(f"SELECT length(photo) FROM {wr.table} WHERE name = 'bin'" if wr.engine == "postgresql"
                  else f"SELECT LENGTH(photo) FROM {wr.table} WHERE name = 'bin'")[0][0] == 4  # fmt: skip


@pytest.mark.parametrize(
    "values",
    [
        {"nope": 1},
        {"name": None},
        {"photo": {"$base64": "%%%"}},
        {"qty": {"a": 1}},
        {"name": "x\u0000y"},
    ],
)
async def test_invalid_inserts_are_rejected_without_side_effects(wr, values):
    before = wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0]
    r = await wr.api.post(f"{wr.base()}/rows", {"values": values})
    assert r.status_code in (400, 422), r.text
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == before


@pytest.mark.security
async def test_insert_values_are_bound_never_interpolated(wr):
    evil = "x'); DROP TABLE " + wr.table + "; --"
    r = await wr.api.post(f"{wr.base()}/rows", {"values": {"name": evil}})
    assert r.status_code == 201
    assert wr.sql(f"SELECT name FROM {wr.table} WHERE id = {r.json()['row'][0]}")[0][0] == evil


# ------------------------------------------------------------------------------- update


async def test_update_with_expected_values(wr):
    key = await wr.row_key("a")
    r = await wr.api.patch(
        f"{wr.base()}/rows/{key}", {"values": {"qty": 42}, "expected": {"qty": 1}}
    )
    assert r.status_code == 200, r.text
    assert wr.sql(f"SELECT qty, name FROM {wr.table} WHERE name = 'a'")[0] == (42, "a")


async def test_stale_update_is_a_conflict_and_changes_nothing(wr):
    key = await wr.row_key("b")
    wr.sql(f"UPDATE {wr.table} SET qty = 99 WHERE name = 'b'")  # someone else edits first
    r = await wr.api.patch(
        f"{wr.base()}/rows/{key}", {"values": {"qty": 7}, "expected": {"qty": 2}}
    )
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    assert wr.sql(f"SELECT qty FROM {wr.table} WHERE name = 'b'")[0][0] == 99


async def test_update_matches_null_previous_values(wr):
    key = await wr.row_key("b")  # `big` is NULL for row b
    r = await wr.api.patch(
        f"{wr.base()}/rows/{key}", {"values": {"big": "77"}, "expected": {"big": None}}
    )
    assert r.status_code == 200, r.text
    assert str(wr.sql(f"SELECT big FROM {wr.table} WHERE name = 'b'")[0][0]) == "77"
    stale = await wr.api.patch(
        f"{wr.base()}/rows/{key}", {"values": {"big": "78"}, "expected": {"big": None}}
    )
    assert stale.status_code == 409  # it is no longer NULL


async def test_update_requires_previous_values_and_allows_explicit_unchecked(wr):
    key = await wr.row_key("a")
    r = await wr.api.patch(f"{wr.base()}/rows/{key}", {"values": {"qty": 5}, "expected": {}})
    assert r.status_code == 422 and "expected" in r.json()["detail"].lower()
    ok = await wr.api.patch(
        f"{wr.base()}/rows/{key}",
        {"values": {"meta": {"z": 1}}, "expected": {"meta": {"$unchecked": True}}},
    )
    assert ok.status_code == 200, ok.text
    ev = (
        await wr.api.get("audit/events", params={"connection_id": wr.cid, "action": "data.update"})
    ).json()["events"]
    assert ev[0]["detail"]["unchecked"] == ["meta"]  # the unchecked column is visible in the audit


async def test_primary_key_and_unknown_columns_cannot_be_updated(wr):
    key = await wr.row_key("a")
    for values in ({"id": 99}, {"ghost": 1}):
        r = await wr.api.patch(f"{wr.base()}/rows/{key}", {"values": values, "expected": values})
        assert r.status_code == 422, r.text


@pytest.mark.security
async def test_forged_row_keys_are_rejected(wr):
    import base64

    bad = [
        "garbage!!",
        base64.urlsafe_b64encode(b'{"r":[1,2]}').decode(),  # wrong arity
        base64.urlsafe_b64encode(b'{"r":[null]}').decode(),
        base64.urlsafe_b64encode(b'{"r":[{"$ne":1}]}').decode(),
        base64.urlsafe_b64encode(b'{"r":["1 OR 1=1"]}').decode(),
    ]
    before = wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0]
    for key in bad:
        r = await wr.api.request("DELETE", f"{wr.base()}/rows/{key}", json={"confirm": wr.table})
        assert r.status_code in (400, 409, 422), (key, r.status_code)  # all refused
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == before  # nothing deleted


async def test_two_editors_the_second_one_loses(wr):
    key = await wr.row_key("d")
    first = await wr.api.patch(
        f"{wr.base()}/rows/{key}", {"values": {"qty": 10}, "expected": {"qty": 4}}
    )
    second = await wr.api.patch(
        f"{wr.base()}/rows/{key}", {"values": {"qty": 20}, "expected": {"qty": 4}}
    )
    assert (first.status_code, second.status_code) == (200, 409)
    assert wr.sql(f"SELECT qty FROM {wr.table} WHERE name = 'd'")[0][0] == 10


# ------------------------------------------------------------------------------- delete


async def test_delete_and_double_delete(wr):
    key = await wr.row_key("e")
    r = await wr.api.request("DELETE", f"{wr.base()}/rows/{key}", json={"expected": {"qty": 5}})
    assert r.status_code == 204
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE name = 'e'")[0][0] == 0
    again = await wr.api.request("DELETE", f"{wr.base()}/rows/{key}", json={})
    assert again.status_code == 409


async def test_delete_with_stale_expected_values_is_refused(wr):
    key = await wr.row_key("c")
    r = await wr.api.request("DELETE", f"{wr.base()}/rows/{key}", json={"expected": {"qty": 999}})
    assert r.status_code == 409
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE name = 'c'")[0][0] == 1


# ------------------------------------------------------------------------------- editability


async def test_pages_expose_row_keys_and_editability(wr):
    page = await wr.rows()
    assert page["editable"] is True and len(page["row_keys"]) == len(page["rows"])
    assert all(page["row_keys"])
    nopk = (await wr.api.get(f"{wr.base(wr.table + '_nopk')}/rows")).json()
    assert nopk["editable"] is False and nopk["row_keys"] == [None]
    view = (await wr.api.get(f"{wr.base(wr.table + '_v')}/rows")).json()
    assert view["editable"] is False


async def test_tables_without_a_primary_key_and_views_cannot_be_edited(wr):
    nopk = wr.base(wr.table + "_nopk")
    fake = (await wr.api.get(f"{wr.base()}/rows?limit=1")).json()["row_keys"][0]
    r = await wr.api.patch(f"{nopk}/rows/{fake}", {"values": {"b": "y"}, "expected": {"b": "x"}})
    assert r.status_code == 422 and r.json()["code"] == "not_editable"
    r = await wr.api.request("DELETE", f"{nopk}/rows/{fake}", json={"confirm": "x"})
    assert r.status_code == 422 and r.json()["code"] == "not_editable"
    r = await wr.api.post(f"{wr.base(wr.table + '_v')}/rows", {"values": {"name": "v"}})
    assert r.status_code == 422 and r.json()["code"] == "not_editable"
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}_nopk")[0][0] == 1


# ------------------------------------------------------------------------------- permissions


@pytest.mark.security
async def test_read_only_connections_cannot_be_written(wr, admin):
    info = wr.info
    ro = await create_connection(admin, wr.engine, info)  # read_only defaults to true
    ro_w = type(wr)(admin, wr.engine, ro["id"], wr.table, info)
    key = (await ro_w.rows())["row_keys"][0]
    for call in (
        admin.post(f"{ro_w.base()}/rows", {"values": {"name": "x"}}),
        admin.patch(f"{ro_w.base()}/rows/{key}", {"values": {"qty": 1}, "expected": {"qty": 1}}),
        admin.request("DELETE", f"{ro_w.base()}/rows/{key}", json={"confirm": wr.table}),
        admin.post(
            f"{ro_w.base()}/rows/bulk/preview",
            {"operation": "delete", "selector": {"all_rows": True}},
        ),
    ):
        r = await call
        assert r.status_code == 403 and "permission" in r.json()["detail"].lower(), r.text
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5


@pytest.mark.security
async def test_roles_limit_what_can_be_edited(wr, admin, new_user):
    viewer, editor = await new_user("Viewer"), await new_user("Editor")
    await grant(admin, wr.cid, viewer, "Viewer")
    await grant(admin, wr.cid, editor, "Editor")
    key = await wr.row_key("a")
    r = await viewer.post(f"{wr.base()}/rows", {"values": {"name": "v"}})
    assert r.status_code == 403
    ok = await editor.post(f"{wr.base()}/rows", {"values": {"name": "by-editor"}})
    assert ok.status_code == 201
    upd = await editor.patch(
        f"{wr.base()}/rows/{key}", {"values": {"qty": 3}, "expected": {"qty": 1}}
    )
    assert upd.status_code == 200
    # Editors may export but have no bulk-write permission; viewers cannot export at all
    prev = await editor.post(
        f"{wr.base()}/rows/bulk/preview",
        {"operation": "delete", "selector": {"all_rows": True}},
    )
    assert prev.status_code == 403
    assert (await editor.get(f"{wr.base()}/export")).status_code == 200
    assert (await viewer.get(f"{wr.base()}/export")).status_code == 403


# ------------------------------------------------------------------------------- production


@pytest.mark.security
async def test_production_deletes_need_the_object_name(wr, admin):
    prod = await writable_conn(admin, wr, environment="production")
    key = (await prod.rows(filters=[{"column": "name", "op": "eq", "value": "d"}]))["row_keys"][0]
    for confirm in (None, "wrong", wr.table.upper()):
        r = await admin.request("DELETE", f"{prod.base()}/rows/{key}", json={"confirm": confirm})
        assert r.status_code == 428 and r.json()["code"] == "confirmation_required", confirm
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE name = 'd'")[0][0] == 1
    ok = await admin.request("DELETE", f"{prod.base()}/rows/{key}", json={"confirm": wr.table})
    assert ok.status_code == 204
    denied = (
        await admin.get("audit/events", params={"connection_id": prod.cid, "result": "denied"})
    ).json()["events"]
    assert denied and denied[0]["action"] == "data.delete"
    # development connections do not ask
    key2 = await wr.row_key("e")
    assert (await admin.request("DELETE", f"{wr.base()}/rows/{key2}", json={})).status_code == 204


# ------------------------------------------------------------------------------- bulk


def sel_filters(*items):
    return {"filters": [{"column": c, "op": o, "value": v} for c, o, v in items]}


async def preview(wr, op, selector, values=None):
    body = {"operation": op, "selector": selector, **({"values": values} if values else {})}
    return await wr.api.post(f"{wr.base()}/rows/bulk/preview", body)


async def test_bulk_delete_requires_a_matching_preview(wr):
    sel = sel_filters(("qty", "gte", 4))
    p = await preview(wr, "delete", sel)
    assert p.status_code == 200, p.text
    pv = p.json()
    assert (
        pv["count"] == 2
        and len(pv["sample"]) == 2
        and pv["token"]
        and not pv["requires_confirmation"]
    )
    # executing without / with a mismatching token or count is refused
    base = {"selector": sel, "expected_count": 2, "token": pv["token"]}
    for bad in (
        {**base, "token": "nope"},
        {**base, "expected_count": 1},
        {**base, "selector": sel_filters(("qty", "gte", 1))},
    ):
        r = await wr.api.post(f"{wr.base()}/rows/bulk/delete", bad)
        assert r.status_code == 422, r.text
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5  # still untouched
    ok = await wr.api.post(f"{wr.base()}/rows/bulk/delete", base)
    assert ok.status_code == 200 and ok.json() == {"affected": 2}
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 3


async def test_bulk_delete_aborts_when_the_selection_changed_since_the_preview(wr):
    sel = sel_filters(("qty", "gte", 4))
    pv = (await preview(wr, "delete", sel)).json()
    wr.sql(f"INSERT INTO {wr.table} (name, qty) VALUES ('late', 100)")  # now 3 rows match
    r = await wr.api.post(
        f"{wr.base()}/rows/bulk/delete",
        {"selector": sel, "expected_count": pv["count"], "token": pv["token"]},
    )
    assert r.status_code == 409 and "changed since the preview" in r.json()["detail"]
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 6  # nothing was deleted


async def test_bulk_update_by_row_keys_and_by_filter(wr):
    page = await wr.rows(sort=[{"column": "name", "direction": "asc"}])
    keys = page["row_keys"][:2]
    sel = {"row_keys": keys}
    pv = (await preview(wr, "update", sel, {"qty": 500})).json()
    assert pv["count"] == 2
    r = await wr.api.post(
        f"{wr.base()}/rows/bulk/update",
        {"selector": sel, "values": {"qty": 500}, "expected_count": 2, "token": pv["token"]},
    )
    assert r.status_code == 200 and r.json()["affected"] == 2
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE qty = 500")[0][0] == 2
    # a token for one `values` cannot be replayed with other values
    pv2 = (await preview(wr, "update", sel_filters(("qty", "eq", 500)), {"qty": 1})).json()
    r = await wr.api.post(
        f"{wr.base()}/rows/bulk/update",
        {
            "selector": sel_filters(("qty", "eq", 500)),
            "values": {"qty": 0, "note": "evil"},
            "expected_count": 2,
            "token": pv2["token"],
        },
    )
    assert r.status_code == 422


async def test_bulk_needs_a_filter_or_explicit_all_rows(wr):
    assert (await preview(wr, "delete", {})).status_code == 422
    assert (await preview(wr, "delete", {"row_keys": [], "filters": []})).status_code == 422
    ok = await preview(wr, "delete", {"all_rows": True})
    assert ok.status_code == 200 and ok.json()["count"] == 5


async def test_bulk_is_capped(wr, admin, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "bulk_max_rows", 3)
    capped = await writable_conn(admin, wr)  # new connection => adapter picks up the new limit
    r = await admin.post(
        f"{capped.base()}/rows/bulk/preview",
        {"operation": "delete", "selector": {"all_rows": True}},
    )
    assert r.status_code == 422 and "more than 3" in r.json()["detail"]


@pytest.mark.security
async def test_bulk_selectors_cannot_inject(wr):
    for flt in (
        [("name", "eq", "x' OR '1'='1")],
        [("qty; DROP TABLE x", "eq", 1)],
        [("qty", "= 1 OR 1=1 --", 1)],
    ):
        r = await preview(wr, "delete", sel_filters(*flt))
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            assert r.json()["count"] == 0
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5


@pytest.mark.security
async def test_production_bulk_requires_confirmation_even_with_a_valid_preview(wr, admin):
    prod = await writable_conn(admin, wr, environment="production")
    sel = sel_filters(("qty", "gte", 4))
    pv = (
        await admin.post(
            f"{prod.base()}/rows/bulk/preview", {"operation": "delete", "selector": sel}
        )
    ).json()
    assert pv["requires_confirmation"] is True
    body = {"selector": sel, "expected_count": 2, "token": pv["token"]}
    assert (await admin.post(f"{prod.base()}/rows/bulk/delete", body)).status_code == 428
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5
    ok = await admin.post(f"{prod.base()}/rows/bulk/delete", {**body, "confirm": wr.table})
    assert ok.status_code == 200


async def test_bulk_is_audited_with_counts_but_not_values(wr):
    sel = sel_filters(("qty", "eq", 3))
    pv = (await preview(wr, "update", sel, {"note": "audit-marker-value"})).json()
    await wr.api.post(
        f"{wr.base()}/rows/bulk/update",
        {
            "selector": sel,
            "values": {"note": "audit-marker-value"},
            "expected_count": 1,
            "token": pv["token"],
        },
    )
    ev = (
        await wr.api.get(
            "audit/events", params={"connection_id": wr.cid, "action": "data.bulk_update"}
        )
    ).json()["events"][0]
    assert ev["detail"]["rows"] == 1 and ev["detail"]["columns"] == ["note"]
    assert "audit-marker-value" not in json.dumps(ev)


# ------------------------------------------------------------------------------- cells


async def test_truncated_cells_can_be_fetched_in_full(wr):
    page = await wr.rows(filters=[{"column": "name", "op": "eq", "value": "c"}])
    cols = [c["name"] for c in page["columns"]]
    cell = page["rows"][0][cols.index("note")]
    assert cell["$truncated"] and cell["length"] == 6000
    r = await wr.api.get(f"{wr.base()}/rows/{page['row_keys'][0]}/cells/note")
    assert r.status_code == 200
    assert r.json()["value"] == "x" * 6000 and r.json()["length"] == 6000


async def test_binary_cells_can_be_fetched(wr):
    page = await wr.rows(filters=[{"column": "name", "op": "eq", "value": "a"}])
    r = await wr.api.get(f"{wr.base()}/rows/{page['row_keys'][0]}/cells/photo")
    assert r.status_code == 200 and r.json()["value"]["base64"] == "3q2+7w=="


async def test_cell_fetch_validates_and_audits(wr):
    key = await wr.row_key("a")
    assert (await wr.api.get(f"{wr.base()}/rows/{key}/cells/ghost")).status_code == 422
    await wr.api.get(f"{wr.base()}/rows/{key}/cells/note")
    ev = (
        await wr.api.get(
            "audit/events", params={"connection_id": wr.cid, "action": "data.read.cell"}
        )
    ).json()["events"]
    assert ev and ev[0]["target"]["column"] == "note"


# ------------------------------------------------------------------------------- masking


async def mark(admin, wr, *fields):
    markers = [
        {
            "database": "shop",
            "schema": "public" if wr.engine == "postgresql" else "",
            "object": wr.table,
            "field": f,
        }
        for f in fields
    ]
    r = await admin.put(f"connections/{wr.cid}/sensitive", {"markers": markers})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.security
async def test_sensitive_columns_are_masked_for_users_without_permission(wr, admin, new_user):
    await mark(admin, wr, "note")
    editor = await new_user("Editor")  # has data.read but not data.read_sensitive
    await grant(admin, wr.cid, editor, "Editor")

    def note_of(page, name):
        cols = [c["name"] for c in page["columns"]]
        row = next(r for r in page["rows"] if r[cols.index("name")] == name)
        return row[cols.index("note")]

    seen = (await editor.get(f"{wr.base()}/rows", params={"limit": 100})).json()
    assert note_of(seen, "a") == {"$masked": True}
    assert note_of(seen, "d") is None  # NULL stays NULL
    assert "secret-a" not in json.dumps(seen)
    mine = await wr.rows()  # super admin has data.read_sensitive
    assert note_of(mine, "a") == "secret-a"


@pytest.mark.security
async def test_sensitive_columns_cannot_be_probed_through_filters_sort_or_search(
    wr, admin, new_user
):
    await mark(admin, wr, "note")
    editor = await new_user("Editor")
    await grant(admin, wr.cid, editor, "Editor")
    cases = (
        {"filters": json.dumps([{"column": "note", "op": "starts_with", "value": "secret-a"}])},
        {"sort": json.dumps([{"column": "note", "direction": "asc"}])},
        {"search": "secret", "search_columns": json.dumps(["name", "note"])},
    )
    for params in cases:
        r = await editor.get(f"{wr.base()}/rows", params=params)
        assert r.status_code == 403 and "sensitive" in r.json()["detail"], params
    key = await wr.row_key("a")
    assert (await editor.get(f"{wr.base()}/rows/{key}/cells/note")).status_code == 403
    # non-sensitive columns still work
    ok = await editor.get(
        f"{wr.base()}/rows",
        params={"filters": json.dumps([{"column": "name", "op": "eq", "value": "a"}])},
    )
    assert ok.status_code == 200


@pytest.mark.security
async def test_sensitive_columns_cannot_be_written_or_compared_without_permission(
    wr, admin, new_user
):
    await mark(admin, wr, "note")
    editor = await new_user("Editor")
    await grant(admin, wr.cid, editor, "Editor")
    key = await wr.row_key("a")
    r = await editor.patch(
        f"{wr.base()}/rows/{key}", {"values": {"note": "x"}, "expected": {"note": "secret-a"}}
    )
    assert r.status_code == 403
    assert (
        await editor.post(f"{wr.base()}/rows", {"values": {"name": "n", "note": "x"}})
    ).status_code == 403
    assert wr.sql(f"SELECT note FROM {wr.table} WHERE name = 'a'")[0][0] == "secret-a"


async def test_markers_are_validated_and_primary_keys_cannot_be_marked(wr, admin):
    base = {
        "database": "shop",
        "schema": "public" if wr.engine == "postgresql" else "",
        "object": wr.table,
    }
    pk = await admin.put(f"connections/{wr.cid}/sensitive", {"markers": [{**base, "field": "id"}]})
    assert pk.status_code == 422 and "Primary-key" in pk.json()["detail"]
    ghost = await admin.put(
        f"connections/{wr.cid}/sensitive", {"markers": [{**base, "field": "ghost"}]}
    )
    assert ghost.status_code == 422
    dup = await admin.put(
        f"connections/{wr.cid}/sensitive", {"markers": [{**base, "field": "note"}] * 2}
    )
    assert dup.status_code == 422
    listed = await mark(admin, wr, "note", "photo")
    assert {m["field"] for m in listed} == {"note", "photo"}
    assert {m["field"] for m in (await admin.get(f"connections/{wr.cid}/sensitive")).json()} == {
        "note",
        "photo",
    }
    assert (await mark(admin, wr)) == []  # clearing works


@pytest.mark.security
async def test_only_connection_editors_can_change_markers(wr, admin, new_user):
    viewer = await new_user("Viewer")
    await grant(admin, wr.cid, viewer, "Viewer")
    r = await viewer.put(f"connections/{wr.cid}/sensitive", {"markers": []})
    assert r.status_code == 403


# ------------------------------------------------------------------------------- export


def parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


async def test_csv_export_is_streamed_complete_and_audited(wr):
    r = await wr.api.get(f"{wr.base()}/export", params={"format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f'filename="{wr.table}.csv"' in r.headers["content-disposition"]
    rows = parse_csv(r.text)
    assert rows[0][:3] == ["id", "name", "qty"] and len(rows) == 6
    by_name = {x[1]: x for x in rows[1:]}
    assert by_name["b"][rows[0].index("big")] == ""  # NULL is an empty cell
    assert by_name["a"][rows[0].index("big")] == "9007199254740993"  # exact big integer
    ev = (
        await wr.api.get("audit/events", params={"connection_id": wr.cid, "action": "data.export"})
    ).json()["events"][0]
    assert (
        ev["result"] == "success" and ev["detail"]["rows"] == 5 and ev["detail"]["format"] == "csv"
    )


async def test_csv_export_neutralises_formula_injection(wr):
    for name in ("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(A1)"):
        await wr.api.post(f"{wr.base()}/rows", {"values": {"name": name}})
    await wr.api.post(f"{wr.base()}/rows", {"values": {"name": "plain", "price": "-5.50"}})
    rows = parse_csv((await wr.api.get(f"{wr.base()}/export", params={"format": "csv"})).text)
    names = {x[1] for x in rows[1:]}
    assert (
        "'=cmd|' /C calc'!A0" in names
        and "'+1+1" in names
        and "'-2+3" in names
        and "'@SUM(A1)" in names
    )
    plain = next(x for x in rows[1:] if x[1] == "plain")
    assert plain[rows[0].index("price")] == "-5.50"  # real negative numbers are not prefixed


async def test_json_and_ndjson_exports(wr):
    j = (await wr.api.get(f"{wr.base()}/export", params={"format": "json"})).json()
    assert len(j) == 5 and j[0]["name"] == "a" and j[0]["big"] == "9007199254740993"
    assert j[1]["big"] is None
    nd = (
        (await wr.api.get(f"{wr.base()}/export", params={"format": "ndjson"}))
        .text.strip()
        .splitlines()
    )
    assert len(nd) == 5 and json.loads(nd[2])["name"] == "c"
    assert json.loads(nd[2])["note"] == "x" * 6000  # exports are not truncated like grid cells


async def test_export_honours_filters_sort_and_columns(wr):
    q = {
        "format": "json",
        "filters": json.dumps([{"column": "qty", "op": "gte", "value": 3}]),
        "sort": json.dumps([{"column": "name", "direction": "desc"}]),
        "columns": json.dumps(["name", "qty"]),
    }
    j = (await wr.api.get(f"{wr.base()}/export", params=q)).json()
    assert [x["name"] for x in j] == ["e", "d", "c"] and set(j[0]) == {"name", "qty"}


@pytest.mark.security
async def test_export_masks_sensitive_columns_and_blocks_probing(wr, admin, new_user):
    await mark(admin, wr, "note")
    user = await new_user("Database Admin")  # has export + data.read_sensitive
    await grant(admin, wr.cid, user, "Database Admin")
    assert "secret-a" in (await user.get(f"{wr.base()}/export", params={"format": "csv"})).text
    # a custom role with export but without data.read_sensitive
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
    r = await admin.get(f"connections/{wr.cid}/grants")
    roles_now = [{"user_id": g["user_id"], "role_id": g["role_id"]} for g in r.json()] + [
        {"user_id": limited.user_id, "role_id": role["id"]}
    ]
    await admin.put(f"connections/{wr.cid}/grants", {"grants": roles_now})
    csv_text = (await limited.get(f"{wr.base()}/export", params={"format": "csv"})).text
    assert "secret-a" not in csv_text and "[masked]" in csv_text
    js = (await limited.get(f"{wr.base()}/export", params={"format": "json"})).json()
    assert js[0]["note"] == {"$masked": True} and js[3]["note"] is None
    probe = await limited.get(
        f"{wr.base()}/export",
        params={"filters": json.dumps([{"column": "note", "op": "contains", "value": "a"}])},
    )
    assert probe.status_code == 403
    ev = (
        await admin.get("audit/events", params={"connection_id": wr.cid, "action": "data.export"})
    ).json()["events"]
    assert any(e["detail"]["masked"] == ["note"] for e in ev)


async def test_export_is_size_limited(wr, admin, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "export_max_rows", 3)
    capped = await writable_conn(admin, wr)
    r = await admin.get(f"{capped.base()}/export")
    assert r.status_code == 422 and "exceed 3" in r.json()["detail"]
    ok = await admin.get(
        f"{capped.base()}/export",
        params={"filters": json.dumps([{"column": "qty", "op": "lte", "value": 3}])},
    )
    assert ok.status_code == 200 and len(parse_csv(ok.text)) == 4


async def test_export_of_bad_requests_fails_before_streaming(wr):
    for params in (
        {"columns": json.dumps(["ghost"])},
        {"format": "xml"},
        {"sort": json.dumps([{"column": "ghost", "direction": "asc"}])},
    ):
        assert (await wr.api.get(f"{wr.base()}/export", params=params)).status_code == 422
