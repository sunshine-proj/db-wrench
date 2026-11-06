"""Microsoft SQL Server adapter (pymssql / FreeTDS behind a SQLAlchemy connection pool).

SQL Server has **no read-only transaction**, so the boundary differs from the other relational
engines (ADR-0018):

* every statement that is not an authorised write runs in a transaction that is **always rolled
  back**, never committed (pooled connections are also reset with a rollback on return);
* user-authored SQL on a non-writing path must pass a strict token gate (``SELECT``/``WITH`` only,
  no DML/DDL/``EXEC``/``INTO``/``OPENROWSET``/``xp_*`` anywhere in the batch) because T-SQL lets
  statements follow each other without ``;`` and some statements (``CREATE DATABASE``, ``BACKUP``,
  ``xp_cmdshell``) are not transactional. The gate is defence in depth; the strongest control is
  still a least-privilege login, which the connection form recommends.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pymssql
import sqlalchemy.exc
from sqlalchemy.dialects.mssql.pymssql import MSDialect_pymssql
from sqlalchemy.pool import QueuePool

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
from app.database.relational.principals import (
    GrantEntry,
    GrantTarget,
    PrincipalInfo,
    check_password,
    normalise_privileges,
    refuse_protected,
)
from app.database.relational.sql_builder import MSSQLDialect
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
from app.observability import logger, scrub

SYSTEM_DATABASES = ("master", "tempdb", "model", "msdb")
SYSTEM_SCHEMAS = frozenset(
    {"sys", "information_schema", "guest", "dbo"}
    | {"db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin", "db_backupoperator"}
    | {"db_datareader", "db_datawriter", "db_denydatareader", "db_denydatawriter"}
)
_TIMEOUT_CODES = {20003}  # DBPROCESS read timeout
_CONNECTION_CODES = {18456, 18451, 18452, 4060, 20002, 20009, 20017, 20047, 20006}
_TYPE_CODES = {1: "string", 2: "binary", 3: "number", 4: "datetime", 5: "decimal"}

# Anything that can change state, reach outside the database, or run dynamic SQL. Matched against
# *words* (never inside strings, comments or [bracketed] identifiers).
_READ_DENY = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER", "EXEC",
        "EXECUTE", "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE", "DBCC", "RECONFIGURE",
        "KILL", "SHUTDOWN", "OPENROWSET", "OPENDATASOURCE", "OPENQUERY", "OPENXML", "BULK",
        "INTO", "USE", "SET", "DECLARE", "BEGIN", "COMMIT", "ROLLBACK", "SAVE", "WAITFOR",
        "GOTO", "RAISERROR", "THROW", "PRINT", "CHECKPOINT", "READTEXT", "WRITETEXT",
        "UPDATETEXT", "ENABLE", "DISABLE", "REVERT", "IMPERSONATE", "OUTPUT", "FETCH_NEXT_VALUE",
    }
)  # fmt: skip
_DENY_PREFIXES = ("XP_", "SP_", "FN_")  # checked per dotted segment, e.g. master.dbo.xp_x
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#@.]*")


def tsql_words(sql: str) -> list[str]:
    """Upper-cased words outside comments, strings and quoted identifiers."""
    words: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif c in ("'", '"', "["):
            close = "]" if c == "[" else c
            i += 1
            while i < n:
                if sql[i] == close:
                    if i + 1 < n and sql[i + 1] == close:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
        elif (m := _WORD.match(sql, i)) and (c.isalpha() or c == "_"):
            words.append(m.group(0).upper())
            i = m.end()
        else:
            i += 1
    return words


def assert_read_only_batch(sql: str) -> None:
    """Strict gate for statements that must not change anything (see module docstring)."""
    words = tsql_words(sql)
    if not words or words[0] not in ("SELECT", "WITH"):
        raise StatementNotAllowed("Only SELECT statements can run on this path.")
    for w in words:
        if w in _READ_DENY or any(seg.startswith(_DENY_PREFIXES) for seg in w.split(".")):
            raise StatementNotAllowed(
                f"{w.split('.')[-1]} is not allowed in a read-only statement on SQL Server."
            )


class _Cursor:
    """Adds the ``%%`` convention (identifier quoting doubles ``%``) that pymssql only honours
    when parameters are present, and accepts lists."""

    def __init__(self, cur: Any):
        self._cur = cur

    def execute(self, sql: str, params: Any = None) -> None:
        if params:
            self._cur.execute(sql, tuple(params))
        else:
            self._cur.execute(sql.replace("%%", "%"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class _Conn:
    """pymssql connection whose cursors speak the shared ``%s``/``%%`` convention."""

    def __init__(self, conn: Any):
        self._conn = conn

    def cursor(self) -> _Cursor:
        return _Cursor(self._conn.cursor())

    def raw_cursor(self) -> Any:
        return self._conn.cursor()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class MSSQLAdapter(RelationalAdapter):
    capabilities = Capabilities(
        family="relational",
        engine="mssql",
        supports_schemas=True,
        supports_views=True,
        supports_foreign_keys=True,
        supports_read_only_transactions=False,  # see module docstring
        supports_keyset_pagination=True,
    )
    dialect = MSSQLDialect()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pool_obj: QueuePool | None = None
        self._lock = threading.Lock()

    # ---- connection handling ----------------------------------------------------------

    def _connect(self, database: str | None = None) -> Any:
        s = self.spec
        pinned = self.policy.resolve(s.host, s.port)[0]
        if s.tls_mode in (TlsMode.VERIFY_CA, TlsMode.VERIFY_FULL):
            raise ConnectionFailed(
                "Certificate verification is not available for SQL Server connections yet; "
                "use 'require' (encrypted) or terminate TLS on a trusted network path."
            )
        encryption = "off" if s.tls_mode == TlsMode.DISABLE else "require"
        timeout = self.limits.statement_timeout_s
        try:
            return pymssql.connect(
                server=pinned,
                port=str(s.port),
                user=s.username,
                password=s.password,
                database=database or s.database or "master",
                login_timeout=s.connect_timeout_s,
                timeout=timeout + 5,
                autocommit=False,
                appname="DB Wrench",
                encryption=encryption,
                tds_version="7.4",
            )
        except pymssql.Error as exc:
            raise ConnectionFailed(scrub(self._message(exc), s.password)) from exc

    @staticmethod
    def _message(exc: BaseException) -> str:
        args: tuple[Any, ...] = tuple(getattr(exc, "args", ()))
        msg: Any = args[1] if len(args) > 1 else (args[0] if args else str(exc))
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        text = str(msg).strip()
        return text.split("\n")[0][:500] or "SQL Server error"

    def _pool(self) -> QueuePool:
        with self._lock:
            if self._pool_obj is None:
                self._pool_obj = QueuePool(
                    self._connect,
                    pool_size=max(1, self.spec.pool_max),
                    max_overflow=0,
                    timeout=10,
                    recycle=1800,
                    pre_ping=True,
                    dialect=MSDialect_pymssql(),
                )
            return self._pool_obj

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except (ConnectionFailed, StatementNotAllowed):
            raise
        except pymssql.OperationalError as exc:
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 0
            text = self._message(exc)
            if code in _TIMEOUT_CODES or "timed out" in text.lower():
                raise StatementTimeout() from exc
            if code in _CONNECTION_CODES or "connection" in text.lower():
                raise ConnectionFailed(scrub(text, self.spec.password)) from exc
            raise QueryFailed(scrub(text, self.spec.password)) from exc
        except sqlalchemy.exc.TimeoutError as exc:
            raise ConnectionFailed("The connection pool is exhausted; try again shortly.") from exc
        except pymssql.Error as exc:
            raise QueryFailed(scrub(self._message(exc), self.spec.password)) from exc

    def _use(self, conn: Any, database: str | None) -> None:
        if database:  # always validated against the server's database list by the callers
            cur = conn.cursor()
            cur.execute(f"USE {self.dialect.quote(database)}")

    def _run_many(
        self, statements: list[tuple[str, list[Any]]], database: str | None = None
    ) -> list[tuple[list[str], list[tuple[Any, ...]]]]:
        results: list[tuple[list[str], list[tuple[Any, ...]]]] = []
        with self._errors():
            pooled = self._pool().connect()
            try:
                conn = _Conn(pooled.driver_connection)
                self._use(conn, database)
                cur = conn.cursor()
                for sql, params in statements:
                    cur.execute(sql, params)
                    names = [d[0] for d in cur.description or []]
                    results.append((names, [tuple(r) for r in cur.fetchall()] if names else []))
            finally:
                # Never commit on this path: whatever ran is rolled back (pool reset as well).
                with contextlib.suppress(Exception):
                    pooled.rollback()
                pooled.close()
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
        return schema or "dbo"

    def close(self) -> None:
        with self._lock:
            pool, self._pool_obj = self._pool_obj, None
        if pool:
            pool.dispose()

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            _, rows = self._run_many([("SELECT @@VERSION", [])])[0]
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        first = str(rows[0][0]).split("\n")[0]
        return TestResult(True, first, round((time.perf_counter() - started) * 1000, 1))

    # ---- metadata ---------------------------------------------------------------------

    def list_databases(self) -> list[DatabaseInfo]:
        def load() -> list[DatabaseInfo]:
            _, rows = self._run_many(
                [
                    (
                        "SELECT name FROM sys.databases WHERE database_id > 4 AND state = 0 "
                        "AND HAS_DBACCESS(name) = 1 ORDER BY name",
                        [],
                    )
                ]
            )[0]
            return [DatabaseInfo(name=r[0]) for r in rows]

        return self._meta.get_or_load(("databases",), load)

    def list_schemas(self, database: str) -> list[SchemaInfo]:
        self._require_database(database)
        _, rows = self._run_many(
            [
                (
                    "SELECT s.name, p.name FROM sys.schemas s "
                    "LEFT JOIN sys.database_principals p ON p.principal_id = s.principal_id "
                    "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                    "AND s.name NOT LIKE 'db[_]%%' ORDER BY s.name",
                    [],
                )
            ],
            database,
        )[0]
        return [SchemaInfo(name=r[0], owner=r[1]) for r in rows]

    def _require_schema(self, database: str, schema: str | None) -> str:
        name = schema or "dbo"
        if name not in {s.name for s in self.list_schemas(database)}:
            raise NotFound(f"Schema {name!r} was not found.")
        return name

    def list_tables(self, database: str, schema: str | None) -> list[TableInfo]:
        self._require_database(database)
        name = self._require_schema(database, schema)
        _, rows = self._run_many(
            [
                (
                    "SELECT o.name, o.type, (SELECT SUM(p.rows) FROM sys.partitions p "
                    "WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)) "
                    "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
                    "WHERE s.name = %s AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0 "
                    "ORDER BY o.name",
                    [name],
                )
            ],
            database,
        )[0]
        return [
            TableInfo(r[0], "table" if r[1].strip() == "U" else "view", _int(r[2])) for r in rows
        ]

    @staticmethod
    def _type_name(t: str, max_len: int, prec: int, scale: int) -> str:
        t = t.lower()
        if t in ("varchar", "char", "varbinary", "binary"):
            return f"{t}({'max' if max_len == -1 else max_len})"
        if t in ("nvarchar", "nchar"):
            return f"{t}({'max' if max_len == -1 else max_len // 2})"
        if t in ("decimal", "numeric"):
            return f"{t}({prec},{scale})"
        if t in ("datetime2", "time", "datetimeoffset"):
            return f"{t}({scale})"
        return t

    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        self._require_database(database)
        sch = self._require_schema(database, schema)
        head = self._run_many(
            [
                (
                    "SELECT o.object_id, o.type FROM sys.objects o "
                    "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                    "WHERE s.name = %s AND o.name = %s AND o.type IN ('U', 'V')",
                    [sch, table],
                )
            ],
            database,
        )[0][1]
        if not head:
            raise NotFound(f"Table {sch}.{table} was not found.")
        oid, otype = head[0]
        p = [oid]
        cols, idx, fks, checks, keys, est = (
            r[1]
            for r in self._run_many(
                [
                    (
                        "SELECT c.column_id, c.name, t.name, c.max_length, c.precision, c.scale, "
                        "c.is_nullable, dc.definition, c.is_identity, c.is_computed, "
                        "CAST(ep.value AS NVARCHAR(4000)) FROM sys.columns c "
                        "JOIN sys.types t ON t.user_type_id = c.user_type_id "
                        "LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id "
                        "LEFT JOIN sys.extended_properties ep ON ep.major_id = c.object_id "
                        "AND ep.minor_id = c.column_id AND ep.class = 1 AND ep.name = 'MS_Description' "
                        "WHERE c.object_id = %s ORDER BY c.column_id",
                        p,
                    ),
                    (
                        "SELECT i.name, i.is_unique, i.is_primary_key, i.type_desc, c.name "
                        "FROM sys.indexes i JOIN sys.index_columns ic "
                        "ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
                        "JOIN sys.columns c ON c.object_id = ic.object_id "
                        "AND c.column_id = ic.column_id "
                        "WHERE i.object_id = %s AND i.type > 0 AND ic.is_included_column = 0 "
                        "ORDER BY i.index_id, ic.key_ordinal",
                        p,
                    ),
                    (
                        "SELECT fk.name, pc.name, rs.name, rt.name, rc.name, "
                        "fk.update_referential_action_desc, fk.delete_referential_action_desc "
                        "FROM sys.foreign_keys fk JOIN sys.foreign_key_columns fkc "
                        "ON fkc.constraint_object_id = fk.object_id "
                        "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
                        "AND pc.column_id = fkc.parent_column_id "
                        "JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id "
                        "JOIN sys.schemas rs ON rs.schema_id = rt.schema_id "
                        "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
                        "AND rc.column_id = fkc.referenced_column_id "
                        "WHERE fk.parent_object_id = %s ORDER BY fk.name, fkc.constraint_column_id",
                        p,
                    ),
                    (
                        "SELECT name, definition FROM sys.check_constraints "
                        "WHERE parent_object_id = %s",
                        p,
                    ),
                    ("SELECT name, type FROM sys.key_constraints WHERE parent_object_id = %s", p),
                    (
                        "SELECT SUM(rows) FROM sys.partitions WHERE object_id = %s "
                        "AND index_id IN (0, 1)",
                        p,
                    ),
                ],
                database,
            )
        )
        idx_cols: dict[str, list[str]] = defaultdict(list)
        idx_meta: dict[str, tuple[bool, bool, str]] = {}
        for name, unique, primary, kind, col in idx:
            idx_cols[name].append(col)
            idx_meta[name] = (bool(unique), bool(primary), kind)
        pk = next((c for n, c in idx_cols.items() if idx_meta[n][1]), [])

        fk_map: dict[str, ForeignKeyInfo] = {}
        for name, col, rschema, rtable, rcol, upd, dele in fks:
            fk = fk_map.setdefault(
                name,
                ForeignKeyInfo(
                    name, [], rschema, rtable, [], upd.replace("_", " "), dele.replace("_", " ")
                ),
            )
            fk.columns.append(col)
            fk.ref_columns.append(rcol)

        constraints = [
            ConstraintInfo(
                n, "primary_key" if t.strip() == "PK" else "unique", idx_cols.get(n, []), None
            )
            for n, t in keys
        ] + [ConstraintInfo(n, "check", [], d) for n, d in checks]

        return TableStructure(
            schema=sch,
            name=table,
            kind="table" if otype.strip() == "U" else "view",
            columns=[
                ColumnInfo(
                    r[1],
                    self._type_name(r[2], r[3], r[4], r[5]),
                    bool(r[6]),
                    r[7],
                    bool(r[8]),
                    bool(r[9]),
                    r[10] or None,
                    r[0],
                )
                for r in cols
            ],
            primary_key=pk,
            foreign_keys=list(fk_map.values()),
            indexes=[
                IndexInfo(n, c, idx_meta[n][0], idx_meta[n][1], idx_meta[n][2].lower())
                for n, c in idx_cols.items()
            ],  # fmt: skip
            constraints=constraints,
            estimated_rows=_int(est[0][0]) if est else None,
        )

    # ---- Phase 5: administration ------------------------------------------------------

    PROTECTED_DATABASES = frozenset(SYSTEM_DATABASES)
    PROTECTED_SCHEMAS = SYSTEM_SCHEMAS

    def _admin_conn(self, database: str | None) -> Any:
        raw = self._connect(database)
        raw.autocommit(True)
        return _Conn(raw)

    def _database_names(self) -> set[str]:
        return {d.name for d in self.list_databases()}

    def _forget_database(self, database: str) -> None:
        return None  # pooled connections `USE` per call; DROP uses SINGLE_USER, see below

    def _create_database_sql(self, name: str, options: dict[str, Any]) -> str:
        if options:
            raise ValidationFailed(f"Unknown option(s): {', '.join(sorted(options))}.")
        return f"CREATE DATABASE {self.dialect.quote(name)}"

    def _drop_database_sql(self, name: str) -> str:
        q = self.dialect.quote(name)
        # Pooled sessions of this app may still hold the database; roll them back and drop.
        return f"ALTER DATABASE {q} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {q}"

    def create_schema(self, database: str, schema: str) -> None:
        from app.database.relational.admin import validate_new_name

        validate_new_name("schema", schema)
        self._assert_admin_writable()
        self._require_database(database)
        if schema.lower() in {s.name.lower() for s in self.list_schemas(database)} | SYSTEM_SCHEMAS:
            raise Conflict(f"Schema {schema!r} already exists or is reserved.")
        with self._admin_cursor(database) as cur:
            cur.execute(f"CREATE SCHEMA {self.dialect.quote(schema)}")
        self._refresh()

    def drop_schema(self, database: str, schema: str) -> None:
        self._assert_admin_writable()
        self._require_database(database)
        if schema not in {s.name for s in self.list_schemas(database)}:
            raise NotFound(f"Schema {schema!r} was not found.")
        if schema.lower() in self.PROTECTED_SCHEMAS:
            raise Forbidden(f"{schema!r} is a system schema and cannot be dropped.")
        with self._admin_cursor(database) as cur:  # fails while the schema still has objects
            cur.execute(f"DROP SCHEMA {self.dialect.quote(schema)}")
        self._refresh()

    def _create_index_sql(
        self, schema: str | None, table: str, name: str, columns: list[str], unique: bool
    ) -> str:
        d = self.dialect
        cols = ", ".join(d.quote(c) for c in columns)
        return (
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {d.quote(name)} "
            f"ON {d.qualified(schema or 'dbo', table)} ({cols})"
        )

    def _drop_index_sql(self, schema: str | None, table: str, name: str) -> str:
        return f"DROP INDEX {self.dialect.quote(name)} ON {self.dialect.qualified(schema or 'dbo', table)}"

    # ---- Phase 6: logins, users and privileges ----------------------------------------

    _LOGIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    _DB_PRIVS = frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "REFERENCES", "VIEW DEFINITION",
         "CREATE TABLE", "CREATE VIEW", "CREATE PROCEDURE", "CREATE FUNCTION", "ALTER", "CONNECT"}
    )  # fmt: skip
    _OBJ_PRIVS = frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "VIEW DEFINITION", "ALTER"}
    )
    _SCHEMA_PRIVS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "REFERENCES"})

    def list_principals(self) -> list[PrincipalInfo]:
        _, rows = self._run_many(
            [
                (
                    "SELECT name, type, is_disabled, IS_SRVROLEMEMBER('sysadmin', name) "
                    "FROM sys.server_principals WHERE type IN ('S', 'U', 'G') "
                    "AND name NOT LIKE '##%%' ORDER BY name",
                    [],
                )
            ]
        )[0]
        return [
            PrincipalInfo(
                name=r[0],
                kind="user",
                can_login=not r[2],
                protected=bool(r[3]) or r[0] == "sa" or r[0] == self.spec.username or r[1] != "S",
                attributes={"disabled": bool(r[2]), "sysadmin": bool(r[3]), "type": r[1]},
            )
            for r in rows
        ]

    def _principal(self, name: str) -> PrincipalInfo:
        found = next((p for p in self.list_principals() if p.name == name), None)
        return refuse_protected(found, name)

    def create_principal(
        self, name: str, *, password: str | None, host: str | None, options: dict[str, Any]
    ) -> None:
        self._assert_admin_writable()
        if not self._LOGIN.fullmatch(name):
            raise ValidationFailed("Invalid login name (letters, digits, '_', up to 64).")
        if options:
            raise ValidationFailed(f"Unknown option(s): {', '.join(sorted(options))}.")
        if not password:
            raise ValidationFailed("A login needs a password.")
        check_password(password)
        if any(p.name.lower() == name.lower() for p in self.list_principals()):
            raise Conflict(f"Login {name!r} already exists.")
        with self._admin_cursor(None) as cur:
            # The password is a literal in T-SQL DDL (no parameters); pymssql quotes it.
            cur.execute(
                f"CREATE LOGIN {self.dialect.quote(name)} WITH PASSWORD = %s, CHECK_POLICY = ON",
                (password,),
            )

    def alter_principal(
        self, name: str, *, host: str | None, password: str | None, options: dict[str, Any]
    ) -> None:
        self._assert_admin_writable()
        self._principal(name)
        unknown = set(options) - {"lock"}
        if unknown:
            raise ValidationFailed(f"Unknown or forbidden option(s): {', '.join(sorted(unknown))}.")
        if not password and "lock" not in options:
            raise ValidationFailed("Nothing to change.")
        q = self.dialect.quote(name)
        with self._admin_cursor(None) as cur:
            if password:
                check_password(password)
                cur.execute(f"ALTER LOGIN {q} WITH PASSWORD = %s", (password,))
            if "lock" in options:
                if not isinstance(options["lock"], bool):
                    raise ValidationFailed("lock must be true or false.")
                cur.execute(f"ALTER LOGIN {q} {'DISABLE' if options['lock'] else 'ENABLE'}")

    def drop_principal(self, name: str, *, host: str | None) -> None:
        self._assert_admin_writable()
        self._principal(name)
        with self._admin_cursor(None) as cur:
            cur.execute(f"DROP LOGIN {self.dialect.quote(name)}")

    def _db_user(self, database: str, login: str) -> str | None:
        _, rows = self._run_many(
            [
                (
                    "SELECT dp.name FROM sys.database_principals dp "
                    "JOIN sys.server_principals sp ON sp.sid = dp.sid WHERE sp.name = %s",
                    [login],
                )
            ],
            database,
        )[0]
        return rows[0][0] if rows else None

    def list_principal_grants(
        self, name: str, *, host: str | None, database: str | None
    ) -> list[GrantEntry]:
        if not any(p.name == name for p in self.list_principals()):
            raise NotFound(f"{name!r} was not found.")
        dbs = [database] if database else [d.name for d in self.list_databases()]
        out: list[GrantEntry] = []
        for db in dbs:
            self._require_database(db)
            user = self._db_user(db, name)
            if not user:
                continue
            _, rows = self._run_many(
                [
                    (
                        "SELECT pe.class_desc, pe.permission_name, pe.state_desc, s.name, o.name "
                        "FROM sys.database_permissions pe "
                        "JOIN sys.database_principals dp ON dp.principal_id = pe.grantee_principal_id "
                        "LEFT JOIN sys.objects o ON pe.class = 1 AND o.object_id = pe.major_id "
                        "LEFT JOIN sys.schemas s ON (pe.class = 1 AND s.schema_id = o.schema_id) "
                        "OR (pe.class = 3 AND s.schema_id = pe.major_id) "
                        "WHERE dp.name = %s AND pe.state IN ('G', 'W')",
                        [user],
                    )
                ],
                db,
            )[0]
            grouped: dict[tuple[str, str | None, str | None], GrantEntry] = {}
            for cls, perm, state, sch, obj in rows:
                kind = {"DATABASE": "database", "SCHEMA": "schema"}.get(cls, "table")
                e = grouped.setdefault((kind, sch, obj), GrantEntry(kind, db, sch, obj, [], False))
                e.privileges.append(perm)
                e.grantable = e.grantable or state == "GRANT_WITH_GRANT_OPTION"
            out.extend(grouped.values())
        return sorted(out, key=lambda e: (e.database or "", e.kind, e.schema or "", e.object or ""))

    def change_privileges(  # noqa: PLR0913
        self,
        name: str,
        *,
        host: str | None,
        grant: bool,
        privileges: list[str],
        target: GrantTarget,
    ) -> None:
        self._assert_admin_writable()
        self._principal(name)
        if not target.database:
            raise ValidationFailed("A database is required.")
        self._require_database(target.database)
        d = self.dialect
        if target.kind == "database":
            privs = normalise_privileges(privileges, self._DB_PRIVS)
            on = ""
        elif target.kind == "schema":
            privs = normalise_privileges(privileges, self._SCHEMA_PRIVS)
            sch = self._require_schema(target.database, target.schema)
            on = f"ON SCHEMA::{d.quote(sch)} "
        elif target.kind == "table":
            privs = normalise_privileges(privileges, self._OBJ_PRIVS)
            st = self.get_structure(target.database, target.schema, target.object or "")
            on = f"ON OBJECT::{d.qualified(st.schema, st.name)} "
        else:
            raise UnsupportedCapability("SQL Server grants apply to a database, schema or table.")
        user = self._db_user(target.database, name)
        with self._admin_cursor(target.database) as cur:
            if not user:  # a login needs a database user before it can hold permissions
                cur.execute(f"CREATE USER {d.quote(name)} FOR LOGIN {d.quote(name)}")
                user = name
            verb = "GRANT {} {}TO {}" if grant else "REVOKE {} {}FROM {}"
            cur.execute(verb.format(", ".join(privs), on, d.quote(user)))  # noqa: S608
        self._refresh()

    # ---- Phase 3: writes and streaming ------------------------------------------------

    def _connect_dedicated(self, database: str, *, writable: bool) -> Any:
        conn = _Conn(self._connect())
        self._use(conn, database)
        return conn

    def _insert(
        self, cur: Any, st: TableStructure, ns: str | None, cols: list[str], vals: list[Any]
    ) -> list[Any] | None:
        d = self.dialect
        target = d.qualified(ns, st.name)
        output = (
            " OUTPUT " + ", ".join(f"INSERTED.{d.quote(c)}" for c in st.primary_key)
            if st.primary_key
            else ""
        )
        if cols:
            sql = (  # noqa: S608
                f"INSERT INTO {target} ({', '.join(d.quote(c) for c in cols)}){output} "
                f"VALUES ({', '.join(['%s'] * len(cols))})"
            )
        else:
            sql = f"INSERT INTO {target}{output} DEFAULT VALUES"  # noqa: S608
        cur.execute(sql, vals)
        if not st.primary_key:
            return None
        row = cur.fetchone()
        return list(row) if row is not None else None

    def _adapt_complex(self, col: ColumnInfo, value: dict[str, Any] | list[Any]) -> Any:
        if col.data_type.lower().startswith(("nvarchar", "varchar")):
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
                with contextlib.suppress(Exception):
                    conn.rollback()
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
        if not write:
            for stmt in statements:
                assert_read_only_batch(stmt)
        results: list[RawResult] = []
        timed_out = threading.Event()
        with self._errors():
            raw = self._connect()
            watchdog: threading.Timer | None = None
            try:
                conn = _Conn(raw)
                self._use(conn, database)
                cur = raw.cursor()  # raw: user SQL is sent verbatim, '%' is not a placeholder
                cur.execute("SET LOCK_TIMEOUT %d" % (int(timeout_s) * 1000))  # noqa: UP031
                cur.execute("SELECT @@SPID")
                ref = {"spid": int(cur.fetchone()[0])}
                on_start(ref)
                watchdog = threading.Timer(timeout_s, self._safe_kill, args=(ref, timed_out))
                watchdog.daemon = True
                watchdog.start()
                for stmt in statements:
                    cur.execute(stmt)
                    while True:
                        desc = cur.description
                        cols = [
                            {"name": d[0], "type": _TYPE_CODES.get(d[1], str(d[1]))}
                            for d in desc or []
                        ]
                        rows = cur.fetchmany(max_rows + 1) if desc else []
                        results.append(
                            RawResult(
                                cols,
                                [tuple(r) for r in rows[:max_rows]],
                                cur.rowcount,
                                len(rows) > max_rows,
                            )
                        )
                        if not cur.nextset():
                            break
                if write and not rollback:
                    raw.commit()
            except Exception:
                if timed_out.is_set():  # the watchdog killed the session: report it as a timeout
                    raise StatementTimeout() from None
                raise
            finally:
                if watchdog:
                    watchdog.cancel()
                with contextlib.suppress(Exception):
                    raw.rollback()  # no-op after commit; the *only* path for everything else
                with contextlib.suppress(Exception):
                    raw.close()
        return results

    def _safe_kill(self, ref: dict[str, Any], flag: threading.Event | None = None) -> None:
        if flag is not None:
            flag.set()
        try:
            self.cancel_backend(ref)
        except Exception:
            logger.warning("mssql_watchdog_kill_failed", exc_info=True)

    def cancel_backend(self, ref: dict[str, Any]) -> None:
        spid = int(ref["spid"])
        with self._errors():
            conn = self._connect()
            try:
                conn.autocommit(True)
                conn.cursor().execute(f"KILL {spid}")
            finally:
                conn.close()

    def stream_query(
        self, database: str, schema: str | None, sql: str, batch: int = 1000
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        self._require_database(database)
        assert_read_only_batch(sql)
        with self._errors():
            raw = self._connect()
            try:
                self._use(_Conn(raw), database)
                cur = raw.cursor()
                cur.execute(sql)
                cols = [
                    {"name": d[0], "type": _TYPE_CODES.get(d[1], str(d[1]))}
                    for d in cur.description or []
                ]
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
                    with contextlib.suppress(Exception):
                        raw.rollback()
                    with contextlib.suppress(Exception):
                        raw.close()

        return cols, rows()


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None
