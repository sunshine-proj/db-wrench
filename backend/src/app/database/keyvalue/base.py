"""Key-value adapter contract (Redis). No relational or document concepts leak in here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.database.core.base import DatabaseAdapter
from app.database.keyvalue.models import (
    CommandResult,
    KeyDetail,
    KeyPage,
    KvDatabaseInfo,
)


class KeyValueAdapter(DatabaseAdapter, ABC):
    @abstractmethod
    def list_databases(self) -> list[KvDatabaseInfo]: ...

    @abstractmethod
    def scan_keys(
        self,
        database: str,
        *,
        pattern: str | None,
        key_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> KeyPage: ...

    @abstractmethod
    def get_key(
        self, database: str, key: str, *, cursor: str | None, limit: int, full: bool = False
    ) -> KeyDetail: ...

    @abstractmethod
    def put_key(
        self,
        database: str,
        key: str | None,
        *,
        new_name: str | None,
        type: str,
        value: Any,
        ttl_ms: int | None,
        keep_ttl: bool,
        version: str | None,
    ) -> KeyDetail:
        """Create (``key is None``, needs ``new_name``) or replace a key atomically. Replacing
        requires the ``version`` from the last read; a mismatch is a conflict."""

    @abstractmethod
    def delete_key(self, database: str, key: str, version: str | None) -> None: ...

    @abstractmethod
    def set_ttl(self, database: str, key: str, ttl_ms: int | None) -> None: ...

    @abstractmethod
    def run_command(self, database: str, args: list[bytes], *, write: bool) -> CommandResult: ...
