"""The reverse-proxy rate limits: strict for credential endpoints, general for the calls the
single-page app makes on every page load (throttling those logs users out of the interface)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3] / "deployment" / "nginx"
STRICT = re.compile(r"location ~ (\^/api/v1/auth/\([^)]*\)[^{ ]*) \{\s*limit_req zone=auth")


@pytest.mark.parametrize("conf", ["proxy.conf", "proxy.prod.conf"])
def test_strict_auth_limit_only_covers_credential_endpoints(conf):
    text = (ROOT / conf).read_text()
    m = STRICT.search(text)
    assert m, "strict limit location missing"
    pattern = re.compile(m.group(1))
    for path in (
        "/api/v1/auth/login", "/api/v1/auth/setup", "/api/v1/auth/password",
        "/api/v1/auth/mfa/verify", "/api/v1/auth/oidc/login", "/api/v1/auth/oidc/callback",
    ):  # fmt: skip
        assert pattern.match(path), path
    for path in (
        "/api/v1/auth/session", "/api/v1/auth/setup-status", "/api/v1/auth/oidc/config",
        "/api/v1/auth/sessions",
    ):  # fmt: skip
        assert not pattern.match(path), f"{path} must not be throttled like a login"
    fallback = re.search(r"location /api/v1/auth/ \{\s*limit_req zone=(\w+)", text)
    assert fallback and fallback.group(1) == "api"
