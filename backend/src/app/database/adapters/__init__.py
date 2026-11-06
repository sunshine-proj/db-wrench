"""Concrete engine adapters and their registration."""

from __future__ import annotations

from app.database.adapters.clickhouse import ClickHouseAdapter
from app.database.adapters.elasticsearch import ElasticsearchAdapter
from app.database.adapters.mariadb import MariaDBAdapter
from app.database.adapters.mongodb import MongoAdapter
from app.database.adapters.mssql import MSSQLAdapter
from app.database.adapters.mysql import MySQLAdapter
from app.database.adapters.postgres import PostgresAdapter
from app.database.adapters.redis import RedisAdapter
from app.database.adapters.sqlite import SQLiteAdapter
from app.database.core.base import DbType
from app.database.core.manager import AdapterManager


def register_default_adapters(manager: AdapterManager) -> None:
    manager.register(DbType.POSTGRESQL, PostgresAdapter)
    manager.register(DbType.MYSQL, MySQLAdapter)
    manager.register(DbType.MARIADB, MariaDBAdapter)
    manager.register(DbType.MSSQL, MSSQLAdapter)
    manager.register(DbType.CLICKHOUSE, ClickHouseAdapter)
    manager.register(DbType.SQLITE, SQLiteAdapter)
    manager.register(DbType.MONGODB, MongoAdapter)
    manager.register(DbType.REDIS, RedisAdapter)
    manager.register(DbType.ELASTICSEARCH, ElasticsearchAdapter)


def capabilities_for(db_type: DbType) -> dict[str, object]:
    cls = {
        DbType.POSTGRESQL: PostgresAdapter,
        DbType.MYSQL: MySQLAdapter,
        DbType.MARIADB: MariaDBAdapter,
        DbType.MSSQL: MSSQLAdapter,
        DbType.CLICKHOUSE: ClickHouseAdapter,
        DbType.SQLITE: SQLiteAdapter,
        DbType.MONGODB: MongoAdapter,
        DbType.REDIS: RedisAdapter,
        DbType.ELASTICSEARCH: ElasticsearchAdapter,
    }[db_type]
    return cls.capabilities.to_dict()
