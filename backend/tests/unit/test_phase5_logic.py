"""Phase 5 pure logic: scope coverage, write-stage validation, import parsing, syslog format."""

from __future__ import annotations

import json
import uuid

import pytest

from app.audit.sink import SinkConfig, _frame, format_message, validate
from app.auth.permissions import Perm
from app.auth.tokens import ALLOWED_SCOPES, FORBIDDEN_SCOPES, looks_like_token
from app.connections.scope import (
    SCOPABLE,
    ObjectScope,
    ScopedGrant,
    covers,
    filter_listing,
    scoped_permissions,
)
from app.database.document.pipeline import (
    is_write_pipeline,
    validate_pipeline,
    validate_write_pipeline,
)
from app.errors import ValidationFailed
from app.governance.service import ALWAYS_APPROVED, POLICY_OPERATIONS, params_digest
from app.importing import parse

R = frozenset({"metadata.view", "data.read"})


def g(db="shop", schema="", obj="", perms=R) -> ScopedGrant:
    return ScopedGrant(db, schema, obj, perms)


# ------------------------------------------------------------------------------- scopes


@pytest.mark.parametrize(
    "grant,target,expected",
    [
        (g(), ObjectScope("shop", "public", "t"), True),  # database grant covers a table
        (g(), ObjectScope("other", "public", "t"), False),
        (g(schema="public"), ObjectScope("shop", "public", "t"), True),
        (g(schema="public"), ObjectScope("shop", "extra", "t"), False),
        (g(schema="public", obj="t"), ObjectScope("shop", "public", "t"), True),
        (g(schema="public", obj="t"), ObjectScope("shop", "public", "u"), False),
        (g(obj="t"), ObjectScope("shop", None, "t"), True),  # MySQL / Mongo shape
        (g(obj="t"), ObjectScope("shop", None, "u"), False),
        # a narrower grant never authorises a wider target ...
        (g(schema="public", obj="t"), ObjectScope("shop", "public"), False),
        (g(schema="public", obj="t"), ObjectScope("shop"), False),
        (g(schema="public"), ObjectScope("shop"), False),
        # ... a database-wide grant covers the database as a whole
        (g(), ObjectScope("shop"), True),
        (g(), ObjectScope("shop", "public"), True),
        # listing a container: any grant inside it makes it visible
        (g(schema="public", obj="t"), ObjectScope("shop", inside=True), True),
        (g(schema="public", obj="t"), ObjectScope("shop", "public", inside=True), True),
        (g(schema="public", obj="t"), ObjectScope("shop", "extra", inside=True), False),
        (g(schema="public", obj="t"), ObjectScope("other", inside=True), False),
    ],
)
def test_scope_coverage(grant, target, expected):
    assert covers(grant, target) is expected


def test_permissions_accumulate_only_from_covering_grants():
    grants = [
        g(obj="a", perms=frozenset({"data.read"})),
        g(obj="b", perms=frozenset({"data.insert"})),
    ]
    assert scoped_permissions(grants, ObjectScope("shop", None, "a")) == {"data.read"}
    assert scoped_permissions(grants, ObjectScope("shop", None, "z")) == frozenset()


def test_scopable_permissions_exclude_everything_connection_wide():
    for p in (Perm.QUERY_EXECUTE_READ, Perm.QUERY_EXECUTE_WRITE, Perm.QUERY_EXECUTE_SCRIPT,
              Perm.ADMIN_MANAGE_DATABASES, Perm.CONNECTION_MANAGE_ACCESS, Perm.CONNECTION_DELETE,
              Perm.GOVERNANCE_APPROVE, Perm.CONNECTION_EDIT):  # fmt: skip
        assert p.value not in SCOPABLE


