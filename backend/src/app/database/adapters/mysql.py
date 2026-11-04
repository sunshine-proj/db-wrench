"""MySQL adapter (PyMySQL behind SQLAlchemy connection pools)."""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pymysql
import pymysql.cursors
import pymysql.err
import sqlalchemy.exc
from pymysql.constants import CLIENT, FIELD_TYPE
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.database.core.base import Capabilities, TestResult, TlsMode
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
from app.database.relational.sql_builder import MySQLDialect
from app.errors import (
    ConnectionFailed,
    Forbidden,
    NotFound,
    QueryFailed,
    StatementTimeout,
    UnsupportedCapability,
    ValidationFailed,
)
from app.observability import logger, scrub

SYSTEM_DATABASES = ("mysql", "sys", "performance_schema", "information_schema")
_TIMEOUT_CODES = {3024, 1317}  # max_execution_time exceeded / query interrupted
# Access / capacity errors meaning "could not (stay) connected"; client-library errors are >= 2000.
_CONNECTION_CODES = {1040, 1044, 1045, 1129, 1130, 1226}


class MySQLAdapter(RelationalAdapter):
    capabilities = Capabilities(
        family="relational",
        engine="mysql",
        supports_schemas=False,  # a MySQL "database" is the namespace
        supports_views=True,
        supports_foreign_keys=True,
        supports_keyset_pagination=True,
    )
    dialect = MySQLDialect()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._engine_obj: Engine | None = None
        self._lock = threading.Lock()
        self._ca_path: str | None = None

    # ---- connection handling ----------------------------------------------------------

    def _ca_file(self) -> str | None:
        if not self.spec.tls_ca:
            return None
        if self._ca_path is None:
            fd, path = tempfile.mkstemp(prefix="dbw-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as fh:
                fh.write(self.spec.tls_ca)
            self._ca_path = path
        return self._ca_path

    def _connect(self, writable: bool = False, multi: bool = False) -> Any:
        s = self.spec
        pinned = self.policy.resolve(s.host, s.port)[0]
        timeout = self.limits.statement_timeout_s
        kwargs: dict[str, Any] = {
            "port": s.port,
            "user": s.username,
            "password": s.password,
            "connect_timeout": s.connect_timeout_s,
            "read_timeout": timeout + 5,
            "write_timeout": timeout + 5,
            "charset": "utf8mb4",
            "autocommit": False,
            "init_command": (
                f"SET SESSION transaction_read_only={0 if writable else 1}, "
                f"SESSION max_execution_time={timeout * 1000}"
            ),
        }
        flag = 0
        if writable:
            # Report *matched* rows (not only changed rows) so concurrency checks are exact.
            flag |= CLIENT.FOUND_ROWS
        if multi:
            flag |= CLIENT.MULTI_STATEMENTS  # only for permitted scripts (query.execute_script)
        if flag:
            kwargs["client_flag"] = flag
        mode = s.tls_mode
        if mode == TlsMode.DISABLE:
            kwargs["host"] = pinned
            kwargs["ssl_disabled"] = True
        elif mode == TlsMode.REQUIRE:
            kwargs["host"] = pinned
            kwargs["ssl"] = {"fake_flag_to_enable_tls": True}
            kwargs["ssl_verify_cert"] = False
            kwargs["ssl_verify_identity"] = False
        else:
            ca = self._ca_file() or ssl.get_default_verify_paths().cafile
            if not ca:
                raise ConnectionFailed("TLS verification requires a CA certificate.")
            kwargs["ssl_ca"] = ca
            kwargs["ssl_verify_cert"] = True
            kwargs["ssl_verify_identity"] = mode == TlsMode.VERIFY_FULL
            # PyMySQL verifies the certificate against the connect host, so full verification
            # connects by name (the resolution was validated above); verify-ca can pin the IP.
            kwargs["host"] = s.host if mode == TlsMode.VERIFY_FULL else pinned
        return pymysql.connect(**kwargs)

    def _engine(self) -> Engine:
        with self._lock:
            if self._engine_obj is None:
                self._engine_obj = create_engine(
                    "mysql+pymysql://",
                    creator=self._connect,
                    pool_size=max(1, self.spec.pool_max),
                    max_overflow=0,
                    pool_timeout=10,
                    pool_recycle=1800,
                    pool_pre_ping=True,
                )
            return self._engine_obj

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else 0
            text = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
            if code in _TIMEOUT_CODES or "timed out" in text.lower():
                raise StatementTimeout() from exc
            if code in _CONNECTION_CODES or (isinstance(code, int) and code >= 2000):
                raise ConnectionFailed(scrub(text, self.spec.password)) from exc
            raise QueryFailed(scrub(text, self.spec.password)) from exc
        except sqlalchemy.exc.TimeoutError as exc:
            raise ConnectionFailed("The connection pool is exhausted; try again shortly.") from exc
        except pymysql.err.MySQLError as exc:
            text = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
            raise QueryFailed(scrub(text, self.spec.password)) from exc

    def _run_many(
        self, statements: list[tuple[str, list[Any]]], database: str | None = None
    ) -> list[tuple[list[str], list[tuple[Any, ...]]]]:
        results: list[tuple[list[str], list[tuple[Any, ...]]]] = []
        with self._errors():
            conn = self._engine().raw_connection()
            try:
                if database:  # already validated against the server's database list
                    assert conn.driver_connection is not None
                    conn.driver_connection.select_db(database)
                cur = conn.cursor()
                for sql, params in statements:
                    cur.execute(sql, params)
                    names = [d[0] for d in cur.description or []]
                    results.append((names, list(cur.fetchall()) if cur.description else []))
            finally:
                conn.close()
        return results

    def _require_database(self, database: str) -> None:
        if database not in {d.name for d in self.list_databases()}:
            raise NotFound(f"Database {database!r} was not found.")

    def _query(
        self, database: str, sql: str, params: list[Any]
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        self._require_database(database)
        return self._run_many([(sql, params)], database)[0]

    def _namespace(self, database: str, schema: str | None) -> str | None:
        return database

    def close(self) -> None:
        with self._lock:
            eng, self._engine_obj = self._engine_obj, None
        if eng:
            eng.dispose()
        if self._ca_path:
            with contextlib.suppress(OSError):
                os.unlink(self._ca_path)
            self._ca_path = None

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            _, rows = self._run_many([("SELECT VERSION()", [])])[0]
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        return TestResult(True, str(rows[0][0]), round((time.perf_counter() - started) * 1000, 1))

    # ---- metadata ---------------------------------------------------------------------

    def list_databases(self) -> list[DatabaseInfo]:
        def load() -> list[DatabaseInfo]:
            placeholders = ", ".join(["%s"] * len(SYSTEM_DATABASES))
            _, rows = self._run_many(
                [
                    (
                        "SELECT schema_name FROM information_schema.schemata "  # noqa: S608
                        f"WHERE schema_name NOT IN ({placeholders}) ORDER BY schema_name",
                        list(SYSTEM_DATABASES),
                    )
                ]
            )[0]
            return [DatabaseInfo(name=r[0]) for r in rows]

        return self._meta.get_or_load(("databases",), load)

    def list_schemas(self, database: str) -> list[SchemaInfo]:
        raise UnsupportedCapability("MySQL has no schema level below the database.")

    def list_tables(self, database: str, schema: str | None) -> list[TableInfo]:
        self._require_database(database)
        _, rows = self._run_many(
            [
                (
                    "SELECT table_name, table_type, table_rows, table_comment "
                    "FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
                    [database],
                )
            ]
        )[0]
        return [
            TableInfo(
                r[0],
                "table" if r[1] == "BASE TABLE" else "view",
                int(r[2]) if r[2] is not None else None,
                r[3] or None,
            )
            for r in rows
        ]

    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        self._require_database(database)
        p = [database, table]
        head, cols, stats, fks, cons = self._run_many(
            [
                (
                    "SELECT table_type, table_rows, table_comment FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    p,
                ),
                (
                    "SELECT ordinal_position, column_name, column_type, is_nullable = 'YES', "
                    "column_default, extra LIKE '%%auto_increment%%', "
                    "COALESCE(generation_expression, '') <> '', column_comment "
                    "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                    "ORDER BY ordinal_position",
                    p,
                ),
                (
                    "SELECT index_name, COALESCE(column_name, '(expression)'), non_unique, "
                    "index_type FROM information_schema.statistics "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY index_name, seq_in_index",
                    p,
                ),
                (
                    "SELECT kcu.constraint_name, kcu.column_name, kcu.referenced_table_schema, "
                    "kcu.referenced_table_name, kcu.referenced_column_name, rc.update_rule, "
                    "rc.delete_rule FROM information_schema.key_column_usage kcu "
                    "JOIN information_schema.referential_constraints rc "
                    "ON rc.constraint_schema = kcu.constraint_schema "
                    "AND rc.constraint_name = kcu.constraint_name AND rc.table_name = kcu.table_name "
                    "WHERE kcu.table_schema = %s AND kcu.table_name = %s "
                    "AND kcu.referenced_table_name IS NOT NULL "
                    "ORDER BY kcu.constraint_name, kcu.ordinal_position",
                    p,
                ),
                (
                    "SELECT tc.constraint_name, tc.constraint_type, cc.check_clause "
                    "FROM information_schema.table_constraints tc "
                    "LEFT JOIN information_schema.check_constraints cc "
                    "ON cc.constraint_schema = tc.constraint_schema "
                    "AND cc.constraint_name = tc.constraint_name "
                    "WHERE tc.table_schema = %s AND tc.table_name = %s "
                    "AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'CHECK')",
                    p,
                ),
            ]
        )
        if not head[1]:
            raise NotFound(f"Table {database}.{table} was not found.")
        table_type, est, comment = head[1][0]

        idx_cols: dict[str, list[str]] = defaultdict(list)
        idx_meta: dict[str, tuple[bool, str]] = {}
        for name, col, non_unique, itype in stats[1]:
            idx_cols[name].append(col)
            idx_meta[name] = (not non_unique, itype)
        pk = idx_cols.get("PRIMARY", [])

        fk_map: dict[str, ForeignKeyInfo] = {}
        for name, col, rschema, rtable, rcol, upd, dele in fks[1]:
            fk = fk_map.setdefault(name, ForeignKeyInfo(name, [], rschema, rtable, [], upd, dele))
            fk.columns.append(col)
            fk.ref_columns.append(rcol)

        constraints = []
        for name, ctype, clause in cons[1]:
            kind = {"PRIMARY KEY": "primary_key", "UNIQUE": "unique", "CHECK": "check"}[ctype]
            constraints.append(
                ConstraintInfo(
                    name,
                    kind,
                    idx_cols.get("PRIMARY" if kind == "primary_key" else name, []),
                    clause,
                )
            )
        return TableStructure(
            schema=None,
            name=table,
            kind="table" if table_type == "BASE TABLE" else "view",
            columns=[
                ColumnInfo(r[1], r[2], bool(r[3]), r[4], bool(r[5]), bool(r[6]), r[7] or None, r[0])
                for r in cols[1]
            ],
            primary_key=pk,
            foreign_keys=list(fk_map.values()),
            indexes=[
                IndexInfo(n, c, idx_meta[n][0], n == "PRIMARY", idx_meta[n][1])
                for n, c in idx_cols.items()
            ],
            constraints=constraints,
            estimated_rows=int(est) if est is not None else None,
            comment=comment or None,
        )

    # ---- Phase 3: writes and streaming ------------------------------------------------

    def _connect_dedicated(self, database: str, *, writable: bool) -> Any:
        conn = self._connect(writable=writable)
        conn.select_db(database)
        return conn

    def _insert(
        self, cur: Any, st: TableStructure, ns: str | None, cols: list[str], vals: list[Any]
    ) -> list[Any] | None:
        d = self.dialect
        target = d.qualified(ns, st.name)
        if cols:
            sql = (  # noqa: S608
                f"INSERT INTO {target} ({', '.join(d.quote(c) for c in cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})"
            )
        else:
            sql = f"INSERT INTO {target} () VALUES ()"  # noqa: S608
        cur.execute(sql, vals)
        if not st.primary_key:
            return None
        given = dict(zip(cols, vals, strict=True))
        if all(c in given for c in st.primary_key):
            return [given[c] for c in st.primary_key]
        if len(st.primary_key) == 1 and cur.lastrowid:
            return [cur.lastrowid]
        return None

    def _adapt_complex(self, col: ColumnInfo, value: dict[str, Any] | list[Any]) -> Any:
        if col.data_type.lower() == "json":
            return json.dumps(value)
        raise ValidationFailed(f"Column {col.name!r} ({col.data_type}) does not accept JSON.")

    def _stream_batches(
        self, database: str, sql: str, params: list[Any], batch: int = 1000
    ) -> Iterator[list[tuple[Any, ...]]]:
        with self._errors():
            conn = self._connect_dedicated(database, writable=False)
            try:
                cur = conn.cursor(pymysql.cursors.SSCursor)
                cur.execute(sql, params)
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        return
                    yield [tuple(r) for r in rows]
            finally:
                conn.close()

    # ---- Phase 4: user-authored SQL -----------------------------------------------------

    _TYPE_NAMES = {
        v: k.lower() for k, v in vars(FIELD_TYPE).items() if k.isupper() and isinstance(v, int)
    }

    def _describe(self, cur: Any) -> list[dict[str, str]]:
        return [
            {"name": d[0], "type": self._TYPE_NAMES.get(d[1], str(d[1]))}
            for d in (cur.description or [])
        ]

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
        multi = script and len(statements) > 1
        results: list[RawResult] = []
        with self._errors():
            conn = self._connect(writable=write, multi=multi)
            watchdog: threading.Timer | None = None
            try:
                conn.select_db(database)
                cur = conn.cursor(pymysql.cursors.SSCursor)
                cur.execute(f"SET SESSION max_execution_time={int(timeout_s) * 1000}")
                # Explicit transaction characteristics cannot be changed from inside it.
                cur.execute("START TRANSACTION" if write else "START TRANSACTION READ ONLY")
                ref = {"thread_id": conn.thread_id()}
                on_start(ref)
                # max_execution_time only covers SELECT; the watchdog bounds everything else.
                watchdog = threading.Timer(timeout_s + 2, self._safe_kill, args=(ref,))
                watchdog.daemon = True
                watchdog.start()
                cur.execute(";\n".join(statements) if multi else statements[0])
                while True:
                    cols = self._describe(cur)
                    rows = cur.fetchmany(max_rows + 1) if cur.description else []
                    results.append(
                        RawResult(
                            cols,
                            [tuple(r) for r in rows[:max_rows]],
                            cur.rowcount,
                            len(rows) > max_rows,
                        )
                    )
                    if not multi or not cur.nextset():
                        break
                if write and not rollback:
                    conn.commit()
            finally:
                if watchdog:
                    watchdog.cancel()
                self._abandon(conn)
        return results

    @staticmethod
    def _abandon(conn: Any) -> None:
        """Close without draining a half-read unbuffered result (it may hold millions of rows;
        pymysql would otherwise read them all just to discard them)."""
        result = getattr(conn, "_result", None)
        if result is not None:
            result.unbuffered_active = False
        conn.close()

    def _safe_kill(self, ref: dict[str, Any]) -> None:
        """Watchdog action (runs on a timer thread): never raises."""
        try:
            self.cancel_backend(ref)
        except Exception:
            logger.warning("mysql_watchdog_kill_failed", exc_info=True)

    def cancel_backend(self, ref: dict[str, Any]) -> None:
        thread_id = int(ref["thread_id"])
        with self._errors():
            conn = self._connect()
            try:
                conn.cursor().execute(f"KILL QUERY {thread_id}")
            finally:
                conn.close()

    def stream_query(
        self, database: str, schema: str | None, sql: str, batch: int = 1000
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        self._require_database(database)
        with self._errors():
            conn = self._connect_dedicated(database, writable=False)
            try:
                cur = conn.cursor(pymysql.cursors.SSCursor)
                cur.execute("START TRANSACTION READ ONLY")
                cur.execute(sql)
                cols = self._describe(cur)
            except BaseException:
                conn.close()
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
                    self._abandon(conn)

        return cols, rows()
