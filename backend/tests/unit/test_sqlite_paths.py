"""SQLite file confinement and the authorizer (no server needed)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database.adapters.sqlite import _authorizer, resolve_db_path
from app.errors import NotFound, ValidationFailed


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    (r / "sub").mkdir(parents=True)
    for name in ("a.db", "sub/b.db"):
        c = sqlite3.connect(r / name)
        c.execute("CREATE TABLE t (x)")
        c.commit()
        c.close()
    (r / "notes.txt").write_text("not a database")
    secret = tmp_path / "secret.db"
    c = sqlite3.connect(secret)
    c.execute("CREATE TABLE s (x)")
    c.commit()
    c.close()
    (r / "link.db").symlink_to(secret)
    (r / "dirlink").symlink_to(tmp_path)
    return r


def test_files_inside_the_root_resolve(root):
    assert resolve_db_path("a.db", root) == (root / "a.db").resolve()
    assert resolve_db_path("sub/b.db", root) == (root / "sub" / "b.db").resolve()


@pytest.mark.security
@pytest.mark.parametrize(
    "rel",
    ["", "/etc/passwd", "../secret.db", "sub/../../secret.db", "..", "a.db/../../secret.db",
     "sub\\b.db", "~/x.db", "a\x00.db", "C:/x.db", "./a.db", "sub//b.db", "x" * 300],
)  # fmt: skip
def test_escapes_and_odd_paths_are_refused(root, rel):
    with pytest.raises((ValidationFailed, NotFound)):
        resolve_db_path(rel, root)


@pytest.mark.security
def test_symlinks_cannot_leave_the_root(root):
    with pytest.raises(ValidationFailed, match="inside"):
        resolve_db_path("link.db", root)
    with pytest.raises(ValidationFailed, match="inside"):
        resolve_db_path("dirlink/secret.db", root)


def test_only_existing_sqlite_files_are_opened(root):
    with pytest.raises(NotFound):
        resolve_db_path("missing.db", root)
    with pytest.raises(ValidationFailed, match="not an SQLite"):
        resolve_db_path("notes.txt", root)
    with pytest.raises(ValidationFailed, match="regular file"):
        resolve_db_path("sub", root)
    assert not (root / "missing.db").exists()  # never created


def _db(tmp_path: Path, write: bool) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS t (a)")
    c.set_authorizer(_authorizer(write))
    return c


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE '/tmp/evil.db' AS e",
        "ATTACH ':memory:' AS m",
        "DETACH DATABASE main",
        "PRAGMA writable_schema = ON",
        "PRAGMA journal_mode = OFF",
        "PRAGMA database_list",
        "PRAGMA compile_options",
        "SELECT load_extension('x')",
        "SELECT readfile('/etc/passwd')",
        "CREATE VIRTUAL TABLE v USING fts5(x)",
        "INSERT INTO t VALUES (1)",
        "CREATE TABLE z (a)",
    ],
)
def test_authorizer_blocks_dangerous_statements_in_read_mode(tmp_path, sql):
    c = _db(tmp_path, write=False)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute(sql)


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    ["ATTACH DATABASE '/tmp/evil.db' AS e", "PRAGMA writable_schema = ON",
     "PRAGMA journal_mode = DELETE", "SELECT load_extension('x')", "PRAGMA database_list"],
)  # fmt: skip
def test_authorizer_blocks_dangerous_statements_in_write_mode_too(tmp_path, sql):
    c = _db(tmp_path, write=True)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute(sql)


def test_authorizer_allows_normal_work(tmp_path):
    c = _db(tmp_path, write=True)
    c.execute("INSERT INTO t VALUES (1)")
    c.execute("CREATE INDEX i ON t(a)")
    assert c.execute("SELECT a FROM t").fetchall() == [(1,)]
    assert c.execute("PRAGMA table_info(t)").fetchall()
    r = _db(tmp_path, write=False)
    assert r.execute("SELECT count(*) FROM t").fetchone() == (1,)
    assert r.execute("PRAGMA table_xinfo(t)").fetchall()
