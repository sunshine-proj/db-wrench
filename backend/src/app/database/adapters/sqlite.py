"""SQLite adapter (stdlib ``sqlite3``). ADR-0021.

SQLite has no network endpoint, so the *file* is the attack surface:

* a connection names a file **relative to the administrator-configured root**
  (``DBW_SQLITE_ROOT``; unset = SQLite disabled). Paths are validated (no absolute paths, ``..``,
  NUL, backslashes), resolved through symlinks and must stay inside the root; the file must exist
  and start with the SQLite magic header. Databases are never created.
* read-only connections open the file with ``mode=ro`` (enforced by SQLite itself) and
  ``PRAGMA query_only``; **every** connection installs an authorizer that forbids
  ``ATTACH``/``DETACH``, file-touching functions and unsafe pragmas, so user SQL cannot reach other
  files; ``VACUUM`` (which can write ``INTO`` any path) and extension loading are refused.
* statement timeouts and cancellation use ``interrupt()`` through a progress handler.
  Cancellation works on the replica that runs the statement (the connection is in-process).
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote as urlquote

from app.config import get_settings
from app.database.core.base import Capabilities, TestResult
from app.database.relational.base import RelationalAdapter
from app.database.relational.models import (
    ColumnInfo,
    ConstraintInfo,
    DatabaseInfo,
    ForeignKeyInfo,
    IndexInfo,
    RawResult,
    SchemaInfo,
    TableInfo,
    TableStructure,
)
from app.database.relational.sql_builder import SQLiteDialect
from app.errors import (
    Conflict,
    ConnectionFailed,
    Forbidden,
    NotFound,
    QueryFailed,
    StatementNotAllowed,
    StatementTimeout,
    UnsupportedCapability,
    ValidationFailed,
)
from app.observability import logger

MAGIC = b"SQLite format 3\x00"
MAIN = "main"
MAX_PATH = 255

_DENY_FUNCTIONS = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "zipfile",
        "sqlar_compress",
    }
)
# Introspection pragmas the adapter itself needs (schema reads). Anything else must not carry a
# value (so it cannot change state) and must be on the second list.
_PRAGMA_INTROSPECT = frozenset(
    {"table_info", "table_xinfo", "table_list", "foreign_key_list", "index_list", "index_info",
     "index_xinfo"}
)  # fmt: skip
_PRAGMA_READ = frozenset({"user_version", "schema_version", "data_version", "foreign_keys"})
_PRAGMA_WRITE_OK = frozenset({"foreign_keys"})

_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT,
}
_WRITE_ACTIONS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
}
_ALLOWED_FIRST_WORDS_FOR_WRITE = None  # statements are validated by the authorizer, not by text
_VACUUM = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*vacuum\b", re.I | re.S)

# ref -> (connection, cancel flag): same-replica cancellation
_RUNNING: dict[str, tuple[sqlite3.Connection, threading.Event]] = {}
_RUNNING_LOCK = threading.Lock()


def sqlite_root() -> Path:
    raw = get_settings().sqlite_root
    if not raw:
        raise UnsupportedCapability(
            "SQLite is disabled: the administrator must set DBW_SQLITE_ROOT to a directory."
        )
    return Path(raw)


def resolve_db_path(rel: str | None, root: Path | None = None) -> Path:
    """The real path of a database file inside the root, or a domain error. Never creates files."""
    base = (root or sqlite_root()).resolve(strict=True)
    if not rel or len(rel) > MAX_PATH or "\x00" in rel or "\\" in rel or rel.startswith(("/", "~")):
        raise ValidationFailed(
            "Enter the database file as a relative path, for example data/app.db."
        )
    parts = PurePosixPath(rel).parts
    if (
        any(p in ("", ".", "..") for p in parts)
        or re.match(r"^[A-Za-z]:", rel)
        or rel != str(PurePosixPath(rel))  # canonical form only: no './', '//' or trailing '/'
    ):
        raise ValidationFailed("The database path may not contain '.', '..' or a drive letter.")
    try:
        real = (base / rel).resolve(strict=True)  # follows symlinks
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        raise NotFound("The database file was not found below the SQLite root.") from exc
    if not real.is_relative_to(base):
        raise ValidationFailed("The database file must be inside the SQLite root.")
    if not real.is_file():
        raise ValidationFailed("The path is not a regular file.")
    try:
        with real.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        raise ConnectionFailed("The database file cannot be read.") from exc
    if head != MAGIC and real.stat().st_size > 0:
        raise ValidationFailed("The file is not an SQLite database.")
    return real


def _authorizer(write: bool) -> Callable[..., int]:
    allowed = _READ_ACTIONS | (_WRITE_ACTIONS if write else set())

    def check(
        action: int, arg1: str | None, arg2: str | None, db: str | None, trig: str | None
    ) -> int:
        if action == sqlite3.SQLITE_FUNCTION:
            return (
                sqlite3.SQLITE_DENY
                if (arg2 or "").lower() in _DENY_FUNCTIONS
                else sqlite3.SQLITE_OK
            )
        if action == sqlite3.SQLITE_PRAGMA:
            name = (arg1 or "").lower()
            if name in _PRAGMA_INTROSPECT:
                return sqlite3.SQLITE_OK
            if name in _PRAGMA_READ and arg2 is None:
                return sqlite3.SQLITE_OK
            if write and name in _PRAGMA_WRITE_OK:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in allowed:
            if action == sqlite3.SQLITE_READ and (db or "main") not in ("main", "temp"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY  # ATTACH, DETACH, virtual tables, everything else

    return check


class _Cursor:
    """``%s``/``%%`` (shared builder convention) -> sqlite3 qmark parameters."""

    _pat = re.compile(r"%[%s]")

    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        text = self._pat.sub(lambda m: "?" if m.group(0) == "%s" else "%", sql)
        self._cur.execute(text, tuple(params or ()))
        return self

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cur)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class _Conn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self) -> _Cursor:
        return _Cursor(self._conn.cursor())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class SQLiteAdapter(RelationalAdapter):
    capabilities = Capabilities(
        family="relational",
        engine="sqlite",
        supports_schemas=False,
        supports_views=True,
        supports_foreign_keys=True,
        supports_indexes=True,
        supports_read_only_transactions=True,  # mode=ro + query_only
        supports_keyset_pagination=True,
    )
    dialect = SQLiteDialect()

    # ---- connection handling ----------------------------------------------------------

    def _path(self) -> Path:
        return resolve_db_path(self.spec.database)

    def _connect(self, *, writable: bool, timeout_s: int | None = None) -> sqlite3.Connection:
        path = self._path()
        if writable and not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")
        mode = "rw" if writable else "ro"
        try:
            conn = sqlite3.connect(
                f"file:{urlquote(str(path))}?mode={mode}",
                uri=True,
                timeout=min(timeout_s or self.limits.statement_timeout_s, 30),
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise ConnectionFailed(f"Cannot open the database: {exc}") from exc
        try:
            if not writable:
                conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            # Re-check after opening: the real file must still be the validated one.
            if Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve() != path:
                raise ConnectionFailed("The database file changed while opening it.")
            # Installed last: the setup above uses pragmas that user SQL may never run.
            conn.set_authorizer(_authorizer(writable))
        except BaseException:
            conn.close()
            raise
        return conn

    @contextlib.contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except (
            ConnectionFailed,
            Conflict,
            Forbidden,
            NotFound,
            QueryFailed,
            StatementNotAllowed,
            StatementTimeout,
            UnsupportedCapability,
            ValidationFailed,
        ):
            raise
        except sqlite3.OperationalError as exc:
            text = str(exc)
            if "interrupted" in text.lower():
                raise StatementTimeout() from exc
            if "locked" in text.lower() or "busy" in text.lower():
                raise QueryFailed("The database is locked by another writer; try again.") from exc
            raise QueryFailed(text) from exc
        except sqlite3.DatabaseError as exc:
            if "not authorized" in str(exc).lower() or "authoriz" in str(exc).lower():
                raise StatementNotAllowed("This statement is not allowed on SQLite.") from exc
            raise QueryFailed(str(exc)) from exc
        except (sqlite3.Warning, sqlite3.ProgrammingError) as exc:
            raise QueryFailed(str(exc)) from exc

    def _run_many(
        self, statements: list[tuple[str, list[Any]]]
    ) -> list[tuple[list[str], list[tuple[Any, ...]]]]:
        out: list[tuple[list[str], list[tuple[Any, ...]]]] = []
        with self._errors():
            conn = _Conn(self._connect(writable=False))
            try:
                cur = conn.cursor()
                for sql, params in statements:
                    cur.execute(sql, params)
                    names = [d[0] for d in cur.description or []]
                    out.append((names, [tuple(r) for r in cur.fetchall()] if names else []))
            finally:
                conn.close()
        return out

    def _require_database(self, database: str) -> None:
        if database != MAIN:
            raise NotFound(f"Database {database!r} was not found.")

    def _query(
        self, database: str, sql: str, params: list[Any]
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        self._require_database(database)
        deadline = time.monotonic() + self.limits.statement_timeout_s
        with self._errors():
            raw = self._connect(writable=False)
            raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            conn = _Conn(raw)
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                names = [d[0] for d in cur.description or []]
                return names, [tuple(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def _namespace(self, database: str, schema: str | None) -> str | None:
        return None

    def close(self) -> None:
        return None  # connections are opened per operation

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            rows = self._run_many([("SELECT sqlite_version()", [])])[0][1]
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        return TestResult(
            True, f"SQLite {rows[0][0]}", round((time.perf_counter() - started) * 1000, 1)
        )

    # ---- metadata ---------------------------------------------------------------------

    def list_databases(self) -> list[DatabaseInfo]:
        return [DatabaseInfo(name=MAIN, size_bytes=self._path().stat().st_size)]

    def list_schemas(self, database: str) -> list[SchemaInfo]:
        raise UnsupportedCapability("SQLite has no schema level below the database.")

    def list_tables(self, database: str, schema: str | None) -> list[TableInfo]:
        self._require_database(database)
        rows = self._run_many(
            [
                (
                    "SELECT name, type FROM sqlite_schema WHERE type IN ('table', 'view') "
                    "AND name NOT LIKE 'sqlite\\_%%' ESCAPE '\\' ORDER BY name",
                    [],
                )
            ]
        )[0][1]
        return [
            TableInfo(r[0], "table" if r[1] == "table" else "view", self._estimate(r[0], r[1]))
            for r in rows
        ]

    def _estimate(self, name: str, kind: str) -> int | None:
        if kind != "table":
            return None
        try:  # O(log n) for rowid tables; WITHOUT ROWID tables have no rowid
            rows = self._run_many([(f"SELECT MAX(rowid) FROM {self.dialect.quote(name)}", [])])  # noqa: S608
        except Exception:
            return None
        v = rows[0][1][0][0] if rows[0][1] else None
        return int(v) if v is not None else 0

    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        self._require_database(database)
        head = self._run_many(
            [
                (
                    "SELECT type, sql FROM sqlite_schema WHERE name = %s AND type IN ('table', 'view')",
                    [table],
                )
            ]
        )[0][1]
        if not head:
            raise NotFound(f"Table {table} was not found.")  # the name is now known to be real
        kind = "table" if head[0][0] == "table" else "view"
        q = self.dialect.quote(table).replace("%%", "%")
        cols_raw, fks_raw, idx_raw = (
            r[1]
            for r in self._run_many(
                [
                    (f"PRAGMA table_xinfo({q})", []),
                    (f"PRAGMA foreign_key_list({q})", []),
                    (f"PRAGMA index_list({q})", []),
                ]
            )
        )
        pk_cols = [c for c in sorted(cols_raw, key=lambda c: c[5]) if c[5] > 0]
        pk_names = [c[1] for c in pk_cols]
        columns: list[ColumnInfo] = []
        for cid, name, ctype, notnull, default, pk, hidden in cols_raw:
            if hidden == 1:  # hidden column of a virtual table
                continue
            rowid_alias = len(pk_names) == 1 and pk == 1 and ctype.upper() == "INTEGER"
            columns.append(
                ColumnInfo(
                    name,
                    ctype or "",
                    not (notnull or pk),
                    default,
                    rowid_alias,
                    hidden in (2, 3),
                    None,
                    int(cid) + 1,
                )  # fmt: skip
            )
        fk_map: dict[int, ForeignKeyInfo] = {}
        for fid, _seq, ref_table, frm, to, on_upd, on_del, _match in fks_raw:
            fk = fk_map.setdefault(
                fid, ForeignKeyInfo(f"fk_{table}_{fid}", [], None, ref_table, [], on_upd, on_del)
            )
            fk.columns.append(frm)
            fk.ref_columns.append(to or "")
        indexes: list[IndexInfo] = []
        constraints: list[ConstraintInfo] = []
        if pk_names:
            indexes.append(IndexInfo("(primary key)", pk_names, True, True, "btree"))
            constraints.append(ConstraintInfo("primary_key", "primary_key", pk_names))
        for _seq, iname, unique, origin, _partial in idx_raw:
            qi = self.dialect.quote(iname).replace("%%", "%")
            cols = [r[2] for r in self._run_many([(f"PRAGMA index_info({qi})", [])])[0][1]]
            if origin == "pk":
                continue  # a WITHOUT ROWID / composite primary key is already listed above
            indexes.append(IndexInfo(iname, cols, bool(unique), False, "btree"))
            if origin == "u":
                constraints.append(ConstraintInfo(iname, "unique", cols))
        return TableStructure(
            schema=None,
            name=table,
            kind=kind,
            columns=columns,
            primary_key=pk_names if kind == "table" else [],
            foreign_keys=list(fk_map.values()),
            indexes=indexes,
            constraints=constraints,
            estimated_rows=self._estimate(table, kind),
        )

    # ---- Phase 5: administration (indexes only) ------------------------------------------

    PROTECTED_DATABASES = frozenset({MAIN})

    def _admin_conn(self, database: str | None) -> Any:
        conn = self._connect(writable=True)
        return _Conn(conn)

    def _database_names(self) -> set[str]:
        return {MAIN}

    def _forget_database(self, database: str) -> None:
        return None

    def create_database(self, name: str, options: dict[str, Any] | None = None) -> None:
        raise UnsupportedCapability(
            "A SQLite connection is one database file; it cannot create others."
        )

    def drop_database(self, name: str) -> None:
        raise UnsupportedCapability("A SQLite database file cannot be dropped from here.")

    def _create_database_sql(self, name: str, options: dict[str, Any]) -> str:
        raise UnsupportedCapability("A SQLite connection is one database file.")

    def _drop_database_sql(self, name: str) -> str:
        raise UnsupportedCapability("A SQLite database file cannot be dropped from here.")

    def _create_index_sql(
        self, schema: str | None, table: str, name: str, columns: list[str], unique: bool
    ) -> str:
        d = self.dialect
        cols = ", ".join(d.quote(c) for c in columns)
        return f"CREATE {'UNIQUE ' if unique else ''}INDEX {d.quote(name)} ON {d.quote(table)} ({cols})"

    def _drop_index_sql(self, schema: str | None, table: str, name: str) -> str:
        return f"DROP INDEX {self.dialect.quote(name)}"

    # ---- Phase 3: writes and streaming ------------------------------------------------

    def _connect_dedicated(self, database: str, *, writable: bool) -> Any:
        conn = self._connect(writable=writable)
        try:
            conn.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
        except BaseException:
            conn.close()
            raise
        return _Conn(conn)

    def _insert(
        self, cur: Any, st: TableStructure, ns: str | None, cols: list[str], vals: list[Any]
    ) -> list[Any] | None:
        d = self.dialect
        returning = (
            " RETURNING " + ", ".join(d.quote(c) for c in st.primary_key) if st.primary_key else ""
        )
        if cols:
            sql = (  # noqa: S608
                f"INSERT INTO {d.quote(st.name)} ({', '.join(d.quote(c) for c in cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))}){returning}"
            )
        else:
            sql = f"INSERT INTO {d.quote(st.name)} DEFAULT VALUES{returning}"  # noqa: S608
        cur.execute(sql, vals)
        if not st.primary_key:
            return None
        row = cur.fetchone()
        return list(row) if row is not None else None

    def _adapt_complex(self, col: ColumnInfo, value: dict[str, Any] | list[Any]) -> Any:
        if col.data_type.upper() in ("JSON", "TEXT", "") or "CHAR" in col.data_type.upper():
            return json.dumps(value)
        raise ValidationFailed(f"Column {col.name!r} ({col.data_type}) does not accept JSON.")

    def _stream_batches(
        self, database: str, sql: str, params: list[Any], batch: int = 1000
    ) -> Iterator[list[tuple[Any, ...]]]:
        with self._errors():
            conn = self._connect_dedicated(database, writable=False)
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        return
                    yield [tuple(r) for r in rows]
            finally:
                conn.close()

    # ---- Phase 4: user-authored SQL -----------------------------------------------------

    def run_sql(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        statements: list[str],
        *,
        write: bool,
        script: bool,
        timeout_s: int,
        max_rows: int,
        rollback: bool,
        on_start: Callable[[dict[str, Any]], None],
    ) -> list[RawResult]:
        self._require_database(database)
        if write and not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")
        for stmt in statements:
            if _VACUUM.match(stmt):
                raise StatementNotAllowed("VACUUM is not allowed (it can write to any path).")
        token = f"sq-{uuid.uuid4()}"
        cancel = threading.Event()
        deadline = time.monotonic() + timeout_s
        results: list[RawResult] = []
        with self._errors():
            raw = self._connect(writable=write, timeout_s=timeout_s)
            with _RUNNING_LOCK:
                _RUNNING[token] = (raw, cancel)
            raw.set_progress_handler(
                lambda: 1 if cancel.is_set() or time.monotonic() > deadline else 0, 1000
            )
            try:
                on_start({"token": token})
                raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                cur = raw.cursor()
                try:
                    for stmt in statements:
                        cur.execute(stmt)
                        cols = [{"name": d[0], "type": ""} for d in cur.description or []]
                        rows = cur.fetchmany(max_rows + 1) if cur.description else []
                        results.append(
                            RawResult(
                                cols,
                                [tuple(r) for r in rows[:max_rows]],
                                cur.rowcount,
                                len(rows) > max_rows,
                            )
                        )
                    if write and not rollback:
                        raw.execute("COMMIT")
                    else:
                        raw.execute("ROLLBACK")
                except sqlite3.OperationalError as exc:
                    with contextlib.suppress(sqlite3.Error):
                        raw.execute("ROLLBACK")
                    if "interrupted" in str(exc).lower():
                        if cancel.is_set():
                            raise QueryFailed("The query was cancelled.") from exc
                        raise StatementTimeout() from exc
                    raise
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        raw.execute("ROLLBACK")
                    raise
            finally:
                with _RUNNING_LOCK:
                    _RUNNING.pop(token, None)
                with contextlib.suppress(Exception):
                    raw.close()
        return results

    def cancel_backend(self, ref: dict[str, Any]) -> None:
        with _RUNNING_LOCK:
            hit = _RUNNING.get(str(ref.get("token")))
        if hit is None:
            logger.info("sqlite_cancel_not_local")  # another replica runs it: best effort
            return
        hit[1].set()
        with contextlib.suppress(Exception):
            hit[0].interrupt()

    def stream_query(
        self, database: str, schema: str | None, sql: str, batch: int = 1000
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        self._require_database(database)
        if _VACUUM.match(sql):
            raise StatementNotAllowed("VACUUM is not allowed.")
        with self._errors():
            raw = self._connect(writable=False)
            try:
                raw.execute("BEGIN")
                cur = raw.cursor()
                cur.execute(sql)
                cols = [{"name": d[0], "type": ""} for d in cur.description or []]
            except BaseException:
                raw.close()
                raise

        def rows() -> Iterator[list[tuple[Any, ...]]]:
            with self._errors():
                try:
                    while True:
                        chunk = cur.fetchmany(batch)
                        if not chunk:
                            return
                        yield [tuple(r) for r in chunk]
                finally:
                    raw.close()

        return cols, rows()
