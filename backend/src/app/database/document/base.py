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
    ) -> tuple[list[dict[str, Any]], bool]: ...

    @abstractmethod
    def cancel_operation(self, comment: str) -> bool:
        """Best-effort ``killOp`` of the operation tagged with ``comment``."""
