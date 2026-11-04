"""PostgreSQL adapter (psycopg 3 behind SQLAlchemy connection pools)."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import sqlalchemy.exc
from psycopg.types.json import Json, Jsonb
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.database.core.base import Capabilities, TestResult
from app.database.relational.base import RelationalAdapter
from app.database.relational.models import (
    ColumnInfo,
    ConstraintInfo,
    DatabaseInfo,
    ForeignKeyInfo,
    FunctionInfo,
    IndexInfo,
    RawResult,
    SchemaInfo,
    SequenceInfo,
    TableInfo,
    TableStructure,
)
from app.database.relational.sql_builder import PostgresDialect
from app.errors import (
    ConnectionFailed,
    Forbidden,
    NotFound,
    QueryFailed,
    StatementTimeout,
    ValidationFailed,
)
from app.observability import scrub

_KIND = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
    "m": "materialized_view",
    "f": "foreign_table",
}
_FK_ACTION = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}
_CONTYPE = {"p": "primary_key", "u": "unique", "c": "check", "x": "exclusion"}
_PROKIND = {"f": "function", "p": "procedure", "a": "aggregate", "w": "window"}


class PostgresAdapter(RelationalAdapter):
    capabilities = Capabilities(
        family="relational",
        engine="postgresql",
        supports_schemas=True,
        supports_views=True,
        supports_sequences=True,
        supports_functions=True,
        supports_foreign_keys=True,
        supports_keyset_pagination=True,
    )
    dialect = PostgresDialect()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._engines: dict[str, Engine] = {}
        self._lock = threading.Lock()
        self._ca_path: str | None = None

    # ---- connection handling ----------------------------------------------------------

    def _default_db(self) -> str:
        return self.spec.database or "postgres"

    def _ca_file(self) -> str | None:
        if not self.spec.tls_ca:
            return None
        if self._ca_path is None:
            fd, path = tempfile.mkstemp(prefix="dbw-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as fh:
                fh.write(self.spec.tls_ca)
            self._ca_path = path
        return self._ca_path

    def _connect(
        self, database: str, writable: bool = False, timeout_s: int | None = None
    ) -> psycopg.Connection[Any]:
        s = self.spec
        # Validate every resolved address, then connect to the pinned one (hostaddr);
        # `host` is kept for TLS certificate verification.
        pinned = self.policy.resolve(s.host, s.port)[0]
        timeout_ms = (timeout_s or self.limits.statement_timeout_s) * 1000
        conn = psycopg.connect(
            host=s.host,
            hostaddr=pinned,
            port=s.port,
            dbname=database,
            user=s.username,
            password=s.password,
            connect_timeout=s.connect_timeout_s,
            sslmode=s.tls_mode.value,
            sslrootcert=self._ca_file(),
            application_name="db-wrench",
            options=(
                f"-c statement_timeout={timeout_ms} "
                f"-c idle_in_transaction_session_timeout={timeout_ms} "
                f"-c default_transaction_read_only={'off' if writable else 'on'}"
            ),
        )
        conn.read_only = not writable
        return conn

    def _engine(self, database: str) -> Engine:
        with self._lock:
            eng = self._engines.get(database)
            if eng is None:
                eng = create_engine(
                    "postgresql+psycopg://",
                    creator=lambda: self._connect(database),
                    pool_size=max(1, self.spec.pool_max),
                    max_overflow=0,
                    pool_timeout=10,
                    pool_recycle=1800,
                    pool_pre_ping=True,
                )
                self._engines[database] = eng
            return eng

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except psycopg.errors.QueryCanceled as exc:
            raise StatementTimeout() from exc
        except (psycopg.OperationalError, sqlalchemy.exc.OperationalError) as exc:
            raise ConnectionFailed(
                scrub(str(exc).strip().splitlines()[0], self.spec.password)
            ) from exc
        except sqlalchemy.exc.TimeoutError as exc:
            raise ConnectionFailed("The connection pool is exhausted; try again shortly.") from exc
        except psycopg.Error as exc:
            msg = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
            raise QueryFailed(scrub(msg, self.spec.password)) from exc

    def _run_many(
        self, database: str, statements: list[tuple[str, list[Any]]]
    ) -> list[tuple[list[str], list[tuple[Any, ...]]]]:
        results: list[tuple[list[str], list[tuple[Any, ...]]]] = []
        with self._errors():
            conn = self._engine(database).raw_connection()
            try:
                cur: Any = conn.cursor()
                # Prime the transaction. PostgreSQL lets a transaction switch from read-only
                # back to read-write (`SET TRANSACTION READ WRITE`) only before its first
                # query, so running one here closes that door for everything after it
                # (ADR-0008). Cheap, and essential once user-authored SQL exists.
                cur.execute("SELECT 1")
                for sql, params in statements:
                    cur.execute(sql, params)
                    names = [d.name for d in cur.description or []]
                    results.append((names, cur.fetchall() if cur.description else []))
            finally:
                conn.close()  # returns to the pool and rolls back
        return results

    def _query(
        self, database: str, sql: str, params: list[Any]
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        self._require_database(database)
        return self._run_many(database, [(sql, params)])[0]

    def _require_database(self, database: str) -> None:
        if database not in {d.name for d in self.list_databases()}:
            raise NotFound(f"Database {database!r} was not found.")

    @staticmethod
    def _need_schema(schema: str | None) -> str:
        if not schema:
            raise ValidationFailed("A schema is required for PostgreSQL.")
        return schema

    def _namespace(self, database: str, schema: str | None) -> str | None:
        return self._need_schema(schema)

    def close(self) -> None:
        with self._lock:
            engines, self._engines = list(self._engines.values()), {}
        for e in engines:
            e.dispose()
        if self._ca_path:
            with contextlib.suppress(OSError):
                os.unlink(self._ca_path)
            self._ca_path = None

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            _, rows = self._run_many(self._default_db(), [("SELECT version()", [])])[0]
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        return TestResult(True, str(rows[0][0]), round((time.perf_counter() - started) * 1000, 1))

    # ---- metadata ---------------------------------------------------------------------

    def list_databases(self) -> list[DatabaseInfo]:
        def load() -> list[DatabaseInfo]:
            _, rows = self._run_many(
                self._default_db(),
                [
                    (
                        "SELECT d.datname, shobj_description(d.oid, 'pg_database') "
                        "FROM pg_database d WHERE NOT d.datistemplate AND d.datallowconn "
                        "ORDER BY d.datname",
                        [],
                    )
                ],
            )[0]
            return [DatabaseInfo(name=r[0], comment=r[1]) for r in rows]

        return self._meta.get_or_load(("databases",), load)

    def list_schemas(self, database: str) -> list[SchemaInfo]:
        self._require_database(database)
        _, rows = self._run_many(
            database,
            [
                (
                    "SELECT n.nspname, pg_get_userbyid(n.nspowner) FROM pg_namespace n "
                    "WHERE n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema' "
                    "ORDER BY n.nspname",
                    [],
                )
            ],
        )[0]
        return [SchemaInfo(name=r[0], owner=r[1]) for r in rows]

    def list_tables(self, database: str, schema: str | None) -> list[TableInfo]:
        self._require_database(database)
        self._need_schema(schema)
        exists, tables = self._run_many(
            database,
            [
                ("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema]),
                (
                    "SELECT c.relname, c.relkind, NULLIF(c.reltuples, -1)::bigint, "
                    "obj_description(c.oid, 'pg_class') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','f') "
                    "AND NOT c.relispartition ORDER BY c.relname",
                    [schema],
                ),
            ],
        )
        if not exists[1]:
            raise NotFound(f"Schema {schema!r} was not found.")
        return [TableInfo(r[0], _KIND[r[1]], r[2], r[3]) for r in tables[1]]  # type: ignore[arg-type]

    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        self._require_database(database)
        self._need_schema(schema)
        head = self._run_many(
            database,
            [
                (
                    "SELECT c.oid::bigint, c.relkind, NULLIF(c.reltuples, -1)::bigint, "
                    "obj_description(c.oid, 'pg_class') FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r','p','v','m','f')",
                    [schema, table],
                )
            ],
        )[0][1]
        if not head:
            raise NotFound(f"Table {schema}.{table} was not found.")
        oid, relkind, est, comment = head[0]
        col_sql = (
            "SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), a.attidentity <> '', a.attgenerated <> '', "
            "col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
        )
        colnames = (
            "(SELECT array_agg(att.attname ORDER BY k.ord) "
            "FROM unnest({keys}) WITH ORDINALITY k(attnum, ord) "
            "JOIN pg_attribute att ON att.attrelid = {rel} AND att.attnum = k.attnum)"
        )
        con_sql = (
            "SELECT con.conname, con.contype, "
            + colnames.format(keys="con.conkey", rel="con.conrelid")
            + ", pg_get_constraintdef(con.oid) FROM pg_constraint con "
            "WHERE con.conrelid = %s AND con.contype IN ('p','u','c','x') ORDER BY con.conname"
        )
        fk_sql = (
            "SELECT con.conname, "
            + colnames.format(keys="con.conkey", rel="con.conrelid")
            + ", fn.nspname, fc.relname, "
            + colnames.format(keys="con.confkey", rel="con.confrelid")
            + ", con.confupdtype, con.confdeltype FROM pg_constraint con "
            "JOIN pg_class fc ON fc.oid = con.confrelid "
            "JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
            "WHERE con.conrelid = %s AND con.contype = 'f' ORDER BY con.conname"
        )
        idx_sql = (
            "SELECT i.relname, ix.indisunique, ix.indisprimary, am.amname, "
            "pg_get_indexdef(ix.indexrelid), "
            "(SELECT array_agg(pg_get_indexdef(ix.indexrelid, k, true) ORDER BY k) "
            " FROM generate_series(1, ix.indnkeyatts::int) k) "
            "FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_am am ON am.oid = i.relam WHERE ix.indrelid = %s ORDER BY i.relname"
        )
        cols, cons, fks, idx = self._run_many(
            database,
            [(col_sql, [oid]), (con_sql, [oid]), (fk_sql, [oid]), (idx_sql, [oid])],
        )
        columns = [
            ColumnInfo(r[1], r[2], bool(r[3]), r[4], bool(r[5]), bool(r[6]), r[7], r[0])
            for r in cols[1]
        ]
        constraints = [
            ConstraintInfo(r[0], _CONTYPE[r[1]], list(r[2] or []), r[3]) for r in cons[1]
        ]
        pk = next((c.columns for c in constraints if c.kind == "primary_key"), [])
        return TableStructure(
            schema=schema,
            name=table,
            kind=_KIND[relkind],
            columns=columns,
            primary_key=pk,
            foreign_keys=[
                ForeignKeyInfo(
                    r[0],
                    list(r[1] or []),
                    r[2],
                    r[3],
                    list(r[4] or []),
                    _FK_ACTION.get(r[5]),
                    _FK_ACTION.get(r[6]),
                )  # fmt: skip
                for r in fks[1]
            ],
            indexes=[
                IndexInfo(r[0], list(r[5] or []), bool(r[1]), bool(r[2]), r[3], r[4])
                for r in idx[1]
            ],
            constraints=constraints,
            estimated_rows=est,
            comment=comment,
        )

    def list_sequences(self, database: str, schema: str | None) -> list[SequenceInfo]:
        self._require_database(database)
        self._need_schema(schema)
        _, rows = self._run_many(
            database,
            [
                (
                    "SELECT c.relname, format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, "
                    "s.seqmin, s.seqmax, s.seqcycle FROM pg_sequence s "
                    "JOIN pg_class c ON c.oid = s.seqrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s ORDER BY c.relname",
                    [schema],
                )
            ],
        )[0]
        return [SequenceInfo(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6])) for r in rows]

    _FN_SQL = (
        "SELECT p.oid::text, p.proname, pg_get_function_identity_arguments(p.oid), "
        "pg_get_function_result(p.oid), l.lanname, p.prokind, {definition} "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_language l ON l.oid = p.prolang WHERE n.nspname = %s {extra}"
    )

    def list_functions(self, database: str, schema: str | None) -> list[FunctionInfo]:
        self._require_database(database)
        self._need_schema(schema)
        sql = self._FN_SQL.format(definition="NULL", extra="ORDER BY p.proname, p.oid LIMIT 1000")
        _, rows = self._run_many(database, [(sql, [schema])])[0]
        return [
            FunctionInfo(r[0], r[1], r[2], r[3], r[4], _PROKIND.get(r[5], "function")) for r in rows
        ]

    def get_function(self, database: str, schema: str | None, function_id: str) -> FunctionInfo:
        self._require_database(database)
        self._need_schema(schema)
        if not function_id.isdigit():
            raise ValidationFailed("Invalid function identifier.")
        sql = self._FN_SQL.format(
            definition="CASE WHEN p.prokind IN ('f','p') THEN pg_get_functiondef(p.oid) END",
            extra="AND p.oid = %s",
        )
        _, rows = self._run_many(database, [(sql, [schema, int(function_id)])])[0]
        if not rows:
            raise NotFound("Function not found.")
        r = rows[0]
        return FunctionInfo(r[0], r[1], r[2], r[3], r[4], _PROKIND.get(r[5], "function"), r[6])

    # ---- Phase 3: writes and streaming ------------------------------------------------

    def _connect_dedicated(self, database: str, *, writable: bool) -> Any:
        return self._connect(database, writable=writable)

    def _insert(
        self, cur: Any, st: TableStructure, ns: str | None, cols: list[str], vals: list[Any]
    ) -> list[Any] | None:
        d = self.dialect
        target = d.qualified(ns, st.name)
        returning = (
            " RETURNING " + ", ".join(d.quote(c) for c in st.primary_key) if st.primary_key else ""
        )
        if cols:
            sql = (  # noqa: S608
                f"INSERT INTO {target} ({', '.join(d.quote(c) for c in cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))}){returning}"
            )
        else:
            sql = f"INSERT INTO {target} DEFAULT VALUES{returning}"  # noqa: S608
        cur.execute(sql, vals)
        if not st.primary_key:
            return None
        row = cur.fetchone()
        return list(row) if row is not None else None

    def _adapt_complex(self, col: ColumnInfo, value: dict[str, Any] | list[Any]) -> Any:
        t = col.data_type.lower()
        if t == "jsonb":
            return Jsonb(value)
        if t == "json":
            return Json(value)
        if t.endswith("[]") and isinstance(value, list):
            return value
        raise ValidationFailed(f"Column {col.name!r} ({col.data_type}) does not accept JSON.")

    def _stream_batches(
        self, database: str, sql: str, params: list[Any], batch: int = 1000
    ) -> Iterator[list[tuple[Any, ...]]]:
        with self._errors():
            conn = self._connect(database)
            try:
                conn.execute("SELECT 1")  # prime the transaction (ADR-0008)
                cur = conn.cursor(name="dbw_export")
                cur.execute(sql, params)
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        return
                    yield [tuple(r) for r in rows]
            finally:
                conn.close()

    # ---- Phase 4: user-authored SQL -----------------------------------------------------

    def _prepare_session(
        self, conn: Any, schema: str | None, on_start: Callable[[dict[str, Any]], None]
    ) -> None:
        if schema:
            conn.execute("SELECT set_config('search_path', %s, false)", [schema])
        # Prime the transaction (ADR-0008): afterwards `SET TRANSACTION READ WRITE` is refused.
        conn.execute("SELECT 1")
        on_start({"pid": conn.info.backend_pid})

    def _check_schema(self, database: str, schema: str | None) -> None:
        if schema and schema not in {x.name for x in self.list_schemas(database)}:
            raise NotFound(f"Schema {schema!r} was not found.")

    @staticmethod
    def _describe(conn: Any, cur: Any) -> list[dict[str, str]]:
        cols = []
        for d in cur.description or []:
            info = conn.adapters.types.get(d.type_code)
            cols.append({"name": d.name, "type": info.name if info else str(d.type_code)})
        return cols

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
        self._check_schema(database, schema)
        if write and not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")
        results: list[RawResult] = []
        with self._errors():
            conn = self._connect(database, writable=write, timeout_s=timeout_s)
            try:
                self._prepare_session(conn, schema, on_start)
                cur = conn.cursor()
                if script and len(statements) > 1:
                    cur.execute(";\n".join(statements))
                    batches = True
                else:
                    batches = False
                while True:
                    if not batches and not statements:
                        break
                    if not batches:
                        # exactly one statement per call: the extended protocol rejects more
                        cur.execute(statements.pop(0), prepare=True)
                    cols = self._describe(conn, cur) if cur.description else []
                    rows = cur.fetchmany(max_rows + 1) if cur.description else []
                    results.append(
                        RawResult(
                            cols,
                            [tuple(r) for r in rows[:max_rows]],
                            cur.rowcount,
                            len(rows) > max_rows,
                        )
                    )
                    if batches:
                        if not cur.nextset():
                            break
                    elif not statements:
                        break
                if write and not rollback:
                    conn.commit()
            finally:
                conn.close()  # rolls back anything uncommitted
        return results

    def cancel_backend(self, ref: dict[str, Any]) -> None:
        pid = int(ref["pid"])
        with self._errors():
            conn = self._connect(self._default_db())
            try:
                conn.execute("SELECT pg_cancel_backend(%s)", [pid])
            finally:
                conn.close()

    def stream_query(
        self, database: str, schema: str | None, sql: str, batch: int = 1000
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        self._require_database(database)
        self._check_schema(database, schema)
        with self._errors():
            conn = self._connect(database)
            try:
                self._prepare_session(conn, schema, lambda _ref: None)
                cur = conn.cursor(name="dbw_query_export")
                cur.execute(sql)
                cols = self._describe(conn, cur)
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
                    conn.close()

        return cols, rows()
