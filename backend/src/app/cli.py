"""Operational commands: ``python -m app.cli <command>``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.audit.service import verify_chain
from app.auth import service
from app.config import get_settings
from app.connections.service import rotate_keys
from app.db import dispose_engine, session_factory


async def _reset_setup_token() -> int:
    async with session_factory()() as db, db.begin():
        token = await service.reset_setup_token(db)
    if token is None:
        print("Setup is not pending (users already exist).", file=sys.stderr)
        return 1
    print(f"Setup token: {token}")
    return 0


async def _verify_audit() -> int:
    async with session_factory()() as db:
        report = await verify_chain(db)
    if report.ok:
        print(f"Audit chain OK ({report.checked} records).")
        return 0
    print(f"AUDIT CHAIN BROKEN at record {report.first_bad_id}: {report.reason}", file=sys.stderr)
    return 2


async def _rotate_keys() -> int:
    settings = get_settings()
    async with session_factory()() as db, db.begin():
        counts = await rotate_keys(db, settings)
    print(f"Re-wrapped under KEK {settings.active_kek_id!r}: {counts}")
    return 0


COMMANDS = {
    "reset-setup-token": _reset_setup_token,
    "verify-audit": _verify_audit,
    "rotate-keys": _rotate_keys,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()

    async def run() -> int:
        try:
            return await COMMANDS[args.command]()
        finally:
            await dispose_engine()

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
