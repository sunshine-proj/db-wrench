"""Validation of client-supplied MongoDB filters, sorts and projections.

Filters arrive as MongoDB Extended JSON text. They are parsed as *plain JSON first*, walked
against an operator allowlist, and only then converted to BSON types. Anything that could
execute server-side JavaScript (``$where``, ``$function``, ``$accumulator``, ``$code``) or
evaluate arbitrary expressions (``$expr``) is rejected.
"""

from __future__ import annotations

import json
from typing import Any

from bson import json_util

from app.errors import ValidationFailed

MAX_JSON_BYTES = 64 * 1024
MAX_DEPTH = 20
MAX_NODES = 5000
MAX_LIST = 1000
MAX_REGEX_LEN = 256
MAX_FIELD_LEN = 255

LOGICAL = {"$and", "$or", "$nor"}
COMPARISON = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte"}
LIST_OPS = {"$in", "$nin", "$all"}
ELEMENT = {"$exists", "$type"}
OTHER = {"$regex", "$options", "$mod", "$size", "$not", "$elemMatch"}
FIELD_OPERATORS = COMPARISON | LIST_OPS | ELEMENT | OTHER

# Extended JSON type wrappers: a dict made only of these keys is a *value*, not an operator.
EJSON_WRAPPERS = {
    "$oid", "$date", "$numberLong", "$numberInt", "$numberDouble", "$numberDecimal",
    "$uuid", "$binary", "$timestamp", "$regularExpression", "$minKey", "$maxKey",
}  # fmt: skip
EJSON_CHILD_KEYS = {"base64", "subType", "t", "i", "pattern", "options"}


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0

    def tick(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ValidationFailed("Filter is too complex.")


def _fail(msg: str) -> ValidationFailed:
    return ValidationFailed(msg)


def parse_json(text: str, what: str) -> Any:
    if len(text.encode()) > MAX_JSON_BYTES:
        raise _fail(f"{what} is too large (limit {MAX_JSON_BYTES // 1024} KB).")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(f"{what} is not valid JSON: {exc.msg}") from exc


def _is_wrapper(d: dict[str, Any]) -> bool:
    return bool(d) and set(d) <= EJSON_WRAPPERS and len(d) == 1


def check_field_name(name: str) -> None:
    if not name or len(name) > MAX_FIELD_LEN or "\x00" in name:
        raise _fail(f"Invalid field name: {name[:40]!r}")
    for seg in name.split("."):
        if seg.startswith("$"):
            raise _fail(f"Field names may not start with '$': {name!r}")


def _value(v: Any, depth: int, b: _Budget) -> None:
    b.tick()
    if depth > MAX_DEPTH:
        raise _fail("Filter is nested too deeply.")
    if isinstance(v, dict):
        if _is_wrapper(v):
            inner = next(iter(v.values()))
            if isinstance(inner, dict) and not (
                set(inner) <= EJSON_CHILD_KEYS or _is_wrapper(inner)
            ):
                raise _fail("Invalid Extended JSON value.")
            return
        for k, sub in v.items():
            if k.startswith("$"):
                raise _fail(f"Operator {k!r} is not allowed inside a value.")
            check_field_name(k)
            _value(sub, depth + 1, b)
    elif isinstance(v, list):
        if len(v) > MAX_LIST:
            raise _fail("List is too long.")
        for item in v:
            _value(item, depth + 1, b)
    elif not (v is None or isinstance(v, str | int | float | bool)):
        raise _fail("Unsupported value type.")


def _operator_expr(expr: dict[str, Any], depth: int, b: _Budget) -> None:
    for op, arg in expr.items():
        b.tick()
        if op not in FIELD_OPERATORS:
            raise _fail(f"Operator {op!r} is not allowed.")
        if op in LIST_OPS:
            if not isinstance(arg, list) or len(arg) > MAX_LIST:
                raise _fail(f"{op} needs a list (max {MAX_LIST} items).")
            for item in arg:
                _value(item, depth + 1, b)
        elif op in COMPARISON:
            _value(arg, depth + 1, b)
        elif op == "$exists":
            if not isinstance(arg, bool):
                raise _fail("$exists needs a boolean.")
        elif op == "$type":
            ok = isinstance(arg, str | int) or (
                isinstance(arg, list) and all(isinstance(a, str | int) for a in arg)
            )
            if not ok or isinstance(arg, bool):
                raise _fail("$type needs a type name or number.")
        elif op == "$size":
            if not isinstance(arg, int) or isinstance(arg, bool) or arg < 0:
                raise _fail("$size needs a non-negative integer.")
        elif op == "$mod":
            if not (
                isinstance(arg, list)
                and len(arg) == 2
                and all(isinstance(a, int | float) and not isinstance(a, bool) for a in arg)
            ):
                raise _fail("$mod needs [divisor, remainder].")
        elif op == "$regex":
            if isinstance(arg, str):
                if len(arg) > MAX_REGEX_LEN:
                    raise _fail(f"Regular expressions are limited to {MAX_REGEX_LEN} characters.")
            elif not (isinstance(arg, dict) and set(arg) == {"$regularExpression"}):
                raise _fail("$regex needs a string.")
        elif op == "$options":
            if not (isinstance(arg, str) and set(arg) <= set("imsx")):
                raise _fail("$options may only contain i, m, s, x.")
        elif op == "$not":
            if isinstance(arg, dict) and set(arg) == {"$regularExpression"}:
                continue
            if not isinstance(arg, dict) or not arg or not all(k.startswith("$") for k in arg):
                raise _fail("$not needs an operator expression.")
            _operator_expr(arg, depth + 1, b)
        elif op == "$elemMatch":
            if not isinstance(arg, dict):
                raise _fail("$elemMatch needs a document.")
            if arg and all(k.startswith("$") and k not in LOGICAL for k in arg):
                _operator_expr(arg, depth + 1, b)
            else:
                _filter_doc(arg, depth + 1, b)


def _field_condition(val: Any, depth: int, b: _Budget) -> None:
    if depth > MAX_DEPTH:
        raise _fail("Filter is nested too deeply.")
    if (
        isinstance(val, dict)
        and val
        and not _is_wrapper(val)
        and all(k.startswith("$") for k in val)
    ):
        _operator_expr(val, depth, b)
        return
    _value(val, depth, b)


def _filter_doc(doc: dict[str, Any], depth: int, b: _Budget) -> None:
    if depth > MAX_DEPTH:
        raise _fail("Filter is nested too deeply.")
    for key, val in doc.items():
        b.tick()
        if key.startswith("$"):
            if key not in LOGICAL:
                raise _fail(f"Operator {key!r} is not allowed at the top level.")
            if not isinstance(val, list) or not val or len(val) > 100:
                raise _fail(f"{key} needs a non-empty list of filters.")
            for sub in val:
                if not isinstance(sub, dict):
                    raise _fail(f"{key} entries must be documents.")
                _filter_doc(sub, depth + 1, b)
        else:
            check_field_name(key)
            _field_condition(val, depth + 1, b)


def to_bson(value: Any) -> Any:
    """Convert validated Extended JSON (parsed) into BSON-typed Python objects."""
    return json_util.loads(json.dumps(value))


def validate_filter(text: str | None) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    doc = parse_json(text, "Filter")
    if not isinstance(doc, dict):
        raise _fail("Filter must be a JSON object.")
    _filter_doc(doc, 0, _Budget())
    result: dict[str, Any] = to_bson(doc)
    return result


def validate_sort(text: str | None) -> list[tuple[str, int]]:
    if not text or not text.strip():
        return []
    doc = parse_json(text, "Sort")
    if not isinstance(doc, dict) or len(doc) > 5:
        raise _fail("Sort must be an object with at most 5 fields.")
    out: list[tuple[str, int]] = []
    for field_name, direction in doc.items():
        check_field_name(field_name)
        if direction not in (1, -1) or isinstance(direction, bool):
            raise _fail("Sort directions must be 1 or -1.")
        out.append((field_name, int(direction)))
    return out


def validate_projection(text: str | None) -> dict[str, int] | None:
    if not text or not text.strip():
        return None
    doc = parse_json(text, "Projection")
    if not isinstance(doc, dict) or not doc or len(doc) > 100:
        raise _fail("Projection must be an object with 1-100 fields.")
    out: dict[str, int] = {}
    for field_name, flag in doc.items():
        check_field_name(field_name)
        if flag not in (0, 1, True, False):
            raise _fail("Projection values must be 0 or 1.")
        out[field_name] = 1 if flag else 0
    includes = {v for k, v in out.items() if k != "_id"}
    if len(includes) > 1:
        raise _fail("Projection cannot mix included and excluded fields.")
    return out


def encode_id_cursor(last_id: Any) -> str:
    import base64

    payload = json.dumps({"k": json.loads(json_util.dumps(last_id))}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_id_cursor(cursor: str) -> Any:
    import base64

    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        key = data["k"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise _fail("Invalid cursor.") from exc
    _value(key, 0, _Budget())
    return to_bson(key)


def encode_offset_cursor(offset: int) -> str:
    import base64

    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def decode_offset_cursor(cursor: str, max_offset: int) -> int:
    import base64

    try:
        off = json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise _fail("Invalid cursor.") from exc
    if not isinstance(off, int) or isinstance(off, bool) or off < 0:
        raise _fail("Invalid cursor.")
    if off > max_offset:
        raise _fail(f"Offset pagination is limited to {max_offset} documents; add a filter.")
    return off


# ----------------------------------------------------------------------------- documents


MAX_DOC_BYTES = 1024 * 1024


def _contains_regex(value: Any) -> bool:
    from bson import Regex

    if isinstance(value, Regex) or hasattr(value, "pattern") and hasattr(value, "flags"):
        return True
    if isinstance(value, dict):
        return any(_contains_regex(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_regex(v) for v in value)
    return False


def validate_document(doc: Any) -> dict[str, Any]:
    """Validate a client-supplied document (parsed Extended JSON) and convert it to BSON types.

    Field names may not start with ``$`` and may not contain NUL; the encoded size is capped.
    """
    import bson

    if isinstance(doc, str):
        doc = parse_json(doc, "Document")
    if not isinstance(doc, dict):
        raise _fail("A document must be a JSON object.")
    b = _Budget()
    for key, val in doc.items():
        check_field_name(key)
        _value(val, 1, b)
    result: dict[str, Any] = to_bson(doc)
    if _contains_regex(result):
        raise _fail("Regular-expression values cannot be edited here.")
    if len(bson.encode(result)) > MAX_DOC_BYTES:
        raise _fail("Document is larger than 1 MB.")
    return result


def validate_set(doc: Any) -> dict[str, Any]:
    """Validate the field->value map of a bulk ``$set`` (dotted paths allowed, no ``_id``)."""
    if isinstance(doc, str):
        doc = parse_json(doc, "Update")
    if not isinstance(doc, dict) or not doc or len(doc) > 100:
        raise _fail("The update must be an object with 1-100 fields.")
    out: dict[str, Any] = {}
    for key, val in doc.items():
        check_field_name(key)
        if key == "_id" or key.startswith("_id."):
            raise _fail("_id cannot be changed.")
        _value(val, 1, _Budget())
        out[key] = to_bson(val)
    return out


def filter_fields(flt: Any) -> set[str]:
    """Every field path a (validated) filter touches, for permission checks on masked fields."""
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in LOGICAL and isinstance(val, list):
                    for sub in val:
                        walk(sub, prefix)
                elif key.startswith("$"):
                    if key == "$elemMatch" or key == "$not":
                        walk(val, prefix)
                else:
                    path = f"{prefix}.{key}" if prefix else key
                    found.add(path)
                    walk(val, path)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix)

    walk(flt, "")
    return found


def path_is_masked(path: str, masked: set[str] | frozenset[str]) -> bool:
    """True if ``path`` equals, lies below, or contains a masked field path."""
    return any(path == m or path.startswith(m + ".") or m.startswith(path + ".") for m in masked)


def mask_document(doc: Any, masked: set[str] | frozenset[str]) -> Any:
    """Replace values at masked paths with a marker (arrays are traversed element-wise)."""
    if not masked:
        return doc

    def apply(node: Any, prefix: str) -> Any:
        if isinstance(node, list):
            return [apply(x, prefix) for x in node]
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, val in node.items():
                path = f"{prefix}.{key}" if prefix else key
                if path in masked:
                    out[key] = None if val is None else {"$masked": True}
                else:
                    out[key] = apply(val, path)
            return out
        return node

    return apply(doc, "")
