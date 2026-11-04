"""Signed, short-lived tokens that prove a bulk operation was previewed.

The token binds the user, the target object, the selection, the operation and the previewed
row count. Executing a bulk operation requires a valid token *and* the same count, so a bulk
change can never run on a selection the user has not seen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from app.errors import ValidationFailed
from app.services.runtime import get_envelope

TTL_SECONDS = 600


def _sign(payload: bytes) -> str:
    return hmac.new(get_envelope().derive_key("bulk-token"), payload, hashlib.sha256).hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def issue(user_id: uuid.UUID, claims: dict[str, Any]) -> str:
    body = {"u": str(user_id), "exp": int(time.time()) + TTL_SECONDS, **claims}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode() + "." + _sign(raw)


def verify(token: str, user_id: uuid.UUID, claims: dict[str, Any]) -> None:
    try:
        b64, sig = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64.encode())
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Invalid preview token.") from exc
    if not hmac.compare_digest(sig, _sign(raw)):
        raise ValidationFailed("Invalid preview token.")
    if body.get("exp", 0) < time.time():
        raise ValidationFailed("The preview expired. Preview the operation again.")
    if body.get("u") != str(user_id):
        raise ValidationFailed("Invalid preview token.")
    for key, value in claims.items():
        if body.get(key) != value:
            raise ValidationFailed("The preview does not match this operation. Preview again.")
