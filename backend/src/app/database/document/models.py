"""Data models for document (MongoDB) adapters. Deliberately not relational."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocDatabaseInfo:
    name: str
    size_on_disk: int | None = None
    empty: bool | None = None


@dataclass
class CollectionInfo:
    name: str
    type: str = "collection"  # collection | view | timeseries


@dataclass
class DocIndexInfo:
    name: str
    keys: list[list[Any]]  # [[field, direction], ...] direction kept as stored (1, -1, "text", ...)
    unique: bool = False
    sparse: bool = False
    ttl_seconds: int | None = None
    partial_filter: dict[str, Any] | None = None


@dataclass
class CollectionDetail:
    name: str
    type: str
    options: dict[str, Any] = field(default_factory=dict)
    capped: bool = False
    stats: dict[str, Any] = field(default_factory=dict)
    indexes: list[DocIndexInfo] = field(default_factory=list)


@dataclass
class DocQuery:
    filter: dict[str, Any] = field(default_factory=dict)  # already validated, BSON-typed
    projection: dict[str, int] | None = None
    sort: list[tuple[str, int]] = field(default_factory=list)
    limit: int = 50
    cursor: str | None = None


@dataclass
class DocPage:
    documents: list[dict[str, Any]]  # canonical Extended JSON
    limit: int
    next_cursor: str | None = None
    total_estimate: int | None = None
    truncated: bool = False
    pagination: str = "keyset"
    warnings: list[str] = field(default_factory=list)
    doc_keys: list[str | None] = field(default_factory=list)
