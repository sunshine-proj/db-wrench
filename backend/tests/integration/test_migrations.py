"""Alembic migrations: full up/down/up cycle and no drift from the ORM models."""

from __future__ import annotations

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from tests.integration.conftest import BACKEND, pg_dsn


@pytest.fixture
def scratch_url(pg_container) -> str:
    d = pg_dsn(pg_container, "appdb")
    with psycopg.connect(**d, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS migcheck")
        conn.execute("CREATE DATABASE migcheck")
    return f"postgresql+psycopg://{d['user']}:{d['password']}@{d['host']}:{d['port']}/migcheck"


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_downgrade_upgrade_cycle(scratch_url):
    cfg = alembic_cfg(scratch_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_models_and_migrations_do_not_drift(scratch_url):
    """Fails if a model change was made without a migration (alembic would autogenerate ops)."""
    cfg = alembic_cfg(scratch_url)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_migrated_schema_enforces_audit_immutability(scratch_url):
    command.upgrade(alembic_cfg(scratch_url), "head")
    dsn = scratch_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO audit_events (ts, action, target, result, detail, prev_hash, hash) "
            "VALUES (now(), 'x', '{}', 'success', '{}', 'a', 'b')"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            conn.execute("DELETE FROM audit_events")
