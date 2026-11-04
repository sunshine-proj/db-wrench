"""Validation of client-supplied MongoDB aggregation pipelines (Phase 4).

* Stages come from an **allowlist**; unknown stages are rejected.
* ``$match`` uses the strict filter validator (no ``$expr``, no ``$where``).
* Operators that run server-side JavaScript or write data (``$function``, ``$accumulator``,
  ``$where``, ``$out``, ``$merge``) are rejected **anywhere** in the pipeline.
* Stages that read other collections (``$lookup``, ``$unionWith``, ``$graphLookup``) may only
  name collections of the *same database*; the targets are returned so the caller can check them.
* Size, depth and stage-count limits bound the work a pipeline can ask for.
"""

from __future__ import annotations

import json
from typing import Any

from app.database.document.filters import (
    MAX_DEPTH,
    MAX_NODES,
    _filter_doc,
    check_field_name,
    parse_json,
    to_bson,
)
from app.database.document.filters import _Budget as _FilterBudget
from app.errors import ValidationFailed

MAX_STAGES = 30
MAX_FACETS = 10

ALLOWED_STAGES = {
    "$match", "$project", "$addFields", "$set", "$unset", "$group", "$sort", "$limit", "$skip",
    "$unwind", "$count", "$sample", "$sortByCount", "$replaceRoot", "$replaceWith", "$bucket",
    "$bucketAuto", "$facet", "$lookup", "$unionWith", "$graphLookup",
}  # fmt: skip
FORBIDDEN_ANYWHERE = {
    "$function", "$accumulator", "$where", "$out", "$merge", "$currentOp", "$listSessions",
    "$listLocalSessions", "$planCacheStats", "$changeStream", "$collStats", "$indexStats",
    "$search", "$searchMeta", "$vectorSearch", "$documents", "$listSearchIndexes",
}  # fmt: skip


def _fail(msg: str) -> ValidationFailed:
    return ValidationFailed(msg)


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0

    def tick(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES * 4:
            raise _fail("Pipeline is too complex.")


def _scan_forbidden(node: Any, depth: int, b: _Budget) -> None:
    b.tick()
    if depth > MAX_DEPTH:
        raise _fail("Pipeline is nested too deeply.")
    if isinstance(node, dict):
        for key, val in node.items():
            if key in FORBIDDEN_ANYWHERE:
                raise _fail(f"Operator {key!r} is not allowed.")
            if "\x00" in key:
                raise _fail("Invalid field name.")
            _scan_forbidden(val, depth + 1, b)
    elif isinstance(node, list):
        for item in node:
            _scan_forbidden(item, depth + 1, b)


def _collection_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or "\x00" in value:
        raise _fail(f"{what} must name a collection in this database.")
    if value.startswith("system."):
        raise _fail("System collections cannot be read.")
    return value


def _stage(stage: Any, targets: set[str], depth: int, b: _Budget) -> None:
    if not isinstance(stage, dict) or len(stage) != 1:
        raise _fail("Each pipeline stage must be an object with exactly one operator.")
    name, body = next(iter(stage.items()))
    if name not in ALLOWED_STAGES:
        raise _fail(f"Pipeline stage {name!r} is not allowed.")
    if name == "$match":
        if not isinstance(body, dict):
            raise _fail("$match needs a filter document.")
        _filter_doc(body, 0, _FilterBudget())
    elif name in ("$limit", "$skip"):
        if not isinstance(body, int) or isinstance(body, bool) or body < 0:
            raise _fail(f"{name} needs a non-negative integer.")
    elif name == "$sample":
        if not (isinstance(body, dict) and isinstance(body.get("size"), int)):
            raise _fail("$sample needs {size: <integer>}.")
    elif name == "$count":
        if not isinstance(body, str) or not body or body.startswith("$") or "." in body:
            raise _fail("$count needs an output field name.")
    elif name == "$lookup":
        if not isinstance(body, dict):
            raise _fail("$lookup needs a document.")
        targets.add(_collection_name(body.get("from"), "$lookup.from"))
        sub = body.get("pipeline")
        if sub is not None:
            _pipeline(sub, targets, depth + 1, b)
    elif name == "$unionWith":
        if isinstance(body, str):
            targets.add(_collection_name(body, "$unionWith"))
        elif isinstance(body, dict):
            targets.add(_collection_name(body.get("coll"), "$unionWith.coll"))
            if body.get("pipeline") is not None:
                _pipeline(body["pipeline"], targets, depth + 1, b)
        else:
            raise _fail("$unionWith needs a collection name or document.")
    elif name == "$graphLookup":
        if not isinstance(body, dict):
            raise _fail("$graphLookup needs a document.")
        targets.add(_collection_name(body.get("from"), "$graphLookup.from"))
        md = body.get("maxDepth")
        if md is None or not isinstance(md, int) or not 0 <= md <= 20:
            raise _fail("$graphLookup needs maxDepth between 0 and 20.")
    elif name == "$facet":
        if not isinstance(body, dict) or not 0 < len(body) <= MAX_FACETS:
            raise _fail(f"$facet needs 1-{MAX_FACETS} named sub-pipelines.")
        for key, sub in body.items():
            check_field_name(key)
            _pipeline(sub, targets, depth + 1, b)
    elif name == "$unset":
        if not isinstance(body, str | list):
            raise _fail("$unset needs a field name or a list of field names.")
    # remaining stages ($project, $group, ...) are covered by the deep scan


def _pipeline(stages: Any, targets: set[str], depth: int, b: _Budget) -> None:
    if not isinstance(stages, list) or not stages:
        raise _fail("A pipeline must be a non-empty list of stages.")
    if len(stages) > MAX_STAGES:
        raise _fail(f"A pipeline may have at most {MAX_STAGES} stages.")
    if depth > 4:
        raise _fail("Sub-pipelines are nested too deeply.")
    for s in stages:
        _scan_forbidden(s, 0, b)
        _stage(s, targets, depth, b)


def validate_pipeline(
    raw: str | list[Any], max_limit: int
) -> tuple[list[dict[str, Any]], set[str]]:
    """Validate and convert a pipeline. Returns (BSON-typed stages, collections it reads).

    A final ``$limit`` of ``max_limit + 1`` is appended unless the pipeline already ends with a
    smaller limit, so a pipeline can never return unbounded output."""
    data = parse_json(raw, "Pipeline") if isinstance(raw, str) else raw
    targets: set[str] = set()
    _pipeline(data, targets, 0, _Budget())
    stages: list[dict[str, Any]] = to_bson(json.loads(json.dumps(data)))
    last = stages[-1]
    if not ("$limit" in last and last["$limit"] <= max_limit):
        stages.append({"$limit": max_limit + 1})
    return stages, targets
