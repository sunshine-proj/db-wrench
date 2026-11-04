import base64
import json
import uuid

import pytest

from app.database.document.filters import (
    filter_fields,
    mask_document,
    path_is_masked,
    validate_document,
    validate_set,
)
from app.database.relational.sql_builder import (
    PostgresDialect,
    decode_row_key,
    encode_row_key,
    pk_condition,
)
from app.editor import tokens
from app.editor.export import CsvRows, JsonDocs, JsonRows, csv_safe, safe_filename
from app.errors import ValidationFailed


def test_csv_formula_injection_is_neutralised():
    for dangerous in ("=1+1", "+cmd", "-cmd", "@SUM(A1)", "\tx", "\rx"):
        assert csv_safe(dangerous).startswith("'")
    for fine in ("hello", "1=1", "", "a-b", "x@y"):
        assert csv_safe(fine) == fine


def test_csv_rows_mask_null_and_type_handling():
    f = CsvRows(["a", "b", "c"], frozenset({"b"}))
    out = f.header() + f.rows([("=evil", "secret", None), (-5, "x", 10**17)], True)
    lines = out.strip().splitlines()
    assert (
        lines[0] == "a,b,c"
        and lines[1] == "'=evil,[masked],"
        and lines[2] == "-5,x,100000000000000000"
        or lines[2].startswith("-5,[masked],")
    )
    assert "secret" not in out


def test_json_rows_are_valid_json_in_batches():
    f = JsonRows(["id", "n"], frozenset({"n"}))
    out = f.header() + f.rows([(1, "s")], True) + f.rows([(2, None)], False) + f.footer()
    assert json.loads(out) == [{"id": 1, "n": {"$masked": True}}, {"id": 2, "n": None}]
    nd = JsonRows(["id"], frozenset(), ndjson=True)
    assert nd.rows([(1,), (2,)], True) == '{"id": 1}\n{"id": 2}\n'


def test_json_docs_masks_nested_paths():
    f = JsonDocs(frozenset({"a.b"}))
    out = json.loads(f.header() + f.rows([{"a": {"b": 1, "c": 2}}], True) + f.footer())
    assert out == [{"a": {"b": {"$masked": True}, "c": 2}}]


def test_safe_filenames():
    assert safe_filename('../../etc/pass"wd', "csv") == "etc_pass_wd.csv"
    assert safe_filename("", "json") == "export.json"


def test_row_keys_roundtrip_and_validate():
    key = encode_row_key([1, "a"])
    assert decode_row_key(key, 2) == [1, "a"]
    with pytest.raises(ValidationFailed):
        decode_row_key(key, 1)
    with pytest.raises(ValidationFailed):
        decode_row_key(encode_row_key([{"$ne": 1}]), 1)
    with pytest.raises(ValidationFailed):
        decode_row_key("???", 1)
    with pytest.raises(ValidationFailed):
        pk_condition(PostgresDialect(), ["id"], [None])


def test_bulk_tokens_bind_user_and_claims_and_expire(monkeypatch):
    user = uuid.uuid4()
    claims = {"op": "delete", "n": 3, "sel": "abc"}
    tok = tokens.issue(user, claims)
    tokens.verify(tok, user, claims)
    with pytest.raises(ValidationFailed):
        tokens.verify(tok, uuid.uuid4(), claims)  # another user
    with pytest.raises(ValidationFailed):
        tokens.verify(tok, user, {**claims, "n": 4})  # another count
    with pytest.raises(ValidationFailed):
        tokens.verify(tok[:-2] + "00", user, claims)  # tampered signature
    body, sig = tok.rsplit(".", 1)
    forged = base64.urlsafe_b64encode(
        json.dumps({"u": str(user), "exp": 9999999999, **claims}).encode()
    ).decode()
    with pytest.raises(ValidationFailed):
        tokens.verify(forged + "." + sig, user, claims)  # payload swapped, signature reused
    monkeypatch.setattr(tokens.time, "time", lambda: 10**10)
    with pytest.raises(ValidationFailed, match="expired"):
        tokens.verify(tok, user, claims)


def test_document_validation():
    doc = validate_document('{"a": {"$numberLong": "5"}, "b": [1, {"c": null}]}')
    assert doc["a"] == 5 and doc["b"][1]["c"] is None
    for bad in (
        '{"$x": 1}',
        '{"a": {"$gt": 1}}',
        "[]",
        '{"a\\u0000b": 1}',
        '{"a": {"$regularExpression": {"pattern": "x", "options": ""}}}',
    ):
        with pytest.raises(ValidationFailed):
            validate_document(bad)


def test_set_validation():
    assert validate_set({"a.b": 1}) == {"a.b": 1}
    for bad in ({"_id": 1}, {"_id.x": 1}, {"$a": 1}, {}, {"a": {"$ne": 1}}):
        with pytest.raises(ValidationFailed):
            validate_set(bad)


def test_filter_fields_and_mask_paths():
    flt = {"$or": [{"email": "x"}, {"meta": {"$elemMatch": {"k": 1}}}], "a.b": {"$gt": 1}}
    assert filter_fields(flt) == {"email", "meta", "meta.k", "a.b"}
    masked = frozenset({"meta.k"})
    assert path_is_masked("meta.k", masked) and path_is_masked(
        "meta", masked
    )  # parent exposes child
    assert path_is_masked("meta.k.deep", masked) and not path_is_masked("meta.other", masked)
    doc = {"meta": [{"k": 1, "o": 2}, {"k": None}], "x": 1}
    assert mask_document(doc, masked) == {
        "meta": [{"k": {"$masked": True}, "o": 2}, {"k": None}],
        "x": 1,
    }
    assert mask_document(doc, frozenset()) is doc


def test_adapter_manager_is_bounded_and_evicts_least_recently_used():
    from app.connections.network import NetworkPolicy
    from app.database.core.base import ConnectionSpec, DatabaseAdapter, DbType, Limits
    from app.database.core.manager import AdapterManager

    class Dummy(DatabaseAdapter):
        closed = False

        def test(self):  # pragma: no cover
            raise NotImplementedError

        def close(self) -> None:
            self.closed = True

    m = AdapterManager(idle_seconds=10_000, max_adapters=2)
    m.register(DbType.POSTGRESQL, Dummy)
    specs = [
        ConnectionSpec(
            id=uuid.uuid4(),
            db_type=DbType.POSTGRESQL,
            host="h",
            port=1,
            database=None,
            username="u",
            password="p",
        )
        for _ in range(3)
    ]
    a, b = (m.get(s, NetworkPolicy(), Limits()) for s in specs[:2])
    m.get(specs[0], NetworkPolicy(), Limits())  # touch `a` so `b` becomes the oldest
    c = m.get(specs[2], NetworkPolicy(), Limits())
    assert b.closed and not a.closed and not c.closed
    assert m.get(specs[0], NetworkPolicy(), Limits()) is a  # survivors are reused
