"""Application settings (environment-driven, ``DBW_`` prefix, ``*_FILE`` supported)."""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRIVATE_CIDRS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DBW_", extra="ignore")

    env: Literal["development", "production", "test"] = "production"
    log_level: str = "INFO"

    # Application database (SQLAlchemy URL, psycopg 3 driver)
    database_url: SecretStr
    db_pool_size: int = 10

    # Envelope encryption. Format: "kid1:<base64 32 bytes>,kid2:<base64 32 bytes>"
    encryption_keys: SecretStr
    active_kek_id: str

    # Sessions / cookies
    cookie_secure: bool = True
    cookie_name: str = "dbw_session"
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    allowed_origins: list[str] = Field(default_factory=list)
    public_url: str | None = None  # externally visible base URL (needed for SSO redirects)
    # Number of trusted reverse proxies in front of the backend (0 = ignore X-Forwarded-For).
    trusted_proxy_hops: int = 0

    # Authentication policy
    password_min_length: int = 12
    login_max_failures: int = 5
    login_lock_base_seconds: int = 30
    login_lock_max_seconds: int = 900
    login_ip_max_attempts: int = 30
    login_ip_window_seconds: int = 300
    mfa_issuer: str = "DB Wrench"

    # Network destination policy (defaults; administrators override via settings API)
    network_allow_cidrs: list[str] = Field(default_factory=lambda: list(PRIVATE_CIDRS))
    network_allow_hosts: list[str] = Field(default_factory=list)
    network_allow_loopback: bool = False  # development / test only

    # SQLite: connections may only open files below this directory (unset = SQLite disabled).
    sqlite_root: str | None = None

    # Execution limits (AGENTS.md section 6)
    statement_timeout_s: int = 30
    default_page_size: int = 100
    max_page_size: int = 1000
    max_result_rows: int = 10_000
    max_response_bytes: int = 50 * 1024 * 1024
    max_offset: int = 100_000
    max_cell_chars: int = 4096
    max_request_bytes: int = 8 * 1024 * 1024
    bulk_max_rows: int = 10_000
    export_max_rows: int = 1_000_000
    max_cell_fetch_chars: int = 4 * 1024 * 1024
    max_statement_timeout_s: int = 300
    concurrent_queries_per_user: int = 5
    history_purge_interval_s: int = 3600
    audit_forward_interval_s: int = 5
    # OpenTelemetry tracing (opt-in; needs the 'otel' extra)
    otel_enabled: bool = False
    otel_endpoint: str | None = None  # OTLP/HTTP, e.g. https://collector:4318
    otel_service_name: str = "db-wrench"
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    import_max_rows: int = 100_000
    import_max_documents: int = 50_000
    default_pool_size: int = 10
    concurrent_queries_per_connection: int = 20
    db_worker_threads: int = 32
    adapter_idle_seconds: int = 900
    adapter_cache_max: int = 100  # open adapters (each may hold pooled connections)

    metrics_enabled: bool = False

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _validate_secure_defaults(self) -> Settings:
        keys = self.parsed_keys()
        if self.active_kek_id not in keys:
            raise ValueError("DBW_ACTIVE_KEK_ID does not match any key in DBW_ENCRYPTION_KEYS")
        if self.env == "production":
            if not self.cookie_secure:
                raise ValueError("DBW_COOKIE_SECURE must be true in production")
            if self.network_allow_loopback:
                raise ValueError("DBW_NETWORK_ALLOW_LOOPBACK is not permitted in production")
        if self.sqlite_root:
            root = Path(self.sqlite_root)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("DBW_SQLITE_ROOT must be an existing absolute directory")
        return self

    def parsed_keys(self) -> dict[str, bytes]:
        keys: dict[str, bytes] = {}
        for item in self.encryption_keys.get_secret_value().split(","):
            item = item.strip()
            if not item:
                continue
            kid, _, b64 = item.partition(":")
            try:
                raw = base64.b64decode(b64, validate=True)
            except ValueError as exc:
                raise ValueError(f"encryption key {kid!r} is not valid base64") from exc
            if not kid or len(raw) != 32:
                raise ValueError(f"encryption key {kid!r} must be exactly 32 bytes")
            keys[kid] = raw
        if not keys:
            raise ValueError("DBW_ENCRYPTION_KEYS is empty")
        return keys


def _file_overrides() -> dict[str, str]:
    """Support ``DBW_<NAME>_FILE`` (Docker/Compose secrets)."""
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("DBW_") and key.endswith("_FILE") and value:
            out[key[4:-5].lower()] = Path(value).read_text(encoding="utf-8").strip()
    return out


@lru_cache
def get_settings() -> Settings:
    return Settings(**_file_overrides())  # type: ignore[arg-type]
