"""Shared test configuration. Settings must be in the environment before ``app`` is imported."""

from __future__ import annotations

import base64
import os

os.environ.setdefault("DBW_ENV", "test")
os.environ.setdefault("DBW_COOKIE_SECURE", "false")
os.environ.setdefault("DBW_NETWORK_ALLOW_LOOPBACK", "true")
os.environ.setdefault("DBW_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/none")
os.environ.setdefault("DBW_ACTIVE_KEK_ID", "test1")
os.environ.setdefault(
    "DBW_ENCRYPTION_KEYS",
    "test1:"
    + base64.b64encode(b"k" * 32).decode()
    + ",test0:"
    + base64.b64encode(b"o" * 32).decode(),
)
os.environ.setdefault("DBW_LOGIN_MAX_FAILURES", "3")
os.environ.setdefault("DBW_LOGIN_LOCK_BASE_SECONDS", "60")
os.environ.setdefault("DBW_LOGIN_IP_MAX_ATTEMPTS", "1000")
os.environ.setdefault("DBW_ADAPTER_CACHE_MAX", "25")
