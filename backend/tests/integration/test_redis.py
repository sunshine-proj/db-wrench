"""Redis (ADR-0019): databases, key browsing, typed values, editing with versions, console."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import redis as redis_lib

from app.connections.network import NetworkPolicy
from app.database.adapters.redis import RedisAdapter, encode_key
from app.database.core.base import ConnectionSpec, DbType, Limits, TlsMode
from app.errors import ConnectionFailed, NetworkPolicyViolation, QueryFailed
from tests.integration.conftest import REDIS_PASSWORD, create_connection, grant

PREFIX = "connections"


def kid(name: str | bytes) -> str:
    return encode_key(name.encode() if isinstance(name, str) else name)


class Rd:
    def __init__(self, api: Any, cid: str, info: dict[str, Any]):
        self.api, self.cid, self.info = api, cid, info

    def url(self, path: str = "") -> str:
        return f"{PREFIX}/{self.cid}/redis{path}"

    def direct(self, db: int = 0) -> redis_lib.Redis:
        return redis_lib.Redis(
            host=self.info["host"], port=self.info["port"], password=REDIS_PASSWORD, db=db
        )

    async def keys(self, **params: Any) -> dict[str, Any]:
        r = await self.api.get(self.url("/databases/0/keys"), params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def key(self, name: str | bytes, **params: Any) -> dict[str, Any]:
        r = await self.api.get(self.url(f"/databases/0/keys/{kid(name)}"), params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def cmd(self, line: str, db: str = "0", **kw: Any):
        return await self.api.post(
            self.url("/commands/execute"), {"database": db, "command": line, **kw}
        )


@pytest.fixture
async def rd(admin, redis_managed) -> Rd:
    c = await create_connection(admin, "redis", redis_managed, read_only=False)
    return Rd(admin, c["id"], redis_managed)


@pytest.fixture
async def rd_ro(admin, redis_managed) -> Rd:
    c = await create_connection(admin, "redis", redis_managed)  # read-only by default
    return Rd(admin, c["id"], redis_managed)


def scratch(name: str = "t") -> str:
    return f"scratch:{name}:{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------------ explorer


async def test_connection_test_and_capabilities(rd):
    r = await rd.api.post(f"{PREFIX}/{rd.cid}/test", {})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["server_version"].startswith(("Redis", "Valkey"))
    caps = (await rd.api.get(f"{PREFIX}/{rd.cid}")).json()["capabilities"]
    assert caps["family"] == "keyvalue" and caps["engine"] == "redis"


async def test_relational_and_mongo_endpoints_refuse_a_redis_connection(rd):
    r = await rd.api.get(f"{PREFIX}/{rd.cid}/databases")
    assert r.status_code in (400, 409, 422) and "keyvalue" in r.text


async def test_databases(rd):
    dbs = (await rd.api.get(rd.url("/databases"))).json()
    assert [d["name"] for d in dbs][:2] == ["0", "1"] and len(dbs) >= 2
    assert dbs[0]["keys"] > 300 and dbs[1]["keys"] == 1
    assert (await rd.api.get(rd.url("/databases/99/keys"))).status_code == 404
    assert (await rd.api.get(rd.url("/databases/abc/keys"))).status_code == 404
    assert (await rd.api.get(rd.url("/databases/-1/keys"))).status_code == 404


async def test_scan_walks_every_key_once(rd):
    seen: list[str] = []
    cursor = None
    for _ in range(100):
        page = await rd.keys(limit=50, **({"cursor": cursor} if cursor else {}))
        seen += [k["name"] for k in page["keys"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert cursor is None
    assert len(seen) == len(set(seen))  # SCAN duplicates are removed within a page only...
    expected = {k.decode("utf-8", "backslashreplace") for k in rd.direct().keys("*")}
    assert expected <= set(seen)


async def test_pattern_and_type_filters(rd):
    page = await rd.keys(pattern="user:00*", limit=500)
    names = {k["name"] for k in page["keys"]}
    assert len(names) == 100 and all(n.startswith("user:00") for n in names)
    hashes = await rd.keys(type="hash", limit=100)
    assert [k["name"] for k in hashes["keys"]] == ["h:profile"]
    assert all(k["type"] == "hash" for k in hashes["keys"])
    ttl = (await rd.keys(pattern="session:*"))["keys"][0]
    assert ttl["ttl_ms"] and 0 < ttl["ttl_ms"] <= 3_600_000
    plain = (await rd.keys(pattern="other:1"))["keys"][0]
    assert plain["ttl_ms"] is None and plain["type"] == "string"
    assert (
        await rd.api.get(rd.url("/databases/0/keys"), params={"type": "nope"})
    ).status_code == 422


async def test_binary_keys_and_values_are_markers_not_html_or_errors(rd):
    page = await rd.keys(pattern="\\xde*", limit=10)
    raw = rd.direct().keys("*")
    assert any(k == b"\xde\xad\xbe\xef" for k in raw)
    allk = []
    cursor = None
    while True:
        p = await rd.keys(limit=500, **({"cursor": cursor} if cursor else {}))
        allk += p["keys"]
        cursor = p["next_cursor"]
        if not cursor:
            break
    binary = [k for k in allk if k["binary"]]
    assert binary and binary[0]["key"] == kid(b"\xde\xad\xbe\xef")
    d = await rd.key(b"\xde\xad\xbe\xef")
    assert d["value"]["value"] == "binary-key" and d["binary"] is True
    blob = await rd.key("blob:bin")
    assert isinstance(blob["value"]["value"], dict) and "$binary" in blob["value"]["value"]
    assert blob["editable"] is False
    del page


async def test_value_pages_by_type(rd):
    s = await rd.key("user:0007")
    assert s["type"] == "string" and s["value"]["value"] == "value-7" and s["version"]
    assert s["size_bytes"] and s["encoding"]
    big = await rd.key("big:text")
    assert big["value"]["truncated"] is True and big["value"]["length"] == 20000
    full = await rd.key("big:text", full=True)
    assert len(full["value"]["value"]) == 20000
    h = await rd.key("h:profile")
    assert h["value"]["value"] == {"name": "Ann", "city": "Paris"} or h["value"]["value"] == {
        "city": "Paris",
        "name": "Ann",
    }
    lst = await rd.key("l:queue", limit=2)
    assert lst["value"]["value"] == ["a", "b"] and lst["value"]["next_cursor"] == "2"
    lst2 = await rd.key("l:queue", limit=2, cursor="2")
    assert lst2["value"]["value"] == ["c"] and lst2["value"]["next_cursor"] is None
    st = await rd.key("s:tags")
    assert sorted(st["value"]["value"]) == ["x", "y"]
    z = await rd.key("z:board")
    assert z["value"]["value"] == [["bob", 7.0], ["ann", 10.5]]
    x = await rd.key("x:events", limit=1)
    assert x["value"]["value"][0][0] == "1-1" and x["value"]["next_cursor"] == "(1-1"
    x2 = await rd.key("x:events", limit=1, cursor="(1-1")
    assert x2["value"]["value"][0][0] == "2-1" and x["editable"] is False
    missing = await rd.api.get(rd.url(f"/databases/0/keys/{kid('nope:nope')}"))
    assert missing.status_code == 404


# ------------------------------------------------------------------------ editing


@pytest.mark.parametrize(
    ("kind", "value", "expect"),
    [
        ("string", "hello", "hello"),
        ("hash", {"a": "1", "b": "2"}, {"a": "1", "b": "2"}),
        ("list", ["x", "y"], ["x", "y"]),
        ("set", ["p", "q"], ["p", "q"]),
        ("zset", [["m1", 1], ["m2", 2.5]], [["m1", 1.0], ["m2", 2.5]]),
    ],
)
async def test_create_and_replace_each_type(rd, kind, value, expect):
    name = scratch(kind)
    r = await rd.api.post(rd.url("/databases/0/keys"), {"name": name, "type": kind, "value": value})
    assert r.status_code == 201, r.text
    body = r.json()
    got = body["value"]["value"]
    assert (sorted(got) if kind == "set" else got) == expect
    dup = await rd.api.post(
        rd.url("/databases/0/keys"), {"name": name, "type": kind, "value": value}
    )
    assert dup.status_code == 409
    new = {"string": "bye", "hash": {"z": "9"}, "list": ["only"], "set": ["only"],
           "zset": [["only", 3]]}[kind]  # fmt: skip
    ok = await rd.api.put(
        rd.url(f"/databases/0/keys/{kid(name)}"),
        {"type": kind, "value": new, "version": body["version"]},
    )
    assert ok.status_code == 200, ok.text
    stale = await rd.api.put(
        rd.url(f"/databases/0/keys/{kid(name)}"),
        {"type": kind, "value": new, "version": body["version"]},
    )
    assert stale.status_code == 409  # the version changed with the first replacement
    rd.direct().delete(name)


async def test_ttl_is_kept_or_replaced_and_changed(rd):
    name = scratch("ttl")
    r = await rd.api.post(
        rd.url("/databases/0/keys"),
        {"name": name, "type": "string", "value": "v", "ttl_ms": 600000},
    )
    d = r.json()
    assert 0 < d["ttl_ms"] <= 600000
    kept = await rd.api.put(
        rd.url(f"/databases/0/keys/{kid(name)}"),
        {"type": "string", "value": "v2", "version": d["version"]},
    )
    assert kept.json()["ttl_ms"] and kept.json()["ttl_ms"] <= 600000
    dropped = await rd.api.put(
        rd.url(f"/databases/0/keys/{kid(name)}"),
        {"type": "string", "value": "v3", "version": kept.json()["version"], "keep_ttl": False},
    )
    assert dropped.json()["ttl_ms"] is None
    assert (
        await rd.api.put(rd.url(f"/databases/0/keys/{kid(name)}/ttl"), {"ttl_ms": 90000})
    ).status_code == 204
    assert 0 < (await rd.key(name))["ttl_ms"] <= 90000
    assert (
        await rd.api.put(rd.url(f"/databases/0/keys/{kid(name)}/ttl"), {"ttl_ms": None})
    ).status_code == 204
    assert (await rd.key(name))["ttl_ms"] is None
    rd.direct().delete(name)


async def test_replace_refuses_type_changes_and_bad_values(rd):
    name = scratch("v")
    d = (
        await rd.api.post(
            rd.url("/databases/0/keys"), {"name": name, "type": "string", "value": "a"}
        )
    ).json()
    path = rd.url(f"/databases/0/keys/{kid(name)}")
    for body in (
        {"type": "hash", "value": {"a": "1"}, "version": d["version"]},  # type change
        {"type": "string", "value": {"a": 1}, "version": d["version"]},
        {"type": "string", "value": "a\u0000b", "version": d["version"]},
        {"type": "string", "value": "x", "version": d["version"], "ttl_ms": -5},
        {"type": "stream", "value": "x", "version": d["version"]},
    ):
        assert (await rd.api.put(path, body)).status_code == 422, body
    for kind, value in (("hash", {}), ("list", []), ("zset", [["m", "nan"]]), ("zset", [["m"]])):
        r = await rd.api.post(
            rd.url("/databases/0/keys"), {"name": scratch("bad"), "type": kind, "value": value}
        )
        assert r.status_code == 422, (kind, value)
    rd.direct().delete(name)


async def test_delete_with_version_and_confirmation(rd):
    name = scratch("del")
    d = (
        await rd.api.post(
            rd.url("/databases/0/keys"), {"name": name, "type": "string", "value": "a"}
        )
    ).json()
    path = rd.url(f"/databases/0/keys/{kid(name)}")
    rd.direct().set(name, "changed-elsewhere")
    stale = await rd.api.request("DELETE", path, json={"version": d["version"]})
    assert stale.status_code == 409 and rd.direct().exists(name)
    fresh = (await rd.key(name))["version"]
    assert (await rd.api.request("DELETE", path, json={"version": fresh})).status_code == 204
    assert not rd.direct().exists(name)
    assert (await rd.api.request("DELETE", path, json={})).status_code == 404


async def test_production_connections_need_the_key_name_typed(admin, redis_managed):
    c = await create_connection(
        admin, "redis", redis_managed, read_only=False, environment="production"
    )
    p = Rd(admin, c["id"], redis_managed)
    name = scratch("prod")
    p.direct().set(name, "v")
    path = p.url(f"/databases/0/keys/{kid(name)}")
    r = await admin.request("DELETE", path, json={})
    assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert p.direct().exists(name)
    assert (await admin.request("DELETE", path, json={"confirm": name})).status_code == 204


async def test_read_only_connection_refuses_every_write(rd_ro):
    name = scratch("ro")
    path = rd_ro.url("/databases/0/keys")
    assert (
        await rd_ro.api.post(path, {"name": name, "type": "string", "value": "x"})
    ).status_code == 403
    assert (
        await rd_ro.api.put(
            f"{path}/{kid('user:0001')}", {"type": "string", "value": "x", "version": "v"}
        )
    ).status_code == 403
    assert (
        await rd_ro.api.request("DELETE", f"{path}/{kid('user:0001')}", json={})
    ).status_code == 403
    assert (await rd_ro.api.put(f"{path}/{kid('user:0001')}/ttl", {"ttl_ms": 5})).status_code == 403
    assert rd_ro.direct().get("user:0001") == b"value-1"
    assert rd_ro.direct().ttl("user:0001") == -1
    r = await rd_ro.cmd("SET user:0001 pwned")
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert rd_ro.direct().get("user:0001") == b"value-1"
    ok = await rd_ro.cmd("GET user:0001")
    assert ok.status_code == 200 and ok.json()["reply"] == "value-1"


# ------------------------------------------------------------------------ console


async def test_console_reads_and_writes(rd):
    name = scratch("c")
    r = await rd.cmd(f'SET {name} "hello world"')
    assert r.status_code == 403 or r.status_code == 428 or r.status_code == 200, r.text
    if r.status_code != 200:
        r = await rd.cmd(f'SET {name} "hello world"', confirm_destructive=True)
    assert r.status_code == 200 and r.json()["reply"] == "OK" and r.json()["kind"] == "write"
    g = await rd.cmd(f"GET {name}")
    assert g.json()["reply"] == "hello world" and g.json()["kind"] == "read"
    h = await rd.cmd("HGETALL h:profile")
    assert h.json()["reply"] in ({"name": "Ann", "city": "Paris"}, {"city": "Paris", "name": "Ann"})
    z = await rd.cmd("ZRANGE z:board 0 -1 WITHSCORES")
    assert z.status_code == 200
    one = await rd.cmd("GET only:in:one", db="1")
    assert one.json()["reply"] == "1"
    none = await rd.cmd("GET only:in:one")
    assert none.json()["reply"] is None
    info = await rd.cmd("INFO keyspace")
    assert "db0" in str(info.json()["reply"])
    rd.direct().delete(name)


async def test_destructive_commands_need_explicit_confirmation(rd):
    name = scratch("d")
    rd.direct().set(name, "v")
    r = await rd.cmd(f"DEL {name}")
    assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert rd.direct().exists(name)
    ok = await rd.cmd(f"DEL {name}", confirm_destructive=True)
    assert ok.status_code == 200 and ok.json()["reply"] == 1
    inc = await rd.cmd(f"INCR {scratch('n')}")
    assert inc.status_code == 200 and inc.json()["reply"] == 1  # not destructive


@pytest.mark.security
@pytest.mark.parametrize(
    "line",
    ["EVAL 'return redis.call(\"flushall\")' 0", "FLUSHALL", "FLUSHDB", "CONFIG SET dir /tmp",
     "DEBUG SLEEP 0", "KEYS *", "SHUTDOWN NOSAVE", "REPLICAOF evil.example 6379", "ACL SETUSER x on",
     "MONITOR", "SELECT 5", "MODULE LOAD /x.so", "XREAD BLOCK 0 STREAMS x:events 0"],
)  # fmt: skip
async def test_dangerous_commands_never_reach_the_server(rd, line):
    before = rd.direct().dbsize()
    r = await rd.cmd(line, confirm_destructive=True)
    assert r.status_code in (400, 403, 422), (line, r.status_code, r.text)
    assert rd.direct().dbsize() == before


async def test_console_validation(rd):
    for body in ({"command": "", "database": "0"}, {"command": "GET k", "database": "99"},
                 {"command": 'GET "unterminated', "database": "0"}):  # fmt: skip
        r = await rd.api.post(rd.url("/commands/execute"), body)
        assert r.status_code in (404, 422), body
    big = await rd.cmd("SCAN 0 COUNT 100000")
    assert big.status_code == 200


# ------------------------------------------------------------------------ authorization


async def test_roles_gate_every_operation(admin, new_user, rd):
    viewer, editor = await new_user("Viewer"), await new_user("Editor")
    await grant(admin, rd.cid, viewer, "Viewer")
    await grant(admin, rd.cid, editor, "Editor")
    outsider = await new_user("Viewer")
    assert (await outsider.get(rd.url("/databases"))).status_code == 404
    assert (await viewer.get(rd.url("/databases/0/keys"))).status_code == 200
    name = scratch("authz")
    body = {"name": name, "type": "string", "value": "x"}
    assert (await viewer.post(rd.url("/databases/0/keys"), body)).status_code == 403
    assert (await editor.post(rd.url("/databases/0/keys"), body)).status_code == 201
    path = rd.url(f"/databases/0/keys/{kid(name)}")
    assert (await viewer.request("DELETE", path, json={})).status_code == 403
    assert (
        await viewer.post(rd.url("/commands/execute"), {"database": "0", "command": "GET x"})
    ).status_code in (200, 403)
    wr = await viewer.post(rd.url("/commands/execute"), {"database": "0", "command": "SET x 1"})
    assert wr.status_code == 403
    assert (await editor.request("DELETE", path, json={})).status_code == 204


# ------------------------------------------------------------------------ adapter level


def spec(info: dict[str, Any], **over: Any) -> ConnectionSpec:
    return ConnectionSpec(
        id=uuid.uuid4(), db_type=DbType.REDIS, host=info["host"], port=info["port"],
        database=info["database"], username=over.pop("username", info["username"]),
        password=over.pop("password", info["password"]), pool_max=2, **over,
    )  # fmt: skip


@pytest.mark.security
def test_wrong_credentials_do_not_leak_the_password(redis_managed):
    bad = "Wr0ng-Credential-Value"
    a = RedisAdapter(
        spec(redis_managed, password=bad), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "connection_failed"
        assert bad not in (res.error or "")
    finally:
        a.close()


@pytest.mark.security
def test_network_policy_is_enforced_on_every_new_connection(redis_managed):
    strict = NetworkPolicy(allow_cidrs=("10.0.0.0/8",), allow_loopback=False)
    a = RedisAdapter(spec(redis_managed), strict, Limits())
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "network_policy_denied"
        with pytest.raises(NetworkPolicyViolation):
            a.list_databases()
    finally:
        a.close()


@pytest.mark.security
def test_an_acl_limited_account_is_still_limited_by_the_server(redis_managed):
    """The account's own ACL is the real boundary; the adapter surfaces its refusal cleanly."""
    a = RedisAdapter(
        spec(redis_managed, username="reader", password="readerpw-123", allow_writes=True),
        NetworkPolicy(allow_loopback=True),
        Limits(),
    )
    try:
        assert a.test().ok
        with pytest.raises(QueryFailed, match="(?i)noperm|permission"):
            a.run_command("0", [b"SET", b"acl:probe", b"1"], write=True)
        assert (
            redis_lib.Redis(
                host=redis_managed["host"], port=redis_managed["port"], password=REDIS_PASSWORD
            ).get("acl:probe")
            is None
        )
        assert a.run_command("0", [b"GET", b"user:0001"], write=False).reply == "value-1"
    finally:
        a.close()


def test_tls_verification_without_a_ca_is_refused(redis_managed):
    a = RedisAdapter(
        spec(redis_managed, tls_mode=TlsMode.VERIFY_CA),
        NetworkPolicy(allow_loopback=True),
        Limits(),
    )
    try:
        res = a.test()
        assert res.ok is False
    finally:
        a.close()


def test_connection_failures_are_typed(redis_managed):
    a = RedisAdapter(
        spec({**redis_managed, "port": 1}), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        with pytest.raises(ConnectionFailed):
            a.list_databases()
    finally:
        a.close()
