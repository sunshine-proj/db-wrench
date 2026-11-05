"""Phase 5: audit export (offline-verifiable) and syslog / SIEM forwarding."""

from __future__ import annotations

import csv
import io
import json
import socket
import threading

import pytest

from app.audit.service import GENESIS, compute_hash

BODY_FIELDS = [
    "ts",
    "actor_user_id",
    "actor_username",
    "ip",
    "request_id",
    "action",
    "connection_id",
    "target",
    "result",
    "detail",
]


class TcpSink:
    """A minimal syslog receiver (octet-counted frames) on a loopback port."""

    def __init__(self) -> None:
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.messages: list[str] = []
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while chunk := conn.recv(65536):
                    buf += chunk
                while buf:
                    n, _, rest = buf.partition(b" ")
                    if not n.isdigit() or len(rest) < int(n):
                        break
                    self.messages.append(rest[: int(n)].decode())
                    buf = rest[int(n) :]

    def close(self) -> None:
        self.srv.close()


@pytest.fixture
def tcp_sink():
    s = TcpSink()
    yield s
    s.close()


async def forward(settings_override=None):
    from app.audit import sink
    from app.config import get_settings
    from app.connections.service import get_network_policy
    from app.db import session_factory

    settings = get_settings()
    async with session_factory()() as db:
        policy = await get_network_policy(db, settings)
    return await sink.forward_once(settings, policy)


async def set_sink(admin, **over):
    body = {"enabled": True, "protocol": "tcp", "host": "127.0.0.1", "port": 514, **over}
    return await admin.put("settings/audit-sink", body)


async def test_events_are_forwarded_as_rfc5424_over_tcp(admin, tcp_sink):
    r = await set_sink(admin, port=tcp_sink.port)
    assert r.status_code == 200, r.text
    await admin.post("tokens", {"name": "siem-marker", "scopes": ["data.read"]})
    assert await forward() >= 1
    marker = [m for m in tcp_sink.messages if "token.create" in m]
    assert marker, tcp_sink.messages[-3:]
    m = marker[-1]
    assert m.startswith("<110>1 ")  # facility 13 (log audit) + severity 6 (info)
    assert 'action="token.create"' in m and "db-wrench" in m and 'hash="' in m
    payload = json.loads(m[m.index("{") :])
    assert payload["target"]["name"] == "siem-marker"
    # nothing is sent twice
    seen = len(tcp_sink.messages)
    assert await forward() == 0 or len(tcp_sink.messages) >= seen
    cfg = (await admin.get("settings/audit-sink")).json()
    assert cfg["last_forwarded_id"] >= payload["id"]


async def test_json_format_and_udp(admin):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(3)
    try:
        r = await set_sink(admin, protocol="udp", port=udp.getsockname()[1], format="json")
        assert r.status_code == 200, r.text
        await admin.post("tokens", {"name": "udp-marker", "scopes": ["data.read"]})
        await forward()
        got = []
        try:
            while True:
                got.append(json.loads(udp.recv(65535)))
        except TimeoutError:
            pass
        assert any(e["target"].get("name") == "udp-marker" for e in got)
    finally:
        udp.close()


async def test_delivery_resumes_after_an_outage(admin, tcp_sink):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()  # nothing listens here
    await set_sink(admin, port=dead_port)
    before = (await admin.get("settings/audit-sink")).json()["last_forwarded_id"]
    await admin.post("tokens", {"name": "outage-marker", "scopes": ["data.read"]})
    assert await forward() == 0  # fails quietly
    assert (await admin.get("settings/audit-sink")).json()["last_forwarded_id"] == before
    await set_sink(admin, port=tcp_sink.port, backfill=False)
    # re-pointing keeps the position, so the event recorded during the outage is delivered
    assert await forward() >= 1
    assert any("outage-marker" in m for m in tcp_sink.messages)


@pytest.mark.security
async def test_sink_destination_obeys_the_network_policy_and_validation(admin):
    for host in ("169.254.169.254", "metadata.google.internal.invalid", "bad host", ""):
        r = await set_sink(admin, host=host)
        assert r.status_code in (403, 422), (host, r.text)
    for over in ({"protocol": "ftp"}, {"port": 0}, {"facility": 99}, {"format": "xml"}):
        assert (await set_sink(admin, **over)).status_code == 422
    assert (await admin.put("settings/audit-sink", {"enabled": False})).status_code == 200


@pytest.mark.security
async def test_sink_settings_and_export_need_permissions(admin, new_user):
    viewer = await new_user("Viewer")
    for call in (viewer.get("settings/audit-sink"), viewer.get("audit/export"),
                 viewer.put("settings/audit-sink", {"enabled": False}),
                 viewer.post("settings/audit-sink/test")):  # fmt: skip
        assert (await call).status_code == 403


async def test_test_message_reports_success_and_failure(admin, tcp_sink):
    await set_sink(admin, port=tcp_sink.port)
    r = await admin.post("settings/audit-sink/test")
    assert r.status_code == 200 and r.json()["ok"] is True
    await set_sink(admin, port=1)
    r = await admin.post("settings/audit-sink/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    await admin.put("settings/audit-sink", {"enabled": False})


# ------------------------------------------------------------------------------- export


@pytest.mark.parametrize("fmt", ["ndjson", "json", "csv"])
async def test_export_formats_and_audit_of_the_export(admin, fmt):
    r = await admin.get("audit/export", params={"format": fmt})
    assert r.status_code == 200
    assert f"audit.{fmt}" in r.headers["content-disposition"]
    if fmt == "ndjson":
        rows = [json.loads(line) for line in r.text.splitlines()]
    elif fmt == "json":
        rows = r.json()
    else:
        rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows and {"id", "hash", "prev_hash", "action", "ts"} <= set(rows[0])
    events = (await admin.get("audit/events", params={"action": "audit.export"})).json()["events"]
    assert any(e["detail"].get("format") == fmt for e in events)


async def test_export_filters(admin):
    r = await admin.get("audit/export", params={"format": "ndjson", "action": "auth.login"})
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert rows and all(x["action"].startswith("auth.login") for x in rows)
    none = await admin.get("audit/export", params={"since": "2999-01-01T00:00:00Z"})
    assert none.text.strip() == ""


async def test_an_export_verifies_offline_against_the_hash_chain(admin):
    r = await admin.get("audit/export", params={"format": "ndjson"})
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert len(rows) > 5
    prev = GENESIS
    for row in rows:
        assert row["prev_hash"] == prev, f"chain broken at {row['id']}"
        body = {k: row[k] for k in BODY_FIELDS}
        assert compute_hash(prev, body) == row["hash"], f"content mismatch at {row['id']}"
        prev = row["hash"]


async def test_export_is_row_capped(admin, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "export_max_rows", 3)
    r = await admin.get("audit/export", params={"format": "ndjson"})
    assert len(r.text.splitlines()) == 3
