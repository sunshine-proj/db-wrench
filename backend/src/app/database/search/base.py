"""Search adapter contract (Elasticsearch / OpenSearch). Documents and indices only."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.database.core.base import DatabaseAdapter
from app.database.search.models import (
    DocHit,
    SearchIndexDetail,
    SearchIndexInfo,
    SearchPage,
    SearchResult,
)


class SearchAdapter(DatabaseAdapter, ABC):
    @abstractmethod
    def list_indices(self) -> list[SearchIndexInfo]: ...

    @abstractmethod
    def get_index(self, index: str) -> SearchIndexDetail: ...

    @abstractmethod
    def search_documents(
        self,
        index: str,
        *,
        text: str | None,
        sort: list[tuple[str, str]],
        offset: int,
        size: int,
    ) -> SearchPage: ...

    @abstractmethod
    def get_document(self, index: str, key: str) -> DocHit: ...

    @abstractmethod
    def put_document(
        self,
        index: str,
        key: str | None,
        *,
        new_id: str | None,
        source: dict[str, Any],
        seq_no: int | None,
        primary_term: int | None,
    ) -> DocHit:
        """Create (``key is None``; optional ``new_id``) or replace with optimistic concurrency."""

    @abstractmethod
    def delete_document(
        self, index: str, key: str, seq_no: int | None, primary_term: int | None
    ) -> None: ...

    @abstractmethod
    def create_index(
        self, name: str, *, shards: int, replicas: int, mappings: dict[str, Any] | None
    ) -> None: ...

    @abstractmethod
    def delete_index(self, name: str) -> None: ...

    @abstractmethod
    def run_search(self, index: str, body: dict[str, Any], *, timeout_s: int) -> SearchResult: ...
