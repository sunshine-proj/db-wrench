"""Alembic environment: URL comes from DBW_DATABASE_URL (or DBW_DATABASE_URL_FILE)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.models import Base

target_metadata = Base.metadata


def _url() -> str:
    # An explicit sqlalchemy.url (used by tests against scratch databases) wins over settings.
    return context.config.get_main_option("sqlalchemy.url") or (
        get_settings().database_url.get_secret_value()
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
