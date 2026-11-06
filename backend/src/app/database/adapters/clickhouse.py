"""ClickHouse adapter over the HTTP interface (httpx, no extra driver). ADR-0020.

* Read-only is enforced by the **server**: every read path sends ``readonly=1``. In that mode
  ClickHouse refuses inserts, DDL, ``SET`` (including ``SETTINGS readonly=0``), and the
  ``url()``/``file()`` table functions, and rejects multi-statement input.
* Values are never interpolated: ``%s`` placeholders become typed server-side parameters
  (``{p0:Int64}`` + ``param_p0=...``).
* There are no row identities or transactions in ClickHouse, so tables are append-only here:
  the data editor, import and bulk operations are refused (use ``INSERT`` in the SQL workspace).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import json
import os
import re
import ssl
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from app.database.core.base import Capabilities, TestResult, TlsMode
from app.database.relational.base import RelationalAdapter
from app.database.relational.models import (
    ColumnInfo,
    DatabaseInfo,
    IndexInfo,
    RawResult,
    SchemaInfo,
    TableInfo,
    TableStructure,
)
from app.database.relational.sql_builder import ClickHouseDialect
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

SYSTEM_DATABASES = ("system", "INFORMATION_SCHEMA", "information_schema")
VIEW_ENGINES = frozenset({"View", "MaterializedView", "LiveView", "WindowView"})
_ERR_RE = re.compile(r"Code:\s*(\d+)\.\s*(?:DB::Exception:\s*)?(.*)", re.S)
_TIMEOUT_CODES = {159, 160}  # TIMEOUT_EXCEEDED, TOO_SLOW
_AUTH_CODES = {192, 193, 194, 516}  # UNKNOWN_USER, WRONG_PASSWORD, REQUIRED_PASSWORD, AUTH_FAILED
_READONLY = 164
_CANCELLED = 394
_PLACEHOLDER = re.compile(r"%[%s]")
_MAX_INT64 = (1 << 63) - 1
_TEXT_ONLY = ("%", "{")  # characters that need care when text is placed in the URL


def _append_only() -> UnsupportedCapability:
    return UnsupportedCapability(
        "ClickHouse tables are append-only here: rows cannot be edited or deleted individually. "
        "Use INSERT or ALTER TABLE in the SQL workspace."
    )


def _param_type(v: Any) -> tuple[str, str]:
    """Server-side parameter type and text for a Python value (never NULL: handled inline)."""
    if isinstance(v, bool):
        return "UInt8", "1" if v else "0"
    if isinstance(v, int):
        if v > _MAX_INT64:
            return ("UInt64", str(v)) if v < (1 << 64) else ("String", str(v))
        return "Int64", str(v)
    if isinstance(v, float):
        return "Float64", repr(v)
    if isinstance(v, decimal.Decimal | dt.date | dt.time | uuid.UUID):
        return "String", str(v)
    if isinstance(v, str):
        if "\x00" in v:
            raise ValidationFailed("Values may not contain NUL characters.")
        return "String", v
    raise ValidationFailed("Unsupported parameter type.")


def bind(sql: str, params: list[Any]) -> tuple[str, dict[str, str]]:
    """Replace ``%s`` with typed server-side parameters and ``%%`` with ``%``."""
    out: dict[str, str] = {}
    it = iter(params)

    def sub(m: re.Match[str]) -> str:
        if m.group(0) == "%%":
            return "%"
        try:
            v = next(it)
        except StopIteration as exc:
            raise ValidationFailed("Parameter count mismatch.") from exc
        if v is None:
            return "NULL"
        typ, text = _param_type(v)
        name = f"p{len(out)}"
        out[f"param_{name}"] = text
        return f"{{{name}:{typ}}}"

    text = _PLACEHOLDER.sub(sub, sql)
    if next(it, StopIteration) is not StopIteration:
        raise ValidationFailed("Parameter count mismatch.")
    return text, out


class ClickHouseAdapter(RelationalAdapter):
    capabilities = Capabilities(
        family="relational",
        engine="clickhouse",
        supports_schemas=False,  # a ClickHouse database is the namespace
        supports_views=True,
        supports_foreign_keys=False,
        supports_indexes=True,
        supports_read_only_transactions=True,  # via the readonly=1 setting
        supports_keyset_pagination=False,  # no unique primary key
    )
    dialect = ClickHouseDialect()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client_obj: httpx.Client | None = None
        self._lock = threading.Lock()
        self._ca_path: str | None = None

    # ---- connection handling ----------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext | bool:
        s = self.spec
        if s.tls_mode in (TlsMode.DISABLE,):
            return False
        ctx = ssl.create_default_context()
        if s.tls_mode == TlsMode.REQUIRE:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if s.tls_ca:
            if self._ca_path is None:
                fd, path = tempfile.mkstemp(prefix="dbw-ca-", suffix=".pem")
                with os.fdopen(fd, "w") as fh:
                    fh.write(s.tls_ca)
                self._ca_path = path
            ctx.load_verify_locations(self._ca_path)
        ctx.check_hostname = s.tls_mode == TlsMode.VERIFY_FULL
        return ctx

    def _client(self) -> httpx.Client:
        with self._lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(
                    verify=self._ssl_context(),
                    timeout=httpx.Timeout(
                        self.limits.statement_timeout_s + 10,
                        connect=self.spec.connect_timeout_s,
                    ),
                    limits=httpx.Limits(max_connections=max(1, self.spec.pool_max)),
                    follow_redirects=False,
                )
            return self._client_obj

    def _request_parts(
        self,
        sql: str,
        params: list[Any],
        settings: dict[str, Any],
        database: str | None,
        raw: bool = False,
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any], str]:
        s = self.spec
        pinned = self.policy.resolve(s.host, s.port)[0]  # validated on every request
        host = f"[{pinned}]" if ":" in pinned else pinned
        scheme = "http" if s.tls_mode == TlsMode.DISABLE else "https"
        # User-authored SQL is sent verbatim ('%' is not a placeholder there).
        text, bound = (sql, {}) if raw else bind(sql, params)
        query = {**{k: str(v) for k, v in settings.items()}, **bound}
        if database:
            query["database"] = database
        headers = {
            "X-ClickHouse-User": s.username or "default",
            "X-ClickHouse-Key": s.password,
            "Host": f"{s.host}:{s.port}",
            "Content-Type": "text/plain; charset=utf-8",
        }
        ext: dict[str, Any] = {}
        if scheme == "https" and s.tls_mode != TlsMode.REQUIRE:
            ext["sni_hostname"] = s.host
        return f"{scheme}://{host}:{s.port}/", query, headers, ext, text

    def _fail(self, status: int, body: str) -> Exception:
        m = _ERR_RE.search(body)
        code = int(m.group(1)) if m else 0
        msg = scrub((m.group(2) if m else body).strip().split("\n")[0][:500], self.spec.password)
        if code in _TIMEOUT_CODES or status == 408 or "TIMEOUT_EXCEEDED" in body:
            return StatementTimeout()
        if code in _AUTH_CODES or status in (401, 403) and code == 0:
            return ConnectionFailed(msg or "Authentication failed.")
        if code == _READONLY:
            return QueryFailed(f"Read-only: {msg}")
        if code == _CANCELLED:
            return QueryFailed("The query was cancelled.")
        return QueryFailed(msg or f"ClickHouse returned HTTP {status}.")

    def _execute(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        database: str | None = None,
        read_only: bool = True,
        timeout_s: int | None = None,
        query_id: str | None = None,
        max_rows: int | None = None,
        raw: bool = False,
    ) -> tuple[list[dict[str, str]], list[tuple[Any, ...]], bool]:
        timeout = timeout_s or self.limits.statement_timeout_s
        settings: dict[str, Any] = {
            "default_format": "JSONCompact",
            "output_format_json_quote_decimals": 1,
            "output_format_json_quote_64bit_integers": 1,
            "max_execution_time": timeout,
            "timeout_overflow_mode": "throw",
            "max_result_bytes": self.limits.max_response_bytes,
            "result_overflow_mode": "break",
            "wait_end_of_query": 1,  # an error is a clean HTTP error, never half a result
        }
        if read_only:
            settings["readonly"] = 1
        if query_id:
            settings["query_id"] = query_id
        if max_rows is not None:
            settings["max_result_rows"] = max_rows + 1
            settings["result_overflow_mode"] = "break"
        url, query, headers, ext, text = self._request_parts(
            sql, params or [], settings, database, raw
        )
        try:
            r = self._client().post(
                url,
                params=query,
                headers=headers,
                content=text.encode(),
                extensions=ext,
                timeout=httpx.Timeout(
                    timeout + 10, connect=self.spec.connect_timeout_s, read=timeout + 10
                ),
            )
        except httpx.ReadTimeout as exc:
            raise StatementTimeout() from exc
        except httpx.TimeoutException as exc:
            raise ConnectionFailed("Timed out connecting to ClickHouse.") from exc
        except (httpx.TransportError, ssl.SSLError, OSError) as exc:
            raise ConnectionFailed(scrub(str(exc), self.spec.password)) from exc
        if r.status_code != 200:
            raise self._fail(r.status_code, r.text)
        if not r.content:
            return [], [], False
        try:
            data = r.json()
        except json.JSONDecodeError as exc:
            raise QueryFailed("ClickHouse returned an unreadable response.") from exc
        meta = [{"name": m["name"], "type": m["type"]} for m in data.get("meta", [])]
        rows = [tuple(x) for x in data.get("data", [])]
        truncated = False
        if max_rows is not None and len(rows) > max_rows:
            rows, truncated = rows[:max_rows], True
        return meta, rows, truncated

    def _errors(self) -> Any:
        return contextlib.nullcontext()  # errors are raised as typed domain errors directly

    def _run(
        self, sql: str, params: list[Any] | None = None, database: str | None = None
    ) -> list[tuple[Any, ...]]:
        return self._execute(sql, params, database=database)[1]

    def _require_database(self, database: str) -> None:
        if database not in {d.name for d in self.list_databases()}:
            raise NotFound(f"Database {database!r} was not found.")

    def _query(
        self, database: str, sql: str, params: list[Any]
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        self._require_database(database)
        meta, rows, _ = self._execute(sql, params, database=database)
        return [m["name"] for m in meta], rows

    def _namespace(self, database: str, schema: str | None) -> str | None:
        return database

    def close(self) -> None:
        with self._lock:
            client, self._client_obj = self._client_obj, None
        if client:
            client.close()
        if self._ca_path:
            with contextlib.suppress(OSError):
                os.unlink(self._ca_path)
            self._ca_path = None

    def test(self) -> TestResult:
        started = time.perf_counter()
        try:
            rows = self._run("SELECT version()")
        except Exception as exc:
            code = getattr(exc, "code", "connection_failed")
            return TestResult(False, error=getattr(exc, "detail", str(exc)), error_code=code)
        return TestResult(
            True, f"ClickHouse {rows[0][0]}", round((time.perf_counter() - started) * 1000, 1)
        )

    # ---- metadata ---------------------------------------------------------------------

    def list_databases(self) -> list[DatabaseInfo]:
        def load() -> list[DatabaseInfo]:
            ph = ", ".join(["%s"] * len(SYSTEM_DATABASES))
            rows = self._run(
                f"SELECT name FROM system.databases WHERE name NOT IN ({ph}) ORDER BY name",  # noqa: S608
                list(SYSTEM_DATABASES),
            )
            return [DatabaseInfo(name=r[0]) for r in rows]

        return self._meta.get_or_load(("databases",), load)

    def list_schemas(self, database: str) -> list[SchemaInfo]:
        raise UnsupportedCapability("ClickHouse has no schema level below the database.")

    def list_tables(self, database: str, schema: str | None) -> list[TableInfo]:
        self._require_database(database)
        rows = self._run(
            "SELECT name, engine, total_rows, comment FROM system.tables "
            "WHERE database = %s AND NOT is_temporary ORDER BY name",
            [database],
        )
        return [
            TableInfo(
                r[0],
                "view" if r[1] in VIEW_ENGINES else "table",
                int(r[2]) if r[2] is not None else None,
                r[3] or None,
            )
            for r in rows
        ]

    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        self._require_database(database)
        head = self._run(
            "SELECT engine, total_rows, comment, sorting_key FROM system.tables "
            "WHERE database = %s AND name = %s AND NOT is_temporary",
            [database, table],
        )
        if not head:
            raise NotFound(f"Table {database}.{table} was not found.")
        engine, est, comment, sorting_key = head[0]
        cols = self._run(
            "SELECT position, name, type, default_kind, default_expression, comment "
            "FROM system.columns WHERE database = %s AND table = %s ORDER BY position",
            [database, table],
        )
        skips = self._run(
            "SELECT name, expr, type FROM system.data_skipping_indices "
            "WHERE database = %s AND table = %s ORDER BY name",
            [database, table],
        )
        indexes: list[IndexInfo] = []
        if sorting_key:
            parts = [p.strip() for p in sorting_key.split(",") if p.strip()]
            indexes.append(IndexInfo("(sorting key)", parts, False, False, engine))
        indexes += [IndexInfo(n, [e], False, False, t) for n, e, t in skips]
        return TableStructure(
            schema=None,
            name=table,
            kind="view" if engine in VIEW_ENGINES else "table",
            columns=[
                ColumnInfo(
                    r[1],
                    r[2],
                    r[2].startswith(("Nullable(", "LowCardinality(Nullable(")),
                    r[4] if r[3] == "DEFAULT" and r[4] else None,
                    False,
                    r[3] in ("MATERIALIZED", "ALIAS", "EPHEMERAL"),
                    r[5] or None,
                    int(r[0]),
                )
                for r in cols
            ],
            primary_key=[],  # the ClickHouse "primary key" is a sparse index, not an identity
            foreign_keys=[],
            indexes=indexes,
            constraints=[],
            estimated_rows=int(est) if est is not None else None,
            comment=comment or None,
        )

    # ---- editing, import and administration: append-only --------------------------------

    def insert_row(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def update_row(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def delete_row(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def bulk_preview(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def bulk_delete(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def bulk_update(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def import_rows(self, *a: Any, **k: Any) -> Any:
        raise UnsupportedCapability("Import is not available for ClickHouse yet.")

    def fetch_cell(
        self, database: str, schema: str | None, table: str, row_key: str, column: str
    ) -> dict[str, Any]:
        raise _append_only()

    def _connect_dedicated(self, database: str, *, writable: bool) -> Any:
        raise _append_only()

    def _insert(self, *a: Any, **k: Any) -> Any:
        raise _append_only()

    def _adapt_complex(self, col: ColumnInfo, value: dict[str, Any] | list[Any]) -> Any:
        raise _append_only()

    def _stream_batches(
        self, database: str, sql: str, params: list[Any], batch: int = 1000
    ) -> Iterator[list[tuple[Any, ...]]]:
        self._require_database(database)
        yield from self._stream(sql, params, database=database, batch=batch)[1]

    # administration: databases and indexes are managed with SQL; only the structure is shown
    PROTECTED_DATABASES = frozenset(d.lower() for d in SYSTEM_DATABASES) | {"default"}

    def _admin_conn(self, database: str | None) -> Any:
        raise UnsupportedCapability("Use the SQL workspace for ClickHouse administration.")

    def _database_names(self) -> set[str]:
        return {d.name for d in self.list_databases()}

    def _forget_database(self, database: str) -> None:
        return None

    def _create_database_sql(self, name: str, options: dict[str, Any]) -> str:
        raise UnsupportedCapability("Use the SQL workspace for ClickHouse administration.")

    def _drop_database_sql(self, name: str) -> str:
        raise UnsupportedCapability("Use the SQL workspace for ClickHouse administration.")

    def create_database(self, name: str, options: dict[str, Any] | None = None) -> None:
        raise UnsupportedCapability("Use the SQL workspace for ClickHouse administration.")

    def drop_database(self, name: str) -> None:
        raise UnsupportedCapability("Use the SQL workspace for ClickHouse administration.")

    def create_index(self, *a: Any, **k: Any) -> None:
        raise UnsupportedCapability("Use ALTER TABLE ... ADD INDEX in the SQL workspace.")

    def drop_index(self, *a: Any, **k: Any) -> None:
        raise UnsupportedCapability("Use ALTER TABLE ... DROP INDEX in the SQL workspace.")

    def _create_index_sql(self, *a: Any, **k: Any) -> str:
        raise UnsupportedCapability("Use ALTER TABLE ... ADD INDEX in the SQL workspace.")

    def _drop_index_sql(self, *a: Any, **k: Any) -> str:
        raise UnsupportedCapability("Use ALTER TABLE ... DROP INDEX in the SQL workspace.")

    # ---- streaming ----------------------------------------------------------------------

    def _stream(
        self,
        sql: str,
        params: list[Any],
        *,
        database: str | None,
        batch: int = 1000,
        timeout_s: int | None = None,
        raw: bool = False,
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        timeout = timeout_s or self.limits.statement_timeout_s
        settings: dict[str, Any] = {
            "default_format": "JSONCompactEachRowWithNamesAndTypes",
            "output_format_json_quote_decimals": 1,
            "output_format_json_quote_64bit_integers": 1,
            "max_execution_time": timeout,
            "readonly": 1,
        }
        url, query, headers, ext, text = self._request_parts(sql, params, settings, database, raw)
        cm = self._client().stream(
            "POST", url, params=query, headers=headers, content=text.encode(), extensions=ext
        )
        try:
            resp = cm.__enter__()
        except httpx.TimeoutException as exc:
            raise ConnectionFailed("Timed out connecting to ClickHouse.") from exc
        except (httpx.TransportError, OSError) as exc:
            raise ConnectionFailed(scrub(str(exc), self.spec.password)) from exc
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "replace")
            cm.__exit__(None, None, None)
            raise self._fail(resp.status_code, body)
        lines = resp.iter_lines()
        try:
            names = json.loads(next(lines))
            types = json.loads(next(lines))
        except (StopIteration, json.JSONDecodeError):
            cm.__exit__(None, None, None)
            return [], iter(())
        meta = [{"name": n, "type": t} for n, t in zip(names, types, strict=True)]

        def rows() -> Iterator[list[tuple[Any, ...]]]:
            buf: list[tuple[Any, ...]] = []
            try:
                for line in lines:
                    if not line:
                        continue
                    try:
                        buf.append(tuple(json.loads(line)))
                    except json.JSONDecodeError as exc:
                        raise self._fail(200, line) from exc
                    if len(buf) >= batch:
                        yield buf
                        buf = []
                if buf:
                    yield buf
            except httpx.ReadTimeout as exc:
                raise StatementTimeout() from exc
            finally:
                cm.__exit__(None, None, None)

        return meta, rows()

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
        if write and rollback:
            raise ValidationFailed("ClickHouse has no transactions: rollback is not available.")
        if len(statements) > 1:
            raise ValidationFailed("ClickHouse runs one statement at a time.")
        qid = f"dbw-{uuid.uuid4()}"
        on_start({"query_id": qid})
        meta, rows, truncated = self._execute(
            statements[0],
            [],
            database=database,
            read_only=not write,
            timeout_s=timeout_s,
            query_id=qid,
            max_rows=max_rows,
            raw=True,
        )
        return [RawResult(meta, rows, len(rows), truncated)]

    def cancel_backend(self, ref: dict[str, Any]) -> None:
        qid = str(ref["query_id"])
        if not re.fullmatch(r"dbw-[0-9a-f-]{36}", qid):
            raise ValidationFailed("Invalid query id.")
        try:
            self._execute("KILL QUERY WHERE query_id = %s ASYNC", [qid], read_only=False)
        except Exception:
            logger.warning("clickhouse_cancel_failed", exc_info=True)
            raise

    def stream_query(
        self, database: str, schema: str | None, sql: str, batch: int = 1000
    ) -> tuple[list[dict[str, str]], Iterator[list[tuple[Any, ...]]]]:
        self._require_database(database)
        return self._stream(sql, [], database=database, batch=batch, raw=True)
