"""Document adapter contract (MongoDB). No relational concepts leak in here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from app.database.core.base import DatabaseAdapter
from app.database.document.models import (
    CollectionDetail,
    CollectionInfo,
    DocDatabaseInfo,
    DocPage,
    DocQuery,
)


class DocumentAdapter(DatabaseAdapter, ABC):
    @abstractmethod
    def list_databases(self) -> list[DocDatabaseInfo]: ...

    @abstractmethod
    def list_collections(self, database: str) -> list[CollectionInfo]: ...

    @abstractmethod
    def get_collection(self, database: str, collection: str) -> CollectionDetail: ...

    @abstractmethod
    def find_documents(self, database: str, collection: str, q: DocQuery) -> DocPage: ...

    # ---- writes (Phase 3) -------------------------------------------------------------

    @abstractmethod
    def insert_document(
        self, database: str, collection: str, doc: dict[str, Any]
    ) -> dict[str, Any]: ...

    @abstractmethod
    def replace_document(
        self,
        database: str,
        collection: str,
        doc_key: str,
        original: dict[str, Any],
        new: dict[str, Any],
    ) -> dict[str, Any]: ...

    @abstractmethod
    def delete_document(
        self, database: str, collection: str, doc_key: str, original: dict[str, Any] | None
    ) -> None: ...

    @abstractmethod
    def bulk_preview(
        self, database: str, collection: str, flt: dict[str, Any]
    ) -> tuple[int, list[dict[str, Any]]]: ...

    @abstractmethod
    def bulk_delete(
        self, database: str, collection: str, flt: dict[str, Any], expected_count: int
    ) -> int: ...

    @abstractmethod
    def bulk_set(
        self,
        database: str,
        collection: str,
        flt: dict[str, Any],
        values: dict[str, Any],
        expected_count: int,
    ) -> int: ...

    @abstractmethod
    def export_documents(
        self,
        database: str,
        collection: str,
        flt: dict[str, Any],
        sort: list[tuple[str, int]],
        projection: dict[str, int] | None,
    ) -> Iterator[list[dict[str, Any]]]: ...

    # ---- workspace (Phase 4) ----------------------------------------------------------

    @abstractmethod
    def run_find(
        self, database: str, collection: str, q: DocQuery, *, timeout_s: int, comment: str
    ) -> DocPage: ...

    @abstractmethod
    def run_aggregate(
        self,
        database: str,
        collection: str,
        pipeline: list[dict[str, Any]],
        *,
        max_docs: int,
        timeout_s: int,
        comment: str,
        write: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]: ...

    @abstractmethod
    def cancel_operation(self, comment: str) -> bool:
        """Best-effort ``killOp`` of the operation tagged with ``comment``."""

    # ---- administration (Phase 5) -----------------------------------------------------

    @abstractmethod
    def create_database(self, name: str, first_collection: str) -> None:
        """MongoDB creates databases lazily, so one is created together with a collection."""

    @abstractmethod
    def drop_database(self, name: str) -> None: ...

    @abstractmethod
    def create_collection(
        self, database: str, name: str, options: dict[str, Any] | None = None
    ) -> None: ...

    @abstractmethod
    def drop_collection(self, database: str, collection: str) -> None: ...

    @abstractmethod
    def create_index(  # noqa: PLR0913
        self,
        database: str,
        collection: str,
        keys: list[tuple[str, Any]],
        *,
        name: str | None = None,
        unique: bool = False,
        sparse: bool = False,
        ttl_seconds: int | None = None,
        partial_filter: dict[str, Any] | None = None,
    ) -> str: ...

    @abstractmethod
    def drop_index(self, database: str, collection: str, name: str) -> None: ...

    # ---- import (Phase 5) -------------------------------------------------------------

    @abstractmethod
    def existing_ids(self, database: str, collection: str, ids: list[Any]) -> set[Any]:
        """Which of ``ids`` already exist (used to report import collisions up front)."""

    @abstractmethod
    def import_documents(
        self, database: str, collection: str, docs: list[dict[str, Any]]
    ) -> tuple[int, list[dict[str, Any]]]:
        """Insert (never upsert). Returns (inserted, per-document errors). Not atomic."""

    # ---- users, roles and privileges (Phase 6) ----------------------------------------

    @abstractmethod
    def list_users(self, database: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def create_user(
        self, database: str, name: str, password: str, roles: list[dict[str, str]]
    ) -> None: ...

    @abstractmethod
    def set_user_password(self, database: str, name: str, password: str) -> None: ...

    @abstractmethod
    def drop_user(self, database: str, name: str) -> None: ...

    @abstractmethod
    def change_user_roles(
        self, database: str, name: str, roles: list[dict[str, str]], *, grant: bool
    ) -> None: ...

    @abstractmethod
    def list_roles(self, database: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def create_role(
        self,
        database: str,
        name: str,
        privileges: list[dict[str, Any]],
        roles: list[dict[str, str]],
    ) -> None: ...

    @abstractmethod
    def drop_role(self, database: str, role: str) -> None: ...

    @abstractmethod
    def change_role_privileges(
        self, database: str, role: str, privileges: list[dict[str, Any]], *, grant: bool
    ) -> None: ...
