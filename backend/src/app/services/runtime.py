"""Process-wide singletons (envelope encryption, adapter manager, DB worker pool)."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.connections.crypto import Envelope, StaticKeyProvider


@lru_cache
def get_envelope() -> Envelope:
    return Envelope(StaticKeyProvider.from_settings(get_settings()))


@lru_cache
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=get_settings().db_worker_threads, thread_name_prefix="dbw-managed"
    )


async def run_blocking[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking managed-database call on the bounded, dedicated worker pool."""
    from app.tracing import run_in_context

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()  # request id and trace context follow the call
    return await loop.run_in_executor(
        _executor(), lambda: ctx.run(run_in_context, fn, *args, **kwargs)
    )


def reset_runtime() -> None:
    get_envelope.cache_clear()
    ex = _executor
    if ex.cache_info().currsize:
        ex().shutdown(wait=False, cancel_futures=True)
    ex.cache_clear()
