"""JSON-safe serialization of relational values with type fidelity.

* integers outside the JavaScript safe range, and all decimals, become strings
* NaN / Infinity become strings
* dates, times and intervals become ISO-8601 / canonical strings
* bytes and long text are truncated to a preview with explicit markers
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import ipaddress
import math
import uuid
from typing import Any

MAX_SAFE_INT = 2**53 - 1
BINARY_PREVIEW_BYTES = 256


def truncated_text(value: str, limit: int) -> dict[str, Any]:
    return {"$truncated": True, "preview": value[:limit], "length": len(value)}


def binary_marker(value: bytes) -> dict[str, Any]:
    return {
        "$binary": True,
        "length": len(value),
        "preview_base64": base64.b64encode(value[:BINARY_PREVIEW_BYTES]).decode(),
        "truncated": len(value) > BINARY_PREVIEW_BYTES,
    }


def to_json_value(value: Any, max_chars: int = 4096) -> Any:  # noqa: PLR0911
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= max_chars else truncated_text(value, max_chars)
    if isinstance(value, bytes | bytearray | memoryview):
        return binary_marker(bytes(value))
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID | ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    if isinstance(value, ipaddress.IPv4Interface | ipaddress.IPv6Interface):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_json_value(v, max_chars) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json_value(v, max_chars) for v in value]
    text = str(value)
    return text if len(text) <= max_chars else truncated_text(text, max_chars)


def approx_size(value: Any) -> int:
    """Cheap size estimate used to enforce the response byte limit."""
    if value is None or isinstance(value, bool | int | float):
        return 8
    if isinstance(value, str):
        return len(value) + 2
    if isinstance(value, dict):
        return sum(approx_size(k) + approx_size(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(approx_size(v) for v in value)
    return 16
