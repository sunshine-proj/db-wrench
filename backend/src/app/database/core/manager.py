"""Adapter registry and per-connection adapter cache (connection lifecycle + pools)."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

from app.connections.network import NetworkPolicy
from app.database.core.base import ConnectionSpec, DatabaseAdapter, DbType, Limits
from app.errors import UnsupportedCapability
from app.observability import logger

AdapterFactory = Callable[[ConnectionSpec, NetworkPolicy, Limits], DatabaseAdapter]


class AdapterManager:
    def __init__(self, idle_seconds: int = 900, max_adapters: int = 100):
        self._factories: dict[DbType, AdapterFactory] = {}
        self._cache: dict[uuid.UUID, tuple[tuple[str, str], DatabaseAdapter]] = {}
        self._lock = threading.Lock()
        self._idle = idle_seconds
        self._max = max_adapters

    def register(self, db_type: DbType, factory: AdapterFactory) -> None:
        self._factories[db_type] = factory

    def supported(self) -> list[DbType]:
        return list(self._factories)

    def get(self, spec: ConnectionSpec, policy: NetworkPolicy, limits: Limits) -> DatabaseAdapter:
        factory = self._factories.get(spec.db_type)
        if factory is None:
            raise UnsupportedCapability(f"No adapter is registered for {spec.db_type}.")
        key = (spec.version_key, policy.fingerprint())
        stale: DatabaseAdapter | None = None
        with self._lock:
            hit = self._cache.get(spec.id)
            if hit and hit[0] == key:
                hit[1].touch()
                return hit[1]
            if hit:
                stale = hit[1]
            adapter = factory(spec, policy, limits)
            self._cache[spec.id] = (key, adapter)
            self._sweep_locked()
        if stale:
            self._close_quietly(stale)
        return adapter

    def invalidate(self, connection_id: uuid.UUID) -> None:
        with self._lock:
            hit = self._cache.pop(connection_id, None)
        if hit:
            self._close_quietly(hit[1])

    def _sweep_locked(self) -> None:
        now = time.monotonic()
        for cid in [c for c, (_, a) in self._cache.items() if now - a.last_used > self._idle]:
            self._close_quietly(self._cache.pop(cid)[1])
        # Bound the number of open adapters (and therefore pooled connections): evict the
        # least recently used. An evicted adapter is simply rebuilt on its next use.
        while len(self._cache) > self._max:
            oldest = min(self._cache, key=lambda c: self._cache[c][1].last_used)
            self._close_quietly(self._cache.pop(oldest)[1])

    def close_all(self) -> None:
        with self._lock:
            items = list(self._cache.values())
            self._cache.clear()
        for _, adapter in items:
            self._close_quietly(adapter)

    @staticmethod
    def _close_quietly(adapter: DatabaseAdapter) -> None:
        try:
            adapter.close()
        except Exception:
            logger.warning("adapter_close_failed", exc_info=True)


_manager: AdapterManager | None = None


def get_adapter_manager() -> AdapterManager:
    global _manager
    if _manager is None:
        from app.config import get_settings
        from app.database.adapters import register_default_adapters

        _manager = AdapterManager(
            get_settings().adapter_idle_seconds, get_settings().adapter_cache_max
        )
        register_default_adapters(_manager)
    return _manager


def reset_adapter_manager() -> None:
    global _manager
    if _manager is not None:
        _manager.close_all()
    _manager = None
