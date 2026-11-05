"""Parsing of import payloads (CSV for relational tables, JSON / NDJSON for MongoDB).

Pure functions with hard limits; nothing here touches a database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any

from app.errors import ValidationFailed

DELIMITERS = {",": ",", ";": ";", "tab": "\t", "|": "|"}
MAX_COLUMNS = 500
MAX_CELL_CHARS = 1 << 20


def digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
        h.update(b"\x00")
    return h.hexdigest()


def parse_csv(
    content: str, *, delimiter: str, has_header: bool, max_rows: int
) -> tuple[list[str] | None, list[list[str]]]:
    """Return (header or ``None``, data rows). Every row must have the header's width."""
    if delimiter not in DELIMITERS:
        raise ValidationFailed(f"delimiter must be one of {', '.join(DELIMITERS)}.")
    if "\x00" in content:
        raise ValidationFailed("The file contains NUL characters; it is not text.")
    csv.field_size_limit(MAX_CELL_CHARS)
    reader = csv.reader(
        io.StringIO(content, newline=""), delimiter=DELIMITERS[delimiter], strict=True
    )
    header: list[str] | None = None
    rows: list[list[str]] = []
    width: int | None = None
    try:
        for lineno, row in enumerate(reader, start=1):
            if not row:
                continue  # blank line
            if width is None:
                width = len(row)
                if width > MAX_COLUMNS:
                    raise ValidationFailed(f"At most {MAX_COLUMNS} columns are supported.")
            if has_header and header is None:
                header = [h.strip() for h in row]
                if len(set(header)) != len(header) or any(not h for h in header):
                    raise ValidationFailed("Header names must be non-empty and unique.")
                continue
            if len(row) != width:
                raise ValidationFailed(f"Line {lineno} has {len(row)} fields, expected {width}.")
            rows.append(row)
            if len(rows) > max_rows:
                raise ValidationFailed(f"Too many rows (limit {max_rows}).")
    except csv.Error as exc:
        raise ValidationFailed(f"Not valid CSV: {exc}") from exc
    if has_header and header is None:
        raise ValidationFailed("The file is empty.")
    if not rows:
        raise ValidationFailed("The file has no data rows.")
    return header, rows


def parse_documents(content: str, *, fmt: str, max_docs: int) -> list[Any]:
    """JSON array or NDJSON (one object per line)."""
    if "\x00" in content:
        raise ValidationFailed("The file contains NUL characters; it is not text.")
    docs: list[Any]
    try:
        if fmt == "json":
            data = json.loads(content)
            if not isinstance(data, list):
                raise ValidationFailed("A JSON import must be an array of documents.")
            docs = data
        elif fmt == "ndjson":
            docs = [json.loads(line) for line in content.splitlines() if line.strip()]
        else:
            raise ValidationFailed("format must be json or ndjson.")
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"Not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
    if not docs:
        raise ValidationFailed("The file has no documents.")
    if len(docs) > max_docs:
        raise ValidationFailed(f"Too many documents (limit {max_docs}).")
    return docs
