"""CSV import for relational adapters (Phase 5).

* Rows go through the same column validation and value adaptation as single-row inserts.
* **Preview** really inserts every row inside a transaction that is always rolled back (each
  row behind a savepoint), so type errors, constraint violations, foreign keys and triggers are
  reported exactly as a commit would hit them, with the row number, without changing anything.
* **Commit** is all-or-nothing: the first failing row aborts and rolls back the whole import.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from app.database.relational.models import TableStructure
from app.errors import AppError, ValidationFailed

MAX_ERRORS = 20
_BINARY_TYPES = ("bytea", "blob", "binary", "varbinary")


@dataclass
class RowError:
    row: int  # 1-based data row
    message: str


@dataclass
class ImportResult:
    rows: int
    inserted: int
    errors: list[RowError] = field(default_factory=list)
    committed: bool = False


class _DryRun(Exception):
    """Raised inside the write transaction to roll it back after a preview."""


class RelationalImport:
    @abstractmethod
    def get_structure(self, database: str, schema: str | None, table: str) -> TableStructure: ...

    @abstractmethod
    def _namespace(self, database: str, schema: str | None) -> str | None: ...

    @abstractmethod
    def _assert_editable(self, st: TableStructure, *, need_pk: bool = True) -> None: ...

    @abstractmethod
    def _prepare(
        self, st: TableStructure, values: dict[str, Any], *, allow_pk: bool
    ) -> tuple[list[str], list[Any]]: ...

    @abstractmethod
    def _write_tx(self, database: str) -> AbstractContextManager[Any]: ...

    @abstractmethod
    def _errors(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def _insert(
        self, cur: Any, st: TableStructure, ns: str | None, cols: list[str], vals: list[Any]
    ) -> list[Any] | None: ...

    def validate_import_columns(
        self, database: str, schema: str | None, table: str, columns: list[str]
    ) -> TableStructure:
        st = self.get_structure(database, schema, table)
        self._assert_editable(st, need_pk=False)
        by_name = {c.name: c for c in st.columns}
        if len(set(columns)) != len(columns):
            raise ValidationFailed("A table column can be mapped only once.")
        for name in columns:
            col = by_name.get(name)
            if col is None:
                raise ValidationFailed(f"Unknown column: {name!r}")
            if col.is_generated:
                raise ValidationFailed(f"Column {name!r} is generated and cannot be imported.")
            if any(t in col.data_type.lower() for t in _BINARY_TYPES):
                raise ValidationFailed(f"Binary column {name!r} cannot be imported from CSV.")
        return st

    def import_rows(  # noqa: PLR0913
        self,
        database: str,
        schema: str | None,
        table: str,
        columns: list[str],
        rows: Iterator[list[str | None]] | list[list[str | None]],
        *,
        commit: bool,
    ) -> ImportResult:
        st = self.validate_import_columns(database, schema, table, columns)
        ns = self._namespace(database, schema)
        result = ImportResult(rows=0, inserted=0)
        try:
            with self._write_tx(database) as cur:
                for n, raw in enumerate(rows, start=1):
                    result.rows = n
                    try:
                        cols, vals = self._prepare(
                            st, dict(zip(columns, raw, strict=True)), allow_pk=True
                        )
                        if not commit:
                            cur.execute("SAVEPOINT dbw_import")
                        with self._errors():
                            self._insert(cur, st, ns, cols, vals)
                        if not commit:
                            cur.execute("RELEASE SAVEPOINT dbw_import")
                        result.inserted += 1
                    except AppError as exc:
                        if commit:
                            raise ValidationFailed(
                                f"Row {n}: {exc.detail} Nothing was imported."
                            ) from exc
                        cur.execute("ROLLBACK TO SAVEPOINT dbw_import")
                        result.errors.append(RowError(n, exc.detail))
                        if len(result.errors) >= MAX_ERRORS:
                            break
                if not commit:
                    raise _DryRun
        except _DryRun:
            pass
        result.committed = commit
        return result
