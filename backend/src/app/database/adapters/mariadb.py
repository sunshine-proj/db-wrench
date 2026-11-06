"""MariaDB adapter.

MariaDB is *not* treated as MySQL (PRD §4): it speaks the same wire protocol, so this adapter
reuses the MySQL implementation, and overrides only what differs on a real server (verified
against 10.11 and 11.4):

* per-statement timeout is ``max_statement_time`` (seconds, covers every statement type), not
  ``max_execution_time``; the interrupted-query error is 1969;
* the read-only session variable is ``tx_read_only`` (``transaction_read_only`` exists from 11.x);
* ``JSON`` is an alias of ``LONGTEXT`` plus a ``json_valid`` CHECK constraint, so such columns are
  reported as ``json`` (otherwise the editor would refuse JSON values for them);
* ``mysql.user`` is a view without ``account_locked``; lock state and role flag are read from
  ``mysql.global_priv``; roles are not login accounts and are excluded from the user list.
"""

from __future__ import annotations

import re

from app.database.adapters.mysql import SYSTEM_DATABASES, MySQLAdapter
from app.database.core.base import Capabilities
from app.database.relational.models import TableStructure
from app.database.relational.principals import PrincipalInfo
from app.database.relational.sql_builder import MariaDBDialect

_TIMEOUT_CODES = {1969, 3024, 1317}  # max_statement_time exceeded / query interrupted


class MariaDBAdapter(MySQLAdapter):
    capabilities = Capabilities(
        family="relational",
        engine="mariadb",
        supports_schemas=False,  # a MariaDB "database" is the namespace
        supports_views=True,
        supports_foreign_keys=True,
        supports_keyset_pagination=True,
    )
    dialect = MariaDBDialect()
    _SYSTEM_DBS = SYSTEM_DATABASES
    _TIMEOUT_ERROR_CODES = _TIMEOUT_CODES
    _LABEL = "MariaDB"

    @staticmethod
    def _session_init(writable: bool, timeout_s: int) -> str:
        return (
            f"SET SESSION tx_read_only={0 if writable else 1}, "
            f"SESSION max_statement_time={int(timeout_s)}"
        )

    @staticmethod
    def _timeout_sql(timeout_s: int) -> str:
        return f"SET SESSION max_statement_time={int(timeout_s)}"

    def _load_structure(self, database: str, schema: str | None, table: str) -> TableStructure:
        st = super()._load_structure(database, schema, table)
        json_cols = {
            m.group(1)
            for c in st.constraints
            if c.kind == "check" and c.definition
            for m in re.finditer(r"json_valid\(`([^`]+)`\)", c.definition, re.IGNORECASE)
        }
        for col in st.columns:
            if col.name in json_cols and col.data_type.lower() in ("longtext", "text"):
                col.data_type = "json"
        return st

    def list_principals(self) -> list[PrincipalInfo]:
        _, rows = self._run_many(
            [
                (
                    "SELECT u.User, u.Host, JSON_VALUE(g.Priv, '$.account_locked'), "
                    "u.password_expired, u.plugin FROM mysql.user u "
                    "JOIN mysql.global_priv g ON g.User = u.User AND g.Host = u.Host "
                    "WHERE u.is_role = 'N' ORDER BY u.User, u.Host",
                    [],
                )
            ]
        )[0]
        return [
            PrincipalInfo(
                name=r[0],
                kind="user",
                can_login=str(r[2]) not in ("1", "true"),
                protected=r[0] in self._SYSTEM_USERS or r[0] == self.spec.username or not r[0],
                host=r[1],
                attributes={
                    "locked": str(r[2]) in ("1", "true"),
                    "password_expired": r[3] == "Y",
                    "plugin": r[4],
                },
            )
            for r in rows
        ]
