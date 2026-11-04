"""Data models shared by the relational adapters (PostgreSQL, MySQL)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class DatabaseInfo:
    name: str
    comment: str | None = None
    size_bytes: int | None = None


@dataclass
class SchemaInfo:
    name: str
    owner: str | None = None


@dataclass
class TableInfo:
    name: str
    kind: Literal["table", "view", "materialized_view", "foreign_table", "partitioned_table"]
    estimated_rows: int | None = None
    comment: str | None = None


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    is_identity: bool = False
    is_generated: bool = False
    comment: str | None = None
    ordinal: int = 0


@dataclass
class ForeignKeyInfo:
    name: str
    columns: list[str]
    ref_schema: str | None
    ref_table: str
    ref_columns: list[str]
    on_update: str | None = None
    on_delete: str | None = None


@dataclass
class IndexInfo:
    name: str
    columns: list[str]
    unique: bool
    primary: bool = False
    method: str | None = None
    definition: str | None = None


@dataclass
class ConstraintInfo:
    name: str
    kind: str  # primary_key | unique | check | exclusion
    columns: list[str] = field(default_factory=list)
    definition: str | None = None


@dataclass
class TableStructure:
    schema: str | None
    name: str
    kind: str
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKeyInfo]
    indexes: list[IndexInfo]
    constraints: list[ConstraintInfo]
    estimated_rows: int | None = None
    comment: str | None = None

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class SequenceInfo:
    name: str
    data_type: str
    start: int
    increment: int
    min_value: int
    max_value: int
    cycle: bool


@dataclass
class FunctionInfo:
    id: str  # opaque, server-validated identifier (oid)
    name: str
    arguments: str
    returns: str | None
    language: str | None
    kind: str  # function | procedure | aggregate | window
    definition: str | None = None


FILTER_OPS = (
    "eq", "ne", "lt", "lte", "gt", "gte",
    "contains", "starts_with", "ends_with",
    "is_null", "is_not_null", "in",
)  # fmt: skip


@dataclass
class SortSpec:
    column: str
    direction: Literal["asc", "desc"] = "asc"


@dataclass
class FilterSpec:
    column: str
    op: str
    value: Any = None


@dataclass
class RowQuery:
    columns: list[str] | None = None
    sort: list[SortSpec] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    search: str | None = None
    search_columns: list[str] = field(default_factory=list)
    limit: int = 100
    cursor: str | None = None


@dataclass
class RowPage:
    columns: list[dict[str, str]]
    rows: list[list[Any]]
    limit: int
    next_cursor: str | None = None
    total_estimate: int | None = None
    truncated: bool = False
    pagination: Literal["keyset", "offset", "unordered_offset"] = "keyset"
    warnings: list[str] = field(default_factory=list)
    row_keys: list[str | None] = field(default_factory=list)
    editable: bool = False


@dataclass
class RawResult:
    """One result set of a user statement, before JSON serialization."""

    columns: list[dict[str, str]]
    rows: list[tuple[Any, ...]]
    row_count: int  # affected rows for DML, -1 when not applicable
    truncated: bool = False
