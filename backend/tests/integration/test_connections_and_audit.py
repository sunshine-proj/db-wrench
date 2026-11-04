"""Connection management, RBAC scoping, encryption at rest, network policy, audit chain."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.integration.conftest import create_connection

SECRET = "pw"  # the managed PostgreSQL password from the fixture


async def test_create_returns_no_secrets_and_stores_ciphertext(admin, pg_managed, app_sql):
    pw = "Sup3r-Secret-Connection-Pw"
    conn = await create_connection(admin, "postgresql", {**pg_managed, "password": pw})
    assert conn["has_password"] is True and "password" not in conn
    assert conn["read_only"] is True  # secure by default
    assert conn["capabilities"]["family"] == "relational"

    # never in any read response either
    for path in ("connections", f"connections/{conn['id']}"):
        assert pw not in (await admin.get(path)).text

    stored = app_sql("SELECT secret FROM connections WHERE id = %s", (conn["id"],))[0][0]
    assert pw not in stored and json.loads(stored)["kek"] == "test1"


async def test_validation_and_duplicate_names(admin, pg_managed):
    base = {
        "db_type": "postgresql",
        "host": pg_managed["host"],
        "port": pg_managed["port"],
        "username": "u",
        "password": "x",
    }
    assert (await admin.post("connections", {**base, "name": "x"})).status_code == 422  # too short
    assert (
        await admin.post("connections", {**base, "name": "ok name", "port": 70000})
    ).status_code == 422
    assert (
        await admin.post("connections", {**base, "name": "ok name", "tls_ca": "not a cert"})
    ).status_code == 422
    assert (
        await admin.post("connections", {**base, "name": "ok name", "options": {"evil": 1}})
    ).status_code == 422
    assert (
        await admin.post("connections", {**base, "name": "ok name", "extra_field": 1})
    ).status_code == 422
    a = await admin.post("connections", {**base, "name": f"dup-{uuid.uuid4().hex[:5]}"})
    assert a.status_code == 201
    assert (await admin.post("connections", {**base, "name": a.json()["name"]})).status_code == 409


@pytest.mark.security
@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "metadata.google.internal", "fe80::1", "0.0.0.0", "224.0.0.1", "8.8.8.8"],
)
async def test_destinations_blocked_by_network_policy(admin, host):
    r = await admin.post(
        "connections",
        {
            "name": f"ssrf-{uuid.uuid4().hex[:6]}",
            "db_type": "postgresql",
            "host": host,
            "port": 5432,
            "username": "u",
            "password": "p",
        },
    )
    assert r.status_code == 403 and r.json()["code"] == "network_policy_denied"


@pytest.mark.security
async def test_editing_host_revalidates_network_policy(admin, pg_managed):
    conn = await create_connection(admin, "postgresql", pg_managed)
    r = await admin.patch(f"connections/{conn['id']}", {"host": "169.254.169.254"})
    assert r.status_code == 403
    assert (await admin.get(f"connections/{conn['id']}")).json()["host"] == pg_managed["host"]


async def test_update_rotates_password_and_audit_never_logs_values(admin, pg_managed, app_sql):
    conn = await create_connection(admin, "postgresql", pg_managed)
    r = await admin.patch(
        f"connections/{conn['id']}",
        {"password": "Another-Secret-Value-77", "environment": "staging"},
    )
    assert r.status_code == 200 and r.json()["environment"] == "staging"
    rows = app_sql(
        "SELECT detail::text, target::text FROM audit_events WHERE connection_id = %s",
        (conn["id"],),
    )
    blob = " ".join(" ".join(r) for r in rows)
    assert "Another-Secret-Value-77" not in blob and pg_managed["password"] not in blob
    assert "password" in blob  # the *field name* is recorded, not the value
    # the old password no longer works, the new one is what's stored
    assert (await admin.post(f"connections/{conn['id']}/test")).json()["ok"] is False


async def test_connection_test_success_and_sanitised_failure(admin, pg_managed):
    good = await create_connection(admin, "postgresql", pg_managed)
    r = (await admin.post(f"connections/{good['id']}/test")).json()
    assert r["ok"] and "PostgreSQL" in r["server_version"] and r["latency_ms"] >= 0

    bad = await create_connection(admin, "postgresql", {**pg_managed, "password": "WrongPw-12345"})
    r = (await admin.post(f"connections/{bad['id']}/test")).json()
    assert r["ok"] is False and r["error_code"] == "connection_failed"
    assert "WrongPw-12345" not in json.dumps(r)


async def test_disabled_connection_refuses_data_access(admin, pg_managed):
    conn = await create_connection(admin, "postgresql", pg_managed, enabled=False)
    r = await admin.get(f"connections/{conn['id']}/databases")
    assert r.status_code == 409
    await admin.patch(f"connections/{conn['id']}", {"enabled": True})
    assert (await admin.get(f"connections/{conn['id']}/databases")).status_code == 200


@pytest.mark.security
async def test_connection_visibility_and_permissions_follow_grants(admin, new_user, pg_managed):
    conn = await create_connection(admin, "postgresql", pg_managed)
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    viewer = await new_user("Viewer")

    # No grant: invisible (404, not 403 -> no existence oracle) and not listed.
    assert (await viewer.get(f"connections/{conn['id']}")).status_code == 404
    assert conn["id"] not in [c["id"] for c in (await viewer.get("connections")).json()]
    assert (await viewer.get(f"connections/{conn['id']}/databases")).status_code == 404

    # Grant Viewer: can read metadata, cannot edit/test/delete/manage access.
    r = await admin.put(
        f"connections/{conn['id']}/grants",
        {"grants": [{"user_id": viewer.user_id, "role_id": roles["Viewer"]["id"]}]},
    )
    assert r.status_code == 200 and r.json()[0]["role"] == "Viewer"
    assert (await viewer.get(f"connections/{conn['id']}/databases")).status_code == 200
    assert (
        await viewer.patch(f"connections/{conn['id']}", {"name": "hijacked"})
    ).status_code == 403
    assert (await viewer.post(f"connections/{conn['id']}/test")).status_code == 403
    assert (await viewer.delete(f"connections/{conn['id']}")).status_code == 403
    assert (await viewer.get(f"connections/{conn['id']}/grants")).status_code == 403
    assert (
        await viewer.post(
            "connections",
            {
                "name": "mine",
                "db_type": "postgresql",
                "host": "10.0.0.1",
                "port": 1,
                "username": "u",
            },
        )
    ).status_code == 403
    mine = (await viewer.get(f"connections/{conn['id']}")).json()
    assert "data.read" in mine["permissions"] and "data.update" not in mine["permissions"]

    # Revoke: gone again.
    await admin.put(f"connections/{conn['id']}/grants", {"grants": []})
    assert (await viewer.get(f"connections/{conn['id']}/databases")).status_code == 404


async def test_creator_without_super_admin_gets_database_admin_grant(admin, new_user, pg_managed):
    dba = await new_user("Database Admin")
    conn = await create_connection(dba, "postgresql", pg_managed)
    listed = (await dba.get("connections")).json()
    assert conn["id"] in [c["id"] for c in listed]
    other = await new_user("Database Admin")
    assert (await other.get(f"connections/{conn['id']}")).status_code == 404  # not shared


async def test_cannot_grant_roles_beyond_own_rights(admin, new_user, pg_managed):
    dba = await new_user("Database Admin")
    conn = await create_connection(dba, "postgresql", pg_managed)
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    target = await new_user("Viewer")
    r = await dba.put(
        f"connections/{conn['id']}/grants",
        {"grants": [{"user_id": dba.user_id, "role_id": roles["Database Admin"]["id"]},
                    {"user_id": target.user_id, "role_id": roles["Super Admin"]["id"]}]},
    )  # fmt: skip
    assert r.status_code == 403


async def test_read_only_flag_strips_write_permissions_for_everyone(admin, pg_managed):
    ro = await create_connection(admin, "postgresql", pg_managed)
    assert "data.update" not in ro["permissions"] and ro["read_only"]
    rw = await create_connection(admin, "postgresql", pg_managed, read_only=False)
    assert "data.update" in rw["permissions"]


async def test_key_rotation_rewraps_without_breaking_connections(
    admin, pg_managed, app_sql, monkeypatch
):
    conn = await create_connection(admin, "postgresql", pg_managed)
    # Simulate a connection encrypted under the previous key.
    from app.services.runtime import get_envelope

    env = get_envelope()
    old_blob = env.rewrap(
        app_sql("SELECT secret FROM connections WHERE id = %s", (conn["id"],))[0][0],
        f"connection:{conn['id']}",
        "test0",
    )
    app_sql("UPDATE connections SET secret = %s WHERE id = %s", (old_blob, conn["id"]))
    r = await admin.post("settings/encryption/rotate")
    assert (
        r.status_code == 200 and r.json()["connections"] >= 1 and r.json()["active_kek"] == "test1"
    )
    stored = app_sql("SELECT secret FROM connections WHERE id = %s", (conn["id"],))[0][0]
    assert json.loads(stored)["kek"] == "test1"
    assert (await admin.post(f"connections/{conn['id']}/test")).json()["ok"] is True


async def test_network_policy_settings_roundtrip_and_validation(admin):
    r = await admin.get("settings/network-policy")
    assert r.status_code == 200 and "169.254.0.0/16" in r.json()["always_denied"]
    original = {"allow_cidrs": r.json()["allow_cidrs"], "allow_hosts": r.json()["allow_hosts"]}
    assert (
        await admin.put("settings/network-policy", {"allow_cidrs": ["bogus"], "allow_hosts": []})
    ).status_code == 422
    ok = await admin.put(
        "settings/network-policy",
        {"allow_cidrs": ["10.0.0.0/8"], "allow_hosts": ["*.corp.example"]},
    )
    assert ok.status_code == 200 and ok.json()["allow_hosts"] == ["*.corp.example"]
    # restore (loopback stays a deployment flag, not an API option)
    await admin.put("settings/network-policy", original)


# ----------------------------------------------------------------------------------- audit


async def test_audit_records_operations_with_actor_and_request_id(admin, pg_managed):
    conn = await create_connection(admin, "postgresql", pg_managed)
    r = await admin.get("audit/events", params={"connection_id": conn["id"]})
    events = r.json()["events"]
    create = next(e for e in events if e["action"] == "connection.create")
    assert (
        create["actor_username"] == "admin"
        and create["request_id"]
        and create["result"] == "success"
    )
    assert create["target"]["name"] == conn["name"]
    assert "password" not in json.dumps(create).replace("has_password", "")


async def test_audit_failures_and_denials_are_recorded(admin, anon):
    await anon.post("auth/login", {"username": "admin", "password": "wrong-Password-99!"})
    events = (
        await admin.get("audit/events", params={"action": "auth.login", "result": "failure"})
    ).json()["events"]
    assert events and events[0]["detail"]["reason"] == "bad_credentials"


async def test_audit_chain_verifies(admin):
    r = await admin.get("audit/verify")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["checked"] > 10


@pytest.mark.security
def test_audit_table_is_append_only_at_the_database_level(app_sql):
    for stmt in (
        "UPDATE audit_events SET action = 'tampered'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(Exception, match="append-only"):
            app_sql(stmt)


@pytest.mark.security
async def test_audit_tampering_is_detected_by_the_hash_chain(admin, app_sql):
    # A DBA with trigger privileges could bypass the triggers; the chain must still expose it.
    victim_id = app_sql("SELECT id FROM audit_events ORDER BY id LIMIT 1 OFFSET 3")[0][0]
    original = app_sql("SELECT action FROM audit_events WHERE id = %s", (victim_id,))[0][0]
    app_sql("ALTER TABLE audit_events DISABLE TRIGGER USER")
    try:
        app_sql("UPDATE audit_events SET action = 'forged' WHERE id = %s", (victim_id,))
        report = (await admin.get("audit/verify")).json()
        assert report["ok"] is False and report["first_bad_id"] == victim_id

        app_sql("UPDATE audit_events SET action = %s WHERE id = %s", (original, victim_id))
        # deleting a record breaks the chain link instead
        second = app_sql("SELECT id, action FROM audit_events ORDER BY id LIMIT 1 OFFSET 5")[0]
        row = app_sql("SELECT * FROM audit_events WHERE id = %s", (second[0],))[0]
        app_sql("DELETE FROM audit_events WHERE id = %s", (second[0],))
        report = (await admin.get("audit/verify")).json()
        assert report["ok"] is False and "removed" in report["reason"]
        app_sql(
            "INSERT INTO audit_events (id, ts, actor_user_id, actor_username, ip, request_id, action, "
            "connection_id, target, result, detail, prev_hash, hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)",
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                json.dumps(row[8]),
                row[9],
                json.dumps(row[10]),
                row[11],
                row[12],
            ),
        )
    finally:
        app_sql("ALTER TABLE audit_events ENABLE TRIGGER USER")
    assert (await admin.get("audit/verify")).json()["ok"] is True
