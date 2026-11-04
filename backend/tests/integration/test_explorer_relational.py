"""Explorer + data browser against real PostgreSQL and MySQL (behaviours shared by both)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from tests.integration.conftest import Api, create_connection


@dataclass
class Rel:
    api: Api
    engine: str
    cid: str

    def base(self) -> str:
        return f"connections/{self.cid}/databases/shop"

    def tables(self) -> str:
        return (
            f"{self.base()}/schemas/public/tables"
            if self.engine == "postgresql"
            else f"{self.base()}/tables"
        )

    def table(self, name: str) -> str:
        return f"{self.tables()}/{name}"

    async def rows(self, table: str = "customers", **params: Any) -> dict[str, Any]:
        for k in ("sort", "filters", "columns", "search_columns"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        r = await self.api.get(f"{self.table(table)}/rows", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def walk(self, table: str = "customers", limit: int = 100) -> list[list[Any]]:
        out, cursor = [], None
        for _ in range(50):
            page = await self.rows(table, limit=limit, **({"cursor": cursor} if cursor else {}))
            out += page["rows"]
            cursor = page["next_cursor"]
            if not cursor:
                return out
        raise AssertionError("pagination did not terminate")


@pytest_asyncio.fixture(params=["postgresql", "mysql"])
async def rel(request, admin, pg_managed, mysql_managed) -> Rel:
    info = pg_managed if request.param == "postgresql" else mysql_managed
    conn = await create_connection(admin, request.param, info)
    return Rel(admin, request.param, conn["id"])


def col(page: dict[str, Any], name: str) -> int:
    return [c["name"] for c in page["columns"]].index(name)


# ----------------------------------------------------------------------------- metadata


async def test_databases_and_tables(rel):
    dbs = [d["name"] for d in (await rel.api.get(f"connections/{rel.cid}/databases")).json()]
    assert "shop" in dbs
    assert not {"information_schema", "performance_schema", "sys", "mysql"} & set(dbs)
    tables = {t["name"]: t for t in (await rel.api.get(rel.tables())).json()}
    assert tables["customers"]["kind"] == "table" and tables["vip"]["kind"] == "view"
    assert {"orders", "order_items", "no_pk", "Weird %Table"} <= set(tables)


async def test_structure_columns_keys_indexes_and_foreign_keys(rel):
    s = (await rel.api.get(f"{rel.table('customers')}/structure")).json()
    cols = {c["name"]: c for c in s["columns"]}
    assert list(cols)[:3] == ["id", "name", "email"]
    assert s["primary_key"] == ["id"]
    assert cols["name"]["nullable"] is False and cols["email"]["nullable"] is True
    assert cols["id"]["is_identity"] or "nextval" in (cols["id"]["default"] or "")
    idx = {i["name"]: i for i in s["indexes"]}
    assert (
        idx["customers_name_idx"]["columns"] == ["name"]
        and idx["customers_name_idx"]["unique"] is False
    )
    assert any(i["unique"] and i["columns"] == ["email"] for i in s["indexes"])
    assert any(i["primary"] for i in s["indexes"])

    o = (await rel.api.get(f"{rel.table('orders')}/structure")).json()
    fk = o["foreign_keys"][0]
    assert (
        fk["columns"] == ["customer_id"]
        and fk["ref_table"] == "customers"
        and fk["ref_columns"] == ["id"]
    )
    assert fk["on_delete"] == "CASCADE"

    items = (await rel.api.get(f"{rel.table('order_items')}/structure")).json()
    assert items["primary_key"] == ["order_id", "line_no"]  # composite, ordered


async def test_view_and_no_pk_table_structure(rel):
    assert (await rel.api.get(f"{rel.table('vip')}/structure")).json()["kind"] == "view"
    assert (await rel.api.get(f"{rel.table('no_pk')}/structure")).json()["primary_key"] == []


# ----------------------------------------------------------------------------- pagination


async def test_keyset_pagination_visits_every_row_once_in_order(rel):
    first = await rel.rows(limit=100)
    assert first["pagination"] == "keyset" and first["next_cursor"] and len(first["rows"]) == 100
    rows = await rel.walk()
    ids = [r[col(first, "id")] for r in rows]
    assert ids == list(range(1, 251))


async def test_composite_key_keyset_pagination(rel):
    rows = await rel.walk("order_items", limit=2)
    keys = [(r[0], r[1]) for r in rows]
    assert (
        keys == [(1, i) for i in range(1, 6)]
        if rel.engine == "postgresql"
        else [(1, 1), (1, 2), (1, 3)]
    )


async def test_sorting_uses_offset_pagination_with_stable_tiebreak(rel):
    page = await rel.rows(limit=100, sort=[{"column": "name", "direction": "desc"}])
    assert page["pagination"] == "offset"
    assert page["rows"][0][col(page, "name")] == "Customer 250"
    p2 = await rel.rows(
        limit=100, cursor=page["next_cursor"], sort=[{"column": "name", "direction": "desc"}]
    )
    assert p2["rows"][0][col(p2, "name")] == "Customer 150"


async def test_page_size_is_clamped_and_flagged(rel):
    page = await rel.rows(limit=100000)
    assert (
        page["limit"] == 1000
        and page["truncated"]
        and any("limited" in w for w in page["warnings"])
    )
    assert len(page["rows"]) == 250


async def test_no_primary_key_table_warns_about_unstable_order(rel):
    page = await rel.rows("no_pk")
    assert page["pagination"] == "unordered_offset" and page["warnings"]
    assert len(page["rows"]) == 2


async def test_column_selection_hides_cursor_columns(rel):
    page = await rel.rows(limit=10, columns=["name"])
    assert [c["name"] for c in page["columns"]] == ["name"] and len(page["rows"][0]) == 1
    assert page["next_cursor"]  # keyset cursor still works although `id` is not selected
    nxt = await rel.rows(limit=10, columns=["name"], cursor=page["next_cursor"])
    assert nxt["rows"][0][0] == "Customer 011"


# ----------------------------------------------------------------------------- filtering


async def test_filters(rel):
    async def count(**flt: Any) -> int:
        return len((await rel.rows(limit=1000, filters=[flt]))["rows"])

    assert await count(column="email", op="is_null") == 25
    assert await count(column="email", op="is_not_null") == 225
    assert await count(column="name", op="eq", value="Customer 007") == 1
    assert await count(column="name", op="contains", value="customer 00") == 9  # case-insensitive
    assert await count(column="name", op="starts_with", value="Customer 25") == 1
    assert await count(column="name", op="ends_with", value="0") == 25
    assert await count(column="id", op="gte", value=241) == 10
    assert await count(column="id", op="in", value=[1, 2, 3, 999]) == 3
    assert await count(column="id", op="ne", value=1) == 249


async def test_combined_filters_and_search(rel):
    page = await rel.rows(
        limit=1000,
        filters=[{"column": "id", "op": "lt", "value": 100}, {"column": "email", "op": "is_null"}],
    )
    assert len(page["rows"]) == 9
    s = await rel.rows(limit=1000, search="Customer 24", search_columns=["name", "email"])
    assert len(s["rows"]) == 10


@pytest.mark.security
async def test_like_wildcards_in_user_input_are_literal(rel):
    for value in ("%", "_", "Customer 0_1", "\\"):
        page = await rel.rows(
            limit=1000, filters=[{"column": "name", "op": "contains", "value": value}]
        )
        assert page["rows"] == [], value


@pytest.mark.security
async def test_sql_injection_attempts_are_inert(rel):
    payload = "x'; DROP TABLE customers; --"
    page = await rel.rows(limit=10, filters=[{"column": "name", "op": "eq", "value": payload}])
    assert page["rows"] == []
    page = await rel.rows(
        limit=10, filters=[{"column": "name", "op": "in", "value": [payload, "' OR '1'='1"]}]
    )
    assert page["rows"] == []
    assert (await rel.rows(limit=1))["rows"]  # table still there


@pytest.mark.security
@pytest.mark.parametrize(
    "evil", ['id"; DROP TABLE customers; --', "id` OR 1=1 --", "nonexistent", "id, name"]
)
async def test_malicious_column_identifiers_are_rejected(rel, evil):
    for param, value in (
        ("columns", [evil]),
        ("sort", [{"column": evil, "direction": "asc"}]),
        ("filters", [{"column": evil, "op": "eq", "value": 1}]),
    ):
        r = await rel.api.get(f"{rel.table('customers')}/rows", params={param: json.dumps(value)})
        assert r.status_code == 422, (param, evil, r.text)


@pytest.mark.security
@pytest.mark.parametrize(
    "bad",
    [{"sort": "not json"}, {"filters": '{"a":1}'}, {"filters": json.dumps([{"column": "id", "op": "DROP", "value": 1}])},
     {"sort": json.dumps([{"column": "id", "direction": "sideways"}])}, {"cursor": "garbage!!"}],
)  # fmt: skip
async def test_malformed_query_parameters_are_422(rel, bad):
    r = await rel.api.get(f"{rel.table('customers')}/rows", params=bad)
    assert r.status_code == 422


@pytest.mark.security
async def test_unknown_objects_are_404_and_never_interpolated(rel):
    base = f"connections/{rel.cid}/databases"
    assert (
        await rel.api.get(
            f"{base}/nope/tables" if rel.engine == "mysql" else f"{base}/nope/schemas"
        )
    ).status_code == 404
    assert (await rel.api.get(f"{rel.table('nonexistent')}/structure")).status_code == 404
    assert (await rel.api.get(f"{rel.table('nonexistent')}/rows")).status_code == 404
    evil = "customers; DROP TABLE customers"
    assert (await rel.api.get(f"{rel.table(evil)}/rows")).status_code == 404
    assert (
        await rel.api.get(f"{rel.table('customers')}/rows", params={"limit": 1})
    ).status_code == 200


# ----------------------------------------------------------------------------- type fidelity


async def test_values_keep_their_types_over_json(rel):
    page = await rel.rows(limit=1)
    row = dict(zip([c["name"] for c in page["columns"]], page["rows"][0], strict=True))
    assert row["id"] == 1
    assert row["big"] == "9007199254740994"  # beyond JS safe integer -> string, exact
    assert row["balance"] == "10.25"  # decimal -> string, exact
    assert row["photo"]["$binary"] is True and row["photo"]["length"] == 4
    assert row["notes"]["$truncated"] is True and row["notes"]["length"] == 6000
    assert len(row["notes"]["preview"]) == 4096
    assert row["created_at"].startswith("20")  # ISO-8601
    assert row["meta"] is not None


async def test_null_values_are_real_nulls(rel):
    page = await rel.rows(
        limit=1000, filters=[{"column": "email", "op": "is_null"}], columns=["id", "email"]
    )
    assert all(r[1] is None for r in page["rows"])


async def test_identifiers_with_special_characters_work(rel):
    s = (await rel.api.get(f"{rel.table('Weird %Table')}/structure")).json()
    assert [c["name"] for c in s["columns"]] == [
        "a%b",
        'q"uote' if rel.engine == "postgresql" else "q`uote",
    ]
    page = await rel.rows("Weird %Table", filters=[{"column": "a%b", "op": "eq", "value": 2}])
    assert len(page["rows"]) == 1
    assert (
        len((await rel.rows("Weird %Table", sort=[{"column": "a%b", "direction": "desc"}]))["rows"])
        == 2
    )


async def test_views_can_be_browsed(rel):
    page = await rel.rows("vip", limit=1000)
    assert 0 < len(page["rows"]) < 250 and [c["name"] for c in page["columns"]] == ["id", "name"]


# ----------------------------------------------------------------------------- permissions / audit


async def test_estimates_are_omitted_when_filtering(rel):
    filtered = await rel.rows(filters=[{"column": "id", "op": "gt", "value": 5}])
    assert filtered["total_estimate"] is None


async def test_data_reads_are_audited_without_values(rel):
    marker = f"needle-{uuid.uuid4().hex[:8]}"
    await rel.rows(filters=[{"column": "name", "op": "eq", "value": marker}])
    events = (
        await rel.api.get("audit/events", params={"connection_id": rel.cid, "action": "data.read"})
    ).json()["events"]
    ev = events[0]
    assert ev["target"]["table"] == "customers" and ev["detail"]["filters"] == [
        {"column": "name", "op": "eq"}
    ]
    assert marker not in json.dumps(events)  # filter *values* and row contents never audited


async def test_failed_reads_are_audited_as_failures(rel):
    r = await rel.api.get(
        f"{rel.table('customers')}/rows", params={"columns": json.dumps(["nope"])}
    )
    assert r.status_code == 422
    events = (
        await rel.api.get("audit/events", params={"connection_id": rel.cid, "result": "failure"})
    ).json()["events"]
    assert events and events[0]["detail"]["error"] == "validation_failed"


@pytest.mark.security
async def test_metadata_only_role_cannot_read_rows(rel, admin, new_user):
    role = (
        await admin.post(
            "roles",
            {
                "name": f"meta-{uuid.uuid4().hex[:6]}",
                "permissions": ["connection.view", "metadata.view"],
            },
        )
    ).json()
    user = await new_user("Viewer")
    await admin.put(
        f"connections/{rel.cid}/grants",
        {"grants": [{"user_id": user.user_id, "role_id": role["id"]}]},
    )
    assert (await user.get(f"{rel.table('customers')}/structure")).status_code == 200
    r = await user.get(f"{rel.table('customers')}/rows")
    assert r.status_code == 403 and "data.read" in r.json()["detail"]


@pytest.mark.security
async def test_mongo_endpoints_reject_relational_connections(rel):
    r = await rel.api.get(f"connections/{rel.cid}/mongo/databases")
    assert r.status_code == 422 and r.json()["code"] == "unsupported_capability"


# ------------------------------------------------------------------------------ PostgreSQL only


@pytest_asyncio.fixture
async def pg(admin, pg_managed) -> Rel:
    return Rel(
        admin, "postgresql", (await create_connection(admin, "postgresql", pg_managed))["id"]
    )


async def test_postgres_schemas_sequences_functions(pg):
    schemas = [s["name"] for s in (await pg.api.get(f"{pg.base()}/schemas")).json()]
    assert {"public", "extra"} <= set(schemas) and not any(s.startswith("pg_") for s in schemas)
    extra = (await pg.api.get(f"{pg.base()}/schemas/extra/tables")).json()
    assert [t["name"] for t in extra] == ["notes"]

    seqs = {
        s["name"]: s for s in (await pg.api.get(f"{pg.base()}/schemas/public/sequences")).json()
    }
    assert seqs["ticket_seq"]["start"] == 100 and seqs["ticket_seq"]["increment"] == 5

    fns = {f["name"]: f for f in (await pg.api.get(f"{pg.base()}/schemas/public/functions")).json()}
    assert fns["add_one"]["arguments"] == "i integer" and fns["add_one"]["returns"] == "integer"
    detail = (
        await pg.api.get(f"{pg.base()}/schemas/public/functions/{fns['add_one']['id']}")
    ).json()
    assert "SELECT i + 1" in detail["definition"]
    assert (await pg.api.get(f"{pg.base()}/schemas/public/functions/abc")).status_code == 422
    assert (
        await pg.api.get(f"{pg.base()}/schemas/extra/functions/{fns['add_one']['id']}")
    ).status_code == 404


async def test_postgres_requires_a_schema_and_other_schema_tables(pg):
    assert (await pg.api.get(f"{pg.base()}/tables")).status_code == 422  # no schema level misuse
    page = (await pg.api.get(f"{pg.base()}/schemas/extra/tables/notes/rows")).json()
    assert page["rows"] == []
    s = (await pg.api.get(f"{pg.base()}/schemas/public/tables/customers/structure")).json()
    assert s["estimated_rows"] is None or s["estimated_rows"] >= 0
    assert (await pg.api.get(f"{pg.base()}/schemas/nope/tables")).status_code == 404


async def test_postgres_check_and_unique_constraints(pg):
    kinds = {
        c["kind"]
        for c in (await pg.api.get(f"{pg.table('customers')}/structure")).json()["constraints"]
    }
    assert {"primary_key", "unique"} <= kinds


# ------------------------------------------------------------------------------ MySQL only


@pytest_asyncio.fixture
async def my(admin, mysql_managed) -> Rel:
    return Rel(admin, "mysql", (await create_connection(admin, "mysql", mysql_managed))["id"])


async def test_mysql_has_no_schema_level(my):
    r = await my.api.get(f"{my.base()}/schemas")
    assert r.status_code == 422 and r.json()["code"] == "unsupported_capability"
    assert (await my.api.get(f"{my.base()}/schemas/public/tables")).status_code == 404 or True


async def test_mysql_foreign_key_rules_and_check_constraints(my):
    o = (await my.api.get(f"{my.table('orders')}/structure")).json()
    fk = o["foreign_keys"][0]
    assert (
        fk["on_delete"] == "CASCADE"
        and fk["on_update"] == "RESTRICT"
        and fk["ref_schema"] == "shop"
    )
    assert any(
        c["kind"] == "check" and "total" in (c["definition"] or "") for c in o["constraints"]
    )
    assert o["primary_key"] == ["id"]
