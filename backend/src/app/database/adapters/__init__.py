"""Concrete engine adapters and their registration."""

from __future__ import annotations

from app.database.adapters.mariadb import MariaDBAdapter
from app.database.adapters.mongodb import MongoAdapter
from app.database.adapters.mysql import MySQLAdapter
from app.database.adapters.postgres import PostgresAdapter
from app.database.core.base import DbType
from app.database.core.manager import AdapterManager


def register_default_adapters(manager: AdapterManager) -> None:
    manager.register(DbType.POSTGRESQL, PostgresAdapter)
    manager.register(DbType.MYSQL, MySQLAdapter)
    manager.register(DbType.MARIADB, MariaDBAdapter)
    manager.register(DbType.MONGODB, MongoAdapter)


def capabilities_for(db_type: DbType) -> dict[str, object]:
    cls = {
        DbType.POSTGRESQL: PostgresAdapter,
        DbType.MYSQL: MySQLAdapter,
        DbType.MARIADB: MariaDBAdapter,
        DbType.MONGODB: MongoAdapter,
    }[db_type]
    return cls.capabilities.to_dict()
