"""Data models for search (Elasticsearch / OpenSearch) adapters. Documents, not rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchIndexInfo:
    name: str
    health: str | None = None
    status: str | None = None
    docs: int | None = None
    size_bytes: int | None = None
    primaries: int | None = None
    replicas: int | None = None
    aliases: list[str] = field(default_factory=list)


@dataclass
class FieldInfo:
    path: str
    type: str


@dataclass
class SearchIndexDetail:
    name: str
    fields: list[FieldInfo]
    settings: dict[str, Any]
    aliases: list[str]
    docs: int | None = None
    size_bytes: int | None = None


@dataclass
class DocHit:
    key: str  # opaque id of the document id (URL-safe); clients never build it
    id: str
    source: dict[str, Any]
    score: float | None = None
    seq_no: int | None = None
    primary_term: int | None = None
    sort: list[Any] | None = None


@dataclass
class SearchPage:
    hits: list[DocHit]
    total: int | None
    total_relation: str  # eq | gte
    took_ms: int
    offset: int
    size: int
    next_cursor: str | None = None
    truncated: bool = False
    timed_out: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    """Raw ``_search`` response for the console (hits keep ES' shape, JSON-safe)."""

    body: dict[str, Any]
    took_ms: int
    truncated: bool = False
