"""Shared adapter contracts, connection spec, limits and capability discovery."""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from app.config import Settings
from app.connections.network import NetworkPolicy

T = TypeVar("T")


class DbType(StrEnum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    MSSQL = "mssql"
    CLICKHOUSE = "clickhouse"
    SQLITE = "sqlite"
    MONGODB = "mongodb"
    REDIS = "redis"
    ELASTICSEARCH = "elasticsearch"


_FAMILIES: dict[DbType, str] = {
    DbType.MONGODB: "document",
    DbType.REDIS: "keyvalue",
    DbType.ELASTICSEARCH: "search",
}


def family_of(db_type: DbType | str) -> str:
    """Adapter family of an engine: relational (default), document, keyvalue or search."""
    return _FAMILIES.get(DbType(db_type), "relational")


class TlsMode(StrEnum):
    DISABLE = "disable"
    REQUIRE = "require"  # encrypted, certificate not verified
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


@dataclass(frozen=True)
class Capabilities:
    family: str  # "relational" | "document"
    engine: str
    supports_schemas: bool = False
    supports_views: bool = False
    supports_sequences: bool = False
    supports_functions: bool = False
    supports_indexes: bool = True
    supports_foreign_keys: bool = False
    supports_read_only_transactions: bool = True
    supports_cancellation: bool = True
    supports_keyset_pagination: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Limits:
    statement_timeout_s: int = 30
    max_page_size: int = 1000
    max_result_rows: int = 10_000
    max_response_bytes: int = 50 * 1024 * 1024
    max_offset: int = 100_000
    max_cell_chars: int = 4096
    bulk_max_rows: int = 10_000
    export_max_rows: int = 1_000_000
    max_cell_fetch_chars: int = 4 * 1024 * 1024
    max_statement_timeout_s: int = 300

    @classmethod
    def from_settings(cls, s: Settings) -> Limits:
        return cls(
            statement_timeout_s=s.statement_timeout_s,
            max_page_size=s.max_page_size,
            max_result_rows=s.max_result_rows,
            max_response_bytes=s.max_response_bytes,
            max_offset=s.max_offset,
            max_cell_chars=s.max_cell_chars,
            bulk_max_rows=s.bulk_max_rows,
            export_max_rows=s.export_max_rows,
            max_cell_fetch_chars=s.max_cell_fetch_chars,
            max_statement_timeout_s=s.max_statement_timeout_s,
        )


@dataclass(frozen=True)
class ConnectionSpec:
    id: uuid.UUID
    db_type: DbType
    host: str
    port: int
    database: str | None
    username: str
    password: str = field(repr=False)
    tls_mode: TlsMode = TlsMode.DISABLE
    tls_ca: str | None = None
    connect_timeout_s: int = 10
    pool_max: int = 10
    options: dict[str, Any] = field(default_factory=dict)
    allow_writes: bool = False  # False for read-only connections: writers refuse to open
    version_key: str = ""  # changes whenever the stored connection changes
    parent_id: uuid.UUID | None = None  # the connection a per-user spec derives from


@dataclass
class TestResult:
    __test__ = False  # not a pytest class

    ok: bool
    server_version: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    error_code: str | None = None


class DatabaseAdapter(ABC):
    """Common lifecycle for every engine. Concrete families add their own operations."""

    capabilities: Capabilities

    def __init__(self, spec: ConnectionSpec, policy: NetworkPolicy, limits: Limits):
        self.spec = spec
        self.policy = policy
        self.limits = limits
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @abstractmethod
    def test(self) -> TestResult: ...

    @abstractmethod
    def close(self) -> None: ...


class TtlCache:
    """Tiny bounded TTL cache for metadata (explicit invalidation = adapter replacement)."""

    def __init__(self, ttl: float = 30.0, max_items: int = 500):
        self._ttl, self._max = ttl, max_items
        self._data: dict[Any, tuple[float, Any]] = {}

    def get_or_load(self, key: Any, loader: Callable[[], T]) -> T:
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        value = loader()
        if len(self._data) >= self._max:
            self._data.clear()
        self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        self._data.clear()
