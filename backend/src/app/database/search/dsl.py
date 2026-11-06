"""Validation of user-supplied Elasticsearch search bodies (ADR-0022).

The console can only call ``_search`` against one concrete index, so the request itself cannot
write. What remains is what a search body can *reach*: scripts (arbitrary code), other indices
(``terms`` lookups, ``more_like_this`` documents, indexed shapes), encoded sub-queries
(``wrapper``), and unbounded work. This module walks the body and refuses those, accepts only a
known set of top-level keys, and caps size, depth, ``from + size`` and aggregation size.
"""

from __future__ import annotations

import json
from typing import Any

from app.errors import StatementNotAllowed, ValidationFailed

MAX_BODY_BYTES = 256 * 1024
MAX_DEPTH = 30
MAX_NODES = 5000
MAX_WINDOW = 10_000  # index.max_result_window default
MAX_AGG_SIZE = 10_000

ALLOWED_TOP = frozenset(
    {
        "query", "aggs", "aggregations", "size", "from", "sort", "_source", "fields",
        "docvalue_fields", "highlight", "suggest", "track_total_hits", "min_score", "collapse",
        "rescore", "post_filter", "knn", "search_after", "timeout", "terminate_after", "explain",
        "version", "seq_no_primary_term", "track_scores",
    }
)  # fmt: skip
# Keys that are refused wherever they appear (compared in lower case, exact or by substring).
BLOCKED_EXACT = frozenset(
    {
        "wrapper", "percolate", "indexed_shape", "pit", "scroll", "runtime_mappings", "inline",
        "stored_fields", "_index", "ext", "indices_boost", "stored_script", "join",
    }
)  # fmt: skip
BLOCKED_SUBSTRING = ("script",)  # script, script_score, script_fields, bucket_script, ...
LOOKUP_SHAPE = frozenset({"index", "id", "path"})  # terms lookup: {"index", "id", "path"}


def _walk(node: Any, depth: int, counter: dict[str, int]) -> None:
    counter["n"] += 1
    if depth > MAX_DEPTH or counter["n"] > MAX_NODES:
        raise ValidationFailed("The search body is too deeply nested or too large.")
    if isinstance(node, dict):
        keys = {str(k) for k in node}
        if keys >= LOOKUP_SHAPE:
            raise StatementNotAllowed("Lookups into other indices are not allowed.")
        for k, v in node.items():
            low = str(k).lower()
            if low in BLOCKED_EXACT or any(s in low for s in BLOCKED_SUBSTRING):
                raise StatementNotAllowed(f"{k!r} is not allowed in a search body.")
            if low == "size" and "field" in keys and isinstance(v, int) and v > MAX_AGG_SIZE:
                raise ValidationFailed(f"Aggregation size is limited to {MAX_AGG_SIZE}.")
            _walk(v, depth + 1, counter)
    elif isinstance(node, list):
        for v in node:
            _walk(v, depth + 1, counter)
    elif isinstance(node, str) and len(node) > 32_768:
        raise ValidationFailed("A string in the search body is too long.")


def validate_search_body(body: Any, *, max_size: int) -> dict[str, Any]:
    """Return a copy of ``body`` that is safe to send to ``POST /<index>/_search``."""
    if not isinstance(body, dict):
        raise ValidationFailed("The search body must be a JSON object.")
    try:
        text = json.dumps(body)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("The search body is not valid JSON.") from exc
    if len(text) > MAX_BODY_BYTES:
        raise ValidationFailed("The search body is too large.")
    extra = sorted(set(body) - ALLOWED_TOP)
    if extra:
        raise StatementNotAllowed(f"Unsupported search option(s): {', '.join(extra)}.")
    _walk(body, 0, {"n": 0})
    out = dict(body)
    size = out.get("size", 10)
    start = out.get("from", 0)
    for name, val in (("size", size), ("from", start)):
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValidationFailed(f"{name} must be a non-negative integer.")
    if size > max_size:
        raise ValidationFailed(f"size is limited to {max_size}.")
    if size + start > MAX_WINDOW:
        raise ValidationFailed(f"from + size may not exceed {MAX_WINDOW:,}; narrow the query.")
    out["size"] = size
    th = out.get("track_total_hits", True)
    if not isinstance(th, bool) and not (isinstance(th, int) and 0 <= th <= 1_000_000):
        raise ValidationFailed("track_total_hits must be true, false or a number up to 1,000,000.")
    if th is True:
        out["track_total_hits"] = MAX_WINDOW  # exact counts of huge indices are expensive
    return out


def validate_mappings(mappings: Any) -> dict[str, Any]:
    """Mappings for index creation: same walk (no runtime fields or scripts)."""
    if mappings is None:
        return {}
    if not isinstance(mappings, dict):
        raise ValidationFailed("mappings must be a JSON object.")
    if len(json.dumps(mappings)) > MAX_BODY_BYTES:
        raise ValidationFailed("The mappings are too large.")
    _walk(mappings, 0, {"n": 0})
    return mappings
