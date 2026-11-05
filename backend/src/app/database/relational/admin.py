"""Structural administration for relational adapters (Phase 5): databases, schemas, indexes.

Rules enforced here, independent of the HTTP layer:

* every operation needs a writable connection (``spec.allow_writes``) and uses a dedicated,
  non-pooled connection in autocommit mode (``CREATE/DROP DATABASE`` cannot run in a
  transaction on PostgreSQL, and MySQL DDL commits implicitly), closed afterwards;
* names of **new** objects are validated against a conservative pattern and dialect-quoted;
  names of **existing** objects must appear in fetched metadata, never trusted from the client;
* system objects and the database the connection itself is configured for cannot be dropped;
* drops use the restrictive form (no ``CASCADE``): dependent objects make the statement fail
  instead of silently disappearing.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from app.database.core.base import ConnectionSpec, Limits
from app.database.core.base import TtlCache as _TtlCache
from app.database.relational.models import TableStructure
from app.database.relational.sql_builder import Dialect
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

NEW_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
MAX_INDEX_COLUMNS = 16
MYSQL_CHARSETS = frozenset({"utf8mb4", "utf8mb3", "latin1", "ascii"})


def validate_new_name(kind: str, name: str) -> str:
    """Names we create. Deliberately narrower than what the engines accept: no quoting
    surprises, no whitespace, no unicode confusables, and short enough for every engine."""
    if not isinstance(name, str) or not NEW_NAME.fullmatch(name):
        raise ValidationFailed(
            f"Invalid {kind} name. Use 1-63 letters, digits, '_' or '$', starting with a "
            "letter or underscore."
        )
    return name


class RelationalAdmin:
    """Mixed into :class:`RelationalAdapter`."""

    spec: ConnectionSpec
    limits: Limits
    dialect: Dialect
    _meta: _TtlCache

    # Protected names are case-insensitive and never droppable through this API.
    PROTECTED_DATABASES: frozenset[str] = frozenset()
    PROTECTED_SCHEMAS: frozenset[str] = frozenset()

    @abstractmethod
    def _admin_conn(self, database: str | None) -> Any:
        """Writable, autocommit, dedicated connection (``None`` = maintenance database)."""

    @abstractmethod
    def _errors(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def _database_names(self) -> set[str]: ...

    @abstractmethod
    def get_structure(self, database: str, schema: str | None, table: str) -> TableStructure: ...

    @abstractmethod
    def list_schemas(self, database: str) -> list[Any]: ...

    @abstractmethod
    def _forget_database(self, database: str) -> None:
        """Release pooled connections to a database that is about to be dropped."""

    def _assert_admin_writable(self) -> None:
        if not self.spec.allow_writes:
            raise Forbidden("This connection is read-only.")

    @contextmanager
    def _admin_cursor(self, database: str | None) -> Any:
        self._assert_admin_writable()
        with self._errors():
            conn = self._admin_conn(database)
            try:
                cur = conn.cursor()
                yield cur
            finally:
                conn.close()

    def _refresh(self) -> None:
        self._meta.clear()

    # ---- databases --------------------------------------------------------------------

    def create_database(self, name: str, options: dict[str, Any] | None = None) -> None:
        validate_new_name("database", name)
        self._assert_admin_writable()
        if name.lower() in {d.lower() for d in self._database_names()}:
            raise Conflict(f"Database {name!r} already exists.")
        sql = self._create_database_sql(name, options or {})
        with self._admin_cursor(None) as cur:
            cur.execute(sql)
        self._refresh()

    def drop_database(self, name: str) -> None:
        self._assert_admin_writable()
        existing = self._database_names()
        if name not in existing:
            raise NotFound(f"Database {name!r} was not found.")
        if name.lower() in self.PROTECTED_DATABASES:
            raise Forbidden(f"{name!r} is a system database and cannot be dropped.")
        if self.spec.database and name == self.spec.database:
            raise Conflict(
                "This is the database the connection is configured for. Point the connection "
                "elsewhere before dropping it."
            )
        self._forget_database(name)
        with self._admin_cursor(None) as cur:
            cur.execute(self._drop_database_sql(name))
        self._refresh()

    @abstractmethod
    def _create_database_sql(self, name: str, options: dict[str, Any]) -> str: ...

    @abstractmethod
    def _drop_database_sql(self, name: str) -> str: ...

    # ---- schemas (PostgreSQL only) ----------------------------------------------------

    def create_schema(self, database: str, schema: str) -> None:
        raise _no_schemas()

    def drop_schema(self, database: str, schema: str) -> None:
        raise _no_schemas()

    # ---- indexes ----------------------------------------------------------------------

    def create_index(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        table: str,
        name: str,
        columns: list[str],
        *,
        unique: bool = False,
    ) -> None:
        self._assert_admin_writable()
        validate_new_name("index", name)
        st = self.get_structure(database, schema, table)
        if st.kind not in ("table", "partitioned_table", "materialized_view"):
            raise ValidationFailed("Indexes can only be created on tables.")
        if not columns or len(columns) > MAX_INDEX_COLUMNS:
            raise ValidationFailed(f"An index needs 1-{MAX_INDEX_COLUMNS} columns.")
        if len(set(columns)) != len(columns):
            raise ValidationFailed("A column may appear only once in an index.")
        known = set(st.column_names())
        missing = [c for c in columns if c not in known]
        if missing:
            raise ValidationFailed(f"Unknown column(s): {', '.join(missing)}.")
        if name.lower() in {i.name.lower() for i in st.indexes}:
            raise Conflict(f"An index named {name!r} already exists on this table.")
        sql = self._create_index_sql(schema, st.name, name, columns, unique)
        with self._admin_cursor(database) as cur:
            cur.execute(sql)
        self._refresh()

    def drop_index(self, database: str, schema: str | None, table: str, name: str) -> None:
        self._assert_admin_writable()
        st = self.get_structure(database, schema, table)
        idx = next((i for i in st.indexes if i.name == name), None)
        if idx is None:
            raise NotFound(f"Index {name!r} was not found on this table.")
        constraint_backed = {c.name for c in st.constraints if c.kind in ("primary_key", "unique")}
        if idx.primary or name in constraint_backed:
            raise Conflict(
                "This index enforces a primary key or unique constraint; drop the constraint "
                "with SQL instead."
            )
        with self._admin_cursor(database) as cur:
            cur.execute(self._drop_index_sql(schema, st.name, name))
        self._refresh()

    @abstractmethod
    def _create_index_sql(
        self, schema: str | None, table: str, name: str, columns: list[str], unique: bool
    ) -> str: ...

    @abstractmethod
    def _drop_index_sql(self, schema: str | None, table: str, name: str) -> str: ...


def _no_schemas() -> Exception:
    from app.errors import UnsupportedCapability

    return UnsupportedCapability("This engine has no schema level (a database is the namespace).")
