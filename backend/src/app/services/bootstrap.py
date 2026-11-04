"""Startup tasks: schema check, built-in roles, first-run setup token."""

from __future__ import annotations

from sqlalchemy import text

from app.api.admin import expected_revision
from app.auth import service
from app.db import get_engine, session_factory
from app.errors import SchemaMismatch
from app.observability import logger


async def check_schema() -> None:
    expected = expected_revision()
    try:
        async with get_engine().connect() as conn:
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as exc:
        raise SchemaMismatch(
            "The application database is not initialised. Run `make migrate` "
            "(or the migrate container) before starting the backend."
        ) from exc
    if expected and current != expected:
        raise SchemaMismatch(
            f"Application database schema is at {current!r} but this release expects "
            f"{expected!r}. Run the migrations first."
        )


async def run_startup() -> None:
    await check_schema()
    async with session_factory()() as db, db.begin():
        await service.ensure_builtin_roles(db)
        token = await service.ensure_setup_token(db)
        pending = await service.setup_required(db)
    if token:
        logger.warning(
            "FIRST-RUN SETUP: no users exist. Open the application and use this one-time "
            "setup token to create the first Super Admin: %s",
            token,
        )
    elif pending:
        logger.warning(
            "FIRST-RUN SETUP is pending but the setup token was created by an earlier start "
            "and cannot be shown again. Run `python -m app.cli reset-setup-token`."
        )
