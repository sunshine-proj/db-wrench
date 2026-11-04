"""Print the OpenAPI document. Used by `make openapi` and the CI breaking-change check."""

from __future__ import annotations

import base64
import json
import os

os.environ.setdefault("DBW_ENV", "test")
os.environ.setdefault("DBW_COOKIE_SECURE", "false")
os.environ.setdefault("DBW_DATABASE_URL", "postgresql+psycopg://u:p@localhost/none")
os.environ.setdefault("DBW_ACTIVE_KEK_ID", "k1")
os.environ.setdefault("DBW_ENCRYPTION_KEYS", "k1:" + base64.b64encode(b"x" * 32).decode())

from app.main import app  # noqa: E402

print(json.dumps(app.openapi(), indent=2, sort_keys=True))
