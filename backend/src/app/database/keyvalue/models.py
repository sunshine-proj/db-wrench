"""Data models for key-value (Redis) adapters. Deliberately neither relational nor document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KEY_TYPES = ("string", "hash", "list", "set", "zset", "stream")


@dataclass
class KvDatabaseInfo:
    index: int
    name: str  # the index as text; the API addresses logical databases by it
    keys: int = 0
    expires: int = 0


@dataclass
class KeyInfo:
    key: str  # opaque, URL-safe id of the raw key bytes (clients never build it)
    name: str  # display text ({"$binary": ...} marker when the key is not UTF-8)
    type: str
    ttl_ms: int | None  # None = no expiry
    binary: bool = False


@dataclass
class KeyPage:
    keys: list[KeyInfo]
    limit: int
    next_cursor: str | None = None
    total_estimate: int | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class ValuePage:
    value: Any  # JSON-safe: text | {"$binary": ...} | dict | list
    next_cursor: str | None = None
    length: int | None = None  # STRLEN / HLEN / LLEN / SCARD / ZCARD / XLEN
    truncated: bool = False


@dataclass
class KeyDetail:
    key: str
    name: str
    type: str
    ttl_ms: int | None
    encoding: str | None
    size_bytes: int | None
    version: str  # digest of DUMP: the optimistic-concurrency token
    editable: bool
    value: ValuePage
    binary: bool = False


@dataclass
class CommandResult:
    reply: Any  # JSON-safe
    truncated: bool = False
    elapsed_ms: float = 0.0
    command: str = ""
    kind: str = "read"  # read | write