def test_listings_are_filtered_to_what_grants_cover():
    grants = [g(schema="public", obj="customers"), g(schema="extra")]
    dbs = filter_listing(grants, ["shop", "other"], str, "databases")
    assert dbs == ["shop"]
    schemas = filter_listing(
        grants, ["public", "extra", "private"], str, "schemas", database="shop"
    )
    assert schemas == ["public", "extra"]
    tables = filter_listing(
        grants, ["customers", "orders"], str, "tables", database="shop", schema="public"
    )
    assert tables == ["customers"]
    assert filter_listing(grants, ["a", "b"], str, "tables", database="shop", schema="extra") == [
        "a",
        "b",
    ]
    # grants without metadata.view list nothing
    quiet = [g(perms=frozenset({"data.read"}))]
    assert filter_listing(quiet, ["shop"], str, "databases") == []


# -------------------------------------------------------------------------- write stages


def test_trailing_out_and_merge_are_accepted_and_described():
    stages, reads, w = validate_write_pipeline([{"$match": {"a": 1}}, {"$out": "copy"}], "shop")
    assert (
        w.stage == "$out"
        and w.collection == "copy"
        and stages[-1] == {"$out": "copy"}
        and not reads
    )
    _, _, w = validate_write_pipeline(
        [{"$match": {}}, {"$merge": {"into": "dst", "on": "_id", "whenMatched": "keepExisting"}}],
        "shop",
    )
    assert w.stage == "$merge" and w.detail["whenMatched"] == "keepExisting"
    _, _, w = validate_write_pipeline([{"$out": {"db": "shop", "coll": "copy"}}], "shop")
    assert w.collection == "copy"


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$out": "x"}, {"$match": {}}],
        [{"$match": {}}, {"$out": {"db": "admin", "coll": "x"}}],
        [{"$match": {}}, {"$out": "system.profile"}],
        [{"$match": {}}, {"$out": 3}],
        [{"$match": {}}, {"$merge": {"into": "x", "whenMatched": [{"$set": {"a": 1}}]}}],
        [{"$match": {}}, {"$merge": {"into": "x", "whenMatched": {"a": 1}}}],
        [{"$match": {}}, {"$merge": {"into": "x", "let": {}}}],
        [{"$match": {}}, {"$merge": {"into": "x", "whenNotMatched": "nope"}}],
        [{"$match": {}}, {"$merge": {"into": "x", "on": []}}],
        [{"$match": {}}, {"$out": "x", "$limit": 1}],
        [{"$facet": {"a": [{"$out": "x"}]}}, {"$out": "y"}],
        [
            {"$lookup": {"from": "a", "pipeline": [{"$merge": {"into": "b"}}], "as": "z"}},
            {"$out": "y"},
        ],
    ],
)
def test_write_stage_abuse_is_rejected(pipeline):
    with pytest.raises(ValidationFailed):
        validate_write_pipeline(pipeline, "shop")


def test_the_read_validator_still_refuses_write_stages_and_detection_is_exact():
    for stage in ({"$out": "x"}, {"$merge": {"into": "x"}}):
        with pytest.raises(ValidationFailed):
            validate_pipeline([stage], 100)
    assert is_write_pipeline([{"$match": {}}, {"$out": "x"}])
    assert not is_write_pipeline([{"$out": "x"}, {"$match": {}}])
    assert (
        not is_write_pipeline([]) and not is_write_pipeline("nope") and not is_write_pipeline([5])
    )


# ---------------------------------------------------------------------------- governance


def test_approval_digest_binds_operation_connection_and_every_parameter():
    cid = uuid.uuid4()
    base = params_digest("admin.drop_database", cid, {"database": "a"})
    assert base == params_digest("admin.drop_database", cid, {"database": "a"})
    assert base != params_digest("admin.drop_database", cid, {"database": "b"})
    assert base != params_digest("admin.drop_schema", cid, {"database": "a"})
    assert base != params_digest("admin.drop_database", uuid.uuid4(), {"database": "a"})


