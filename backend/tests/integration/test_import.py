"""Phase 5: import with validation and preview (CSV into tables, JSON into collections)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.integration.conftest import create_connection, grant
from tests.integration.test_admin import mongo_client

CSV_OK = "name,qty,price\nalpha,10,1.50\nbeta,20,2.50\ngamma,,3.50\n"


def csv_body(content: str = CSV_OK, **over) -> dict:
    return {"content": content, **over}


async def preview(wr, content: str = CSV_OK, api=None, **over):
    return await (api or wr.api).post(f"{wr.base()}/import/preview", csv_body(content, **over))


async def commit(wr, pv: dict, content: str = CSV_OK, api=None, headers=None, **over):
    body = csv_body(content, token=pv["token"], **over)
    return await (api or wr.api).post(f"{wr.base()}/import/commit", body, headers=headers or {})


def count(wr) -> int:
    return wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0]


async def test_preview_validates_without_changing_anything(wr):
    r = await preview(wr)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["rows"] == 3 and pv["valid_rows"] == 3 and pv["errors"] == [] and pv["token"]
    assert pv["columns"] == ["name", "qty", "price"] and pv["sample"][0] == ["alpha", "10", "1.50"]
    assert count(wr) == 5  # untouched


async def test_commit_imports_everything_and_maps_empty_to_null(wr):
    pv = (await preview(wr)).json()
    r = await commit(wr, pv)
    assert r.status_code == 200 and r.json() == {"imported": 3}, r.text
    assert count(wr) == 8
    row = wr.sql(f"SELECT name, qty, price FROM {wr.table} WHERE name = 'gamma'")[0]
    assert (
        row[1] is None and str(row[2]) == "3.50"
    )  # empty cell -> NULL (qty has a default but NULL wins)


async def test_empty_cells_can_be_kept_as_empty_text(wr):
    content = "name,note\nx,\n"
    pv = (await preview(wr, content, empty_as_null=False)).json()
    assert (await commit(wr, pv, content, empty_as_null=False)).status_code == 200
    assert wr.sql(f"SELECT note FROM {wr.table} WHERE name = 'x'")[0][0] == ""


async def test_preview_reports_every_kind_of_row_error_with_row_numbers(wr):
    content = "name,qty\nok,1\n,2\nbad,notanumber\nok2,3\n"
    r = await preview(wr, content)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["token"] is None  # nothing can be committed from an invalid file
    assert pv["rows"] == 4 and pv["valid_rows"] == 2
    assert [e["row"] for e in pv["errors"]] == [2, 3]
    assert all(e["message"] for e in pv["errors"])
    assert count(wr) == 5


async def test_constraint_violations_are_found_in_preview(wr):
    content = "id,name\n1,dup-pk\n900,fine\n"
    pv = (await preview(wr, content)).json()
    assert [e["row"] for e in pv["errors"]] == [1] and pv["token"] is None


async def test_commit_is_all_or_nothing(wr):
    content = "name,qty\nn1,1\nn2,2\n"
    pv = (await preview(wr, content)).json()
    assert pv["token"]
    wr.sql(f"ALTER TABLE {wr.table} ADD CONSTRAINT uq_name UNIQUE (name)")
    wr.sql(f"INSERT INTO {wr.table} (name) VALUES ('n2')")  # appears after the preview
    r = await commit(wr, pv, content)
    assert (
        r.status_code == 422
        and "Row 2" in r.json()["detail"]
        and "Nothing was imported" in r.json()["detail"]
    )
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE name = 'n1'")[0][0] == 0


async def test_the_token_binds_the_exact_payload(wr):
    pv = (await preview(wr)).json()
    for body in (CSV_OK + "extra,1,1\n", CSV_OK.replace("alpha", "ALPHA")):
        assert (await commit(wr, pv, body)).status_code == 422
    assert (await commit(wr, pv, delimiter=";")).status_code in (400, 422)
    assert (await commit(wr, {"token": "garbage"})).status_code == 422
    assert count(wr) == 5
    other = await preview(wr, "name\nz\n")
    assert (await commit(wr, other.json(), CSV_OK)).status_code == 422  # token from another file


async def test_column_mapping_delimiters_and_headerless_files(wr):
    content = "Full Name;Amount;ignored\nmapped;7;zzz\n"
    mapping = {"Full Name": "name", "Amount": "qty"}
    pv = (await preview(wr, content, delimiter=";", mapping=mapping)).json()
    assert pv["columns"] == ["name", "qty"]
    assert (await commit(wr, pv, content, delimiter=";", mapping=mapping)).status_code == 200
    assert wr.sql(f"SELECT qty FROM {wr.table} WHERE name = 'mapped'")[0][0] == 7

    nohdr = "hdrless\t8\n"
    kw = {"delimiter": "tab", "has_header": False, "mapping": {"0": "name", "1": "qty"}}
    pv = (await preview(wr, nohdr, **kw)).json()
    assert (await commit(wr, pv, nohdr, **kw)).status_code == 200
    assert (
        await preview(wr, nohdr, delimiter="tab", has_header=False)
    ).status_code == 422  # needs a mapping
    assert (
        await preview(wr, nohdr, delimiter="tab", has_header=False, mapping={"5": "name"})
    ).status_code == 422


@pytest.mark.parametrize(
    "content,over",
    [
        ("nope,qty\n1,2\n", {}),  # unknown column
        ("name,name\na,b\n", {}),  # duplicate header
        ("name,qty\na\n", {}),  # ragged
        ("name\n", {}),  # no data rows
        ("", {}),
        ("name\na\x00b\n", {}),
        ("photo\ndeadbeef\n", {}),  # binary column
        ("name,qty\na,1\n", {"mapping": {"zzz": "name"}}),
        ("name,qty\na,1\n", {"mapping": {"name": "name", "qty": "name"}}),  # one column twice
        ('name\n"unterminated\n', {}),
    ],
)
async def test_invalid_files_are_rejected(wr, content, over):
    r = await preview(wr, content, **over)
    assert r.status_code == 422, (content, r.text)
    assert count(wr) == 5


async def test_values_are_data_never_sql(wr):
    evil = "x'); DROP TABLE " + wr.table + "; --"
    content = f'name\n"{evil}"\n"=1+1"\n'
    pv = (await preview(wr, content)).json()
    assert (await commit(wr, pv, content)).status_code == 200
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE name = %s", (evil,))[0][0] == 1
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table} WHERE name = '=1+1'")[0][0] == 1


async def test_row_limit(wr, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "import_max_rows", 2)
    assert (await preview(wr)).status_code == 422


async def test_tables_without_primary_key_can_be_imported_but_views_cannot(wr):
    r = await wr.api.post(f"{wr.base(wr.table + '_nopk')}/import/preview", csv_body("a,b\n9,z\n"))
    assert r.status_code == 200 and r.json()["valid_rows"] == 1
    r = await wr.api.post(f"{wr.base(wr.table + '_v')}/import/preview", csv_body("name\nq\n"))
    assert r.status_code == 422


async def test_unknown_tables_are_404(wr):
    assert (
        await wr.api.post(f"{wr.base('nope_nope')}/import/preview", csv_body())
    ).status_code == 404


# ------------------------------------------------------------------------- authorization


@pytest.mark.security
async def test_import_permission_and_read_only_enforced(
    wr, admin, new_user, pg_managed, mysql_managed
):
    editor = await new_user("Editor")  # may insert, but has no import permission
    await grant(admin, wr.cid, editor, "Editor")
    assert (await preview(wr, api=editor)).status_code == 403
    viewer = await new_user("Viewer")
    await grant(admin, wr.cid, viewer, "Viewer")
    assert (await preview(wr, api=viewer)).status_code == 403
    outsider = await new_user("Database Admin")
    assert (await preview(wr, api=outsider)).status_code == 404
    ro = await create_connection(admin, wr.engine, wr.info)  # read-only
    r = await admin.post(
        f"connections/{ro['id']}/databases/shop/{'schemas/public/' if wr.engine == 'postgresql' else ''}tables/{wr.table}/import/preview",
        csv_body(),
    )
    assert r.status_code == 403


@pytest.mark.security
async def test_sensitive_columns_cannot_be_imported_without_the_permission(wr, admin, new_user):
    role = await admin.post(
        "roles",
        {
            "name": f"imp-{uuid.uuid4().hex[:6]}",
            "permissions": [
                "connection.view",
                "metadata.view",
                "data.read",
                "data.insert",
                "export.import",
            ],
        },
    )
    assert role.status_code == 201, role.text
    user = await new_user("Viewer")
    await admin.patch(f"users/{user.user_id}", {"role_id": role.json()["id"]})
    await user.login(user.username, "Us3r-Passw0rd-Long!")
    r = await admin.get(f"connections/{wr.cid}/grants")
    grants = [{"user_id": g["user_id"], "role_id": g["role_id"]} for g in r.json()]
    grants.append({"user_id": user.user_id, "role_id": role.json()["id"]})
    assert (await admin.put(f"connections/{wr.cid}/grants", {"grants": grants})).status_code == 200
    schema = "public" if wr.engine == "postgresql" else ""
    await admin.put(
        f"connections/{wr.cid}/sensitive",
        {"markers": [{"database": "shop", "schema": schema, "object": wr.table, "field": "note"}]},
    )
    content = "name,note\ns,secret\n"
    assert (await preview(wr, content, api=user)).status_code == 403
    assert (await preview(wr, "name\nfine\n", api=user)).status_code == 200


async def test_production_commit_needs_the_table_name(wr, admin):
    from tests.integration.conftest import writable_conn

    prod = await writable_conn(admin, wr, environment="production")
    pv = await preview(prod)
    assert pv.json()["requires_confirmation"] is True
    r = await commit(prod, pv.json())
    assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert (await commit(prod, pv.json(), confirm=prod.table)).status_code == 200


async def test_import_can_require_a_second_person(wr, admin, new_user):
    requester, reviewer = await new_user("Database Admin"), await new_user("Database Admin")
    await grant(admin, wr.cid, requester, "Database Admin")
    await grant(admin, wr.cid, reviewer, "Database Admin")
    await admin.put(
        "governance/policies", {"connection_id": wr.cid, "operations": ["import.commit"]}
    )
    pv = (await preview(wr, api=requester)).json()
    r = await commit(wr, pv, api=requester)
    assert r.status_code == 428 and r.json()["code"] == "approval_required"
    aid = r.json()["approval_id"]
    assert count(wr) == 5
    await reviewer.post(f"approvals/{aid}/approve", {})
    ok = await commit(wr, pv, api=requester, headers={"Approval-Id": aid})
    assert ok.status_code == 200, ok.text
    assert count(wr) == 8


async def test_import_is_audited_without_row_data(wr, admin):
    content = "name\nsecret-audit-value\n"
    pv = (await preview(wr, content)).json()
    await commit(wr, pv, content)
    events = (
        await admin.get("audit/events", params={"connection_id": wr.cid, "limit": 20})
    ).json()["events"]
    acts = {e["action"] for e in events}
    assert {"import.preview", "import.commit"} <= acts
    assert "secret-audit-value" not in str(events)
    c = next(e for e in events if e["action"] == "import.commit")
    assert c["detail"]["rows"] == 1 and c["detail"]["digest"]


# ----------------------------------------------------------------------------- MongoDB


@pytest_asyncio.fixture
async def mimp(admin, mongo_managed):
    name = f"imp_{uuid.uuid4().hex[:8]}"
    c = mongo_client(mongo_managed)
    c[name].people.insert_one({"_id": 1, "name": "existing"})
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    yield conn["id"], name, c
    c.drop_database(name)
    c.close()


def murl(cid, db, what):
    return f"connections/{cid}/mongo/databases/{db}/collections/people/import/{what}"


JSON_DOCS = (
    '[{"_id": 10, "name": "a", "born": {"$date": "2020-01-02T03:04:05Z"}, '
    '"bal": {"$numberDecimal": "12.30"}, "tags": ["x", "y"], "addr": {"city": "Rome"}},'
    ' {"_id": 11, "name": "b"}]'
)


async def test_mongo_preview_and_commit_preserve_types(admin, mimp):
    cid, db, c = mimp
    pv = (await admin.post(murl(cid, db, "preview"), {"content": JSON_DOCS})).json()
    assert pv["documents"] == 2 and pv["valid_documents"] == 2 and pv["token"] and not pv["errors"]
    assert c[db].people.count_documents({}) == 1
    r = await admin.post(murl(cid, db, "commit"), {"content": JSON_DOCS, "token": pv["token"]})
    assert r.status_code == 200 and r.json() == {"imported": 2}, r.text
    doc = c[db].people.find_one({"_id": 10})
    import datetime as dt

    import bson

    assert isinstance(doc["born"], dt.datetime) and isinstance(doc["bal"], bson.Decimal128)
    assert str(doc["bal"]) == "12.30" and doc["addr"] == {"city": "Rome"}


async def test_mongo_ndjson(admin, mimp):
    cid, db, c = mimp
    nd = '{"_id": 20, "n": 1}\n\n{"_id": 21, "n": 2}\n'
    pv = (await admin.post(murl(cid, db, "preview"), {"content": nd, "format": "ndjson"})).json()
    assert pv["documents"] == 2 and pv["token"]
    r = await admin.post(
        murl(cid, db, "commit"), {"content": nd, "format": "ndjson", "token": pv["token"]}
    )
    assert r.status_code == 200 and c[db].people.count_documents({}) == 3


async def test_mongo_preview_finds_every_problem(admin, mimp):
    cid, db, _ = mimp
    docs = '[{"_id": 1, "n": 1}, {"_id": 50, "$bad": 1}, {"_id": 51}, {"_id": 51}, 5]'
    pv = (await admin.post(murl(cid, db, "preview"), {"content": docs})).json()
    assert pv["token"] is None and pv["errors"]
    text = " ".join(e["message"] for e in pv["errors"])
    assert "Duplicate _id" in text or "already exist" in text
    assert any(e["index"] == 2 for e in pv["errors"])  # the $-field
    clash = (await admin.post(murl(cid, db, "preview"), {"content": '[{"_id": 1}]'})).json()
    assert clash["token"] is None and "already exist" in clash["errors"][0]["message"]


@pytest.mark.parametrize("content", ["", "{", "[]", "{}", '"x"', "[1,", "null"])
async def test_mongo_malformed_files_are_rejected(admin, mimp, content):
    cid, db, c = mimp
    r = await admin.post(murl(cid, db, "preview"), {"content": content})
    assert r.status_code == 422, (content, r.text)
    assert c[db].people.count_documents({}) == 1


async def test_mongo_commit_needs_a_matching_token_and_valid_content(admin, mimp):
    cid, db, c = mimp
    pv = (await admin.post(murl(cid, db, "preview"), {"content": JSON_DOCS})).json()
    changed = JSON_DOCS.replace('"b"', '"c"')
    assert (
        await admin.post(murl(cid, db, "commit"), {"content": changed, "token": pv["token"]})
    ).status_code == 422
    assert (
        await admin.post(murl(cid, db, "commit"), {"content": JSON_DOCS, "token": "x"})
    ).status_code == 422
    assert c[db].people.count_documents({}) == 1


async def test_mongo_limits_and_permissions(admin, mimp, new_user, monkeypatch, mongo_managed):
    from app.config import get_settings

    cid, db, c = mimp
    monkeypatch.setattr(get_settings(), "import_max_documents", 1)
    assert (await admin.post(murl(cid, db, "preview"), {"content": JSON_DOCS})).status_code == 422
    monkeypatch.undo()
    editor = await new_user("Editor")
    await grant(admin, cid, editor, "Editor")
    assert (await editor.post(murl(cid, db, "preview"), {"content": JSON_DOCS})).status_code == 403
    ro = (await create_connection(admin, "mongodb", mongo_managed))["id"]
    assert (await admin.post(murl(ro, db, "preview"), {"content": JSON_DOCS})).status_code == 403
    assert (
        await admin.post(murl(cid, db, "preview").replace("people", "nope"), {"content": JSON_DOCS})
    ).status_code == 404


async def test_mongo_production_confirmation_and_approval(admin, new_user, mongo_managed):
    name = f"imp_{uuid.uuid4().hex[:8]}"
    c = mongo_client(mongo_managed)
    c[name].people.insert_one({"_id": 1})
    try:
        cid = (
            await create_connection(
                admin, "mongodb", mongo_managed, read_only=False, environment="production"
            )
        )["id"]
        requester, reviewer = await new_user("Database Admin"), await new_user("Database Admin")
        await grant(admin, cid, requester, "Database Admin")
        await grant(admin, cid, reviewer, "Database Admin")
        await admin.put(
            "governance/policies", {"connection_id": cid, "operations": ["import.commit"]}
        )
        pv = (await requester.post(murl(cid, name, "preview"), {"content": JSON_DOCS})).json()
        body = {"content": JSON_DOCS, "token": pv["token"]}
        r = await requester.post(murl(cid, name, "commit"), body)
        assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
        r = await requester.post(murl(cid, name, "commit"), {**body, "confirm": "people"})
        assert r.json()["code"] == "approval_required"
        aid = r.json()["approval_id"]
        await reviewer.post(f"approvals/{aid}/approve", {})
        ok = await requester.post(
            murl(cid, name, "commit"), {**body, "confirm": "people"}, headers={"Approval-Id": aid}
        )
        assert ok.status_code == 200, ok.text
        assert c[name].people.count_documents({}) == 3
    finally:
        c.drop_database(name)
        c.close()