def test_mongo_write_stages_are_always_under_approval_and_policy_ops_are_known():
    assert "query.mongo_write_stage" in ALWAYS_APPROVED <= POLICY_OPERATIONS


# ------------------------------------------------------------------------------- tokens


def test_token_scopes_never_include_privileged_permissions():
    assert not (ALLOWED_SCOPES & FORBIDDEN_SCOPES)
    for p in ("connection.edit", "connection.delete", "connection.manage_access",
              "governance.approve", "app.manage_users", "audit.view"):  # fmt: skip
        assert p not in ALLOWED_SCOPES
    assert "data.read" in ALLOWED_SCOPES and "query.execute_read" in ALLOWED_SCOPES


def test_token_shape_check_is_cheap_and_strict():
    assert looks_like_token("dbw_" + "a" * 43)
    for bad in ("", "dbw_", "x" * 50, "dbw_" + "a" * 500, "Bearer dbw_aaaa"):
        assert not looks_like_token(bad)


# -------------------------------------------------------------------------------- import


def test_csv_parsing_headers_delimiters_and_limits():
    header, rows = parse.parse_csv("a;b\n1;2\n\n3;4\n", delimiter=";", has_header=True, max_rows=10)
    assert header == ["a", "b"] and rows == [["1", "2"], ["3", "4"]]
    header, rows = parse.parse_csv('x\t"y z"\n', delimiter="tab", has_header=False, max_rows=10)
    assert header is None and rows == [["x", "y z"]]
    header, rows = parse.parse_csv(
        'a,b\n"line\nbreak",2\n', delimiter=",", has_header=True, max_rows=10
    )
    assert rows == [["line\nbreak", "2"]]


@pytest.mark.parametrize(
    "text,kw",
    [
        ("", {}),
        ("a,b\n", {}),  # header only
        ("a,a\n1,2\n", {}),
        ("a,b\n1\n", {}),
        ("a\n1\n2\n3\n", {"max_rows": 2}),
        ("a\x00\n1\n", {}),
        ('a\n"x\n', {}),
        ("a\n1\n", {"delimiter": "x"}),
    ],
)
def test_csv_rejections(text, kw):
    args = {"delimiter": ",", "has_header": True, "max_rows": 100, **kw}
    with pytest.raises(ValidationFailed):
        parse.parse_csv(text, **args)


def test_document_parsing():
    assert parse.parse_documents('[{"a":1},{"b":2}]', fmt="json", max_docs=5) == [
        {"a": 1},
        {"b": 2},
    ]
    assert parse.parse_documents('{"a":1}\n\n{"b":2}\n', fmt="ndjson", max_docs=5) == [
        {"a": 1},
        {"b": 2},
    ]
    for text, fmt in (
        ("", "json"),
        ("{}", "json"),
        ("[", "json"),
        ("{", "ndjson"),
        ("[]", "json"),
        ("[1]", "xml"),
    ):
        with pytest.raises(ValidationFailed):
            parse.parse_documents(text, fmt=fmt, max_docs=5)
    with pytest.raises(ValidationFailed):
        parse.parse_documents("[1,2,3]", fmt="json", max_docs=2)


def test_payload_digest_is_order_insensitive_for_options_but_exact_for_content():
    assert parse.digest("a", {"x": 1, "y": 2}) == parse.digest("a", {"y": 2, "x": 1})
    assert parse.digest("a") != parse.digest("b")


# ------------------------------------------------------------------------------- syslog


EVENT = {
    "id": 7, "ts": "2024-05-01T10:00:00+00:00", "actor_user_id": None, "actor_username": 'a"b]c',
    "ip": None, "request_id": "req1", "action": "data.update", "connection_id": None,
    "target": {}, "result": "failure", "detail": {}, "hash": "f" * 64,
}  # fmt: skip


def test_rfc5424_message_is_well_formed_and_escapes_structured_data():
    msg = format_message(SinkConfig(host="h", facility=13), EVENT, "host1").decode()
    assert msg.startswith("<108>1 2024-05-01T10:00:00+00:00 host1 db-wrench - req1 [dbw@32473 ")
    assert 'actor="a\\"b\\]c"' in msg  # quote and bracket cannot break out of the SD element
    assert json.loads(msg[msg.index("{") :])["id"] == 7


def test_json_format_and_framing():
    cfg = SinkConfig(host="h", format="json")
    assert json.loads(format_message(cfg, EVENT, "h"))["action"] == "data.update"
    framed = _frame(SinkConfig(host="h", protocol="tcp"), b"hello")
    assert framed == b"5 hello"
    assert _frame(SinkConfig(host="h", protocol="udp"), b"hello") == b"hello"


@pytest.mark.parametrize(
    "bad",
    [
        {"protocol": "ftp"},
        {"format": "xml"},
        {"facility": 24},
        {"port": 0},
        {"host": ""},
        {"host": "a b"},
        {"host": "a/b"},
    ],
)
def test_sink_validation(bad):
    ok = {"protocol": "tcp", "format": "rfc5424", "facility": 13, "port": 514, "host": "siem.local"}
    validate(ok)
    with pytest.raises(ValidationFailed):
        validate({**ok, **bad})


# ------------------------------------------------------------------------------ phase 6


def test_privilege_allowlists_never_expand_to_engine_level_all():
    from app.database.adapters.mysql import MySQLAdapter
    from app.database.adapters.postgres import PostgresAdapter
    from app.database.relational.principals import normalise_privileges

    for allowed in (
        MySQLAdapter._DB_PRIVS,
        MySQLAdapter._TABLE_PRIVS,
        PostgresAdapter._TABLE_PRIVS,
    ):
        assert normalise_privileges(["all"], allowed) == sorted(allowed)
        assert normalise_privileges(["select", "SELECT", " select "], allowed) == ["SELECT"]
        for bad in ("GRANT OPTION", "SUPER", "FILE", "PROCESS", "SHUTDOWN", "RELOAD", "CREATE USER",
                    "SELECT, DROP", "SELECT;", "", "superuser", "ALL ON *.*"):  # fmt: skip
            with pytest.raises(ValidationFailed):
                normalise_privileges([bad], allowed)
    with pytest.raises(ValidationFailed):
        normalise_privileges([], MySQLAdapter._DB_PRIVS)
    assert "EXECUTE" not in MySQLAdapter._TABLE_PRIVS  # routine rights are database-level only


def test_password_policy_and_protected_accounts():
    from app.database.relational.principals import PrincipalInfo, check_password, refuse_protected
    from app.errors import Forbidden, NotFound

    assert check_password("x" * 12) == "x" * 12
    for bad in ("short", "x" * 257, "a\x00" + "b" * 20, 5):
        with pytest.raises(ValidationFailed):
            check_password(bad)  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        refuse_protected(None, "ghost")
    with pytest.raises(Forbidden):
        refuse_protected(PrincipalInfo("root", "user", protected=True), "root")
    assert refuse_protected(PrincipalInfo("app", "user"), "app").name == "app"


def test_new_permission_is_read_only_and_connection_scoped():
    from app.auth.permissions import APP_SCOPE, WRITE_PERMS

    assert Perm.ADMIN_VIEW_USERS not in WRITE_PERMS and Perm.ADMIN_VIEW_USERS not in APP_SCOPE
    assert Perm.ADMIN_MANAGE_USERS in WRITE_PERMS and Perm.ADMIN_MANAGE_PRIVILEGES in WRITE_PERMS
    assert Perm.ADMIN_VIEW_USERS.value not in SCOPABLE  # principals are connection-level only
    for op in ("admin.create_principal", "admin.alter_principal", "admin.drop_principal",
               "admin.grant_privileges", "admin.revoke_privileges"):  # fmt: skip
        assert op in POLICY_OPERATIONS
